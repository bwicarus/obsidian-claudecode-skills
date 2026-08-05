import Foundation
import CryptoKit

enum ReaderLocalNoteFormat {
    static func contentHash(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map {
            String(format: "%02x", $0)
        }.joined()
    }

    static func markdown(
        name: String,
        text: String,
        sourceFile: String,
        sourcePage: Int
    ) -> String {
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return "# \(trimmedName)\n\n\(trimmedText)\(sourceLine(file: sourceFile, page: sourcePage))\n"
    }

    static func sanitizedFileBaseName(_ value: String) -> String {
        let forbidden = CharacterSet(charactersIn: "<>:\"/\\|?*")
            .union(.controlCharacters)
        let scalars = value.unicodeScalars.filter {
            !forbidden.contains($0)
        }
        var cleaned = String(String.UnicodeScalarView(scalars))
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "."))
        if cleaned.lowercased().hasSuffix(".md") {
            cleaned.removeLast(3)
            cleaned = cleaned.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
        }
        let candidate = cleaned.isEmpty ? "untitled" : cleaned
        var result = ""
        var byteCount = 0
        for character in candidate {
            let characterBytes = String(character).utf8.count
            if byteCount + characterBytes > 180 { break }
            result.append(character)
            byteCount += characterBytes
        }
        return result.isEmpty ? "untitled" : result
    }

    private static func sourceLine(file: String, page: Int) -> String {
        let safeFile = file
            .replacingOccurrences(of: "\r", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "]]", with: "]")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !safeFile.isEmpty else { return "" }
        let webValue = safeFile.hasPrefix("web:")
            ? String(safeFile.dropFirst(4))
            : safeFile
        if let url = URL(string: webValue),
           let scheme = url.scheme?.lowercased(),
           (scheme == "http" || scheme == "https"),
           url.user == nil,
           url.password == nil {
            let target = url.absoluteString.replacingOccurrences(
                of: ">",
                with: "%3E"
            )
            return "\n\n> 来源：[网页](<\(target)>)"
        }
        if page > 0 {
            return "\n\n> 来源：[[\(safeFile)#page=\(page)]]"
        }
        return "\n\n> 来源：[[\(safeFile)]]"
    }
}

struct ReaderLocalNoteCreateRequest: Codable, Equatable, Identifiable, Sendable {
    static let schema = 2

    let schema: Int
    let id: String
    let name: String
    let text: String
    let sourceFile: String
    let sourcePage: Int
    let vaultGeneration: String
    let createdAtMilliseconds: Int64

    init?(
        id: String,
        name: String,
        text: String,
        sourceFile: String,
        sourcePage: Int,
        vaultGeneration: String,
        now: Date = Date()
    ) {
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            ReaderNativeBridgeContract.isSafeRequestID(id),
            !trimmedName.isEmpty,
            !trimmedText.isEmpty,
            trimmedName.utf8.count <= 512,
            trimmedText.utf8.count <= 262_144,
            sourceFile.utf8.count <= 8_192,
            (0...10_000_000).contains(sourcePage),
            ReaderNativeBridgeContract.isSafeRequestID(vaultGeneration)
        else {
            return nil
        }
        schema = Self.schema
        self.id = id
        self.name = trimmedName
        self.text = trimmedText
        self.sourceFile = sourceFile
        self.sourcePage = sourcePage
        self.vaultGeneration = vaultGeneration
        createdAtMilliseconds = Int64(now.timeIntervalSince1970 * 1_000)
    }

    var markdown: String {
        ReaderLocalNoteFormat.markdown(
            name: name,
            text: text,
            sourceFile: sourceFile,
            sourcePage: sourcePage
        )
    }

    var contentHash: String {
        ReaderLocalNoteFormat.contentHash(markdown)
    }

    var desiredFileName: String {
        "\(ReaderLocalNoteFormat.sanitizedFileBaseName(name)).md"
    }

    var projection: ReaderLocalNoteProjection {
        ReaderLocalNoteProjection(
            id: id,
            title: name,
            fileName: desiredFileName,
            content: markdown,
            sourceFile: sourceFile,
            sourcePage: sourcePage,
            pendingExport: true,
            now: Date(
                timeIntervalSince1970:
                    TimeInterval(createdAtMilliseconds) / 1_000
            )
        )
    }

    func hasSamePayload(as other: ReaderLocalNoteCreateRequest) -> Bool {
        name == other.name &&
            text == other.text &&
            sourceFile == other.sourceFile &&
            sourcePage == other.sourcePage &&
            vaultGeneration == other.vaultGeneration
    }
}

enum ReaderLocalNoteOutboxError: LocalizedError {
    case requestConflict
    case queueFull

    var errorDescription: String? {
        switch self {
        case .requestConflict:
            return "本机笔记请求号与已有请求冲突"
        case .queueFull:
            return "本机笔记待同步队列已满，请先打开 BWReader App 完成同步"
        }
    }
}

struct ReaderLocalNoteOutboxStore: Sendable {
    private static let directoryName = "NativeFeatures/LocalNoteOutbox"
    private static let maximumPendingCount = 200

    private func requireDirectory() throws -> URL {
        guard let groupURL = FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier:
                ReaderNativeBridgeContract.appGroupIdentifier
        ) else {
            throw CocoaError(.fileNoSuchFile)
        }
        let directory = groupURL.appendingPathComponent(
            Self.directoryName,
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        return directory
    }

    private func requestURL(id: String) throws -> URL {
        guard ReaderNativeBridgeContract.isSafeRequestID(id) else {
            throw ReaderLocalNoteOutboxError.requestConflict
        }
        return try requireDirectory().appendingPathComponent(
            "\(id).json",
            isDirectory: false
        )
    }

    func pending() throws -> [ReaderLocalNoteCreateRequest] {
        let directory = try requireDirectory()
        let urls = try FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ).filter { $0.pathExtension.lowercased() == "json" }
        let decoder = JSONDecoder()
        return urls.compactMap {
            guard let data = try? Data(contentsOf: $0),
                  let request = try? decoder.decode(
                    ReaderLocalNoteCreateRequest.self,
                    from: data
                  )
            else {
                // A process may disappear between enumeration and read. A
                // single incomplete record must never block the whole queue.
                return nil
            }
            return request
        }.filter {
            $0.schema == ReaderLocalNoteCreateRequest.schema &&
                ReaderNativeBridgeContract.isSafeRequestID($0.id)
        }.sorted {
            if $0.createdAtMilliseconds == $1.createdAtMilliseconds {
                return $0.id < $1.id
            }
            return $0.createdAtMilliseconds < $1.createdAtMilliseconds
        }
    }

    @discardableResult
    func enqueue(
        _ request: ReaderLocalNoteCreateRequest
    ) throws -> ReaderLocalNoteCreateRequest {
        let existing = try pending()
        if let sameID = existing.first(where: { $0.id == request.id }) {
            guard sameID.hasSamePayload(as: request) else {
                throw ReaderLocalNoteOutboxError.requestConflict
            }
            return sameID
        }
        if let samePayload = existing.first(where: {
            $0.hasSamePayload(as: request)
        }) {
            return samePayload
        }
        guard existing.count < Self.maximumPendingCount else {
            throw ReaderLocalNoteOutboxError.queueFull
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(request)
        let target = try requestURL(id: request.id)
        let temporary = try requireDirectory().appendingPathComponent(
            ".\(request.id)-\(UUID().uuidString).tmp",
            isDirectory: false
        )
        defer { try? FileManager.default.removeItem(at: temporary) }
        do {
            try data.write(to: temporary, options: [.atomic])
            try FileManager.default.moveItem(at: temporary, to: target)
            return request
        } catch {
            if let stored = try? JSONDecoder().decode(
                ReaderLocalNoteCreateRequest.self,
                from: Data(contentsOf: target)
            ), stored.hasSamePayload(as: request) {
                return stored
            }
            throw error
        }
    }

    func remove(id: String) throws {
        let target = try requestURL(id: id)
        guard FileManager.default.fileExists(atPath: target.path) else {
            return
        }
        try FileManager.default.removeItem(at: target)
    }
}

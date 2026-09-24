import Foundation
import ZIPFoundation

/// One file-backed archive, read off the main actor. No whole-book Data, JSZip
/// image or extracted directory is retained. Existing chapter/anchor parsing
/// consumes the original entry bytes until its native migration is complete.
actor ReaderNativeEPUBArchive {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    private var identity = ""
    private var source: URL?
    private var archive: Archive?
    private var entries: [Data: Entry] = [:]
    private var catalog: [[String: Any]] = []
    private var publication: [String: Any]?

    static func canonicalPath(_ raw: String) -> String? {
        let path = raw.replacingOccurrences(of: "\\", with: "/")
        guard !path.isEmpty, !path.hasPrefix("/"), !path.contains("\0"),
              path.range(of: "^[A-Za-z][A-Za-z0-9+.-]*:", options: .regularExpression) == nil else { return nil }
        var parts: [Substring] = []
        for part in path.split(separator: "/") where part != "." {
            if part == ".." { guard !parts.isEmpty else { return nil }; parts.removeLast() }
            else { parts.append(part) }
        }
        return parts.isEmpty ? nil : parts.joined(separator: "/")
    }

    /// Check the same bounded ZIP envelope as the previous App loader before
    /// a library allocates entry metadata. The library owns decompression.
    private static func checkEnvelope(_ url: URL) throws -> Int {
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        let length = try file.seekToEnd()
        guard length >= 22, length <= 512 * 1024 * 1024 else { throw Failure(message: "EPUB 文件大小无效") }
        let tailSize = Int(min(length, 65557))
        try file.seek(toOffset: length - UInt64(tailSize))
        guard let tail = try file.read(upToCount: tailSize), tail.count == tailSize else { throw Failure(message: "EPUB 中央目录缺失") }
        func word(_ data: Data, _ offset: Int, _ count: Int) -> UInt64 {
            (0..<count).reduce(0) { $0 | (UInt64(data[offset + $1]) << (8 * $1)) }
        }
        var end: Int?
        for offset in stride(from: tail.count - 22, through: 0, by: -1) {
            if word(tail, offset, 4) == 0x06054b50,
               offset + 22 + Int(word(tail, offset + 20, 2)) == tail.count { end = offset; break }
        }
        guard let end, word(tail, end + 4, 2) == 0, word(tail, end + 6, 2) == 0 else {
            throw Failure(message: "EPUB 中央目录无效或为分卷 ZIP")
        }
        let count = word(tail, end + 10, 2), size = word(tail, end + 12, 4), offset = word(tail, end + 16, 4)
        guard count == word(tail, end + 8, 2), count <= 10000, size <= 64 * 1024 * 1024,
              offset + size == length - UInt64(tailSize) + UInt64(end) else {
            throw Failure(message: "EPUB 中央目录超过限制或为 ZIP64")
        }
        try file.seek(toOffset: offset)
        guard let directory = try file.read(upToCount: Int(size)), directory.count == Int(size) else {
            throw Failure(message: "EPUB 中央目录不完整")
        }
        var cursor = 0, actual = 0
        while cursor < directory.count {
            guard cursor + 46 <= directory.count, word(directory, cursor, 4) == 0x02014b50 else {
                throw Failure(message: "EPUB 中央目录结构无效")
            }
            cursor += 46 + Int(word(directory, cursor + 28, 2) + word(directory, cursor + 30, 2) + word(directory, cursor + 32, 2))
            actual += 1
            guard cursor <= directory.count, actual <= 10000 else { throw Failure(message: "EPUB 文件项过多") }
        }
        guard cursor == directory.count, actual == Int(count) else { throw Failure(message: "EPUB 文件计数不一致") }
        return actual
    }

    private func open(_ url: URL, identity nextIdentity: String) throws {
        if archive != nil, source == url, identity == nextIdentity { return }
        archive = nil; entries = [:]; catalog = []; publication = nil; source = nil; identity = ""
        let count = try Self.checkEnvelope(url)
        let candidate = try Archive(url: url, accessMode: .read)
        var records: [[String: Any]] = [], found: [Data: Entry] = [:], total: UInt64 = 0
        for entry in candidate {
            try Task.checkCancellation()
            guard records.count < 10000, let path = Self.canonicalPath(entry.path),
                  found[Data(path.utf8)] == nil, entry.type != .symlink else {
                throw Failure(message: "EPUB 存在重复、不安全路径或符号链接")
            }
            let size = entry.uncompressedSize, compressed = entry.compressedSize
            if entry.type != .directory {
                guard size <= 128 * 1024 * 1024,
                      !(size > 1024 * 1024 && compressed > 0 && Double(size) / Double(compressed) > 200) else {
                    throw Failure(message: "EPUB 单项大小或压缩比超过限制")
                }
                total += size
                guard total <= 1024 * 1024 * 1024 else { throw Failure(message: "EPUB 解压后过大") }
            }
            found[Data(path.utf8)] = entry
            records.append(["name": entry.path, "path": path, "directory": entry.type == .directory,
                            "size": size, "compressedSize": compressed])
        }
        guard records.count == count else { throw Failure(message: "EPUB 存在无法读取的文件项") }
        archive = candidate; entries = found; catalog = records; source = url; identity = nextIdentity
    }

    func list(url: URL, identity: String) throws -> [[String: Any]] {
        try open(url, identity: identity); return catalog
    }

    func describe(url: URL, identity: String) throws -> [String: Any] {
        try open(url, identity: identity)
        if let publication { return publication }
        let available = Set(entries.filter { $0.value.type == .file }.keys)
        let result = try ReaderNativeEPUBPublication.load(available: available) { path in
            try read(url: url, identity: identity, path: path, maximumBytes: 8 * 1024 * 1024)
        }
        publication = result
        return result
    }

    func read(url: URL, identity: String, path: String, maximumBytes: Int) throws -> Data {
        guard [8 * 1024 * 1024, 32 * 1024 * 1024].contains(maximumBytes),
              Self.canonicalPath(path)?.utf8.elementsEqual(path.utf8) == true else { throw Failure(message: "EPUB 读取参数无效") }
        try open(url, identity: identity)
        guard let archive, let entry = entries[Data(path.utf8)], entry.type == .file,
              entry.uncompressedSize <= UInt64(maximumBytes) else { throw Failure(message: "EPUB 文件项不存在或超过读取上限") }
        var data = Data()
        let checksum = try archive.extract(entry, bufferSize: 64 * 1024) { chunk in
            try Task.checkCancellation()
            guard chunk.count <= maximumBytes - data.count else { throw Failure(message: "EPUB 实际解压大小超过限制") }
            data.append(chunk)
        }
        guard data.count == Int(entry.uncompressedSize), checksum == entry.checksum else {
            throw Failure(message: "EPUB 文件项校验失败")
        }
        return data
    }
}

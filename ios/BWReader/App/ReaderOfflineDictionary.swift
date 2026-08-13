import CryptoKit
import Foundation
import SwiftUI

enum ReaderOfflineDictionaryError: LocalizedError {
    case notInstalled
    case sourceUnavailable(String)
    case invalidManifest
    case invalidResource(String)
    case storageUnavailable(String)

    var errorDescription: String? {
        switch self {
        case .notInstalled:
            return "本机离线词典尚未下载"
        case .sourceUnavailable(let detail):
            return "下载离线词典失败：\(detail)"
        case .invalidManifest:
            return "下载的离线词典清单不受信任或版本不兼容"
        case .invalidResource(let path):
            return "离线词典资源校验失败：\(path)"
        case .storageUnavailable(let detail):
            return "无法保存离线词典：\(detail)"
        }
    }
}

struct ReaderOfflineDictionaryInfo: Equatable, Sendable {
    let release: String
    let byteCount: Int64
    let installedAt: Date
}

/// Owns only the App-private, user-requested dictionary bytes. The immutable
/// ReaderBundle, Safari extension, books, App Group, Pi sync and registry data
/// never point at this directory.
enum ReaderOfflineDictionaryStore {
    static let manifestContract = "bw-jmdict-manifest/2"
    static let shardContract = "bw-jmdict-shard/2"
    static let shardAlgorithm = "utf8-prefix-2-kana-3/1"
    static let sourceRelease = "3.6.2+20260810124713"
    static let sourceDigest =
        "e6802135b445627a8f09c544bf8c32c3d344515f6e95a473e8bd39e09ad00109"
    static let sourceRevision =
        "65a740870120e673f386d7f38994a215f072ff51"
    static let manifestDigest =
        "c3a16cd3715a5a1d481cd9634bafd72e8011ad0f6336a847ab955720ddb75add"
    static let datasetID = "jmdict-3.6.2-20260810124713-zhwiktionary-v2"
    static let installContract = "bw-offline-dictionary-install/1"
    static let sourceBaseURL = URL(
        string: "https://raw.githubusercontent.com/bwicarus/obsidian-claudecode-skills/\(sourceRevision)/ios/BWReader/DictionaryData/"
    )!

    struct Manifest: Decodable, Sendable {
        struct Source: Decodable, Sendable {
            let release: String
            let sha256: String
        }

        struct ChineseSource: Decodable, Sendable {
            let release: String
            let sha256: String
        }

        struct Resource: Decodable, Sendable {
            let path: String
            let sha256: String
            let bytes: Int64?
        }

        struct Shard: Decodable, Sendable {
            let path: String
            let sha256: String
            let bytes: Int64
        }

        let contract: String
        let normalization: String
        let shardAlgorithm: String
        let source: Source
        let chineseSource: ChineseSource
        let license: Resource
        let chineseLicense: Resource
        let shards: [String: Shard]
    }

    private struct InstallMarker: Codable {
        let contract: String
        let dataset: String
        let manifestSHA256: String
        let bytes: Int64
        let installedAt: Date
    }

    static func applicationRoot() throws -> URL {
        do {
            let support = try FileManager.default.url(
                for: .applicationSupportDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            )
            return support
                .appendingPathComponent("BWReader", isDirectory: true)
                .appendingPathComponent(
                    "OfflineJapaneseDictionary",
                    isDirectory: true
                )
        } catch {
            throw ReaderOfflineDictionaryError.storageUnavailable(
                error.localizedDescription
            )
        }
    }

    static func releaseRoot() throws -> URL {
        try applicationRoot()
            .appendingPathComponent("releases", isDirectory: true)
            .appendingPathComponent(datasetID, isDirectory: true)
    }

    static func partialRoot() throws -> URL {
        try applicationRoot()
            .appendingPathComponent("downloads", isDirectory: true)
            .appendingPathComponent("\(datasetID).partial", isDirectory: true)
    }

    static func prepareApplicationRoot() throws {
        var root = try applicationRoot()
        try FileManager.default.createDirectory(
            at: root,
            withIntermediateDirectories: true
        )
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try root.setResourceValues(values)
    }

    static func decodeAndValidateManifest(_ data: Data) throws -> Manifest {
        guard sha256Hex(data) == manifestDigest,
              let manifest = try? JSONDecoder().decode(Manifest.self, from: data),
              manifest.contract == manifestContract,
              manifest.normalization == "NFC",
              manifest.shardAlgorithm == shardAlgorithm,
              manifest.source.release == sourceRelease,
              manifest.source.sha256 == sourceDigest,
              manifest.chineseSource.release
                == "zhwiktionary-ja-605256f3b7fc",
              manifest.chineseSource.sha256
                == "605256f3b7fc73337b9b9d47612ab27477cff92c230dfc2c900545d52de1c63c",
              manifest.license.path == "LICENSE-JMdict.txt",
              manifest.chineseLicense.path
                == "LICENSE-ZhWiktionary.txt",
              !manifest.shards.isEmpty else {
            throw ReaderOfflineDictionaryError.invalidManifest
        }
        for (key, shard) in manifest.shards {
            guard key.range(of: #"^[a-f0-9]{2,6}$"#, options: .regularExpression) != nil,
                  shard.path == "shards/\(key).json",
                  shard.bytes > 0,
                  isLowercaseSHA256(shard.sha256) else {
                throw ReaderOfflineDictionaryError.invalidManifest
            }
        }
        guard isLowercaseSHA256(manifest.license.sha256),
              isLowercaseSHA256(manifest.chineseLicense.sha256) else {
            throw ReaderOfflineDictionaryError.invalidManifest
        }
        return manifest
    }

    static func installedInfo() throws -> ReaderOfflineDictionaryInfo? {
        let root = try releaseRoot()
        let markerURL = root.appendingPathComponent("installed.json")
        let manifestURL = root.appendingPathComponent("manifest.json")
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        guard let markerData = try? Data(contentsOf: markerURL),
              let marker = try? decoder.decode(InstallMarker.self, from: markerData),
              marker.contract == installContract,
              marker.dataset == datasetID,
              marker.manifestSHA256 == manifestDigest,
              marker.bytes > 0,
              let manifestData = try? Data(
                contentsOf: manifestURL,
                options: .mappedIfSafe
              ),
              (try? decodeAndValidateManifest(manifestData)) != nil else {
            return nil
        }
        return ReaderOfflineDictionaryInfo(
            release: sourceRelease,
            byteCount: marker.bytes,
            installedAt: marker.installedAt
        )
    }

    static func readRuntimeResource(relative: String) throws -> Data {
        guard relative == "manifest.json"
                || relative.range(
                    of: #"^shards/[a-f0-9]{2,6}\.json$"#,
                    options: .regularExpression
                ) != nil else {
            throw ReaderOfflineDictionaryError.invalidResource(relative)
        }
        guard try installedInfo() != nil else {
            throw ReaderOfflineDictionaryError.notInstalled
        }
        let root = try releaseRoot()
        let manifestData = try Data(
            contentsOf: root.appendingPathComponent("manifest.json"),
            options: .mappedIfSafe
        )
        let manifest = try decodeAndValidateManifest(manifestData)
        if relative == "manifest.json" {
            return manifestData
        }
        let key = String(relative.dropFirst("shards/".count).dropLast(".json".count))
        guard let metadata = manifest.shards[key] else {
            throw ReaderOfflineDictionaryError.invalidResource(relative)
        }
        let fileURL = root
            .appendingPathComponent("shards", isDirectory: true)
            .appendingPathComponent("\(key).json", isDirectory: false)
        guard let data = try? Data(contentsOf: fileURL, options: .mappedIfSafe),
              data.count == Int(metadata.bytes),
              sha256Hex(data) == metadata.sha256 else {
            throw ReaderOfflineDictionaryError.invalidResource(relative)
        }
        return data
    }

    static func writeInstallMarker(
        at root: URL,
        bytes: Int64,
        installedAt: Date
    ) throws {
        let marker = InstallMarker(
            contract: installContract,
            dataset: datasetID,
            manifestSHA256: manifestDigest,
            bytes: bytes,
            installedAt: installedAt
        )
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(marker)
        try data.write(
            to: root.appendingPathComponent("installed.json"),
            options: .atomic
        )
    }

    static func remoteURL(relative: String) throws -> URL {
        guard relative == "manifest.json"
                || relative == "LICENSE-JMdict.txt"
                || relative == "LICENSE-ZhWiktionary.txt"
                || relative.range(
                    of: #"^shards/[a-f0-9]{2,6}\.json$"#,
                    options: .regularExpression
                ) != nil else {
            throw ReaderOfflineDictionaryError.invalidResource(relative)
        }
        return relative.split(separator: "/").reduce(sourceBaseURL) {
            $0.appendingPathComponent(String($1), isDirectory: false)
        }
    }

    static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func isLowercaseSHA256(_ value: String) -> Bool {
        value.count == 64 && value.allSatisfy {
            $0.isNumber || ("a"..."f").contains(String($0))
        }
    }

    static func removeAll() throws {
        let root = try applicationRoot()
        if FileManager.default.fileExists(atPath: root.path) {
            try FileManager.default.removeItem(at: root)
        }
    }
}

private enum ReaderOfflineDictionaryInstaller {
    private struct Asset: Sendable {
        let relative: String
        let bytes: Int64
        let sha256: String
    }

    static func install(
        progress: @escaping @Sendable (Int64, Int64) async -> Void
    ) async throws -> ReaderOfflineDictionaryInfo {
        try Task.checkCancellation()
        try ReaderOfflineDictionaryStore.prepareApplicationRoot()

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 180
        configuration.waitsForConnectivity = true
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }

        let manifestURL = try ReaderOfflineDictionaryStore.remoteURL(
            relative: "manifest.json"
        )
        let manifestData = try await fetch(
            url: manifestURL,
            session: session,
            label: "manifest.json"
        )
        let manifest = try ReaderOfflineDictionaryStore.decodeAndValidateManifest(
            manifestData
        )

        let licenseBytes = manifest.license.bytes ?? 0
        let chineseLicenseBytes = manifest.chineseLicense.bytes ?? 0
        var assets = [
            Asset(
                relative: manifest.license.path,
                bytes: licenseBytes,
                sha256: manifest.license.sha256
            ),
            Asset(
                relative: manifest.chineseLicense.path,
                bytes: chineseLicenseBytes,
                sha256: manifest.chineseLicense.sha256
            ),
        ]
        assets.append(contentsOf: manifest.shards.sorted(by: { $0.key < $1.key }).map {
            Asset(
                relative: $0.value.path,
                bytes: $0.value.bytes,
                sha256: $0.value.sha256
            )
        })
        let totalBytes = Int64(manifestData.count)
            + assets.reduce(Int64(0)) { $0 + $1.bytes }

        let staging = try ReaderOfflineDictionaryStore.partialRoot()
        if let existingManifest = try? Data(
            contentsOf: staging.appendingPathComponent("manifest.json"),
            options: .mappedIfSafe
        ), ReaderOfflineDictionaryStore.sha256Hex(existingManifest)
            != ReaderOfflineDictionaryStore.manifestDigest {
            try? FileManager.default.removeItem(at: staging)
        }
        try FileManager.default.createDirectory(
            at: staging.appendingPathComponent("shards", isDirectory: true),
            withIntermediateDirectories: true
        )
        try manifestData.write(
            to: staging.appendingPathComponent("manifest.json"),
            options: .atomic
        )

        var completedBytes = Int64(manifestData.count)
        await progress(completedBytes, totalBytes)
        var nextIndex = 0
        try await withThrowingTaskGroup(of: Int64.self) { group in
            let concurrency = min(4, assets.count)
            for _ in 0..<concurrency {
                let asset = assets[nextIndex]
                nextIndex += 1
                group.addTask {
                    try await installAsset(
                        asset,
                        staging: staging,
                        session: session
                    )
                }
            }
            while let completed = try await group.next() {
                completedBytes += completed
                await progress(completedBytes, totalBytes)
                if nextIndex < assets.count {
                    let asset = assets[nextIndex]
                    nextIndex += 1
                    group.addTask {
                        try await installAsset(
                            asset,
                            staging: staging,
                            session: session
                        )
                    }
                }
            }
        }

        let installedAt = Date()
        try ReaderOfflineDictionaryStore.writeInstallMarker(
            at: staging,
            bytes: totalBytes,
            installedAt: installedAt
        )
        let destination = try ReaderOfflineDictionaryStore.releaseRoot()
        try FileManager.default.createDirectory(
            at: destination.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if FileManager.default.fileExists(atPath: destination.path) {
            try FileManager.default.removeItem(at: destination)
        }
        try FileManager.default.moveItem(at: staging, to: destination)

        let releases = destination.deletingLastPathComponent()
        if let children = try? FileManager.default.contentsOfDirectory(
            at: releases,
            includingPropertiesForKeys: nil
        ) {
            for child in children where child.lastPathComponent
                != ReaderOfflineDictionaryStore.datasetID {
                try? FileManager.default.removeItem(at: child)
            }
        }
        return ReaderOfflineDictionaryInfo(
            release: ReaderOfflineDictionaryStore.sourceRelease,
            byteCount: totalBytes,
            installedAt: installedAt
        )
    }

    private static func installAsset(
        _ asset: Asset,
        staging: URL,
        session: URLSession
    ) async throws -> Int64 {
        try Task.checkCancellation()
        let target = asset.relative.split(separator: "/").reduce(staging) {
            $0.appendingPathComponent(String($1), isDirectory: false)
        }
        if let existing = try? Data(contentsOf: target, options: .mappedIfSafe),
           existing.count == Int(asset.bytes),
           ReaderOfflineDictionaryStore.sha256Hex(existing) == asset.sha256 {
            return asset.bytes
        }
        let remote = try ReaderOfflineDictionaryStore.remoteURL(
            relative: asset.relative
        )
        let data = try await fetch(
            url: remote,
            session: session,
            label: asset.relative
        )
        guard data.count == Int(asset.bytes),
              ReaderOfflineDictionaryStore.sha256Hex(data) == asset.sha256 else {
            throw ReaderOfflineDictionaryError.invalidResource(asset.relative)
        }
        try data.write(to: target, options: .atomic)
        return asset.bytes
    }

    private static func fetch(
        url: URL,
        session: URLSession,
        label: String
    ) async throws -> Data {
        do {
            let (data, response) = try await session.data(from: url)
            guard let http = response as? HTTPURLResponse,
                  http.statusCode == 200 else {
                let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                throw ReaderOfflineDictionaryError.sourceUnavailable(
                    "\(label) HTTP \(status)"
                )
            }
            return data
        } catch is CancellationError {
            throw CancellationError()
        } catch let error as ReaderOfflineDictionaryError {
            throw error
        } catch {
            throw ReaderOfflineDictionaryError.sourceUnavailable(
                "\(label)：\(error.localizedDescription)"
            )
        }
    }
}

@MainActor
final class ReaderOfflineDictionaryManager: ObservableObject {
    enum State: Equatable {
        case notInstalled
        case downloading
        case installed(ReaderOfflineDictionaryInfo)
        case failed(String)
    }

    static let shared = ReaderOfflineDictionaryManager()

    @Published private(set) var state: State = .notInstalled
    @Published private(set) var completedBytes: Int64 = 0
    @Published private(set) var totalBytes: Int64 = 0
    private var downloadTask: Task<Void, Never>?

    private init() {
        refresh()
    }

    var isDownloading: Bool {
        state == .downloading
    }

    var isInstalled: Bool {
        if case .installed = state { return true }
        return false
    }

    var progress: Double {
        guard totalBytes > 0 else { return 0 }
        return min(1, Double(completedBytes) / Double(totalBytes))
    }

    var statusText: String {
        switch state {
        case .notInstalled:
            return "未下载"
        case .downloading:
            return "正在下载 \(Self.bytes(completedBytes)) / \(Self.bytes(totalBytes))"
        case .installed(let info):
            return "已下载 · \(Self.bytes(info.byteCount))"
        case .failed(let message):
            return "下载失败：\(message)"
        }
    }

    func refresh() {
        guard !isDownloading else { return }
        do {
            if let info = try ReaderOfflineDictionaryStore.installedInfo() {
                state = .installed(info)
                completedBytes = info.byteCount
                totalBytes = info.byteCount
            } else {
                state = .notInstalled
                completedBytes = 0
                totalBytes = 0
            }
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    func download() {
        guard downloadTask == nil else { return }
        state = .downloading
        completedBytes = 0
        totalBytes = 0
        downloadTask = Task { [weak self] in
            do {
                let info = try await ReaderOfflineDictionaryInstaller.install {
                    [weak self] completed, total in
                    await MainActor.run {
                        self?.completedBytes = completed
                        self?.totalBytes = total
                    }
                }
                guard !Task.isCancelled else { return }
                self?.state = .installed(info)
                self?.completedBytes = info.byteCount
                self?.totalBytes = info.byteCount
            } catch is CancellationError {
                self?.state = .notInstalled
            } catch {
                self?.state = .failed(error.localizedDescription)
            }
            self?.downloadTask = nil
        }
    }

    func removeDownloadedDictionary() {
        guard downloadTask == nil else { return }
        do {
            try ReaderOfflineDictionaryStore.removeAll()
            state = .notInstalled
            completedBytes = 0
            totalBytes = 0
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    private static func bytes(_ value: Int64) -> String {
        guard value > 0 else { return "0 MB" }
        return ByteCountFormatter.string(
            fromByteCount: value,
            countStyle: .file
        )
    }
}

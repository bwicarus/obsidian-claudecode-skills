import CryptoKit
import Foundation

// 离线日语词典的**只读核心**：存储路径、清单校验、安装状态与资源读取。
//
// 为什么在 Shared/ 而不是 App/（2026-08-19，C 组 #19）：
//   Safari 扩展要用 App 的离线词典查词 —— 那是扩展打 Pi 最频繁的一类请求
//   （每点一个词一次）。而 Safari 的 native handler 跑在**扩展进程**里，编译的是
//   BWReaderSafariExtension target 的源码集合；读取代码留在 App/ 的话，
//   那个 target 根本编译不到它。
//
//   下载与安装（ReaderOfflineDictionaryInstaller）留在 App/：它用 URLSession
//   拉几百 MB，扩展既不需要也不该有这个能力。依赖是单向的 —— Store 不引用
//   Installer，只有 Manager 引用它，所以这条边界切得干净。
//
// 同一个模式的先例：ReaderRealtimeCredentialCore.swift。

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
    static let manifestContract = "bw-jmdict-manifest/3"
    static let shardContract = "bw-jmdict-shard/3"
    static let shardAlgorithm = "utf8-prefix-2-kana-3/1"
    static let sourceRelease = "3.6.2+20260810124713"
    static let sourceDigest =
        "e6802135b445627a8f09c544bf8c32c3d344515f6e95a473e8bd39e09ad00109"
    static let sourceRevision =
        "9ecb02ab0c302d59d71a5e86ffeb933c5f2aa0e2"
    static let manifestDigest =
        "1c719097c9a3feb51b3bd088dbb009e40e98ba6dc097b9b60cb8b56a14b86825"
    static let datasetID = "jmdict-3.6.2-20260810124713-rich-zh-v3"
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
        let tanakaSource: ChineseSource
        let kanjidicSource: ChineseSource
        let license: Resource
        let chineseLicense: Resource
        let tanakaLicense: Resource
        let resources: [String: Resource]
        let shards: [String: Shard]
    }

    private struct InstallMarker: Codable {
        let contract: String
        let dataset: String
        let manifestSHA256: String
        let bytes: Int64
        let installedAt: Date
    }

    /// 词典的根目录 —— 在 **App Group 共享容器**里。
    ///
    /// 2026-08-19 从 App 私有的 Application Support 搬过来。原因是 C19：
    /// Safari 扩展要用 App 的离线词典查词（每点一个词一次，是扩展打 Pi 最频繁的
    /// 那类请求），而 Safari 的 native handler 跑在**扩展进程**里 ——
    /// `applicationSupportDirectory` 在那儿解析出的是扩展自己的目录，
    /// 词典根本不在那条路径上。共享容器是两个进程都看得见的唯一地方。
    ///
    /// 快照与本机笔记状态早就在用同一个 App Group，这里只是加入它们。
    static func applicationRoot() throws -> URL {
        guard let container = FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier:
                ReaderNativeBridgeContract.appGroupIdentifier
        ) else {
            throw ReaderOfflineDictionaryError.storageUnavailable(
                "App Group 共享容器不可用"
            )
        }
        let root = container
            .appendingPathComponent("BWReader", isDirectory: true)
            .appendingPathComponent(
                "OfflineJapaneseDictionary",
                isDirectory: true
            )
        migrateLegacyApplicationRootIfNeeded(to: root)
        return root
    }

    /// 旧位置（App 私有 Application Support）里已经装好的词典搬过来。
    ///
    /// 不迁移的话，升级这一版之后 `installedInfo()` 会返回 nil、UI 显示"未安装"，
    /// 用户得重下几百 MB —— 而那几百 MB 就在磁盘上、只是换了个地方找。
    ///
    /// 用 moveItem 而不是 copy：复制会在迁移期间占双份空间。移动失败就当没迁移过
    /// （旧目录留在原地，下次再试），**不删任何东西** —— 迁移出错时宁可多占一份
    /// 空间，也不能把用户下好的词典弄丢。
    private static func migrateLegacyApplicationRootIfNeeded(to target: URL) {
        let manager = FileManager.default
        guard !manager.fileExists(atPath: target.path) else { return }
        guard let support = try? manager.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: false
        ) else { return }
        let legacy = support
            .appendingPathComponent("BWReader", isDirectory: true)
            .appendingPathComponent(
                "OfflineJapaneseDictionary",
                isDirectory: true
            )
        guard manager.fileExists(atPath: legacy.path) else { return }
        do {
            try manager.createDirectory(
                at: target.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try manager.moveItem(at: legacy, to: target)
        } catch {
            // 迁移失败不是致命的：下面的安装检查会认为"未安装"，用户可以重新下载。
            // 但要留痕 —— 否则"我明明下过"就成了一桩无法解释的怪事。
            NSLog("[bw-dict] 词典迁移到共享容器失败：%@", error.localizedDescription)
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
                == "2026.07.15",
              manifest.chineseSource.sha256
                == "3ef3022e6b9310c1bc8c82af5a27d273e73b60ae5a0e500d7bc424535ede8938",
              manifest.tanakaSource.sha256
                == "a5d50104737e9ab1ff40324c6d6f0bc9be32541942fd6119bea001c1b47570aa",
              manifest.kanjidicSource.sha256
                == "05c10cb87dc109e087f6e99c95a8fb8dd02705cbd0e86130ba0e80bf8db7fa26",
              manifest.license.path == "LICENSE-JMdict.txt",
              manifest.chineseLicense.path
                == "LICENSE-ZhWiktionary.txt",
              manifest.tanakaLicense.path == "LICENSE-Tanaka.txt",
              manifest.resources["kanji.json"]?.path == "kanji.json",
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
              isLowercaseSHA256(manifest.chineseLicense.sha256),
              isLowercaseSHA256(manifest.tanakaLicense.sha256),
              isLowercaseSHA256(manifest.resources["kanji.json"]?.sha256 ?? ""),
              (manifest.resources["kanji.json"]?.bytes ?? 0) > 0 else {
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
        guard relative == "manifest.json" || relative == "kanji.json"
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
        if relative == "kanji.json" {
            guard let metadata = manifest.resources[relative] else {
                throw ReaderOfflineDictionaryError.invalidResource(relative)
            }
            let fileURL = root.appendingPathComponent(relative)
            guard let data = try? Data(contentsOf: fileURL, options: .mappedIfSafe),
                  data.count == Int(metadata.bytes ?? 0),
                  sha256Hex(data) == metadata.sha256 else {
                throw ReaderOfflineDictionaryError.invalidResource(relative)
            }
            return data
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
                || relative == "LICENSE-Tanaka.txt"
                || relative == "kanji.json"
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

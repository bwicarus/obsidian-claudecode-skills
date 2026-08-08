import CryptoKit
import Foundation

enum ReaderBookUserStateDomainName: String, Codable, CaseIterable, Sendable {
    case readingPosition = "reading-position"
    case highlights
    case ink
    case closedRegions = "closed-regions"
    case notes
    case userPages = "user-pages"
    case cardPlacements = "card-placements"
    case entityReferences = "entity-references"
}

struct ReaderBookUserStateDomainPayload: Codable, Hashable, Sendable {
    let name: ReaderBookUserStateDomainName
    let revision: Int64
    let digest: String
    let byteCount: Int
    let empty: Bool

    /// Canonical JSON rather than an arbitrary Codable tree. Its exact UTF-8
    /// bytes are verified before the renderer is allowed to parse or import it.
    let payloadJson: String
}

struct ReaderBookUserStatePackage: Codable, Hashable, Sendable {
    static let currentContract = "reader-book-user-state/1"

    let contract: String
    let bookId: String
    let contentSha256: String
    let revision: Int64
    let updatedAt: String
    let domains: [ReaderBookUserStateDomainPayload]

    func domain(_ name: ReaderBookUserStateDomainName)
        -> ReaderBookUserStateDomainPayload? {
        domains.first(where: { $0.name == name })
    }
}

struct ReaderBookUserStateDomainHeader: Codable, Hashable, Sendable {
    let digest: String
    let revision: Int64
    let empty: Bool
}

struct ReaderBookUserStateBaselineDomain: Codable, Hashable, Sendable {
    let digest: String
    let piRevision: Int64
}

struct ReaderBookUserStateBaseline: Codable, Hashable, Sendable {
    static let currentSchema = "reader-book-user-state-baseline/1"

    let schema: String
    /// SHA-256 of a verified, non-secret server account binding. The raw user
    /// id, namespace and cookie never enter this file or the WebView.
    let accountScopeDigest: String
    let localBookId: String
    let remoteBookId: String
    let contentSha256: String
    let observedPackageRevision: Int64
    let domains: [String: ReaderBookUserStateBaselineDomain]
    let updatedAt: String
}

enum ReaderBookUserStateClassification: String, Codable, Sendable {
    case unchanged
    case localNewer = "local-newer"
    case piNewer = "pi-newer"
    case conflict
}

enum ReaderBookUserStatePlanAction: String, Codable, Sendable {
    case keep
    case `import`
}

struct ReaderBookUserStateDomainDecision: Codable, Hashable, Sendable {
    let name: ReaderBookUserStateDomainName
    let classification: ReaderBookUserStateClassification
    let action: ReaderBookUserStatePlanAction
    let reason: String
    let localDigest: String?
    let localRevision: Int64?
    let piDigest: String
    let piRevision: Int64
    let baselineDigest: String?
    let baselinePiRevision: Int64?
}

struct ReaderBookUserStateImportPlan: Codable, Hashable, Sendable {
    static let currentContract = "reader-book-user-state-plan/1"

    let contract: String
    let bookId: String
    let contentSha256: String
    let packageRevision: Int64
    let hasConflicts: Bool
    let decisions: [ReaderBookUserStateDomainDecision]
}

struct ReaderBookUserStatePreparedImport: Sendable {
    let package: ReaderBookUserStatePackage
    let plan: ReaderBookUserStateImportPlan
    let baseline: ReaderBookUserStateBaseline?
    let localHeaders: [ReaderBookUserStateDomainName: ReaderBookUserStateDomainHeader]
    let localBookId: String
    let accountScopeDigest: String
}

struct ReaderBookUserStateImportTransaction: Codable, Hashable, Sendable {
    static let currentContract = "reader-book-user-state-import/1"

    let contract: String
    let transactionId: String
    let localBookId: String
    let remoteBookId: String
    let contentSha256: String
    let packageRevision: Int64
    /// The complete local headers observed while planning, restricted to the
    /// imported domains. The renderer must compare them again immediately
    /// before its one atomic write transaction so edits made after prepare do
    /// not get overwritten.
    let expectedLocalHeaders: [String: ReaderBookUserStateDomainHeader]
    let domains: [ReaderBookUserStateDomainPayload]
}

struct ReaderBookUserStateImportReceipt: Codable, Hashable, Sendable {
    static let currentContract = "reader-book-user-state-import-receipt/1"

    let contract: String
    let transactionId: String
    let committed: Bool
    let domainDigests: [String: String]
}

enum ReaderBookUserStatePackageError: LocalizedError {
    case packageTooLarge
    case invalidPackage(String)
    case invalidAccountScope
    case contentVersionMismatch
    case invalidLocalSnapshot
    case invalidImportReceipt

    var errorDescription: String? {
        switch self {
        case .packageTooLarge:
            return "书籍附属数据包过大，未导入"
        case .invalidPackage(let message):
            return "书籍附属数据包无效：\(message)"
        case .invalidAccountScope:
            return "无法确认当前 Pi 账户，未导入书籍附属数据"
        case .contentVersionMismatch:
            return "书籍附属数据与当前书籍版本不一致"
        case .invalidLocalSnapshot:
            return "无法安全读取本机书籍数据状态"
        case .invalidImportReceipt:
            return "本机书籍数据事务没有返回完整确认，未记录同步基线"
        }
    }
}

/// The implementation must read all requested document records from one
/// consistent IndexedDB snapshot and apply every transaction domain in one
/// IndexedDB read-write transaction. A partial commit must be reported as an
/// error, never as a receipt. Swift intentionally cannot emulate that atomicity
/// by sending one message per domain.
@MainActor
protocol ReaderBookUserStateAtomicApplying: AnyObject {
    func snapshotHeaders(
        localBookId: String
    ) async throws -> [ReaderBookUserStateDomainName: ReaderBookUserStateDomainHeader]

    func applyAtomically(
        _ transaction: ReaderBookUserStateImportTransaction
    ) async throws -> ReaderBookUserStateImportReceipt
}

enum ReaderBookUserStatePackageCodec {
    static let maximumPackageBytes = 96 * 1_024 * 1_024
    static let maximumRawDomainBytes = 64 * 1_024 * 1_024
    static let maximumRevision: Int64 = 9_007_199_254_740_991

    private static let domainMaximumBytes: [ReaderBookUserStateDomainName: Int] = [
        .readingPosition: 64 * 1_024,
        .highlights: 6 * 1_024 * 1_024,
        .ink: 24 * 1_024 * 1_024,
        .closedRegions: 6 * 1_024 * 1_024,
        .notes: 10 * 1_024 * 1_024,
        .userPages: 12 * 1_024 * 1_024,
        .cardPlacements: 3 * 1_024 * 1_024,
        .entityReferences: 3 * 1_024 * 1_024,
    ]
    private static let sensitiveKeys: Set<String> = [
        "authorization", "cookie", "credentials", "ownertoken", "password",
        "secret", "storagenamespace", "token", "userid",
    ]
    private static let pathKeys: Set<String> = [
        "absolutepath", "file", "filepath", "filesystempath", "localpath",
        "path", "sourcepath",
    ]

    static func decode(_ data: Data) throws -> ReaderBookUserStatePackage {
        guard data.count <= maximumPackageBytes else {
            throw ReaderBookUserStatePackageError.packageTooLarge
        }
        do {
            guard let envelope = try JSONSerialization.jsonObject(
                with: data
            ) as? [String: Any],
                  Set(envelope.keys) == Set([
                    "contract", "bookId", "contentSha256", "revision",
                    "updatedAt", "domains",
                  ]),
                  let rawDomains = envelope["domains"] as? [[String: Any]],
                  rawDomains.allSatisfy({ raw in
                    Set(raw.keys) == Set([
                        "name", "revision", "digest", "byteCount", "empty",
                        "payloadJson",
                    ])
                  }) else {
                throw ReaderBookUserStatePackageError.invalidPackage(
                    "含未登记的包或数据域字段"
                )
            }
        } catch let error as ReaderBookUserStatePackageError {
            throw error
        } catch {
            throw ReaderBookUserStatePackageError.invalidPackage(
                "JSON 无法读取"
            )
        }
        let package: ReaderBookUserStatePackage
        do {
            package = try JSONDecoder().decode(
                ReaderBookUserStatePackage.self,
                from: data
            )
        } catch {
            throw ReaderBookUserStatePackageError.invalidPackage(
                "JSON 无法读取"
            )
        }
        try validate(package)
        return package
    }

    static func validate(_ package: ReaderBookUserStatePackage) throws {
        guard package.contract == ReaderBookUserStatePackage.currentContract,
              isFullMatch(package.bookId, pattern: #"book_[a-f0-9]{32}"#),
              isSHA256(package.contentSha256),
              validRevision(package.revision),
              validISOUTC(package.updatedAt),
              package.domains.map(\.name) == ReaderBookUserStateDomainName.allCases else {
            throw ReaderBookUserStatePackageError.invalidPackage(
                "合同、书籍编号、版本或数据域不匹配"
            )
        }

        var rawBytes = 0
        for domain in package.domains {
            rawBytes += try validateDomainPayload(domain)
            guard rawBytes <= maximumRawDomainBytes else {
                throw ReaderBookUserStatePackageError.packageTooLarge
            }
        }
    }

    /// Internal bridge entry points can revalidate a selected transaction
    /// domain without manufacturing a fake complete package around it.
    static func validateDomainPayload(
        _ domain: ReaderBookUserStateDomainPayload
    ) throws -> Int {
        guard validRevision(domain.revision),
              isSHA256(domain.digest),
              let maximum = domainMaximumBytes[domain.name] else {
            throw ReaderBookUserStatePackageError.invalidPackage(
                "\(domain.name.rawValue) 的版本或摘要无效"
            )
        }
        let payload = Data(domain.payloadJson.utf8)
        guard domain.byteCount == payload.count,
              payload.count <= maximum else {
            throw ReaderBookUserStatePackageError.invalidPackage(
                "\(domain.name.rawValue) 的大小无效"
            )
        }
        guard sha256(payload) == domain.digest else {
            throw ReaderBookUserStatePackageError.invalidPackage(
                "\(domain.name.rawValue) 的摘要校验失败"
            )
        }
        let object: Any
        do {
            object = try JSONSerialization.jsonObject(
                with: payload,
                options: [.fragmentsAllowed]
            )
        } catch {
            throw ReaderBookUserStatePackageError.invalidPackage(
                "\(domain.name.rawValue) 不是有效 JSON"
            )
        }
        try validateDomainShape(domain.name, value: object)
        var nodes = 0
        try validateJSON(
            object,
            depth: 0,
            parentKey: nil,
            nodes: &nodes
        )
        guard isEmpty(domain.name, value: object) == domain.empty else {
            throw ReaderBookUserStatePackageError.invalidPackage(
                "\(domain.name.rawValue) 的空状态不一致"
            )
        }
        return payload.count
    }

    static func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map {
            String(format: "%02x", $0)
        }.joined()
    }

    private static func validRevision(_ value: Int64) -> Bool {
        value >= 1 && value <= maximumRevision
    }

    private static func isSHA256(_ value: String) -> Bool {
        isFullMatch(value, pattern: #"[a-f0-9]{64}"#)
    }

    private static func isFullMatch(_ value: String, pattern: String) -> Bool {
        value.range(
            of: "^(?:\(pattern))$",
            options: .regularExpression
        ) != nil
    }

    private static func validISOUTC(_ value: String) -> Bool {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [
            .withInternetDateTime,
            .withFractionalSeconds,
        ]
        if withFraction.date(from: value) != nil, value.hasSuffix("Z") {
            return true
        }
        let wholeSeconds = ISO8601DateFormatter()
        wholeSeconds.formatOptions = [.withInternetDateTime]
        return wholeSeconds.date(from: value) != nil && value.hasSuffix("Z")
    }

    private static func normalizedKey(_ value: String) -> String {
        value.replacingOccurrences(of: "-", with: "")
            .replacingOccurrences(of: "_", with: "")
            .lowercased()
    }

    private static func looksAbsoluteLocalPath(_ value: String) -> Bool {
        value.lowercased().hasPrefix("file:")
            || value.range(
            of: #"^[A-Za-z]:[\\/]"#,
            options: .regularExpression
        ) != nil
            || value.hasPrefix("\\")
            || value.hasPrefix("/")
            || value.hasPrefix("~/")
            || value.hasPrefix(#"~\"#)
    }

    private static func validateJSON(
        _ value: Any,
        depth: Int,
        parentKey: String?,
        nodes: inout Int
    ) throws {
        nodes += 1
        guard nodes <= 750_000, depth <= 40 else {
            throw ReaderBookUserStatePackageError.packageTooLarge
        }
        if value is NSNull { return }
        if let string = value as? String {
            guard Data(string.utf8).count <= 2 * 1_024 * 1_024 else {
                throw ReaderBookUserStatePackageError.packageTooLarge
            }
            if let parentKey,
               pathKeys.contains(normalizedKey(parentKey)),
               looksAbsoluteLocalPath(string) {
                throw ReaderBookUserStatePackageError.invalidPackage(
                    "数据中含本机绝对路径"
                )
            }
            return
        }
        if let number = value as? NSNumber {
            guard number.doubleValue.isFinite else {
                throw ReaderBookUserStatePackageError.invalidPackage(
                    "数据中含无效数字"
                )
            }
            return
        }
        if let array = value as? [Any] {
            for item in array {
                try validateJSON(
                    item,
                    depth: depth + 1,
                    parentKey: nil,
                    nodes: &nodes
                )
            }
            return
        }
        if let dictionary = value as? [String: Any] {
            for (key, item) in dictionary {
                guard !key.isEmpty else {
                    throw ReaderBookUserStatePackageError.invalidPackage(
                        "数据中含空字段名"
                    )
                }
                if sensitiveKeys.contains(normalizedKey(key)) {
                    throw ReaderBookUserStatePackageError.invalidPackage(
                        "数据中含凭据或账户字段"
                    )
                }
                try validateJSON(
                    item,
                    depth: depth + 1,
                    parentKey: key,
                    nodes: &nodes
                )
            }
            return
        }
        throw ReaderBookUserStatePackageError.invalidPackage(
            "数据中含不受支持的值"
        )
    }

    private static func validateDomainShape(
        _ name: ReaderBookUserStateDomainName,
        value: Any
    ) throws {
        switch name {
        case .readingPosition:
            guard value is NSNull || value is [String: Any] else {
                throw ReaderBookUserStatePackageError.invalidPackage(
                    "reading-position 必须是对象或 null"
                )
            }
        case .notes, .userPages, .cardPlacements, .entityReferences:
            guard value is [Any] else {
                throw ReaderBookUserStatePackageError.invalidPackage(
                    "\(name.rawValue) 必须是数组"
                )
            }
        case .highlights:
            guard let hosts = value as? [String: Any],
                  Set(hosts.keys) == Set(["pdf", "epub"]),
                  hosts["pdf"] is [Any],
                  hosts["epub"] is [Any] else {
                throw ReaderBookUserStatePackageError.invalidPackage(
                    "highlights 必须含 pdf 与 epub 数组"
                )
            }
        case .ink, .closedRegions:
            guard let hosts = value as? [String: Any],
                  Set(hosts.keys) == Set(["pdf", "epub"]) else {
                throw ReaderBookUserStatePackageError.invalidPackage(
                    "\(name.rawValue) 必须含 pdf 与 epub 映射"
                )
            }
            for host in ["pdf", "epub"] {
                guard let surfaces = hosts[host] as? [String: Any] else {
                    throw ReaderBookUserStatePackageError.invalidPackage(
                        "\(name.rawValue).\(host) 不是映射"
                    )
                }
                for (surface, strokes) in surfaces {
                    guard validInkSurface(surface),
                          let strokeValues = strokes as? [[String: Any]],
                          strokeValues.allSatisfy({ stroke in
                            ((stroke["t"] as? String) == "region")
                                == (name == .closedRegions)
                          }) else {
                        throw ReaderBookUserStatePackageError.invalidPackage(
                            "\(name.rawValue).\(host) 的页/章数据无效"
                        )
                    }
                }
            }
        }
    }

    private static func validInkSurface(_ value: String) -> Bool {
        if isFullMatch(value, pattern: #"(?:\d+|u_[a-fA-F0-9]{4,32})"#) {
            return true
        }
        guard let separator = value.firstIndex(of: "|"),
              value[..<separator] == "pdf",
              let lastSeparator = value.lastIndex(of: "|"),
              separator != lastSeparator else {
            return false
        }
        let relativeBook = String(value[value.index(after: separator)..<lastSeparator])
        let page = String(value[value.index(after: lastSeparator)...])
        guard !relativeBook.isEmpty,
              relativeBook.utf8.count <= 512,
              !relativeBook.hasPrefix("/"),
              !relativeBook.contains("\\"),
              relativeBook.range(
                of: #"^[A-Za-z][A-Za-z0-9+.-]*:"#,
                options: .regularExpression
              ) == nil,
              isFullMatch(page, pattern: #"[1-9]\d*"#) else {
            return false
        }
        return relativeBook.split(
            separator: "/",
            omittingEmptySubsequences: false
        ).allSatisfy { component in
            !component.isEmpty && component != "." && component != ".."
        }
    }

    private static func isEmpty(
        _ name: ReaderBookUserStateDomainName,
        value: Any
    ) -> Bool {
        if name == .highlights || name == .ink || name == .closedRegions,
           let hosts = value as? [String: Any] {
            return ["pdf", "epub"].allSatisfy { host in
                if let values = hosts[host] as? [Any] { return values.isEmpty }
                if let values = hosts[host] as? [String: Any] {
                    return values.isEmpty
                }
                return false
            }
        }
        if value is NSNull { return true }
        if let string = value as? String { return string.isEmpty }
        if let array = value as? [Any] { return array.isEmpty }
        if let dictionary = value as? [String: Any] {
            return dictionary.isEmpty
        }
        return false
    }
}

enum ReaderBookUserStatePlanner {
    static func plan(
        package: ReaderBookUserStatePackage,
        localHeaders: [ReaderBookUserStateDomainName: ReaderBookUserStateDomainHeader],
        baseline: ReaderBookUserStateBaseline?,
        localIsNewOrEmpty: Bool
    ) throws -> ReaderBookUserStateImportPlan {
        try ReaderBookUserStatePackageCodec.validate(package)

        var decisions: [ReaderBookUserStateDomainDecision] = []
        for domain in package.domains {
            let local = localHeaders[domain.name]
            let based = baseline?.domains[domain.name.rawValue]
            let classification: ReaderBookUserStateClassification
            let action: ReaderBookUserStatePlanAction
            let reason: String

            if let based,
               (domain.revision < based.piRevision
                || (domain.revision == based.piRevision
                    && domain.digest != based.digest)) {
                classification = .conflict
                action = .keep
                reason = "Pi 版本倒退或在同一版本下改变，拒绝覆盖"
            } else if local?.digest == domain.digest {
                classification = .unchanged
                action = .keep
                reason = "本机与 Pi 摘要相同"
            } else if localIsNewOrEmpty, local == nil || local?.empty == true {
                if domain.empty {
                    classification = .unchanged
                    action = .keep
                    reason = "两端都没有该类数据"
                } else {
                    classification = .piNewer
                    action = .import
                    reason = "新下载的本机书尚无该类数据"
                }
            } else if based == nil {
                if domain.empty {
                    classification = .localNewer
                    action = .keep
                    reason = "Pi 为空且尚无同步基线"
                } else if local == nil || local?.empty == true {
                    classification = .piNewer
                    action = .import
                    reason = "本机为空且尚无同步基线"
                } else {
                    classification = .conflict
                    action = .keep
                    reason = "两端都有数据但尚无共同基线"
                }
            } else {
                let localChanged = local == nil || local?.digest != based?.digest
                let piChanged = domain.digest != based?.digest
                switch (localChanged, piChanged) {
                case (false, false):
                    classification = .unchanged
                    action = .keep
                    reason = "两端都与基线相同"
                case (true, false):
                    classification = .localNewer
                    action = .keep
                    reason = "只有本机在基线后发生变化"
                case (false, true):
                    classification = .piNewer
                    action = .import
                    reason = "只有 Pi 在基线后发生变化"
                case (true, true):
                    classification = .conflict
                    action = .keep
                    reason = "本机与 Pi 都在基线后发生变化"
                }
            }

            decisions.append(ReaderBookUserStateDomainDecision(
                name: domain.name,
                classification: classification,
                action: action,
                reason: reason,
                localDigest: local?.digest,
                localRevision: local?.revision,
                piDigest: domain.digest,
                piRevision: domain.revision,
                baselineDigest: based?.digest,
                baselinePiRevision: based?.piRevision
            ))
        }

        return ReaderBookUserStateImportPlan(
            contract: ReaderBookUserStateImportPlan.currentContract,
            bookId: package.bookId,
            contentSha256: package.contentSha256,
            packageRevision: package.revision,
            hasConflicts: decisions.contains(where: {
                $0.classification == .conflict
            }),
            decisions: decisions
        )
    }
}

final class ReaderBookUserStateBaselineStore {
    private let rootURL: URL
    private let fileManager: FileManager

    init(rootURL: URL? = nil, fileManager: FileManager = .default) throws {
        self.fileManager = fileManager
        if let rootURL {
            self.rootURL = rootURL
        } else {
            let applicationSupport = try fileManager.url(
                for: .applicationSupportDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            )
            self.rootURL = applicationSupport
                .appendingPathComponent("BWReader", isDirectory: true)
                .appendingPathComponent("UserStateBaselines", isDirectory: true)
        }
        try fileManager.createDirectory(
            at: self.rootURL,
            withIntermediateDirectories: true
        )
    }

    func load(
        accountScopeDigest: String,
        localBookId: String,
        remoteBookId: String,
        contentSha256: String
    ) throws -> ReaderBookUserStateBaseline? {
        let url = targetURL(
            accountScopeDigest: accountScopeDigest,
            localBookId: localBookId,
            remoteBookId: remoteBookId
        )
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        let value = try JSONDecoder().decode(
            ReaderBookUserStateBaseline.self,
            from: Data(contentsOf: url, options: [.mappedIfSafe])
        )
        guard value.schema == ReaderBookUserStateBaseline.currentSchema,
              value.accountScopeDigest == accountScopeDigest,
              value.localBookId == localBookId,
              value.remoteBookId == remoteBookId,
              value.contentSha256 == contentSha256 else {
            return nil
        }
        return value
    }

    func save(_ baseline: ReaderBookUserStateBaseline) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(baseline)
        try data.write(
            to: targetURL(
                accountScopeDigest: baseline.accountScopeDigest,
                localBookId: baseline.localBookId,
                remoteBookId: baseline.remoteBookId
            ),
            options: [.atomic]
        )
    }

    private func targetURL(
        accountScopeDigest: String,
        localBookId: String,
        remoteBookId: String
    ) -> URL {
        let key = Data(
            "\(accountScopeDigest)\u{0}\(localBookId)\u{0}\(remoteBookId)".utf8
        )
        let leaf = ReaderBookUserStatePackageCodec.sha256(key) + ".json"
        return rootURL.appendingPathComponent(leaf, isDirectory: false)
    }
}

@MainActor
final class ReaderBookUserStatePackageCoordinator {
    private let baselineStore: ReaderBookUserStateBaselineStore

    init(baselineStore: ReaderBookUserStateBaselineStore) {
        self.baselineStore = baselineStore
    }

    func prepareImport(
        packageData: Data,
        accountScopeDigest: String,
        localBookId: String,
        expectedRemoteBookId: String,
        expectedContentSha256: String,
        localIsNewOrEmpty: Bool,
        applier: ReaderBookUserStateAtomicApplying
    ) async throws -> ReaderBookUserStatePreparedImport {
        let package = try ReaderBookUserStatePackageCodec.decode(packageData)
        guard accountScopeDigest.range(
            of: #"^[a-f0-9]{64}$"#,
            options: .regularExpression
        ) != nil else {
            throw ReaderBookUserStatePackageError.invalidAccountScope
        }
        guard package.bookId == expectedRemoteBookId,
              package.contentSha256 == expectedContentSha256 else {
            throw ReaderBookUserStatePackageError.contentVersionMismatch
        }
        let localHeaders = try await applier.snapshotHeaders(
            localBookId: localBookId
        )
        guard Set(localHeaders.keys)
                == Set(ReaderBookUserStateDomainName.allCases) else {
            throw ReaderBookUserStatePackageError.invalidLocalSnapshot
        }
        let baseline = try baselineStore.load(
            accountScopeDigest: accountScopeDigest,
            localBookId: localBookId,
            remoteBookId: expectedRemoteBookId,
            contentSha256: expectedContentSha256
        )
        let plan = try ReaderBookUserStatePlanner.plan(
            package: package,
            localHeaders: localHeaders,
            baseline: baseline,
            localIsNewOrEmpty: localIsNewOrEmpty
        )
        return ReaderBookUserStatePreparedImport(
            package: package,
            plan: plan,
            baseline: baseline,
            localHeaders: localHeaders,
            localBookId: localBookId,
            accountScopeDigest: accountScopeDigest
        )
    }

    /// Imports every Pi-newer domain in one renderer transaction. Conflict and
    /// local-newer domains are deliberately absent from the transaction.
    @discardableResult
    func commitImport(
        _ prepared: ReaderBookUserStatePreparedImport,
        applier: ReaderBookUserStateAtomicApplying
    ) async throws -> ReaderBookUserStateImportPlan {
        let importNames = Set(prepared.plan.decisions.compactMap { decision in
            decision.action == .import ? decision.name : nil
        })
        let importedDomains = prepared.package.domains.filter {
            importNames.contains($0.name)
        }

        if !importedDomains.isEmpty {
            var expectedLocalHeaders: [String: ReaderBookUserStateDomainHeader] = [:]
            for domain in importedDomains {
                guard let header = prepared.localHeaders[domain.name] else {
                    throw ReaderBookUserStatePackageError.invalidLocalSnapshot
                }
                expectedLocalHeaders[domain.name.rawValue] = header
            }
            let transaction = ReaderBookUserStateImportTransaction(
                contract: ReaderBookUserStateImportTransaction.currentContract,
                transactionId: "us_" + UUID().uuidString
                    .replacingOccurrences(of: "-", with: "")
                    .lowercased(),
                localBookId: prepared.localBookId,
                remoteBookId: prepared.package.bookId,
                contentSha256: prepared.package.contentSha256,
                packageRevision: prepared.package.revision,
                expectedLocalHeaders: expectedLocalHeaders,
                domains: importedDomains
            )
            let receipt = try await applier.applyAtomically(transaction)
            let expected = Dictionary(uniqueKeysWithValues: importedDomains.map {
                ($0.name.rawValue, $0.digest)
            })
            guard receipt.contract == ReaderBookUserStateImportReceipt.currentContract,
                  receipt.transactionId == transaction.transactionId,
                  receipt.committed,
                  receipt.domainDigests == expected else {
                throw ReaderBookUserStatePackageError.invalidImportReceipt
            }
        }

        var nextDomains = prepared.baseline?.domains ?? [:]
        for decision in prepared.plan.decisions {
            // Matching local/Pi state and a committed Pi-newer state both form
            // a valid common baseline. Local-newer/conflict state keeps its old
            // baseline (or none), so a later sync cannot erase that evidence.
            if decision.classification == .unchanged
                || decision.classification == .piNewer {
                nextDomains[decision.name.rawValue] =
                    ReaderBookUserStateBaselineDomain(
                        digest: decision.piDigest,
                        piRevision: decision.piRevision
                    )
            }
        }
        let nextBaseline = ReaderBookUserStateBaseline(
            schema: ReaderBookUserStateBaseline.currentSchema,
            accountScopeDigest: prepared.accountScopeDigest,
            localBookId: prepared.localBookId,
            remoteBookId: prepared.package.bookId,
            contentSha256: prepared.package.contentSha256,
            observedPackageRevision: max(
                prepared.baseline?.observedPackageRevision ?? 0,
                prepared.package.revision
            ),
            domains: nextDomains,
            updatedAt: ISO8601DateFormatter().string(from: Date())
        )
        try baselineStore.save(nextBaseline)
        return prepared.plan
    }
}

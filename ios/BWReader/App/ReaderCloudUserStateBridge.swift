import CryptoKit
import Foundation

/// 同步引擎与阅读器之间**唯一**的接触点。
///
/// 引擎是个 actor、阅读器在主线程。`ReaderWebViewModel` 是 `@MainActor` 类型
/// （因而隐式 Sendable），所以这里持有它、用 `await` 调它的方法就够了 ——
/// 每一处跨隔离域的调用都显式带着 `await`，边界藏不住。
///
/// ⚠ **只服务当前打开的那本书**。导出/写回都要穿过该书的本地 runtime
/// （`export-package` / `apply-atomically` 都是对着已加载的书说话的），换本书就
/// 换了一个 runtime。别的书的远端改动由引擎那侧存进待处理区，等它被打开再合。
final class ReaderCloudUserStateBridge: ReaderCloudUserStateSource, @unchecked Sendable {
    private weak var reader: ReaderWebViewModel?

    init(reader: ReaderWebViewModel) {
        self.reader = reader
    }

    func syncableContentDigests() async -> [String] {
        guard let reader, let digest = await reader.cloudSyncContentDigest,
              digest.count == 64 else { return [] }
        return [digest]
    }

    func exportDomains(contentSHA256: String) async throws -> [ReaderCloudUserStateSync.DomainSnapshot] {
        guard let reader else { return [] }
        return try await reader.exportUserStateForCloudSync(contentSHA256: contentSHA256)
    }

    func applyDomains(_ domains: [ReaderCloudUserStateSync.DomainSnapshot],
                      contentSHA256: String) async throws {
        guard let reader else { return }
        try await reader.applyUserStateFromCloudSync(domains, contentSHA256: contentSHA256)
    }
}

/// 合并结果 → 事务里那一项。
///
/// ⚠ 三个字段都不能猜：
/// · `digest` 是 **payloadJson 原样字节**的 sha256（runtime 会重算一遍核对）；
/// · `byteCount` 是同一串的 UTF-8 长度；
/// · `empty` 必须与 runtime 自己那份 `userStateDomainEmpty` 算出来的一致 ——
///   这就是合并模块里那个逐字副本 `domainEmpty` 存在的原因。
/// 任何一项对不上，**整笔事务**被拒（`BW_USER_STATE_DOMAIN_INVALID`），
/// 而那时表面上只是"同步没生效"，不会有别的线索。
enum ReaderCloudUserStateEncoding {
    static func sha256Hex(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    static func payload(
        name: ReaderBookUserStateDomainName,
        json: String,
        revision: Int64,
        empty: Bool
    ) -> ReaderBookUserStateDomainPayload {
        ReaderBookUserStateDomainPayload(
            name: name,
            // revision 必须 ≥ 1（runtime 的硬校验）。
            revision: max(1, revision),
            digest: sha256Hex(json),
            byteCount: Data(json.utf8).count,
            empty: empty,
            payloadJson: json)
    }

    /// 事务要的 `remoteBookId` 形如 `book_<32 hex>`。本机导入的书没有服务器书号，
    /// 但同步身份本来就是内容摘要 —— 取它的前 32 位即可，跨设备天然一致。
    /// ⚠ 不要用 UUID：那会让同一本书在两台设备上生成两个事务身份。
    static func remoteBookId(contentSHA256: String) -> String {
        "book_" + String(contentSHA256.lowercased().prefix(32))
    }
}

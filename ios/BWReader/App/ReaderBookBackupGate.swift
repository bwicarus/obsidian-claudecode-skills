import Foundation

/// 「本地的书必须先上传服务器才能开始使用」这条规矩的落点。
///
/// ## 规则从哪来
///
/// 用户 2026-08-28 拍板（A 方案，明知代价而选的）：
///
/// > 本地的书使用时必须先上传服务器才能开始使用
///
/// 它买到的是一个**不变量**：任何一本能用的书，两边都有。
/// 从服务器下载的天然满足；本地导入的上传后也满足 —— 两条路收敛成同一个
/// 状态，「两批书各是各的」这个问题从根上消失，而不是靠事后同步去补。
///
/// ## ⚠ 为什么落点是「打开」而不是「导入」
///
/// 这套没有"导入"这一步：书是靠扫描你选的那个文件夹**自己出现**的。
/// 所以唯一能真正拦住的地方是**打开**。
///
/// ## ⚠ 判断依据是内容哈希，不是文件名
///
/// `ReaderLocalBookRecord.contentSha256` 本来就有，服务器 `list` 也返回
/// sha256 —— 所以这个判断可以是**精确的**。
///
/// 用名字或大小去猜的话，猜错的后果是把一本没备份的书当成已备份 ——
/// 那会**静默地**破坏不变量，而不变量的全部价值就在于它不该有例外。
///
/// ## ⚠ 代价，以及为什么用户仍然选了它
///
/// 服务器没开时，新书打不开。现阶段服务器是会关机的那台，所以这个代价真实
/// 存在；换成常开的机器之后归零。
///
/// 另一条路（能读但标成"未备份"）被否掉的理由是：**那种需要人去注意的标记
/// 迟早被忽略**，然后在重装时才发现。A 的代价是当场的、不积累的。
@MainActor
final class ReaderBookBackupGate: ObservableObject {
    static let shared = ReaderBookBackupGate()

    /// 服务器上已有的内容哈希。nil = **还没问过**，跟"服务器上没有"是两回事。
    ///
    /// ⚠ 这两者绝不能混：没问过就拦，会在服务器好好的时候平白拦住人；
    /// 没问过就放，会在服务器空着的时候放过所有书。所以用 Optional 强制
    /// 调用方面对这个区别。
    @Published private(set) var serverHashes: Set<String>?
    @Published private(set) var lastError: String?
    @Published private(set) var uploading: Set<String> = []

    func refresh() async {
        do {
            let books = try await ReaderServerLibrary.list()
            serverHashes = Set(books.map(\.sha256))
            lastError = nil
        } catch {
            // ⚠ 失败时**不要**把 serverHashes 设成空集合 —— 那等于说
            // 「服务器上一本书都没有」，会让每一本书都显示成未备份。
            // 保持 nil（"不知道"），并把原因说出来。
            serverHashes = nil
            lastError = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
        }
    }

    enum Status {
        case backed
        case notBacked
        /// 还不知道 —— 服务器没问到。**这是一个独立状态，不是"没备份"。**
        case unknown(String)
    }

    func status(of book: ReaderLocalBookRecord) -> Status {
        guard let hashes = serverHashes else {
            return .unknown(lastError ?? "还没问过\(ReaderServer.displayName)")
        }
        guard let sha = book.contentSha256, !sha.isEmpty else {
            // 本地还没算出哈希 —— 同样是"不知道"，不是"没备份"。
            return .unknown("这本书的内容指纹还没算出来")
        }
        return hashes.contains(sha) ? .backed : .notBacked
    }

    /// 确保这本书在服务器上；不在就传上去。
    ///
    /// - Returns: 可以打开了吗。
    /// - 失败时 `lastError` 里是**能指导下一步**的原因，调用方要显示它。
    @discardableResult
    func ensureBacked(
        _ book: ReaderLocalBookRecord, fileURL: URL
    ) async -> Bool {
        if case .backed = status(of: book) { return true }
        uploading.insert(book.id)
        defer { uploading.remove(book.id) }
        do {
            try await ReaderServerLibrary.upload(fileURL: fileURL)
            // 传完重新问一次,而不是乐观地把哈希塞进本地集合 ——
            // 「我以为传上去了」正是这条不变量最容易被悄悄破坏的方式。
            await refresh()
            if case .backed = status(of: book) {
                lastError = nil
                return true
            }
            lastError = "上传报告成功，但\(ReaderServer.displayName)上没找到这本"
                + "——没有放行，因为不变量没有真的成立"
            return false
        } catch {
            lastError = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
            return false
        }
    }
}

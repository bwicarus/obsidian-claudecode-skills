import Foundation
import WebKit

/// 只清**派生数据**，永不碰权威数据。
///
/// ## 为什么需要它
///
/// 2026-08-28 用户实测：App 数据涨到 **54 GB**，把 IndexedDB 的配额撑爆，
/// 表现是「本机 Reader 无法安全启动 / BW_DATA_BACKEND / 无法打开 IndexedDB」
/// —— 而那个报错跟真正的原因（页图缓存）看不出任何关系。
///
/// 根因是页图带着一年的 `immutable` 缓存头（已改成 5 分钟），而整本预热会
/// 渲染**每一页**、每页还按**多个宽度**各存一份。存量得有人清。
///
/// ## ⚠ 最要紧的一条：清什么、绝不清什么
///
/// | 类型 | 是什么 | 动不动 |
/// |---|---|---|
/// | `diskCache` / `memoryCache` | 页图等**派生数据**，删了会重新渲染 | ✅ 清 |
/// | `WKWebsiteDataTypeIndexedDBDatabases` | **高亮、笔记、阅读位置的权威副本** | ❌ **绝不** |
/// | `localStorage` / cookies | 设置与登录态 | ❌ 不清 |
///
/// CLAUDE.md 写着阅读器的数据权威在 **App 本地**，Pi 只是中继。所以
/// 「清缓存」和「清站点数据」在这里是**一个能做、一个会丢数据**的区别，
/// 而系统设置里那个「卸载 App」两样一起清 —— 这也是为什么必须在应用内提供
/// 一个只清派生数据的出口，而不是让人去设置里想办法。
enum ReaderCacheHygiene {
    /// 超过这个量就自动清一次。
    ///
    /// ⚠ 挑 2 GB 不是随便定的：一本千页书按三种宽度缓存约 400–600 MB，
    /// 2 GB 能容下正在读的几本，又远低于会撑爆配额的量级。
    static let autoPurgeThresholdBytes: Int64 = 2 * 1024 * 1024 * 1024

    /// 只包含派生数据。**这个集合是这个文件的全部要害**，改它之前先读上面那张表。
    private static var derivedTypes: Set<String> {
        [
            WKWebsiteDataTypeDiskCache,
            WKWebsiteDataTypeMemoryCache,
            WKWebsiteDataTypeOfflineWebApplicationCache,
        ]
    }

    /// 现在占了多少（只算派生的那几类）。
    static func derivedBytes() async -> Int64 {
        let records = await WKWebsiteDataStore.default()
            .dataRecords(ofTypes: derivedTypes)
        // ⚠ WKWebsiteDataRecord **不给字节数**。这里只能回记录条数当粗略信号，
        // 真实字节要靠容器大小估。不假装能给准确值 —— 给一个编出来的数字，
        // 比说"量不出来"更糟。
        return Int64(records.count)
    }

    /// 清掉派生数据。返回清了几条记录。
    @discardableResult
    static func purgeDerived() async -> Int {
        let store = WKWebsiteDataStore.default()
        let records = await store.dataRecords(ofTypes: derivedTypes)
        guard !records.isEmpty else { return 0 }
        await store.removeData(ofTypes: derivedTypes, for: records)
        return records.count
    }

    /// App 容器实际占了多少字节。
    ///
    /// ⚠ 这是唯一能真的看出「涨到 54 GB」的口子：`WKWebsiteDataRecord` 不给
    /// 字节数，而系统设置里那个数字 app 自己读不到。所以自己走一遍目录。
    /// 慢，所以只在启动时后台跑一次。
    static func containerBytes() -> Int64 {
        let manager = FileManager.default
        guard let root = manager.urls(
            for: .libraryDirectory, in: .userDomainMask).first else { return 0 }
        var total: Int64 = 0
        guard let walker = manager.enumerator(
            at: root,
            includingPropertiesForKeys: [.totalFileAllocatedSizeKey, .isRegularFileKey],
            options: [.skipsHiddenFiles]) else { return 0 }
        for case let url as URL in walker {
            let values = try? url.resourceValues(
                forKeys: [.totalFileAllocatedSizeKey, .isRegularFileKey])
            guard values?.isRegularFile == true else { continue }
            total += Int64(values?.totalFileAllocatedSize ?? 0)
        }
        return total
    }

    /// 启动时看一眼，超了就清。
    ///
    /// ⚠ **只在超阈值时才清**，不是每次启动都清：页图缓存本来就是为了让翻页
    /// 顺滑，无差别清掉会让每次冷启动都卡一下 —— 那是拿一个真问题换一个假问题。
    static func purgeIfBloated() async -> String? {
        let bytes = await Task.detached(priority: .utility) {
            containerBytes()
        }.value
        guard bytes > autoPurgeThresholdBytes else { return nil }
        let cleared = await purgeDerived()
        let gigabytes = Double(bytes) / 1_073_741_824
        return String(
            format: "本地缓存已达 %.1f GB，清掉了 %d 条派生记录（高亮与笔记未受影响）",
            gigabytes, cleared)
    }
}

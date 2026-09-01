import Foundation
import WebKit

/// 量清楚 App 到底占了什么，然后只清**派生数据**。
///
/// ## 为什么重写
///
/// 2026-08-28：用户 App 数据涨到 **54 GB**，撑爆 IndexedDB 配额，表现是
/// 「本机 Reader 无法安全启动」。我据此**推断**是页图缓存（它带着一年的
/// `immutable` 头），加了自动清理 —— **结果没清掉任何东西**。
///
/// 因为我从头到尾**没有量过**，只是推理。而第一版还有个更糟的性质：
/// 量不到就返回 0 → 低于阈值 → **静默什么都不做**。于是"目录量错了"和
/// "本来就不大"表现完全一样。
///
/// > 这正是这个项目一整天在反复纠正的那件事：**先能分辨，再谈修。**
///
/// 所以这一版反过来：**先把每个目录量出来摆给人看**，清理是次要的。
/// 量出来之后，是不是页图、该清什么，才有依据。
enum ReaderCacheHygiene {

    struct Entry: Identifiable {
        let path: String
        let bytes: Int64
        var id: String { path }
        var readable: String {
            let mega = Double(bytes) / 1_048_576
            return mega >= 1024
                ? String(format: "%.2f GB", mega / 1024)
                : String(format: "%.0f MB", mega)
        }
    }

    /// 超过这个量才自动清。
    static let autoPurgeThresholdBytes: Int64 = 2 * 1024 * 1024 * 1024

    /// 只包含派生数据。**改它之前先想清楚：IndexedDB 里是高亮和笔记的
    /// 权威副本，一个字节都不能碰。** 系统设置里那个"卸载 App"两样一起清，
    /// 这也是必须在应用内提供只清派生数据出口的原因。
    private static var derivedTypes: Set<String> {
        [
            WKWebsiteDataTypeDiskCache,
            WKWebsiteDataTypeMemoryCache,
            WKWebsiteDataTypeOfflineWebApplicationCache,
            // 2026-09-01 实锤 10.16GB 堆在 Library/WebKit：SWR 页图走的
            // CacheStorage 属 FetchCache —— 不在清单里,启动清理每次
            // "清了"却清不到大头。FetchCache 是纯缓存,清后按需重拉。
            // ⚠ IndexedDB/LocalStorage 是权威数据(精确高亮/词汇态),
            // 永远不进这个清单。
            WKWebsiteDataTypeFetchCache,
        ]
    }

    /// 把容器里每个顶层目录量一遍，从大到小。
    ///
    /// ⚠ 第一版只走了 `Library`，而且失败就返回 0。这一版：
    /// - 走 **Documents / Library / tmp** 三处（54 GB 可能在任何一处）
    /// - Library 下再拆一层（`Caches` / `WebKit` / …），否则"Library 40GB"
    ///   这句话跟"它很大"一样没用
    /// - **量不到就说量不到**，不冒充 0
    static func breakdown() -> [Entry] {
        let manager = FileManager.default
        var entries: [Entry] = []

        func measure(_ url: URL) -> Int64 {
            var total: Int64 = 0
            guard let walker = manager.enumerator(
                at: url,
                includingPropertiesForKeys: [
                    .totalFileAllocatedSizeKey, .isRegularFileKey,
                ],
                options: []) else { return -1 }        // -1 = 量不到，不是 0
            for case let item as URL in walker {
                let values = try? item.resourceValues(
                    forKeys: [.totalFileAllocatedSizeKey, .isRegularFileKey])
                guard values?.isRegularFile == true else { continue }
                total += Int64(values?.totalFileAllocatedSize ?? 0)
            }
            return total
        }

        var roots: [URL] = []
        for directory in [FileManager.SearchPathDirectory.documentDirectory,
                          .libraryDirectory] {
            if let url = manager.urls(for: directory, in: .userDomainMask).first {
                roots.append(url)
            }
        }
        roots.append(URL(fileURLWithPath: NSTemporaryDirectory()))

        for root in roots {
            // Library 下面拆一层：真正的大头（Caches / WebKit）在子目录里，
            // 只报 "Library" 等于没报。
            let children = (try? manager.contentsOfDirectory(
                at: root, includingPropertiesForKeys: nil)) ?? []
            if root.lastPathComponent == "Library", !children.isEmpty {
                for child in children {
                    entries.append(Entry(
                        path: "Library/" + child.lastPathComponent,
                        bytes: measure(child)))
                }
            } else {
                entries.append(Entry(
                    path: root.lastPathComponent, bytes: measure(root)))
            }
        }
        // WebKit 里面再拆一层（2026-09-01：10GB 只写「WebKit」等于没说 ——
        // 是页图缓存还是 IndexedDB,决定能不能清、怎么清）。
        let webkitData = FileManager.default.urls(
            for: .libraryDirectory, in: .userDomainMask
        ).first?.appendingPathComponent("WebKit", isDirectory: true)
        if let webkitData {
            // 递归三层（2026-09-01 实锤：只拆一层看到「Default 10.77GB」
            // 等于没说 —— Default 是 per-origin 容器,真凶在
            // Default/<origin>/<类型> 里)。>64MB 才报;条目带「└」前缀,
            // totalBytes 会跳过 —— 它们是上面 WebKit 行的**内部构成**,
            // 计入总和就是双重计数(21.84GB 假读数实锤)。
            func drill(_ dir: URL, depth: Int, label: String) {
                guard depth <= 3,
                      let subs = try? FileManager.default.contentsOfDirectory(
                          at: dir, includingPropertiesForKeys: nil)
                else { return }
                for sub in subs {
                    var isDir: ObjCBool = false
                    guard FileManager.default.fileExists(
                        atPath: sub.path, isDirectory: &isDir),
                        isDir.boolValue else { continue }
                    let bytes = measure(sub)
                    guard bytes > 64 * 1024 * 1024 else { continue }
                    let name = label + "/" + sub.lastPathComponent
                    entries.append(Entry(path: "└ WebKit" + name,
                                         bytes: bytes))
                    drill(sub, depth: depth + 1, label: name)
                }
            }
            drill(webkitData, depth: 1, label: "")
        }
        return entries.sorted { $0.bytes > $1.bytes }
    }

    static func totalBytes(_ entries: [Entry]) -> Int64 {
        entries.reduce(0) {
            $1.path.hasPrefix("└") ? $0 : $0 + max($1.bytes, 0)
        }
    }

    /// 清派生数据。返回清了几条记录。
    @discardableResult
    static func purgeDerived() async -> Int {
        let store = await WKWebsiteDataStore.default()
        let records = await store.dataRecords(ofTypes: derivedTypes)
        guard !records.isEmpty else { return 0 }
        await store.removeData(ofTypes: derivedTypes, for: records)
        return records.count
    }

    /// 启动时量一次并如实汇报。
    ///
    /// ⚠ **无论清没清都要出声。** 第一版只在"超阈值且清了"时才返回一句话，
    /// 于是"目录量错了"和"本来就不大"表现完全一样 —— 而实测正是前者。
    /// 一个只在成功时说话的诊断，等于在失败时撒谎。
    static func inspectAndPurge() async -> String {
        let entries = await Task.detached(priority: .utility) {
            breakdown()
        }.value
        let total = totalBytes(entries)
        let top = entries.prefix(4)
            .map { "\($0.path) \($0.bytes < 0 ? "量不到" : $0.readable)" }
            .joined(separator: " · ")
        let totalText = String(format: "%.2f GB", Double(total) / 1_073_741_824)

        guard total > autoPurgeThresholdBytes else {
            return "本地占用 \(totalText)（未超 2 GB，没清）\n\(top)"
        }
        let cleared = await purgeDerived()
        let after = await Task.detached(priority: .utility) {
            totalBytes(breakdown())
        }.value
        let afterText = String(format: "%.2f GB", Double(after) / 1_073_741_824)
        // ⚠ 把**清完之后**的数字也报出来：清了多少条记录说明不了问题,
        // "从 54 GB 变成 53.9 GB" 才说明清错了地方。
        return "本地占用 \(totalText) → \(afterText)，清了 \(cleared) 条派生记录"
            + "（高亮与笔记未动）\n\(top)"
    }
}

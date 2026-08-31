import Foundation

/// 卡片图片的 App 本地资产库（用户 2026-09-01 拍板：「数据都 App 本地化」
/// 的图片半边）。桥（Windows）留底的图同步进设备，显示永远走本地路由，
/// 桥只负责补货 —— 桥离线不影响任何已同步的卡。
///
/// 「同步」不需要独立队列：本地路由未命中时拉桥一次存下再回（渲染即
/// 同步）。资产以内容哈希命名、不可变，被容量裁剪掉的下次未命中会再
/// 补货，不构成数据丢失。
actor ReaderCardAssetLocalStore {
    static let shared = ReaderCardAssetLocalStore()
    static let maximumBytes: Int64 = 512 * 1024 * 1024

    private let directory: URL
    private static let extensionByType: [String: String] = [
        "image/avif": "avif",
        "image/bmp": "bmp",
        "image/gif": "gif",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/svg+xml": "svg",
        "image/webp": "webp",
    ]

    init() {
        let base = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask
        )[0]
        directory = base.appendingPathComponent(
            "card-assets", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true)
    }

    nonisolated static func isValidAssetID(_ value: String) -> Bool {
        value.count == 16 && value.allSatisfy {
            $0.isHexDigit && !$0.isUppercase
        }
    }

    func load(_ id: String) -> (data: Data, contentType: String)? {
        guard Self.isValidAssetID(id) else { return nil }
        for (type, ext) in Self.extensionByType {
            let url = directory.appendingPathComponent(id + "." + ext)
            if let data = try? Data(contentsOf: url), !data.isEmpty {
                return (data, type)
            }
        }
        return nil
    }

    func save(_ id: String, data: Data, contentType: String) {
        guard Self.isValidAssetID(id), !data.isEmpty,
              let ext = Self.extensionByType[contentType] else { return }
        let url = directory.appendingPathComponent(id + "." + ext)
        try? data.write(to: url, options: .atomic)
        trim()
    }

    func totalBytes() -> Int64 {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: [.fileSizeKey]
        ) else { return 0 }
        return files.reduce(Int64(0)) { sum, url in
            let size = (try? url.resourceValues(
                forKeys: [.fileSizeKey]))?.fileSize ?? 0
            return sum + Int64(size)
        }
    }

    /// 粗 LRU：总量超限裁最旧（按修改时间）。
    private func trim() {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: [
                .contentModificationDateKey, .fileSizeKey,
            ]
        ) else { return }
        var rows: [(url: URL, at: Date, size: Int64)] = []
        var total: Int64 = 0
        for url in files {
            let values = try? url.resourceValues(forKeys: [
                .contentModificationDateKey, .fileSizeKey,
            ])
            let size = Int64(values?.fileSize ?? 0)
            rows.append(
                (url, values?.contentModificationDate ?? .distantPast, size))
            total += size
        }
        guard total > Self.maximumBytes else { return }
        for row in rows.sorted(by: { $0.at < $1.at }) {
            try? fm.removeItem(at: row.url)
            total -= row.size
            if total <= Self.maximumBytes { break }
        }
    }
}

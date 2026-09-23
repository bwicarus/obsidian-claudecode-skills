import Foundation
/// Native three-way merge. Existing cloud callers retain their interface, but
/// no longer load a JavaScript runtime or depend on a bundled script. Invalid
/// data still aborts synchronization; it never falls back to replacing a side.
final class ReaderUserStateMerge {
    enum MergeError: Error {
        case invalidPayload       // 传进来的不是合法 JSON
        case failed(String)       // 模块自己抛的
    }

    struct Result {
        let value: Any            // 合并后的域值（可再序列化成 payloadJson）
        let changed: Bool         // false = 与本地一致，不必写回
        let unknown: Bool         // 模块不认识这个域名：保留了本地值
    }

    init?() { }

    /// domain 取 USER_STATE_DOMAINS 里的名字；三个值都是已解析的 JSON
    /// （`[String: Any]` / `[Any]` / `NSNull`）。
    func merge(domain: String, base: Any?, mine: Any?, theirs: Any?) throws -> Result {
        let output = try ReaderNativeBookMerge.merge(domain: domain, base: base, mine: mine, theirs: theirs)
        return Result(value: output.value, changed: output.changed, unknown: output.unknown)
    }

    /// Empty-domain flags have the same semantics as the browser contract.
    func domainEmpty(domain: String, value: Any) throws -> Bool {
        guard JSONSerialization.isValidJSONObject([value]) else { throw MergeError.invalidPayload }
        return ReaderNativeBookMerge.empty(domain: domain, value: value)
    }

    /// 便利入口：三边都给规范化 JSON 字符串（`payloadJson` 就是这个形状），
    /// 拿回合并后的 JSON 字符串。
    ///
    /// ⚠ 序列化用 `.sortedKeys`：域摘要（digest）算的是**规范化** JSON 的字节，
    /// 键序一变摘要就变，于是每次同步都以为"域改过了"而白写一轮。
    func mergeJSON(domain: String, baseJSON: String?, mineJSON: String,
                   theirsJSON: String) throws -> (json: String, changed: Bool, unknown: Bool) {
        func parse(_ text: String?) throws -> Any {
            guard let text, !text.isEmpty, let data = text.data(using: .utf8) else { return NSNull() }
            return try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        }
        let merged = try merge(domain: domain, base: try parse(baseJSON),
                               mine: try parse(mineJSON), theirs: try parse(theirsJSON))
        let data = try JSONSerialization.data(
            withJSONObject: merged.value, options: [.sortedKeys, .fragmentsAllowed])
        guard let json = String(data: data, encoding: .utf8) else { throw MergeError.invalidPayload }
        return (json, merged.changed, merged.unknown)
    }
}

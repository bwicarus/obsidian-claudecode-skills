import Foundation
import JavaScriptCore

/// 书籍用户状态的三方合并（本地 / 远端 / 共同祖先）。
///
/// ⚠ **规则不在这里**：这只是把 `ReaderBundle/native/user-state-merge.js` 放进
/// JavaScriptCore 跑一遍。与 `ReaderNativePDFSelection` 同一个办法 —— 那份模块是
/// node 契约测试真正执行的对象（`tests/reader_contract/user-state-merge.contract.test.mjs`
/// 的 14 条用例），在 Swift 里照抄一遍规则等于把「两台设备各改各的怎么合」
/// 变成两种答案，而这种分歧只在真撞上时才暴露，表现为数据丢失。
///
/// 为什么需要合并：`apply-atomically` 是**整域权威覆盖**（带 expectedLocalHeaders
/// 的乐观并发）。两台设备各改各的时，后写的那次要么被拒、要么把对方整域盖掉。
///
/// ⚠ 这里**不做单例**：`init?` 可能失败（包里缺文件），而失败时正确的反应是
/// **放弃这次同步**，不是退化成"整域取一边" —— 静默丢掉另一台设备的改动是这条
/// 链上最贵的失败。让调用方持有实例，它就必须面对"拿不到合并器"这件事。
///
/// ⚠ 它带一个 JSContext，**不是线程安全的**。持有方必须保证串行使用：
/// 同步引擎是个 actor，actor 的串行执行就是它的保护。别把实例递出那个隔离域。
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

    private let context: JSContext
    private let mergeDomain: JSValue

    init?() {
        guard let root = Bundle.main.url(forResource: "ReaderBundle", withExtension: nil),
              let source = try? String(
                contentsOf: root.appendingPathComponent("native/user-state-merge.js"),
                encoding: .utf8),
              let context = JSContext() else { return nil }
        context.evaluateScript(source)
        guard context.exception == nil,
              let runtime = context.objectForKeyedSubscript("BWReaderRuntime"),
              let api = runtime.objectForKeyedSubscript("userStateMerge"),
              !api.isUndefined, !api.isNull,
              let function = api.objectForKeyedSubscript("mergeDomain"),
              !function.isUndefined, !function.isNull else { return nil }
        self.context = context
        self.mergeDomain = function
    }

    /// domain 取 USER_STATE_DOMAINS 里的名字；三个值都是已解析的 JSON
    /// （`[String: Any]` / `[Any]` / `NSNull`）。
    func merge(domain: String, base: Any?, mine: Any?, theirs: Any?) throws -> Result {
        context.exception = nil
        let arguments: [Any] = [domain, base ?? NSNull(), mine ?? NSNull(), theirs ?? NSNull()]
        let output = mergeDomain.call(withArguments: arguments)
        if let exception = context.exception {
            throw MergeError.failed(exception.toString() ?? "merge 抛出异常")
        }
        guard let output, output.isObject else { throw MergeError.invalidPayload }
        guard let changed = output.objectForKeyedSubscript("changed")?.toBool() else {
            throw MergeError.invalidPayload
        }
        let unknown = output.objectForKeyedSubscript("unknown")?.toBool() ?? false
        let value = output.objectForKeyedSubscript("value")?.toObject() ?? NSNull()
        return Result(value: value, changed: changed, unknown: unknown)
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

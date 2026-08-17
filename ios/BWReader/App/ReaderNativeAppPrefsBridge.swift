import Foundation
import WebKit

/// 网页设置面板读写**原生偏好**的通道。
///
/// 起因（2026-08-18 用户要求）：App 里那两个原生悬浮按钮打开的 sheet 塞了 12 个 Section
/// ——文字识别、离线词典、书库、OpenAI Key、Pi 同步、绘图触控、阅读位置、设备能力、
/// 快捷指令、本地 Obsidian……全平铺在一张表里，没有层级；用户要求把这些并进
/// 我们自己的设置面板（网页侧 rc-settings.js，App/扩展/Pi 共用同一份）。
///
/// 要并过去，网页就得能读写这些原生偏好，而现有 7 条 bwNative* 通道都是特定功能的
/// （本地笔记 / Anki 移动端 / Pi 网关 / Pi 同步 / Realtime / BookOCR / PDF 变更），
/// 没有一条能做通用偏好读写。这个类补上那一条。
///
/// ⚠ 安全：**必须白名单**。网页脚本能读写原生 UserDefaults 本身就是个口子，
/// 不限定 key 等于把整个 UserDefaults（含 Pi 令牌、OpenAI Key 这类东西）暴露给页面。
/// 这里只放行"纯偏好"——布尔与短枚举字符串；凭据、路径、书库这些一律不进名单，
/// 它们继续留在原生 sheet 里由系统 UI 管。
@MainActor
final class ReaderNativeAppPrefsBridge: NSObject, WKScriptMessageHandlerWithReply {
    static let messageName = "bwNativeAppPrefs"

    /// 允许网页读写的 key。新增前先问一句：这条泄漏出去会不会有代价？
    /// 会的话就不该进这张表。
    private enum Allowed {
        static let bools: Set<String> = [
            "reader.textRecognition.enabled",
            "reader.textRecognition.automaticLocal",
        ]
        /// 枚举型：值必须落在给定集合里，避免网页写进一个原生解不出来的字符串。
        static let enums: [String: Set<String>] = [
            "native-pencil.double-tap": ["none", "toggleEraser", "showPalette", "toggleSelection"],
            "native-pencil.squeeze": ["none", "toggleEraser", "showPalette", "toggleSelection"],
        ]
        static func isBool(_ key: String) -> Bool { bools.contains(key) }
        static func enumValues(_ key: String) -> Set<String>? { enums[key] }
        static func known(_ key: String) -> Bool { isBool(key) || enums[key] != nil }
    }

    func userContentController(
        _ controller: WKUserContentController,
        didReceive message: WKScriptMessage
    ) async -> (Any?, String?) {
        guard let body = message.body as? [String: Any],
              let action = body["action"] as? String
        else {
            return (nil, "bad payload")
        }
        let defaults = UserDefaults.standard

        switch action {
        case "list":
            // 一次把整张白名单的当前值给出去，省得网页逐条问。
            var out: [String: Any] = [:]
            for key in Allowed.bools {
                out[key] = defaults.object(forKey: key) as? Bool ?? false
            }
            for (key, _) in Allowed.enums {
                out[key] = defaults.string(forKey: key) ?? ""
            }
            return (out, nil)

        case "get":
            guard let key = body["key"] as? String, Allowed.known(key) else {
                return (nil, "key not allowed")
            }
            if Allowed.isBool(key) {
                return (defaults.object(forKey: key) as? Bool ?? false, nil)
            }
            return (defaults.string(forKey: key) ?? "", nil)

        case "set":
            guard let key = body["key"] as? String, Allowed.known(key) else {
                return (nil, "key not allowed")
            }
            if Allowed.isBool(key) {
                guard let value = body["value"] as? Bool else { return (nil, "bad value") }
                defaults.set(value, forKey: key)
                return (true, nil)
            }
            guard let value = body["value"] as? String,
                  let allowed = Allowed.enumValues(key),
                  allowed.contains(value)
            else {
                return (nil, "bad value")
            }
            defaults.set(value, forKey: key)
            return (true, nil)

        default:
            return (nil, "unknown action")
        }
    }
}

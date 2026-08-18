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

    /// 顶栏「书籍」按钮请求打开原生书库 sheet。
    /// 网页侧**不做 URL 导航** —— 产品已本地化，书架是 SwiftUI，
    /// 任何一条经过网络地址的路径都可能跑去打开不该存在的网页。
    var onOpenLibrary: (() -> Void)?
    /// 顶栏「App 设置」按钮请求打开原生工具 sheet（过渡期：还没搬完的原生项仍在那里）。
    var onOpenNativeTools: (() -> Void)?
    /// 网页请求唤起原生「选择 Vault 文件夹」。必须是原生 picker —— 只有它产出的
    /// security-scoped URL 才能做 bookmarkData，网页拿不到也不该拿到。
    var onOpenVaultPicker: (() -> Void)?
    /// 网页请求唤起「输入 / 替换 OpenAI Key」的单一用途 sheet。**Key 永不经过 JS**。
    var onOpenRealtimeKey: (() -> Void)?
    /// 网页请求唤起 Pi 登录（固定 origin 的 WKWebView，cookie 落共享 dataStore）。
    var onOpenPiLogin: (() -> Void)?

    /// 这条消息是否来自可信来源。
    ///
    /// ⚠ 2026-08-19 补：此前这个桥**全程没有 frame / origin 校验**，而导航策略允许
    /// youtube-nocookie / player.bilibili.com 作为同页**子框**存在，
    /// `addScriptMessageHandler` 的 handler 对该 content world 的所有 frame 可见 ——
    /// 也就是说嵌进来的第三方播放器页面能调这个桥。当时暴露面只有 4 个偏好键，
    /// 危害有限；而下面这批新动作里有"删词典 / 撤 Vault 授权 / 弹原生 picker"，
    /// 再不设闸就是把它们一起交出去。对齐本地笔记桥的 isMainFrame + 可信 URL 双检。
    var isTrustedFrame: ((WKScriptMessage) -> Bool)?

    /// 只读状态的提供方。返回值必须是**布尔 / 短串 / 数字**，
    /// 绝不含 key、bookmark、文件系统路径（folderName 只给 lastPathComponent）。
    var surfacesProvider: (() -> [String: Any])?
    /// 具名副作用。返回 nil 表示成功，返回字符串则是要**原样显示给用户**的失败原因
    /// —— 比如 Vault 还有 pending 笔记时 clearFolder 会拒绝，那个原因必须能到用户眼前，
    /// 否则就又是一次"点了没反应"（references/silent-failure-lessons.md）。
    var performAction: ((String, Any?) -> String?)?

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
        // 先验来源，再看内容 —— 顺序不能反。
        guard isTrustedFrame?(message) == true else {
            return (nil, "untrusted frame")
        }
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

        case "openLibrary":
            onOpenLibrary?()
            return (true, nil)

        case "openNativeTools":
            onOpenNativeTools?()
            return (true, nil)

        case "openVaultPicker":
            onOpenVaultPicker?()
            return (true, nil)

        case "openRealtimeKey":
            onOpenRealtimeKey?()
            return (true, nil)

        case "openPiLogin":
            onOpenPiLogin?()
            return (true, nil)

        case "surfaces":
            // 词典 / Vault / 凭据 三组的**只读**状态，供网页设置面板直接渲染。
            // 这是"把内容真正并进设置 tab"和"只在 tab 里放一个跳原生的入口"的分界。
            return (surfacesProvider?() ?? [:], nil)

        case "dictDownload", "dictRemove",
             "vaultSetEnabled", "vaultClear",
             "realtimeClear":
            // 具名动作白名单。注意这里**没有** realtimeSave 之类会携带密钥的动作 ——
            // Key 只经原生 SecureField 进 Keychain，任何形式都不经过 JS。
            if let failure = performAction?(action, body["value"]) {
                return (nil, failure)
            }
            return (true, nil)

        default:
            return (nil, "unknown action")
        }
    }
}

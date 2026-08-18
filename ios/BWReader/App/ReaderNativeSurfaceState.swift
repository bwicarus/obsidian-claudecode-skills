import Foundation

/// 「本机」设置的**只读快照**与**具名动作**。
///
/// 用户 2026-08-18：两个原生按钮里的内容没好好分类，应该并进我们自己的设置 tab。
/// 此前只并进去了文字识别与 Pencil 两组开关（纯 bool/枚举，`bwNativeAppPrefs` 的
/// list/get/set 足够）；离线词典、Vault 目录、凭据这三项仍留在原生 sheet 里，
/// tab 里只有一枚"打开 App 原生设置"的跳转按钮 —— 那不叫并进来。
///
/// 这三项并不进去的原因是通道表达不了：它们要的是"结构化状态读 + 具名副作用触发"，
/// 而不是键值读写。这个类型就是那一层。
///
/// ## 边界（这条线是安全设计，不是实现细节）
///
/// - **能过 JS 的**：布尔、短串、数字。词典的安装/下载中/进度、Vault 的
///   已配置/已启用/文件夹**名**、凭据的是否已配置/型号/保存时间。
/// - **绝不过 JS 的**：OpenAI Key 的值、security-scoped bookmark、文件系统路径。
///   Key 只经原生 `SecureField` 进 Keychain；文件夹只经原生 picker 产出的
///   security-scoped URL 做 bookmark。所以下面**没有** `realtimeSave`、
///   也没有任何接收路径的动作 —— 网页只能请求"弹出那个原生 UI"。
/// - `folderName` 本来就只存 `lastPathComponent`（ReaderLocalNotes 的
///   DefaultsKey.folderName），完整路径不在这个字段里。
///
/// ## 失败必须说话
///
/// `perform` 返回 nil = 成功，返回字符串 = 要**原样显示给用户**的原因。
/// 这三个 manager 表达失败的方式各不相同，这里统一成返回值：
/// - `ReaderLocalNotesManager` 在每个动作开头清 `errorMessage`、失败时设 ——
///   所以动作后读它就是这一次的结果（例如 Vault 还有 pending 笔记时
///   `clearFolder` 会拒绝，那句话必须到用户眼前）。
/// - `ReaderOfflineDictionaryManager.removeDownloadedDictionary` 在下载中会
///   **静默 return**；这里前置检查并说出原因，不让它变成"点了没反应"。
/// 见 references/silent-failure-lessons.md。
@MainActor
enum ReaderNativeSurfaceState {
    static func snapshot() -> [String: Any] {
        var out: [String: Any] = [:]

        let dictionary = ReaderOfflineDictionaryManager.shared
        out["dict"] = [
            "installed": dictionary.isInstalled,
            "downloading": dictionary.isDownloading,
            "progress": dictionary.progress,
            "statusText": dictionary.statusText,
        ]

        let notes = ReaderLocalNotesManager.shared
        out["vault"] = [
            "configured": notes.isConfigured,
            "enabled": notes.isEnabled,
            // 这个字段存的本来就是 lastPathComponent，不是完整路径。
            "folderName": notes.folderName,
            "notice": notes.notice ?? "",
            "error": notes.errorMessage ?? "",
        ]

        let realtime = ReaderRealtimeCredentialManager.shared
        out["realtime"] = [
            "configured": realtime.status.isConfigured,
            "model": realtime.status.model,
            "importedAt": realtime.status.importedAt?.timeIntervalSince1970 ?? 0,
        ]

        return out
    }

    /// 具名副作用。未知动作返回错误串而不是静默成功 —— 网页发来一个原生不认识的
    /// 动作时必须看得见，否则又是一处"两边名字漂开了但谁都没报错"。
    static func perform(_ action: String, value: Any?) -> String? {
        switch action {
        case "dictDownload":
            let manager = ReaderOfflineDictionaryManager.shared
            if manager.isDownloading { return nil }   // 已经在下了，不是错误
            manager.download()
            return nil

        case "dictRemove":
            let manager = ReaderOfflineDictionaryManager.shared
            // 原方法在下载中直接 return，什么都不说。
            guard !manager.isDownloading else {
                return "正在下载，先暂停或等它完成再删除"
            }
            manager.removeDownloadedDictionary()
            return nil

        case "vaultSetEnabled":
            guard let enabled = value as? Bool else { return "参数无效" }
            let manager = ReaderLocalNotesManager.shared
            manager.setEnabled(enabled)
            return manager.errorMessage

        case "vaultClear":
            let manager = ReaderLocalNotesManager.shared
            manager.clearFolder()
            return manager.errorMessage

        case "realtimeClear":
            ReaderRealtimeCredentialManager.shared.clear()
            return nil

        default:
            return "未知动作：\(action)"
        }
    }
}

import Foundation

/// 侧栏对话记录的**本机缓存**。
///
/// 为什么要有：侧栏的消息来自页面里的助手层，而助手层的历史要从 Windows 服务器
/// 拉。拉之前、拉失败、离线的时候，侧栏就是空的 —— 2026-09-23 用户："侧边栏的
/// 对话记录好像没有被缓存到 app，没有开启语音对话时直接显示为空"。
///
/// 规则：
/// - 页面交来的快照里**有真正的对话**（你 / 助手）才写缓存；只有提示条（比如
///   "暂时无法恢复对话"）不算，否则一次失败就把好好的缓存冲掉。
/// - 快照里没有对话时，先显示缓存，并标明是本机缓存。
/// - 缓存里的卡片是**只读展示**：动作身份（actionId / pinId / dragId / selectId /
///   controls）一律去掉 —— 它们指向的是上一次页面注册的动作，已经失效，点了只会报错。
/// - 清空对话时连缓存一起清。
/// - 普通对话与复习对话各一份（服务器上本来就是两段独立的对话）。
enum ReaderNativeConversationCache {
    private static let limit = 80
    private static let queue = DispatchQueue(label: "reader.conversation-cache", qos: .utility)
    private static let strippedKeys: Set<String> = ["actionId", "actionLabel", "pinId", "dragId", "selectId",
                                                     "removeId", "controls", "inspectId"]

    private static func url(_ mode: String) -> URL? {
        guard let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        let name = mode == "review" ? "review" : "normal"
        return base.appendingPathComponent("conversation-cache", isDirectory: true)
            .appendingPathComponent(name + ".json")
    }

    /// 至少有一条"你"或"助手"的消息，才算一段对话。
    static func hasConversation(_ raw: [[String: Any]]) -> Bool {
        raw.contains { ["user", "assistant"].contains($0["role"] as? String ?? "") }
    }

    static func load(_ mode: String) -> [[String: Any]] {
        guard let url = url(mode), let data = try? Data(contentsOf: url),
              let value = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return [] }
        return value
    }

    /// 写盘在后台队列做，调用方不等。⚠ 快照一秒能来好几次、对话长了一份就是几百 KB，
    /// 所以由调用方节流（见 ReaderNativeConversationModel.rememberConversation）。
    static func save(_ raw: [[String: Any]], mode: String) {
        let trimmed = Array(raw.suffix(limit)).map(sanitize)
        guard let url = url(mode), JSONSerialization.isValidJSONObject(trimmed),
              let data = try? JSONSerialization.data(withJSONObject: trimmed) else { return }
        let box = Payload(data: data, url: url)
        queue.async {
            try? FileManager.default.createDirectory(at: box.url.deletingLastPathComponent(),
                                                     withIntermediateDirectories: true)
            try? box.data.write(to: box.url, options: .atomic)
        }
    }

    static func clear(_ mode: String) {
        guard let url = url(mode) else { return }
        queue.async { try? FileManager.default.removeItem(at: url) }
    }

    private struct Payload: @unchecked Sendable {
        let data: Data
        let url: URL
    }

    private static func sanitize(_ message: [String: Any]) -> [String: Any] {
        var message = message.filter { !strippedKeys.contains($0.key) }
        message["streaming"] = false
        if let parts = message["parts"] as? [[String: Any]] {
            message["parts"] = parts.map { part -> [String: Any] in
                var part = part.filter { !strippedKeys.contains($0.key) }
                if let data = part["data"] as? [String: Any] {
                    part["data"] = data.filter { !strippedKeys.contains($0.key) }
                }
                return part
            }
        }
        message.removeValue(forKey: "reviewSelections")
        return message
    }
}

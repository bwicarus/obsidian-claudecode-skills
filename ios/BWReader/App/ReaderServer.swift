import Foundation

/// 「我的服务器」—— 存书和备份的那台机器。
///
/// ## ⚠ 为什么是一个可配置的地址，而不是「Windows」
///
/// 用户 2026-08-28 明确说了：**将来可能换 Mac mini 当服务器**。
///
/// 而同一天他抱怨的「太混乱了，这么多没用的按钮」，根源正是把 **Pi** 这个
/// 实现细节当成了一等公民 ——「登录 Pi」「Pi 书库」「与 Pi 同步」散在界面各处。
/// 换一台机器时，要改的应该是**一行配置**，不是把一个词从几十个文件里挖出来。
///
/// 所以这里的规矩是：
///
/// - 代码里**不写**「Windows」「Mac」这类机器名，只有 `ReaderServer.origin`
/// - 界面上**不出现**具体机器，只说「我的服务器」
/// - 换机器 = 改这一个值
///
/// ## ⚠ 它**不是**用来替换 Pi 的
///
/// 现在有 108 条路由 `owner=pi`（助手、词典、翻译、语音…）。这个类型
/// **不碰它们**，只承载**新的**能力（书的存储与备份）。
///
/// 假装"迁移完了"比不迁移更糟：那会让人以为 Pi 已经可以关掉，而实际上
/// 关掉之后一半功能会静默失效。存量什么时候搬、怎么搬，是另一件事。
/// （CLAUDE.md 2026-08-25：「新东西不去 Pi」——**新的**，不是全部。）
enum ReaderServer {
    private static let hostKey = "reader.server.host"

    /// 默认指向当前那台服务器。⚠ 这是**默认值不是常量** —— 用户换机器时
    /// 从设置里改，不需要出新版本。
    private static let fallbackHost = "bwicarus-2.taile44d0c.ts.net"

    /// 当前服务器主机名。
    static var host: String {
        get {
            let stored = UserDefaults.standard.string(forKey: hostKey)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return (stored?.isEmpty == false) ? stored! : fallbackHost
        }
        set {
            let trimmed = newValue.trimmingCharacters(in: .whitespacesAndNewlines)
            // 空值就回落到默认，而不是存一个空字符串让后面每次请求都失败。
            UserDefaults.standard.set(trimmed.isEmpty ? nil : trimmed, forKey: hostKey)
        }
    }

    static var origin: String { "https://" + host }

    static func url(_ path: String) -> URL? {
        URL(string: origin + (path.hasPrefix("/") ? path : "/" + path))
    }

    /// 用户面前的名字。**永远不说具体是哪台机器** —— 那是实现细节，
    /// 而且它会变（Windows → Mac mini）。
    static let displayName = "我的服务器"

    /// 书的规矩（用户 2026-08-28 拍板的 A 方案）。
    ///
    /// > **本地的书必须先上传服务器才能开始使用。**
    ///
    /// 它买到的是一个不变量：**任何一本能用的书，两边都有**。
    /// 从服务器下载的书天然满足；本地导入的上传之后也满足 ——
    /// 两条路收敛成同一个状态，那个「两批书各是各的」问题从根上消失，
    /// 而不是靠事后同步去补。
    ///
    /// ⚠ 代价是明确的：**服务器没开时，新书打不开。**
    /// 用户在知道这个代价的前提下选了 A，理由是另一条路（能读但标记为
    /// "未备份"）会留下一个需要人去注意的状态 —— 而那种标记迟早被忽略，
    /// 然后在重装时才发现。A 的代价是当场的、不积累的；
    /// 而换成常开的 Mac mini 之后它直接归零。
    static let requiresUploadBeforeUse = true
}

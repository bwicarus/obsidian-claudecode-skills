import Foundation

/// 手机与手表之间的载荷契约（2026-08-27 用户要的手表伴侣 app）。
///
/// **为什么是「手机转发」而不是手表直连**：手表要看的三样东西（语音桥状态、
/// 卡片、待办）数据源都在 Windows 桥 `bwicarus-2.taile44d0c.ts.net` 上，而那台
/// 机器只在 tailnet 内可达、**认证方式就是「你在 tailnet 里」**。watchOS 没有
/// Tailscale 客户端，手表也不走 iPhone 的 VPN 隧道。想让手表直连只能把桥暴露
/// 到公网 —— 而那台机器能控浏览器、能开麦克风、能操作 Anki，这个交换不划算，
/// 仓库里本来就有代码在拦（`references/reader-computer-direct-bridge.md`：
/// 「Funnel 永不启用」）。所以：**手机是手表的唯一数据源**。
///
/// ⚠ 由此推出一条必须写在最前面的语义：**手表看到的是手机的镜子**。手机 App
/// 不在跑时，手表上不会有新卡片。这不是 bug，是这个架构的定义。UI 上必须把
/// 「这份数据有多旧」显示出来，否则用户会把陈旧当现状 —— 那正是这个项目
/// 反复吃过亏的那类静默失败。
///
/// 传输用 `WCSession.updateApplicationContext`：它会合并、只保留最新一份，
/// 且**手表 app 没开着也能送达**（下次打开就有）。代价是不保证即时。
/// 卡片这种"错过一张也无所谓"的东西正合适。
enum ReaderWatchPayload {
    /// 载荷契约版本。手机与手表可能不同版本共存（用户先更新了一边），
    /// 对不上时手表显示"版本不匹配"而不是渲染半份数据。
    static let contract = "reader-watch/1"

    /// applicationContext 的大小上限约 262KB。留足余量：超了整份都送不到，
    /// 而 WCSession 的报错在手表上根本看不见。
    static let maximumBytes = 60_000

    enum Key {
        static let contract = "contract"
        static let generatedAtMs = "generatedAtMs"
        static let voice = "voice"
        static let cards = "cards"
        static let notifications = "notifications"
    }
}

/// 语音桥在手表上的样子。字段直接对应手机侧已有的 ReaderNativeVoiceStatus，
/// 不新造概念。
struct ReaderWatchVoice: Codable, Equatable {
    var active: Bool
    var busy: Bool
    /// 人话状态（"已连接" / "未启动"），直接显示，手表不做二次判断。
    var phase: String
    /// 补充说明（错误原因、正在连哪个 app）。可能没有。
    /// ⚠ 叫 detail 而不是 appKind：手机侧传进来的确实是 state.detail，
    /// 名不副实的字段名会让下一个人按错误的假设用它。
    var detail: String?

    static let unknown = ReaderWatchVoice(
        active: false, busy: false, phase: "未知", detail: nil)
}

/// 一张卡片在手表上的样子。
///
/// ⚠ **不是卡片契约的第 7 种 kind**。卡片契约的权威在
/// `_server_deploy/reader_card_contract.py`（六种 kind、单卡 ≤32KiB、
/// `contract_gaps()` 非空就 fail-closed），这里只是**投影**：手机把已经
/// 渲染过的卡片压成手表能显示的形状。加字段不用碰那份契约。
struct ReaderWatchCard: Codable, Equatable, Identifiable {
    var id: String
    var kind: String
    var title: String
    /// 保底文本。手机侧用卡片渲染器现成的 `_infoText(card)` 生成 ——
    /// **原生视图只是"有则更好"**，任何 kind 至少能显示这段字。
    var text: String
    var receivedAtMs: Double
    /// 可选缩略图（手机侧降采样后的 JPEG）。手表**够不到 tailnet**，
    /// 所以图必须随载荷带过来，不能给 URL 让它自己取。
    var thumbnailBase64: String?
}

/// 一条待办在手表上的样子。
struct ReaderWatchNotification: Codable, Equatable, Identifiable {
    var id: String
    var title: String
    var body: String
    /// 到点时刻；没有就是没设时间的持续待办。
    var dueAtMs: Double?
}

/// 手表收到的完整一份快照。
struct ReaderWatchSnapshot: Codable, Equatable {
    var contract: String = ReaderWatchPayload.contract
    var generatedAtMs: Double
    var voice: ReaderWatchVoice
    var cards: [ReaderWatchCard]
    var notifications: [ReaderWatchNotification]

    static let empty = ReaderWatchSnapshot(
        generatedAtMs: 0,
        voice: .unknown,
        cards: [],
        notifications: [])

    /// 这份数据有多旧。手表 UI 必须显示它 —— 见文件头那条语义。
    func ageSeconds(now: Date = Date()) -> Double {
        max(0, now.timeIntervalSince1970 - generatedAtMs / 1000)
    }
}

/// 手表发给手机的指令。故意只有几条：手表是遥控器，不是第二个大脑。
enum ReaderWatchCommand: String, Codable {
    case voiceStart = "voice-start"
    case voiceStop = "voice-stop"
    /// 手表主动要一份最新快照（下拉刷新、或刚打开 app）。
    case refresh = "refresh"

    static let key = "command"
}

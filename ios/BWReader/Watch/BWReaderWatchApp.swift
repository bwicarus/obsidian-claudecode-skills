import SwiftUI
import UIKit
import WatchConnectivity

// BWReader 手表伴侣 app（2026-08-27 用户要的）。
//
// 三件事：看 AI 发的卡片、看待办、开关电脑语音桥。
//
// ⚠ 设计前提写在 Shared/ReaderWatchPayload.swift 的文件头：**手表看到的是
// 手机的镜子**。手表够不到 tailnet，唯一数据源是配对的 iPhone。所以每屏
// 都显示"这份数据有多旧"，宁可让人看见陈旧，也不让陈旧冒充现状。
//
// 屏幕预算：46mm 表盘 416×496 像素 @2x = 可用约 208×248 **点**。
// 按一半算，别照手机的排版直觉写。

@main
struct BWReaderWatchApp: App {
    @StateObject private var link = WatchLink.shared

    var body: some Scene {
        WindowGroup {
            RootView().environmentObject(link)
        }
    }
}

struct RootView: View {
    @EnvironmentObject private var link: WatchLink

    var body: some View {
        TabView {
            CallView()
            TalkView()
            CardsView()
            NotificationsView()
            VoiceView()
            ProbeView()
        }
        .tabViewStyle(.page)
        .onAppear { link.activate() }
    }
}

// ── 数据新鲜度 ──
// 这个视图在每一屏顶部出现。它存在的唯一理由是：手表上的数据必然会陈旧
// （手机 App 不开就不更新），而不说出来的话用户会当成现状。

struct FreshnessBar: View {
    @EnvironmentObject private var link: WatchLink

    var body: some View {
        HStack(spacing: 4) {
            Circle()
                .fill(link.reachable ? Color.green : Color.orange)
                .frame(width: 6, height: 6)
            Text(link.freshnessText)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 0)
        }
    }
}

// ── 卡片 ──

struct CardsView: View {
    @EnvironmentObject private var link: WatchLink

    var body: some View {
        NavigationStack {
            List {
                Section { FreshnessBar() }
                if link.snapshot.cards.isEmpty {
                    // 空态要说清"为什么空"和"怎么才会有"，而不是一个句号。
                    Text("还没有卡片。\nAI 在手机上发卡片时会同步到这里。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(link.snapshot.cards) { card in
                        NavigationLink {
                            CardDetail(card: card)
                        } label: {
                            CardRow(card: card)
                        }
                    }
                }
            }
            .navigationTitle("卡片")
        }
    }
}

struct CardRow: View {
    let card: ReaderWatchCard

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(card.title.isEmpty ? card.kind : card.title)
                .font(.headline)
                .lineLimit(2)
            Text(card.text)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
    }
}

struct CardDetail: View {
    let card: ReaderWatchCard

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                if let image = WatchImageDecoder.image(card.thumbnailBase64) {
                    // 图是手机降采样后随载荷带来的 —— 手表够不到 tailnet，
                    // 给 URL 它自己取不到。
                    Image(uiImage: image)
                        .resizable()
                        .scaledToFit()
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                Text(card.title.isEmpty ? card.kind : card.title)
                    .font(.headline)
                Text(card.text)
                    .font(.footnote)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle(card.kind)
    }
}

enum WatchImageDecoder {
    static func image(_ base64: String?) -> UIImage? {
        guard let base64, !base64.isEmpty,
              let data = Data(base64Encoded: base64) else { return nil }
        return UIImage(data: data)
    }
}

// ── 待办 ──

struct NotificationsView: View {
    @EnvironmentObject private var link: WatchLink

    var body: some View {
        NavigationStack {
            List {
                Section { FreshnessBar() }
                if link.snapshot.notifications.isEmpty {
                    Text("没有待办。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(link.snapshot.notifications) { item in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.title).font(.headline).lineLimit(2)
                            if !item.body.isEmpty {
                                Text(item.body)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(3)
                            }
                            if let due = item.dueAtMs {
                                Text(WatchTime.due(due))
                                    .font(.caption2)
                                    .foregroundStyle(.tint)
                            }
                        }
                    }
                }
            }
            .navigationTitle("待办")
        }
    }
}

// ── 按住说话 ──
//
// 这一屏跟下面那屏「语音桥」是**两件不同的事**，别混：
//   这屏 = 手表当麦克风,借手机问 Pi 的语音助手,一问一答
//   下屏 = 遥控电脑上那条常连的语音桥的开关
// 为什么不能在表上直接连电脑那条桥,见 App/ReaderWatchVoiceTurn.swift 文件头。

struct TalkView: View {
    @StateObject private var voice = WatchVoice()
    @EnvironmentObject private var link: WatchLink

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 10) {
                    switch voice.phase {
                    case .idle:
                        Text("按住说话")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    case .recording:
                        Label("在听……", systemImage: "waveform")
                            .foregroundStyle(.red)
                    case .sending:
                        Text("发送中…").font(.footnote)
                    case .thinking:
                        ProgressView().padding(.vertical, 4)
                    case .done(let heard, let reply):
                        VStack(alignment: .leading, spacing: 6) {
                            // 把"听到了什么"显示出来 —— 答非所问时用户
                            // 一眼能看出是听岔了还是答错了,这两件事该做的
                            // 处理完全不同。
                            if !heard.isEmpty {
                                Text("「" + heard + "」")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                            Text(reply).font(.footnote)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    case .failed(let why):
                        Text(why)
                            .font(.caption2)
                            .foregroundStyle(.orange)
                            .multilineTextAlignment(.center)
                    }

                    Button {
                    } label: {
                        Label(
                            voice.phase == .recording ? "松开发送" : "按住说话",
                            systemImage: "mic.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .tint(voice.phase == .recording ? .red : .accentColor)
                    // 按住/松开而不是点一下 —— 边界由手指定,省掉一套 VAD,
                    // 也省掉"它到底还在不在听"的猜测。
                    .simultaneousGesture(
                        DragGesture(minimumDistance: 0)
                            .onChanged { _ in voice.begin() }
                            .onEnded { _ in voice.finish() })
                }
            }
            .navigationTitle("说话")
        }
        // 结果是异步另发一条回来的（见 WatchVoice.send 的注释）。
        .onChange(of: link.lastTurn) { _, _ in voice.observe(link) }
    }
}

// ── 语音桥 ──

struct VoiceView: View {
    @EnvironmentObject private var link: WatchLink

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 10) {
                    FreshnessBar()
                    Text(link.snapshot.voice.phase)
                        .font(.headline)
                        .multilineTextAlignment(.center)
                    if let detail = link.snapshot.voice.detail,
                       !detail.isEmpty {
                        Text(detail)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    // ── 一次性配给 ──
                    // ⚠ 这跟上面那个开关是**两条完全不同的链路**：
                    // 开关是遥控手机（手机不在前台就用不了）；
                    // 配给是把 token 存进手表 Keychain，配好之后语音由手表
                    // **直连 Pi**，手机开不开都行。
                    // 用户最初的抱怨正是「完全依赖手机 app 是否打开」，
                    // 所以这两件事必须在界面上就分得开。
                    Divider()
                    let provisioned = WatchTokenStore.load() != nil
                    Text(provisioned
                         ? "✅ 语音 token 已配好（直连，不经手机）"
                         : "⚠️ 还没配语音 token")
                        .font(.system(size: 10))
                        // ⚠ 两边必须是同一个类型：`.secondary` 是
                        // HierarchicalShapeStyle 而 `.orange` 是 Color，
                        // 混着写编译不过。统一成 Color。
                        .foregroundStyle(provisioned ? Color.secondary : Color.orange)
                    Button(provisioned ? "重新配给" : "配给语音 token") {
                        link.send(.provisionToken)
                    }
                    .buttonStyle(.bordered)
                    .font(.system(size: 12))
                    Divider()

                    Button {
                        link.send(link.snapshot.voice.active
                                  ? .voiceStop : .voiceStart)
                    } label: {
                        Label(
                            link.snapshot.voice.active ? "停止" : "开始",
                            systemImage: link.snapshot.voice.active
                                ? "stop.fill" : "mic.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .tint(link.snapshot.voice.active ? .red : .accentColor)
                    .disabled(link.snapshot.voice.busy || !link.reachable)

                    if !link.reachable {
                        // 手表是遥控器，手机不在旁边就遥控不了。说清楚，
                        // 别让按钮看起来能按其实没反应。
                        Text("手机不在旁边，开关用不了")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    if let note = link.lastCommandNote {
                        Text(note)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                }
            }
            .navigationTitle("语音桥")
        }
    }
}

enum WatchTime {
    static func due(_ ms: Double) -> String {
        let delta = ms / 1000 - Date().timeIntervalSince1970
        if delta < 0 { return "已过期" }
        if delta < 3600 { return "\(Int(delta / 60)) 分钟后" }
        if delta < 86_400 { return "\(Int(delta / 3600)) 小时后" }
        return "\(Int(delta / 86_400)) 天后"
    }

    static func age(_ seconds: Double) -> String {
        if seconds < 60 { return "刚刚" }
        if seconds < 3600 { return "\(Int(seconds / 60)) 分钟前" }
        if seconds < 86_400 { return "\(Int(seconds / 3600)) 小时前" }
        return "\(Int(seconds / 86_400)) 天前"
    }
}

// ── 网络豁免探针 ──
//
// ⚠ 这是**临时的诊断屏**，验完就删。留着它的一天里，它是这个 app 里唯一
// 一个"给开发者看的"界面。
//
// 它在验：`.playAndRecord`（双工要的）配异步 activate 能不能解禁低层网络。
// 背景与判据见 Watch/WatchNetworkProbe.swift 的文件头。
//
// ⚠ **屏幕上的东西不是判据** —— 真正要看的在落盘的 jsonl 里，因为要测的
// 恰恰是"放下手腕、按数码表冠之后还活不活着"，那时候没人在看屏幕。

struct ProbeView: View {
    @StateObject private var probe = WatchNetworkProbe()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    statusLine

                    if case .idle = probe.state {
                        // ⚠ 第一轮实测里 D/E 两个负对照**都没有失败**，
                        // 最可能是被前面跑过的档留下的音频会话搭了便车。
                        // 负对照一旦没失败，正面结果就什么都证明不了 ——
                        // 所以把测试顺序直接写在按钮上方，而不是指望人记得。
                        Text("先测 F。\n要测 D/E 请**先杀掉 app 重开**，它们必须是本次启动第一个跑的。")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                        ForEach(WatchNetworkProbe.Mode.allCases) { mode in
                            Button {
                                probe.start(mode)
                            } label: {
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(mode.rawValue).font(.system(size: 13, weight: .medium))
                                    Text(mode.expectation)
                                        .font(.system(size: 9))
                                        .foregroundStyle(.secondary)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .buttonStyle(.bordered)
                        }
                    } else {
                        Button("停止", role: .destructive) { probe.stop() }
                            .buttonStyle(.bordered)
                        Button("回到选择") { probe.reset() }
                            .buttonStyle(.bordered)
                    }

                    if !probe.tail.isEmpty {
                        Divider()
                        // 只是"它在跑"的证据，不是判据。
                        ForEach(Array(probe.tail.enumerated()), id: \.offset) { _, line in
                            Text(line)
                                .font(.system(size: 9, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                    }

                    // ── 战报 ──
                    // ⚠ 这一段是整个探针存在的理由。屏幕上的 tail 只能告诉你
                    // "现在连着"，而实验真正要回答的是「放下手腕、按了表冠
                    // 之后还活着吗」—— 那时候没人在看屏幕，只能事后从落盘的
                    // 日志里把答案算出来。
                    let verdicts = probe.verdicts()
                    if !verdicts.isEmpty {
                        Divider()
                        Text("战报").font(.system(size: 12, weight: .semibold))
                        ForEach(verdicts) { verdict in
                            VStack(alignment: .leading, spacing: 1) {
                                Text(verdict.mode)
                                    .font(.system(size: 10, weight: .medium))
                                Text(verdict.line)
                                    .font(.system(size: 9))
                                    .foregroundStyle(.secondary)
                                // ⚠ 全部信号单独一行。上面那句 line 只有一行，
                                // 优先级会把别的信号挡住 —— 这个错已经犯了三次。
                                // **挑重点是人的事，这里只负责别丢。**
                                Text(verdict.signals)
                                    .font(.system(size: 8))
                                    .foregroundStyle(.orange)
                                // 前台/后台的心跳分开显示：两个数一起看才知道
                                // "没测到后台"是因为没进后台，还是进了就断。
                                Text("前台 \(verdict.beatsWhileActive) · 后台 \(verdict.beatsWhileBackground) · \(verdict.heldSeconds)s")
                                    .font(.system(size: 8, design: .monospaced))
                                    .foregroundStyle(.tertiary)
                                // ⚠ 吞吐单独一行：它常常**解释**上面那句结论。
                                // 「后台活下来了」配上「实际只有 16Hz」，
                                // 意思跟配上「实际 50Hz」完全不同。
                                if !verdict.throughput.isEmpty {
                                    Text(verdict.throughput)
                                        .font(.system(size: 8, design: .monospaced))
                                        .foregroundStyle(.tertiary)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }

                    Divider()
                    Text("日志 \(probe.logSize) 字节")
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                    Button("清空日志") { probe.clearLog() }
                        .buttonStyle(.bordered)
                }
                .padding(.horizontal, 2)
            }
            .navigationTitle("网络探针")
        }
    }

    @ViewBuilder private var statusLine: some View {
        switch probe.state {
        case .idle:
            Text("待命").font(.system(size: 13, weight: .semibold))
        case .preparing:
            Text("准备中…").font(.system(size: 13, weight: .semibold))
        case .waiting(let why):
            VStack(alignment: .leading, spacing: 2) {
                Text("⏳ 等待中").font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.orange)
                // ⚠ 原文照抄不做翻译：POSIX 50 / "Network is down" 正是豁免
                // 被拒的签名，把它折成"连接失败"就把唯一的诊断信息扔了。
                Text(why).font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        case .connected(let echoes):
            // ⚠ 光有回声数是不够的：第四轮实测里「数字停止变化但没有报错」
            // 就是靠人盯着数字才发现的。**陈旧必须自己说出来** ——
            // 这跟每屏顶部 FreshnessBar 是同一个道理，探针屏原来漏了。
            let silent = probe.silentSeconds ?? 0
            VStack(alignment: .leading, spacing: 1) {
                Text("✅ 连上了 · 回声 \(echoes)")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(silent > 5 ? .orange : .green)
                Text(silent > 5
                     ? "⚠️ 已经 \(silent) 秒没有回声了"
                     : "上次回声 \(silent)s 前")
                    .font(.system(size: 10))
                    .foregroundStyle(silent > 5 ? .orange : .secondary)
            }
        case .failed(let why):
            VStack(alignment: .leading, spacing: 2) {
                Text("❌ 失败").font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.red)
                Text(why).font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        case .stopped(let echoes, let held):
            Text("已停 · 回声 \(echoes) · 撑了 \(held) 秒")
                .font(.system(size: 13, weight: .semibold))
        }
    }
}

// ── 连续通话 ──
//
// 这一屏跟「按住说话」是**两条完全不同的链路**，别混：
//   这屏 = 手表**直连 Pi**,连续双工,手机开不开都行
//   TalkView = 借手机打 Pi 的回合制问答(手机不在前台就用不了)
//
// 为什么这条能不经手机：手表用**活动音频会话**解禁了低层网络
// （TN3135 豁免①,不是 CallKit —— CallKit 会锁死界面）。
// 全链路实测见 references/watch-companion.md。

struct CallView: View {
    @StateObject private var call = WatchVoiceCall()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 10) {
                    statusBlock

                    switch call.phase {
                    case .idle:
                        // ⚠ 只在待机时能改 —— 音频会话起来之后换模式要重建
                        // 整条链路,而**重建音频会话在后台是不可恢复的**
                        // (Apple 的红线)。所以宁可不让改,也不给一个
                        // "改了之后通话悄悄哑掉"的按钮。
                        Picker("音频档", selection: $call.mode) {
                            ForEach(WatchVoiceCall.Mode.allCases) { one in
                                Text(one.rawValue).tag(one)
                            }
                        }
                        .font(.system(size: 11))
                        .frame(height: 52)
                        startButton("呼叫电脑")
                    case .connecting, .live, .reconnecting:
                        Button("挂断", role: .destructive) { call.stop() }
                            .buttonStyle(.borderedProminent)
                            .tint(.red)
                    case .ended:
                        startButton("再打一次")
                        Button("返回") { call.reset() }
                            .buttonStyle(.bordered)
                            .font(.system(size: 12))
                    }

                    // ⚠ 数字一直显示。这条链路上最常见的失败是「还连着但没声音」,
                    // 而那种失败在界面上跟正常通话长得一模一样 —— 除非把
                    // 收发计数摆出来。
                    // ⚠ 收到的音频峰值。**这一行是用来分辨"下行本来就轻"和
                    // "手表放不响"的** —— 两者修法完全不同,光听分不出来。
                    // 峰值接近 0 = 对面送来的就是静音;峰值很高但听不见 = 播放路径的锅。
                    if call.inboundPeak > 0 {
                        HStack(spacing: 3) {
                            Text("入声").font(.system(size: 8))
                            ProgressView(value: Double(call.inboundPeak))
                                .frame(width: 70)
                            Text(String(format: "%.2f", call.inboundPeak))
                                .font(.system(size: 8, design: .monospaced))
                        }
                        .foregroundStyle(.secondary)
                    }
                    if !call.routeNote.isEmpty {
                        Text(call.routeNote)
                            .font(.system(size: 8))
                            .foregroundStyle(.tertiary)
                            .multilineTextAlignment(.center)
                    }
                    // 通话中 AI 发来的卡片。放在数字上面 —— 它是内容,
                    // 那些是诊断。
                    ForEach(call.liveCards) { card in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(card.title)
                                .font(.system(size: 12, weight: .semibold))
                            if !card.text.isEmpty {
                                Text(card.text).font(.system(size: 11))
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(6)
                        .background(Color.gray.opacity(0.18))
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                    }

                    if call.framesSent > 0 || call.framesPlayed > 0 {
                        Text("发 \(call.framesSent) · 收 \(call.framesPlayed)")
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(.tertiary)
                    }
                }
                .padding(.horizontal, 2)
            }
            .navigationTitle("通话")
        }
    }

    private func startButton(_ title: String) -> some View {
        Button {
            call.start()
        } label: {
            Label(title, systemImage: "phone.fill")
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
    }

    @ViewBuilder private var statusBlock: some View {
        switch call.phase {
        case .idle:
            Text("按一下,接通电脑上的语音助手")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        case .connecting:
            VStack(spacing: 4) {
                ProgressView()
                Text("接通中…").font(.system(size: 11))
            }
        case .live:
            VStack(spacing: 2) {
                Label("通话中", systemImage: "waveform")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Color.green)
                // 沉默秒数一直走。陈旧必须看得见 ——
                // 这跟每屏顶部 FreshnessBar 是同一个道理。
                if call.silentSeconds > 1 {
                    Text("已 \(call.silentSeconds) 秒没有声音")
                        .font(.system(size: 9))
                        .foregroundStyle(Color.orange)
                }
            }
        case .reconnecting(let why):
            VStack(spacing: 2) {
                Label("重连中", systemImage: "arrow.clockwise")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Color.orange)
                // 原文照抄:重连原因决定了是等一等还是去修什么。
                Text(why)
                    .font(.system(size: 8, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }
        case .ended(let why):
            VStack(spacing: 2) {
                Text("已结束").font(.system(size: 13, weight: .semibold))
                Text(why)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
        }
    }
}

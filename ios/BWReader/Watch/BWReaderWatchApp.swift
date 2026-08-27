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
    @State private var probe = WatchNetworkProbe()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    statusLine

                    if case .idle = probe.state {
                        Text("挑一档开始。先跑 A 确认探针本身是好的。")
                            .font(.system(size: 11))
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
                                // 前台/后台的心跳分开显示：两个数一起看才知道
                                // "没测到后台"是因为没进后台，还是进了就断。
                                Text("前台 \(verdict.beatsWhileActive) · 后台 \(verdict.beatsWhileBackground) · \(verdict.heldSeconds)s")
                                    .font(.system(size: 8, design: .monospaced))
                                    .foregroundStyle(.tertiary)
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
            Text("✅ 连上了 · 回声 \(echoes)")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.green)
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

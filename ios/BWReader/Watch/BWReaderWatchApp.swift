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

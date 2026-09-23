import SwiftUI

/// 底部字幕条 —— 侧栏关着时，通话里双方正在说的话。照原版 `#vc-cap`（rc-voicecall）画。
///
/// 为什么原生要自己画：原生正文接管后网页层是透明的，网页那条字幕根本看不见
/// （2026-09-23 用户："侧边栏关闭时 ai 回复的流式字幕没有正确显示"）。
/// 侧栏开着时不画 —— 那时对话就在侧栏里逐字出现，两处同时滚是噪音（原版同规则）。
///
/// 2026-09-24 用户："侧边栏收起时候的字幕还是没有显示 …… 就是显示 ai 回复那个，现在是流式传输
/// 应该可以做成流式字幕，原版的格式美化下就可以用"。网页那条 #vc-cap 只在语音链路活跃时才亮，
/// 打字问的回复永远不上字幕 —— 所以除了照搬 #vc-cap，这里还**直接跟着对话流里正在长的那条 AI
/// 回复**出字幕：按句切，正在生成的那句大字、上一句淡一档（原版两行格式），工具在跑时出状态行，
/// 还没出字时三个跳点；回复完停几秒淡出。语音那条有内容时以它为准（它和声音同步）。
@MainActor
struct ReaderNativeCaptionBar: View {
    @ObservedObject var model: ReaderNativeConversationModel
    /// 本次打开后**亲眼看着它流出来**的回复（旧回复不该一开书就冒出来当字幕）。
    @State private var followed: String?
    /// 已经播完、停够了、该收起的回复。
    @State private var retired: String?

    var body: some View {
        let lines = currentLines
        ZStack(alignment: .bottom) {
            // 常驻的零尺寸锚：计时任务挂在它上面，不随字幕出现/消失而被取消。
            Color.clear.frame(width: 0, height: 0)
                .task(id: replyKey) { await followReply() }
            if !model.sidebarOpen, !lines.isEmpty {
                bar(lines)
            }
        }
    }

    // MARK: 数据：语音字幕优先，否则跟着 AI 回复流

    private var currentLines: [ReaderNativeCaptions.Line] {
        guard model.captions.enabled else { return [] }
        if model.captions.on, !model.captions.lines.isEmpty { return model.captions.lines }
        return replyLines
    }

    /// 最后一轮：最后一条 AI 回复（可能还是上一轮的，新回复的消息还没建出来）和它前面那条用户消息。
    private var latestReply: ReaderNativeConversationMessage? {
        guard let last = model.messages.last else { return nil }
        return last.role == "user" ? nil : last
    }
    private var latestUser: ReaderNativeConversationMessage? { model.messages.last(where: { $0.role == "user" }) }

    private var replyKey: String {
        let reply = latestReply
        return "\(reply?.id ?? "-")|\(reply?.streaming == true)|\(model.busy)"
    }

    private func followReply() async {
        let live = model.busy || latestReply?.streaming == true
        if live {
            if let id = latestReply?.id { followed = id }
            retired = nil
            return
        }
        // 写完了：原版字幕播完停留几秒再淡出。
        guard let id = latestReply?.id, id == followed, retired != id else { return }
        try? await Task.sleep(nanoseconds: 6_000_000_000)
        guard !Task.isCancelled else { return }
        withAnimation(.easeOut(duration: 0.34)) { retired = id }
    }

    private var replyLines: [ReaderNativeCaptions.Line] {
        let reply = latestReply
        let live = model.busy || reply?.streaming == true
        // 刚发出去、AI 的消息还没建出来：上一句=你刚问的，下面三个跳点。
        if model.busy, reply == nil || reply?.id == retired || (reply?.id != followed && reply?.streaming != true) {
            var lines: [ReaderNativeCaptions.Line] = []
            if let asked = latestUser.map({ Self.plain($0.text) }), !asked.isEmpty {
                lines.append(.init(id: 0, kind: "line", user: true, previous: true, text: String(asked.suffix(120))))
            }
            lines.append(.init(id: 2, kind: "wait", user: false, previous: false, text: ""))
            return lines
        }
        guard let reply, reply.id == followed || reply.streaming, reply.id != retired || live else { return [] }
        let sentences = Self.sentences(Self.plain(reply.text))
        var lines: [ReaderNativeCaptions.Line] = []
        if sentences.count >= 2 {
            lines.append(.init(id: 0, kind: "line", user: false, previous: true, text: sentences[sentences.count - 2]))
        }
        if let current = sentences.last {
            lines.append(.init(id: 1, kind: "line", user: false, previous: false, text: current))
        }
        // 工具在跑：状态行（原版 __vcCapStatus —— 侧栏关着时也能看到 agent 在干嘛）。
        if live, let tool = reply.tools.last(where: \.isRunning) {
            let label = tool.title.isEmpty ? "正在处理…" : tool.title + "…"
            lines.append(.init(id: 3, kind: "status", user: false, previous: false, text: label))
        } else if live, sentences.isEmpty {
            lines.append(.init(id: 2, kind: "wait", user: false, previous: false, text: ""))
        }
        return lines
    }

    /// Markdown 去标记、并空白：字幕只要读得出来的字。
    static func plain(_ text: String) -> String {
        var out = text
        for mark in ["**", "__", "`", "$$"] { out = out.replacingOccurrences(of: mark, with: "") }
        out = out.split(separator: "\n", omittingEmptySubsequences: false).map { line -> String in
            var s = line.trimmingCharacters(in: .whitespaces)
            while let first = s.first, "#>-*".contains(first) { s.removeFirst(); s = s.trimmingCharacters(in: .whitespaces) }
            return s
        }.joined(separator: "\n")
        return out
    }

    /// 按句切（原版字幕一句一行）：中日句末标点、换行；太长的一句按 90 字再断，免得一行塞满屏。
    static func sentences(_ text: String) -> [String] {
        var out: [String] = []
        var current = ""
        func flush() {
            let trimmed = current.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { out.append(trimmed) }
            current = ""
        }
        for ch in text {
            if ch == "\n" { flush(); continue }
            current.append(ch)
            if "。！？!?；…".contains(ch) || current.count >= 90 { flush() }
        }
        flush()
        return out
    }

    // MARK: 版式（原版 #vc-cap：深色磨砂圆角条，上一句淡小、当前句大字）

    private func bar(_ lines: [ReaderNativeCaptions.Line]) -> some View {
            VStack(alignment: .leading, spacing: 1) {
                ForEach(lines) { line in
                    row(line)
                }
            }
            .padding(.horizontal, 17).padding(.vertical, 11)
            .frame(maxWidth: 640, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)
            .readerCaptionSurface(in: RoundedRectangle(cornerRadius: 22))
            .overlay(RoundedRectangle(cornerRadius: 22).stroke(Color.white.opacity(0.14), lineWidth: 0.5))
            .shadow(color: .black.opacity(0.5), radius: 24, y: 14)
            .padding(.horizontal, 24)
            .transition(.opacity.combined(with: .offset(y: 12)))
            // 只在"几行、哪几种"变了时做动画：逐字增长也动画的话整条会一直抖。
            .animation(.easeOut(duration: 0.34), value: lines.map { "\($0.id)\($0.kind)" })
            .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private func row(_ line: ReaderNativeCaptions.Line) -> some View {
        switch line.kind {
        case "wait":
            // 「正在听」：三个跳动的小点。
            ReaderNativeCaptionDots()
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(Color.white.opacity(0.09), in: RoundedRectangle(cornerRadius: 10))
                .padding(.top, 3)
        case "status", "ok", "error":
            Text(line.text)
                .font(.system(size: 12))
                .foregroundStyle(line.kind == "ok" ? Color(red: 0.66, green: 0.92, blue: 0.73)
                                 : line.kind == "error" ? Color(red: 1, green: 0.77, blue: 0.75)
                                 : Color.white.opacity(0.8))
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(line.kind == "ok" ? Color(red: 0.19, green: 0.82, blue: 0.35).opacity(0.16)
                            : line.kind == "error" ? Color(red: 1, green: 0.41, blue: 0.38).opacity(0.16)
                            : Color.white.opacity(0.09),
                            in: RoundedRectangle(cornerRadius: 10))
                .padding(.top, 3)
        default:
            // 正文行：上一句淡一档、小一档；你说的话左侧一道强调色细条，AI 的话没有。
            Text(line.text)
                .font(.system(size: line.previous ? 13 : 16, weight: line.previous ? .regular : .medium))
                // 正在长的那句：超出三行时留住**末尾**（最新的字），前面折起。
                .lineLimit(line.previous ? 2 : 3)
                .truncationMode(line.previous ? .tail : .head)
                .foregroundStyle(Color.white.opacity(line.previous ? 0.42 : (line.user ? 0.92 : 0.97)))
                .lineSpacing(3)
                .padding(.leading, 11).padding(.vertical, 3)
                .overlay(alignment: .leading) {
                    if line.user {
                        RoundedRectangle(cornerRadius: 2).fill(ReaderNativeTheme.accent)
                            .frame(width: 2.5).padding(.vertical, 7)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct ReaderNativeCaptionDots: View {
    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            HStack(spacing: 4) {
                ForEach(0..<3, id: \.self) { index in
                    let phase = (t / 1.4 + Double(index) * 0.157).truncatingRemainder(dividingBy: 1)
                    let lift = sin(phase * .pi)
                    Circle().fill(Color.white)
                        .frame(width: 4, height: 4)
                        .opacity(0.25 + 0.7 * lift)
                        .offset(y: -2.5 * lift)
                }
            }
            .frame(height: 12)
        }
        .accessibilityLabel("正在听")
    }
}

private extension View {
    /// 字幕底：原版是深色渐变 + 30px 磨砂（白页上字要立得住）。iOS 26 上用染深色的 Liquid Glass。
    @ViewBuilder
    func readerCaptionSurface<S: Shape>(in shape: S) -> some View {
        let tint = Color(red: 0.08, green: 0.08, blue: 0.1).opacity(0.8)
        if #available(iOS 26.0, *) {
            self.glassEffect(.regular.tint(tint), in: shape)
        } else {
            self.background(tint, in: shape).background(.ultraThinMaterial, in: shape)
        }
    }
}

import SwiftUI

/// 底部字幕条 —— 侧栏关着时，通话里双方正在说的话。照原版 `#vc-cap`（rc-voicecall）画。
///
/// 为什么原生要自己画：原生正文接管后网页层是透明的，网页那条字幕根本看不见
/// （2026-09-23 用户："侧边栏关闭时 ai 回复的流式字幕没有正确显示"）。
/// 侧栏开着时不画 —— 那时对话就在侧栏里逐字出现，两处同时滚是噪音（原版同规则）。
@MainActor
struct ReaderNativeCaptionBar: View {
    @ObservedObject var model: ReaderNativeConversationModel

    var body: some View {
        if model.captions.on, !model.sidebarOpen, !model.captions.lines.isEmpty {
            VStack(alignment: .leading, spacing: 1) {
                ForEach(model.captions.lines) { line in
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
            .animation(.easeOut(duration: 0.34), value: model.captions)
            .accessibilityElement(children: .combine)
        }
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

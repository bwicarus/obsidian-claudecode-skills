import SwiftUI

/// 贴词小框 —— 原版 `#word-pop` 的原生版（reader.src/15-phrase-wordpop.js「单词小框」）。
///
/// 2026-09-23 用户："这个词典框太丑了"（此前是从底部升起的整块面板）。
/// 原版：点一个词，结果贴着那个词弹一个小深色框（词头 + 读音/音标 + 发音 + 释义 + 例句 +
/// 底部「标记掌握 / 语法」），滚动或点别处就收起。内容与底部面板共用 ReaderNativeLookupContent。
struct ReaderNativeWordPopLayer: View {
    @ObservedObject var reader: ReaderWebViewModel

    var body: some View {
        GeometryReader { geometry in
            if let model = reader.nativeWordPop, let anchor = reader.nativeWordPopAnchor {
                let frame = geometry.frame(in: .global)
                ReaderNativeSelectionPanelPlacement(
                    anchor: anchor.offsetBy(dx: -frame.minX, dy: -frame.minY), maxWidth: 360
                ) {
                    ReaderNativeWordPop(model: model) { reader.nativeWordPop = nil }
                        .id(model.id)
                }
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: reader.nativeWordPop?.id)
    }
}

struct ReaderNativeWordPop: View {
    @ObservedObject var model: ReaderNativeLookupModel
    let onClose: () -> Void

    var body: some View {
        // 内边距由正文各段自己给（原版各段 padding 14，底部按钮条黑底通栏），这里只管外框。
        // 滚动由正文自己管：按钮条固定在底部，其余内容在上面滚动。
        ReaderNativeLookupContent(model: model)
        .frame(width: 340)
        .frame(maxHeight: 540)   // 原版 max-height:80vh：例句 / 汉字 / AI 解释都在框里，别截得太矮
        // 与页卡/侧栏卡同一套设计语言（原版 .vc-card）：深色卡面、0.5pt 白 14% 描边、圆角 16。
        // 卡面比页卡更实一点（0.97）：小框盖在正文上，字要清楚。
        .background(Color(red: 30 / 255, green: 30 / 255, blue: 34 / 255).opacity(0.97))
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(ReaderNativeCardStyle.border, lineWidth: 0.5))
        .overlay(alignment: .topTrailing) {
            Button(action: onClose) {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.white.opacity(0.6))
                    .frame(width: 26, height: 26)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(4)
            .accessibilityLabel("关闭词典")
        }
        .shadow(color: .black.opacity(0.4), radius: 20, y: 12)   // .vc-card box-shadow 0 12px 40px
        .environment(\.colorScheme, .dark)
        .task { await model.load() }
    }
}

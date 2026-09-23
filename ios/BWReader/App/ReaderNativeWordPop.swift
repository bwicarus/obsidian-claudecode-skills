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

    private static let surface = Color(red: 28 / 255, green: 28 / 255, blue: 30 / 255)
    private static let accent = Color(red: 10 / 255, green: 132 / 255, blue: 1)

    var body: some View {
        ScrollView {
            ReaderNativeLookupContent(model: model)
                .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 12)
                .padding(.trailing, 18)   // 给右上角 × 让位
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollBounceBehavior(.basedOnSize)
        .frame(width: 340)
        .frame(maxHeight: 400)
        .dynamicTypeSize(.small)
        .background(Self.surface.opacity(0.97), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Self.accent.opacity(0.45), lineWidth: 1))
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
        .shadow(color: .black.opacity(0.55), radius: 12, y: 6)
        .environment(\.colorScheme, .dark)
        .task { await model.load() }
    }
}

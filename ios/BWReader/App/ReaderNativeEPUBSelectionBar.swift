import SwiftUI

/// EPUB 选中文字后的操作条。
///
/// **为什么不是系统的编辑菜单**：`UIEditMenuInteractionAnimating` 只有
/// `addAnimations`/`addCompletion`，是纯动画协议，加不了菜单项；而 WKWebView 的
/// 编辑菜单没有一条稳妥的公开路子让宿主插项（`buildMenu(with:)` 管的是菜单栏与
/// 上下文菜单）。与其跟那套 API 较劲，不如画一条自己完全可控的 —— 这也正是
/// Readium 的分工：正文交给 web view，界面由应用负责。
///
/// ⚠ 网页那条选区工具栏在原生界面开着时会收起（`captureSel` 里按
/// `bw-native-navigation` 判断），否则同一个选区上下各一排按钮。
struct ReaderNativeEPUBSelectionBar: View {
    let text: String
    let onAction: (String) -> Void

    /// 与 PDF 选区菜单同一组动作、同样的顺序 —— 两个阅读器上手势记忆一致。
    private static let actions: [(title: String, icon: String, mode: String)] = [
        ("查词", "character.book.closed", "dict"),
        ("词组", "text.badge.star", "phrase"),
        ("翻译", "translate", "translate"),
        ("解释", "lightbulb", "explain"),
        ("语法", "chart.bar.doc.horizontal", "grammar"),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // 把选中的那段摆出来：EPUB 的选区会被"对齐到词边界"调整过，
            // 让人看见实际送出去的是什么，省得对着一个自己没选的词纳闷。
            Text(text)
                .font(.footnote)
                .foregroundStyle(ReaderNativeTheme.muted)
                .lineLimit(1)
                .truncationMode(.middle)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(Self.actions, id: \.mode) { action in
                        Button {
                            onAction(action.mode)
                        } label: {
                            Label(action.title, systemImage: action.icon)
                                .font(.callout)
                                .labelStyle(.titleAndIcon)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .background(ReaderNativeTheme.card, in: Capsule())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 2)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .padding(.horizontal, 12)
        .padding(.bottom, 10)
        .transition(.move(edge: .bottom).combined(with: .opacity))
    }
}

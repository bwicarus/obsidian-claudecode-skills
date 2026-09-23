import SwiftUI

/// PDF 选区窗口 —— 原版 `#sel-toolbar` 的原生版。
///
/// 2026-09-23 用户："这和我们之前设计的不一样"（此前弹的是系统编辑菜单）。
/// 版式与规则照原版（templates/pdf_reader.html 的 #sel-toolbar + pdf-styles.css，
/// 按钮分组见 reader.src/14-textlayer-legacy.js `_updateToolbarMode`）：
///   · 左栏：画笔 + 竖排色板。点色 = 立刻划线并记为当前色；再点当前色 = 取消激活（原版 onPickColor）。
///   · 右栏：预览「已选：…（N 字）」，下面是按钮组：
///       单个英文词 → 复制 / 查词 / OCR / 搜索；
///       其余 → [词组（短词组才有）] / 复制 / OCR / 翻译 / 解释 / 对话 / 搜索；
///     再下面一行「语法分析」。
/// 单击一个词不出这个窗口 —— 直接查词（原版「单击单词 → 单词小框」）。
struct ReaderNativePDFSelectionPanelLayer: View {
    @ObservedObject var document: ReaderNativePDFDocument

    var body: some View {
        GeometryReader { geometry in
            if let panel = document.selectionPanel, let anchor = document.selectionPanelAnchor {
                let frame = geometry.frame(in: .global)
                ReaderNativeSelectionPanelPlacement(anchor: anchor.offsetBy(dx: -frame.minX, dy: -frame.minY)) {
                    ReaderNativePDFSelectionPanel(panel: panel) { key in
                        document.performSelectionAction(key)
                        // 划线会自己清掉选区（窗口随之收起）；其余动作只收窗口，选区留着。
                        if !key.hasPrefix("highlight:") { document.dismissSelectionPanel() }
                    }
                    .id(panel.id)
                }
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: document.selectionPanelAnchor != nil)
    }
}

/// 贴着选区摆：优先放在选区下方，放不下就放上方；左右夹进屏幕。
private struct ReaderNativeSelectionPanelPlacement: Layout {
    let anchor: CGRect

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        proposal.replacingUnspecifiedDimensions()
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard let view = subviews.first else { return }
        let margin: CGFloat = 8
        let size = view.sizeThatFits(ProposedViewSize(width: min(480, max(160, bounds.width - margin * 2)), height: nil))
        var y = anchor.maxY + margin
        if y + size.height > bounds.height - margin { y = anchor.minY - size.height - margin }
        let x = min(max(margin, anchor.minX), max(margin, bounds.width - size.width - margin))
        y = min(max(margin, y), max(margin, bounds.height - size.height - margin))
        view.place(at: CGPoint(x: bounds.minX + x, y: bounds.minY + y),
                   proposal: ProposedViewSize(width: size.width, height: size.height))
    }
}

struct ReaderNativePDFSelectionPanel: View {
    let panel: ReaderNativePDFDocument.SelectionPanel
    let onAction: (String) -> Void
    /// 当前激活色（原版 localStorage `pdf-hl-active`）。
    @AppStorage("reader.pdfActiveHighlight") private var activeColor = ""

    /// 四支笔。键名与原生划线路径一致（nativeSelectionHighlight 的 palette），
    /// 色值与网页色板默认值一致。
    static let colors: [(key: String, hex: String, name: String)] = [
        ("yellow", "#fff59d", "黄"), ("green", "#a7f3d0", "绿"),
        ("blue", "#a3d4ff", "蓝"), ("pink", "#fda4af", "粉"),
    ]

    private var text: String { panel.text.trimmingCharacters(in: .whitespacesAndNewlines) }

    /// 原版 `_updateToolbarMode`：≤30 字的单个拉丁词才算"单词"。
    private var isWord: Bool {
        text.count <= 30 && text.range(of: #"^[A-Za-z][A-Za-z'’\-]*$"#, options: .regularExpression) != nil
    }

    /// 原版 `_isShortPhrase`：中日 2–60 字、拉丁 2–12 词且 ≤120 字；句末标点结尾的是整句。
    private var isShortPhrase: Bool {
        guard !text.isEmpty, text.range(of: #"[。！？.!?]$"#, options: .regularExpression) == nil else { return false }
        if text.range(of: #"[぀-ヿ㐀-鿿]"#, options: .regularExpression) != nil {
            return text.count >= 2 && text.count <= 60 && text.range(of: "[。！？]", options: .regularExpression) == nil
        }
        let words = text.split(whereSeparator: \.isWhitespace)
        return words.count >= 2 && words.count <= 12 && text.count <= 120
    }

    /// 原版预览：超过 120 字取头 60 + 尾 40。
    private var preview: String {
        guard text.count > 120 else { return text }
        return String(text.prefix(60)) + " … " + String(text.suffix(40))
    }

    private var buttons: [(title: String, icon: String, key: String)] {
        if isWord {
            return [("复制", "doc.on.clipboard", "copy"), ("查词", "magnifyingglass", "dict"),
                    ("OCR", "text.viewfinder", "ocr"), ("搜索", "magnifyingglass", "search")]
        }
        var list: [(title: String, icon: String, key: String)] = []
        if isShortPhrase { list.append(("词组", "book.fill", "phrase")) }
        list += [("复制", "doc.on.clipboard", "copy"), ("OCR", "text.viewfinder", "ocr"),
                 ("翻译", "globe", "translate"), ("解释", "lightbulb", "explain"),
                 ("对话", "bubble.left.and.bubble.right", "chat"), ("搜索", "magnifyingglass", "search")]
        return list
    }

    private static let accent = Color(red: 10 / 255, green: 132 / 255, blue: 1)          // --rc-accent #0a84ff
    private static let raised = Color(red: 44 / 255, green: 44 / 255, blue: 46 / 255)     // --rc-bg-raised #2c2c2e
    private static let swatchBorder = Color(red: 0x5b / 255, green: 0x6a / 255, blue: 0x85 / 255)

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            palette
            VStack(alignment: .leading, spacing: 6) {
                previewView
                ReaderNativeFlowLayout(spacing: 4) {
                    ForEach(buttons, id: \.key) { button in
                        Button { onAction(button.key) } label: {
                            Label(button.title, systemImage: button.icon)
                                .labelStyle(.titleAndIcon)
                                .font(.system(size: 12))
                                .foregroundStyle(.white)
                                .padding(.horizontal, 10).padding(.vertical, 6)
                                .contentShape(RoundedRectangle(cornerRadius: 4))
                        }
                        .buttonStyle(ReaderNativeToolbarButtonStyle())
                    }
                }
                if !isWord {
                    Button { onAction("grammar") } label: {
                        Label("语法分析", systemImage: "chart.bar.doc.horizontal")
                            .font(.system(size: 12))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 12).padding(.vertical, 5)
                            .background(Color(red: 0x24 / 255, green: 0x44 / 255, blue: 0x70 / 255),
                                        in: RoundedRectangle(cornerRadius: 6))
                            .overlay(RoundedRectangle(cornerRadius: 6)
                                .stroke(Color(red: 0x3b / 255, green: 0x6d / 255, blue: 0xb5 / 255), lineWidth: 1))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(6)
        .frame(maxWidth: 480, alignment: .leading)
        .background(Self.raised, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Self.accent.opacity(0.58), lineWidth: 1))
        .shadow(color: .black.opacity(0.6), radius: 8, y: 6)
        .environment(\.colorScheme, .dark)
    }

    private var palette: some View {
        VStack(spacing: 6) {
            Image(systemName: "paintbrush.pointed")
                .font(.system(size: 11))
                .foregroundStyle(Color.white.opacity(0.7))
                .padding(.top, 2)
            ForEach(Self.colors, id: \.key) { color in
                let active = activeColor == color.key
                Button {
                    // 原版 onPickColor：再点当前色 = 只取消激活；点别的色 = 切过去并立刻划线。
                    if active { activeColor = ""; return }
                    activeColor = color.key
                    onAction("highlight:" + color.key)
                } label: {
                    Circle()
                        .fill(Color(hex: color.hex) ?? .yellow)
                        .frame(width: 17, height: 17)
                        .overlay(Circle().stroke(active ? Color.white : Self.swatchBorder, lineWidth: 2))
                        .overlay(Circle().stroke(active ? Self.accent : .clear, lineWidth: 2).padding(-3))
                        .scaleEffect(active ? 1.05 : 1)
                        .frame(width: 28, height: 24)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("用\(color.name)色划线")
            }
        }
        .padding(2)
        .padding(.trailing, 6)
        .overlay(alignment: .trailing) {
            Rectangle().fill(Color.white.opacity(0.18)).frame(width: 1)
        }
    }

    private var previewView: some View {
        (Text("已选：").bold().foregroundColor(.white)
         + Text(preview).foregroundColor(.white)
         + Text("（\(text.count) 字）").font(.system(size: 10)).foregroundColor(Color.white.opacity(0.38)))
            .font(.system(size: 11))
            .lineSpacing(3)
            .lineLimit(3)
            .padding(.horizontal, 9).padding(.vertical, 5)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.black.opacity(0.3), in: RoundedRectangle(cornerRadius: 4))
            .overlay(alignment: .leading) {
                Rectangle().fill(Self.accent).frame(width: 2)
                    .clipShape(UnevenRoundedRectangle(topLeadingRadius: 4, bottomLeadingRadius: 4))
            }
    }
}

/// 原版按钮：透明底，按下时浅灰底（`#sel-toolbar button:hover`）。
private struct ReaderNativeToolbarButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(Color(red: 120 / 255, green: 120 / 255, blue: 128 / 255)
                .opacity(configuration.isPressed ? 0.24 : 0), in: RoundedRectangle(cornerRadius: 4))
    }
}

/// 放不下就换行（原版 `.btns{display:flex;flex-wrap:wrap;gap:4px}`）。
struct ReaderNativeFlowLayout: Layout {
    var spacing: CGFloat = 4

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0, widest: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width { x = 0; y += row + spacing; row = 0 }
            x += size.width + spacing
            row = max(row, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: min(widest, width), height: y + row)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, row: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX { x = bounds.minX; y += row + spacing; row = 0 }
            view.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            row = max(row, size.height)
        }
    }
}

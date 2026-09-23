import SwiftUI
import UIKit
import SwiftSoup

private extension NSAttributedString.Key {
    static let readerRuby = NSAttributedString.Key("ReaderRuby")
}

/// HTML is parsed as data by SwiftSoup. TextKit owns display, selection, links,
/// and accessibility; no WKWebView or HTML rendering engine is involved.
@MainActor
struct ReaderNativeRichText: UIViewRepresentable {
    let content: String
    var format = "markdown"
    var onSelection: ((String) -> Void)?
    /// 页卡正文按原版 CSS 给字号与字色（结论 15/600、细节 12 #b8c6e2…）。
    /// 不给就是侧栏的默认：subheadline + 系统 label 色。
    var font: UIFont? = nil
    var color: UIColor? = nil

    func makeCoordinator() -> Coordinator { Coordinator(onSelection: onSelection) }

    func makeUIView(context: Context) -> ReaderNativeTextView {
        let view = ReaderNativeTextView(usingTextLayoutManager: false)
        view.backgroundColor = .clear
        view.isEditable = false
        view.isSelectable = true
        view.isScrollEnabled = false
        view.textContainer.lineFragmentPadding = 0
        view.textContainerInset = .zero
        view.adjustsFontForContentSizeCategory = true
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        view.delegate = context.coordinator
        return view
    }

    func updateUIView(_ view: ReaderNativeTextView, context: Context) {
        let coordinator = context.coordinator
        coordinator.onSelection = onSelection
        let font = self.font ?? UIFont.preferredFont(forTextStyle: .subheadline)
        let color = self.color ?? UIColor.label
        guard coordinator.source != content || coordinator.format != format || coordinator.fontSize != font.pointSize
                || coordinator.font != font || coordinator.color != color else { return }
        coordinator.source = content
        coordinator.format = format
        coordinator.fontSize = font.pointSize
        coordinator.font = font
        coordinator.color = color
        coordinator.updating = true
        let selected = view.selectedRange
        let rendered = ReaderNativeTextParser.render(content, format: format, font: font, color: color)
        view.attributedText = rendered
        view.textContainerInset = UIEdgeInsets(top: rendered.hasRuby ? font.pointSize * 0.6 : 0, left: 0, bottom: 0, right: 0)
        if selected.location != NSNotFound, NSMaxRange(selected) <= rendered.length { view.selectedRange = selected }
        coordinator.updating = false
        coordinator.textViewDidChangeSelection(view)
        view.invalidateIntrinsicContentSize()
        view.setNeedsLayout()
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: ReaderNativeTextView, context: Context) -> CGSize? {
        guard let width = proposal.width, width > 0 else { return nil }
        return uiView.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude))
    }

    static func dismantleUIView(_ uiView: ReaderNativeTextView, coordinator: Coordinator) {
        coordinator.releaseSelection()
        uiView.delegate = nil
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        var source = "", format = ""
        var fontSize: CGFloat = 0
        var font: UIFont?
        var color: UIColor?
        var updating = false
        var onSelection: ((String) -> Void)?
        private var selectionWork: DispatchWorkItem?
        private var lastSelection = ""

        init(onSelection: ((String) -> Void)?) { self.onSelection = onSelection }
        deinit { selectionWork?.cancel() }

        func textViewDidChangeSelection(_ textView: UITextView) {
            guard !updating else { return }
            selectionWork?.cancel()
            let range = textView.selectedRange
            guard range.location != NSNotFound, range.length > 0,
                  NSMaxRange(range) <= textView.attributedText.length else { releaseSelection(); return }
            let selection = (textView.attributedText.string as NSString).substring(with: range)
            guard selection != lastSelection else { return }
            let work = DispatchWorkItem { [weak self] in
                guard let self else { return }
                self.lastSelection = selection
                self.onSelection?(selection)
            }
            selectionWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.18, execute: work)
        }

        func releaseSelection() {
            selectionWork?.cancel()
            if !lastSelection.isEmpty { onSelection?("") }
            lastSelection = ""
        }

        func textViewDidEndEditing(_ textView: UITextView) { releaseSelection() }

        func textView(_ textView: UITextView, shouldInteractWith URL: URL, in characterRange: NSRange,
                      interaction: UITextItemInteraction) -> Bool {
            ["https", "http"].contains(URL.scheme?.lowercased() ?? "")
        }
    }
}

/// 振假名标注用的标签。单独一个类只为一件事：不存数组也能从
/// `subviews` 里把它们认出来（理由见下面 layoutSubviews 的注释）。
private final class ReaderNativeRubyLabel: UILabel {}

@MainActor
final class ReaderNativeTextView: UITextView {
    /// 重入闸。⚠ 它是 **Bool**：零值读出来就是 false，即使内存还是零
    /// 也不会解引用任何东西 —— 这一点在下面那段里很关键。
    private var rebuildingRuby = false

    override func layoutSubviews() {
        super.layoutSubviews()
        // ⚠⚠ **这里不能有任何 Swift 引用类型的存储属性被读**（2026-09-22
        //   崩溃实录，dSYM 符号化到本函数，出错指令 `ldr x21, [x8, #16]`，x8 = 0）。
        //
        //   原来这里存着 `rubyLabels: [UILabel]`，而它被读出来是**空指针** ——
        //   Swift 数组永远不可能是空指针，除非这块内存还是零：
        //   `UITextView(usingTextLayoutManager:)` 是**继承来的 ObjC 初始化器**，
        //   它在内部就可能触发一次布局，而那一刻子类的 Swift 存储属性还没赋值。
        //
        //   ⚠ 所以正确的做法不是"加保护"（我上一版加的重入闸就没拦住它：
        //   那是个 Bool，零值正好放行），而是**让那个会读到零的存储属性不存在**：
        //   标签改从 `subviews` 里认（ObjC 属性，任何时刻都返回合法数组）。
        guard !rebuildingRuby else { return }
        rebuildingRuby = true
        defer { rebuildingRuby = false }

        subviews.compactMap { $0 as? ReaderNativeRubyLabel }.forEach { $0.removeFromSuperview() }

        guard let attributedText, attributedText.length > 0 else { return }
        layoutManager.ensureLayout(for: textContainer)
        // ⚠ 先全算进**局部**数组，最后一次性挂上去。
        //   在 enumerate 的回调里边建边挂，addSubview 会让布局失效、UIKit 可能同步
        //   重入本函数，那是另一类麻烦（上面那个闸就是为它留的）。
        var built: [ReaderNativeRubyLabel] = []
        attributedText.enumerateAttribute(.readerRuby, in: NSRange(location: 0, length: attributedText.length)) { reading, range, _ in
            guard let reading = reading as? String, !reading.isEmpty else { return }
            let glyphs = layoutManager.glyphRange(forCharacterRange: range, actualCharacterRange: nil)
            let rect = layoutManager.boundingRect(forGlyphRange: glyphs, in: textContainer)
            let font = attributedText.attribute(.font, at: range.location, effectiveRange: nil) as? UIFont
                ?? UIFont.preferredFont(forTextStyle: .subheadline)
            let label = ReaderNativeRubyLabel()
            label.text = reading
            label.font = font.withSize(font.pointSize * 0.52)
            label.textColor = .secondaryLabel
            label.textAlignment = .center
            label.adjustsFontSizeToFitWidth = true
            label.minimumScaleFactor = 0.65
            label.isUserInteractionEnabled = false
            label.isAccessibilityElement = false
            label.frame = CGRect(x: rect.minX + textContainerInset.left,
                                 y: rect.minY + textContainerInset.top - font.pointSize * 0.55,
                                 width: max(rect.width, font.pointSize), height: font.pointSize * 0.6)
            built.append(label)
        }
        built.forEach { addSubview($0) }
    }
}

private extension NSAttributedString {
    var hasRuby: Bool {
        var found = false
        enumerateAttribute(.readerRuby, in: NSRange(location: 0, length: length)) { value, _, stop in
            if value != nil { found = true; stop.pointee = true }
        }
        return found
    }
}

@MainActor
enum ReaderNativeTextParser {
    static func render(_ content: String, format: String, font: UIFont, color: UIColor = .label) -> NSAttributedString {
        let output = NSMutableAttributedString(string: "")
        let base: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color]
        if format != "html", let parsed = try? AttributedString(markdown: content, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)) {
            for run in parsed.runs {
                var attributes = base
                var traits: UIFontDescriptor.SymbolicTraits = []
                if run.inlinePresentationIntent?.contains(.stronglyEmphasized) == true { traits.insert(.traitBold) }
                if run.inlinePresentationIntent?.contains(.emphasized) == true { traits.insert(.traitItalic) }
                if let descriptor = font.fontDescriptor.withSymbolicTraits(traits) { attributes[.font] = UIFont(descriptor: descriptor, size: font.pointSize) }
                if run.inlinePresentationIntent?.contains(.code) == true { attributes[.font] = UIFont.monospacedSystemFont(ofSize: font.pointSize, weight: .regular) }
                if let link = run.link, ["http", "https"].contains(link.scheme?.lowercased() ?? "") { attributes[.link] = link }
                output.append(NSAttributedString(string: String(parsed[run.range].characters), attributes: attributes))
            }
        } else if let document = try? SwiftSoup.parseBodyFragment(content), let body = document.body() {
            append(body, to: output, attributes: base)
        } else {
            output.append(NSAttributedString(string: content, attributes: base))
        }
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = output.hasRuby ? font.pointSize * 0.6 : 3
        output.addAttribute(.paragraphStyle, value: paragraph, range: NSRange(location: 0, length: output.length))
        return output
    }

    private static func append(_ node: Node, to output: NSMutableAttributedString, attributes: [NSAttributedString.Key: Any]) {
        if let text = node as? TextNode {
            output.append(NSAttributedString(string: text.getWholeText(), attributes: attributes))
            return
        }
        guard let element = node as? Element else { return }
        let tag = element.tagName().lowercased()
        if ["script", "style", "iframe", "object", "embed", "rt", "rp"].contains(tag) { return }
        var style = attributes
        let font = style[.font] as? UIFont ?? UIFont.preferredFont(forTextStyle: .subheadline)
        var traits = font.fontDescriptor.symbolicTraits
        if ["b", "strong", "th", "h1", "h2", "h3", "h4"].contains(tag) { traits.insert(.traitBold) }
        if ["i", "em"].contains(tag) { traits.insert(.traitItalic) }
        if let descriptor = font.fontDescriptor.withSymbolicTraits(traits) { style[.font] = UIFont(descriptor: descriptor, size: font.pointSize) }
        if ["code", "pre"].contains(tag) { style[.font] = UIFont.monospacedSystemFont(ofSize: font.pointSize, weight: .regular) }
        if tag == "u" { style[.underlineStyle] = NSUnderlineStyle.single.rawValue }
        if ["s", "del"].contains(tag) { style[.strikethroughStyle] = NSUnderlineStyle.single.rawValue }
        if tag == "a", let href = try? element.attr("href"), let url = URL(string: href),
           ["http", "https"].contains(url.scheme?.lowercased() ?? "") { style[.link] = url }
        if tag == "br" { output.append(NSAttributedString(string: "\n", attributes: style)); return }
        if tag == "li" { output.append(NSAttributedString(string: "• ", attributes: style)) }
        if tag == "img", let alt = try? element.attr("alt"), !alt.isEmpty {
            output.append(NSAttributedString(string: "[\(alt)]", attributes: style))
        }
        let start = output.length
        for child in element.getChildNodes() { append(child, to: output, attributes: style) }
        if tag == "ruby", let reading = try? element.select("rt").text(), !reading.isEmpty, output.length > start {
            output.addAttribute(.readerRuby, value: reading, range: NSRange(location: start, length: output.length - start))
        }
        if ["td", "th"].contains(tag) { output.append(NSAttributedString(string: "\t", attributes: style)) }
        if ["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre"].contains(tag),
           output.length > 0, !output.string.hasSuffix("\n") { output.append(NSAttributedString(string: "\n", attributes: style)) }
    }
}

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
        let font = UIFont.preferredFont(forTextStyle: .subheadline)
        guard coordinator.source != content || coordinator.format != format || coordinator.fontSize != font.pointSize else { return }
        coordinator.source = content
        coordinator.format = format
        coordinator.fontSize = font.pointSize
        coordinator.updating = true
        let selected = view.selectedRange
        let rendered = ReaderNativeTextParser.render(content, format: format, font: font)
        view.attributedText = rendered
        view.textContainerInset = UIEdgeInsets(top: rendered.hasRuby ? font.pointSize * 0.6 : 0, left: 0, bottom: 0, right: 0)
        if selected.location != NSNotFound, NSMaxRange(selected) <= rendered.length { view.selectedRange = selected }
        coordinator.updating = false
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

@MainActor
final class ReaderNativeTextView: UITextView {
    private var rubyLabels: [UILabel] = []

    override func layoutSubviews() {
        super.layoutSubviews()
        rubyLabels.forEach { $0.removeFromSuperview() }
        rubyLabels.removeAll(keepingCapacity: true)
        guard let attributedText, attributedText.length > 0 else { return }
        layoutManager.ensureLayout(for: textContainer)
        attributedText.enumerateAttribute(.readerRuby, in: NSRange(location: 0, length: attributedText.length)) { reading, range, _ in
            guard let reading = reading as? String, !reading.isEmpty else { return }
            let glyphs = layoutManager.glyphRange(forCharacterRange: range, actualCharacterRange: nil)
            let rect = layoutManager.boundingRect(forGlyphRange: glyphs, in: textContainer)
            let font = attributedText.attribute(.font, at: range.location, effectiveRange: nil) as? UIFont
                ?? UIFont.preferredFont(forTextStyle: .subheadline)
            let label = UILabel()
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
            addSubview(label)
            rubyLabels.append(label)
        }
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
    static func render(_ content: String, format: String, font: UIFont) -> NSAttributedString {
        let output = NSMutableAttributedString(string: "")
        let base: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: UIColor.label]
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

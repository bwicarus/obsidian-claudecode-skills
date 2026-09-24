import Foundation
import SwiftSoup

/// Review display rules on parsed data, never a WKWebView document. Keep
/// semantic Markdown, template HTML, replacement reveals and provenance rules.
@MainActor
enum ReaderNativeReviewFaces {
    struct Faces { let front: String; let back: String; let mode: String }
    private final class Cached: NSObject { let value: Faces; init(_ value: Faces) { self.value = value } }
    private static let cache: NSCache<NSString, Cached> = {
        let cache = NSCache<NSString, Cached>(); cache.countLimit = 48; cache.totalCostLimit = 4 * 1024 * 1024; return cache
    }()
    private static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: .regularExpression) != nil
    }
    private static func trim(_ text: String) -> String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private static func plain(_ node: Node, joining separator: String = " ") -> String {
        var stack = [node], texts: [String] = []
        while let item = stack.popLast() {
            if let text = item as? TextNode { texts.append(text.getWholeText()) }
            else { stack.append(contentsOf: item.getChildNodes().reversed()) }
        }
        return trim(texts.joined(separator: separator).replacingOccurrences(of: "[\\s\\uFEFF]+", with: " ", options: .regularExpression))
    }
    private static func root(_ html: String, retaining documents: inout [Document]) throws -> Element {
        let document = try SwiftSoup.parseBodyFragment(html)
        documents.append(document)
        document.outputSettings().prettyPrint(pretty: false)
        for item in try document.select("script,style,link,template,iframe,object,embed,base,meta,form").array() { try item.remove() }
        for item in try document.select("*").array() {
            for attribute in item.getAttributes()?.asList() ?? [] {
                let key = attribute.getKey().lowercased(), value = attribute.getValue()
                if key.hasPrefix("on") || key == "srcdoc" || (["href", "src"].contains(key) && matches(value, "(?i)^\\s*javascript:")) {
                    try item.removeAttr(key)
                }
            }
        }
        return document.body()!
    }
    private static func meaningful(_ node: Node) -> Bool {
        if node is Comment { return false }
        if let text = node as? TextNode { return !trim(text.getWholeText()).isEmpty }
        return (node as? Element).map { $0.tagName() != "br" } ?? false
    }
    private static func trailing(_ node: Node, within root: Node) -> Bool {
        var cursor: Node? = node
        while let item = cursor, item !== root {
            var sibling = item.nextSibling()
            while let next = sibling { if meaningful(next) { return false }; sibling = next.nextSibling() }
            cursor = item.parent()
        }
        return cursor === root
    }
    private static func safeFile(_ raw: String) -> Bool {
        func valid(_ text: String) -> Bool {
            !text.isEmpty && !text.hasPrefix("/") && !matches(text, "(?i)^[a-z]:/") &&
                !matches(text, "[\\x00-\\x1f\\x7f]") && !text.components(separatedBy: "/").contains { ["", ".", ".."].contains($0) }
        }
        var file = trim(raw).replacingOccurrences(of: "\\", with: "/")
        guard valid(file) else { return false }
        for _ in 0..<3 where file.contains("%") {
            guard let decoded = file.removingPercentEncoding else { return false }
            if decoded == file { break }; file = decoded
        }
        return valid(file.precomposedStringWithCompatibilityMapping.replacingOccurrences(of: "\\", with: "/"))
    }
    private static func isMaterialLink(_ node: Element) throws -> Bool {
        let anchors = try node.select("a[href]").array()
        guard anchors.count == 1 else { return false }
        let raw = trim(try anchors[0].attr("href"))
        guard !matches(raw, "[\\x00-\\x1f\\x7f]"),
              let url = URL(string: raw, relativeTo: URL(string: "https://reader.invalid/")),
              let parts = URLComponents(url: url.absoluteURL, resolvingAgainstBaseURL: true),
              ["http", "https"].contains(parts.scheme?.lowercased() ?? ""), parts.user == nil, parts.password == nil,
              parts.fragment == nil, matches(parts.percentEncodedPath, "/pdf/view/?$") else { return false }
        let items = parts.queryItems ?? []
        let files = items.filter { $0.name == "file" }, pages = items.filter { $0.name == "page" }
        return items.count == 2 && files.count == 1 && pages.count == 1 && safeFile(files[0].value ?? "") && matches(pages[0].value ?? "", "^\\d{1,7}$")
    }
    private static func proof(_ node: Node, within root: Node) -> Set<String> {
        guard node.parent() === root else { return [] }
        var cursor = node.nextSibling(), markers: Set<String> = []
        while let item = cursor {
            defer { cursor = item.nextSibling() }
            if let text = item as? TextNode, trim(text.getWholeText()).isEmpty { continue }
            guard item is Comment, let html = try? item.outerHtml(), html.hasPrefix("<!--"), html.hasSuffix("-->") else { return [] }
            let value = trim(String(html.dropFirst(4).dropLast(3)))
            if matches(value, "(?is)^@src:.{1,2000}$") { markers.insert("src") }
            else if matches(value, "(?i)^@entity:[A-Za-z0-9_-]{1,160}:\\d{1,7}$") { markers.insert("entity") }
            else { return [] }
        }
        return markers
    }
    private static func metadata(_ root: Element) throws {
        for node in try root.select(".tags").array() where trailing(node, within: root) { try node.remove() }
        for node in try root.select(".audio-line").array() where matches(plain(node, joining: ""), "(?i)^\\s*\\[anki:play:[^\\]]+\\]\\s*$") { try node.remove() }
        for node in try root.select(".url,.src").array() where try isMaterialLink(node) { try node.remove() }
        for node in root.getChildNodes().compactMap({ $0 as? Element }).reversed() {
            guard trailing(node, within: root) else { continue }
            let text = plain(node)
            guard matches(text, "(?i)^(?:来源|原因|卡片编号|Local\\s*ID)\\s*[：:]") else { continue }
            let source = matches(text, "(?:^|\\s)来源\\s*[：:]"), reason = matches(text, "(?:^|\\s)原因\\s*[：:]")
            let local = matches(text, "(?i)(?:^|\\s)Local\\s*ID\\s*[：:]\\s*[A-Za-z0-9_-]{4,160}")
            let card = matches(text, "(?i)(?:^|\\s)卡片编号\\s*[：:]\\s*[A-Za-z0-9_-]{4,160}")
            let markers = proof(node, within: root)
            let proven = markers.isEmpty ? local && matches(text, "(?i)问\\s*AI|改进这张卡") && (source || reason)
                : markers.contains("src") && source || markers.contains("entity") && card
            guard proven else { continue }
            try node.remove()
            while let tail = root.getChildNodes().last {
                if tail is Comment || (tail as? TextNode).map({ trim($0.getWholeText()).isEmpty }) == true ||
                    (tail as? Element).map({ ["br", "hr"].contains($0.tagName()) }) == true { try tail.remove() }
                else { break }
            }
            break
        }
    }
    private static func comparison(_ html: String) throws -> String {
        var documents: [Document] = []
        defer { withExtendedLifetime(documents) {} }
        let node = try root(html, retaining: &documents)
        for ruby in try node.select("rt").array() { try ruby.remove() }
        var stack = node.getChildNodes()
        while let child = stack.popLast() {
            if child is Comment { try child.remove() }
            else if let text = child as? TextNode {
                if trim(text.getWholeText()).isEmpty { try text.remove() }
                else { text.text(text.getWholeText().replacingOccurrences(of: "[\\s\\uFEFF]+", with: " ", options: .regularExpression)) }
            } else {
                stack.append(contentsOf: child.getChildNodes())
            }
        }
        return trim(try node.html())
    }
    static func project(front rawFront: String, back rawBack: String, format: String) throws -> Faces {
        let key = "\(format):\(rawFront.utf8.count):\(rawFront)\(rawBack)" as NSString
        if let found = cache.object(forKey: key) { return found.value }
        var documents: [Document] = []
        defer { withExtendedLifetime(documents) {} }
        let front = try root(format == "markdown" ? ReaderNativeMarkdown.html(rawFront) : rawFront, retaining: &documents)
        let answer = try root(format == "markdown" ? ReaderNativeMarkdown.html(rawBack) : rawBack, retaining: &documents)
        var back = answer, mode = "append"
        let nodes = answer.getChildNodes()
        let explicit = nodes.firstIndex { node in
            guard let item = node as? Element else { return false }
            return item.tagName() == "hr" && (item.id().lowercased() == "answer" || item.hasAttr("data-answer"))
        }
        func after(_ index: Int) throws -> Element { try root(nodes.dropFirst(index + 1).map { try $0.outerHtml() }.joined(), retaining: &documents) }
        if let explicit { back = try after(explicit) }
        else if try [".cloze", ".jp-sent"].contains(where: {
            let hasFront = try !front.select($0).isEmpty(), hasBack = try !answer.select($0).isEmpty()
            return hasFront && hasBack
        }) { mode = "replace" }
        else {
            var divider: Int?
            let frontComparison = try comparison(front.html())
            for (index, node) in nodes.enumerated() {
                guard let item = node as? Element, item.tagName() == "hr", !item.hasAttr("id"), !item.hasAttr("data-answer") else { continue }
                let prefix = try nodes.prefix(index).map { try $0.outerHtml() }.joined()
                if !frontComparison.isEmpty, try comparison(prefix).utf16.elementsEqual(frontComparison.utf16) { divider = index; break }
            }
            if let divider { back = try after(divider) }
            else {
                let question = plain(front), answerText = plain(answer)
                if question.utf16.count >= 6, answerText.utf16.starts(with: question.utf16) { mode = "replace" }
            }
        }
        try metadata(front); try metadata(back)
        for item in try back.select(".more").array() {
            if (try item.parents().select("details.rv-card-extra").isEmpty()) {
                try item.wrap("<details class=\"rv-card-extra\"></details>")
                try (item.parent() as? Element)?.prepend("<summary>补充信息</summary>")
            }
        }
        let value = Faces(front: try front.html(), back: try back.html(), mode: mode)
        let cost = rawFront.utf8.count + rawBack.utf8.count + value.front.utf8.count + value.back.utf8.count
        if cost <= 512 * 1024 { cache.setObject(Cached(value), forKey: key, cost: cost) }
        return value
    }
    static func state(_ input: [String: Any]) -> [String: Any] {
        var result = input
        for field in ["current", "previous", "next"] {
            guard var card = input[field] as? [String: Any], card["native_review_faces"] as? Bool == true else { continue }
            do {
                let value = try project(front: card["front"] as? String ?? "", back: card["back"] as? String ?? "", format: card["face_format"] as? String ?? "html")
                card["front"] = value.front; card["question"] = value.front
                card["back"] = value.back; card["answer"] = value.back; card["reveal_mode"] = value.mode; card["face_format"] = "html"
                result[field] = card
            } catch { result["notice"] = "卡面整理失败，保留原文：" + error.localizedDescription }
        }
        return result
    }
}

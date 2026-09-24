import Foundation
import SwiftSoup

/// Turns the original favorite record into a free page placement. The
/// placement gets a new ID; learning gid/cid and the complete cards stay intact.
@MainActor
enum ReaderNativeFavoritePlacement {
    static func body(_ record: [String: Any], file: String, page: Int, x: Double, y: Double,
                     pageWidth: Double) throws -> [String: Any] {
        guard page > 0, x.isFinite, y.isFinite, (0...1).contains(x), (0...1).contains(y) else {
            throw ReaderNativeFavoritesService.Failure(message: "收藏卡落点无效")
        }
        let width = pageWidth.isFinite && pageWidth > 0 ? pageWidth : 0
        var body: [String: Any] = ["file": file,
            "id": "c_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
            "anchor": ["kind": "pdf", "page": page, "x": x, "y": y],
            "color": "#0d1322", "w": width > 0 ? max(240, min(480, (width * 0.44).rounded())) : 300,
            "h": 210, "collapsed": false]
        var cards = (record["payload"] as? [String: Any])?["cards"] as? [[String: Any]]
        if cards == nil, record["kind"] as? String == "cards" || record["gid"] != nil,
           let raw = record["raw"] as? String {
            cards = try JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [[String: Any]]
        }
        let cid = record["cid"] as? String ?? record["gid"] as? String ?? record["id"] as? String ?? ""
        guard !cid.isEmpty else { throw ReaderNativeFavoritesService.Failure(message: "收藏卡缺少身份编号") }
        if let cards {
            guard !cards.isEmpty, cards.count <= 64 else { throw ReaderNativeFavoritesService.Failure(message: "学习卡内容无效") }
            for card in cards {
                let fields: [String] = card["type"] as? String == "cloze"
                    ? [ReaderNativeCardRules.string(card["cloze"] ?? card["text"])]
                    : [ReaderNativeCardRules.string(card["front"] ?? card["question"]), ReaderNativeCardRules.string(card["back"] ?? card["answer"])]
                guard fields.allSatisfy({ $0.utf16.count <= 100_000 }) else {
                    throw ReaderNativeFavoritesService.Failure(message: "学习卡正文超过页面限制")
                }
            }
            body["card"] = ["cards": cards, "gid": record["gid"] ?? cid, "cid": cid, "base_w": width]
        } else {
            guard record["kind"] as? String != "cards", record["gid"] == nil else {
                throw ReaderNativeFavoritesService.Failure(message: "学习卡数据缺失，未降级为文字卡")
            }
            let raw = record["raw"] as? String ?? ""
            let content = try normalizeImages(raw.isEmpty ? record["text"] as? String ?? "" : raw)
            let isHTML = record["isHtml"] as? Bool == true
            var context = (record["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if context.isEmpty { context = content }
            if isHTML { context = try plain(context) }
            guard !content.isEmpty, content.utf16.count <= 100_000, context.utf16.count <= 100_000 else {
                throw ReaderNativeFavoritesService.Failure(message: "收藏卡正文或上下文无效")
            }
            body["html"] = ["content": content, "contextText": context, "isHtml": isHTML,
                            "label": record["label"] ?? "收藏卡片", "type": "", "category": "", "icon": "",
                            "cid": cid, "base_w": width]
        }
        return body
    }

    private static func plain(_ html: String) throws -> String {
        guard let body = try SwiftSoup.parseBodyFragment(html).body() else { return "" }
        // DOM textContent concatenates adjacent text nodes without inserting
        // spaces between tags. Preserve that behavior for existing card context.
        func content(_ node: Node) -> String {
            if let text = node as? TextNode { return text.getWholeText() }
            if let data = node as? DataNode { return data.getWholeData() }
            return node.getChildNodes().map(content).joined()
        }
        return content(body).replacingOccurrences(of: "[\\s\\uFEFF]+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func normalizeImages(_ html: String) throws -> String {
        guard html.contains("data-aid") || html.contains("bwicarus-2.taile44d0c.ts.net/reader-card-asset/") else { return html }
        let document = try SwiftSoup.parseBodyFragment(html)
        document.outputSettings().prettyPrint(pretty: false)
        var changed = false
        for image in try document.select("img").array() {
            let src = try image.attr("src")
            let direct = URLComponents(string: src)
            let proxied = direct?.path == "/pdf/api/img-proxy"
                ? direct?.queryItems?.first(where: { $0.name == "url" })?.value : nil
            if let remote = URL(string: proxied ?? src), remote.scheme == "https",
               remote.host == "bwicarus-2.taile44d0c.ts.net", remote.user == nil, remote.password == nil,
               remote.query == nil, remote.fragment == nil,
               remote.path.range(of: "^/reader-card-asset/[a-f0-9]{16}$", options: .regularExpression) != nil {
                try image.attr("src", "/pdf/api/card-asset?id=" + remote.lastPathComponent); changed = true
            }
            let aid = try image.attr("data-aid").trimmingCharacters(in: .whitespacesAndNewlines)
            guard aid.range(of: "^[a-z]{2,4}_[a-f0-9]{4,12}$", options: .regularExpression) != nil else { continue }
            if try image.attr("data-source-url").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
               let proxied, let source = URL(string: proxied), source.scheme == "https", source.host != nil,
               source.user == nil, source.password == nil, source.fragment == nil {
                try image.attr("data-source-url", proxied); changed = true
            }
            if image.hasAttr("data-asset-fallback-done") { try image.removeAttr("data-asset-fallback-done"); changed = true }
            let primary = "/pdf/api/asset/" + aid + "?proxy=1"
            if try image.attr("src") != primary { try image.attr("src", primary); changed = true }
        }
        return changed ? try document.body()?.html() ?? html : html
    }
}

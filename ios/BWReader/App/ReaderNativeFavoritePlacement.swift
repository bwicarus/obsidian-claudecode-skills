import Foundation
import SwiftSoup

/// Turns the original favorite record into a free page placement. The
/// placement gets a new ID; learning gid/cid and the complete cards stay intact.
@MainActor
enum ReaderNativeFavoritePlacement {
    /// Event originals use the same durable page HTML contract as favorites.
    /// Serialize only on a drop; no hidden card body or webpage layout is built.
    static func semanticRecord(_ card: [String: Any]) throws -> [String: Any]? {
        guard let kind = card["kind"] as? String, ["weather", "news", "fact", "general", "images", "videos"].contains(kind) else { return nil }
        guard let cid = card["cid"] as? String, !cid.isEmpty else {
            throw ReaderNativeFavoritesService.Failure(message: "卡片原件缺少身份编号")
        }
        let data = card["data"] as? [String: Any] ?? [:]
        func string(_ value: Any?) -> String { ReaderNativeCardRules.string(value) }
        func escaped(_ value: Any?) -> String { ReaderNativeMathSyntax.escape(string(value)) }
        func markdown(_ value: Any?) throws -> String {
            try ReaderNativePageCardHTML.sanitize(ReaderNativeMarkdown.html(string(value))).content
        }
        let title = string(card["title"])
        var html: String, context: String
        switch kind {
        case "weather":
            html = "<div class=\"vc-if-w\"><div class=\"vc-if-wt\">" + escaped(data["lo"]) + "–" + escaped(data["hi"]) + "°C</div>"
                + "<div class=\"vc-if-wc\">" + escaped(data["cond"])
                + (data["precip"] == nil || data["precip"] is NSNull ? "" : " · 降水 " + escaped(data["precip"]) + "%") + "</div>"
                + "<div class=\"vc-if-ws\">" + escaped(data["loc"]) + " " + escaped(data["date"]) + "</div>"
            if !string(data["tip"]).isEmpty { html += "<div class=\"vc-if-tip\">" + escaped(data["tip"]) + "</div>" }
            html += "</div>"
            let temperature = data["lo"] == nil || data["lo"] is NSNull ? "" : string(data["lo"]) + "-" + string(data["hi"]) + "°C"
            let rain = data["precip"] == nil || data["precip"] is NSNull ? "" : "降水" + string(data["precip"]) + "%"
            context = (title.isEmpty ? "天气" : title) + ":" + [string(data["loc"]), string(data["date"]), string(data["cond"]), temperature, rain, string(data["tip"])].filter { !$0.isEmpty }.joined(separator: ",")
        case "news":
            let items = data["items"] as? [[String: Any]] ?? []
            html = "<div class=\"vc-if-n\">" + items.prefix(5).map { item in
                "<div class=\"vc-if-ni\"><div class=\"vc-if-nt\">" + escaped(item["t"]) + "</div><div class=\"vc-if-ns\">"
                    + escaped(item["s"]) + (string(item["src"]).isEmpty ? "" : " <span class=\"vc-if-src\">— " + escaped(item["src"]) + "</span>") + "</div></div>"
            }.joined() + "</div>"
            context = (title.isEmpty ? "新闻" : title) + ":" + items.map { string($0["t"]) + "(" + string($0["s"]) + ")" }.joined(separator: ";")
        case "fact":
            html = "<div class=\"vc-if-f\"><div class=\"vc-if-fa\">" + (try markdown(data["answer"])) + "</div>"
            if !string(data["detail"]).isEmpty { html += "<div class=\"vc-if-fd\">" + (try markdown(data["detail"])) + "</div>" }
            html += "</div>"
            context = title + ":" + string(data["answer"]) + " " + string(data["detail"])
        case "images", "videos":
            let items = data["items"] as? [[String: Any]] ?? []
            html = "<div class=\"vc-ig\">"
            var descriptions: [String] = []
            for (index, item) in items.enumerated() where !ReaderNativeMediaArtifact.gone(item) {
                let i = String(index), itemTitle = string(item["title"])
                html += "<div class=\"vc-ig-cell\" data-i=\"" + i + "\""
                let isMap = kind == "images" && ReaderNativeMediaArtifact.map(string(item["url"])) != nil
                if isMap { html += " data-map-url=\"" + escaped(item["url"]) + "\"" }
                html += "><button type=\"button\" class=\"vc-ig-x\" data-i=\"" + i + "\" aria-label=\"移除\"><span class=\"rc-i rc-i-close\"></span></button>"
                if kind == "images" {
                    if isMap { html += "<button type=\"button\" class=\"vc-ig-map\" data-i=\"" + i + "\" aria-label=\"全屏地图\">⛶</button>" }
                    if let route = ReaderNativeMediaArtifact.imageRoute(item) {
                        html += "<img class=\"vc-ig-img\" data-i=\"" + i + "\""
                        if let aid = ReaderNativeMediaArtifact.assetID(item["aid"]) { html += " data-aid=\"" + escaped(aid) + "\"" }
                        html += " data-source-url=\"" + escaped(item["url"]) + "\" src=\"" + escaped(route) + "\" alt=\"" + escaped(itemTitle) + "\">"
                    } else { html += "<span class=\"rc-img-broken\">图片地址无效</span>" }
                    if !itemTitle.isEmpty { html += "<div class=\"vc-ig-t\">" + escaped(itemTitle) + "</div>" }
                    descriptions.append(itemTitle)
                } else {
                    let ref = ReaderNativeMediaArtifact.video(item), thumb = ReaderNativeMediaArtifact.thumbnail(item, video: ref)
                    let bili = ref["src"] == "bili"
                    let label = bili ? "B站" : ref["src"] == "yt" ? "YouTube" : string(item["src"])
                    html += "<span class=\"vc-vg-tag" + (bili ? " bili" : "") + "\">" + escaped(label) + "</span><div class=\"vc-vg-wrap\">"
                    if let route = ReaderNativeMediaRoute.route(thumb) {
                        html += "<img class=\"vc-ig-img\" data-i=\"" + i + "\" loading=\"lazy\" referrerpolicy=\"same-origin\" data-source-url=\"" + escaped(thumb) + "\" src=\"" + escaped(route) + "\" alt=\"\">"
                    } else { html += "<div class=\"vc-vg-empty\">无预览图</div>" }
                    html += "<button type=\"button\" class=\"vc-vg-play\" data-i=\"" + i + "\""
                    for key in ["id", "src", "url", "title"] { html += " data-video-" + key + "=\"" + escaped(ref[key]) + "\"" }
                    html += " aria-label=\"播放\">▶</button></div><div class=\"vc-ig-t\">" + escaped(itemTitle)
                    let channel = string(item["channel"])
                    if !channel.isEmpty { html += "<br><span class=\"vc-vg-ch\">" + escaped(channel) + "</span>" }
                    html += "</div>"
                    descriptions.append(itemTitle + (channel.isEmpty ? "" : "(" + channel + ")") + " " + string(item["url"]))
                }
                html += "</div>"
            }
            html += "</div>"
            context = (title.isEmpty ? (kind == "images" ? "配图" : "视频") : title) + ":" + descriptions.joined(separator: ";")
        default:
            let text = string(data["text"]).isEmpty ? string(card["brief"]) : string(data["text"])
            html = "<div class=\"vc-if-g\">" + (try markdown(text)) + "</div>"
            context = text.isEmpty ? title : text
        }
        let sources = card["sources"] as? [[String: Any]] ?? []
        if !sources.isEmpty {
            html += "<div class=\"vc-if-srcs\">" + sources.prefix(3).map { source in
                let label = string(source["title"]).isEmpty ? "来源" : string(source["title"]).components(separatedBy: ".")[0]
                // Sanitization below rejects executable links in source data.
                return "<a href=\"" + escaped(source["url"] ?? "#") + "\" target=\"_blank\" rel=\"noopener\">" + escaped(label) + "</a>"
            }.joined(separator: " · ") + "</div>"
        }
        html = try ReaderNativePageCardHTML.sanitize(html).content
        return ["id": cid, "cid": cid, "kind": kind, "raw": html, "text": context,
                "label": title.isEmpty ? "卡片" : title, "isHtml": true]
    }

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

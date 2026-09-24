import Foundation
import CryptoKit

/// Native resource authorization stays on the guarded local image routes.
enum ReaderNativeMediaRoute {
    static func route(_ source:String) -> String? {
        let raw = source.trimmingCharacters(in:.whitespacesAndNewlines)
        guard !raw.isEmpty,raw.utf16.count <= 8192 else { return nil }
        if raw.hasPrefix("data:image/") { return raw }
        if raw.range(of:"^/pdf/api/(?:page-image(?:\\?|$)|asset/|img-proxy(?:\\?|$)|card-asset(?:\\?|$))",options:.regularExpression) != nil { return raw }
        guard let url = URL(string:raw),url.scheme?.lowercased() == "https",url.host != nil,url.user == nil,url.password == nil,url.fragment == nil else { return nil }
        if url.host == "bwicarus-2.taile44d0c.ts.net",url.query == nil,url.path.range(of:"^/reader-card-asset/[a-f0-9]{16}$",options:.regularExpression) != nil {
            return "/pdf/api/card-asset?id=" + url.lastPathComponent
        }
        let safe = CharacterSet(charactersIn:"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()")
        return raw.addingPercentEncoding(withAllowedCharacters:safe).map { "/pdf/api/img-proxy?url=" + $0 }
    }
}

/// Original media-card data is sufficient for native presentation and actions.
/// Indices refer to the original array, including removed slots.
enum ReaderNativeMediaArtifact {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    static func text(_ value: Any?) -> String { value as? String ?? "" }
    static func gone(_ item: [String: Any]) -> Bool {
        (item["_gone"] as? NSNumber)?.boolValue == true
    }
    static func assetID(_ value: Any?) -> String? {
        let value = text(value).trimmingCharacters(in: .whitespacesAndNewlines)
        return value.range(of: "^[a-z]{2,4}_[a-f0-9]{4,12}$", options: .regularExpression) != nil ? value : nil
    }
    static func https(_ value: Any?) -> String? {
        let raw = text(value)
        guard !raw.isEmpty, raw == raw.trimmingCharacters(in: .whitespacesAndNewlines),
              let url = URL(string: raw), url.scheme?.lowercased() == "https", url.host != nil,
              url.user == nil, url.password == nil, url.fragment == nil else { return nil }
        return url.absoluteString
    }
    static func video(_ item: [String: Any]) -> [String: String] {
        var url = https(item["url"]) ?? "", id = "", source = ""
        if let parsed = URLComponents(string: url) {
            let host = (parsed.host ?? "").lowercased(), parts = parsed.path.split(separator: "/").map(String.init)
            if host == "youtu.be" { source = "yt"; id = parts.first ?? "" }
            else if ["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"].contains(host) {
                source = "yt"
                if parsed.path == "/watch" { id = parsed.queryItems?.first(where: { $0.name == "v" })?.value ?? "" }
                else if parts.count >= 2, ["embed", "shorts", "live"].contains(parts[0]) { id = parts[1] }
            } else if host == "bilibili.com" || host.hasSuffix(".bilibili.com") || host == "b23.tv" {
                source = "bili"
                if parts.count >= 2, parts[0].lowercased() == "video" { id = parts[1] }
            }
        }
        let hint = text(item["src"]).lowercased()
        if source.isEmpty, hint.range(of: "bili|b站|哔哩", options: .regularExpression) != nil { source = "bili" }
        if source.isEmpty, hint.range(of: "youtube|\\byt\\b", options: .regularExpression) != nil { source = "yt" }
        let pattern = source == "bili" ? "^(?:BV[0-9A-Za-z]{10}|av[0-9]{1,16})$" : "^[A-Za-z0-9_-]{11}$"
        if id.range(of: pattern, options: .regularExpression) == nil { id = "" }
        let fallback = text(item["id"])
        if id.isEmpty, fallback.range(of: pattern, options: .regularExpression) != nil { id = fallback }
        if !id.isEmpty, source != "bili" { source = "yt" }
        if url.isEmpty, !id.isEmpty { url = source == "bili" ? "https://www.bilibili.com/video/" + id : "https://www.youtube.com/watch?v=" + id }
        return ["id": id, "src": source, "url": url, "title": text(item["title"])]
    }
    static func thumbnail(_ item: [String: Any], video: [String: String]) -> String {
        let supplied = text(item["thumb"]).trimmingCharacters(in: .whitespacesAndNewlines)
        if ReaderNativeMediaRoute.route(supplied) != nil { return supplied }
        guard video["src"] == "yt", let id = video["id"], !id.isEmpty else { return "" }
        return "https://i.ytimg.com/vi/" + id + "/mqdefault.jpg"
    }
    static func imageRoute(_ item: [String: Any]) -> String? {
        if let aid = assetID(item["aid"]) { return "/pdf/api/asset/" + aid + "?proxy=1" }
        return ReaderNativeMediaRoute.route(text(item["url"]))
    }
    static func map(_ source: String) -> [String: Any]? {
        guard let url = URLComponents(string: source), let host = url.host?.lowercased() else { return nil }
        let google = host == "maps.googleapis.com" && url.path == "/maps/api/staticmap"
        let yandex = host == "static-maps.yandex.ru"
        guard google || yandex else { return nil }
        let query = url.queryItems ?? []
        func value(_ key: String) -> String { query.first(where: { $0.name == key })?.value ?? "" }
        func point(_ source: String, reversed: Bool = false) -> [Double]? {
            let pieces = source.split(separator: ",")
            guard pieces.count >= 2, let a = Double(pieces[0]), let b = Double(pieces[1]), a.isFinite, b.isFinite else { return nil }
            let pair = reversed ? [b, a] : [a, b]
            return abs(pair[0]) <= 90 && abs(pair[1]) <= 180 ? pair : nil
        }
        var marks: [[Double]] = []
        if google {
            for item in query where item.name == "markers" {
                let coordinate = (item.value ?? "").split(separator: "|").last.map(String.init) ?? ""
                if let pair = point(coordinate.trimmingCharacters(in: .whitespacesAndNewlines)) { marks.append(pair) }
            }
        } else {
            marks = value("pt").split(separator: "~").compactMap { point(String($0), reversed: true) }
        }
        guard let center = point(value(google ? "center" : "ll"), reversed: yandex) ?? marks.first else { return nil }
        if marks.isEmpty { marks = [center] }
        let zoom = Double(value(google ? "zoom" : "z")) ?? 5
        return ["lat": center[0], "lon": center[1], "zoom": zoom.isFinite ? min(19, max(2, zoom)) : 5, "marks": marks]
    }
    static func project(_ data: [String: Any]) -> [String: Any] {
        guard let detail = data["nativeDetail"] as? [String: Any], let card = detail["content"] as? [String: Any],
              let kind = card["kind"] as? String, ["images", "videos"].contains(kind),
              let originals = (card["data"] as? [String: Any])?["items"] as? [[String: Any]] else { return data }
        let controls = data["items"] as? [[String: Any]] ?? []
        var result = data
        result["items"] = controls.compactMap { control -> [String: Any]? in
            guard let index = control["index"] as? Int, originals.indices.contains(index), !gone(originals[index]) else { return nil }
            let item = originals[index]
            var value = control
            value["title"] = text(item["title"]); value["source"] = text(item["src"])
            let ref = kind == "videos" ? video(item) : nil
            value["video"] = ref as Any? ?? NSNull()
            value["nativeRoute"] = ref.flatMap { ReaderNativeMediaRoute.route(thumbnail(item, video: $0)) } ?? (ref == nil ? imageRoute(item) : nil) ?? ""
            let resourceKey = text(value["nativeRoute"]) + "\n" + (ref?["id"] ?? "") + "\n" + (ref?["src"] ?? "")
            let digest = SHA256.hash(data: Data(resourceKey.utf8)).map { String(format: "%02x", $0) }.joined()
            value["mediaID"] = text(control["mediaID"]) + ":" + digest
            let source = [text(item["page"]), text(item["source_url"]), ref?["url"] ?? text(item["url"])].first { !$0.isEmpty }
            value["sourceURL"] = https(source) ?? ""
            let map = map(text(item["url"]))
            value["isMap"] = map != nil; value["map"] = map as Any? ?? NSNull()
            return value
        }
        return result
    }
    static func selectionCommands(card: [String: Any], index: Int, action: String, selected: [String]) throws -> [[String: Any]] {
        guard let cid = card["cid"] as? String, !cid.isEmpty, cid.utf16.count <= 450,
              let kind = card["kind"] as? String, ["images", "videos"].contains(kind),
              let items = (card["data"] as? [String: Any])?["items"] as? [[String: Any]],
              items.indices.contains(index), !gone(items[index]), ["remove", "toggle"].contains(action) else {
            throw Failure(message: "图片已移除或更新")
        }
        let prefix = "card:" + cid + "/item:", id = prefix + String(index), item = items[index]
        if action == "remove" { return [["operation": "remove", "id": id]] }
        let wasSelected = selected.contains(id)
        var commands: [[String: Any]] = selected.filter { $0.hasPrefix(prefix) }.map { ["operation": "deselect", "id": $0] }
        if !wasSelected {
            let title = text(item["title"]), channel = text(item["channel"])
            let label = (title.isEmpty ? (kind == "videos" ? "视频" : "配图") : title) + (kind == "videos" ? "·视频" : "·图") + String(index + 1)
            let context = title + (channel.isEmpty ? "" : "(" + channel + ")") + " " + text(item["url"])
            let record: [String: Any] = ["id": id, "parentId": "card:" + cid, "kind": kind == "videos" ? "video-item" : "image-item",
                "label": String(decoding: label.utf16.prefix(240), as: UTF16.self),
                "text": String(decoding: context.utf16.prefix(500), as: UTF16.self),
                "source": ["cid": cid, "item": index], "meta": [String: Any](), "covers": [String]()]
            commands.append(["operation": "select", "id": id, "record": record])
        }
        return commands
    }
}

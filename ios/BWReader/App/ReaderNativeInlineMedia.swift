import Foundation
import CryptoKit
import SwiftSoup

/// Only images present in a displayed document get resource tokens. Parsing is
/// data-only and uses the same guarded local routes as native page cards.
@MainActor
final class ReaderNativeInlineMedia {
    private struct Document {
        let content: String
        let images: [String: String]
        let routes: [String: String]
        var cost: Int { content.utf8.count + routes.values.reduce(0) { $0 + $1.utf8.count } }
    }
    private var documents: [String: Document] = [:]
    private var order: [String] = []
    private var generation = UUID().uuidString

    func reset() { documents = [:]; order = []; generation = UUID().uuidString }

    func images(content: String, format: String) -> [String: String] {
        let key = SHA256.hash(data: Data((format + "\n" + content).utf8)).map { String(format: "%02x", $0) }.joined()
        if let saved = documents[key] { return saved.images }
        let html = format == "html" ? content : ReaderNativeMarkdown.html(content)
        guard let body = try? SwiftSoup.parseBodyFragment(html),
              let elements = try? body.select("img[src]").array() else { return [:] }
        var images: [String: String] = [:], routes: [String: String] = [:]
        for element in elements.prefix(64) {
            guard let source = try? element.attr("src"), images[source] == nil,
                  let route = ReaderNativeMediaRoute.route(source) else { continue }
            let token = "native-inline:" + generation + ":" + key + ":" + String(images.count)
            images[source] = token; routes[token] = route
        }
        while !order.isEmpty && (order.count >= 64 || documents.values.reduce(0, { $0 + $1.cost }) + content.utf8.count > 8 * 1024 * 1024) {
            documents.removeValue(forKey: order.removeFirst())
        }
        documents[key] = Document(content: content, images: images, routes: routes); order.append(key)
        return images
    }

    func resource(_ token: String, isCurrent: (String) -> Bool) -> String? {
        guard token.hasPrefix("native-inline:" + generation + ":") else { return nil }
        for document in documents.values {
            if let route = document.routes[token], isCurrent(document.content) { return route }
        }
        return nil
    }
}

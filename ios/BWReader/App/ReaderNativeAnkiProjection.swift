import Foundation
import SwiftSoup

/// Anki accepts HTML, while Reader keeps the original semantic Markdown.
/// Use the native parser without mounting a second card or evaluating scripts.
@MainActor
enum ReaderNativeAnkiProjection {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    static func html(_ source: String) throws -> String {
        let document = try SwiftSoup.parseBodyFragment(ReaderNativeMarkdown.html(source))
        for image in try document.select("img").array() { try validateImage(image.attr("src")) }
        for math in try document.select("span[data-reader-math]").array() {
            guard let bytes = Data(base64Encoded: try math.attr("data-reader-math")), let latex = String(data: bytes, encoding: .utf8) else { continue }
            let display = try math.attr("data-reader-display") == "1"
            try math.text((display ? "\\[" : "\\(") + latex + (display ? "\\]" : "\\)"))
        }
        let allowed = try Whitelist.relaxed().addTags("ruby", "rt", "rp", "span", "div", "del", "s", "input")
            .addAttributes("input", "type", "checked", "disabled")
            .removeProtocols("img", "src", "http", "https").preserveRelativeLinks(true)
        let result = try SwiftSoup.clean(document.body()?.html() ?? "", allowed) ?? ""
        guard result.utf16.count <= 64_000 else { throw Failure(message: "Anki HTML 投影超过大小上限") }
        return result
    }
    static func validateImage(_ value: String) throws {
        let source = value.trimmingCharacters(in: .whitespacesAndNewlines)
        func reject() -> Failure { .init(message: "Anki 图片需要已有媒体文件名或公开 HTTPS 地址") }
        guard !source.isEmpty, source.rangeOfCharacter(from: .controlCharacters) == nil else { throw reject() }
        if source.range(of: "^[A-Za-z][A-Za-z0-9+.-]*:", options: .regularExpression) == nil {
            guard source != ".", source != "..", source.rangeOfCharacter(from: CharacterSet(charactersIn: "/\\<>:\"|?*#")) == nil else { throw reject() }
            return
        }
        guard let url = URLComponents(string: source), url.scheme?.lowercased() == "https", let host = url.host?.lowercased(),
              host.contains("."), url.user == nil, url.password == nil, url.fragment == nil, url.port == nil || url.port == 443,
              host != "localhost", !host.contains(":"), host.range(of: "^[0-9.]+$", options: .regularExpression) == nil,
              !["localhost", "local", "lan", "internal", "home", "home.arpa"].contains(where: { host.hasSuffix("." + $0) }) else { throw reject() }
    }
}

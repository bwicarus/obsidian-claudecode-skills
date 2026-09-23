import Foundation
import cmark_gfm
import cmark_gfm_extensions

/// cmark parses GFM into a data string; SwiftSoup/TextKit draw the result.
/// This never creates a web document or fetches resources during parsing.
@MainActor
enum ReaderNativeMarkdown {
    private static let registered: Void = { cmark_gfm_core_extensions_ensure_registered() }()
    private static let cache: NSCache<NSString, NSString> = {
        let cache = NSCache<NSString, NSString>(); cache.countLimit = 64; cache.totalCostLimit = 4 * 1024 * 1024; return cache
    }()
    static func html(_ source: String) -> String {
        if let saved = cache.object(forKey: source as NSString) { return saved as String }
        _ = registered
        let prepared = ReaderNativeMathSyntax.prepare(source)
        guard let parser = cmark_parser_new(CMARK_OPT_DEFAULT) else { return ReaderNativeMathSyntax.escape(source) }
        defer { cmark_parser_free(parser) }
        for name in ["autolink", "strikethrough", "table", "tasklist"] {
            if let ext = cmark_find_syntax_extension(name) { cmark_parser_attach_syntax_extension(parser, ext) }
        }
        cmark_parser_feed(parser, prepared.text, prepared.text.utf8.count)
        guard let document = cmark_parser_finish(parser) else { return ReaderNativeMathSyntax.escape(source) }
        defer { cmark_node_free(document) }
        // Keep ruby and other original HTML as data. The native renderer only
        // recognizes supported elements and never executes scripts or URLs.
        guard let buffer = cmark_render_html(document, CMARK_OPT_UNSAFE | CMARK_OPT_HARDBREAKS, cmark_parser_get_syntax_extensions(parser)) else {
            return ReaderNativeMathSyntax.escape(source)
        }
        defer { free(buffer) }
        let result = ReaderNativeMathSyntax.restore(String(cString: buffer), prepared: prepared)
        cache.setObject(result as NSString, forKey: source as NSString, cost: source.utf8.count + result.utf8.count)
        return result
    }
}

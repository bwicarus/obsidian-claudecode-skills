import Foundation

/// Native resource authorization stays on the guarded local image routes.
enum ReaderNativeMediaRoute {
    static func route(_ source:String) -> String? {
        let raw = source.trimmingCharacters(in:.whitespacesAndNewlines)
        guard !raw.isEmpty,raw.utf16.count <= 8192 else { return nil }
        if raw.hasPrefix("data:image/") { return raw }
        if raw.range(of:"^/pdf/api/(?:page-image(?:\\?|$)|asset/|img-proxy(?:\\?|$)|card-asset(?:\\?|$))",options:.regularExpression) != nil { return raw }
        guard let url = URL(string:raw),url.scheme?.lowercased() == "https",url.host != nil,url.user == nil,url.password == nil else { return nil }
        if url.host == "bwicarus-2.taile44d0c.ts.net",url.path.range(of:"^/reader-card-asset/[a-f0-9]{16}$",options:.regularExpression) != nil {
            return "/pdf/api/card-asset?id=" + url.lastPathComponent
        }
        let safe = CharacterSet(charactersIn:"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()")
        return raw.addingPercentEncoding(withAllowedCharacters:safe).map { "/pdf/api/img-proxy?url=" + $0 }
    }
}

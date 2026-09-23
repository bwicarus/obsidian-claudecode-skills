import Foundation

/// Protect math before Markdown parses backslashes, underscores and pipes.
/// Code and HTML attributes stay literal; unmatched delimiters remain visible.
enum ReaderNativeMathSyntax {
    struct Formula { let token: String, latex: String, original: String; let display: Bool }
    struct Prepared { let text: String; let formulas: [Formula] }
    static func prepare(_ source: String) -> Prepared {
        let pattern = #"(?s:```.*?(?:```|\z)|~~~.*?(?:~~~|\z)|<code\b[^>]*>.*?</code>|<pre\b[^>]*>.*?</pre>)|`+[^`]*`+|<[^>\n]+>|(?<!\\)\\\(([\s\S]*?)\\\)|(?<!\\)\\\[([\s\S]*?)\\\]|(?<!\\)\$\$([\s\S]*?)\$\$|(?<![\\$])\$(?![\s$])([^\n$]*?\S)\$(?![\d$])"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return Prepared(text: source, formulas: []) }
        let ns = source as NSString, prefix = "BWNativeMath" + UUID().uuidString.replacingOccurrences(of: "-", with: "")
        var output = "", end = 0, formulas: [Formula] = []
        for match in regex.matches(in: source, range: NSRange(location: 0, length: ns.length)) {
            guard let group = (1...4).first(where: { match.range(at: $0).location != NSNotFound }) else { continue }
            let latex = ns.substring(with: match.range(at: group))
            guard !latex.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
            output += ns.substring(with: NSRange(location: end, length: match.range.location - end))
            let token = prefix + "N" + String(formulas.count) + "Z"
            formulas.append(Formula(token: token, latex: latex, original: ns.substring(with: match.range), display: group == 2 || group == 3))
            output += token; end = NSMaxRange(match.range)
        }
        output += ns.substring(from: end)
        return Prepared(text: output, formulas: formulas)
    }
    static func escape(_ value: String) -> String {
        value.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
    }
    static func restore(_ html: String, prepared: Prepared) -> String {
        prepared.formulas.reduce(html) { result, formula in
            let encoded = Data(formula.latex.utf8).base64EncodedString()
            let span = "<span data-reader-math=\"\(encoded)\" data-reader-display=\"\(formula.display ? 1 : 0)\">\(escape(formula.original))</span>"
            return result.replacingOccurrences(of: formula.token, with: span)
        }
    }
}

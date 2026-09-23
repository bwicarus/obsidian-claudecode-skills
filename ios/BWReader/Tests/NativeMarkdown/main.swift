import Foundation

let protectedCases: [(String, [String])] = [
    (#"式 \(x^2\) と \[\frac{a}{b}\]"#, ["x^2", #"\frac{a}{b}"#]),
    (#"a $x+1$ b $$\sum_i x_i$$"#, ["x+1", #"\sum_i x_i"#]),
    (#"`$x$` and \(y\)"#, ["y"]),
    ("```tex\n$x$\n```\n$z$", ["z"]),
    ("~~~tex\n$x$\n~~~\n$z$", ["z"]),
    (#"price $5 and $10; escaped \$x$"#, []),
    (#"<code>\(x\)</code><img alt='$x$'> $y$"#, ["y"]),
    (#"unclosed \(x"#, []),
    ("```tex\n$x$", [])
]
for (source, expected) in protectedCases {
    let value = ReaderNativeMathSyntax.prepare(source)
    precondition(value.formulas.map(\.latex) == expected, "math delimiters changed literal code / currency: \(source)")
    if expected.isEmpty { precondition(value.text == source) }
    else { precondition(!ReaderNativeMathSyntax.restore(value.text, prepared: value).contains(value.formulas[0].token)) }
}
await MainActor.run {
    let html = ReaderNativeMarkdown.html("# Heading\n\n| 名前 | 読み |\n|---|---|\n| 日本 | にほん |\n\n3. three\n4. four\n\n- [x] yes\n\n~~old~~ **bold**\n\n<ruby>漢字<rt>かんじ</rt></ruby>\n\n\\(\\frac{a}{b}\\)")
    for part in ["<h1>", "<table>", "<ol start=\"3\">", "checked", "<del>", "<strong>", "<ruby>", "data-reader-math="] {
        precondition(html.contains(part), "native GFM lost \(part): \(html)")
    }
    precondition(ReaderNativeMarkdown.html("`$x$`").contains("<code>$x$</code>"))
    let again = ReaderNativeMarkdown.html("# Heading")
    precondition(again == ReaderNativeMarkdown.html("# Heading"))

    let media = ReaderNativeInlineMedia()
    let content = "![図](https://example.com/figure.png)\n\n| 図 |\n|---|\n| ![表](https://example.com/table.png) |"
    let images = media.images(content: content, format: "markdown")
    precondition(images.count == 2 && images == media.images(content: content, format: "markdown"))
    let token = images["https://example.com/figure.png"]!
    precondition(media.resource(token, isCurrent: { $0 == content }) == "/pdf/api/img-proxy?url=https%3A%2F%2Fexample.com%2Ffigure.png")
    precondition(media.resource(token, isCurrent: { _ in false }) == nil, "removed content still authorized an image")
    precondition(media.images(content: "<img src='javascript:bad'><img src='file:///tmp/private'><img src='/api/admin'>", format: "html").isEmpty)
    media.reset()
    precondition(media.resource(token, isCurrent: { _ in true }) == nil, "previous document generation retained authority")
}
print("Native Markdown: GFM, math boundaries, native image routes and stale-token revocation passed")
try await MainActor.run {
    let exported = try ReaderNativeAnkiProjection.html("**題**\n\n<ruby>漢字<rt>かんじ</rt></ruby>\n\n\\(x^2\\)\n\n![図](https://example.com/image.png)\n\n<img src='existing.png' onerror='bad()'><script>bad()</script>")
    for part in ["<strong>", "<ruby>", "\\(x^2\\)", "https://example.com/image.png", "existing.png"] { precondition(exported.contains(part), "Anki projection lost \(part): \(exported)") }
    precondition(!exported.contains("onerror") && !exported.contains("<script") && !exported.contains("data-reader-math"))
    for source in ["https://127.0.0.1/x", "https://localhost/x", "https://host.local/x", "https://user:password@example.com/x", "javascript:bad", "file:///tmp/x", "../private", "https://example.com:444/x"] {
        do { try ReaderNativeAnkiProjection.validateImage(source); preconditionFailure("unsafe media accepted: \(source)") } catch is ReaderNativeAnkiProjection.Failure {}
    }
}

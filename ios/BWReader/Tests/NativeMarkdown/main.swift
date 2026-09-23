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
}
print("Native Markdown: GFM tables, lists, code, ruby, math delimiters and cache passed")

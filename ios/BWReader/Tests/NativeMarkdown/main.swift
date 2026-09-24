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
    typealias Review = ReaderNativeReviewFaces
    let explicit = try Review.project(front: "<p>Question</p>", back: "<p>Question</p><hr id='answer'><p>Answer</p>", format: "html")
    precondition(!explicit.back.contains("Question") && explicit.back.contains("Answer") && explicit.mode == "append")
    let repeated = try Review.project(front: "<p>Question</p>", back: "<p>Question</p><hr><p>Answer</p>", format: "html")
    precondition(!repeated.back.contains("Question") && repeated.mode == "append")
    let unrelated = try Review.project(front: "<p>Question</p>", back: "<p>Different</p><hr><p>Answer</p>", format: "html")
    precondition(unrelated.back.contains("Different") && unrelated.back.contains("<hr"))
    let nested = try Review.project(front: "<p>Question</p>", back: "<div><hr id='answer'><p>Nested original</p></div>", format: "html")
    precondition(nested.back.contains("Nested original"))
    let cloze = try Review.project(front: "<span class='cloze'>[…]</span>", back: "<span class='cloze'>答案</span>", format: "html")
    precondition(cloze.mode == "replace" && cloze.back.contains("答案"))
    let japanese = try Review.project(front: "<div class='jp-sent'>日本語</div>", back: "<div class='jp-sent'><ruby>日本語<rt>にほんご</rt></ruby></div>", format: "html")
    precondition(japanese.mode == "replace" && japanese.back.contains("<ruby>"))
    let ruby = try Review.project(front: "<p><ruby>漢字<rt>かんじ</rt></ruby></p>", back: "<p><ruby>漢字<rt>別読み</rt></ruby></p><hr><p>説明</p>", format: "html")
    precondition(!ruby.back.contains("別読み") && ruby.back.contains("説明"))
    let metadata = try Review.project(front: "題", back: "<p>答案</p><div>来源： 本书 卡片编号： abcdef</div><!--@src:book.pdf-->", format: "html")
    precondition(metadata.back.contains("答案") && !metadata.back.contains("来源"))
    let natural = try Review.project(front: "題", back: "<p>来源：河流源头，这是学习内容。</p>", format: "html")
    precondition(natural.back.contains("河流源头"))
    let material = try Review.project(front: "題", back: "<p>答案</p><div class='url'><a href='/pdf/view?file=books%2Fa.pdf&page=2'>来源</a></div><div class='more'>保留补充</div>", format: "html")
    precondition(!material.back.contains("books") && material.back.contains("保留补充") && material.back.contains("<details"))
    let unsafe = try Review.project(front: "<script>bad()</script>题", back: "<div class='url'><a href='/pdf/view?file=%252e%252e%252fsecret&page=2'>正文链接</a></div><img src='https://example.com/x' onerror='bad()'>", format: "html")
    precondition(!unsafe.front.contains("<script") && !unsafe.back.contains("onerror") && unsafe.back.contains("正文链接"))
    let markdown = try Review.project(front: "**題**\n\n|A|B|\n|-|-|\n|1|2|", back: "答： $x^2$\n\n![図](https://example.com/x.png)", format: "markdown")
    precondition(markdown.front.contains("<strong>") && markdown.front.contains("<table>"))
    precondition(markdown.back.contains("data-reader-math") && markdown.back.contains("example.com/x.png"))
    let original: [String: Any] = ["id": "anki_card_123", "front": "題", "back": "答", "native_review_faces": true, "face_format": "markdown"]
    let state = Review.state(["current": original, "previous": original, "next": original, "showingAnswer": false, "ratingSaving": true])
    precondition((state["current"] as! [String: Any])["id"] as? String == "anki_card_123" && state["ratingSaving"] as? Bool == true)
    precondition(original["front"] as? String == "題" && state["showingAnswer"] as? Bool == false)
    let cached = try Review.project(front: "題", back: "答", format: "markdown")
    precondition(cached.front == (state["current"] as! [String: Any])["front"] as? String)
}
print("Native review faces: dividers, cloze, ruby, proven provenance, original content and GFM passed")
try await MainActor.run {
    let exported = try ReaderNativeAnkiProjection.html("**題**\n\n<ruby>漢字<rt>かんじ</rt></ruby>\n\n\\(x^2\\)\n\n![図](https://example.com/image.png)\n\n<img src='existing.png' onerror='bad()'><script>bad()</script>")
    for part in ["<strong>", "<ruby>", "\\(x^2\\)", "https://example.com/image.png", "existing.png"] { precondition(exported.contains(part), "Anki projection lost \(part): \(exported)") }
    precondition(!exported.contains("onerror") && !exported.contains("<script") && !exported.contains("data-reader-math"))
    for source in ["https://127.0.0.1/x", "https://localhost/x", "https://host.local/x", "https://user:password@example.com/x", "javascript:bad", "file:///tmp/x", "../private", "https://example.com:444/x"] {
        do { try ReaderNativeAnkiProjection.validateImage(source); preconditionFailure("unsafe media accepted: \(source)") } catch is ReaderNativeAnkiProjection.Failure {}
    }
}

import Foundation

func wordCardRecord(_ notes: [[String: Any]]) throws -> String {
    String(decoding: try JSONSerialization.data(withJSONObject: ["value": ["payload": notes]]), as: UTF8.self)
}
do {
    func note(_ cid: String, _ word: String, _ content: String, _ created: Int) -> [String: Any] {
        ["created": created, "html": ["cid": cid, "label": word, "content": content, "bind": ["kind": "page-chars", "text": word]]]
    }
    let records = try [wordCardRecord([
        note("later", "漢 字", "<b>含义</b><span class='rc-note-dict'>重复字典</span><script>bad()</script>", 20),
        note("ignore", "別", "不相关", 5), note("earlier", "漢字", "旧内容", 10)
    ]), wordCardRecord([note("other-book", "漢字", "另一本书", 15)])]
    let result = try ReaderNativeWordCards.project(records, lemma: "漢字", word: "漢 字")
    let index = try ReaderNativeWordCards.makeIndex([("a", records[0]), ("b", records[1])])
    precondition(index["漢字"] == Set(["a", "b"]) && index["別"] == Set(["a"]))
    precondition(result.map(\.cid) == ["earlier", "other-book", "later"])
    precondition(result.last?.text == "含义", "embedded dictionary/scripts leaked into related card")
    let changed = try ReaderNativeWordCards.project([wordCardRecord([note("earlier", "別", "改绑", 10)])], lemma: "漢字", word: "漢字")
    precondition(changed.isEmpty, "old word binding survived live note edit")
    let sameID = try ReaderNativeWordCards.project([wordCardRecord([
        note("same", "漢字", "旧版本", 10), note("same", "別", "新版本", 20)
    ])], lemma: "漢字", word: "漢字")
    precondition(sameID.isEmpty, "stale duplicate cid projected")
}
print("Native related cards: cross-book identity, live content, rebinding, order and embedded-content filtering passed")

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
    typealias Placement = ReaderNativeFavoritePlacement
    let longText = String(repeating: "保留原文", count: 7000)
    let original: [String: Any] = ["cid": "original-card", "kind": "fact", "title": "原题",
        "data": ["answer": "**重点**\n\n" + longText, "detail": "<ruby>接種<rt>せっしゅ</rt></ruby><script>evil()</script>"],
        "sources": [["title": "原始来源", "url": "https://example.com/source"], ["title": "bad", "url": "javascript:evil()"]]]
    let projected = try Placement.semanticRecord(original)!
    let body = try Placement.body(projected, file: "localbook:book", page: 8, x: 0.2, y: 0.7, pageWidth: 600)
    let saved = body["html"] as! [String: Any], html = saved["content"] as! String
    precondition(saved["cid"] as? String == "original-card" && saved["bind"] == nil)
    precondition((body["anchor"] as! [String: Any])["page"] as? Int == 8)
    for value in [longText, "<strong>", "<ruby>", "https://example.com/source"] { precondition(html.contains(value), "placement lost original \(value.prefix(40))") }
    precondition(!html.contains("<script") && !html.contains("javascript:"))
    precondition((saved["contextText"] as! String).contains(longText))
    let another = try Placement.body(projected, file: "localbook:book", page: 9, x: 0, y: 1, pageWidth: 600)
    precondition(body["id"] as? String != another["id"] as? String, "placements must not reuse content identity as their ID")
    let weather = try Placement.semanticRecord(["cid": "weather", "kind": "weather", "data": ["lo": 0, "hi": 12, "precip": 0, "cond": "<晴>"]])!
    precondition((weather["raw"] as! String).contains("0–12°C") && (weather["raw"] as! String).contains("降水 0%"))
    precondition((weather["raw"] as! String).contains("&lt;晴&gt;"))
    for invalid in [(Double.nan, 0.1), (-0.1, 0.5), (0.4, 1.1)] {
        do { _ = try Placement.body(projected, file: "localbook:book", page: 8, x: invalid.0, y: invalid.1, pageWidth: 600); preconditionFailure("invalid drop accepted") }
        catch is ReaderNativeFavoritesService.Failure {}
    }
    let media: [String: Any] = ["kind": "images", "cid": "images", "title": "地图与配图", "data": ["items": [
        ["url": "https://example.com/removed.png", "title": "removed", "_gone": 1],
        ["url": "https://example.com/image.png", "aid": "im_abc123", "title": "原图<script>"],
        ["url": "https://maps.googleapis.com/maps/api/staticmap?center=35.68,139.69&zoom=9&markers=35.68%2C139.69", "title": "地图"]]]]
    let imageRecord = try Placement.semanticRecord(media)!
    let imageHTML = imageRecord["raw"] as! String
    precondition(!imageHTML.contains("removed.png") && !imageHTML.contains("data-i=\"0\""))
    for value in ["data-i=\"1\"", "data-aid=\"im_abc123\"", "/pdf/api/asset/im_abc123?proxy=1", "data-map-url", "vc-ig-map", "原图&lt;script&gt;"] { precondition(imageHTML.contains(value), "media placement lost \(value)") }
    let video: [String: Any] = ["kind": "videos", "cid": "videos", "data": ["items": [
        ["url": "https://youtu.be/abc_DEF-1234", "title": "视频", "channel": "来源"],
        ["url": "https://www.bilibili.com/video/BV1xx411c7mD", "title": "B站视频"]]]]
    let videoRecord = try Placement.semanticRecord(video)!, videoHTML = videoRecord["raw"] as! String
    for value in ["data-video-id=\"abc_DEF-1234\"", "data-video-src=\"yt\"", "data-video-src=\"bili\"", "vc-vg-play", "来源"] { precondition(videoHTML.contains(value), "video placement lost \(value)") }
}
print("Native artifact placement: complete originals, Markdown, identity, sources, safe HTML and PDF coordinates passed")
do {
    typealias Media = ReaderNativeMediaArtifact
    let card: [String: Any] = ["cid": "media", "kind": "images", "data": ["items": [
        ["title": "removed", "_gone": 1], ["title": "full title", "url": "https://example.com/a", "aid": "im_abcd"],
        ["title": "third", "url": "https://example.com/b"]]]]
    let data: [String: Any] = ["nativeDetail": ["content": card], "items": [
        ["index": 0, "mediaID": "native-artifact:a"], ["index": 1, "mediaID": "native-artifact:b"], ["index": 2, "mediaID": "native-artifact:c"]]]
    let rows = Media.project(data)["items"] as! [[String: Any]]
    precondition(rows.count == 2 && rows[0]["index"] as? Int == 1)
    precondition(rows[0]["nativeRoute"] as? String == "/pdf/api/asset/im_abcd?proxy=1")
    precondition(rows[0]["title"] as? String == "full title")
    precondition((rows[0]["mediaID"] as! String).hasPrefix("native-artifact:b:"))
    precondition(Media.https("https://user:pass@example.com/a") == nil && Media.https("javascript:evil()") == nil)
    precondition(Media.video(["url": "https://youtube.com.evil.test/watch?v=abc_DEF-1234"])["id"] == "")
    precondition(Media.video(["url": "https://www.youtube.com/shorts/abc_DEF-1234"])["id"] == "abc_DEF-1234")
    precondition(Media.video(["src": "b站", "id": "BV1xx411c7mD"])["src"] == "bili")
    precondition(Media.map("https://example.com/maps.googleapis.com/maps/api/staticmap?center=35,139") == nil)
    let yandex = Media.map("https://static-maps.yandex.ru/1.x/?ll=139.69,35.68&z=12&pt=139.69,35.68,pm2rdm")!
    precondition(yandex["lat"] as? Double == 35.68 && yandex["lon"] as? Double == 139.69)
    var state = ReaderNativeContextSelection()
    func apply(_ index: Int, _ action: String, _ now: Double) throws {
        var next = state
        for command in try Media.selectionCommands(card: card, index: index, action: action, selected: state.projection["selected"] as! [String]) { try next.apply(command, now: now) }
        state = next
    }
    try apply(1, "toggle", 0)
    precondition(state.projection["selected"] as! [String] == ["card:media/item:1"])
    try apply(2, "toggle", 10)
    precondition(state.projection["selected"] as! [String] == ["card:media/item:2"], "sibling selection must be released")
    try apply(2, "toggle", 20)
    precondition((state.projection["selected"] as! [String]).isEmpty)
    try apply(1, "toggle", 30)
    state.expire(now: 331)
    precondition((state.projection["selected"] as! [String]).isEmpty, "native media must share expiry")
    try apply(1, "toggle", 400); try apply(1, "remove", 401)
    precondition((state.projection["selected"] as! [String]).isEmpty)
    do { try apply(0, "toggle", 402); preconditionFailure("removed media accepted") } catch is Media.Failure {}
}
print("Native media: routes, original indices, maps/video identity, selection exclusion, expiry and removal passed")
await MainActor.run {
    typealias Review = ReaderNativeReviewFaces
    let explicit = Review.source(["source_ref": "book:localbook:abc#p45", "source": ["url": "https://example.com/note"]])
    precondition(explicit.file == "localbook:abc" && explicit.page == 45)
    let typed = Review.source(["source": ["documentId": "localbook:abc", "location": ["kind": "pdf", "page": 12]]])
    precondition(typed.file == "localbook:abc" && typed.page == 12)
    let original = "<div class='src'><a href='https://example.com/pdf/view?file=Books%2Fa.pdf&amp;page=9'>出处</a></div>"
    let legacy = Review.source(["question": original, "answer": original])
    precondition(legacy.file == "Books/a.pdf" && legacy.page == 9)
    let ambiguous = Review.source(["question": original, "answer": original.replacingOccurrences(of: "page=9", with: "page=10")])
    precondition(ambiguous.file == nil && ambiguous.url == nil)
    for file in ["../secret.pdf", "%252e%252e/secret.pdf", "C:/secret.pdf", "/secret.pdf", "Books//a.pdf"] {
        precondition(Review.source(["source_ref": "book:" + file + "#p1"]).file == nil)
    }
    precondition(Review.source(["source_url": "javascript:bad()"]).url == nil)
    precondition(Review.source(["source_url": "https://user:pass@example.com/private"]).url == nil)
    let epub = Review.source(["source": ["documentId": "localbook:epub", "location": ["kind": "epub", "cfi": "epubcfi(/6/4)"]]])
    precondition(epub.file == nil && epub.locations.count == 1)
}
try await MainActor.run {
    let exported = try ReaderNativeAnkiProjection.html("**題**\n\n<ruby>漢字<rt>かんじ</rt></ruby>\n\n\\(x^2\\)\n\n![図](https://example.com/image.png)\n\n<img src='existing.png' onerror='bad()'><script>bad()</script>")
    for part in ["<strong>", "<ruby>", "\\(x^2\\)", "https://example.com/image.png", "existing.png"] { precondition(exported.contains(part), "Anki projection lost \(part): \(exported)") }
    precondition(!exported.contains("onerror") && !exported.contains("<script") && !exported.contains("data-reader-math"))
    for source in ["https://127.0.0.1/x", "https://localhost/x", "https://host.local/x", "https://user:password@example.com/x", "javascript:bad", "file:///tmp/x", "../private", "https://example.com:444/x"] {
        do { try ReaderNativeAnkiProjection.validateImage(source); preconditionFailure("unsafe media accepted: \(source)") } catch is ReaderNativeAnkiProjection.Failure {}
    }
}

do {
    var selection = ReaderNativeContextSelection(expireMs: 1000)
    func answer(_ id: String, _ text: String, index: Int, card: String = "card-a", parent: String = "", covers: [String] = []) -> [String: Any] {
        ["id": id, "text": text, "label": id, "parentId": parent, "covers": covers,
         "kind": index < 0 ? "review-answer" : "review-answer-segment", "source": [:],
         "meta": ["review_mode": true, "card_key": card, "question": "为什么？", "answer_id": "answer",
            "segment_index": index, "card": ["entity_id": card]]]
    }
    for record in [answer("answer", "完整第一段\n\n完整第二段", index: -1, covers: ["p1", "p2"]),
                   answer("p1", "完整第一段", index: 0, parent: "answer"),
                   answer("p2", "完整第二段", index: 1, parent: "answer"),
                   answer("other", "其他卡的回答", index: -1, card: "card-b")] {
        try selection.apply(["operation": "upsert", "id": record["id"]!, "record": record], now: 0)
    }
    try selection.selectReview("p2", cardKey: "card-a", on: true, now: 0)
    try selection.selectReview("p1", cardKey: "card-a", on: true, now: 0)
    precondition(selection.reviewPairs(cardKey: "card-a").first?["answer"] as? String == "完整第一段\n\n完整第二段")
    try selection.selectReview("answer", cardKey: "card-a", on: true, now: 0)
    precondition(selection.reviewPairs(cardKey: "card-a").first?["selection_ids"] as? [String] == ["answer"], "整条回答须覆盖段落，不能重复拼入")
    try selection.selectReview("answer", cardKey: "card-a", on: false, now: 0.2)
    precondition(selection.reviewPairs(cardKey: "card-a").first?["selection_ids"] as? [String] == ["p1", "p2"], "取消整条后保留此前选中的段落")
    do { try selection.selectReview("other", cardKey: "card-a", on: true, now: 0.2); preconditionFailure("其他卡不能被当前操作选中") }
    catch is ReaderNativeContextSelection.Failure {}
    precondition(selection.reviewPairs(cardKey: "card-b").isEmpty)
    selection.expire(now: 1.1)
    precondition(selection.reviewPairs(cardKey: "card-a").isEmpty, "过期的回答不能带入草稿")
}

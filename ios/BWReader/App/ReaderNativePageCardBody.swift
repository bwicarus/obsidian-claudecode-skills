import SwiftUI
import UIKit
import SwiftSoup

/// 钉在书页上的卡片**正文**，逐项照原版 `.vc-card.vc-pinned` 画。
///
/// ⚠ 为什么不复用侧栏的 `ReaderNativeConversationArtifacts`：那是侧栏"生成物"
/// 的样子 —— 自带图标 + 标题 + 「带入对话」+ 圆角底板。塞进页卡里就是
/// **卡里套卡**、标题出现两遍（2026-09-23 用户截图对比："和我原来做的完全
/// 不一样，且还是有没有必要的嵌套"）。原版页卡是**单层**：卡头只有一行标题，
/// 正文直接排内容。
///
/// ⚠ 卡片正文是原版渲染器产出的 HTML（`vc-if-f` / `vc-dict-sec` …），样式全靠
/// class。通用富文本不认 class，于是结论、细节、词典段被压成同一种字，
/// 词头和读音挤成一行（截图里的「エボラ出血熱エボラしゅっけつねつ」）。
/// 这里按 class 分派，每一类的字号/字色/间距都取自原版 CSS，出处写在旁边。
@MainActor
struct ReaderNativePageCardBody: View {
    let parts: [ReaderNativeConversationPart]
    @ObservedObject var model: ReaderNativeConversationModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(parts) { part in
                content(part)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func content(_ part: ReaderNativeConversationPart) -> some View {
        let format = part.string("format")
        if part.kind == "fact" {
            ReaderNativePageCardFact(answer: part.string("answer").isEmpty ? part.text : part.string("answer"),
                                     detail: part.string("detail"), onSelection: selection(part), imageModel: model)
        } else if (part.kind == "general" || part.kind == "knowledge"),
                  format == "html" || part.string("text").range(of: "<[a-z][^>]*>", options: [.regularExpression, .caseInsensitive]) != nil {
            ReaderNativeCardHTML(html: part.string("text").isEmpty ? part.text : part.string("text"),
                                 onSelection: selection(part),inlineImages:part.data["inlineImages"] as? [String:String] ?? [:],imageModel:model)
        } else if part.kind == "general" || part.kind == "knowledge" {
            // .vc-if-g{font-size:13px;line-height:1.55}
            ReaderNativeRichDocument(content: part.string("text").isEmpty ? part.text : part.string("text"),
                                 format: format.isEmpty ? "markdown" : format, onSelection: selection(part),
                                 imageModel: model,
                                 font: .preferredFont(forTextStyle: .body), color: ReaderNativeCardInk.text)
        } else {
            // 学习卡 / 天气 / 新闻 / 图片：交给已有的原生实现，但**不要它那层外壳**。
            ReaderNativeConversationArtifacts(parts: [part], model: model, bare: true)
        }
    }

    private func selection(_ part: ReaderNativeConversationPart) -> (String) -> Void {
        { text in
            let id = part.string("selectId")
            guard !id.isEmpty else { return }
            model.updateTextSelection(id: id, text: text)
        }
    }
}

/// 原版卡面上的几种字色。
enum ReaderNativeCardInk {
    /// .vc-card{color:#f2f2f7}
    static let text = UIColor(red: 0xf2 / 255, green: 0xf2 / 255, blue: 0xf7 / 255, alpha: 1)
    /// .vc-if-fd{color:#b8c6e2}
    static let detail = UIColor(red: 0xb8 / 255, green: 0xc6 / 255, blue: 0xe2 / 255, alpha: 1)
    /// .rnd-ex-zh{color:#8fa3c8}
    static let exampleGloss = Color(red: 0x8f / 255, green: 0xa3 / 255, blue: 0xc8 / 255)
    /// --rc-text-dim
    static let dim = Color(red: 235 / 255, green: 235 / 255, blue: 245 / 255).opacity(0.38)
    /// .wp-pitch .pm.hi 上划线（--rc-accent-cyan）
    static let pitch = Color(red: 0x64 / 255, green: 0xd2 / 255, blue: 0xff / 255)
    /// .wp-pitch .pm.drop 降调竖线
    static let drop = Color(red: 1, green: 0x8a / 255, blue: 0x8a / 255)
    /// 词典段的虚线 / 例句分隔线
    static let dash = Color(red: 160 / 255, green: 160 / 255, blue: 180 / 255)
}

/// `.vc-if-f`：结论（.vc-if-fa 15px/600）+ 细节（.vc-if-fd 12px #b8c6e2，上距 3px）。
@MainActor
private struct ReaderNativePageCardFact: View {
    let answer: String
    let detail: String
    var answerFormat = "markdown"
    var detailFormat = "markdown"
    let onSelection: (String) -> Void
    var inlineImages: [String:String] = [:]
    var imageModel: ReaderNativeConversationModel? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !answer.isEmpty {
                ReaderNativeRichDocument(content: answer, format: answerFormat, onSelection: onSelection, inlineImages:inlineImages,imageModel:imageModel,
                                     font: .preferredFont(forTextStyle: .headline), color: ReaderNativeCardInk.text)
            }
            if !detail.isEmpty, detail != answer {
                ReaderNativeRichDocument(content: detail, format: detailFormat, onSelection: onSelection, inlineImages:inlineImages,imageModel:imageModel,
                                     font: .preferredFont(forTextStyle: .body), color: ReaderNativeCardInk.detail)
            }
        }
    }
}

/// 天气（原版 .vc-if-w）：温度 26px 半粗 → 天况 14px → 地点日期 12px → 细线 + 提示 12px。与侧栏同一套数值。
private struct ReaderNativePageCardWeather: View {
    let temperature: String, condition: String, place: String, tip: String
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if !temperature.isEmpty {
                Text(temperature).font(.system(size: 26, weight: .semibold)).kerning(-0.5).monospacedDigit()
            }
            if !condition.isEmpty { Text(condition).font(.system(size: 14)) }
            if !place.isEmpty { Text(place).font(.system(size: 12)).foregroundStyle(ReaderNativeCardStyle.muted) }
            if !tip.isEmpty {
                Rectangle().fill(ReaderNativeCardStyle.hairline).frame(height: 0.5).padding(.top, 6)
                Text(tip).font(.system(size: 12)).foregroundStyle(ReaderNativeCardStyle.tip).padding(.top, 4)
            }
        }
        .foregroundStyle(ReaderNativeCardStyle.text)
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 新闻（原版 .vc-if-n）：标题 13px 半粗 #e8eefb、摘要 12px #9fb0cf、条目间细线。
private struct ReaderNativePageCardNews: View {
    let items: [(title: String, summary: String, source: String)]
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                if index > 0 { Rectangle().fill(Color.white.opacity(0.08)).frame(height: 0.5) }
                VStack(alignment: .leading, spacing: 1) {
                    if !item.title.isEmpty { Text(item.title).font(.system(size: 13, weight: .semibold)).foregroundStyle(ReaderNativeCardStyle.newsTitle) }
                    if !item.summary.isEmpty { Text(item.summary).font(.system(size: 12)).foregroundStyle(ReaderNativeCardStyle.newsSummary) }
                    if !item.source.isEmpty { Text(item.source).font(.system(size: 11)).foregroundStyle(ReaderNativeCardStyle.newsSummary.opacity(0.65)) }
                }
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 原版渲染器产出的卡片 HTML → 按 class 分派成原生块。
@MainActor
struct ReaderNativeCardHTML: View {
    let html: String
    let onSelection: (String) -> Void
    var inlineImages: [String:String] = [:]
    var imageModel: ReaderNativeConversationModel? = nil

    var body: some View {
        let blocks = ReaderNativeCardHTMLParser.blocks(html)
        let images = inlineImages.merging(imageModel?.inlineImages(content: html, format: "html") ?? [:]) { _, native in native }
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block {
                case .fact(let answer, let detail):
                    ReaderNativePageCardFact(answer: answer, detail: detail, answerFormat: "html",
                                             detailFormat: "html", onSelection: onSelection,inlineImages:images,imageModel:imageModel)
                case .general(let html):
                    ReaderNativeRichDocument(content: html, format: "html", onSelection: onSelection,inlineImages:images,imageModel:imageModel,
                                         font: .preferredFont(forTextStyle: .body), color: ReaderNativeCardInk.text)
                case .rich(let html):
                    // .vc-card{font-size:14px;line-height:1.55}
                    ReaderNativeRichDocument(content: html, format: "html", onSelection: onSelection,
                                             inlineImages:images,imageModel:imageModel,
                                             font: .preferredFont(forTextStyle: .body), color: ReaderNativeCardInk.text)
                case .weather(let temperature, let condition, let place, let tip):
                    ReaderNativePageCardWeather(temperature: temperature, condition: condition, place: place, tip: tip)
                case .news(let items):
                    ReaderNativePageCardNews(items: items)
                case .dictionary(let entry):
                    ReaderNativeCardDictionary(entry: entry)
                case .video(let video):
                    if let imageModel { ReaderNativeVideoButton(video: video, model: imageModel) }
                }
            }
        }
    }
}

struct ReaderNativeCardDictionaryEntry {
    struct Mora { let text: String; let high: Bool; let drop: Bool }
    struct Example { let source: String; let gloss: String }
    var word = ""
    var morae: [Mora] = []
    var pitchType = ""
    var notes: [String] = []
    var definition = ""
    var examples: [Example] = []
}

private enum ReaderNativeCardHTMLBlock {
    /// 原版 .vc-if-w / .vc-if-n：按字段取出，交给与侧栏同一套数值的原生视图画。
    /// ⚠ 不能走通用富文本：UITextView 排「26px 一行 + 几行小字」时中间空出一大截、
    ///   后两行被挤出可见区（2026-09-26 用户截图：天气卡拖到页上排版就变了）。
    case weather(temperature: String, condition: String, place: String, tip: String)
    case news([(title: String, summary: String, source: String)])
    case fact(answer: String, detail: String)
    case general(String)
    case rich(String)
    case dictionary(ReaderNativeCardDictionaryEntry)
    case video(ReaderNativeVideo)
}

@MainActor
private enum ReaderNativeCardHTMLParser {
    private final class Cached: NSObject {
        let blocks: [ReaderNativeCardHTMLBlock]
        init(_ blocks: [ReaderNativeCardHTMLBlock]) { self.blocks = blocks }
    }
    private static let cache: NSCache<NSString, Cached> = {
        let cache = NSCache<NSString, Cached>()
        cache.countLimit = 64
        return cache
    }()

    static func blocks(_ html: String) -> [ReaderNativeCardHTMLBlock] {
        if let cached = cache.object(forKey: html as NSString) { return cached.blocks }
        guard let body = try? SwiftSoup.parseBodyFragment(html).body() else { return [.rich(html)] }
        var result: [ReaderNativeCardHTMLBlock] = []
        var pending = ""
        func flush() {
            if !pending.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { result.append(.rich(pending)) }
            pending = ""
        }
        for node in body.getChildNodes() {
            guard let element = node as? Element else {
                pending += (try? node.outerHtml()) ?? ""
                continue
            }
            let videoButtons = (try? element.select(".vc-vg-play").array()) ?? []
            if !videoButtons.isEmpty {
                flush()
                for button in videoButtons {
                    if let video = video(button) {
                        result.append(.video(video))
                    } else {
                        result.append(.rich((try? button.parent()?.outerHtml()) ?? "视频播放身份缺失"))
                    }
                }
            } else if element.hasClass("vc-dict-sec") || element.hasClass("rc-note-dict") {
                flush(); result.append(.dictionary(dictionary(element)))
            } else if element.hasClass("vc-if-w") {
                flush()
                func field(_ name: String) -> String { text(try? element.select("." + name).first()) }
                result.append(.weather(temperature: field("vc-if-wt"), condition: field("vc-if-wc"),
                                       place: field("vc-if-ws"), tip: field("vc-if-tip")))
            } else if element.hasClass("vc-if-n") {
                flush()
                let items = ((try? element.select(".vc-if-ni").array()) ?? []).map { item -> (title: String, summary: String, source: String) in
                    let source = text(try? item.select(".vc-if-src").first())
                    var summary = text(try? item.select(".vc-if-ns").first())
                    if !source.isEmpty, summary.hasSuffix(source) { summary = String(summary.dropLast(source.count)).trimmingCharacters(in: .whitespaces) }
                    return (text(try? item.select(".vc-if-nt").first()), summary, source.replacingOccurrences(of: "— ", with: ""))
                }
                result.append(.news(items))
            } else if element.hasClass("vc-if-f") {
                flush()
                let answer = (try? element.select(".vc-if-fa").first()?.html()) ?? nil
                let detail = (try? element.select(".vc-if-fd").first()?.html()) ?? nil
                result.append(.fact(answer: answer ?? "", detail: detail ?? ""))
            } else if element.hasClass("vc-if-g") {
                flush(); result.append(.general((try? element.html()) ?? ""))
            } else {
                pending += (try? element.outerHtml()) ?? ""
            }
        }
        flush()
        cache.setObject(Cached(result), forKey: html as NSString)
        return result
    }

    private static func text(_ element: Element?) -> String {
        guard let element else { return "" }
        return ((try? element.text()) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func video(_ button: Element) -> ReaderNativeVideo? {
        var id = (try? button.attr("data-video-id")) ?? ""
        var source = (try? button.attr("data-video-src")) ?? ""
        var title = (try? button.attr("data-video-title")) ?? ""
        var cell = button.parent()
        while let item = cell, !item.hasClass("vc-ig-cell") { cell = item.parent() }
        if title.isEmpty { title = text(try? cell?.select(".vc-ig-t").first()) }
        if id.isEmpty, let image = try? cell?.select(".vc-ig-img").first() {
            let original = (try? image.attr("data-source-url")) ?? ""
            let raw = original.isEmpty ? ((try? image.attr("src")) ?? "") : original
            var parts = URLComponents(string: raw)
            if parts?.path == "/pdf/api/img-proxy", let nested = parts?.queryItems?.first(where: { $0.name == "url" })?.value { parts = URLComponents(string: nested) }
            let path = parts?.path.split(separator: "/").map(String.init) ?? []
            if ["i.ytimg.com", "img.youtube.com"].contains(parts?.host ?? ""), path.count >= 2, path[0] == "vi" {
                id = path[1]; source = "yt"
            }
        }
        var value: [String:Any] = ["id":id,"title":title.isEmpty ? "视频" : title]
        if !source.isEmpty { value["src"] = source }
        return ReaderNativeVideo(value)
    }

    /// `.rc-note-dict`：词头（词 + 音调）、若干说明行、释义、例句。
    private static func dictionary(_ section: Element) -> ReaderNativeCardDictionaryEntry {
        var entry = ReaderNativeCardDictionaryEntry()
        for child in section.children() {
            if child.hasClass("rnd-head") {
                entry.word = text(try? child.select(".rnd-word").first())
                if let pitch = try? child.select(".wp-pitch").first() {
                    for mora in pitch.children() where mora.hasClass("pm") {
                        entry.morae.append(.init(text: text(mora), high: mora.hasClass("hi"), drop: mora.hasClass("drop")))
                    }
                    entry.pitchType = text(try? pitch.select(".pm-type").first())
                }
            } else if child.hasClass("rnd-def") {
                entry.definition = text(child)
            } else if child.hasClass("rnd-ex") {
                entry.examples.append(.init(source: text(try? child.select(".rnd-ex-ja").first()),
                                            gloss: text(try? child.select(".rnd-ex-zh").first())))
            } else if !child.hasClass("rnd-speak") {
                let line = text(child)
                if !line.isEmpty { entry.notes.append(line) }
            }
        }
        return entry
    }
}

/// `.rc-note-dict`：上边一条虚线，内边距 8/10，13px。
@MainActor
private struct ReaderNativeCardDictionary: View {
    let entry: ReaderNativeCardDictionaryEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .bottom, spacing: 8) {
                if !entry.word.isEmpty {
                    // .rnd-word{font-size:16px;font-weight:600;color:#fff}
                    Text(entry.word).font(.title3.weight(.semibold)).foregroundStyle(.white)
                }
                if !entry.morae.isEmpty { pitch }
            }
            .padding(.bottom, 4)
            ForEach(Array(entry.notes.enumerated()), id: \.offset) { _, line in
                // 「当前形 / 原形」那行：原版内联样式 font-size:11px;opacity:.75
                Text(line).font(.subheadline).opacity(0.85).padding(.vertical, 3)
            }
            if !entry.definition.isEmpty {
                // .rnd-def{margin:2px 0 6px;white-space:pre-wrap}
                Text(entry.definition).font(.body).lineSpacing(4).padding(.top, 4).padding(.bottom, 8)
            }
            ForEach(Array(entry.examples.enumerated()), id: \.offset) { _, example in
                // .rnd-ex{margin-top:4px;padding-top:4px;border-top:1px solid rgba(160,160,180,.15)}
                VStack(alignment: .leading, spacing: 1) {
                    if !example.source.isEmpty { Text(example.source).font(.body) }
                    if !example.gloss.isEmpty {
                        Text(example.gloss).font(.subheadline).foregroundStyle(ReaderNativeCardInk.exampleGloss)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 4)
                .overlay(alignment: .top) { Rectangle().fill(ReaderNativeCardInk.dash.opacity(0.15)).frame(height: 1) }
                .padding(.top, 4)
            }
        }
        .foregroundStyle(Color(uiColor: ReaderNativeCardInk.text))
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10).padding(.vertical, 8)
        .overlay(alignment: .top) {
            Line().stroke(ReaderNativeCardInk.dash.opacity(0.35), style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
                .frame(height: 1)
        }
        .padding(.top, 6)
    }

    /// `.wp-pitch`：每个假名一格，高音格顶上一条 2px 青线，降调处一道红竖线，
    /// 末尾一枚声调型小标签。
    private var pitch: some View {
        HStack(alignment: .center, spacing: 0) {
            HStack(alignment: .bottom, spacing: 0) {
            ForEach(Array(entry.morae.enumerated()), id: \.offset) { _, mora in
                Text(mora.text).font(.system(size: 14))
                    .foregroundStyle(.white)
                    .padding(.top, 3).padding(.horizontal, 1)
                    .overlay(alignment: .top) {
                        Rectangle().fill(mora.high ? ReaderNativeCardInk.pitch : .clear).frame(height: 2)
                    }
                    .overlay(alignment: .topTrailing) {
                        if mora.drop { Rectangle().fill(ReaderNativeCardInk.drop).frame(width: 2, height: 9).offset(x: 1) }
                    }
            }
            }
            if !entry.pitchType.isEmpty {
                Text(entry.pitchType).font(.system(size: 10)).foregroundStyle(ReaderNativeCardInk.dim)
                    .padding(.horizontal, 4)
                    .overlay(RoundedRectangle(cornerRadius: 4)
                        .stroke(Color(red: 0x2a / 255, green: 0x34 / 255, blue: 0x50 / 255), lineWidth: 1))
                    .padding(.leading, 6)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private struct Line: Shape {
        func path(in rect: CGRect) -> Path {
            Path { $0.move(to: CGPoint(x: 0, y: rect.midY)); $0.addLine(to: CGPoint(x: rect.maxX, y: rect.midY)) }
        }
    }
}

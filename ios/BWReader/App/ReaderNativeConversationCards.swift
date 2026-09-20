import SwiftUI

@MainActor
struct ReaderNativeConversationMessageView: View {
    let message: ReaderNativeConversationMessage
    @ObservedObject var model: ReaderNativeConversationModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                Text(message.role == "user" ? "你" : message.role == "assistant" ? "助手" : "提示")
                    .fontWeight(.medium)
                if message.streaming {
                    Text(message.role == "user" ? "正在识别…" : "正在回复…")
                }
                Spacer(minLength: 0)
            }
            .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
            if !message.text.isEmpty {
                ReaderNativeConversationMarkdown(text: message.text)
            } else if message.streaming {
                ProgressView().controlSize(.small)
            }
            if !message.tools.isEmpty {
                ReaderNativeConversationTools(parts: message.tools, model: model)
            }
            if !message.artifacts.isEmpty {
                ReaderNativeConversationArtifacts(parts: message.artifacts, model: model)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(message.role == "user" ? 12 : 0)
        .background(message.role == "user" ? ReaderNativeTheme.accent.opacity(0.07) : Color.clear,
                    in: RoundedRectangle(cornerRadius: 14))
    }
}

@MainActor
private struct ReaderNativeConversationMarkdown: View {
    let text: String

    var body: some View {
        Text((try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
             ?? AttributedString(text))
            .font(.subheadline).lineSpacing(4)
            .foregroundStyle(ReaderNativeTheme.ink)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)
    }
}

@MainActor
private struct ReaderNativeConversationTools: View {
    let parts: [ReaderNativeConversationPart]
    @ObservedObject var model: ReaderNativeConversationModel
    @State private var expanded = false

    private var successful: Int { parts.reduce(0) { $0 + ($1.count("successCount") ?? ($1.isComplete && ($1.count("stepCount") ?? 1) == 1 ? 1 : 0)) } }
    private var failed: Int { parts.reduce(0) { $0 + ($1.count("failureCount") ?? ($1.isFailed && ($1.count("stepCount") ?? 1) == 1 ? 1 : 0)) } }
    private var running: Int { parts.reduce(0) { $0 + ($1.count("runningCount") ?? ($1.isRunning && ($1.count("stepCount") ?? 1) == 1 ? 1 : 0)) } }

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(parts) { part in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(alignment: .top, spacing: 7) {
                            Image(systemName: icon(part))
                                .foregroundStyle(part.isFailed ? Color.red : ReaderNativeTheme.accent)
                            Text(part.title.isEmpty ? "工具调用" : part.title).fontWeight(.medium)
                            Spacer(minLength: 0)
                            if let steps = part.count("stepCount"), steps > 1 {
                                Text("\(steps) 步").foregroundStyle(ReaderNativeTheme.muted)
                            }
                        }
                        if !part.text.isEmpty {
                            Text(part.text).foregroundStyle(ReaderNativeTheme.muted)
                                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        }
                        ReaderNativeConversationAction(part: part, model: model)
                    }
                }
            }
            .font(.caption).padding(.top, 10)
        } label: {
            HStack(spacing: 8) {
                if running > 0 { ProgressView().controlSize(.mini) }
                Text("处理过程")
                Spacer(minLength: 0)
                if successful > 0 {
                    Label("\(successful)", systemImage: "checkmark.circle")
                        .foregroundStyle(ReaderNativeTheme.accent)
                }
                if failed > 0 {
                    Label("\(failed)", systemImage: "exclamationmark.circle").foregroundStyle(.red)
                }
                if running > 0 { Text("\(running) 进行中") }
            }
            .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("处理过程，成功 \(successful) 项，失败 \(failed) 项，进行中 \(running) 项")
        }
        .padding(12).background(ReaderNativeTheme.card, in: RoundedRectangle(cornerRadius: 12))
    }

    private func icon(_ part: ReaderNativeConversationPart) -> String {
        if part.isFailed { return "exclamationmark.circle" }
        if part.isComplete { return "checkmark.circle" }
        if part.isRunning { return "ellipsis.circle" }
        return "circle.dashed"
    }
}

@MainActor
private struct ReaderNativeConversationArtifacts: View {
    let parts: [ReaderNativeConversationPart]
    @ObservedObject var model: ReaderNativeConversationModel
    @State private var visibleID: String?

    private var position: Int { (parts.firstIndex { $0.id == visibleID } ?? 0) + 1 }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if parts.count > 1 {
                HStack {
                    Text("生成物")
                    Spacer()
                    Text("\(position) / \(parts.count)").monospacedDigit()
                    Image(systemName: "arrow.left.and.right")
                }
                .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
                ScrollView(.horizontal) {
                    HStack(alignment: .top, spacing: 12) {
                        ForEach(parts) { part in
                            ReaderNativeConversationArtifactCard(part: part, model: model)
                                .containerRelativeFrame(.horizontal)
                                .id(part.id)
                        }
                    }
                    .scrollTargetLayout()
                }
                .scrollTargetBehavior(.viewAligned)
                .scrollPosition(id: $visibleID)
                .scrollIndicators(.hidden)
            } else if let part = parts.first {
                ReaderNativeConversationArtifactCard(part: part, model: model)
            }
        }
    }
}

@MainActor
private struct ReaderNativeConversationArtifactCard: View {
    let part: ReaderNativeConversationPart
    @ObservedObject var model: ReaderNativeConversationModel
    @State private var showsAnswer = false

    private var isAnki: Bool { part.kind == "anki" || part.kind == "flashcard" }
    private var heading: String {
        if !part.title.isEmpty { return part.title }
        if isAnki { return "学习卡" }
        return part.kind == "weather" ? "天气" : part.kind == "news" ? "新闻" : "知识卡"
    }
    private var icon: String {
        if isAnki { return "rectangle.on.rectangle" }
        return part.kind == "weather" ? "cloud.sun" : part.kind == "news" ? "newspaper" : "doc.text"
    }
    private var rawFront: String { firstText(part.string("front"), part.string("frontText"), part.text) }
    private var isCloze: Bool { part.string("type") == "cloze" || rawFront.contains("{{c1::") }
    private var front: String {
        isCloze ? rawFront.replacingOccurrences(of: "(?s)\\{\\{c\\d+::.*?\\}\\}", with: "[…]", options: .regularExpression) : rawFront
    }
    private var back: String {
        let text = firstText(part.string("back"), part.string("backText"), isCloze ? rawFront : "")
        return isCloze ? text.replacingOccurrences(of: "(?s)\\{\\{c\\d+::(.*?)(?:::[^}]*?)?\\}\\}", with: "$1", options: .regularExpression) : text
    }
    private var isDraft: Bool { part.data["draft"] as? Bool == true || part.status == "draft" }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: icon)
                    .foregroundStyle(ReaderNativeTheme.accent)
                Text(heading).font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity, alignment: .leading)
                if isDraft { Text("待确认").font(.caption2).foregroundStyle(ReaderNativeTheme.muted) }
            }
            if isAnki {
                VStack(alignment: .leading, spacing: 8) {
                    Text(showsAnswer ? "背面" : "正面").font(.caption).foregroundStyle(ReaderNativeTheme.muted)
                    ReaderNativeConversationMarkdown(text: readable(showsAnswer ? back : front))
                    if !back.isEmpty {
                        Button(showsAnswer ? "返回正面" : "显示答案") { showsAnswer.toggle() }
                            .font(.caption.weight(.medium)).buttonStyle(.bordered)
                    }
                }
            } else if part.kind == "fact" {
                ReaderNativeConversationMarkdown(text: readable(firstText(part.string("answer"), part.text)))
                let detail = part.string("detail")
                if !detail.isEmpty, detail != part.string("answer") {
                    ReaderNativeConversationMarkdown(text: readable(detail))
                }
            } else if part.kind == "general" || part.kind == "knowledge" {
                ReaderNativeConversationMarkdown(text: readable(firstText(part.string("text"), part.text)))
            } else if part.kind == "weather" {
                weatherContent
            } else if part.kind == "news" {
                newsContent
            } else {
                if !part.text.isEmpty {
                    ReaderNativeConversationMarkdown(text: readable(part.text))
                }
                Text("打开原件可查看完整内容和全部操作。")
                    .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
            }
            if let sources = part.data["sources"] as? [Any], !sources.isEmpty {
                Text("含 \(sources.count) 项来源，详见原件")
                    .font(.caption2).foregroundStyle(ReaderNativeTheme.muted)
            }
            ReaderNativeConversationAction(part: part, model: model)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(ReaderNativeTheme.card, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(ReaderNativeTheme.accent.opacity(0.12), lineWidth: 1))
        .onChange(of: part.id) { _, _ in showsAnswer = false }
    }

    private func firstText(_ choices: String...) -> String { choices.first { !$0.isEmpty } ?? "" }

    private func field(_ key: String) -> String {
        if let text = part.data[key] as? String { return text }
        return (part.data[key] as? NSNumber)?.stringValue ?? ""
    }

    private var weatherContent: some View {
        VStack(alignment: .leading, spacing: 8) {
            let place = [field("loc"), field("date")].filter { !$0.isEmpty }.joined(separator: " · ")
            if !place.isEmpty { Text(place).font(.caption).foregroundStyle(ReaderNativeTheme.muted) }
            let low = field("lo"), high = field("hi")
            if !low.isEmpty && !high.isEmpty {
                Text("\(low)–\(high)°C").font(.title2.weight(.medium)).monospacedDigit()
            } else if !low.isEmpty || !high.isEmpty {
                Text(!low.isEmpty ? "最低 \(low)°C" : "最高 \(high)°C").font(.title3.weight(.medium))
            }
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                if !field("cond").isEmpty { Text(field("cond")).font(.subheadline) }
                let precipitation = field("precip")
                if !precipitation.isEmpty {
                    Text("降水 \(precipitation)\(precipitation.hasSuffix("%") ? "" : "%")")
                        .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
                }
            }
            if !field("tip").isEmpty { ReaderNativeConversationMarkdown(text: readable(field("tip"))) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var newsContent: some View {
        let items = part.data["items"] as? [[String: Any]] ?? []
        return VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                if index > 0 { Divider() }
                VStack(alignment: .leading, spacing: 5) {
                    if let title = item["t"] as? String, !title.isEmpty {
                        Text(readable(title)).font(.subheadline.weight(.medium)).textSelection(.enabled)
                    }
                    if let summary = item["s"] as? String, !summary.isEmpty {
                        ReaderNativeConversationMarkdown(text: readable(summary))
                    }
                    if let source = item["src"] as? String, !source.isEmpty {
                        Text(source).font(.caption2).foregroundStyle(ReaderNativeTheme.muted)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if items.isEmpty {
                Text(part.text.isEmpty ? "暂无新闻条目，请打开原件查看。" : readable(part.text))
                    .font(.subheadline).foregroundStyle(ReaderNativeTheme.muted)
            }
        }
    }

    private func readable(_ value: String) -> String {
        // Native cards provide a readable preview. HTML, media and the original
        // card's mutations remain in the existing renderer behind its action ID.
        value.replacingOccurrences(of: "(?i)<br\\s*/?>|</p>|</div>", with: "\n", options: .regularExpression)
            .replacingOccurrences(of: "(?i)</?(?:div|p|span|br|b|strong|i|em|u|small|font|ruby|rt|rp|h[1-6]|ul|ol|li|table|tr|td|th|a|img)(?:\\s[^>]*|\\s*)>", with: "", options: .regularExpression)
            .replacingOccurrences(of: "&nbsp;", with: " ")
            .replacingOccurrences(of: "&lt;", with: "<")
            .replacingOccurrences(of: "&gt;", with: ">")
            .replacingOccurrences(of: "&quot;", with: "\"")
            .replacingOccurrences(of: "&amp;", with: "&")
    }
}

@MainActor
private struct ReaderNativeConversationAction: View {
    let part: ReaderNativeConversationPart
    @ObservedObject var model: ReaderNativeConversationModel

    private var command: String { model.supports("openArtifact") ? "openArtifact" : "action" }

    var body: some View {
        if let actionId = part.actionId, model.supports(command) {
            Button {
                Task { await model.perform(command, parameters: ["actionId": actionId]) }
            } label: {
                Label(part.isTool ? "查看完整流程" : "在完整界面中操作",
                      systemImage: "arrow.up.forward.square")
                    .font(.caption.weight(.medium))
            }
            .buttonStyle(.bordered)
            .disabled(!model.ready || model.isPerforming(command))
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text("原件定位暂不可用；完整操作仍保留在阅读界面中。")
                    .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
                if model.supports("showLegacy") {
                    Button("打开完整界面") { Task { await model.perform("showLegacy") } }
                        .font(.caption.weight(.medium)).buttonStyle(.bordered)
                        .disabled(model.isPerforming("showLegacy"))
                }
            }
        }
    }
}

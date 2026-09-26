import SwiftUI

@MainActor
struct ReaderNativeConversationMessageView: View {
    let message: ReaderNativeConversationMessage
    @ObservedObject var model: ReaderNativeConversationModel
    /// 同一角色连着说时只在第一条标「助手」/「你」（用户 2026-09-26）。
    var showsRole = true

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if showsRole || message.streaming { HStack(spacing: 7) {
                Text(message.role == "user" ? "你" : message.role == "assistant" ? "助手" : "提示")
                    .fontWeight(.medium)
                if message.streaming {
                    Text(message.role == "user" ? "正在识别…" : "正在回复…")
                }
                Spacer(minLength: 0)
            }
            .font(.caption).foregroundStyle(ReaderNativeTheme.muted) }
            // 有工具卡时，标题（常是原始工具名）与「成功/失败」计数都已在卡头里、且带颜色；
            // 再在卡外画一遍就是用户说的"跑到外面、没有颜色"那种渲染（2026-09-26）。
            if message.tools.isEmpty {
                if !message.title.isEmpty {
                    Text(message.title).font(.subheadline.weight(.medium))
                }
                if !message.progressSummary.isEmpty {
                    Text(message.progressSummary).font(.caption).foregroundStyle(.secondary)
                }
            }
            if !message.statusText.isEmpty {
                Text(message.statusText).font(.caption).foregroundStyle(.secondary)
            }
            if !message.text.isEmpty {
                ReaderNativeConversationMarkdown(text: message.text, model: model)
            } else if message.streaming {
                ProgressView().controlSize(.small)
            }
            if !message.tools.isEmpty {
                ReaderNativeConversationTools(parts: message.tools, model: model)
            }
            if !message.artifacts.isEmpty {
                ReaderNativeConversationArtifacts(parts: message.artifacts, model: model)
            }
            if !message.reviewSelections.isEmpty {
                DisclosureGroup("选用回答或段落") {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(message.reviewSelections) { item in
                            Button {
                                Task { await model.performReview("selectAnswer", values: ["selectionId": item.id, "enabled": !item.selected]) }
                            } label: {
                                HStack(alignment: .top, spacing: 8) {
                                    Image(systemName: item.selected ? "checkmark.circle.fill" : "circle")
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(item.label).font(.caption.weight(.medium))
                                        Text(item.text).font(.caption).foregroundStyle(.secondary).lineLimit(3)
                                    }
                                }.frame(maxWidth: .infinity, alignment: .leading)
                            }.buttonStyle(.plain).disabled(model.isPerforming("reviewAction"))
                        }
                    }.padding(.top, 6)
                }.font(.caption).tint(ReaderNativeTheme.accent)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, message.role == "user" ? 12 : 0)
        .padding(.vertical, message.role == "user" ? 8 : 0)
        .background(message.role == "user" ? ReaderNativeTheme.accent.opacity(0.07) : Color.clear,
                    in: RoundedRectangle(cornerRadius: 14))
    }
}

@MainActor
private struct ReaderNativeConversationMarkdown: View {
    let text: String
    var model: ReaderNativeConversationModel? = nil

    var body: some View {
        ReaderNativeRichDocument(content: text, imageModel: model)
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
struct ReaderNativeConversationArtifacts: View {
    let parts: [ReaderNativeConversationPart]
    @ObservedObject var model: ReaderNativeConversationModel
    /// 页卡里用：只要内容，不要侧栏生成物那层外壳（图标标题行 / 带入对话 / 底板）。
    /// 页卡自己就是那张卡，再套一层就是"卡里套卡"（2026-09-23 用户截图）。
    var bare = false
    @State private var visibleID: String?

    private var position: Int { (parts.firstIndex { $0.id == visibleID } ?? 0) + 1 }
    private var preferredID: String? { parts.first { $0.data["activeInGroup"] as? Bool == true }?.id }

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
                            ReaderNativeConversationArtifactCard(part: part, model: model, bare: bare)
                                .containerRelativeFrame(.horizontal)
                                .id(part.id)
                        }
                    }
                    .scrollTargetLayout()
                }
                .scrollTargetBehavior(.viewAligned)
                .scrollPosition(id: $visibleID)
                .scrollIndicators(.hidden)
                .onChange(of: preferredID, initial: true) { _, next in
                    if let next { visibleID = next }
                }
            } else if let part = parts.first {
                ReaderNativeConversationArtifactCard(part: part, model: model, bare: bare)
            }
        }
    }
}

/// 原网页卡片的视觉（rc-voicecall `.vc-card` / `.vc-if-*`、rc-flashcard `.fc-card`），数值逐项照搬。
/// 2026-09-26 用户：「颜色、质感、字号和整体排版不如原网页设计 —— 先复现原设计，玻璃适度」。
/// 原版无论深浅色都是深色卡：卡内一律按深色方案取色，次要文字自然落到原版的暗灰。
enum ReaderNativeCardStyle {
    static let surface = Color(red: 30/255, green: 30/255, blue: 34/255).opacity(0.9)     // --vc-cardbg
    static let border = Color.white.opacity(0.14)                                           // 0.5px
    static let text = Color(red: 0xf2/255, green: 0xf2/255, blue: 0xf7/255)                // #f2f2f7
    static let purple = Color(red: 0xbf/255, green: 0x5a/255, blue: 0xf2/255)              // --rc-purple
    static let muted = Color(red: 235/255, green: 235/255, blue: 245/255).opacity(0.62)    // --rc-text-muted
    static let tip = Color(red: 0xb8/255, green: 0xc6/255, blue: 0xe2/255)                 // #b8c6e2
    static let newsTitle = Color(red: 0xe8/255, green: 0xee/255, blue: 0xfb/255)           // #e8eefb
    static let newsSummary = Color(red: 0x9f/255, green: 0xb0/255, blue: 0xcf/255)         // #9fb0cf
    static let hairline = Color.white.opacity(0.10)
    /// 学习卡 .fc-card：145° 深蓝渐变 + 淡青描边。
    static let flashGradient = LinearGradient(
        colors: [Color(red: 22/255, green: 32/255, blue: 58/255).opacity(0.82),
                 Color(red: 13/255, green: 19/255, blue: 34/255).opacity(0.88)],
        startPoint: .topLeading, endPoint: .bottomTrailing)
    static let flashBorder = Color(red: 125/255, green: 211/255, blue: 252/255).opacity(0.16)
}

@MainActor
private struct ReaderNativeConversationArtifactCard: View {
    let part: ReaderNativeConversationPart
    @ObservedObject var model: ReaderNativeConversationModel
    var bare = false
    @State private var editing = false

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
    private var controls: [ReaderNativeControl] {
        (part.data["controls"] as? [[String: Any]] ?? []).compactMap(ReaderNativeControl.init)
    }
    private var fields: [[String: String]] { part.data["fields"] as? [[String: String]] ?? [] }
    private var isDraft: Bool { part.string("state") == "draft" }
    /// 拖动预览里的几行正文：天气给温度与天况，其余取正文开头。
    private var dragSummary: String {
        if part.kind == "weather" {
            let low = ReaderWeatherDegrees.bare(field("lo")), high = ReaderWeatherDegrees.bare(field("hi"))
            return [low.isEmpty || high.isEmpty ? "" : "\(low)–\(high)°C", field("cond")].filter { !$0.isEmpty }.joined(separator: "  ")
        }
        let raw = firstText(part.string("answer"), firstText(part.string("text"), part.text))
        return String(readable(raw).trimmingCharacters(in: .whitespacesAndNewlines).prefix(120))
    }
    private var dragPayload: ReaderNativeCardTransfer {
        ReaderNativeCardTransfer(scope: model.scope, actionID: part.string("dragId"))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !bare {
            // 卡头 = 原版 .vc-if-hd：12px 紫色半粗，小图标跟随。
            HStack(alignment: .center, spacing: 6) {
                Image(systemName: icon).font(.system(size: 12, weight: .semibold))
                Text(heading).font(.system(size: 12, weight: .semibold))
                    .frame(maxWidth: .infinity, alignment: .leading)
                if isDraft { Text("待确认").font(.system(size: 11)).foregroundStyle(ReaderNativeCardStyle.muted) }
                if !part.string("dragId").isEmpty {
                    Image(systemName: "hand.draw").font(.system(size: 12)).foregroundStyle(ReaderNativeCardStyle.muted)
                        .accessibilityHidden(true)
                }
            }
            .foregroundStyle(ReaderNativeCardStyle.purple)
            .frame(minHeight: 30)
            // 整条标题都能按住拖（不只是图标和字本身）。
            .contentShape(Rectangle())
            // 拖出去时给一个像"卡片副本"的影子 —— 网页那版拖的就是卡的克隆
            // （rc-voicecall `_dragToDock` 的 ghost）。默认快照拖的是这一行标题，
            // 看着不像在搬一张卡。
            .draggable(dragPayload) {
                // 拖动时手里拿的是一张**卡片**（原版 _dragToDock 拖的是卡的克隆），不是一条标签。
                // 2026-09-26 用户：「拖动时首先显示的不是卡片的样式而是一个长条」。
                ReaderNativeCardDragPreview(heading: heading, icon: icon, isAnki: isAnki, summary: dragSummary)
            }
            .accessibilityHint("长按卡片标题，拖到书页正文放置")
            }
            // 页卡上「带入对话」是**长按卡片**（原版 LP_MS 600），不要再摆一个按钮。
            if !bare, !part.string("pinId").isEmpty {
                Button {
                    Task { await model.perform("liveAction", parameters: ["actionId": part.string("pinId")]) }
                } label: {
                    Label(part.data["pinned"] as? Bool == true ? "已带入对话" : "带入对话",
                          systemImage: part.data["pinned"] as? Bool == true ? "checkmark.circle.fill" : "plus.bubble")
                }
                .font(.caption).buttonStyle(.borderless)
                .disabled(model.isPerforming("liveAction"))
            }
            if isAnki {
                if part.data["live"] as? Bool == true {
                    if isDraft && !fields.isEmpty {
                        ForEach(fields.indices, id: \.self) { index in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(fields[index]["key"] == "front" ? "正面" : fields[index]["key"] == "cloze" ? "填空" : "背面")
                                    .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
                                richContent(fields[index]["value"] ?? "", size: 15)
                            }
                        }
                        Button("修改内容", systemImage: "pencil") { editing = true }
                            .font(.caption).buttonStyle(.bordered)
                            .disabled(part.data["editable"] as? Bool != true)
                    } else {
                        let faces = part.data["faces"] as? [[String: String]] ?? []
                        ForEach(faces.indices, id: \.self) { index in
                            if index > 0 { Divider() }
                            richContent(faces[index]["content"] ?? "", format: faces[index]["format"], size: 15)
                        }
                        if !part.string("notice").isEmpty {
                            Text(part.string("notice")).font(.caption).foregroundStyle(ReaderNativeTheme.muted)
                        }
                    }
                    if !controls.isEmpty {
                        ViewThatFits(in: .horizontal) {
                            HStack(spacing: 6) { liveButtons }
                            VStack(alignment: .leading, spacing: 6) { liveButtons }
                        }
                    }
                } else {
                    // ⚠ 带上原因。只写"正在同步…"的话，卡在这儿就是一条死路 ——
                    //   没人说得出是节点没了、卡组没挂上，还是状态取不到。
                    Text(part.string("liveReason").isEmpty
                         ? "学习卡正在同步…"
                         : "学习卡暂时读不到（\(part.string("liveReason"))）")
                        .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
                        .textSelection(.enabled)
                }
            } else if part.kind == "operations" {
                operationContent
            } else if part.kind == "fact" {
                // 原版 .vc-if-fa 15px 半粗 / .vc-if-fd 12px #b8c6e2。
                richContent(firstText(part.string("answer"), part.text), size: 15, weight: .semibold)
                let detail = part.string("detail")
                if !detail.isEmpty, detail != part.string("answer") {
                    richContent(detail, size: 12, color: UIColor(red: 0xb8/255, green: 0xc6/255, blue: 0xe2/255, alpha: 1))
                }
            } else if part.kind == "general" || part.kind == "knowledge" {
                richContent(firstText(part.string("text"), part.text), format: part.string("format").isEmpty ? nil : part.string("format"), size: 13)
            } else if part.kind == "weather" {
                weatherContent
            } else if part.kind == "news" {
                newsContent
            } else if part.kind == "images" || part.kind == "videos" {
                let items = (part.data["items"] as? [[String: Any]] ?? []).compactMap(ReaderNativeImageItem.init)
                ForEach(items) { item in ReaderNativeImageCard(item: item, model: model) }
                if items.isEmpty { Text("此卡片的图片已移除。").font(.caption).foregroundStyle(.secondary) }
            } else {
                if !part.text.isEmpty {
                    richContent(part.text,format:part.string("format").isEmpty ? nil : part.string("format"))
                }
                Text("完整原件可在内容资料中查看。")
                    .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
            }
            if let sources = part.data["sources"] as? [Any], !sources.isEmpty {
                Text("含 \(sources.count) 项来源，详见内容资料")
                    .font(.caption2).foregroundStyle(ReaderNativeTheme.muted)
            }
            // 页卡上不放「内容资料」：原版页卡没有这个按钮（侧栏生成物里才有）。
            if !bare { ReaderNativeConversationAction(part: part, model: model) }
        }
        .font(.system(size: 14)).lineSpacing(14 * 0.55 * 0.5)
        .foregroundStyle(ReaderNativeCardStyle.text)
        .padding(.horizontal, bare ? 0 : 13).padding(.top, bare ? 0 : 10).padding(.bottom, bare ? 0 : 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background {
            if !bare {
                // 学习卡 = .fc-card 深蓝渐变（圆角 12）；其它 = .vc-card 深色卡面（圆角 16）。
                if isAnki { RoundedRectangle(cornerRadius: 12).fill(ReaderNativeCardStyle.flashGradient) }
                else { RoundedRectangle(cornerRadius: 16).fill(ReaderNativeCardStyle.surface) }
            }
        }
        .overlay {
            if !bare {
                RoundedRectangle(cornerRadius: isAnki ? 12 : 16)
                    .stroke(isAnki ? ReaderNativeCardStyle.flashBorder : ReaderNativeCardStyle.border, lineWidth: isAnki ? 1 : 0.5)
            }
        }
        .shadow(color: .black.opacity(bare ? 0 : 0.28), radius: 14, y: 6)
        .environment(\.colorScheme, .dark)
        .sheet(isPresented: $editing) {
            ReaderNativeCardEditor(fields: fields, model: model)
        }
    }

    @ViewBuilder
    private var operationContent: some View {
        let items = part.data["items"] as? [[String:Any]] ?? []
        ForEach(items.indices,id:\.self) { index in
            let item = items[index], undone = item["undone"] as? Bool == true
            VStack(alignment:.leading,spacing:8) {
                Text(item["label"] as? String ?? "操作记录").strikethrough(undone)
                    .foregroundStyle(undone ? .secondary : .primary)
                HStack {
                    if let page = item["displayPage"] as? NSNumber {
                        Button("第 \(page.intValue) 页",systemImage:"arrow.up.right") { performOperation(item,action:"jump") }
                    }
                    Spacer()
                    Button(undone ? "重做" : "撤销",systemImage:undone ? "arrow.uturn.forward" : "arrow.uturn.backward") { performOperation(item,action:"toggle") }
                }.font(.subheadline).buttonStyle(.bordered)
                    .disabled(model.isPerforming("operationAction") || part.data["nativeOperation"] == nil)
            }.padding(.vertical,4)
        }
        if items.isEmpty { Text("这些操作对应的内容已移除。").foregroundStyle(.secondary) }
    }

    private func performOperation(_ item:[String:Any],action:String) {
        guard var value = part.data["nativeOperation"] as? [String:Any] else { return }
        value["index"] = item["index"]; value["action"] = action
        value["expectedID"] = item["id"]; value["expectedUndone"] = item["undone"]
        Task { await model.perform("operationAction",parameters:["value":value]) }
    }

    @ViewBuilder
    private var liveButtons: some View {
        ForEach(controls) { control in
            Button(role: control.destructive ? .destructive : nil) {
                Task { await model.perform("liveAction", parameters: ["actionId": control.id]) }
            } label: {
                Text(control.title).font(.caption.weight(.medium))
                    .frame(maxWidth: .infinity).padding(.vertical, 3)
            }
            .buttonStyle(.bordered)
            .disabled(control.disabled || model.isPerforming("liveAction"))
        }
    }

    private func firstText(_ choices: String...) -> String { choices.first { !$0.isEmpty } ?? "" }

    @ViewBuilder
    /// 卡内富文本。字号/字重/颜色要**传进去**：正文由 UIKit 文本视图画，SwiftUI 的 .font/.foregroundStyle 管不到它。
    /// 默认 = 原版 .vc-card 正文 14px #f2f2f7。
    private func richContent(_ text: String, format: String? = nil, size: CGFloat = 14,
                             weight: UIFont.Weight = .regular,
                             color: UIColor = UIColor(red: 0xf2/255, green: 0xf2/255, blue: 0xf7/255, alpha: 1)) -> some View {
        let resolved = format ?? (text.range(of: "<[a-z][^>]*>", options: [.regularExpression, .caseInsensitive]) != nil ? "html" : "markdown")
        ReaderNativeRichDocument(content: text, format: resolved, onSelection: { selection in
            let id = part.string("selectId")
            guard !id.isEmpty else { return }
            model.updateTextSelection(id: id, text: selection)
        }, inlineImages: part.data["inlineImages"] as? [String: String] ?? [:], imageModel: model,
           font: .systemFont(ofSize: size, weight: weight), color: color)
        if resolved == "html", text.range(of: "<(script|iframe|button|input|canvas|svg|video|audio)\\b", options: [.regularExpression, .caseInsensitive]) != nil {
            Text("内嵌媒体或交互部分尚未迁移，原件已保留。")
                .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
        }
    }

    private func field(_ key: String) -> String {
        if let text = part.data[key] as? String { return text }
        return (part.data[key] as? NSNumber)?.stringValue ?? ""
    }

    private var weatherContent: some View {
        // 原版 .vc-if-w：温度 26px 半粗在上 → 天况 14px（· 降水）→ 地点日期 12px → 细线 + 提示 12px。
        VStack(alignment: .leading, spacing: 2) {
            let low = ReaderWeatherDegrees.bare(field("lo")), high = ReaderWeatherDegrees.bare(field("hi"))
            if !low.isEmpty && !high.isEmpty {
                Text("\(low)–\(high)°C").font(.system(size: 26, weight: .semibold)).kerning(-0.5).monospacedDigit()
            } else if !low.isEmpty || !high.isEmpty {
                Text(!low.isEmpty ? "最低 \(low)°C" : "最高 \(high)°C").font(.system(size: 22, weight: .semibold))
            }
            let precipitation = field("precip")
            let condition = [field("cond"), precipitation.isEmpty ? "" : "降水 " + precipitation + (precipitation.hasSuffix("%") ? "" : "%")]
                .filter { !$0.isEmpty }.joined(separator: " · ")
            if !condition.isEmpty { Text(condition).font(.system(size: 14)) }
            let place = [field("loc"), field("date")].filter { !$0.isEmpty }.joined(separator: " ")
            if !place.isEmpty { Text(place).font(.system(size: 12)).foregroundStyle(ReaderNativeCardStyle.muted) }
            if !field("tip").isEmpty {
                Rectangle().fill(ReaderNativeCardStyle.hairline).frame(height: 0.5).padding(.top, 6)
                Text(field("tip")).font(.system(size: 12)).foregroundStyle(ReaderNativeCardStyle.tip).padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var newsContent: some View {
        let items = part.data["items"] as? [[String: Any]] ?? []
        return VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                if index > 0 { Rectangle().fill(Color.white.opacity(0.08)).frame(height: 0.5) }
                // 原版 .vc-if-ni：标题 13px 半粗 #e8eefb，摘要 12px #9fb0cf，来源淡一档。
                VStack(alignment: .leading, spacing: 1) {
                    if let title = item["t"] as? String, !title.isEmpty {
                        Text(readable(title)).font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(ReaderNativeCardStyle.newsTitle).textSelection(.enabled)
                    }
                    if let summary = item["s"] as? String, !summary.isEmpty {
                        Text(readable(summary)).font(.system(size: 12)).foregroundStyle(ReaderNativeCardStyle.newsSummary)
                    }
                    if let source = item["src"] as? String, !source.isEmpty {
                        Text(source).font(.system(size: 11)).foregroundStyle(ReaderNativeCardStyle.newsSummary.opacity(0.65))
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if items.isEmpty {
                Text(part.text.isEmpty ? "暂无新闻条目。" : readable(part.text))
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

    var body: some View {
        if part.actionId != nil, model.supports("inspectArtifact") {
            Button {
                Task { await model.inspect(part) }
            } label: {
                Label(part.isTool ? "查看调用详情" : "内容资料",
                      systemImage: "info.circle")
                    .font(.caption.weight(.medium))
            }
            .buttonStyle(.bordered)
            .disabled(!model.ready)
        }
    }
}

@MainActor
private struct ReaderNativeCardEditor: View {
    let fields: [[String: String]]
    @ObservedObject var model: ReaderNativeConversationModel
    @Environment(\.dismiss) private var dismiss
    @State private var values: [String: String] = [:]
    @State private var saving = false
    @State private var failure: String?

    var body: some View {
        NavigationStack {
            Form {
                ForEach(fields.indices, id: \.self) { index in
                    let field = fields[index]
                    let id = field["id"] ?? ""
                    Section(field["key"] == "front" ? "正面" : field["key"] == "cloze" ? "填空" : "背面") {
                        TextEditor(text: Binding(
                            get: { values[id] ?? field["value"] ?? "" },
                            set: { values[id] = $0 }
                        ))
                        .frame(minHeight: 120)
                    }
                }
                if let failure { Text(failure).foregroundStyle(.red) }
            }
            .navigationTitle("修改学习卡")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }.disabled(saving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存修改") {
                        saving = true
                        Task {
                            for field in fields {
                                guard let id = field["id"], let text = values[id], text != field["value"] else { continue }
                                guard await model.perform("liveAction", parameters: ["actionId": id, "text": text]) else {
                                    failure = model.error; saving = false; return
                                }
                            }
                            saving = false; dismiss()
                        }
                    }.disabled(saving)
                }
            }
        }
    }
}

/// 天气卡的 lo/hi 按契约是纯数值，由显示层统一补「°C」。但 AI 常顺手写成 "18°C"，
/// 于是显示成「18°C–23°C°C」（2026-09-26 实机）。所有渲染点先剥掉已有单位再补一次。
enum ReaderWeatherDegrees {
    static func bare(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespaces)
            .replacingOccurrences(of: "\\s*(°\\s*[CcＣ]?|℃|摄氏度|度)\\s*$", with: "", options: .regularExpression)
    }
}


/// 侧栏卡片拖出时手里那张卡：与卡片同一套原版视觉（深色卡面 / 学习卡深蓝渐变 + 紫色卡头）。
private struct ReaderNativeCardDragPreview: View {
    let heading: String
    let icon: String
    let isAnki: Bool
    let summary: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.system(size: 12, weight: .semibold))
                Text(heading).font(.system(size: 12, weight: .semibold)).lineLimit(1)
            }
            .foregroundStyle(ReaderNativeCardStyle.purple)
            if !summary.isEmpty {
                Text(summary).font(.system(size: 14)).foregroundStyle(ReaderNativeCardStyle.text).lineLimit(4)
            }
        }
        .padding(.horizontal, 13).padding(.top, 10).padding(.bottom, 12)
        .frame(width: 260, alignment: .leading)
        .background {
            if isAnki { RoundedRectangle(cornerRadius: 12).fill(ReaderNativeCardStyle.flashGradient) }
            else { RoundedRectangle(cornerRadius: 16).fill(ReaderNativeCardStyle.surface) }
        }
        .overlay(RoundedRectangle(cornerRadius: isAnki ? 12 : 16)
            .stroke(isAnki ? ReaderNativeCardStyle.flashBorder : ReaderNativeCardStyle.border, lineWidth: isAnki ? 1 : 0.5))
        .environment(\.colorScheme, .dark)
    }
}

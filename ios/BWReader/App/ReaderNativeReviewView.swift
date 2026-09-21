import SwiftUI

/// Presentation only. Reader's existing review owner keeps the queue, staged
/// rating, source identity, drafts and externally confirmed results.
@MainActor
struct ReaderNativeReviewView: View {
    @ObservedObject var model: ReaderNativeConversationModel
    @State private var showsImprovement = false
    @State private var confirmation: ReviewDraftConfirmation?
    @State private var deletion: [String: String]?
    @State private var panelHeight: CGFloat = 300
    @State private var heightAtDrag: CGFloat?

    private var state: [String: Any] { model.review }
    private var current: [String: Any] { state["current"] as? [String: Any] ?? [:] }
    private var expanded: Bool { state["expanded"] as? Bool != false }
    private var saving: Bool { model.isPerforming("reviewAction") || state["ratingSaving"] as? Bool == true }
    private var count: Int { (state["count"] as? NSNumber)?.intValue ?? 0 }
    private var index: Int { (state["index"] as? NSNumber)?.intValue ?? 0 }
    private var ids: [String] { state["queueIds"] as? [String] ?? [] }

    var body: some View {
        VStack(spacing: 8) {
            toolbar
            if let notice = state["notice"] as? String, !notice.isEmpty {
                Text(notice).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if state["loading"] as? Bool == true { ProgressView("读取复习卡…") }
            else if current.isEmpty {
                Text(count == 0 && state["scope"] as? String == "current"
                     ? "当前内容暂无待复习卡，可以切换到全部。" : "当前这一批已完成。")
                    .font(.subheadline).foregroundStyle(.secondary).padding(.vertical, 12)
            } else if expanded {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        if state["showingAnswer"] as? Bool != true || current["reveal_mode"] as? String != "replace" {
                            ReaderNativeRichText(content: current["front"] as? String ?? "", format: "html")
                        }
                        if state["showingAnswer"] as? Bool == true {
                            if current["reveal_mode"] as? String != "replace" { Divider() }
                            ReaderNativeRichText(content: current["back"] as? String ?? "", format: "html")
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(12)
                }
                .frame(height: panelHeight)
                .background(ReaderNativeTheme.card, in: RoundedRectangle(cornerRadius: 14))
                ratingControls
                HStack {
                    Button { select(index - 1) } label: { Image(systemName: "chevron.left") }
                        .disabled(index <= 0 || saving)
                    Spacer()
                    Text("\(index + 1) / \(count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    Spacer()
                    Button { select(index + 1) } label: { Image(systemName: "chevron.right") }
                        .disabled(index + 1 >= count || saving)
                }.buttonStyle(.borderless)
                Capsule().fill(ReaderNativeTheme.muted.opacity(0.35)).frame(width: 38, height: 4)
                    .frame(maxWidth: .infinity).frame(height: 18).contentShape(Rectangle())
                    .gesture(DragGesture(minimumDistance: 3).onChanged { value in
                        if heightAtDrag == nil { heightAtDrag = panelHeight }
                        panelHeight = min(440, max(120, (heightAtDrag ?? panelHeight) + value.translation.height))
                    }.onEnded { _ in heightAtDrag = nil })
                    .accessibilityLabel("调整复习卡高度")
                    .accessibilityAdjustableAction { direction in
                        panelHeight = min(440, max(120, panelHeight + (direction == .increment ? 30 : -30)))
                    }
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .sheet(isPresented: $showsImprovement) { improvement }
        .confirmationDialog("删除当前卡片？", isPresented: Binding(get: { deletion != nil }, set: { if !$0 { deletion = nil } }), titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                guard let value = deletion else { return }
                deletion = nil
                Task { await model.performReview("delete", values: ["cardId": value["cardId"] ?? "", "contextKey": value["contextKey"] ?? "", "kind": value["kind"] ?? "", "confirmed": true]) }
            }
            Button("取消", role: .cancel) { deletion = nil }
        } message: {
            Text(deletion?["kind"] == "anki-note" ? "同一 Anki note 生成的全部卡片都会删除。" : "只删除当前这一张卡，同批其他卡不会受影响。")
        }
        .onChange(of: current["id"] as? String) { _, _ in
            confirmation = nil; deletion = nil; showsImprovement = false
        }
    }

    private var toolbar: some View {
        HStack(spacing: 12) {
            Text("到期 \((state["dueTotal"] as? NSNumber)?.intValue ?? 0)")
                .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            Button(state["scope"] as? String == "all" ? "全部" : "当前") {
                Task { await model.performReview("scope", values: ["value": state["scope"] as? String == "all" ? "current" : "all"]) }
            }.font(.caption)
            Spacer(minLength: 0)
            Button { Task { await model.performReview("undo") } } label: { Image(systemName: "arrow.uturn.backward") }
                .disabled(state["canUndo"] as? Bool != true || saving).accessibilityLabel("撤回暂存评分")
            Menu {
                Button("打开出处", systemImage: "book") { Task { await model.performReview("source") } }
                    .disabled(current.isEmpty)
                Button("改进卡片", systemImage: "square.and.pencil") { showsImprovement = true }
                    .disabled(current.isEmpty)
                Button("重新读取", systemImage: "arrow.clockwise") { Task { await model.performReview("reload") } }
                if let kind = state["deleteKind"] as? String, !kind.isEmpty {
                    Button("删除当前卡片", systemImage: "trash", role: .destructive) {
                        deletion = ["cardId": current["id"] as? String ?? "", "contextKey": state["contextKey"] as? String ?? "", "kind": kind]
                    }
                }
                Button("结束复习", systemImage: "xmark") { Task { await model.perform("openReview") } }
            } label: { Image(systemName: "ellipsis") }
            Button { Task { await model.performReview("expanded", values: ["enabled": !expanded]) } } label: {
                Image(systemName: expanded ? "chevron.up" : "chevron.down")
            }.accessibilityLabel(expanded ? "收起复习卡" : "展开复习卡")
        }.buttonStyle(.borderless).disabled(model.isPerforming("reviewAction"))
    }

    @ViewBuilder private var ratingControls: some View {
        if state["showingAnswer"] as? Bool == true {
            HStack(spacing: 6) {
                ForEach(Array(["重来", "困难", "良好", "简单"].enumerated()), id: \.offset) { entry in
                    Button(entry.element) { Task { await model.performReview("rate", values: ["ease": entry.offset + 1]) } }
                        .font(.caption.weight(.medium)).frame(maxWidth: .infinity)
                }
            }.buttonStyle(.bordered).disabled(saving)
        } else {
            Button("显示答案") { Task { await model.performReview("reveal") } }
                .buttonStyle(.borderedProminent).frame(maxWidth: .infinity).disabled(saving)
        }
    }

    private func select(_ next: Int) {
        guard ids.indices.contains(next) else { return }
        Task { await model.performReview("select", values: ["targetId": ids[next]]) }
    }

    private var improvement: some View {
        NavigationStack {
            List {
                if let error = model.error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
                if let notice = state["notice"] as? String, !notice.isEmpty { Text(notice).font(.caption).textSelection(.enabled) }
                Section("选用回答") {
                    let pairs = state["selectedPairs"] as? [[String: Any]] ?? []
                    Text("已选 \(pairs.count) 组问答").font(.subheadline)
                    ForEach(pairs.indices, id: \.self) { index in
                        Text(pairs[index]["answer"] as? String ?? "").font(.caption).lineLimit(5)
                    }
                    if pairs.isEmpty { Text("从复习对话选择回答或段落后生成草稿。").font(.caption).foregroundStyle(.secondary) }
                }
                Section("生成改进草稿") {
                    Picker("详细程度", selection: Binding(get: { state["improveMode"] as? String ?? "verbose" }, set: { value in
                        Task { await model.performReview("improveMode", values: ["value": value]) }
                    })) { Text("详细").tag("verbose"); Text("精炼").tag("concise") }.pickerStyle(.segmented)
                    ForEach(["note", "anki", "all"], id: \.self) { target in
                        Button(["note": "更新笔记草稿", "anki": "改进 Anki 草稿", "all": "生成全部草稿"][target] ?? target) {
                            Task { await model.performReview("prepareDraft", values: ["target": target]) }
                        }
                    }.disabled(saving || (state["selectedPairs"] as? [[String: Any]] ?? []).isEmpty || (current["entity_id"] as? String ?? "").isEmpty)
                }
                draftSections
            }
            .navigationTitle("改进当前卡片").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { showsImprovement = false } } }
        }
        .tint(ReaderNativeTheme.accent)
        .alert(item: $confirmation) { value in
            Alert(title: Text(value.target == "anki" ? "确认写入 Anki 新卡？" : "确认更新原笔记？"),
                  message: Text("使用当前预览的草稿。"),
                  primaryButton: .default(Text("确认写入")) {
                      Task {
                          await model.performReview("commitDraft", values: ["target": value.target, "draftId": value.draftID, "cardId": value.cardID, "contextKey": value.contextKey, "confirmed": true])
                      }
                  }, secondaryButton: .cancel())
        }
    }

    @ViewBuilder private var draftSections: some View {
        if let draft = state["draft"] as? [String: Any] {
            if draft["busy"] as? Bool == true { ProgressView("正在生成草稿…") }
            else if draft["ok"] as? Bool != true { Text(draft["error"] as? String ?? "草稿生成失败").foregroundStyle(.red) }
            else {
                let drafts = draft["drafts"] as? [String: Any] ?? [:]
                let cards = drafts["cards"] as? [[String: Any]] ?? []
                Section("草稿预览 · 尚未写入") {
                    ForEach(cards.indices, id: \.self) { index in
                        VStack(alignment: .leading, spacing: 8) {
                            ReaderNativeRichText(content: cards[index]["front"] as? String ?? "", format: "html")
                            Divider()
                            ReaderNativeRichText(content: cards[index]["back"] as? String ?? "", format: "html")
                        }.padding(.vertical, 6)
                    }
                    if let note = drafts["note"] as? [String: Any] {
                        ReaderNativeRichText(content: note["content"] as? String ?? "")
                    }
                }
                Section("确认写入") {
                    let commits = state["commits"] as? [String: [String: Any]] ?? [:]
                    ForEach(draft["targets"] as? [String] ?? [], id: \.self) { target in
                        let result = commits[target] ?? [:]
                        Button(target == "anki" ? "确认写入 Anki 新卡" : "确认更新原笔记") {
                            confirmation = ReviewDraftConfirmation(target: target, draftID: draft["draft_id"] as? String ?? "", cardID: current["id"] as? String ?? "", contextKey: state["contextKey"] as? String ?? "")
                        }.disabled(saving || result["busy"] as? Bool == true || result["ok"] as? Bool == true)
                        if let message = result["message"] as? String, !message.isEmpty {
                            Text(message).font(.caption).foregroundStyle(result["ok"] as? Bool == true ? ReaderNativeTheme.accent : .red)
                                .textSelection(.enabled)
                        }
                    }
                }
                if draft["trace"] != nil || draft["runner"] != nil {
                    Section {
                        DisclosureGroup("模型处理详情") {
                            let detail: [String: Any] = ["runner": draft["runner"] ?? NSNull(), "trace": draft["trace"] ?? NSNull()]
                            Text(diagnostic(detail)).font(.caption.monospaced()).textSelection(.enabled)
                        }
                    }
                }
            }
        }
    }

    private func diagnostic(_ value: [String: Any]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]),
              let text = String(data: data, encoding: .utf8) else { return "处理详情暂不可读" }
        return text
    }
}

private struct ReviewDraftConfirmation: Identifiable {
    let id = UUID()
    let target: String
    let draftID: String
    let cardID: String
    let contextKey: String
}

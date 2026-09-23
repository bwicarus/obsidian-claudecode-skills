import SwiftUI

@MainActor
struct ReaderNativeConversationView: View {
    @ObservedObject var model: ReaderNativeConversationModel
    @ObservedObject var voiceBridge: NativeVoiceBridge
    let onClose: () -> Void
    let onDiagnostics: () -> Void
    let onSettings: () -> Void

    @State private var nearBottom = true
    @State private var resumeAtBottom = false
    @State private var confirmsClear = false
    @State private var resetsRecall = false
    /// 硬件键盘 Shift+Return 放行的那一个换行（见 composer 的 onChange）。
    @State private var keepsNextNewline = false
    @GestureState private var interacting = false

    private var isReview: Bool { model.conversationMode == "review" }

    private var canSend: Bool {
        model.ready && model.supports("send") && !model.isPerforming("send") &&
            !model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func submitDraft() {
        guard canSend else { return }
        let text = model.draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let original = model.draft
        Task {
            if await model.perform("send", parameters: ["text": text]), model.draft == original {
                model.draft = ""
                model.followsLatest = true
            }
        }
    }

    private var voiceAction: String? {
        if model.voice.mode == "realtime" { return model.supports("toggleVoice") ? "toggleVoice" : nil }
        if model.voice.mode == "computer" || voiceBridge.state.isActive || voiceBridge.state.isBusy {
            return model.supports("toggleComputerVoice") ? "toggleComputerVoice" : nil
        }
        if model.supports("toggleComputerVoice") { return "toggleComputerVoice" }
        return model.supports("toggleVoice") ? "toggleVoice" : nil
    }

    private var voiceActive: Bool {
        model.voice.active || (voiceAction == "toggleComputerVoice" && voiceBridge.state.isActive)
    }

    private var voiceBusy: Bool {
        model.voice.busy || (voiceAction == "toggleComputerVoice" && voiceBridge.state.isBusy)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if isReview && model.supports("reviewAction") {
                ReaderNativeReviewView(model: model)
                Divider()
            }
            if let error = model.error { errorBanner(error) }
            if voiceBridge.state.phase == .failed, let detail = voiceBridge.state.detail {
                Text(detail).font(.caption).foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(12)
            }
            conversation
            Divider()
            composer
        }
        .background(ReaderNativeTheme.canvas)
        .foregroundStyle(ReaderNativeTheme.ink)
        .tint(ReaderNativeTheme.accent)
        .onChange(of: model.scope) { _, _ in resumeAtBottom = false; confirmsClear = false }
        .sheet(item: $model.inspection) { _ in
            ReaderNativeArtifactInspector(model: model)
        }
        .sheet(isPresented: $confirmsClear) {
            NavigationStack {
                Form {
                    Section {
                        Text(isReview ? "清空当前复习对话，普通助手记录保留。学习卡和评分不受影响。" : "清空当前普通助手的对话记录，复习对话保留。书籍、卡片和学习记录不受影响。")
                        if !isReview {
                            Toggle("回顾学习从现在开始", isOn: $resetsRecall)
                            Text("开启后，回顾学习不再引用之前的记录；学习档案仍会保留。")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Button("清空当前对话", role: .destructive) {
                        Task {
                            let ok = await model.perform("clearConversation", parameters: ["value": ["confirmed": true, "resetRecall": resetsRecall]])
                            if ok { confirmsClear = false }
                        }
                    }.disabled(model.isPerforming("clearConversation"))
                    if let error = model.error { Text(error).font(.caption).foregroundStyle(.red) }
                }
                .navigationTitle("清空对话").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { confirmsClear = false } } }
            }.presentationDetents([.medium])
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Image(systemName: "waveform").foregroundStyle(ReaderNativeTheme.accent)
                Text(isReview ? "复习对话" : model.title).font(.headline).lineLimit(1)
                Spacer(minLength: 4)
                Button(action: onDiagnostics) {
                    Image(systemName: "waveform.path.ecg")
                }
                .accessibilityLabel("通话诊断")
                Button(action: onSettings) {
                    Image(systemName: "gearshape")
                }
                .accessibilityLabel("助手设置")
                .disabled(!model.ready || !(model.supports("openSettings") || model.supports("openModels")))
                Button(action: onClose) {
                    Image(systemName: "xmark.circle.fill")
                }
                .accessibilityLabel("收起助手")
            }
            .buttonStyle(.plain)
            HStack(spacing: 7) {
                if voiceBusy || model.busy { ProgressView().controlSize(.mini) }
                else {
                    Circle().fill(voiceActive ? ReaderNativeTheme.accent : ReaderNativeTheme.muted)
                        .frame(width: 6, height: 6)
                }
                Text(statusText).lineLimit(1)
                if model.showingCachedMessages {
                    // 让人知道眼前这些是本机存的那份，最新的还没拉到。
                    Label("本机缓存", systemImage: "internaldrive").labelStyle(.titleAndIcon)
                        .accessibilityLabel("正在显示本机缓存的对话记录")
                }
                Spacer(minLength: 0)
                if model.busy { Text("正在处理").foregroundStyle(ReaderNativeTheme.accent) }
            }
            .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
            HStack(spacing: 10) {
                if let voiceAction {
                    Button {
                        Task { await model.perform(voiceAction) }
                    } label: {
                        Label(voiceActive || voiceBusy ? "结束通话" : voiceAction == "toggleComputerVoice" ? "电脑语音" : "开始语音",
                              systemImage: voiceActive || voiceBusy ? "stop.fill" : "waveform")
                            .font(.subheadline.weight(.medium))
                            .frame(maxWidth: .infinity).padding(.vertical, 3)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!model.ready || model.isPerforming(voiceAction))
                }
                if model.supports("newConversation") {
                    Button {
                        Task { await model.perform("newConversation") }
                    } label: {
                        Label("新话题", systemImage: "square.and.pencil")
                            .font(.subheadline).padding(.vertical, 3)
                    }
                    .buttonStyle(.bordered)
                    .disabled(!model.ready || model.isPerforming("newConversation"))
                }
                // ⚠ 旧界面的入口（“完整功能”）已删除，连带能力本身也不再上报。
                //   菜单的出现条件改成"它自己有东西可点"，而不是"能不能召唤旧界面"。
                if ["openReview", "openModels", "openSettings", "openSearch",
                    "toggleVoice", "toggleComputerVoice", "clearConversation"].contains(where: model.supports) {
                    Menu {
                        if model.supports("openReview") {
                            Button(isReview ? "结束复习" : "打开复习", systemImage: "rectangle.on.rectangle") {
                                Task { await model.perform("openReview") }
                            }
                        }
                        if model.supports("openModels") {
                            Button("模型与声音", systemImage: "slider.horizontal.3") {
                                Task { await model.perform("openModels") }
                            }
                        }
                        if model.supports("openSettings") {
                            Button("语音来源与阅读设置", systemImage: "desktopcomputer") {
                                Task { await model.perform("openSettings") }
                            }
                        }
                        if model.supports("openHistory") {
                            Button("历史对话", systemImage: "clock") {
                                Task { await model.perform("openHistory") }
                            }
                        }
                        // 清空对话常驻在这里。⚠ 原来它排在「带入的卡片」那一排里，于是
                        // 只有带着卡片时才看得见 —— 其余时候根本找不到（2026-09-23 用户实报）。
                        if model.supports("clearConversation") {
                            Button("清空当前对话", systemImage: "trash", role: .destructive) {
                                resetsRecall = false; confirmsClear = true
                            }
                        }
                        if !voiceActive && !voiceBusy {
                            if model.supports("toggleVoice"), voiceAction != "toggleVoice" {
                                Button("开始普通语音", systemImage: "phone") {
                                    Task { await model.perform("toggleVoice") }
                                }
                            }
                            if model.supports("toggleComputerVoice"), voiceAction != "toggleComputerVoice" {
                                Button("开始电脑语音", systemImage: "desktopcomputer") {
                                    Task { await model.perform("toggleComputerVoice") }
                                }
                            }
                        }
                    } label: {
                        Image(systemName: "ellipsis").padding(.vertical, 3)
                    }
                    .buttonStyle(.bordered)
                    .accessibilityLabel("更多助手功能")
                }
            }
        }
        .padding(16)
        .background(ReaderNativeTheme.card)
    }

    private var statusText: String {
        if let label = model.voice.label, !label.isEmpty { return label }
        if model.voice.mode == "realtime" {
            if model.voice.busy { return "正在连接语音…" }
            return model.voice.active ? "通话中" : "随时可以开始"
        }
        switch voiceBridge.state.phase {
        case .active: return "通话中"
        case .suspended: return "等待恢复通话"
        case .preparing, .connecting, .starting: return "正在连接语音…"
        case .stopping: return "正在结束通话…"
        case .failed: return "语音连接失败"
        case .idle: return model.ready ? "随时可以开始" : "正在读取会话…"
        }
    }

    private func errorBanner(_ error: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.circle").foregroundStyle(.red)
            Text(error).font(.caption).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            if model.supports("refresh") {
                Button("重试") { Task { await model.perform("refresh") } }
                    .font(.caption).disabled(model.isPerforming("refresh"))
            }
            Button { model.clearError() } label: { Image(systemName: "xmark") }
                .font(.caption).accessibilityLabel("关闭错误提示")
        }
        .padding(12).background(ReaderNativeTheme.card)
    }

    private var conversation: some View {
        GeometryReader { viewport in
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 18) {
                        if model.messages.isEmpty { emptyState }
                        ForEach(model.messages) { message in
                            ReaderNativeConversationMessageView(message: message, model: model)
                                .id(message.id)
                        }
                        Color.clear.frame(height: 1).id("nativeConversationBottom")
                    }
                    .scrollTargetLayout()
                    .padding(16)
                    .background {
                        GeometryReader { content in
                            Color.clear.preference(key: ReaderNativeConversationBottom.self,
                                                   value: content.frame(in: .named("nativeConversation")).maxY)
                        }
                    }
                }
                .coordinateSpace(name: "nativeConversation")
                .scrollPosition(id: $model.visibleMessageID, anchor: .top)
                .scrollDismissesKeyboard(.interactively)
                .onPreferenceChange(ReaderNativeConversationBottom.self) { bottom in
                    nearBottom = bottom <= viewport.size.height + 48
                    guard !interacting else { return }
                    if resumeAtBottom && nearBottom {
                        model.followsLatest = true
                        resumeAtBottom = false
                    }
                    if model.followsLatest { proxy.scrollTo("nativeConversationBottom", anchor: .bottom) }
                }
                // ⚠ minimumDistance 不能是 0：那样手指一落下就被它认领，卡片标题上的系统长按
                //   拖动（.draggable，UIKit 的拖放交互）起不来 —— 2026-09-23 用户："拖卡甚至
                //   无法在侧边栏中长按进入拖动模式"。它只需要知道"用户在滚"，手指真的动了再算。
                .simultaneousGesture(
                    DragGesture(minimumDistance: 8)
                        .updating($interacting) { _, active, _ in active = true }
                        .onChanged { _ in
                            model.followsLatest = false
                            resumeAtBottom = false
                        }
                        .onEnded { value in
                            resumeAtBottom = value.translation.height < -8 &&
                                abs(value.translation.height) > abs(value.translation.width)
                            if resumeAtBottom && nearBottom {
                                model.followsLatest = true
                                resumeAtBottom = false
                            }
                        }
                )
                .onChange(of: model.revision) { _, _ in
                    guard model.followsLatest, !interacting else { return }
                    proxy.scrollTo("nativeConversationBottom", anchor: .bottom)
                }
                .onChange(of: model.scope) { _, _ in
                    proxy.scrollTo("nativeConversationBottom", anchor: .bottom)
                }
                .onAppear {
                    if model.followsLatest { proxy.scrollTo("nativeConversationBottom", anchor: .bottom) }
                    else if let visibleID = model.visibleMessageID { proxy.scrollTo(visibleID, anchor: .top) }
                }
                .overlay(alignment: .bottomTrailing) {
                    if !model.followsLatest && !interacting {
                        Button {
                            model.followsLatest = true
                            resumeAtBottom = false
                            withAnimation(.easeOut(duration: 0.2)) {
                                proxy.scrollTo("nativeConversationBottom", anchor: .bottom)
                            }
                        } label: {
                            Label("回到最新", systemImage: "arrow.down")
                                .font(.caption.weight(.medium))
                        }
                        .buttonStyle(.borderedProminent).padding(12)
                    }
                }
            }
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !model.ready {
                ProgressView("正在读取当前会话…")
                Text("阅读内容准备好后，消息和生成的卡片会显示在这里。")
                    .font(.subheadline).foregroundStyle(ReaderNativeTheme.muted)
            } else {
                Image(systemName: "text.bubble").font(.title2).foregroundStyle(ReaderNativeTheme.accent)
                Text(isReview ? "一起回顾刚学的内容。" : "边阅读，边聊想法。")
                    .font(.title3.weight(.medium))
                Text(isReview ? "可以询问复习内容。文字会保存到独立的复习对话。" : "可以询问选中的内容，或请助手整理知识卡。语音与文字使用同一段对话。")
                    .font(.subheadline).foregroundStyle(ReaderNativeTheme.muted)
            }
        }
        .lineSpacing(4).padding(.vertical, 20)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 7) {
            if !model.selectionText.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "text.quote").foregroundStyle(ReaderNativeTheme.accent)
                    Text(model.selectionText).font(.caption).lineLimit(3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button {
                        Task { await model.perform("clearSelection") }
                    } label: { Image(systemName: "xmark.circle.fill") }
                    .accessibilityLabel("移除选中文本")
                }
                .padding(9).background(ReaderNativeTheme.accentWash, in: RoundedRectangle(cornerRadius: 10))
            }
            if !model.attachments.isEmpty {
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        ForEach(model.attachments) { item in
                            HStack(spacing: 7) {
                                Image(systemName: "rectangle.on.rectangle").foregroundStyle(ReaderNativeTheme.accent)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.title).font(.caption.weight(.medium)).lineLimit(1)
                                    if !item.text.isEmpty {
                                        Text(item.text).font(.caption2).foregroundStyle(ReaderNativeTheme.muted).lineLimit(1)
                                    }
                                }.frame(maxWidth: 160, alignment: .leading)
                                Button {
                                    Task { await model.perform("liveAction", parameters: ["actionId": item.removeID]) }
                                } label: { Image(systemName: "xmark.circle.fill") }
                                .accessibilityLabel("移除\(item.title)")
                            }
                            .padding(9).background(ReaderNativeTheme.accentWash, in: RoundedRectangle(cornerRadius: 10))
                        }
                    }
                }.scrollIndicators(.hidden)
            }
            if isReview {
                Label(voiceActive || voiceBusy ? "复习文字使用独立对话，与当前语音分开。" : "复习对话", systemImage: "rectangle.on.rectangle")
                    .font(.caption2).foregroundStyle(ReaderNativeTheme.muted)
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField(model.supports("send") ? (isReview ? "在复习对话中输入…" : "也可以输入文字…") : "文字输入尚未就绪", text: $model.draft, axis: .vertical)
                    .lineLimit(1...5).font(.subheadline)
                    .padding(11).background(ReaderNativeTheme.canvas, in: RoundedRectangle(cornerRadius: 14))
                    .disabled(!model.ready || !model.supports("send"))
                    // ⚠ 多行 TextField 的回车默认是换行，onSubmit 根本不触发 ——
                    //   用户 2026-09-23：「回车键无法发送信息」。
                    //   约定跟常见聊天输入一致：回车发送，Shift+回车换行。
                    //   硬件键盘走 onKeyPress；软键盘的回车不经 onKeyPress，
                    //   只能从文字变化里认出「刚插进来的那一个换行」再撤掉它去发送。
                    .onKeyPress(.return, phases: .down) { press in
                        if press.modifiers.contains(.shift) {
                            keepsNextNewline = true
                            return .ignored
                        }
                        submitDraft()
                        return .handled
                    }
                    .onChange(of: model.draft) { old, new in
                        if keepsNextNewline { keepsNextNewline = false; return }
                        guard new.count == old.count + 1,
                              new.filter(\.isNewline).count == old.filter(\.isNewline).count + 1
                        else { return }
                        model.draft = old
                        submitDraft()
                    }
                if model.busy && model.supports("stop") {
                    Button {
                        Task { await model.perform("stop") }
                    } label: {
                        Image(systemName: "stop.circle").font(.title2)
                            .frame(width: 38, height: 42)
                    }
                    .accessibilityLabel("停止当前回复")
                    .disabled(model.isPerforming("stop"))
                }
                Button {
                    submitDraft()
                } label: {
                    Group {
                        if model.isPerforming("send") { ProgressView() }
                        else { Image(systemName: "arrow.up.circle.fill").font(.title) }
                    }.frame(width: 38, height: 42)
                }
                .accessibilityLabel("发送消息")
                .disabled(!canSend)
            }
        }
        .padding(12).background(ReaderNativeTheme.card)
    }
}

private struct ReaderNativeConversationBottom: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

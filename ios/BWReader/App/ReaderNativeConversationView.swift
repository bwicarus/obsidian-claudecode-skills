import SwiftUI
import UIKit
import PhotosUI
import UniformTypeIdentifiers
import ImageIO

@MainActor
struct ReaderNativeConversationView: View {
    @ObservedObject var model: ReaderNativeConversationModel
    @ObservedObject var voiceBridge: NativeVoiceBridge
    @ObservedObject private var media: ReaderNativeMediaDraft
    let onClose: () -> Void
    let onDiagnostics: () -> Void
    let onSettings: () -> Void

    @State private var nearBottom = true
    @State private var resumeAtBottom = false
    @State private var confirmsClear = false
    @State private var resetsRecall = false
    @State private var selectsFiles = false
    @State private var selectionMode = "normal"
    @State private var photos: [PhotosPickerItem] = []
    @State private var mediaEditor: MediaEditorTarget?
    private struct MediaEditorTarget: Identifiable { let id: String; let image: UIImage }
    @GestureState private var interacting = false

    private var isReview: Bool { model.conversationMode == "review" }

    init(model: ReaderNativeConversationModel, voiceBridge: NativeVoiceBridge,
         onClose: @escaping () -> Void, onDiagnostics: @escaping () -> Void, onSettings: @escaping () -> Void) {
        self.model = model; self.voiceBridge = voiceBridge; self.media = model.mediaDraft
        self.onClose = onClose; self.onDiagnostics = onDiagnostics; self.onSettings = onSettings
    }

    private var canSend: Bool {
        model.ready && model.supports("send") && !model.isPerforming("send") &&
            media.ready(in: model.conversationMode) &&
            (!model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !media.items(in: model.conversationMode).isEmpty)
    }

    private func submitDraft() {
        guard canSend else { return }
        let text = model.draft.trimmingCharacters(in: .whitespacesAndNewlines)
        if !media.items(in: model.conversationMode).isEmpty, text.utf16.count > 4000 {
            media.error = "随附件发送的文字最多 4000 字符。较长内容可以作为文本文件添加。"; return
        }
        let original = model.draft
        let mode = model.conversationMode
        let submission = media.submission(text: text, mode: mode)
        guard media.items(in: mode).isEmpty || submission != nil else { return }
        var parameters: [String: Any] = ["text": text.isEmpty ? "请查看附件。" : text]
        if let submission {
            parameters["attachmentIds"] = submission.ids
            parameters["submissionId"] = submission.id
            parameters["attachmentReferences"] = submission.referenceText
        }
        Task {
            if await model.perform("send", parameters: parameters) {
                if let submission { media.accepted(submission) }
                if model.draft == original, model.conversationMode == mode { model.draft = "" }
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
        .fullScreenCover(item: $mediaEditor) { target in
            NativePencilAnnotationEditor(image: target.image, editsAttachment: true) { image in
                media.saveDrawing(image, replacing: target.id)
            }
        }
        .fileImporter(isPresented: $selectsFiles, allowedContentTypes: [.data], allowsMultipleSelection: true) { result in
            switch result {
            case .success(let urls): media.add(urls, mode: selectionMode)
            case .failure(let error): media.error = error.localizedDescription
            }
        }
        .onChange(of: photos) { _, values in
            guard !values.isEmpty else { return }
            let mode = model.conversationMode
            photos = []
            media.addPhotos(values, mode: mode)
        }
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
                    LazyVStack(alignment: .leading, spacing: 12) {
                        if model.messages.isEmpty { emptyState }
                        ForEach(Array(model.messages.enumerated()), id: \.element.id) { index, message in
                            let previous = index > 0 ? model.messages[index - 1].role : ""
                            ReaderNativeConversationMessageView(message: message, model: model,
                                                                showsRole: previous != message.role)
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
            if let error = media.error {
                Text(error).font(.caption).foregroundStyle(.red)
            }
            if media.importing[model.conversationMode, default: 0] > 0 {
                HStack(spacing: 7) {
                    ProgressView().controlSize(.small)
                    Text("正在读取所选照片或视频…").font(.caption).foregroundStyle(.secondary)
                }
            }
            if !media.items(in: model.conversationMode).isEmpty {
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        ForEach(media.items(in: model.conversationMode)) { item in
                            HStack(spacing: 8) {
                                if let preview = item.preview {
                                    Button {
                                        if let image = UIImage(contentsOfFile: preview.path) {
                                            mediaEditor = MediaEditorTarget(id: item.id, image: image)
                                        }
                                    } label: { ReaderMediaThumbnail(url: preview) }
                                    .buttonStyle(.plain).accessibilityLabel("放大并标注\(item.name)")
                                    .disabled(item.working || model.isPerforming("send"))
                                } else { Image(systemName: item.icon) }
                                if item.working { ProgressView().controlSize(.small) }
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.name).font(.caption.weight(.medium)).lineLimit(1)
                                    Text(item.error ?? (item.uploaded ? "待发送 · " + ByteCountFormatter.string(fromByteCount: Int64(item.bytes), countStyle: .file) : "正在上传…"))
                                        .font(.caption2).foregroundStyle(item.error == nil ? ReaderNativeTheme.muted : .red).lineLimit(2)
                                }.frame(maxWidth: 170, alignment: .leading)
                                if item.error != nil, item.file != nil {
                                    Button("重试") { media.retry(item.id) }.font(.caption)
                                }
                                Button { media.remove(item.id) } label: { Image(systemName: "xmark.circle.fill") }
                                    .accessibilityLabel("移除附件\(item.name)")
                                    .disabled(model.isPerforming("send"))
                            }.padding(9).background(ReaderNativeTheme.accentWash, in: RoundedRectangle(cornerRadius: 10))
                        }
                    }
                }.scrollIndicators(.hidden)
                if isReview { Text("附件以文件链接加入复习对话。").font(.caption2).foregroundStyle(.secondary) }
            }
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
                Menu {
                    PhotosPicker(selection: $photos, maxSelectionCount: 10, matching: .any(of: [.images, .videos])) {
                        Label("照片或视频", systemImage: "photo.on.rectangle")
                    }
                    Button("选择文件", systemImage: "doc") {
                        selectionMode = model.conversationMode; selectsFiles = true
                    }
                } label: {
                    Image(systemName: "plus.circle").font(.title2).frame(width: 32, height: 42)
                }
                .accessibilityLabel("添加附件")
                .disabled(model.isPerforming("send"))
                ZStack(alignment: .topLeading) {
                    if model.draft.isEmpty {
                        Text(model.supports("send") ? (isReview ? "在复习对话中输入…" : "也可以输入文字…") : "文字输入尚未就绪")
                            .font(.subheadline).foregroundStyle(.tertiary)
                            .allowsHitTesting(false).accessibilityHidden(true)
                    }
                    ReaderNativeComposerInput(text: $model.draft, onSubmit: submitDraft)
                }
                    .padding(11).background(ReaderNativeTheme.canvas, in: RoundedRectangle(cornerRadius: 14))
                    .disabled(!model.ready || !model.supports("send"))
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

private struct ReaderMediaThumbnail: View {
    let url: URL
    @State private var image: UIImage?
    var body: some View {
        Group {
            if let image { Image(uiImage: image).resizable().scaledToFill() }
            else { Image(systemName: "photo") }
        }
        .frame(width: 44, height: 44).clipShape(RoundedRectangle(cornerRadius: 7))
        .task(id: url) {
            let loaded = await Task.detached(priority: .utility) { () -> UIImage? in
                guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
                      let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                        kCGImageSourceCreateThumbnailFromImageAlways: true,
                        kCGImageSourceThumbnailMaxPixelSize: 132,
                        kCGImageSourceCreateThumbnailWithTransform: true,
                      ] as CFDictionary) else { return nil }
                return UIImage(cgImage: image)
            }.value
            if !Task.isCancelled { image = loaded }
        }
    }
}

/// UIKit owns composition and candidate confirmation. Observing a new newline
/// in a SwiftUI binding cannot distinguish Return from paste, undo or an IME.
@MainActor
private struct ReaderNativeComposerInput: UIViewRepresentable {
    @Binding var text: String
    let onSubmit: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> ReaderNativeComposerTextView {
        let view = ReaderNativeComposerTextView()
        view.delegate = context.coordinator
        view.backgroundColor = .clear
        view.font = .preferredFont(forTextStyle: .subheadline)
        view.adjustsFontForContentSizeCategory = true
        view.textColor = .label
        view.textContainerInset = .zero
        view.textContainer.lineFragmentPadding = 0
        view.returnKeyType = .send
        view.enablesReturnKeyAutomatically = true
        view.accessibilityLabel = "消息输入框"
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return view
    }

    func updateUIView(_ view: ReaderNativeComposerTextView, context: Context) {
        context.coordinator.parent = self
        view.isEditable = context.environment.isEnabled
        // Assigning text while marked text exists cancels the user's candidate
        // session. Unchanged text must also retain its selection/undo state.
        if view.markedTextRange == nil, view.text != text {
            let selection = view.selectedRange
            view.text = text
            let end = (text as NSString).length
            let start = min(selection.location, end)
            view.selectedRange = NSRange(location: start, length: min(selection.length, end - start))
        }
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: ReaderNativeComposerTextView, context: Context) -> CGSize? {
        guard let width = proposal.width, width > 0 else { return nil }
        let line = uiView.font?.lineHeight ?? 20
        let fitting = uiView.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude))
        return CGSize(width: width, height: min(ceil(line * 5), max(ceil(line), ceil(fitting.height))))
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: ReaderNativeComposerInput
        init(_ parent: ReaderNativeComposerInput) { self.parent = parent }

        func textViewDidChange(_ textView: UITextView) {
            parent.text = textView.text ?? ""
            textView.invalidateIntrinsicContentSize()
        }

        func textView(_ textView: UITextView, shouldChangeTextIn range: NSRange, replacementText text: String) -> Bool {
            guard text == "\n", let view = textView as? ReaderNativeComposerTextView,
                  view.markedTextRange == nil, !view.preservesReturn, !view.justConfirmedComposition, !view.isInsertingContent,
                  view.undoManager?.isUndoing != true, view.undoManager?.isRedoing != true else { return true }
            parent.text = view.text ?? ""
            parent.onSubmit()
            return false
        }
    }
}

@MainActor
private final class ReaderNativeComposerTextView: UITextView {
    private(set) var preservesReturn = false
    private(set) var justConfirmedComposition = false
    private var compositionCommit = 0
    private(set) var isInsertingContent = false

    override func unmarkText() {
        if markedTextRange != nil {
            // Software IMEs can remove marked text before delivering their
            // Return replacement. Preserve the candidate-confirm action for
            // this event loop too; the next independent Return can still send.
            justConfirmedComposition = true
            compositionCommit += 1
            let ticket = compositionCommit
            DispatchQueue.main.async { [weak self] in
                guard let self, self.compositionCommit == ticket else { return }
                self.justConfirmedComposition = false
            }
        }
        super.unmarkText()
    }

    private func returnKey(in presses: Set<UIPress>) -> UIKey? {
        presses.compactMap(\.key).first {
            $0.keyCode == .keyboardReturnOrEnter || $0.keyCode == .keypadEnter
        }
    }

    override func pressesBegan(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        if let key = returnKey(in: presses) {
            // Snapshot before UIKit confirms a candidate and removes its marked
            // range. The same physical key press still must not send a message.
            preservesReturn = key.modifierFlags.contains(.shift) || markedTextRange != nil
        }
        super.pressesBegan(presses, with: event)
    }

    override func pressesEnded(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        super.pressesEnded(presses, with: event)
        if returnKey(in: presses) != nil { preservesReturn = false }
    }

    override func pressesCancelled(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        super.pressesCancelled(presses, with: event)
        if returnKey(in: presses) != nil { preservesReturn = false }
    }

    override func insertText(_ text: String) {
        let previous = preservesReturn
        if text == "\n", markedTextRange != nil { preservesReturn = true }
        defer { preservesReturn = previous }
        super.insertText(text)
    }

    override func paste(_ sender: Any?) {
        isInsertingContent = true
        defer { isInsertingContent = false }
        super.paste(sender)
    }

    override func insertDictationResult(_ dictationResult: [UIDictationPhrase]) {
        isInsertingContent = true
        defer { isInsertingContent = false }
        super.insertDictationResult(dictationResult)
    }
}

private struct ReaderNativeConversationBottom: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

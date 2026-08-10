import SwiftUI
import UIKit
import UniformTypeIdentifiers

@MainActor
final class NativeReaderToolsCoordinator: ObservableObject {
    enum Activity: Equatable {
        case idle
        case refreshing
        case recognizing
        case recognizingFormulas
        case preparingAnnotation
        case savingAnnotation

        var isBusy: Bool { self != .idle }
    }

    @Published private(set) var snapshot: ReaderSharedSnapshot?
    @Published private(set) var viewportImage: UIImage?
    @Published private(set) var recognizedText = ""
    @Published private(set) var formulaStatus: NativeFormulaRecognitionStatus?
    @Published private(set) var activity: Activity = .idle
    @Published private(set) var notice: String?
    @Published private(set) var errorMessage: String?
    @Published private(set) var quickNotes: [ReaderQuickNote]

    private let store = ReaderNativeFeatureStore()
    private let recognizer = NativeReaderTextRecognizer()

    init() {
        quickNotes = ReaderNativeFeatureStore().readQuickNotes()
    }

    var preferredText: String {
        if !recognizedText.isEmpty {
            return recognizedText
        }
        guard let snapshot else { return "" }
        if !snapshot.selection.isEmpty {
            return snapshot.selection
        }
        return snapshot.visibleText
    }

    func refresh(using reader: ReaderWebViewModel) async {
        await perform(.refreshing) {
            snapshot = try await reader.refreshNativeSharedSnapshot()
            quickNotes = store.readQuickNotes()
            notice = "当前页面信息已更新"
        }
    }

    func recognizeCurrentViewport(using reader: ReaderWebViewModel) async {
        await perform(.recognizing) {
            async let snapshotTask = reader.refreshNativeSharedSnapshot()
            let image = try await reader.captureNativeViewportImage()
            let text = try await recognizer.recognize(image)
            viewportImage = image
            recognizedText = text
            snapshot = try await snapshotTask
            notice = "已使用设备端实况文本识别当前视口普通文字"
        }
    }

    func recognizeBookFormulas(using reader: ReaderWebViewModel) async {
        await perform(.recognizingFormulas) {
            let current = try await reader.refreshNativeSharedSnapshot()
            snapshot = current
            formulaStatus = try await reader.startNativeFormulaRecognition(
                file: current.file
            )
            if formulaStatus?.detectingBoxes == true {
                notice = "已启动现有公式框检测；稍后点刷新即可继续 AI 批处理"
            } else {
                notice = formulaStatus?.remaining == 0
                    ? "本书公式已全部转成 LaTeX"
                    : "公式已交给现有 AI 批处理；关闭此页面也会继续"
            }
        }
    }

    func refreshFormulaStatus(using reader: ReaderWebViewModel) async {
        guard let file = snapshot?.file, !file.isEmpty else { return }
        await perform(.recognizingFormulas) {
            formulaStatus = try await reader.refreshNativeFormulaRecognitionStatus(
                file: file
            )
        }
    }

    func prepareAnnotation(using reader: ReaderWebViewModel) async {
        await perform(.preparingAnnotation) {
            async let snapshotTask = reader.refreshNativeSharedSnapshot()
            viewportImage = try await reader.captureNativeViewportImage()
            snapshot = try await snapshotTask
            notice = nil
        }
    }

    func saveAnnotation(_ image: UIImage) async {
        await perform(.savingAnnotation) {
            guard let data = image.pngData() else {
                throw CocoaError(.fileWriteUnknown)
            }
            let timestamp = Int64(Date().timeIntervalSince1970 * 1_000)
            let suffix = UUID().uuidString.prefix(8)
            let fileURL = try store.annotationDirectory()
                .appendingPathComponent(
                    "reader-annotation-\(timestamp)-\(suffix).png",
                    isDirectory: false
                )
            try data.write(to: fileURL, options: [.atomic])
            viewportImage = nil
            notice = "标注已保存到 BWReader 的共享存储"
        }
    }

    func dismissNotice() {
        notice = nil
    }

    private func perform(
        _ newActivity: Activity,
        operation: () async throws -> Void
    ) async {
        guard !activity.isBusy else { return }
        activity = newActivity
        errorMessage = nil
        do {
            try await operation()
        } catch {
            errorMessage = error.localizedDescription
        }
        activity = .idle
    }
}

// Stable integration name for the App root and App Intents routing layer.
typealias NativeReaderFeatureCoordinator = NativeReaderToolsCoordinator

private enum ReaderTouchDoubleTapAction: String, CaseIterable, Identifiable {
    case eraser
    case selection
    case none

    var id: String { rawValue }

    var title: String {
        switch self {
        case .eraser:
            return "切换画笔与橡皮"
        case .selection:
            return "切换画笔与选区笔"
        case .none:
            return "不执行操作"
        }
    }
}

struct NativeReaderToolsView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var coordinator = NativeReaderToolsCoordinator()
    @StateObject private var localNotes = ReaderLocalNotesManager.shared
    @StateObject private var piSync = ReaderPiSyncCoordinator()
    @StateObject private var textRecognition = ReaderTextRecognitionPreferences.shared
    @StateObject private var realtimeCredentials =
        ReaderRealtimeCredentialManager.shared
    @State private var presentsAnnotation = false
    @State private var presentsTranslation = false
    @State private var presentsLocalLibrary = false
    @State private var presentsPiLogin = false
    @State private var presentsLocalNotesFolderPicker = false
    @State private var selectedLocalNote: ReaderLocalNoteProjection?
    @State private var translationText = ""
    @State private var performedInitialAction = false
    @State private var touchDoubleTapAction = ReaderTouchDoubleTapAction.eraser
    @State private var touchDoubleTapLoaded = false
    @State private var touchDoubleTapError: String?
    @State private var realtimeKeyDraft = ""

    let reader: ReaderWebViewModel
    let initialAction: ReaderNativeFeatureAction?
    init(
        reader: ReaderWebViewModel,
        initialAction: ReaderNativeFeatureAction? = nil
    ) {
        self.reader = reader
        self.initialAction = initialAction
    }

    var body: some View {
        NavigationStack {
            Form {
                currentPageSection
                nativeActionsSection
                recognitionSection
                textRecognitionSettingsSection
                localLibrarySection
                realtimeCredentialsSection
                piSyncSection
                localNotesSection
                quickNotesSection
                NativePencilSettingsSection()
                touchInputSection
                statusSection
            }
            .navigationTitle("原生阅读工具")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("刷新") {
                        Task { await coordinator.refresh(using: reader) }
                    }
                    .disabled(coordinator.activity.isBusy)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }
                }
            }
            .task {
                if coordinator.snapshot == nil {
                    await coordinator.refresh(using: reader)
                }
                await loadTouchDoubleTapAction()
                await performInitialActionIfNeeded()
            }
            .sheet(isPresented: $presentsTranslation) {
                NativeTranslationToolView(initialText: translationText)
            }
            .sheet(isPresented: $presentsLocalLibrary) {
                ReaderLocalLibraryView(reader: reader)
            }
            .sheet(isPresented: $presentsPiLogin) {
                ReaderPiLoginView(
                    dataStore: reader.webView.configuration.websiteDataStore
                )
            }
            .fullScreenCover(isPresented: $presentsAnnotation) {
                if let image = coordinator.viewportImage {
                    NativePencilAnnotationEditor(image: image) { annotated in
                        Task { await coordinator.saveAnnotation(annotated) }
                    }
                }
            }
            .fileImporter(
                isPresented: $presentsLocalNotesFolderPicker,
                allowedContentTypes: [.folder]
            ) { result in
                switch result {
                case .success(let url):
                    localNotes.configureFolder(url)
                case .failure(let error):
                    if (error as? CocoaError)?.code != .userCancelled {
                        localNotes.reportError(error)
                    }
                }
            }
            .sheet(item: $selectedLocalNote) { note in
                NavigationStack {
                    ScrollView {
                        Text(note.content)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding()
                            .textSelection(.enabled)
                    }
                    .navigationTitle(note.title)
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("完成") { selectedLocalNote = nil }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var textRecognitionSettingsSection: some View {
        Section("文字识别") {
            Toggle("启用书籍文字识别", isOn: $textRecognition.isEnabled)

            Toggle(
                "自动识别无文字层书籍",
                isOn: $textRecognition.automaticLocalProcessingEnabled
            )
            .disabled(!textRecognition.isEnabled)

            Text("开启后，PDF 下载到本机或首次打开时会启动这台设备的 Apple 预处理；不会上传书籍，也不会自动调用 Pi。")
                .font(.caption)
                .foregroundStyle(.secondary)

            Text("本机处理失败或效果不理想时，可在书库中为该书手动选择“Pi 预处理”。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var localLibrarySection: some View {
        Section("书库") {
            Button {
                presentsLocalLibrary = true
            } label: {
                Label("本机书库", systemImage: "books.vertical")
            }

            Text("本机书籍会直接离线打开；Pi 只用于显式同步与备份，不是打开本机书籍的前置条件，也不会自动覆盖或删除任何书籍。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var realtimeCredentialsSection: some View {
        Section("OpenAI Realtime（本机）") {
            SecureField("输入现有 OpenAI Key（sk-…）", text: $realtimeKeyDraft)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .font(.system(.body, design: .monospaced))

            Button {
                Task {
                    let cookies = await reader.remoteLibraryCookies()
                    await realtimeCredentials.saveExistingKey(
                        realtimeKeyDraft,
                        cookies: cookies
                    )
                    if realtimeCredentials.errorMessage == nil,
                       realtimeCredentials.status.isConfigured {
                        realtimeKeyDraft = ""
                    }
                }
            } label: {
                Label(
                    realtimeCredentials.isRunning
                        ? "正在保存并同步语音设置…"
                        : (realtimeCredentials.status.isConfigured
                            ? "替换 Key 并同步语音设置"
                            : "保存 Key 并同步语音设置"),
                    systemImage: "key.fill"
                )
            }
            .disabled(
                realtimeCredentials.isRunning ||
                realtimeKeyDraft.trimmingCharacters(
                    in: .whitespacesAndNewlines
                ).isEmpty
            )

            if realtimeCredentials.status.isConfigured {
                LabeledContent(
                    "状态",
                    value: "已存入 Apple Keychain"
                )
                if !realtimeCredentials.status.model.isEmpty {
                    LabeledContent(
                        "Realtime 模型",
                        value: realtimeCredentials.status.model
                    )
                }
                if let importedAt = realtimeCredentials.status.importedAt {
                    LabeledContent(
                        "保存时间",
                        value: importedAt.formatted(
                            date: .abbreviated,
                            time: .shortened
                        )
                    )
                }
                Button("清除这台 iPad 中的 Key", role: .destructive) {
                    realtimeCredentials.clear()
                }
                .disabled(realtimeCredentials.isRunning)
            } else {
                Text("未保存；请先在 App 中输入一次现有 Key，App 与 Safari 扩展才可脱离 Pi 使用普通 Realtime。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Text("Key 只在这个 App 中输入并写入 Apple Keychain；Safari 扩展只由原生进程共享读取，Key 不会发送给 Pi，也不会进入 Reader 网页、扩展 JavaScript、代码、构建产物或日志。保存时 Pi 仅同步一次不含密钥的现有语音设置，之后语音、选区与笔迹合成图可脱离 Pi 直接连接 OpenAI。")
                .font(.caption)
                .foregroundStyle(.secondary)

            if let notice = realtimeCredentials.notice {
                Label(notice, systemImage: "checkmark.circle.fill")
                    .font(.footnote)
                    .foregroundStyle(.green)
            }
            if let error = realtimeCredentials.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
    }

    @ViewBuilder
    private var piSyncSection: some View {
        Section("Pi 同步") {
            Button {
                presentsPiLogin = true
            } label: {
                Label("登录或重新登录 Pi", systemImage: "person.crop.circle.badge.checkmark")
            }
            .disabled(piSync.isRunning)

            Button {
                Task { await piSync.syncToPi(using: reader) }
            } label: {
                Label(
                    piSync.isRunning ? "正在与 Pi 同步" : "与 Pi 同步",
                    systemImage: "arrow.triangle.2.circlepath.icloud"
                )
            }
            .disabled(piSync.isRunning)

            if piSync.isRunning {
                HStack {
                    ProgressView()
                    Text(piSync.phase.title)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }

            if let report = piSync.report {
                LabeledContent("结果", value: report.state.title)
                Text("书籍：\(report.books.summary)")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                Text("数据：\(report.data.summary)")
                    .font(.footnote)
                    .foregroundStyle(.secondary)

                if !report.books.remoteNewer.isEmpty {
                    Text(
                        "Pi 较新，未覆盖：" +
                            report.books.remoteNewer.prefix(5).joined(separator: "、")
                    )
                    .font(.caption)
                    .foregroundStyle(.orange)
                }
                if !report.books.conflicts.isEmpty {
                    Text(
                        "两端冲突，未覆盖：" +
                            report.books.conflicts.prefix(5).joined(separator: "、")
                    )
                    .font(.caption)
                    .foregroundStyle(.orange)
                }
                if !report.unsupported.isEmpty {
                    Text("仅保存在本机、尚未同步到 Pi：\(report.unsupported.joined(separator: "、"))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Text(
                    report.finishedAt.formatted(
                        date: .abbreviated,
                        time: .standard
                    )
                )
                .font(.caption2)
                .foregroundStyle(.tertiary)
            }

            if let errorMessage = piSync.errorMessage {
                Text(errorMessage)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(4)
            }

            Text("本机先保存，再显式同步到 Pi。Pi 较新、两端冲突或结果未知时不会自动覆盖；再次同步会先重新核对目录与摘要。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var touchInputSection: some View {
        Section("绘图与触控") {
            Picker("触屏双击", selection: $touchDoubleTapAction) {
                ForEach(ReaderTouchDoubleTapAction.allCases) { action in
                    Text(action.title).tag(action)
                }
            }
            .onChange(of: touchDoubleTapAction) { _, action in
                guard touchDoubleTapLoaded else { return }
                Task { await saveTouchDoubleTapAction(action) }
            }

            Text("这里设置的是手指在书页上的连续双击；Apple Pencil 笔身双击仍由下方 Pencil 设置控制。")
                .font(.caption)
                .foregroundStyle(.secondary)

            if let touchDoubleTapError {
                Text(touchDoubleTapError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        }
    }

    private func loadTouchDoubleTapAction() async {
        do {
            let rawValue = try await reader.nativeTouchDoubleTapAction()
            touchDoubleTapAction = ReaderTouchDoubleTapAction(
                rawValue: rawValue
            ) ?? .eraser
            touchDoubleTapError = nil
        } catch {
            touchDoubleTapError = "无法读取触屏双击设置：\(error.localizedDescription)"
        }
        touchDoubleTapLoaded = true
    }

    private func saveTouchDoubleTapAction(
        _ action: ReaderTouchDoubleTapAction
    ) async {
        do {
            try await reader.setNativeTouchDoubleTapAction(action.rawValue)
            touchDoubleTapError = nil
        } catch {
            touchDoubleTapError = "无法保存触屏双击设置：\(error.localizedDescription)"
        }
    }

    @ViewBuilder
    private var currentPageSection: some View {
        Section("当前阅读位置") {
            if let snapshot = coordinator.snapshot {
                LabeledContent(
                    "标题",
                    value: snapshot.title.isEmpty ? "未命名页面" : snapshot.title
                )
                LabeledContent("位置", value: snapshot.pageSummary)
                if !snapshot.selection.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("当前选区")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text(snapshot.selection)
                            .lineLimit(6)
                            .textSelection(.enabled)
                    }
                } else if !snapshot.visibleText.isEmpty {
                    Text(snapshot.visibleText)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .lineLimit(5)
                        .textSelection(.enabled)
                } else {
                    Text("当前视口没有可读文字，可尝试设备端识别。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            } else if coordinator.activity == .refreshing {
                HStack {
                    ProgressView()
                    Text("正在读取当前页面…")
                }
            } else {
                Text("尚未取得当前页面信息")
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private var nativeActionsSection: some View {
        Section("设备端能力") {
            Button {
                Task {
                    await coordinator.recognizeCurrentViewport(using: reader)
                }
            } label: {
                Label("识别当前视口普通文字", systemImage: "text.viewfinder")
            }
            .disabled(coordinator.activity.isBusy)

            Button {
                Task {
                    await coordinator.recognizeBookFormulas(using: reader)
                }
            } label: {
                Label("批量识别本书公式", systemImage: "function")
            }
            .disabled(
                coordinator.activity.isBusy
                    || reader.supportsNativeFormulaRecognition(
                        file: coordinator.snapshot?.file ?? ""
                    ) == false
            )

            Text("普通文字在设备上识别；公式框当前复用现有 DocLayout 模型预处理，再由 AI 批量转成 LaTeX。后续 Core ML 版会逐页读取已下载书籍的本地页图，不会重复下载整本书。")
                .font(.caption)
                .foregroundStyle(.secondary)

            if let status = coordinator.formulaStatus {
                HStack {
                    Text(status.summary)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("刷新") {
                        Task {
                            await coordinator.refreshFormulaStatus(using: reader)
                        }
                    }
                    .buttonStyle(.borderless)
                }
            }

            Button {
                Task {
                    await coordinator.prepareAnnotation(using: reader)
                    if coordinator.viewportImage != nil,
                       coordinator.errorMessage == nil {
                        presentsAnnotation = true
                    }
                }
            } label: {
                Label("用 Pencil 标注当前视口", systemImage: "pencil.tip.crop.circle")
            }
            .disabled(coordinator.activity.isBusy)

            Button {
                translationText = coordinator.preferredText
                presentsTranslation = true
            } label: {
                Label("系统翻译", systemImage: "translate")
            }
            .disabled(coordinator.preferredText.isEmpty)

            if coordinator.activity.isBusy {
                HStack {
                    ProgressView()
                    Text(activityTitle)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    @ViewBuilder
    private var recognitionSection: some View {
        if !coordinator.recognizedText.isEmpty {
            Section("设备端识别结果") {
                Text(coordinator.recognizedText)
                    .textSelection(.enabled)
                Button("复制识别结果") {
                    UIPasteboard.general.string = coordinator.recognizedText
                }
            }
        }
    }

    @ViewBuilder
    private var quickNotesSection: some View {
        if !coordinator.quickNotes.isEmpty {
            Section("快捷指令速记") {
                ForEach(coordinator.quickNotes.prefix(8)) { note in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(note.text)
                            .lineLimit(3)
                            .textSelection(.enabled)
                        Text(note.createdAt.formatted(
                            date: .abbreviated,
                            time: .shortened
                        ))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var localNotesSection: some View {
        Section("本地 Obsidian 笔记") {
            Toggle(
                "写入 iPad 本地 Vault",
                isOn: Binding(
                    get: { localNotes.isEnabled },
                    set: { localNotes.setEnabled($0) }
                )
            )
            .disabled(!localNotes.isConfigured)

            Button {
                presentsLocalNotesFolderPicker = true
            } label: {
                Label(
                    localNotes.isConfigured ? "更换 Vault 文件夹" : "选择 Vault 文件夹",
                    systemImage: "folder.badge.plus"
                )
            }

            if localNotes.isConfigured {
                LabeledContent(
                    "当前文件夹",
                    value: localNotes.folderName.isEmpty
                        ? "已授权文件夹"
                        : localNotes.folderName
                )
                Button("移除本地文件夹授权", role: .destructive) {
                    localNotes.clearFolder()
                }
            }

            if !localNotes.notes.isEmpty {
                ForEach(localNotes.notes.prefix(8)) { note in
                    Button {
                        selectedLocalNote = note
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(note.title)
                                .font(.body.weight(.medium))
                                .foregroundStyle(.primary)
                                .lineLimit(2)
                            Text(note.preview)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(3)
                            Text(note.fileName)
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                                .lineLimit(1)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .buttonStyle(.plain)
                }
            }

            Text("关闭时继续使用原有 Pi 笔记线路；开启后，App 与 Safari 扩展共享创建、查看和读取能力。扩展创建的内容会先安全排队，再由 App 自动写入所选 Vault。请选择 Vault 根目录，以便 Obsidian 链接能够正确打开。")
                .font(.caption)
                .foregroundStyle(.secondary)

            if let notice = localNotes.notice {
                Label(notice, systemImage: "checkmark.circle.fill")
                    .font(.footnote)
                    .foregroundStyle(.green)
            }
            if let error = localNotes.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
    }

    @ViewBuilder
    private var statusSection: some View {
        if let notice = coordinator.notice {
            Section {
                Label(notice, systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .onTapGesture { coordinator.dismissNotice() }
            }
        }
        if let error = coordinator.errorMessage {
            Section {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
    }

    private var activityTitle: String {
        switch coordinator.activity {
        case .idle:
            return ""
        case .refreshing:
            return "正在读取页面"
        case .recognizing:
            return "正在设备端识别"
        case .recognizingFormulas:
            return "正在连接公式批处理"
        case .preparingAnnotation:
            return "正在截取视口"
        case .savingAnnotation:
            return "正在保存标注"
        }
    }

    @MainActor
    private func performInitialActionIfNeeded() async {
        guard !performedInitialAction else { return }
        performedInitialAction = true
        switch initialAction {
        case .scanCurrentPage:
            await coordinator.recognizeCurrentViewport(using: reader)
        case .annotateCurrentPage:
            await coordinator.prepareAnnotation(using: reader)
            if coordinator.viewportImage != nil,
               coordinator.errorMessage == nil {
                presentsAnnotation = true
            }
        case .openReader, .openNativeTools, .none:
            break
        }
    }
}

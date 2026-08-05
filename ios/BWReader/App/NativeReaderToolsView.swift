import SwiftUI
import UIKit

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
            formulaStatus = try await reader.startNativeFormulaRecognition(
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

struct NativeReaderToolsView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var coordinator = NativeReaderToolsCoordinator()
    @State private var presentsAnnotation = false
    @State private var presentsTranslation = false
    @State private var translationText = ""
    @State private var performedInitialAction = false

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
                quickNotesSection
                NativePencilSettingsSection()
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
                await performInitialActionIfNeeded()
            }
            .sheet(isPresented: $presentsTranslation) {
                NativeTranslationToolView(initialText: translationText)
            }
            .fullScreenCover(isPresented: $presentsAnnotation) {
                if let image = coordinator.viewportImage {
                    NativePencilAnnotationEditor(image: image) { annotated in
                        Task { await coordinator.saveAnnotation(annotated) }
                    }
                }
            }
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
                    || coordinator.snapshot?.file.lowercased().hasSuffix(".pdf") != true
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

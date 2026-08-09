import SwiftUI
import UniformTypeIdentifiers

private enum ReaderLibrarySource: String, CaseIterable, Identifiable {
    case local
    case pi
    case all

    var id: String { rawValue }

    var title: String {
        switch self {
        case .local: return "本机"
        case .pi: return "Pi 书库"
        case .all: return "全部"
        }
    }
}

@MainActor
private enum ReaderLibraryRefreshClock {
    static var lastAutomaticRemoteRefreshAt: Date?
}

struct ReaderLocalLibraryView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var library = ReaderLocalLibraryManager.shared
    @StateObject private var remote: ReaderRemoteLibraryCoordinator
    @StateObject private var nativeOCR = NativeBookOCRManager.shared
    @StateObject private var piOCR = ReaderPiOCRCoordinator()
    @StateObject private var recognitionPreferences = ReaderTextRecognitionPreferences.shared
    @State private var selectedSource = ReaderLibrarySource.all
    @State private var presentsFolderPicker = false
    @State private var searchText = ""
    @State private var ocrActionBookID: String?
    @State private var ocrErrorMessage: String?
    @State private var expandedPreprocessingBookIDs = Set<String>()

    let reader: ReaderWebViewModel
    let startupNotice: String?

    init(
        reader: ReaderWebViewModel,
        startupNotice: String? = nil,
        remote: ReaderRemoteLibraryCoordinator? = nil
    ) {
        self.reader = reader
        self.startupNotice = startupNotice
        _remote = StateObject(
            wrappedValue: remote ?? ReaderRemoteLibraryCoordinator()
        )
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Picker("书库来源", selection: $selectedSource) {
                        ForEach(ReaderLibrarySource.allCases) { source in
                            Text(source.title).tag(source)
                        }
                    }
                    .pickerStyle(.segmented)
                }

                if let startupNotice, !startupNotice.isEmpty {
                    Section {
                        Label(
                            startupNotice,
                            systemImage: "exclamationmark.triangle"
                        )
                        .font(.footnote)
                        .foregroundStyle(.orange)
                    }
                }

                localFolderSection

                switch selectedSource {
                case .local:
                    localBooksSection(library.books)
                case .pi:
                    remoteBooksSection(remote.books)
                case .all:
                    localBooksSection(library.books)
                    remoteBooksSection(remoteOnlyBooks)
                }

                statusSections
            }
            .navigationTitle("书库")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(
                text: $searchText,
                placement: .navigationBarDrawer(displayMode: .always),
                prompt: "搜索书名或路径"
            )
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        Task { await refresh(force: true) }
                    } label: {
                        Label("刷新书库", systemImage: "arrow.clockwise")
                    }
                    .disabled(library.isScanning || remote.isRefreshing)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }
                }
            }
            .fileImporter(
                isPresented: $presentsFolderPicker,
                allowedContentTypes: [.folder]
            ) { result in
                switch result {
                case .success(let url):
                    Task {
                        await library.configureFolder(url)
                        await refreshRemote()
                    }
                case .failure(let error):
                    if (error as? CocoaError)?.code != .userCancelled {
                        library.reportError(error)
                    }
                }
            }
            .task { await refreshIfStale() }
            .onChange(of: nativeOCR.lastUpdate) { _, update in
                guard update?.status.state == .failed else { return }
                ocrErrorMessage = update?.status.message ?? "本机预处理失败"
            }
            .alert(
                "预处理失败",
                isPresented: Binding(
                    get: { ocrErrorMessage != nil },
                    set: { if !$0 { ocrErrorMessage = nil } }
                )
            ) {
                Button("好", role: .cancel) { ocrErrorMessage = nil }
            } message: {
                Text(ocrErrorMessage ?? "未知错误")
            }
        }
    }

    @ViewBuilder
    private var localFolderSection: some View {
        Section("本机文件夹") {
            Button {
                presentsFolderPicker = true
            } label: {
                Label(
                    library.isConfigured ? "更换书籍文件夹" : "选择书籍文件夹",
                    systemImage: "folder.badge.plus"
                )
            }
            .disabled(library.isScanning || remote.activeBookID != nil)

            if library.isConfigured {
                LabeledContent(
                    "当前文件夹",
                    value: library.folderName.isEmpty ? "已授权文件夹" : library.folderName
                )
                LabeledContent("本机", value: "\(library.books.count) 本")
                LabeledContent("Pi", value: "\(remote.books.count) 本")
            }

            Text("下载只写入你选择的文件夹，不覆盖同名文件；上传按内容去重，也不会删除本机或 Pi 上的书。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }

        if library.isScanning || remote.isRefreshing {
            Section {
                HStack {
                    ProgressView()
                    Text(library.isScanning ? "正在扫描本机书籍…" : "正在读取 Pi 书库…")
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    @ViewBuilder
    private func localBooksSection(_ sourceBooks: [ReaderLocalBookRecord]) -> some View {
        let books = filteredLocal(sourceBooks)
        if !library.isConfigured {
            placeholder(
                title: "尚未选择本机书籍文件夹",
                detail: "选择“文件”App 中的一个文件夹后，可以扫描、下载和上传 PDF、EPUB。"
            )
        } else if books.isEmpty, selectedSource != .all {
            placeholder(
                title: searchText.isEmpty ? "本机还没有 PDF 或 EPUB" : "没有匹配的本机书籍",
                detail: "把书放入当前文件夹后点刷新，或从 Pi 书库下载。"
            )
        } else if !books.isEmpty {
            Section(selectedSource == .all ? "本机与已同步" : "本机书籍") {
                ForEach(books) { book in
                    localBookRow(book)
                }
            }
        }
    }

    @ViewBuilder
    private func remoteBooksSection(_ sourceBooks: [ReaderRemoteBook]) -> some View {
        let books = filteredRemote(sourceBooks)
        if remote.errorMessage == nil,
           remote.books.isEmpty,
           !remote.isRefreshing,
           selectedSource == .pi {
            placeholder(
                title: "Pi 书库为空",
                detail: "上传一本本机书后，它会出现在这里。"
            )
        } else if books.isEmpty, !searchText.isEmpty, selectedSource == .pi {
            placeholder(title: "没有匹配的 Pi 书籍", detail: "尝试更换搜索词。")
        } else if !books.isEmpty {
            Section(selectedSource == .all ? "仅在 Pi" : "Pi 书库") {
                ForEach(books) { book in
                    remoteBookRow(book)
                }
            }
        }
    }

    @ViewBuilder
    private func localBookRow(_ book: ReaderLocalBookRecord) -> some View {
        let remoteBook = remote.remoteBook(for: book)
        let syncState = remote.syncState(for: book)
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                bookIcon(kind: book.format.rawValue)
                VStack(alignment: .leading, spacing: 3) {
                    Text(book.title)
                        .font(.body.weight(.medium))
                        .lineLimit(2)
                    Text(book.relativePath)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    Text("\(book.format.title) · \(byteCount(book.byteCount)) · \(syncState.title)")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    if book.format == .pdf {
                        preprocessingToggleButton(
                            bookID: "local:\(book.id)",
                            summary: "本机 \(nativeStateTitle(nativeStatus(for: book).state)) · "
                                + "Pi \(piSummary(remoteBook))"
                        )
                    }
                }
                Spacer(minLength: 4)
                actionProgress(
                    ids: [book.id] + (remoteBook.map { [$0.bookId] } ?? [])
                )
            }

            HStack {
                Spacer()
                if let remoteBook {
                    if syncState == .localNewer || syncState == .conflict {
                        Button("上传此版本") {
                            Task { await upload(book) }
                        }
                        .buttonStyle(.bordered)
                        .disabled(remote.activeBookID != nil)
                    }
                    if syncState == .piNewer || syncState == .conflict {
                        Button("下载 Pi 新版") {
                            Task { await download(remoteBook) }
                        }
                        .buttonStyle(.bordered)
                        .disabled(remote.activeBookID != nil)
                    }
                } else {
                    Button("上传到 Pi") {
                        Task { await upload(book) }
                    }
                    .buttonStyle(.bordered)
                    .disabled(remote.activeBookID != nil)
                }
                Button("打开") {
                    Task { await openLocal(book) }
                }
                .buttonStyle(.borderedProminent)
            }

            if expandedPreprocessingBookIDs.contains("local:\(book.id)") {
                preprocessingPanel(localBook: book, remoteBook: remoteBook)
                    .task(id: piStatusTaskIdentity(
                        remoteBook: remoteBook,
                        localBook: book,
                        previewsLegacyResults: expandedPreprocessingBookIDs.contains(
                            "local:\(book.id)"
                        )
                    )) {
                        guard let remoteBook else { return }
                        await refreshPiStatus(
                            remoteBook,
                            localBook: book,
                            previewsLegacyResults: expandedPreprocessingBookIDs.contains(
                                "local:\(book.id)"
                            )
                        )
                    }
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func remoteBookRow(_ book: ReaderRemoteBook) -> some View {
        let localID = remote.localBookID(for: book)
        let localBook = localID.flatMap { localID in
            library.books.first(where: { $0.id == localID })
        }
        let syncState = remote.syncState(for: book)
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                bookIcon(kind: book.kind)
                VStack(alignment: .leading, spacing: 3) {
                    Text(book.name)
                        .font(.body.weight(.medium))
                        .lineLimit(2)
                    Text(book.rel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                    Text("\(book.kind.uppercased()) · \(byteCount(book.size)) · \(syncState.title)")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    if book.kind.lowercased() == "pdf" {
                        preprocessingToggleButton(
                            bookID: "remote:\(book.bookId)",
                            summary: "Pi \(piSummary(book))"
                        )
                    }
                }
                Spacer(minLength: 4)
                actionProgress(
                    ids: [book.bookId] + (localID.map { [$0] } ?? [])
                )
            }

            HStack {
                Spacer()
                if let localID,
                   let localBook = library.books.first(where: { $0.id == localID }),
                   syncState == .synced {
                    Button("打开本机版本") {
                        Task { await openLocal(localBook) }
                    }
                    .buttonStyle(.borderedProminent)
                } else {
                    Button(localID == nil ? "下载并打开" : "下载此版本并打开") {
                        Task { await downloadAndOpen(book) }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!library.isConfigured || remote.activeBookID != nil)
                }
            }

            if expandedPreprocessingBookIDs.contains("remote:\(book.bookId)") {
                preprocessingPanel(remoteBook: book, localBook: localBook)
                    .task(id: piStatusTaskIdentity(
                        remoteBook: book,
                        localBook: localBook,
                        previewsLegacyResults: expandedPreprocessingBookIDs.contains(
                            "remote:\(book.bookId)"
                        )
                    )) {
                        await refreshPiStatus(
                            book,
                            localBook: localBook,
                            previewsLegacyResults: expandedPreprocessingBookIDs.contains(
                                "remote:\(book.bookId)"
                            )
                        )
                    }
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func preprocessingPanel(
        localBook: ReaderLocalBookRecord,
        remoteBook: ReaderRemoteBook?
    ) -> some View {
        if localBook.format == .pdf {
            let status = nativeStatus(for: localBook)
            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Label("本机预处理", systemImage: "ipad.and.iphone")
                            .font(.caption.weight(.semibold))
                        Spacer()
                        Text(nativeStateTitle(status.state))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    if status.state != .idle {
                        nativeProgress(status)
                    }
                    nativeControls(book: localBook, status: status)

                    Divider()
                    piControls(
                        remoteBook: remoteBook,
                        localBook: localBook
                    )
                }
            } label: {
                Label("文字、分词与公式", systemImage: "text.viewfinder")
            }
        } else {
            Text("EPUB 使用其可重排文字层；整页图片型 EPUB 暂不支持这套 PDF 页图预处理。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func preprocessingPanel(
        remoteBook: ReaderRemoteBook,
        localBook: ReaderLocalBookRecord?
    ) -> some View {
        if remoteBook.kind.lowercased() == "pdf" {
            GroupBox {
                piControls(
                    remoteBook: remoteBook,
                    localBook: localBook
                )
            } label: {
                Label("文字、分词与公式", systemImage: "text.viewfinder")
            }
        } else {
            Text("EPUB 当前不支持 Pi 的 PDF 页图预处理；下载后仍按 EPUB 原文字层阅读。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func nativeProgress(_ status: NativeBookOCRBookStatus) -> some View {
        stageProgress("文字", status.textProgress)
        stageProgress("分词", status.wordProgress)
        stageProgress("公式", status.formulaProgress)
        if let currentPage = status.currentPage {
            Text("当前第 \(currentPage)/\(max(1, status.totalPages)) 页")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        if status.formulaPendingRegions > 0 || status.formulaFailedRegions > 0 {
            Text("公式区域：待处理 \(status.formulaPendingRegions)，失败 \(status.formulaFailedRegions)")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        if let message = status.message, !message.isEmpty {
            Text(message)
                .font(.caption2)
                .foregroundStyle(
                    status.state == .failed ? Color.red : Color.secondary
                )
        }
    }

    private func stageProgress(
        _ title: String,
        _ progress: NativeBookOCRStageProgress
    ) -> some View {
        HStack(spacing: 8) {
            Text(title)
                .font(.caption2)
                .frame(width: 32, alignment: .leading)
            ProgressView(value: stageFraction(
                total: progress.total,
                completed: progress.completed
            ))
            Text(stageProgressText(
                total: progress.total,
                completed: progress.completed,
                pending: progress.pending,
                failed: progress.failed,
                unavailable: progress.unavailable
            ))
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func nativeControls(
        book: ReaderLocalBookRecord,
        status: NativeBookOCRBookStatus
    ) -> some View {
        HStack {
            if status.state == .idle {
                Button("本机预处理") {
                    Task { await startNativeOCR(book, reportFailure: true) }
                }
            }
            if status.canPause {
                Button("暂停") { nativeOCR.pause(bookID: book.id) }
            }
            if status.canResume {
                Button("继续") {
                    Task { await resumeNativeOCR(book) }
                }
            }
            if status.state == .running || status.state == .paused {
                Button("取消", role: .destructive) {
                    nativeOCR.cancel(bookID: book.id)
                }
            }
            if status.canRetry {
                Button("重试失败页") {
                    Task { await retryNativeOCR(book) }
                }
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .disabled(ocrActionBookID != nil)
    }

    @ViewBuilder
    private func piControls(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?
    ) -> some View {
        HStack {
            Label("Pi 预处理", systemImage: "server.rack")
                .font(.caption.weight(.semibold))
            Spacer()
            if let remoteBook,
               piOCR.previewingBookID == remoteBook.bookId {
                HStack(spacing: 4) {
                    ProgressView().controlSize(.mini)
                    Text("正在检查现有结果")
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
            } else if let remoteBook,
               let job = piOCR.job(for: remoteBook),
               job.state != "idle" {
                Text(piStateTitle(job.state))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else if let remoteBook,
                      let adoption = piOCR.adoption(for: remoteBook),
                      adoption.available {
                Text("已有 Pi 结果，可采用")
                    .font(.caption2)
                    .foregroundStyle(.tint)
            } else {
                Text("未开始")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }

        if let remoteBook,
           let job = piOCR.job(for: remoteBook),
           job.state != "idle" {
            piProgress(job)
            HStack {
                if job.canPause {
                    Button("暂停") {
                        Task { await controlPi("pause", book: remoteBook, localBook: localBook) }
                    }
                }
                if job.canResume {
                    Button("继续") {
                        Task { await controlPi("resume", book: remoteBook, localBook: localBook) }
                    }
                }
                if job.canCancel {
                    Button("取消", role: .destructive) {
                        Task { await controlPi("cancel", book: remoteBook, localBook: localBook) }
                    }
                }
                if job.canRetry {
                    Button("重试") {
                        Task { await controlPi("retry", book: remoteBook, localBook: localBook) }
                    }
                }
                if job.resultAvailable, let localBook {
                    Button("重新导入结果") {
                        Task {
                            await importPiAttachments(
                                book: remoteBook,
                                localBook: localBook
                            )
                        }
                    }
                }
                if !job.isActive && !job.canResume && !job.canRetry {
                    piStartMenu(remoteBook: remoteBook, localBook: localBook)
                }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
        } else if let remoteBook,
                  let adoption = piOCR.adoption(for: remoteBook),
                  adoption.available {
            VStack(alignment: .leading, spacing: 4) {
                Text("已找到覆盖 \(adoption.totalPages) 页的旧 Pi 文字与分词结果。")
                if adoption.formula.state == "succeeded" {
                    Text("公式结果：已识别 \(adoption.formula.count) 处")
                } else {
                    Text("文字与分词可以先采用；公式仍待处理。")
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
            HStack {
                Button("采用现有 Pi 结果") {
                    Task {
                        await adoptExistingPiResult(
                            book: remoteBook,
                            localBook: localBook
                        )
                    }
                }
                piStartMenu(remoteBook: remoteBook, localBook: localBook)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(
                ocrActionBookID != nil
                    || piOCR.activeBookID != nil
                    || piOCR.previewingBookID != nil
            )
        } else {
            piStartMenu(remoteBook: remoteBook, localBook: localBook)
        }

        if let remoteBook,
           let errorMessage = piOCR.error(for: remoteBook),
           !errorMessage.isEmpty {
            Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                .font(.caption2)
                .foregroundStyle(.red)
                .fixedSize(horizontal: false, vertical: true)
            if piOCR.job(for: remoteBook)?.state == "idle" {
                Button("重试检查现有结果") {
                    Task {
                        await refreshPiStatus(
                            remoteBook,
                            localBook: localBook,
                            previewsLegacyResults: true
                        )
                    }
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
        }
    }

    private func piProgress(_ job: ReaderPiOCRJob) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            piStageProgress("文字", job.textProgress)
            piStageProgress("分词", job.wordProgress)
            piStageProgress("公式", job.formulaProgress)
            if let currentPage = job.currentPage {
                Text("当前第 \(currentPage)/\(max(1, job.totalPages)) 页")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            if !job.message.isEmpty {
                Text(job.message)
                    .font(.caption2)
                    .foregroundStyle(
                        job.state == "failed" ? Color.red : Color.secondary
                    )
            }
        }
    }

    private func piStageProgress(
        _ title: String,
        _ progress: ReaderPiOCRStageProgress
    ) -> some View {
        HStack(spacing: 8) {
            Text(title)
                .font(.caption2)
                .frame(width: 32, alignment: .leading)
            ProgressView(value: progress.fractionCompleted)
            Text(stageProgressText(
                total: progress.total,
                completed: progress.completed,
                pending: progress.pending,
                failed: progress.failed,
                unavailable: progress.unavailable
            ))
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
        }
    }

    private func piStartMenu(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?
    ) -> some View {
        Menu("Pi 预处理") {
            Button("通用 PDF（Vision）") {
                Task {
                    await startPiOCR(
                        remoteBook: remoteBook,
                        localBook: localBook,
                        engine: "vision"
                    )
                }
            }
            Button("漫画文字（Manga OCR）") {
                Task {
                    await startPiOCR(
                        remoteBook: remoteBook,
                        localBook: localBook,
                        engine: "manga"
                    )
                }
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .disabled(
            ocrActionBookID != nil
                || piOCR.activeBookID != nil
                || piOCR.previewingBookID != nil
        )
    }

    @ViewBuilder
    private func actionProgress(ids: [String]) -> some View {
        let activeIDs = [
            remote.activeBookID,
            ocrActionBookID,
            piOCR.activeBookID,
            piOCR.previewingBookID,
        ].compactMap { $0 }
        if activeIDs.contains(where: { ids.contains($0) }) {
            ProgressView().controlSize(.small)
        }
    }

    private func bookIcon(kind: String) -> some View {
        Image(systemName: kind.lowercased() == "pdf" ? "doc.richtext" : "books.vertical")
            .foregroundStyle(.tint)
            .frame(width: 24)
    }

    @ViewBuilder
    private var statusSections: some View {
        if let notice = library.notice {
            Section {
                Label(notice, systemImage: "checkmark.circle.fill")
                    .font(.footnote)
                    .foregroundStyle(.green)
                    .onTapGesture { library.dismissMessages() }
            }
        }
        if let notice = remote.notice {
            Section {
                Label(notice, systemImage: "checkmark.circle.fill")
                    .font(.footnote)
                    .foregroundStyle(.green)
                    .onTapGesture { remote.dismissMessages() }
            }
        }
        if let notice = piOCR.notice {
            Section {
                Label(notice, systemImage: "checkmark.circle.fill")
                    .font(.footnote)
                    .foregroundStyle(.green)
                    .onTapGesture { piOCR.dismissMessages() }
            }
        }
        if let error = library.errorMessage {
            errorSection(error)
        }
        if let error = remote.errorMessage {
            errorSection(error)
        }
        if let error = ocrErrorMessage {
            errorSection(error)
        }
    }

    @ViewBuilder
    private func errorSection(_ error: String) -> some View {
        Section {
            Label(error, systemImage: "exclamationmark.triangle.fill")
                .font(.footnote)
                .foregroundStyle(.red)
                .textSelection(.enabled)
        }
    }

    @ViewBuilder
    private func placeholder(title: String, detail: String) -> some View {
        Section {
            ContentUnavailableView(
                title,
                systemImage: "books.vertical",
                description: Text(detail)
            )
        }
    }

    private var remoteOnlyBooks: [ReaderRemoteBook] {
        remote.books.filter { remote.localBookID(for: $0) == nil }
    }

    private func filteredLocal(
        _ books: [ReaderLocalBookRecord]
    ) -> [ReaderLocalBookRecord] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return books }
        return books.filter {
            $0.title.localizedCaseInsensitiveContains(query)
                || $0.relativePath.localizedCaseInsensitiveContains(query)
        }
    }

    private func filteredRemote(
        _ books: [ReaderRemoteBook]
    ) -> [ReaderRemoteBook] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return books }
        return books.filter {
            $0.name.localizedCaseInsensitiveContains(query)
                || $0.rel.localizedCaseInsensitiveContains(query)
        }
    }

    @MainActor
    private func refreshIfStale() async {
        let now = Date()
        let localTTL: TimeInterval = 15 * 60
        let remoteTTL: TimeInterval = 5 * 60
        if library.isConfigured,
           library.lastScannedAt.map({
             now.timeIntervalSince($0) >= localTTL
           }) ?? true {
            await library.rescan()
        }
        if ReaderLibraryRefreshClock.lastAutomaticRemoteRefreshAt.map({
            now.timeIntervalSince($0) >= remoteTTL
        }) ?? true {
            await refreshRemote()
        }
    }

    @MainActor
    private func refresh(force: Bool) async {
        guard force else {
            await refreshIfStale()
            return
        }
        if library.isConfigured {
            await library.rescan()
        }
        await refreshRemote()
    }

    @MainActor
    private func refreshRemote() async {
        let cookies = await reader.remoteLibraryCookies()
        await remote.refresh(cookies: cookies, localLibrary: library)
        ReaderLibraryRefreshClock.lastAutomaticRemoteRefreshAt = Date()
    }

    private func refreshPiStatus(
        _ book: ReaderRemoteBook,
        localBook: ReaderLocalBookRecord?,
        previewsLegacyResults: Bool = false
    ) async {
        guard book.kind.lowercased() == "pdf" else { return }
        let cookies = await reader.remoteLibraryCookies()
        let localIdentity = await matchingLocalIdentity(
            for: book,
            localBook: localBook
        )
        await piOCR.refresh(
            book: book,
            cookies: cookies,
            localBookID: localIdentity?.bookID,
            localContentSHA256: localIdentity?.contentSHA256,
            previewsLegacyResults: previewsLegacyResults
        )
    }

    private func upload(_ book: ReaderLocalBookRecord) async {
        let cookies = await reader.remoteLibraryCookies()
        _ = await remote.upload(
            book,
            localLibrary: library,
            cookies: cookies
        )
    }

    @discardableResult
    private func download(_ book: ReaderRemoteBook) async -> ReaderLocalBookRecord? {
        let cookies = await reader.remoteLibraryCookies()
        guard let downloaded = await remote.download(
            book,
            localLibrary: library,
            cookies: cookies
        ) else { return nil }
        Task { @MainActor in
            await finishDownloadedBook(downloaded, remoteBook: book, cookies: cookies)
        }
        return downloaded
    }

    private func downloadAndOpen(_ book: ReaderRemoteBook) async {
        guard let localBook = await download(book) else { return }
        // The post-download task owns attachment import and automatic Apple
        // preprocessing; do not race a second automatic start from openLocal.
        await openLocal(localBook, allowAutomaticPreprocessing: false)
    }

    private func openLocal(
        _ book: ReaderLocalBookRecord,
        allowAutomaticPreprocessing: Bool = true
    ) async {
        if await reader.openLocalBook(book, library: library) {
            if allowAutomaticPreprocessing {
                scheduleAutomaticNativeOCR(for: book)
            }
            dismiss()
        }
    }

    private func finishDownloadedBook(
        _ localBook: ReaderLocalBookRecord,
        remoteBook: ReaderRemoteBook,
        cookies: [HTTPCookie]
    ) async {
        // Immutable OCR results and mutable account-scoped state are separate
        // attachments. Run both after the verified original lands locally;
        // either may fail without cancelling the other or blocking the book.
        let derivedTask = Task { @MainActor in
            do {
                let digest = try await library.ensureContentSHA256(
                    for: localBook
                )
                guard digest.caseInsensitiveCompare(
                    remoteBook.contentSha256
                ) == .orderedSame else {
                    throw ReaderPiOCRError.localContentMismatch
                }
                _ = await piOCR.importAvailableAttachments(
                    book: remoteBook,
                    localBookID: localBook.id,
                    localContentSHA256: digest,
                    cookies: cookies
                )
            } catch {
                ocrErrorMessage = "书籍已下载，但 Pi 预处理附件未导入：\(error.localizedDescription)"
            }
        }
        let userStateTask = Task { @MainActor in
            await remote.fetchAndStageUserState(
                for: remoteBook,
                localBook: localBook,
                cookies: cookies
            )
        }
        _ = await (derivedTask.value, userStateTask.value)
        await startAutomaticNativeOCRIfNeeded(localBook)
    }

    private func scheduleAutomaticNativeOCR(for book: ReaderLocalBookRecord) {
        Task { @MainActor in
            await startAutomaticNativeOCRIfNeeded(book)
        }
    }

    private func startAutomaticNativeOCRIfNeeded(
        _ book: ReaderLocalBookRecord
    ) async {
        guard recognitionPreferences.isEnabled,
              recognitionPreferences.automaticLocalProcessingEnabled,
              book.format == .pdf else { return }
        do {
            let digest = try await library.ensureContentSHA256(for: book)
            await nativeOCR.waitUntilReady()
            guard nativeOCR.status(
                for: book.id,
                expectedContentSHA256: digest
            ).state == .idle else { return }
            await startNativeOCR(book, reportFailure: true)
        } catch {
            ocrErrorMessage = error.localizedDescription
        }
    }

    private func startNativeOCR(
        _ book: ReaderLocalBookRecord,
        reportFailure: Bool
    ) async {
        guard recognitionPreferences.isEnabled else {
            if reportFailure { ocrErrorMessage = "请先在设置中启用书籍文字识别" }
            return
        }
        guard book.format == .pdf else {
            if reportFailure { ocrErrorMessage = "EPUB 暂不支持 PDF 页图预处理" }
            return
        }
        guard ocrActionBookID == nil else { return }
        ocrActionBookID = book.id
        defer { if ocrActionBookID == book.id { ocrActionBookID = nil } }
        do {
            let digest = try await library.ensureContentSHA256(for: book)
            let current = library.books.first(where: { $0.id == book.id }) ?? book
            let access = try library.makeOpenAccess(for: current)
            try await nativeOCR.startLocal(book: access, contentSHA256: digest)
        } catch {
            if reportFailure { ocrErrorMessage = error.localizedDescription }
        }
    }

    private func resumeNativeOCR(_ book: ReaderLocalBookRecord) async {
        await performNativeContinuation(book) { access, digest in
            try await nativeOCR.resume(book: access, contentSHA256: digest)
        }
    }

    private func retryNativeOCR(_ book: ReaderLocalBookRecord) async {
        await performNativeContinuation(book) { access, digest in
            try await nativeOCR.retry(book: access, contentSHA256: digest)
        }
    }

    private func performNativeContinuation(
        _ book: ReaderLocalBookRecord,
        operation: @escaping (ReaderLocalBookAccess, String) async throws -> Void
    ) async {
        guard ocrActionBookID == nil else { return }
        ocrActionBookID = book.id
        defer { if ocrActionBookID == book.id { ocrActionBookID = nil } }
        do {
            let digest = try await library.ensureContentSHA256(for: book)
            let current = library.books.first(where: { $0.id == book.id }) ?? book
            let access = try library.makeOpenAccess(for: current)
            try await operation(access, digest)
        } catch {
            ocrErrorMessage = error.localizedDescription
        }
    }

    private func startPiOCR(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?,
        engine: String
    ) async {
        guard recognitionPreferences.isEnabled else {
            ocrErrorMessage = "请先在设置中启用书籍文字识别"
            return
        }
        guard ocrActionBookID == nil else { return }
        let actionID = localBook?.id ?? remoteBook?.bookId ?? "pi-ocr"
        ocrActionBookID = actionID
        defer { if ocrActionBookID == actionID { ocrActionBookID = nil } }
        let cookies = await reader.remoteLibraryCookies()
        var target = remoteBook
        var localDigest: String?
        var currentLocalBook = localBook
        if let localBook {
            do {
                let digest = try await library.ensureContentSHA256(for: localBook)
                localDigest = digest
                let current = library.books.first(where: {
                    $0.id == localBook.id
                }) ?? localBook
                currentLocalBook = current
                let targetMatchesLocal = target.map {
                    $0.contentSha256.caseInsensitiveCompare(digest)
                        == .orderedSame
                } ?? false
                if !targetMatchesLocal {
                    // Pi processing always targets the exact local bytes the
                    // user selected. Local-newer/conflict therefore uploads
                    // first instead of processing an older linked version.
                    target = await remote.upload(
                        current,
                        localLibrary: library,
                        cookies: cookies
                    )
                }
            } catch {
                ocrErrorMessage = error.localizedDescription
                return
            }
        }
        guard let target else {
            ocrErrorMessage = remote.errorMessage
                ?? "无法把这本书准备到 Pi 书库，请稍后重试"
            return
        }
        if let localDigest,
           target.contentSha256.caseInsensitiveCompare(localDigest)
            != .orderedSame {
            ocrErrorMessage = ReaderPiOCRError.localContentMismatch
                .localizedDescription
            return
        }
        await piOCR.start(
            book: target,
            engine: engine,
            cookies: cookies,
            localBookID: currentLocalBook?.id,
            localContentSHA256: localDigest
        )
        if piOCR.errorBookID == target.bookId,
           let errorMessage = piOCR.errorMessage {
            ocrErrorMessage = errorMessage
        }
    }

    private func controlPi(
        _ action: String,
        book: ReaderRemoteBook,
        localBook: ReaderLocalBookRecord?
    ) async {
        let cookies = await reader.remoteLibraryCookies()
        let localIdentity = await matchingLocalIdentity(
            for: book,
            localBook: localBook
        )
        await piOCR.control(
            action,
            book: book,
            cookies: cookies,
            localBookID: localIdentity?.bookID,
            localContentSHA256: localIdentity?.contentSHA256
        )
        presentPiErrorIfNeeded(for: book)
    }

    private func adoptExistingPiResult(
        book: ReaderRemoteBook,
        localBook: ReaderLocalBookRecord?
    ) async {
        guard ocrActionBookID == nil else { return }
        let actionID = localBook?.id ?? book.bookId
        ocrActionBookID = actionID
        defer { if ocrActionBookID == actionID { ocrActionBookID = nil } }
        let cookies = await reader.remoteLibraryCookies()
        let localIdentity = await matchingLocalIdentity(
            for: book,
            localBook: localBook
        )
        await piOCR.adoptExisting(
            book: book,
            cookies: cookies,
            localBookID: localIdentity?.bookID,
            localContentSHA256: localIdentity?.contentSHA256
        )
        presentPiErrorIfNeeded(for: book)
    }

    private func importPiAttachments(
        book: ReaderRemoteBook,
        localBook: ReaderLocalBookRecord
    ) async {
        do {
            let digest = try await library.ensureContentSHA256(for: localBook)
            guard digest.caseInsensitiveCompare(book.contentSha256)
                == .orderedSame else {
                throw ReaderPiOCRError.localContentMismatch
            }
            let cookies = await reader.remoteLibraryCookies()
            let imported = await piOCR.importAvailableAttachments(
                book: book,
                localBookID: localBook.id,
                localContentSHA256: digest,
                cookies: cookies,
                requiresManifest: true,
                reportsExplicitFailure: true
            )
            if !imported {
                presentPiErrorIfNeeded(for: book)
            }
        } catch {
            ocrErrorMessage = error.localizedDescription
        }
    }

    private func presentPiErrorIfNeeded(for book: ReaderRemoteBook) {
        guard piOCR.errorBookID == book.bookId,
              let message = piOCR.errorMessage,
              !message.isEmpty else { return }
        ocrErrorMessage = message
    }

    private func matchingLocalIdentity(
        for remoteBook: ReaderRemoteBook,
        localBook: ReaderLocalBookRecord?
    ) async -> (bookID: String, contentSHA256: String)? {
        guard let localBook else { return nil }
        do {
            let digest = try await library.ensureContentSHA256(for: localBook)
            guard digest.caseInsensitiveCompare(remoteBook.contentSha256)
                == .orderedSame else { return nil }
            return (localBook.id, digest)
        } catch {
            return nil
        }
    }

    private func piStatusTaskIdentity(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?,
        previewsLegacyResults: Bool
    ) -> String {
        [
            remoteBook?.version ?? "none",
            localBook?.id ?? "none",
            localBook?.contentSha256 ?? "unknown",
            String(localBook?.byteCount ?? -1),
            String(localBook?.modifiedAt?.timeIntervalSince1970 ?? -1),
            previewsLegacyResults ? "preview" : "status",
        ].joined(separator: ":")
    }

    private func nativeStatus(
        for book: ReaderLocalBookRecord
    ) -> NativeBookOCRBookStatus {
        guard let digest = book.contentSha256,
              digest.range(
                of: #"^[0-9a-fA-F]{64}$"#,
                options: .regularExpression
              ) != nil else {
            return .idle(bookID: book.id)
        }
        return nativeOCR.status(
            for: book.id,
            expectedContentSHA256: digest
        )
    }

    private func stageFraction(
        total: Int,
        completed: Int
    ) -> Double {
        guard total > 0 else { return 0 }
        return min(1, max(0, Double(completed) / Double(total)))
    }

    private func stageProgressText(
        total: Int,
        completed: Int,
        pending: Int,
        failed: Int,
        unavailable: Int
    ) -> String {
        var parts = ["完成 \(completed)/\(total)"]
        if pending > 0 { parts.append("待处理 \(pending)") }
        if failed > 0 { parts.append("失败 \(failed)") }
        if unavailable > 0 { parts.append("不可用 \(unavailable)") }
        return parts.joined(separator: " · ")
    }

    private func preprocessingToggleButton(
        bookID: String,
        summary: String
    ) -> some View {
        let isExpanded = expandedPreprocessingBookIDs.contains(bookID)
        return Button {
            if isExpanded {
                expandedPreprocessingBookIDs.remove(bookID)
            } else {
                expandedPreprocessingBookIDs.insert(bookID)
            }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                Text("处理：\(summary)")
            }
        }
        .buttonStyle(.plain)
        .font(.caption2)
        .foregroundStyle(.secondary)
        .accessibilityLabel(
            isExpanded ? "收起文字、分词与公式" : "展开文字、分词与公式"
        )
    }

    private func piSummary(_ book: ReaderRemoteBook?) -> String {
        guard let book else { return "未上传" }
        if piOCR.activeBookID == book.bookId { return "请求中" }
        if piOCR.previewingBookID == book.bookId { return "检查现有结果中" }
        if piOCR.error(for: book) != nil { return "失败" }
        if let job = piOCR.job(for: book), job.state != "idle" {
            return piStateTitle(job.state)
        }
        if piOCR.adoption(for: book)?.available == true {
            return "已有结果，可采用"
        }
        return "未开始"
    }

    private func nativeStateTitle(_ state: NativeBookOCRJobState) -> String {
        switch state {
        case .idle: return "未开始"
        case .running: return "处理中"
        case .paused: return "已暂停"
        case .completed: return "已完成"
        case .failed: return "部分失败"
        case .cancelled: return "已取消"
        }
    }

    private func piStateTitle(_ state: String) -> String {
        switch state {
        case "queued": return "排队中"
        case "running": return "处理中"
        case "pause-requested": return "正在暂停"
        case "paused": return "已暂停"
        case "cancel-requested": return "正在取消"
        case "cancelled": return "已取消"
        case "succeeded": return "已完成"
        case "failed": return "失败"
        default: return "未开始"
        }
    }

    private func byteCount(_ value: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: value, countStyle: .file)
    }
}

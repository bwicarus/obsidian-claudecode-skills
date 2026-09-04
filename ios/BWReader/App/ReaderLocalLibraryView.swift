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
        case .pi: return "服务器书库"
        case .all: return "全部"
        }
    }
}

@MainActor
private enum ReaderLibraryRefreshClock {
    static var lastAutomaticRemoteRefreshAt: Date?
}

/// 一次待确认的删除。删除是唯一会真的抹掉数据的动作，所以必须过二次确认，
/// 并且要能分辨"删的是不是当前生效的那一份"——两种情形的文案不一样。
private struct PendingReleaseDeletion: Identifiable {
    let book: ReaderRemoteBook
    let release: ReaderPiOCRRelease
    /// 服务器删完还要删本机导入的那份副本，所以要记住是哪一本本机书。
    let localBook: ReaderLocalBookRecord?

    var id: String { release.runId }
}

struct ReaderLocalLibraryView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var library = ReaderLocalLibraryManager.shared
    @StateObject private var remote: ReaderRemoteLibraryCoordinator
    @StateObject private var nativeOCR = NativeBookOCRManager.shared
    @ObservedObject private var piOCR: ReaderPiOCRCoordinator
    @StateObject private var recognitionPreferences = ReaderTextRecognitionPreferences.shared
    // The shelf opens on the App-owned library. Pi is an explicit backup and
    // processing source, so merely opening the shelf must not start a network
    // request or make local books wait for remote status.
    @State private var selectedSource = ReaderLibrarySource.local
    @State private var presentsFolderPicker = false
    @State private var searchText = ""
    @State private var ocrActionBookID: String?
    @State private var ocrErrorMessage: String?
    @State private var expandedPreprocessingBookIDs = Set<String>()
    // 服务器上历次预处理结果（用户 2026-08-18：「删除预处理的结果」
    // 「标记上日期用以区分，而不是覆盖」）。按 bookId 缓存，展开面板时才拉。
    @State private var releasesByBook: [String: ReaderPiOCRReleaseList] = [:]
    @State private var loadingReleasesBookID: String?
    // 失败必须与"确实没有结果"分开。2026-08-19 我把 catch 写成静默留空，
    // 于是用户看到「0 份 · 还没有预处理结果」，而 Pi 上明明有 3 份 ——
    // 一个请求失败被伪装成了一个事实陈述。
    @State private var releaseErrorByBook: [String: String] = [:]
    @State private var pendingReleaseDeletion: PendingReleaseDeletion?

    let reader: ReaderWebViewModel
    let startupNotice: String?

    init(
        reader: ReaderWebViewModel,
        startupNotice: String? = nil,
        remote: ReaderRemoteLibraryCoordinator? = nil,
        piOCR: ReaderPiOCRCoordinator = .shared
    ) {
        self.reader = reader
        self.startupNotice = startupNotice
        _piOCR = ObservedObject(wrappedValue: piOCR)
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

                // 远端来源=Windows 服务器书库(2026-09-02 用户:Pi 退出这条线路)。
                // Pi 书库那套 remoteBooksSection/remoteBookRow 不再挂进页面;Pi OCR
                // (预处理)仍经 remote 取 Pi 侧书记录,那是另一条线。
                switch selectedSource {
                case .local:
                    localBooksSection(library.books)
                case .pi:
                    serverBooksSection(serverOnlyBooks)
                case .all:
                    localBooksSection(library.books)
                    serverBooksSection(serverOnlyBooks)
                }

                statusSections
            }
            .navigationTitle("书库")
            // ⚠ 备份闸拦住时**必须说为什么**。这条规矩最常见的失败是
            // 「服务器没开」,而那跟「这本书有问题」该做的事完全不同 ——
            // 只拦不说的话,用户看到的是"点了没反应"。
            .alert(
                "还不能打开",
                isPresented: Binding(
                    get: { backupNotice != nil },
                    set: { if !$0 { backupNotice = nil } })
            ) {
                Button("知道了", role: .cancel) { backupNotice = nil }
            } message: {
                Text((backupNotice ?? "")
                     + "\n\n规矩：书要先传到\(ReaderServer.displayName)才能打开，"
                     + "这样任何一本能用的书，两边都有。")
            }
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
                    }
                case .failure(let error):
                    if (error as? CocoaError)?.code != .userCancelled {
                        library.reportError(error)
                    }
                }
            }
            .task { await refreshIfStale() }
            .onChange(of: selectedSource) { _, source in
                guard source != .local else { return }
                Task { await refreshRemoteIfStale() }
            }
            .onChange(of: nativeOCR.lastUpdate) { _, update in
                guard update?.status.state == .failed else { return }
                reportPanelError(update?.status.message ?? "本机预处理失败")
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
            .confirmationDialog(
                "删除这份预处理结果？",
                isPresented: Binding(
                    get: { pendingReleaseDeletion != nil },
                    set: { if !$0 { pendingReleaseDeletion = nil } }
                ),
                titleVisibility: .visible,
                presenting: pendingReleaseDeletion
            ) { pending in
                Button("删除", role: .destructive) {
                    let target = pending
                    pendingReleaseDeletion = nil
                    Task { await confirmReleaseDeletion(target) }
                }
                Button("取消", role: .cancel) { pendingReleaseDeletion = nil }
            } message: { pending in
                // 删当前生效的那份后果不同 —— 分开说，别让用户事后才发现。
                Text(
                    pending.release.isActive
                        ? "「\(pending.release.displayTitle)」是当前生效的结果。删除后这本书在服务器上将没有生效的预处理结果，需要重新预处理或改用其它结果。原书不会被删除。"
                        : "将删除「\(pending.release.displayTitle)」。当前生效的结果不受影响，原书也不会被删除。"
                )
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
                LabeledContent("我的服务器", value: "\(remote.books.count) 本")
            }

            Text("下载只写入你选择的文件夹，不覆盖同名文件；上传按内容去重，也不会删除本机或服务器上的书。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }

        if library.isScanning || remote.isRefreshing {
            Section {
                HStack {
                    ProgressView()
                    Text(library.isScanning ? "正在扫描本机书籍…" : "正在读取服务器书库…")
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
                detail: "把书放入当前文件夹后点刷新，或从服务器书库下载。"
            )
        } else if !books.isEmpty {
            Section(selectedSource == .all ? "本机与已同步" : "本机书籍") {
                ForEach(books) { book in
                    localBookRow(book)
                }
            }
        }
    }

    /// 服务器(Windows)书库里本机没有的书。判据与备份闸同一口径:sha 相同,或同名同字节数。
    private var serverOnlyBooks: [ReaderServerLibrary.Book] {
        let locals = library.books
        let books = backupGate.serverBooks.filter { sb in
            !locals.contains { lb in
                (lb.contentSha256 == sb.sha256)
                    || (Int(lb.byteCount) == sb.bytes
                        && (lb.relativePath as NSString).lastPathComponent == sb.name)
            }
        }
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return books }
        return books.filter { $0.name.localizedCaseInsensitiveContains(query) }
    }

    @State private var serverDownloading: [String: Double] = [:]
    @State private var serverDownloadFailures: [String: String] = [:]

    private func serverStateTitle(_ book: ReaderLocalBookRecord) -> String {
        switch backupGate.status(of: book) {
        case .backed: return "本机 + \(ReaderServer.displayName)"
        case .notBacked: return "仅本机（打开时自动传）"
        case .unknown: return "\(ReaderServer.displayName)状态未知"
        case .ruleNotReady: return "\(ReaderServer.displayName)无书库"
        }
    }

    @ViewBuilder
    private func serverBooksSection(_ books: [ReaderServerLibrary.Book]) -> some View {
        if backupGate.serverHashes == nil, let why = backupGate.lastError, selectedSource == .pi {
            placeholder(title: "问不到\(ReaderServer.displayName)书库", detail: why)
        } else if backupGate.serverHashes == nil, selectedSource == .pi {
            placeholder(title: "还没问过\(ReaderServer.displayName)书库", detail: "下拉刷新一次。")
        } else if books.isEmpty, selectedSource == .pi {
            placeholder(
                title: searchText.isEmpty ? "\(ReaderServer.displayName)上没有本机缺的书" : "没有匹配的服务器书籍",
                detail: "本机每一本打开时都会自动传上去；这里只列服务器有、本机没有的。"
            )
        } else if !books.isEmpty {
            Section(selectedSource == .all ? "仅在\(ReaderServer.displayName)" : "\(ReaderServer.displayName)书库") {
                ForEach(books) { book in
                    serverBookRow(book)
                }
            }
        }
    }

    @ViewBuilder
    private func serverBookRow(_ book: ReaderServerLibrary.Book) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 10) {
                bookIcon(kind: (book.name as NSString).pathExtension.lowercased())
                VStack(alignment: .leading, spacing: 3) {
                    Text(book.name)
                        .font(.body.weight(.medium))
                        .lineLimit(2)
                    Text("\((book.name as NSString).pathExtension.uppercased()) · \(byteCount(Int64(book.bytes))) · 仅在\(ReaderServer.displayName)")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    if let fraction = serverDownloading[book.id] {
                        ProgressView(value: fraction)
                            .progressViewStyle(.linear)
                        Text("正在从\(ReaderServer.displayName)下载 \(Int((fraction * 100).rounded()))% · 下完自动打开")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    } else if let failure = serverDownloadFailures[book.id] {
                        Text("下载没成功：\(failure)（再点一次重试）")
                            .font(.caption2)
                            .foregroundStyle(.red)
                    }
                }
                Spacer(minLength: 4)
            }
            HStack {
                Spacer()
                Button("下载并打开") {
                    Task { await downloadFromServerAndOpen(book) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!library.isConfigured || serverDownloading[book.id] != nil)
            }
        }
        .padding(.vertical, 2)
    }

    private func downloadFromServerAndOpen(_ book: ReaderServerLibrary.Book) async {
        serverDownloadFailures.removeValue(forKey: book.id)
        serverDownloading[book.id] = 0
        defer { serverDownloading.removeValue(forKey: book.id) }
        do {
            let folderAccess = try library.makeFolderAccess()
            defer { withExtendedLifetime(folderAccess) {} }
            let bookID = book.id
            let savedName = try await ReaderServerLibrary.download(
                book, destinationDirectory: folderAccess.url
            ) { fraction in
                self.serverDownloading[bookID] = fraction
            }
            await library.rescan()
            guard let record = library.books.first(where: {
                ($0.relativePath as NSString).lastPathComponent == savedName
            }) else {
                serverDownloadFailures[book.id] = "文件已下到本机文件夹，但重新扫描后没找到它（\(savedName)）"
                return
            }
            await openLocal(record, allowAutomaticPreprocessing: false)
        } catch {
            serverDownloadFailures[book.id] = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
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
                title: "服务器书库为空",
                detail: "上传一本本机书后，它会出现在这里。"
            )
        } else if books.isEmpty, !searchText.isEmpty, selectedSource == .pi {
            placeholder(title: "没有匹配的服务器书籍", detail: "尝试更换搜索词。")
        } else if !books.isEmpty {
            Section(selectedSource == .all ? "仅在服务器" : "服务器书库") {
                ForEach(books) { book in
                    remoteBookRow(book)
                }
            }
        }
    }

    @ViewBuilder
    private func localBookRow(_ book: ReaderLocalBookRecord) -> some View {
        let remoteBook = remote.remoteBook(for: book)   // 仅供 Pi OCR 预处理面板
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
                    Text("\(book.format.title) · \(byteCount(book.byteCount)) · \(serverStateTitle(book))")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    if backupGate.uploading.contains(book.id) {
                        let fraction = backupGate.uploadProgress[book.id] ?? 0
                        ProgressView(value: fraction)
                            .progressViewStyle(.linear)
                        Text("正在传到\(ReaderServer.displayName) "
                             + "\(Int((fraction * 100).rounded()))% · 传完自动打开")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    } else if let failure = backupGate.uploadFailures[book.id] {
                        Text("上传没成功：\(failure)（再点「打开」重试）")
                            .font(.caption2)
                            .foregroundStyle(.red)
                    } else if openBusy.contains(book.id) {
                        // 「打开」按下后到真正开书之间的每一步都要有状态：问服务器清单
                        // 可能要几秒(Tailscale),这段静默曾被读成"点了没反应"。
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.mini)
                            Text(openStage[book.id] ?? "正在打开…")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    } else if let failure = openFailures[book.id] {
                        Text("没能打开：\(failure)")
                            .font(.caption2)
                            .foregroundStyle(.red)
                    }
                    if book.format == .pdf {
                        preprocessingToggleButton(
                            bookID: "local:\(book.id)",
                            summary: "本机 \(nativeStateTitle(nativeStatus(for: book).state)) · "
                                + "服务器 \(piSummary(remoteBook))"
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
                // 传服务器不再是一个按钮:「打开」自动完成(备份闸),状态写在上一行。
                Button("打开") {
                    Task { await openLocal(book) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(backupGate.uploading.contains(book.id) || openBusy.contains(book.id))
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
                        // 面板开着就持续刷新(用户 2026-09-04:此前要关掉重开才更新):
                        // 任务活动中每 3s,空闲每 20s;顺带刷本机文字层状态,选择器不再显示过期的选中项。
                        await pollPreprocessingPanel(
                            remoteBook: remoteBook,
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
                            summary: "服务器 \(piSummary(book))"
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
                        await pollPreprocessingPanel(
                            remoteBook: book,
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
                    textLayerPicker(book: localBook)

                    Divider()
                    releaseHistory(remoteBook: remoteBook, localBook: localBook)

                    Divider()
                    piControls(
                        remoteBook: remoteBook,
                        localBook: localBook
                    )
                }
            } label: {
                Label("文字、分词与公式", systemImage: "text.viewfinder")
            }
            .task(id: "text-layers:\(localBook.id):\(localBook.contentSha256 ?? "pending")") {
                await refreshTextLayers(localBook)
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
                // 从「Pi 书库」那一栏展开时同样要能看到历次结果并删除 ——
                // 上一版只挂在本机书那个重载上，这边整块缺失。
                releaseHistory(remoteBook: remoteBook, localBook: localBook)

                Divider()
                piControls(
                    remoteBook: remoteBook,
                    localBook: localBook
                )
            } label: {
                Label("文字、分词与公式", systemImage: "text.viewfinder")
            }
        } else {
            Text("EPUB 当前不支持服务器的 PDF 页图预处理；下载后仍按 EPUB 原文字层阅读。")
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
    private func textLayerPicker(book: ReaderLocalBookRecord) -> some View {
        HStack(spacing: 8) {
            Label("当前使用", systemImage: "text.page")
                .font(.caption.weight(.semibold))
            Spacer()
        }
        // 列表而不是下拉(用户 2026-09-04):预处理/导入完成的结果自动出现在这里并被选中;
        // 点一行切换;导入进来的层可删(PDF 自带层与兼容旧结果没有可删的目录)。
        if let state = nativeOCR.layerState(
            for: book.id,
            expectedContentSHA256: book.contentSha256
        ) {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(state.available) { metadata in
                    let selected = metadata.layer == state.selected
                    HStack(spacing: 8) {
                        Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(selected ? Color.accentColor : Color.secondary)
                        Text(textLayerOptionTitle(metadata))
                            .font(.caption)
                            .foregroundStyle(selected ? Color.primary : Color.secondary)
                        Spacer()
                        if [.appleVision, .pi, .pc].contains(metadata.layer) {
                            Button {
                                Task { await deleteTextLayer(metadata.layer, for: book) }
                            } label: {
                                Image(systemName: "trash")
                                    .font(.caption)
                            }
                            .buttonStyle(.borderless)
                            .foregroundStyle(.red)
                        }
                    }
                    .contentShape(Rectangle())
                    .onTapGesture {
                        guard !selected else { return }
                        Task { await selectTextLayer(metadata.layer, for: book) }
                    }
                }
            }
            .disabled(ocrActionBookID != nil)
        } else {
            ProgressView().controlSize(.mini)
        }
        Text("预处理或导入完成的结果会自动出现在这里并成为当前文字层；切换后正在阅读的页面立即重载。")
            .font(.caption2)
            .foregroundStyle(.secondary)
    }

    private func textLayerOptionTitle(
        _ metadata: NativeBookOCRLayerMetadata
    ) -> String {
        if metadata.layer == .embedded { return metadata.layer.title }
        // 带上导入日期，让同一个格子被不同批次结果覆盖时也能分辨
        // （updatedAt 一直就在元数据里，只是过去没显示出来）。
        var title = metadata.layer.title
        if metadata.updatedAt > Date(timeIntervalSince1970: 0) {
            let formatter = DateFormatter()
            formatter.dateFormat = "MM-dd"
            title += " · " + formatter.string(from: metadata.updatedAt)
        }
        return title + " · \(metadata.pageCount) 页"
    }

    /// 面板里的错误既显示也进服务器客户端日志(用户 2026-09-04:「预处理附件导入失败」只在面板里一句话,查不到)。
    private func reportPanelError(_ message: String) {
        ocrErrorMessage = message
        Task {
            let cookies = await reader.remoteLibraryCookies()
            ReaderPiOCRClient.shared.postClientLog("预处理面板: " + message, cookies: cookies)
        }
    }

    private func refreshTextLayers(_ book: ReaderLocalBookRecord) async {
        do {
            let digest = try await library.ensureContentSHA256(for: book)
            _ = try await nativeOCR.refreshLayerState(
                bookID: book.id,
                expectedContentSHA256: digest
            )
        } catch {
            reportPanelError("文字层状态读取失败：\(error.localizedDescription)")
        }
    }

    private func deleteTextLayer(
        _ layer: NativeBookOCRLayerID,
        for book: ReaderLocalBookRecord
    ) async {
        guard ocrActionBookID == nil else { return }
        ocrActionBookID = book.id
        defer { if ocrActionBookID == book.id { ocrActionBookID = nil } }
        do {
            let digest = try await library.ensureContentSHA256(for: book)
            _ = try await nativeOCR.deleteTextLayer(
                bookID: book.id,
                expectedContentSHA256: digest,
                layer: layer
            )
        } catch {
            reportPanelError("删除文字层失败：\(error.localizedDescription)")
        }
    }

    private func selectTextLayer(
        _ layer: NativeBookOCRLayerID,
        for book: ReaderLocalBookRecord
    ) async {
        guard ocrActionBookID == nil else { return }
        ocrActionBookID = book.id
        defer { if ocrActionBookID == book.id { ocrActionBookID = nil } }
        do {
            let digest = try await library.ensureContentSHA256(for: book)
            _ = try await nativeOCR.selectTextLayer(
                bookID: book.id,
                expectedContentSHA256: digest,
                layer: layer
            )
        } catch {
            reportPanelError("文字层切换失败：\(error.localizedDescription)")
        }
    }

    /// 服务器上的历次预处理结果。
    ///
    /// 用户指出选择的地方本来就有（「当前使用」那个选择器）——但那个选择器是
    /// **每种引擎一个格子**（embedded/legacy/vision/pi/pc 五个固定枚举），
    /// 装不下"同一种引擎跑过好几次"。所以历次结果列在这里，各带日期；
    /// 选中某一份会让服务器切过去，再重新导入到本机的对应格子。
    @ViewBuilder
    private func releaseHistory(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label("服务器上的结果", systemImage: "clock.arrow.circlepath")
                    .font(.caption.weight(.semibold))
                Spacer()
                if let remoteBook, loadingReleasesBookID == remoteBook.bookId {
                    ProgressView().controlSize(.mini)
                } else if let remoteBook,
                          releaseErrorByBook[remoteBook.bookId] == nil,
                          let listing = releasesByBook[remoteBook.bookId] {
                    Text("\(listing.releases.count) 份")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            if remoteBook == nil {
                // 整段藏起来等于用户看不出这个功能存在 —— 说清为什么是空的。
                Text("这本书还没有上传到服务器，服务器上没有预处理结果。")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        if let remoteBook {
            let listing = releasesByBook[remoteBook.bookId]
            VStack(alignment: .leading, spacing: 6) {
            if let failure = releaseErrorByBook[remoteBook.bookId] {
                VStack(alignment: .leading, spacing: 2) {
                    Text("读取失败：\(failure)")
                    Button("重试") {
                        Task {
                            releaseErrorByBook[remoteBook.bookId] = nil
                            await loadReleases(for: remoteBook)
                        }
                    }
                    .font(.caption2)
                }
                .font(.caption2)
                .foregroundStyle(.orange)
            } else if let listing, listing.releases.isEmpty {
                Text("还没有预处理结果。")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else if let listing {
                ForEach(listing.releases) { release in
                    HStack(spacing: 6) {
                        Text(release.displayTitle)
                            .font(.caption2)
                        if release.isActive {
                            Text("当前生效")
                                .font(.caption2)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1)
                                .background(Color.accentColor.opacity(0.18))
                                .clipShape(Capsule())
                        }
                        Spacer()
                        Menu {
                            if !release.isActive {
                                // 切到这一份后自动导入到本机并成为当前文字层(2026-09-04 去掉了单独的导入按钮)
                                Button("使用这份") {
                                    Task {
                                        await activateRelease(
                                            release,
                                            book: remoteBook,
                                            localBook: localBook
                                        )
                                    }
                                }
                            }
                            Button("删除", role: .destructive) {
                                pendingReleaseDeletion = PendingReleaseDeletion(
                                    book: remoteBook,
                                    release: release,
                                    localBook: localBook
                                )
                            }
                        } label: {
                            Image(systemName: "ellipsis.circle")
                                .font(.caption)
                        }
                        .disabled(ocrActionBookID != nil)
                    }
                }
                Text("这里的日期是服务器上产出的时间；「当前使用」里的日期是导入到本机的时间，两者不一定相同。")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            }
            // 展开面板才拉：打开书架不该为每本书发一次网络请求。
            .task(id: remoteBook.bookId) {
                if releasesByBook[remoteBook.bookId] == nil {
                    await loadReleases(for: remoteBook)
                }
            }
        }
    }

    private func loadReleases(for book: ReaderRemoteBook) async {
        guard loadingReleasesBookID == nil else { return }
        loadingReleasesBookID = book.bookId
        defer {
            if loadingReleasesBookID == book.bookId { loadingReleasesBookID = nil }
        }
        let cookies = await reader.remoteLibraryCookies()
        do {
            let listing = try await piOCR.releases(book: book, cookies: cookies)
            releasesByBook[book.bookId] = listing
            releaseErrorByBook[book.bookId] = nil
        } catch {
            // 不打断面板其余功能，但**必须说出来**：请求失败与"确实没有结果"
            // 长得一样的话，用户只会以为功能没做（他确实这么以为了）。
            releasesByBook[book.bookId] = nil
            releaseErrorByBook[book.bookId] = error.localizedDescription
        }
    }

    private func activateRelease(
        _ release: ReaderPiOCRRelease,
        book: ReaderRemoteBook,
        localBook: ReaderLocalBookRecord?
    ) async {
        guard ocrActionBookID == nil else { return }
        ocrActionBookID = book.bookId
        defer { if ocrActionBookID == book.bookId { ocrActionBookID = nil } }
        let cookies = await reader.remoteLibraryCookies()
        do {
            let listing = try await piOCR.activateRelease(
                book: book,
                runId: release.runId,
                cookies: cookies
            )
            releasesByBook[book.bookId] = listing
        } catch {
            reportPanelError("切换预处理结果失败：\(error.localizedDescription)")
            return
        }
        // 服务器换了当前生效的那一份，本机还留着上一次导入的旧层 ——
        // 必须重新导入，否则 iPad 上看到的仍然是旧结果。
        if let localBook {
            await importPiAttachments(book: book, localBook: localBook)
        }
    }

    /// 把某一份结果拉到本机（不改服务器上哪一份生效）。
    ///
    /// 强制重导：`hasImportedRevision` 是按 revision 去重的，而 revision 是内容
    /// 寻址的 —— 用户手点这一项时想要的就是"不管你觉得重不重复，拉一次"。
    private func importRelease(
        _ release: ReaderPiOCRRelease,
        book: ReaderRemoteBook,
        localBook: ReaderLocalBookRecord?
    ) async {
        guard let localBook, ocrActionBookID == nil else { return }
        ocrActionBookID = book.bookId
        defer { if ocrActionBookID == book.bookId { ocrActionBookID = nil } }
        if !release.isActive {
            // 附件下载走的是"当前生效那一份"，所以要先把它切过去。
            let cookies = await reader.remoteLibraryCookies()
            do {
                let listing = try await piOCR.activateRelease(
                    book: book,
                    runId: release.runId,
                    cookies: cookies
                )
                releasesByBook[book.bookId] = listing
            } catch {
                reportPanelError("切换预处理结果失败：\(error.localizedDescription)")
                return
            }
        }
        await importPiAttachments(book: book, localBook: localBook)
        await refreshTextLayers(localBook)
    }

    private func confirmReleaseDeletion(_ pending: PendingReleaseDeletion) async {
        guard ocrActionBookID == nil else { return }
        let book = pending.book
        ocrActionBookID = book.bookId
        defer { if ocrActionBookID == book.bookId { ocrActionBookID = nil } }
        let cookies = await reader.remoteLibraryCookies()
        do {
            let listing = try await piOCR.deleteRelease(
                book: book,
                runId: pending.release.runId,
                // 删当前生效的那份：用户已经在对话框里确认过后果了。
                allowDeactivate: pending.release.isActive,
                cookies: cookies
            )
            releasesByBook[book.bookId] = listing
        } catch {
            reportPanelError("删除预处理结果失败：\(error.localizedDescription)")
            return
        }
        // 服务器上删掉了，本机导入的那份副本还在，而且「当前使用」里仍然列着它 ——
        // 用户 2026-08-19 实测。按 **revision** 认领：本机层的元数据里记着它是从
        // 哪一版导入的，与被删的那一份对上就一并删掉。
        //
        // 只删对得上的那一层：本机可能还导入过别的版本，或者根本没导入过这一份。
        guard let localBook = pending.localBook,
              let digest = localBook.contentSha256 else { return }
        guard let state = nativeOCR.layerState(
            for: localBook.id,
            expectedContentSHA256: digest
        ) else { return }
        let orphaned = state.available.filter {
            $0.revision == pending.release.revision
        }
        for metadata in orphaned {
            do {
                _ = try await nativeOCR.deleteTextLayer(
                    bookID: localBook.id,
                    expectedContentSHA256: digest,
                    layer: metadata.layer
                )
            } catch {
                // 服务器那边已经删了，本机没删掉要说出来 —— 否则用户会看到
                // 「当前使用」里还有一项，却不知道为什么。
                reportPanelError("服务器上已删除，但本机这一份没能删掉：\(error.localizedDescription)")
            }
        }
    }

    @ViewBuilder
    private func piControls(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?
    ) -> some View {
        HStack {
            Label("服务器 / PC 预处理", systemImage: "server.rack")
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
                Text("\(executorTitle(job.executor)) · \(piStateTitle(job.state))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else if let remoteBook,
                      let adoption = piOCR.adoption(for: remoteBook),
                      adoption.available {
                Text("已有服务器结果，可采用")
                    .font(.caption2)
                    .foregroundStyle(.tint)
            } else {
                Text("未开始")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }

        HStack {
            Label("此电脑 GPU", systemImage: "desktopcomputer")
                .font(.caption2)
            Spacer()
            if piOCR.refreshingExecutors {
                HStack(spacing: 4) {
                    ProgressView().controlSize(.mini)
                    Text("正在确认")
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
            } else {
                Text(pcExecutorTitle)
                    .font(.caption2)
                    .foregroundStyle(
                        pcExecutorIsOnline
                            ? Color.green : Color.secondary
                    )
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
                // 「重新导入结果」已删(2026-09-04):结果一出来轮询就自动导入并成为当前文字层。
                if !job.isActive && !job.canResume && !job.canRetry {
                    preprocessingStartMenus(
                        remoteBook: remoteBook,
                        localBook: localBook
                    )
                }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
        } else if let remoteBook,
                  let adoption = piOCR.adoption(for: remoteBook),
                  adoption.available {
            VStack(alignment: .leading, spacing: 4) {
                Text("已找到覆盖 \(adoption.totalPages) 页的旧服务器文字与分词结果。")
                if adoption.formula.state == "succeeded" {
                    Text("公式结果：已识别 \(adoption.formula.count) 处")
                } else {
                    Text("文字与分词可以先采用；公式仍待处理。")
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
            // 采用不再是一个按钮(2026-09-04):面板轮询发现本机没有这份结果就自动采用并导入。
            HStack(spacing: 6) {
                ProgressView().controlSize(.mini)
                Text(localBook == nil ? "这本书还没有本机副本，采用后无处导入" : "正在自动采用并导入到本机…")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            HStack {
                preprocessingStartMenus(
                    remoteBook: remoteBook,
                    localBook: localBook
                )
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(
                ocrActionBookID != nil
                    || piOCR.activeBookID != nil
                    || piOCR.previewingBookID != nil
            )
        } else {
            preprocessingStartMenus(
                remoteBook: remoteBook,
                localBook: localBook
            )
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

    private func preprocessingStartMenus(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?
    ) -> some View {
        HStack {
            piStartMenu(
                remoteBook: remoteBook,
                localBook: localBook,
                executor: "pi"
            )
            piStartMenu(
                remoteBook: remoteBook,
                localBook: localBook,
                executor: "pc"
            )
        }
    }

    private func piStartMenu(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?,
        executor: String
    ) -> some View {
        Menu(executor == "pc" ? "PC 预处理" : "服务器预处理") {
            Button("通用 PDF（Vision）") {
                Task {
                    await startPiOCR(
                        remoteBook: remoteBook,
                        localBook: localBook,
                        engine: "vision",
                        executor: executor
                    )
                }
            }
            Button("漫画文字（Manga OCR）") {
                Task {
                    await startPiOCR(
                        remoteBook: remoteBook,
                        localBook: localBook,
                        engine: "manga",
                        executor: executor
                    )
                }
            }
            // 用户 2026-08-18：「而不是覆盖或者拒绝进行多次预处理」。
            // 上面两项仍然复用已发布的结果（省时省钱的正确默认）；只有从这里
            // 进去才会真的再跑一次，跑出来的是**新的一份**，旧的不动。
            Section("重新跑一份（不覆盖现有结果）") {
                Button("通用 PDF（Vision）") {
                    Task {
                        await startPiOCR(
                            remoteBook: remoteBook,
                            localBook: localBook,
                            engine: "vision",
                            executor: executor,
                            force: true
                        )
                    }
                }
                Button("漫画文字（Manga OCR）") {
                    Task {
                        await startPiOCR(
                            remoteBook: remoteBook,
                            localBook: localBook,
                            engine: "manga",
                            executor: executor,
                            force: true
                        )
                    }
                }
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .disabled(
            ocrActionBookID != nil
                || piOCR.activeBookID != nil
                || piOCR.previewingBookID != nil
                || (executor == "pc" && !pcExecutorAcceptingJobs)
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
        if library.isConfigured,
           library.lastScannedAt.map({
             now.timeIntervalSince($0) >= localTTL
           }) ?? true {
            await library.rescan()
        }
        if selectedSource != .local { await refreshRemoteIfStale() }
    }

    @MainActor
    private func refreshRemoteIfStale() async {
        let now = Date()
        let remoteTTL: TimeInterval = 5 * 60
        guard ReaderLibraryRefreshClock.lastAutomaticRemoteRefreshAt.map({
            now.timeIntervalSince($0) >= remoteTTL
        }) ?? true else { return }
        await refreshRemote()
    }

    @MainActor
    private func refresh(force: Bool) async {
        guard force else {
            await refreshIfStale()
            return
        }
        if selectedSource != .pi, library.isConfigured {
            await library.rescan()
        }
        if selectedSource != .local { await refreshRemote() }
    }

    @MainActor
    private func refreshRemote() async {
        // 书库远端=Windows 服务器(2026-09-02 Pi 退出这条线路)。Pi 仍刷一次,但只为
        // 预处理(OCR)面板提供 Pi 侧书记录,书库页本身不再展示 Pi 书。
        await backupGate.refresh()
        let cookies = await reader.remoteLibraryCookies()
        await remote.refresh(cookies: cookies, localLibrary: library)
        ReaderLibraryRefreshClock.lastAutomaticRemoteRefreshAt = Date()
    }

    /// 预处理面板展开期间的轮询。`.task(id:)` 随面板出现而起、收起而取消;
    /// 服务器任务活动中(排队/运行/暂停请求…)每 3 秒,空闲每 20 秒;每轮顺带刷本机文字层状态。
    private func pollPreprocessingPanel(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?,
        previewsLegacyResults: Bool
    ) async {
        while !Task.isCancelled {
            if let remoteBook {
                await refreshPiStatus(
                    remoteBook,
                    localBook: localBook,
                    previewsLegacyResults: previewsLegacyResults
                )
            }
            if let localBook { await refreshTextLayers(localBook) }
            await autoAdoptOrImportIfNeeded(remoteBook: remoteBook, localBook: localBook)
            var active = false
            if let remoteBook, let job = piOCR.job(for: remoteBook) {
                active = job.isActive || job.canResume || piOCR.activeBookID == remoteBook.bookId
            }
            let busy = active || ocrActionBookID != nil || piOCR.activeBookID != nil
            do {
                try await Task.sleep(for: .seconds(busy ? 3 : 20))
            } catch {
                return
            }
        }
    }

    /// 面板不再有「采用」「导入」按钮(2026-09-04):这里按状态自动做,每本书每个版本只做一次。
    /// - 服务器有旧结果、当前没有任务 → 自动采用(采用会顺带导入并成为当前文字层);
    /// - 任务已出结果、本机没导入这个版本 → 自动导入。
    private func autoAdoptOrImportIfNeeded(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?
    ) async {
        guard let remoteBook, let localBook,
              ocrActionBookID == nil, piOCR.activeBookID == nil,
              piOCR.previewingBookID == nil else { return }
        let job = piOCR.job(for: remoteBook)
        if let job, job.resultAvailable, !job.isActive, let revision = job.pageCharsRevision,
           let digest = localBook.contentSha256 {
            let key = "\(remoteBook.bookId):\(revision)"
            if !autoImportedKeys.contains(key) {
                let imported = (try? await nativeOCR.hasImportedRevision(
                    expectedContentSHA256: digest,
                    revision: revision
                )) ?? true
                if !imported {
                    autoImportedKeys.insert(key)
                    await importPiAttachments(book: remoteBook, localBook: localBook)
                    return
                }
            }
        }
        if (job == nil || job?.state == "idle"),
           let adoption = piOCR.adoption(for: remoteBook), adoption.available,
           !autoAdoptedBookIDs.contains(remoteBook.bookId) {
            autoAdoptedBookIDs.insert(remoteBook.bookId)
            await adoptExistingPiResult(book: remoteBook, localBook: localBook)
        }
    }

    private func refreshPiStatus(
        _ book: ReaderRemoteBook,
        localBook: ReaderLocalBookRecord?,
        previewsLegacyResults: Bool = false
    ) async {
        guard book.kind.lowercased() == "pdf" else { return }
        let cookies = await reader.remoteLibraryCookies()
        async let executorRefresh: Void = piOCR.refreshExecutors(cookies: cookies)
        let localIdentity = await matchingLocalIdentity(
            for: book,
            localBook: localBook
        )
        async let bookRefresh: Void = piOCR.refresh(
            book: book,
            cookies: cookies,
            localBookID: localIdentity?.bookID,
            localContentSHA256: localIdentity?.contentSHA256,
            previewsLegacyResults: previewsLegacyResults
        )
        _ = await (executorRefresh, bookRefresh)
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

    // 备份闸的提示 —— 只留给两种**真阻塞**(文件读不到 / 服务器没问到)。
    // 上传进度和上传失败不走这里:弹窗文案呈现时定格、改字不刷新、再赋值就再弹,
    // 09-02 用户看到的"关一次弹一次、百分比不动"就是它;那两样画在书那一行。
    @State private var backupNotice: String?
    @ObservedObject private var backupGate = ReaderBookBackupGate.shared
    /// 「打开」按下后的阶段与结果，画在书那一行。任何一条提前返回都必须在这里留字，
    /// 不许静默（silent-failure 规则）。
    @State private var openBusy: Set<String> = []
    /// 面板自动采用/自动导入的去重(每本书一次采用;每个结果版本一次导入)
    @State private var autoAdoptedBookIDs: Set<String> = []
    @State private var autoImportedKeys: Set<String> = []
    @State private var openStage: [String: String] = [:]
    @State private var openFailures: [String: String] = [:]

    private func openLocal(
        _ book: ReaderLocalBookRecord,
        allowAutomaticPreprocessing: Bool = true
    ) async {
        // ⚠ **规矩(用户 2026-08-28 拍板 A):本地的书必须先上传服务器才能用。**
        //
        // 落点在"打开"而不是"导入",因为这套没有导入这一步 ——
        // 书是靠扫描你选的那个文件夹自己出现的,唯一能真正拦住的地方就是这里。
        //
        // 它买到的是一个不变量:**任何一本能用的书,两边都有**。
        // 代价是服务器没开时新书打不开 —— 用户知道这个代价而仍然选了它,
        // 理由是另一条路(能读但标"未备份")会留下一个需要人注意的标记,
        // 而那种标记迟早被忽略,然后在重装时才发现。
        openBusy.insert(book.id)
        openFailures.removeValue(forKey: book.id)
        openStage[book.id] = "正在核对\(ReaderServer.displayName)书库…"
        defer {
            openBusy.remove(book.id)
            openStage.removeValue(forKey: book.id)
        }
        if !(await passesBackupGate(book)) {
            // 闸没放行：上传失败已由闸写在行内(uploadFailures)，其余两种真阻塞走弹窗；
            // 这里再兜一层，保证不存在"什么都没显示"的返回路径。
            if backupGate.uploadFailures[book.id] == nil, backupNotice == nil {
                openFailures[book.id] = backupGate.lastError ?? "备份闸没放行，原因未知"
            }
            return
        }
        openStage[book.id] = "正在打开…"
        if await reader.openLocalBook(book, library: library) {
            if allowAutomaticPreprocessing {
                scheduleAutomaticNativeOCR(for: book)
            }
            dismiss()
        } else {
            openFailures[book.id] = "本机 Reader 拒绝了这次打开，详情见书库底部的提示"
        }
    }

    /// 备份闸。通过才让开书。
    ///
    /// ⚠ **"还不知道"不等于"没备份"。** 服务器问不到时不能一律拦 ——
    /// 那会在服务器只是网络抖了一下的时候平白拦住人;也不能一律放 ——
    /// 那等于规矩不存在。所以这里如实把两者分开,并把原因显示出来。
    private func passesBackupGate(_ book: ReaderLocalBookRecord) async -> Bool {
        let gate = ReaderBookBackupGate.shared
        // (同日早些时候的「Pi 已同步则放行」已撤:用户拍板 Pi 退出书库线路,Pi 上的书
        //  已全部搬到 Windows,对照 Windows 哈希表即可,不再看 Pi。)
        if gate.serverHashes == nil { await gate.refresh() }
        switch gate.status(of: book) {
        case .backed:
            return true
        case .notBacked:
            // 自动传一次 —— 规矩是"必须先上传",不是"必须先手动上传"。
            // 能自动完成的事让用户点一次按钮,只是把规矩变成负担。
            guard let access = try? library.makeOpenAccess(for: book) else {
                backupNotice = "读不到这本书的文件，没法上传"
                return false
            }
            // 进度与失败都由闸发布、画在书那一行(见 localBookRow);这里只等结果。
            // 传成功 → 返回 true → 调用方接着开书,用户看到的是"进度条走完书就开了"。
            return await gate.ensureBacked(book, fileURL: access.url)
        case .ruleNotReady:
            // 服务器还没这个能力 —— **放行**。强制一条它那边还没落地的规则,
            // 结果是所有书都打不开而用户什么也做不了。
            // 但要留痕:沉默的放行会让人以为规矩已经在保护他了。
            return true
        case .unknown(let why):
            // ⚠ 不拦也不放,而是**说清楚现在处于哪种状态** ——
            // 直接拦会让"服务器暂时没答应"看起来像"这本书有问题"。
            backupNotice = why + "——先确认\(ReaderServer.displayName)开着"
            return false
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
                reportPanelError("书籍已下载，但服务器预处理附件未导入：\(error.localizedDescription)")
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
            reportPanelError(error.localizedDescription)
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
            reportPanelError(error.localizedDescription)
        }
    }

    private func startPiOCR(
        remoteBook: ReaderRemoteBook?,
        localBook: ReaderLocalBookRecord?,
        engine: String,
        executor: String,
        force: Bool = false
    ) async {
        guard recognitionPreferences.isEnabled else {
            reportPanelError("请先在设置中启用书籍文字识别")
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
                reportPanelError(error.localizedDescription)
                return
            }
        }
        guard let target else {
            reportPanelError(
                remote.errorMessage ?? "无法把这本书准备到服务器书库，请稍后重试"
            )
            return
        }
        if let localDigest,
           target.contentSha256.caseInsensitiveCompare(localDigest)
            != .orderedSame {
            reportPanelError(
                ReaderPiOCRError.localContentMismatch.localizedDescription
            )
            return
        }
        await piOCR.start(
            book: target,
            engine: engine,
            executor: executor,
            force: force,
            cookies: cookies,
            localBookID: currentLocalBook?.id,
            localContentSHA256: localDigest
        )
        if piOCR.errorBookID == target.bookId,
           let errorMessage = piOCR.errorMessage {
            reportPanelError(errorMessage)
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
                reportsExplicitFailure: true,
                forceReimport: true
            )
            if !imported {
                presentPiErrorIfNeeded(for: book)
            }
        } catch {
            reportPanelError(error.localizedDescription)
        }
    }

    private func presentPiErrorIfNeeded(for book: ReaderRemoteBook) {
        guard piOCR.errorBookID == book.bookId,
              let message = piOCR.errorMessage,
              !message.isEmpty else { return }
        reportPanelError(message)
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
            return "\(executorTitle(job.executor)) \(piStateTitle(job.state))"
        }
        if piOCR.adoption(for: book)?.available == true {
            return "已有结果，可采用"
        }
        return "未开始"
    }

    private var pcExecutorTitle: String {
        guard let status = piOCR.executorStatus("pc") else {
            return "状态未知"
        }
        if !pcExecutorIsOnline { return "离线" }
        if !status.acceptingJobs { return "在线 · 忙碌" }
        return "在线 · 可用"
    }

    private var pcExecutorIsOnline: Bool {
        guard let status = piOCR.executorStatus("pc"), status.online,
              let lastSeen = status.lastSeenAtEpochMs else { return false }
        let ageMilliseconds = Date().timeIntervalSince1970 * 1_000
            - Double(lastSeen)
        return ageMilliseconds >= 0 && ageMilliseconds <= 35_000
    }

    private var pcExecutorAcceptingJobs: Bool {
        pcExecutorIsOnline
            && piOCR.executorStatus("pc")?.acceptingJobs == true
    }

    private func executorTitle(_ executor: String?) -> String {
        executor == "pc" ? "PC" : "我的服务器"
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

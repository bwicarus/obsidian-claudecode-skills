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

struct ReaderLocalLibraryView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var library = ReaderLocalLibraryManager.shared
    @StateObject private var remote = ReaderRemoteLibraryCoordinator()
    @State private var selectedSource = ReaderLibrarySource.all
    @State private var presentsFolderPicker = false
    @State private var searchText = ""

    let reader: ReaderWebViewModel

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
                        Task { await refresh() }
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
            .task { await refresh() }
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
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func remoteBookRow(_ book: ReaderRemoteBook) -> some View {
        let localID = remote.localBookID(for: book)
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
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func actionProgress(ids: [String]) -> some View {
        if let activeBookID = remote.activeBookID,
           ids.contains(activeBookID) {
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
        if let error = library.errorMessage {
            errorSection(error)
        }
        if let error = remote.errorMessage {
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

    private func refresh() async {
        if library.isConfigured {
            await library.rescan()
        }
        await refreshRemote()
    }

    private func refreshRemote() async {
        let cookies = await reader.remoteLibraryCookies()
        await remote.refresh(cookies: cookies, localLibrary: library)
    }

    private func upload(_ book: ReaderLocalBookRecord) async {
        let cookies = await reader.remoteLibraryCookies()
        _ = await remote.upload(
            book,
            localLibrary: library,
            cookies: cookies
        )
    }

    private func download(_ book: ReaderRemoteBook) async {
        let cookies = await reader.remoteLibraryCookies()
        await remote.download(
            book,
            localLibrary: library,
            cookies: cookies
        )
    }

    private func downloadAndOpen(_ book: ReaderRemoteBook) async {
        await download(book)
        guard let localID = remote.localBookID(for: book),
              let localBook = library.books.first(where: { $0.id == localID }) else {
            return
        }
        await openLocal(localBook)
    }

    private func openLocal(_ book: ReaderLocalBookRecord) async {
        if await reader.openLocalBook(book, library: library) {
            dismiss()
        }
    }

    private func byteCount(_ value: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: value, countStyle: .file)
    }
}

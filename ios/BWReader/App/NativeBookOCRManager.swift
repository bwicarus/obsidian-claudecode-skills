import Combine
import Foundation

@MainActor
final class NativeBookOCRManager: ObservableObject {
    static let shared = NativeBookOCRManager()

    @Published private(set) var bookStatuses: [String: NativeBookOCRBookStatus] = [:]
    @Published private(set) var lastUpdate: NativeBookOCRUpdate?

    private let store: NativeBookOCRSidecarStore
    private let statusLoadTask: Task<[NativeBookOCRBookStatus], Never>
    private var tasks: [String: Task<Void, Never>] = [:]
    private var generations: [String: UUID] = [:]
    private var statusWriteTasks: [String: Task<Void, Never>] = [:]
    private var activeContentSHA256: [String: String] = [:]
    private var invalidatedContentSHA256: [String: String] = [:]
    private var didApplyLoadedStatuses = false

    init(store: NativeBookOCRSidecarStore = NativeBookOCRSidecarStore()) {
        self.store = store
        self.statusLoadTask = Task { [store] in
            (try? await store.loadStatuses()) ?? []
        }
        Task { [weak self] in
            await self?.waitUntilReady()
        }
    }

    /// Restored job records are observable history only. They never activate a
    /// localBookID: the current file identity must be supplied by this session.
    func waitUntilReady() async {
        let loaded = await statusLoadTask.value
        guard !didApplyLoadedStatuses else { return }
        didApplyLoadedStatuses = true
        for status in loaded where bookStatuses[status.bookID] == nil {
            bookStatuses[status.bookID] = status
        }
    }

    func activate(
        bookID: String,
        expectedContentSHA256: String
    ) throws {
        guard !bookID.isEmpty, Self.isSHA256(expectedContentSHA256) else {
            throw NativeBookOCRError.invalidContentSHA256
        }
        let normalized = expectedContentSHA256.lowercased()
        activeContentSHA256[bookID] = normalized
        invalidatedContentSHA256.removeValue(forKey: bookID)
    }

    func deactivate(bookID: String) {
        activeContentSHA256.removeValue(forKey: bookID)
    }

    private func invalidate(
        bookID: String,
        contentSHA256: String
    ) {
        activeContentSHA256.removeValue(forKey: bookID)
        invalidatedContentSHA256[bookID] = contentSHA256.lowercased()
    }

    func activatedContentSHA256(for bookID: String) -> String? {
        activeContentSHA256[bookID]
    }

    /// Legacy UI reads are still identity-bound: without explicit activation
    /// they return idle instead of exposing a restored status for another file.
    func status(for bookID: String) -> NativeBookOCRBookStatus {
        guard let expectedContentSHA256 = activeContentSHA256[bookID] else {
            return .idle(bookID: bookID)
        }
        return status(
            for: bookID,
            expectedContentSHA256: expectedContentSHA256
        )
    }

    func status(
        for bookID: String,
        expectedContentSHA256: String
    ) -> NativeBookOCRBookStatus {
        guard Self.isSHA256(expectedContentSHA256),
              invalidatedContentSHA256[bookID]
                != expectedContentSHA256.lowercased(),
              let status = bookStatuses[bookID],
              status.contentSHA256.caseInsensitiveCompare(
                expectedContentSHA256
              ) == .orderedSame else {
            return .idle(bookID: bookID)
        }
        return status
    }

    func readyStatus(
        for bookID: String,
        expectedContentSHA256: String
    ) async -> NativeBookOCRBookStatus {
        await waitUntilReady()
        return status(
            for: bookID,
            expectedContentSHA256: expectedContentSHA256
        )
    }

    func startLocal(
        book: ReaderLocalBookAccess,
        contentSHA256: String,
        configuration: NativeBookOCRConfiguration = NativeBookOCRConfiguration()
    ) async throws {
        await waitUntilReady()
        try await start(
            book: book,
            contentSHA256: contentSHA256,
            configuration: configuration,
            retryFailed: false
        )
    }

    func pause(bookID: String) {
        guard let expected = activeContentSHA256[bookID] else { return }
        let current = status(
            for: bookID,
            expectedContentSHA256: expected
        )
        guard current.canPause else { return }
        tasks[bookID]?.cancel()
        tasks[bookID] = nil
        generations[bookID] = UUID()
        publish(status: replacing(current, state: .paused,
            currentPage: nil,
            message: "已暂停；已完成页面和当前进度均已保存"))
    }

    func resume(
        book: ReaderLocalBookAccess,
        contentSHA256: String,
        configuration: NativeBookOCRConfiguration = NativeBookOCRConfiguration()
    ) async throws {
        await waitUntilReady()
        let current = status(
            for: book.record.id,
            expectedContentSHA256: contentSHA256
        )
        guard current.canResume else { return }
        try await start(
            book: book,
            contentSHA256: contentSHA256,
            configuration: configuration,
            retryFailed: false
        )
    }

    func cancel(bookID: String) {
        guard let expected = activeContentSHA256[bookID] else { return }
        let current = status(
            for: bookID,
            expectedContentSHA256: expected
        )
        guard current.state == .running || current.state == .paused else {
            return
        }
        tasks[bookID]?.cancel()
        tasks[bookID] = nil
        generations[bookID] = UUID()
        publish(status: replacing(current, state: .cancelled,
            currentPage: nil,
            message: "已取消；已完成页面仍保留，可稍后重试"))
    }

    func retry(
        book: ReaderLocalBookAccess,
        contentSHA256: String,
        configuration: NativeBookOCRConfiguration = NativeBookOCRConfiguration()
    ) async throws {
        await waitUntilReady()
        let current = status(
            for: book.record.id,
            expectedContentSHA256: contentSHA256
        )
        guard current.canRetry || current.state == .idle else { return }
        try await start(
            book: book,
            contentSHA256: contentSHA256,
            configuration: configuration,
            retryFailed: true
        )
    }

    func pageCharacters(
        bookID: String,
        expectedContentSHA256: String,
        page: Int
    ) async throws -> NativeBookOCRPageCharacters? {
        await waitUntilReady()
        let current = status(
            for: bookID,
            expectedContentSHA256: expectedContentSHA256
        )
        guard !current.contentSHA256.isEmpty else { return nil }
        return try await store.page(
            contentSHA256: expectedContentSHA256,
            page: page
        )
    }

    func pageStatus(
        bookID: String,
        expectedContentSHA256: String,
        page: Int
    ) async throws -> NativeBookOCRPageState? {
        let value = try await pageCharacters(
            bookID: bookID,
            expectedContentSHA256: expectedContentSHA256,
            page: page
        )
        return value?.status
    }

    func search(
        bookID: String,
        expectedContentSHA256: String,
        query: String,
        limit: Int = 50
    ) async throws -> NativeBookOCRSearchResult {
        await waitUntilReady()
        let current = status(
            for: bookID,
            expectedContentSHA256: expectedContentSHA256
        )
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !current.contentSHA256.isEmpty, !needle.isEmpty else {
            return NativeBookOCRSearchResult(
                matches: [],
                total: 0,
                pages: [],
                incomplete: current.totalPages > 0
            )
        }
        let cap = max(1, min(200, limit))
        let pages = try await store.pages(
            contentSHA256: expectedContentSHA256
        )
        var hits: [NativeBookOCRSearchHit] = []
        var total = 0
        var hitPages = Set<Int>()
        for page in pages where page.status == .ready {
            let match = Self.searchPage(
                page,
                query: String(needle.prefix(256)),
                remaining: cap - hits.count
            )
            total += match.total
            hits.append(contentsOf: match.hits)
            if match.total > 0 { hitPages.insert(page.page) }
            if hits.count >= cap { break }
        }
        let readyPages = pages.filter {
            $0.status == .ready || $0.status == .readyEmpty
        }.count
        return NativeBookOCRSearchResult(
            matches: hits,
            total: total,
            pages: hitPages.sorted(),
            incomplete: readyPages < current.totalPages
                || hits.count >= cap
        )
    }

    /// The caller downloads every immutable manifest entry first, then passes
    /// its bytes here. Import is explicit and never called by Apple OCR failure.
    func importDerivedAttachments(
        bookID: String,
        expectedContentSHA256: String,
        manifest: NativeBookOCRDerivedAttachmentManifest,
        files: [String: Data]
    ) async throws -> NativeBookOCRImportResult {
        await waitUntilReady()
        guard Self.isSHA256(expectedContentSHA256),
              expectedContentSHA256.caseInsensitiveCompare(
                manifest.contentSha256
              ) == .orderedSame else {
            throw NativeBookOCRError.invalidAttachment("本机书籍摘要不匹配")
        }
        try activate(
            bookID: bookID,
            expectedContentSHA256: expectedContentSHA256
        )
        let boundPrevious = status(
            for: bookID,
            expectedContentSHA256: expectedContentSHA256
        )
        let previousStatus = boundPrevious.contentSHA256.isEmpty
            ? nil : boundPrevious
        tasks[bookID]?.cancel()
        tasks[bookID] = nil
        let importGeneration = UUID()
        generations[bookID] = importGeneration
        if let previousStatus,
           previousStatus.state == .running || previousStatus.state == .paused {
            publish(status: replacing(
                previousStatus,
                state: .paused,
                currentPage: nil,
                message: "本机预处理已暂停，正在导入 Pi 附件"
            ))
        }
        let result = try await store.importDerivedAttachments(
            expectedContentSHA256: expectedContentSHA256,
            manifest: manifest,
            files: files
        )
        guard generations[bookID] == importGeneration else { return result }
        let pages = try await store.pages(contentSHA256: result.contentSHA256)
        guard generations[bookID] == importGeneration else { return result }
        let previous = status(
            for: bookID,
            expectedContentSHA256: expectedContentSHA256
        )
        let totalPages = max(
            previous.totalPages,
            pages.map(\.page).max() ?? 0
        )
        let state: NativeBookOCRJobState = pages.contains(where: {
            $0.status == .failed
        }) ? .failed : .completed
        let status = Self.makeStatus(
            bookID: bookID,
            contentSHA256: result.contentSHA256,
            state: state,
            totalPages: totalPages,
            currentPage: nil,
            pages: pages,
            message: "已导入 Pi 预处理附件"
        )
        publish(status: status)
        for page in Set(
            result.importedPages + result.importedFormulaPages
        ).sorted() {
            lastUpdate = NativeBookOCRUpdate(
                contract: NativeBookOCRUpdate.contract,
                bookID: bookID,
                page: page,
                status: status
            )
        }
        return result
    }

    func hasImportedRevision(
        expectedContentSHA256: String,
        revision: String
    ) async throws -> Bool {
        await waitUntilReady()
        guard Self.isSHA256(expectedContentSHA256) else {
            throw NativeBookOCRError.invalidContentSHA256
        }
        return try await store.hasImportedRevision(
            contentSHA256: expectedContentSHA256,
            revision: revision
        )
    }

    private func start(
        book: ReaderLocalBookAccess,
        contentSHA256: String,
        configuration: NativeBookOCRConfiguration,
        retryFailed: Bool
    ) async throws {
        guard book.record.format == .pdf else {
            throw NativeBookOCRError.pdfRequired
        }
        guard Self.isSHA256(contentSHA256) else {
            throw NativeBookOCRError.invalidContentSHA256
        }
        if let indexedDigest = book.record.contentSha256,
           indexedDigest.lowercased() != contentSHA256.lowercased() {
            throw NativeBookOCRError.invalidContentSHA256
        }
        try Self.validateCurrentBook(book)
        let bookID = book.record.id
        try activate(
            bookID: bookID,
            expectedContentSHA256: contentSHA256
        )
        tasks[bookID]?.cancel()
        tasks[bookID] = nil
        let generation = UUID()
        generations[bookID] = generation
        let processor = try NativeBookOCRProcessor(fileURL: book.url)
        let pageCount = await processor.numberOfPages()
        guard generations[bookID] == generation else { return }
        do {
            try Self.validateCurrentBook(book)
        } catch {
            invalidate(bookID: bookID, contentSHA256: contentSHA256)
            throw error
        }
        guard pageCount > 0 else { throw NativeBookOCRError.unreadableBook }
        let existing = try await store.pages(
            contentSHA256: contentSHA256.lowercased()
        )
        guard generations[bookID] == generation else { return }
        let starting = Self.makeStatus(
            bookID: bookID,
            contentSHA256: contentSHA256.lowercased(),
            state: .running,
            totalPages: pageCount,
            currentPage: nil,
            pages: existing,
            message: "正在检查页面文字层"
        )
        publish(status: starting)

        let task = Task { [weak self, book, processor] in
            guard let self else { return }
            await self.run(
                book: book,
                processor: processor,
                contentSHA256: contentSHA256.lowercased(),
                configuration: configuration,
                retryFailed: retryFailed,
                generation: generation
            )
        }
        tasks[bookID] = task
    }

    private func run(
        book: ReaderLocalBookAccess,
        processor: NativeBookOCRProcessor,
        contentSHA256: String,
        configuration: NativeBookOCRConfiguration,
        retryFailed: Bool,
        generation: UUID
    ) async {
        let bookID = book.record.id
        do {
            let totalPages = await processor.numberOfPages()
            guard generations[bookID] == generation else { return }
            let storedPages = try await store.pages(
                contentSHA256: contentSHA256
            )
            guard generations[bookID] == generation else { return }
            var cached = Dictionary(
                storedPages.map { ($0.page, $0) },
                uniquingKeysWith: { current, _ in current }
            )
            for pageNumber in 1...totalPages {
                try Task.checkCancellation()
                guard generations[bookID] == generation else { return }
                try Self.validateCurrentBook(book)
                if let current = cached[pageNumber],
                   current.status == .ready || current.status == .readyEmpty
                    || (!retryFailed && current.status == .failed) {
                    continue
                }

                let geometry = try await processor.geometry(
                    pageNumber: pageNumber,
                    configuration: configuration
                )
                guard generations[bookID] == generation else { return }
                let before = Self.makeStatus(
                    bookID: bookID,
                    contentSHA256: contentSHA256,
                    state: .running,
                    totalPages: totalPages,
                    currentPage: pageNumber,
                    pages: Array(cached.values),
                    message: "正在处理第 \(pageNumber)/\(totalPages) 页"
                )
                publish(status: before, page: pageNumber)

                let value: NativeBookOCRPageCharacters
                do {
                    value = try await processor.processPage(
                        pageNumber: pageNumber,
                        contentSHA256: contentSHA256,
                        configuration: configuration
                    )
                } catch is CancellationError {
                    throw CancellationError()
                } catch {
                    value = NativeBookOCRProcessor.failurePage(
                        contentSHA256: contentSHA256,
                        geometry: geometry,
                        message: error.localizedDescription
                    )
                }
                guard generations[bookID] == generation else { return }
                try Self.validateCurrentBook(book)
                try await store.writePage(value)
                try Self.validateCurrentBook(book)
                cached[pageNumber] = value
                guard generations[bookID] == generation else { return }
                let after = Self.makeStatus(
                    bookID: bookID,
                    contentSHA256: contentSHA256,
                    state: .running,
                    totalPages: totalPages,
                    currentPage: nil,
                    pages: Array(cached.values),
                    message: value.status == .failed
                        ? "第 \(pageNumber) 页本机处理失败；不会自动改用 Pi"
                        : "已保存第 \(pageNumber)/\(totalPages) 页"
                )
                publish(status: after, page: pageNumber)
            }
            guard generations[bookID] == generation else { return }
            try Self.validateCurrentBook(book)
            let pages = try await store.pages(contentSHA256: contentSHA256)
            guard generations[bookID] == generation else { return }
            let failed = pages.contains { $0.status == .failed }
            publish(status: Self.makeStatus(
                bookID: bookID,
                contentSHA256: contentSHA256,
                state: failed ? .failed : .completed,
                totalPages: totalPages,
                currentPage: nil,
                pages: pages,
                message: failed
                    ? "本机预处理已结束，部分页面失败；可重试或手动选择 Pi 预处理"
                    : "本机预处理完成"
            ))
        } catch is CancellationError {
            // pause/cancel publishes its explicit state before cancellation;
            // a stale task must not overwrite it.
        } catch ReaderLocalRuntimeError.bookChanged {
            guard generations[bookID] == generation else { return }
            invalidate(bookID: bookID, contentSHA256: contentSHA256)
            let current = bookStatuses[bookID] ?? .idle(bookID: bookID)
            publish(status: replacing(
                current,
                state: .failed,
                currentPage: nil,
                message: ReaderLocalRuntimeError.bookChanged.localizedDescription
            ))
        } catch {
            guard generations[bookID] == generation else { return }
            let current = status(for: bookID)
            publish(status: replacing(current, state: .failed,
                currentPage: nil,
                message: error.localizedDescription))
        }
        if generations[bookID] == generation {
            tasks[bookID] = nil
        }
    }

    private func publish(
        status: NativeBookOCRBookStatus,
        page: Int? = nil
    ) {
        bookStatuses[status.bookID] = status
        lastUpdate = NativeBookOCRUpdate(
            contract: NativeBookOCRUpdate.contract,
            bookID: status.bookID,
            page: page,
            status: status
        )
        let previousWrite = statusWriteTasks[status.bookID]
        let write = Task { @MainActor [weak self, store] in
            if let previousWrite {
                await previousWrite.value
            }
            guard !Task.isCancelled else { return }
            do {
                try await store.writeStatus(status)
            } catch {
                self?.recordStatusPersistenceFailure(
                    bookID: status.bookID,
                    expectedContentSHA256: status.contentSHA256,
                    message: error.localizedDescription
                )
            }
        }
        statusWriteTasks[status.bookID] = write
    }

    private func recordStatusPersistenceFailure(
        bookID: String,
        expectedContentSHA256: String,
        message: String
    ) {
        guard activeContentSHA256[bookID] == expectedContentSHA256.lowercased()
        else { return }
        let current = status(
            for: bookID,
            expectedContentSHA256: expectedContentSHA256
        )
        guard !current.contentSHA256.isEmpty else { return }
        tasks[bookID]?.cancel()
        tasks[bookID] = nil
        statusWriteTasks[bookID]?.cancel()
        generations[bookID] = UUID()
        let failed = replacing(
            current,
            state: .failed,
            currentPage: nil,
            message: "预处理进度无法持久保存：\(message)"
        )
        bookStatuses[bookID] = failed
        lastUpdate = NativeBookOCRUpdate(
            contract: NativeBookOCRUpdate.contract,
            bookID: bookID,
            page: nil,
            status: failed
        )
    }

    private func replacing(
        _ status: NativeBookOCRBookStatus,
        state: NativeBookOCRJobState,
        currentPage: Int?,
        message: String?
    ) -> NativeBookOCRBookStatus {
        NativeBookOCRBookStatus(
            schema: status.schema,
            bookID: status.bookID,
            contentSHA256: status.contentSHA256,
            state: state,
            source: status.source,
            totalPages: status.totalPages,
            currentPage: currentPage,
            textProgress: status.textProgress,
            wordProgress: status.wordProgress,
            formulaProgress: status.formulaProgress,
            formulaPendingRegions: status.formulaPendingRegions,
            formulaFailedRegions: status.formulaFailedRegions,
            message: message,
            updatedAt: Date()
        )
    }

    private static func makeStatus(
        bookID: String,
        contentSHA256: String,
        state: NativeBookOCRJobState,
        totalPages: Int,
        currentPage: Int?,
        pages: [NativeBookOCRPageCharacters],
        message: String?
    ) -> NativeBookOCRBookStatus {
        var textCompleted = 0
        var textFailed = 0
        var wordCompleted = 0
        var wordPending = 0
        var wordFailed = 0
        var wordUnavailable = 0
        var formulaCompleted = 0
        var formulaPending = 0
        var formulaFailed = 0
        var formulaUnavailable = 0
        var formulaPendingRegions = 0
        var formulaFailedRegions = 0
        let byPage = Dictionary(
            pages.map { ($0.page, $0) },
            uniquingKeysWith: { current, _ in current }
        )
        if totalPages > 0 {
            for pageNumber in 1...totalPages {
                guard let page = byPage[pageNumber] else {
                    wordPending += 1
                    formulaPending += 1
                    continue
                }
                switch page.status {
                case .ready, .readyEmpty:
                    textCompleted += 1
                case .failed:
                    textFailed += 1
                case .idle, .pending:
                    break
                }
                if page.status == .failed {
                    wordFailed += 1
                } else {
                    switch page.wordSegmentation {
                    case .ready:
                        wordCompleted += 1
                    case .partial:
                        wordPending += 1
                    case .unavailable:
                        wordUnavailable += 1
                    }
                }
                switch page.formulaCoverage {
                case .complete:
                    if page.formulaRegions.contains(where: {
                        $0.state == .failed
                    }) {
                        formulaFailed += 1
                    } else {
                        formulaCompleted += 1
                    }
                case .unknown, .partial:
                    formulaPending += 1
                case .unavailable:
                    formulaUnavailable += 1
                }
                formulaPendingRegions += page.formulaRegions.filter {
                    $0.state == .pending
                }.count
                formulaFailedRegions += page.formulaRegions.filter {
                    $0.state == .failed
                }.count
            }
        }
        let sourceSet = Set(pages.compactMap(\.source))
        let source = sourceSet.count == 1 ? sourceSet.first : nil
        return NativeBookOCRBookStatus(
            schema: NativeBookOCRBookStatus.schema,
            bookID: bookID,
            contentSHA256: contentSHA256,
            state: state,
            source: source,
            totalPages: totalPages,
            currentPage: currentPage,
            textProgress: NativeBookOCRStageProgress(
                total: totalPages,
                completed: textCompleted,
                pending: max(0, totalPages - textCompleted - textFailed),
                failed: textFailed,
                unavailable: 0
            ),
            wordProgress: NativeBookOCRStageProgress(
                total: totalPages,
                completed: wordCompleted,
                pending: wordPending,
                failed: wordFailed,
                unavailable: wordUnavailable
            ),
            formulaProgress: NativeBookOCRStageProgress(
                total: totalPages,
                completed: formulaCompleted,
                pending: formulaPending,
                failed: formulaFailed,
                unavailable: formulaUnavailable
            ),
            formulaPendingRegions: formulaPendingRegions,
            formulaFailedRegions: formulaFailedRegions,
            message: message,
            updatedAt: Date()
        )
    }

    private static func searchPage(
        _ page: NativeBookOCRPageCharacters,
        query: String,
        remaining: Int
    ) -> (hits: [NativeBookOCRSearchHit], total: Int) {
        guard remaining > 0 else { return ([], 0) }
        var text = ""
        var spans: [(range: NSRange, character: NativeBookOCRCharacter)] = []
        for character in page.chars {
            let start = (text as NSString).length
            text.append(character.c)
            let end = (text as NSString).length
            spans.append((NSRange(location: start, length: end - start), character))
        }
        let haystack = text as NSString
        var cursor = 0
        var total = 0
        var hits: [NativeBookOCRSearchHit] = []
        while cursor < haystack.length {
            let range = haystack.range(
                of: query,
                options: [.caseInsensitive, .diacriticInsensitive],
                range: NSRange(location: cursor, length: haystack.length - cursor)
            )
            guard range.location != NSNotFound, range.length > 0 else { break }
            total += 1
            if hits.count < remaining {
                let selected = spans.enumerated().filter { _, span in
                    NSIntersectionRange(span.range, range).length > 0
                }
                let first = selected.first?.offset ?? 0
                let last = selected.last?.offset ?? first
                let snippetStart = max(0, range.location - 80)
                let snippetEnd = min(haystack.length, NSMaxRange(range) + 80)
                hits.append(NativeBookOCRSearchHit(
                    page: page.page,
                    text: haystack.substring(
                        with: NSRange(
                            location: snippetStart,
                            length: snippetEnd - snippetStart
                        )
                    ),
                    firstCharacter: first,
                    lastCharacter: last,
                    rects: selected.map { _, span in
                        [span.character.x0, span.character.y0,
                         span.character.x1, span.character.y1]
                    }
                ))
            }
            cursor = NSMaxRange(range)
        }
        return (hits, total)
    }

    private static func isSHA256(_ value: String) -> Bool {
        value.range(
            of: #"^[0-9a-fA-F]{64}$"#,
            options: .regularExpression
        ) != nil
    }

    private static func validateCurrentBook(
        _ book: ReaderLocalBookAccess
    ) throws {
        do {
            try book.validateCurrentFile(maximumEPUBBytes: Int64.max)
        } catch {
            throw ReaderLocalRuntimeError.bookChanged
        }
    }
}

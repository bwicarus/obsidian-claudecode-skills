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
    private var pdfMutationLeases: [String: NativeBookOCRPDFMutationLease] = [:]
    private var pdfMutationResolvedStatus: [String: NativeBookOCRBookStatus] = [:]
    private var activeWriteOperations: [String: Int] = [:]
    private var writeOperationWaiters: [
        String: [CheckedContinuation<Void, Never>]
    ] = [:]
    private var didApplyLoadedStatuses = false

    init(store: NativeBookOCRSidecarStore = .shared) {
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

    func beginPDFMutationLease(
        bookID: String,
        expectedOldDigest: String,
        token: String? = nil
    ) async throws -> NativeBookOCRPDFMutationLease {
        guard Self.isSHA256(expectedOldDigest),
              token == nil || token!.range(
                of: #"^[a-f0-9]{32}$"#,
                options: .regularExpression
              ) != nil else {
            throw NativeBookOCRError.invalidContentSHA256
        }
        let normalized = expectedOldDigest.lowercased()
        if let active = pdfMutationLeases[bookID] {
            guard active.oldContentSHA256 == normalized,
                  token == nil || active.token == token else {
                throw NativeBookOCRError.storage(
                    "本书已有另一项 PDF 改页 OCR 租约"
                )
            }
            try await store.beginPDFMutationLease(active)
            return active
        }
        let lease = NativeBookOCRPDFMutationLease(
            bookID: bookID,
            token: token ?? UUID().uuidString
                .replacingOccurrences(of: "-", with: "")
                .lowercased(),
            oldContentSHA256: normalized
        )

        // Close the MainActor gate before the first suspension. Later manual,
        // import and background writes cannot enter while prior work drains.
        pdfMutationLeases[bookID] = lease
        let runningTask = tasks[bookID]
        runningTask?.cancel()
        generations[bookID] = UUID()
        tasks[bookID] = nil
        if let runningTask { await runningTask.value }
        await waitForWriteOperations(bookID: bookID)
        if let statusTail = statusWriteTasks[bookID] {
            await statusTail.value
            statusWriteTasks[bookID] = nil
        }
        do {
            try await store.beginPDFMutationLease(lease)
        } catch {
            pdfMutationLeases.removeValue(forKey: bookID)
            throw error
        }
        if activeContentSHA256[bookID] == normalized {
            activeContentSHA256.removeValue(forKey: bookID)
        }
        return lease
    }

    func rebuildPDFMutationStatus(
        lease: NativeBookOCRPDFMutationLease,
        resolvedContentSHA256: String,
        totalPages: Int,
        message: String
    ) async throws {
        guard pdfMutationLeases[lease.bookID] == lease,
              Self.isSHA256(resolvedContentSHA256),
              totalPages > 0 else {
            throw NativeBookOCRError.storage("PDF 改页 OCR 恢复身份无效")
        }
        let digest = resolvedContentSHA256.lowercased()
        let pages = try await store.pages(contentSHA256: digest)
        guard pdfMutationLeases[lease.bookID] == lease else {
            throw NativeBookOCRError.storage("PDF 改页 OCR 租约已失效")
        }
        let terminal = pages.filter {
            $0.status == .ready || $0.status == .readyEmpty || $0.status == .failed
        }.count
        let state: NativeBookOCRJobState
        if pages.contains(where: { $0.status == .failed }) {
            state = .failed
        } else if terminal >= totalPages {
            state = .completed
        } else {
            state = .paused
        }
        let status = Self.makeStatus(
            bookID: lease.bookID,
            contentSHA256: digest,
            state: state,
            totalPages: totalPages,
            currentPage: nil,
            pages: pages,
            message: message
        )
        try await store.writeStatus(status, mutationLease: lease)
        guard pdfMutationLeases[lease.bookID] == lease else {
            throw NativeBookOCRError.storage("PDF 改页 OCR 租约已失效")
        }
        pdfMutationResolvedStatus[lease.bookID] = status
        bookStatuses[lease.bookID] = status
        lastUpdate = NativeBookOCRUpdate(
            contract: NativeBookOCRUpdate.contract,
            bookID: lease.bookID,
            page: nil,
            status: status
        )
    }

    func finishPDFMutationLease(
        _ lease: NativeBookOCRPDFMutationLease
    ) async throws {
        guard pdfMutationLeases[lease.bookID] == lease,
              let resolved = pdfMutationResolvedStatus[lease.bookID] else {
            throw NativeBookOCRError.storage(
                "PDF 改页 OCR 状态尚未持久确认"
            )
        }
        try await store.finishPDFMutationLease(lease)
        pdfMutationLeases.removeValue(forKey: lease.bookID)
        pdfMutationResolvedStatus.removeValue(forKey: lease.bookID)
        activeContentSHA256[lease.bookID] = resolved.contentSHA256.lowercased()
        invalidatedContentSHA256.removeValue(forKey: lease.bookID)
    }

    func abortUnstagedPDFMutationLease(
        _ lease: NativeBookOCRPDFMutationLease
    ) async throws {
        guard pdfMutationLeases[lease.bookID] == lease else { return }
        try await store.finishPDFMutationLease(lease)
        pdfMutationLeases.removeValue(forKey: lease.bookID)
        pdfMutationResolvedStatus.removeValue(forKey: lease.bookID)
        activeContentSHA256[lease.bookID] = lease.oldContentSHA256.lowercased()
        invalidatedContentSHA256.removeValue(forKey: lease.bookID)
    }

    func activate(
        bookID: String,
        expectedContentSHA256: String
    ) throws {
        guard !bookID.isEmpty, Self.isSHA256(expectedContentSHA256) else {
            throw NativeBookOCRError.invalidContentSHA256
        }
        guard pdfMutationLeases[bookID] == nil else {
            throw NativeBookOCRError.storage("PDF 改页期间不能切换 OCR 摘要")
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
        let normalized = contentSHA256.lowercased()
        guard activeContentSHA256[bookID] == normalized else { return }
        activeContentSHA256.removeValue(forKey: bookID)
        invalidatedContentSHA256[bookID] = normalized
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
        try beginWriteOperation(bookID: book.record.id)
        defer { endWriteOperation(bookID: book.record.id) }
        await waitUntilReady()
        try await start(
            book: book,
            contentSHA256: contentSHA256,
            configuration: configuration,
            retryFailed: false
        )
    }

    func pause(bookID: String) {
        guard pdfMutationLeases[bookID] == nil else { return }
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
        try beginWriteOperation(bookID: book.record.id)
        defer { endWriteOperation(bookID: book.record.id) }
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
        guard pdfMutationLeases[bookID] == nil else { return }
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
        try beginWriteOperation(bookID: book.record.id)
        defer { endWriteOperation(bookID: book.record.id) }
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
        guard Self.isSHA256(expectedContentSHA256),
              activeContentSHA256[bookID]?.caseInsensitiveCompare(
                expectedContentSHA256
              ) == .orderedSame,
              invalidatedContentSHA256[bookID]
                != expectedContentSHA256.lowercased() else { return nil }
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

    /// Explicit user action: recognize the selected rectangle with Apple
    /// Vision, persist it independently from the base page and return the
    /// effective page immediately. A storage failure is a failed action; it is
    /// never reported as a transient OCR success.
    func recognizeSelection(
        book: ReaderLocalBookAccess,
        contentSHA256: String,
        page: Int,
        bbox: [Double],
        configuration: NativeBookOCRConfiguration = NativeBookOCRConfiguration()
    ) async throws -> NativeBookOCRSelectionResult {
        try beginWriteOperation(bookID: book.record.id)
        defer { endWriteOperation(bookID: book.record.id) }
        await waitUntilReady()
        try validateManualRequest(
            book: book,
            contentSHA256: contentSHA256,
            page: page
        )
        let normalizedDigest = contentSHA256.lowercased()
        let processor = try NativeBookOCRProcessor(fileURL: book.url)
        let base = try await ensureBasePage(
            processor: processor,
            bookID: book.record.id,
            contentSHA256: normalizedDigest,
            page: page,
            configuration: configuration
        )
        guard bbox.count == 4,
              bbox.allSatisfy({ $0.isFinite && $0 >= 0 }),
              bbox[2] - bbox[0] >= 0.5,
              bbox[3] - bbox[1] >= 0.5,
              bbox[2] <= base.pageWidth,
              bbox[3] <= base.pageHeight else {
            throw NativeBookOCRError.invalidSelection
        }
        try Self.validateCurrentBook(book)
        let vision = try await processor.processPage(
            pageNumber: page,
            contentSHA256: normalizedDigest,
            configuration: configuration,
            forceVision: true
        )
        try Self.validateCurrentBook(book)
        guard vision.status == .ready else {
            throw NativeBookOCRError.noRecognizedText
        }
        let selected = vision.chars.filter { character in
            let x = (character.x0 + character.x1) / 2
            let y = (character.y0 + character.y1) / 2
            return x >= bbox[0] && x <= bbox[2]
                && y >= bbox[1] && y <= bbox[3]
        }
        let text = selected.map(\.c).joined()
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !selected.isEmpty, !text.isEmpty else {
            throw NativeBookOCRError.noRecognizedText
        }
        let correction = NativeBookOCRSelectionCorrection(
            schema: NativeBookOCRSelectionCorrection.schema,
            id: "ocrfix-" + UUID().uuidString
                .replacingOccurrences(of: "-", with: "")
                .lowercased(),
            contentSHA256: normalizedDigest,
            page: page,
            bbox: bbox,
            text: text,
            chars: selected,
            createdAt: Date()
        )
        try await store.appendSelectionCorrection(
            correction,
            bookID: book.record.id
        )
        try Self.validateCurrentBook(book)
        guard let effective = try await store.page(
            contentSHA256: normalizedDigest,
            page: page
        ) else {
            throw NativeBookOCRError.storage("选区校正写入后无法读取")
        }
        try await publishManualMutation(
            bookID: book.record.id,
            contentSHA256: normalizedDigest,
            page: page,
            pageCount: await processor.numberOfPages(),
            message: "已保存第 \(page) 页选区文字校正"
        )
        return NativeBookOCRSelectionResult(page: effective, text: text)
    }

    /// Explicit user action: force Apple Vision for one page and keep the
    /// result in a separate manual layer above embedded/imported text.
    func reOCRPage(
        book: ReaderLocalBookAccess,
        contentSHA256: String,
        page: Int,
        configuration: NativeBookOCRConfiguration = NativeBookOCRConfiguration()
    ) async throws -> NativeBookOCRPageCharacters {
        try beginWriteOperation(bookID: book.record.id)
        defer { endWriteOperation(bookID: book.record.id) }
        await waitUntilReady()
        try validateManualRequest(
            book: book,
            contentSHA256: contentSHA256,
            page: page
        )
        let normalizedDigest = contentSHA256.lowercased()
        let processor = try NativeBookOCRProcessor(fileURL: book.url)
        _ = try await ensureBasePage(
            processor: processor,
            bookID: book.record.id,
            contentSHA256: normalizedDigest,
            page: page,
            configuration: configuration
        )
        try Self.validateCurrentBook(book)
        let recognized = try await processor.processPage(
            pageNumber: page,
            contentSHA256: normalizedDigest,
            configuration: configuration,
            forceVision: true
        )
        guard recognized.status == .ready || recognized.status == .readyEmpty else {
            throw NativeBookOCRError.noRecognizedText
        }
        try Self.validateCurrentBook(book)
        let manual = recognized.replacingTextAuthority(
            .localOverride,
            engineRevision: "apple-vision-manual/1"
        )
        try await store.writeManualPageOverride(
            manual,
            bookID: book.record.id
        )
        try Self.validateCurrentBook(book)
        guard let effective = try await store.page(
            contentSHA256: normalizedDigest,
            page: page
        ) else {
            throw NativeBookOCRError.storage("单页重扫写入后无法读取")
        }
        try await publishManualMutation(
            bookID: book.record.id,
            contentSHA256: normalizedDigest,
            page: page,
            pageCount: await processor.numberOfPages(),
            message: "已保存第 \(page) 页本机重扫覆盖"
        )
        return effective
    }

    /// Clears only the explicit whole-page Vision override. Selection
    /// corrections and the immutable base page remain intact.
    func clearManualReOCR(
        book: ReaderLocalBookAccess,
        contentSHA256: String,
        page: Int
    ) async throws -> NativeBookOCRClearResult {
        try beginWriteOperation(bookID: book.record.id)
        defer { endWriteOperation(bookID: book.record.id) }
        await waitUntilReady()
        try validateManualRequest(
            book: book,
            contentSHA256: contentSHA256,
            page: page
        )
        let normalizedDigest = contentSHA256.lowercased()
        let cleared = try await store.clearManualPageOverride(
            bookID: book.record.id,
            contentSHA256: normalizedDigest,
            page: page
        )
        try Self.validateCurrentBook(book)
        let effective = try await store.page(
            contentSHA256: normalizedDigest,
            page: page
        )
        if cleared {
            let processor = try NativeBookOCRProcessor(fileURL: book.url)
            try await publishManualMutation(
                bookID: book.record.id,
                contentSHA256: normalizedDigest,
                page: page,
                pageCount: await processor.numberOfPages(),
                message: "已撤销第 \(page) 页本机重扫；选区校正仍保留"
            )
        }
        return NativeBookOCRClearResult(page: effective, cleared: cleared)
    }

    private func validateManualRequest(
        book: ReaderLocalBookAccess,
        contentSHA256: String,
        page: Int
    ) throws {
        guard book.record.format == .pdf else {
            throw NativeBookOCRError.pdfRequired
        }
        guard Self.isSHA256(contentSHA256), page >= 1 else {
            throw NativeBookOCRError.invalidContentSHA256
        }
        if let indexedDigest = book.record.contentSha256,
           indexedDigest.caseInsensitiveCompare(contentSHA256) != .orderedSame {
            throw NativeBookOCRError.invalidContentSHA256
        }
        try Self.validateCurrentBook(book)
        try activate(
            bookID: book.record.id,
            expectedContentSHA256: contentSHA256
        )
    }

    private func ensureBasePage(
        processor: NativeBookOCRProcessor,
        bookID: String,
        contentSHA256: String,
        page: Int,
        configuration: NativeBookOCRConfiguration
    ) async throws -> NativeBookOCRPageCharacters {
        if let existing = try await store.basePage(
            contentSHA256: contentSHA256,
            page: page
        ) {
            return existing
        }
        let value = try await processor.processPage(
            pageNumber: page,
            contentSHA256: contentSHA256,
            configuration: configuration
        )
        guard value.status == .ready || value.status == .readyEmpty else {
            throw NativeBookOCRError.noRecognizedText
        }
        try await store.writePage(value, bookID: bookID)
        return value
    }

    private func publishManualMutation(
        bookID: String,
        contentSHA256: String,
        page: Int,
        pageCount: Int,
        message: String
    ) async throws {
        let pages = try await store.pages(contentSHA256: contentSHA256)
        let status = Self.makeStatus(
            bookID: bookID,
            contentSHA256: contentSHA256,
            state: .completed,
            totalPages: max(pageCount, pages.map(\.page).max() ?? 0),
            currentPage: nil,
            pages: pages,
            message: message
        )
        publish(status: status, page: page)
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
        guard Self.isSHA256(expectedContentSHA256),
              activeContentSHA256[bookID]?.caseInsensitiveCompare(
                expectedContentSHA256
              ) == .orderedSame,
              invalidatedContentSHA256[bookID]
                != expectedContentSHA256.lowercased(),
              !needle.isEmpty else {
            return NativeBookOCRSearchResult(
                matches: [],
                total: 0,
                pages: [],
                incomplete: current.totalPages > 0
            )
        }
        let cap = max(1, min(200, limit))
        let pages = try await store.effectivePages(
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
        let totalPages = max(current.totalPages, pages.map(\.page).max() ?? 0)
        return NativeBookOCRSearchResult(
            matches: hits,
            total: total,
            pages: hitPages.sorted(),
            incomplete: readyPages < totalPages
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
        try beginWriteOperation(bookID: bookID)
        defer { endWriteOperation(bookID: bookID) }
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
            bookID: bookID,
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
                try await store.writePage(value, bookID: bookID)
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

    private func beginWriteOperation(bookID: String) throws {
        guard pdfMutationLeases[bookID] == nil else {
            throw NativeBookOCRError.storage(
                "PDF 改页期间拒绝并发 OCR 写入"
            )
        }
        activeWriteOperations[bookID, default: 0] += 1
    }

    private func endWriteOperation(bookID: String) {
        let remaining = max(0, (activeWriteOperations[bookID] ?? 1) - 1)
        if remaining > 0 {
            activeWriteOperations[bookID] = remaining
            return
        }
        activeWriteOperations.removeValue(forKey: bookID)
        let waiters = writeOperationWaiters.removeValue(forKey: bookID) ?? []
        waiters.forEach { $0.resume() }
    }

    private func waitForWriteOperations(bookID: String) async {
        guard (activeWriteOperations[bookID] ?? 0) > 0 else { return }
        await withCheckedContinuation { continuation in
            writeOperationWaiters[bookID, default: []].append(continuation)
        }
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

import CoreFoundation
import CryptoKit
import Foundation
import PDFKit
import UIKit
import WebKit

enum ReaderNativePDFMutationError: LocalizedError {
    case invalidRequest
    case untrustedSource
    case unavailable(String)
    case busy
    case bookChanged
    case invalidPage(String)
    case stagingFailed(String)
    case commitFailed(String)

    var errorDescription: String? {
        switch self {
        case .invalidRequest:
            return "BW_NATIVE_PDF_MUTATION_REQUEST：PDF 改页请求无效"
        case .untrustedSource:
            return "BW_NATIVE_PDF_MUTATION_SOURCE：PDF 改页来源无效"
        case .unavailable(let detail):
            return "BW_NATIVE_PDF_MUTATION_UNAVAILABLE：\(detail)"
        case .busy:
            return "BW_NATIVE_PDF_MUTATION_BUSY：本书已有改页任务进行中"
        case .bookChanged:
            return "BW_NATIVE_PDF_MUTATION_BOOK_CHANGED：PDF 在改页期间发生变化"
        case .invalidPage(let detail):
            return "BW_NATIVE_PDF_MUTATION_PAGE：\(detail)"
        case .stagingFailed(let detail):
            return "BW_NATIVE_PDF_MUTATION_STAGING：\(detail)"
        case .commitFailed(let detail):
            return "BW_NATIVE_PDF_MUTATION_COMMIT：\(detail)"
        }
    }
}

enum ReaderNativePDFMutationOperation: String, Codable, Sendable {
    case insert
    case edit
    case delete
}

struct ReaderNativePDFMutationPrepareRequest: Sendable {
    let requestID: String
    let localBookID: String
    let operation: ReaderNativePDFMutationOperation
    let after: Int?
    let page: Int?
    let title: String
    let markdown: String
}

enum ReaderNativePDFMutationCommand: Sendable {
    case prepare(ReaderNativePDFMutationPrepareRequest)
    case commit(requestID: String, localBookID: String, ticket: String)
    case finalize(requestID: String, localBookID: String, ticket: String)
    case cancel(requestID: String, localBookID: String, ticket: String)
    case recover(
        requestID: String,
        localBookID: String,
        ticket: String?,
        oldContentSHA256: String?,
        stagedContentSHA256: String?
    )
}

struct ReaderNativePDFMutationPreparedReceipt: Sendable {
    let ticket: String
    let requestID: String
    let localBookID: String
    let operation: ReaderNativePDFMutationOperation
    let pivotPage: Int
    let oldPageCount: Int
    let newPageCount: Int
    let oldContentSHA256: String
    let stagedContentSHA256: String
    let warnings: [String]
}

struct ReaderNativePDFMutationReplacementReceipt: Sendable {
    let ticket: String
    let localBookID: String
    let operation: ReaderNativePDFMutationOperation
    let pivotPage: Int
    let oldPageCount: Int
    let newPageCount: Int
    let contentSHA256: String
    let modifiedAt: Date
    let byteCount: Int64
}

struct ReaderNativePDFMutationRecoveryReceipt: Sendable {
    enum Outcome: String, Sendable {
        case none
        case committed
        case rolledBack = "rolled-back"
    }

    let outcome: Outcome
    let ticket: String?
    let localBookID: String
    let contentSHA256: String
    let pageCount: Int
    let modifiedAt: Date
    let byteCount: Int64
}

struct ReaderNativePDFMutationRecoveryIdentity: Sendable {
    let ticket: String
    let ocrLease: NativeBookOCRPDFMutationLease
}

/// Owns the only mutable filesystem transaction for a native-local PDF.
/// `prepare` writes and validates a sibling staging PDF without touching the
/// original. `replacePrepared` fences the exact old bytes and keeps a sibling
/// backup until the WebView host has rescanned and reopened the replacement.
actor ReaderNativePDFMutationActor {
    private static let journalSchema = "reader-native-pdf-mutation-journal/1"

    private enum JournalPhase: String, Codable, Sendable {
        case preparing
        case staged
        case installingOCR = "installing-ocr"
        case ocrInstalled = "ocr-installed"
        case replacing
        case replaced
        case rollingBack = "rolling-back"
        case committed
    }

    private struct RenderedPage {
        let page: PDFPage
        let warnings: [String]
    }

    private struct FileIdentity: Codable, Sendable {
        let byteCount: Int64
        let modifiedAt: Date
        let sha256: String
    }

    private struct DurableJournal: Codable, Sendable {
        let schema: String
        let ticket: String
        let requestID: String
        let localBookID: String
        let ocrLeaseToken: String
        let operation: ReaderNativePDFMutationOperation
        let pivotPage: Int
        let oldPageCount: Int
        let newPageCount: Int
        let oldIdentity: FileIdentity
        var stagedContentSHA256: String?
        var phase: JournalPhase
        var hasOCRSource: Bool
        var hadOCRTarget: Bool
    }

    private struct PreparedMutation: Sendable {
        let receipt: ReaderNativePDFMutationPreparedReceipt
        let originalURL: URL
        let stagingURL: URL
        let backupURL: URL
        let oldIdentity: FileIdentity
        let journalURL: URL
        let ocrLease: NativeBookOCRPDFMutationLease
        var replacementIdentity: FileIdentity?
    }

    private let ocrStore: NativeBookOCRSidecarStore
    private var prepared: [String: PreparedMutation] = [:]
    private var preparedTicketByBook: [String: String] = [:]

    init(ocrStore: NativeBookOCRSidecarStore = .shared) {
        self.ocrStore = ocrStore
    }

    func prepare(
        book: ReaderLocalBookAccess,
        request: ReaderNativePDFMutationPrepareRequest,
        ocrLease: NativeBookOCRPDFMutationLease
    ) async throws -> ReaderNativePDFMutationPreparedReceipt {
        guard book.record.id == request.localBookID,
              book.record.format == .pdf,
              book.url.pathExtension.lowercased() == "pdf" else {
            throw ReaderNativePDFMutationError.unavailable(
                "当前文档不是请求指定的本机 PDF"
            )
        }
        guard preparedTicketByBook[request.localBookID] == nil else {
            throw ReaderNativePDFMutationError.busy
        }
        try book.validateCurrentFile(maximumEPUBBytes: .max)
        let oldIdentity = try fileIdentity(book.url)
        guard ocrLease.bookID == request.localBookID,
              ocrLease.oldContentSHA256 == oldIdentity.sha256 else {
            throw ReaderNativePDFMutationError.invalidRequest
        }
        guard let source = PDFDocument(url: book.url), source.pageCount > 0 else {
            throw ReaderNativePDFMutationError.stagingFailed("无法打开原 PDF")
        }

        let oldPageCount = source.pageCount
        let pivotPage: Int
        let replacementIndex: Int
        switch request.operation {
        case .insert:
            guard let after = request.after,
                  (0...oldPageCount).contains(after) else {
                throw ReaderNativePDFMutationError.invalidPage(
                    "after 越界（书共 \(oldPageCount) 页）"
                )
            }
            pivotPage = after + 1
            replacementIndex = after
        case .edit, .delete:
            guard let page = request.page,
                  (1...oldPageCount).contains(page) else {
                throw ReaderNativePDFMutationError.invalidPage(
                    "目标页越界（书共 \(oldPageCount) 页）"
                )
            }
            if request.operation == .delete, oldPageCount == 1 {
                throw ReaderNativePDFMutationError.invalidPage(
                    "不能删除 PDF 的最后一页"
                )
            }
            pivotPage = page
            replacementIndex = page - 1
        }

        let ticket = "npmt_" + Self.randomHex(byteCount: 16)
        let siblingStem = ".bw-pdf-mutation-\(ticket)"
        let directory = book.url.deletingLastPathComponent()
        let stagingURL = directory.appendingPathComponent(
            siblingStem + ".staging.pdf",
            isDirectory: false
        )
        let backupURL = directory.appendingPathComponent(
            siblingStem + ".backup.pdf",
            isDirectory: false
        )
        let journalURL = try mutationJournalURL(
            originalURL: book.url,
            localBookID: request.localBookID
        )
        guard !FileManager.default.fileExists(atPath: journalURL.path) else {
            throw ReaderNativePDFMutationError.busy
        }
        try? FileManager.default.removeItem(at: stagingURL)
        try? FileManager.default.removeItem(at: backupURL)

        var journal = DurableJournal(
            schema: Self.journalSchema,
            ticket: ticket,
            requestID: request.requestID,
            localBookID: request.localBookID,
            ocrLeaseToken: ocrLease.token,
            operation: request.operation,
            pivotPage: pivotPage,
            oldPageCount: oldPageCount,
            newPageCount: oldPageCount
                + (request.operation == .insert ? 1 : 0)
                - (request.operation == .delete ? 1 : 0),
            oldIdentity: oldIdentity,
            stagedContentSHA256: nil,
            phase: .preparing,
            hasOCRSource: false,
            hadOCRTarget: false
        )
        try writeJournal(journal, to: journalURL)
        var stagedContentSHA256: String?

        do {
            var warnings: [String] = []
            switch request.operation {
            case .insert:
                let size = referencePageSize(
                    document: source,
                    insertionIndex: replacementIndex
                )
                let rendered = try renderedPage(
                    size: size,
                    title: request.title,
                    markdown: request.markdown
                )
                warnings = rendered.warnings
                source.insert(rendered.page, at: replacementIndex)
            case .edit:
                let oldPage = source.page(at: replacementIndex)
                let size = oldPage.map {
                    Self.normalizedPageSize($0.bounds(for: .mediaBox).size)
                } ?? CGSize(width: 612, height: 792)
                source.removePage(at: replacementIndex)
                let rendered = try renderedPage(
                    size: size,
                    title: request.title,
                    markdown: request.markdown
                )
                warnings = rendered.warnings
                source.insert(rendered.page, at: replacementIndex)
            case .delete:
                source.removePage(at: replacementIndex)
            }

            guard source.write(to: stagingURL) else {
                throw ReaderNativePDFMutationError.stagingFailed(
                    "无法写入同目录 staging PDF"
                )
            }
            guard let verified = PDFDocument(url: stagingURL) else {
                throw ReaderNativePDFMutationError.stagingFailed(
                    "staging PDF 无法重新打开"
                )
            }
            let expectedCount = oldPageCount
                + (request.operation == .insert ? 1 : 0)
                - (request.operation == .delete ? 1 : 0)
            guard verified.pageCount == expectedCount else {
                throw ReaderNativePDFMutationError.stagingFailed(
                    "staging PDF 页数校验失败"
                )
            }
            if request.operation != .delete {
                let expectedText = Self.renderedPlainText(
                    title: request.title,
                    markdown: request.markdown
                )
                let actualText = verified.page(at: replacementIndex)?.string ?? ""
                let requiredProbe = expectedText
                    .split(whereSeparator: { $0.isWhitespace })
                    .first
                    .map(String.init) ?? "用户插入页"
                // ⚠ 2026-08-24 真机实锤：PDFKit 从写盘重开的 PDF 提取文字时，
                // 系统字体写出的部分汉字会映射成**康熙部首区变体码点**
                // （⽤ U+2F92≠用 U+7528，字形相同），且会散布空格
                // （"Co n t ent s"）。按码点 contains 必然误判 → 空白插入页
                // 被回滚"自动消失"。比较前 NFKC 兼容归一 + 剥除空白。
                let normalizedActual = Self.probeComparableText(actualText)
                guard normalizedActual.contains(
                    Self.probeComparableText(requiredProbe)
                )
                        || normalizedActual.contains("BWPAGE") else {
                    // 诊断要能分辨两种根因：读错页（相邻扫描页无文字层，
                    // actual 为空）还是 PDFKit 跨文档插页写出后丢文字
                    // （actual 为空但邻页有字/页数对）。把现场带全。
                    let neighborText = verified.page(
                        at: max(0, replacementIndex - 1)
                    )?.string ?? ""
                    throw ReaderNativePDFMutationError.stagingFailed(
                        "新页没有可验证的文字层"
                        + "（index=\(replacementIndex)/共\(verified.pageCount)页"
                        + "，probe=\(requiredProbe.prefix(12))"
                        + "，该页文字[\(actualText.count)字]="
                        + "\(actualText.prefix(40))"
                        + "，前一页文字[\(neighborText.count)字]="
                        + "\(neighborText.prefix(20))）"
                    )
                }
            }
            let stagedIdentity = try fileIdentity(stagingURL)
            stagedContentSHA256 = stagedIdentity.sha256
            let ocrStage = try await ocrStore.stagePDFMutation(
                lease: ocrLease,
                ticket: ticket,
                stagedContentSHA256: stagedIdentity.sha256,
                transform: { staging in
                    try Self.migrateOCRContentDirectory(
                        staging,
                        stagedContentSHA256: stagedIdentity.sha256,
                        operation: request.operation,
                        pivotPage: pivotPage
                    )
                }
            )
            journal.stagedContentSHA256 = stagedIdentity.sha256
            journal.phase = .staged
            journal.hasOCRSource = ocrStage.hasSource
            journal.hadOCRTarget = ocrStage.hadTarget
            try writeJournal(journal, to: journalURL)
            let receipt = ReaderNativePDFMutationPreparedReceipt(
                ticket: ticket,
                requestID: request.requestID,
                localBookID: request.localBookID,
                operation: request.operation,
                pivotPage: pivotPage,
                oldPageCount: oldPageCount,
                newPageCount: expectedCount,
                oldContentSHA256: oldIdentity.sha256,
                stagedContentSHA256: stagedIdentity.sha256,
                warnings: warnings
            )
            prepared[ticket] = PreparedMutation(
                receipt: receipt,
                originalURL: book.url,
                stagingURL: stagingURL,
                backupURL: backupURL,
                oldIdentity: oldIdentity,
                journalURL: journalURL,
                ocrLease: ocrLease,
                replacementIdentity: nil
            )
            preparedTicketByBook[request.localBookID] = ticket
            return receipt
        } catch {
            let primaryError = error
            do {
                for url in [stagingURL, backupURL]
                where FileManager.default.fileExists(atPath: url.path) {
                    try FileManager.default.removeItem(at: url)
                }
                try await ocrStore.cleanupPDFMutationArtifacts(
                    lease: ocrLease,
                    ticket: ticket,
                    stagedContentSHA256: stagedContentSHA256
                )
                if FileManager.default.fileExists(atPath: journalURL.path) {
                    try FileManager.default.removeItem(at: journalURL)
                }
            } catch let cleanupError {
                throw ReaderNativePDFMutationError.commitFailed(
                    "\(primaryError.localizedDescription)；失败清理未完成，"
                        + "已保留 journal 供恢复："
                        + cleanupError.localizedDescription
                )
            }
            throw primaryError
        }
    }

    func replacePrepared(
        ticket: String,
        localBookID: String
    ) async throws -> ReaderNativePDFMutationReplacementReceipt {
        guard var mutation = prepared[ticket],
              mutation.receipt.localBookID == localBookID,
              preparedTicketByBook[localBookID] == ticket,
              mutation.replacementIdentity == nil else {
            throw ReaderNativePDFMutationError.invalidRequest
        }
        let currentIdentity = try fileIdentity(mutation.originalURL)
        guard currentIdentity.byteCount == mutation.oldIdentity.byteCount,
              currentIdentity.modifiedAt == mutation.oldIdentity.modifiedAt,
              currentIdentity.sha256 == mutation.oldIdentity.sha256 else {
            throw ReaderNativePDFMutationError.bookChanged
        }
        guard FileManager.default.fileExists(atPath: mutation.stagingURL.path),
              !FileManager.default.fileExists(atPath: mutation.backupURL.path) else {
            throw ReaderNativePDFMutationError.stagingFailed(
                "staging/backup 状态不一致"
            )
        }

        do {
            var journal = try loadJournal(
                from: mutation.journalURL,
                expectedLocalBookID: localBookID
            )
            guard journal.ticket == ticket,
                  journal.phase == .staged,
                  journal.stagedContentSHA256
                    == mutation.receipt.stagedContentSHA256 else {
                throw ReaderNativePDFMutationError.commitFailed(
                    "持久 journal 与 staging 不一致"
                )
            }
            journal.phase = .installingOCR
            try writeJournal(journal, to: mutation.journalURL)
            try await ocrStore.installPDFMutation(
                lease: mutation.ocrLease,
                ticket: ticket,
                stagedContentSHA256: mutation.receipt.stagedContentSHA256,
                hasSource: journal.hasOCRSource,
                hadTarget: journal.hadOCRTarget
            )
            journal.phase = .ocrInstalled
            try writeJournal(journal, to: mutation.journalURL)
            journal.phase = .replacing
            try writeJournal(journal, to: mutation.journalURL)
            _ = try FileManager.default.replaceItemAt(
                mutation.originalURL,
                withItemAt: mutation.stagingURL,
                backupItemName: mutation.backupURL.lastPathComponent,
                options: []
            )
            let replacementIdentity = try fileIdentity(mutation.originalURL)
            // Record the replaced phase before validating the new PDF. If a
            // later check or host rescan fails, cancelOrRollback must still
            // know that the sibling backup is authoritative.
            mutation.replacementIdentity = replacementIdentity
            prepared[ticket] = mutation
            journal.phase = .replaced
            try writeJournal(journal, to: mutation.journalURL)
            guard replacementIdentity.sha256
                    == mutation.receipt.stagedContentSHA256,
                  let verified = PDFDocument(url: mutation.originalURL),
                  verified.pageCount == mutation.receipt.newPageCount else {
                throw ReaderNativePDFMutationError.commitFailed(
                    "替换后的 PDF 未通过摘要或页数校验"
                )
            }
            return ReaderNativePDFMutationReplacementReceipt(
                ticket: ticket,
                localBookID: mutation.receipt.localBookID,
                operation: mutation.receipt.operation,
                pivotPage: mutation.receipt.pivotPage,
                oldPageCount: mutation.receipt.oldPageCount,
                newPageCount: mutation.receipt.newPageCount,
                contentSHA256: replacementIdentity.sha256,
                modifiedAt: replacementIdentity.modifiedAt,
                byteCount: replacementIdentity.byteCount
            )
        } catch {
            do {
                try markRollingBack(mutation)
                try await rollbackPreparedMutation(mutation)
            } catch let rollbackError {
                prepared[ticket] = mutation
                throw ReaderNativePDFMutationError.commitFailed(
                    "\(error.localizedDescription)；自动回滚失败："
                        + rollbackError.localizedDescription
                )
            }
            throw error
        }
    }

    func finalize(ticket: String, localBookID: String) async throws {
        guard let mutation = prepared[ticket],
              mutation.receipt.localBookID == localBookID,
              preparedTicketByBook[localBookID] == ticket,
              mutation.replacementIdentity != nil else {
            throw ReaderNativePDFMutationError.invalidRequest
        }
        var journal = try loadJournal(
            from: mutation.journalURL,
            expectedLocalBookID: localBookID
        )
        guard journal.ticket == ticket, journal.phase == .replaced else {
            throw ReaderNativePDFMutationError.commitFailed(
                "持久 journal 不能确认已替换阶段"
            )
        }
        // The committed tombstone is durable before either backup is removed.
        // A crash from this point forward is recovered as a commit, never as
        // an impossible half rollback.
        journal.phase = .committed
        try writeJournal(journal, to: mutation.journalURL)
        try? FileManager.default.removeItem(at: mutation.backupURL)
        try await ocrStore.cleanupPDFMutationArtifacts(
            lease: mutation.ocrLease,
            ticket: ticket,
            stagedContentSHA256: mutation.receipt.stagedContentSHA256
        )
        removePrepared(ticket: ticket, localBookID: localBookID)
    }

    func cancelOrRollback(ticket: String, localBookID: String) async throws {
        guard let mutation = prepared[ticket],
              mutation.receipt.localBookID == localBookID,
              preparedTicketByBook[localBookID] == ticket else {
            throw ReaderNativePDFMutationError.invalidRequest
        }
        try markRollingBack(mutation)
        try await rollbackPreparedMutation(mutation)
    }

    func hasUnfinishedMutation(book: ReaderLocalBookAccess) throws -> Bool {
        if preparedTicketByBook[book.record.id] != nil {
            return true
        }
        let journalURL = try mutationJournalURL(
            originalURL: book.url,
            localBookID: book.record.id
        )
        return FileManager.default.fileExists(atPath: journalURL.path)
    }

    func mutationLease(
        ticket: String,
        localBookID: String
    ) throws -> NativeBookOCRPDFMutationLease {
        guard let mutation = prepared[ticket],
              mutation.receipt.localBookID == localBookID,
              preparedTicketByBook[localBookID] == ticket else {
            throw ReaderNativePDFMutationError.invalidRequest
        }
        return mutation.ocrLease
    }

    func recoveryIdentity(
        book: ReaderLocalBookAccess
    ) throws -> ReaderNativePDFMutationRecoveryIdentity? {
        if let ticket = preparedTicketByBook[book.record.id],
           let mutation = prepared[ticket] {
            return ReaderNativePDFMutationRecoveryIdentity(
                ticket: ticket,
                ocrLease: mutation.ocrLease
            )
        }
        let journalURL = try mutationJournalURL(
            originalURL: book.url,
            localBookID: book.record.id
        )
        guard FileManager.default.fileExists(atPath: journalURL.path) else {
            return nil
        }
        let journal = try loadJournal(
            from: journalURL,
            expectedLocalBookID: book.record.id
        )
        return ReaderNativePDFMutationRecoveryIdentity(
            ticket: journal.ticket,
            ocrLease: Self.ocrLease(from: journal)
        )
    }

    /// Navigation is not a commit signal. Every pre-commit phase is therefore
    /// rolled back before the host may leave this book. A durable `.committed`
    /// tombstone remains committed because its PDF and OCR backups are already
    /// no longer authoritative.
    func rollbackForOutgoingNavigation(
        book: ReaderLocalBookAccess,
        ticket: String
    ) async throws -> ReaderNativePDFMutationRecoveryReceipt {
        guard let identity = try recoveryIdentity(book: book),
              identity.ticket == ticket else {
            throw ReaderNativePDFMutationError.invalidRequest
        }
        return try await recover(
            book: book,
            ticket: ticket,
            oldContentSHA256: nil,
            stagedContentSHA256: nil
        )
    }

    func recover(
        book: ReaderLocalBookAccess,
        ticket expectedTicket: String?,
        oldContentSHA256 expectedOld: String?,
        stagedContentSHA256 expectedStaged: String?
    ) async throws -> ReaderNativePDFMutationRecoveryReceipt {
        guard book.record.format == .pdf,
              book.url.pathExtension.lowercased() == "pdf" else {
            throw ReaderNativePDFMutationError.unavailable(
                "当前文档不是本机 PDF"
            )
        }
        let journalURL = try mutationJournalURL(
            originalURL: book.url,
            localBookID: book.record.id
        )
        if !FileManager.default.fileExists(atPath: journalURL.path) {
            let identity = try fileIdentity(book.url)
            let outcome: ReaderNativePDFMutationRecoveryReceipt.Outcome
            if let expectedStaged,
               identity.sha256.caseInsensitiveCompare(expectedStaged)
                == .orderedSame {
                outcome = .committed
            } else if let expectedOld,
                      identity.sha256.caseInsensitiveCompare(expectedOld)
                        == .orderedSame {
                outcome = .rolledBack
            } else if expectedTicket == nil,
                      expectedOld == nil,
                      expectedStaged == nil {
                outcome = .none
            } else {
                throw ReaderNativePDFMutationError.commitFailed(
                    "journal 已消失且当前 PDF 摘要无法判定提交或回滚"
                )
            }
            if let activeTicket = preparedTicketByBook[book.record.id] {
                guard expectedTicket == activeTicket else {
                    throw ReaderNativePDFMutationError.commitFailed(
                        "原生事务仍在内存中，但持久 journal 已消失"
                    )
                }
                removePrepared(
                    ticket: activeTicket,
                    localBookID: book.record.id
                )
            }
            return recoveryReceipt(
                outcome: outcome,
                ticket: expectedTicket,
                localBookID: book.record.id,
                pageCount: PDFDocument(url: book.url)?.pageCount ?? 0,
                identity: identity
            )
        }

        var journal = try loadJournal(
            from: journalURL,
            expectedLocalBookID: book.record.id
        )
        guard preparedTicketByBook[book.record.id] == nil
                || preparedTicketByBook[book.record.id] == journal.ticket else {
            throw ReaderNativePDFMutationError.commitFailed(
                "本书内存事务与持久 journal 身份不一致"
            )
        }
        guard expectedTicket == nil || expectedTicket == journal.ticket,
              expectedOld == nil || expectedOld == journal.oldIdentity.sha256,
              expectedStaged == nil
                || expectedStaged == journal.stagedContentSHA256 else {
            throw ReaderNativePDFMutationError.commitFailed(
                "网页 journal 与原生 journal 身份不一致"
            )
        }
        if journal.stagedContentSHA256 == nil {
            let identity = try fileIdentity(book.url)
            guard journal.phase == .preparing,
                  identity.sha256 == journal.oldIdentity.sha256 else {
                throw ReaderNativePDFMutationError.commitFailed(
                    "未完成 staging 的 journal 与当前 PDF 不一致"
                )
            }
            let stem = ".bw-pdf-mutation-\(journal.ticket)"
            let directory = book.url.deletingLastPathComponent()
            for suffix in [".staging.pdf", ".backup.pdf"] {
                try? FileManager.default.removeItem(
                    at: directory.appendingPathComponent(stem + suffix)
                )
            }
            // OCR staging can already exist before the staged digest is
            // durably added to the journal. A crash in that narrow window is
            // still a rollback and must not leave an orphaned content copy.
            let lease = Self.ocrLease(from: journal)
            try await ocrStore.cleanupPDFMutationArtifacts(
                lease: lease,
                ticket: journal.ticket,
                stagedContentSHA256: nil
            )
            return recoveryReceipt(
                outcome: .rolledBack,
                ticket: journal.ticket,
                localBookID: journal.localBookID,
                pageCount: journal.oldPageCount,
                identity: identity
            )
        }
        let mutation = try mutation(
            from: journal,
            originalURL: book.url,
            journalURL: journalURL
        )
        if journal.phase == .committed {
            let identity = try fileIdentity(book.url)
            guard identity.sha256 == journal.stagedContentSHA256 else {
                throw ReaderNativePDFMutationError.commitFailed(
                    "已提交 journal 与当前 PDF 摘要不一致"
                )
            }
            return recoveryReceipt(
                outcome: .committed,
                ticket: journal.ticket,
                localBookID: journal.localBookID,
                pageCount: journal.newPageCount,
                identity: identity
            )
        }

        journal.phase = .rollingBack
        try writeJournal(journal, to: journalURL)
        try await rollbackPreparedMutation(mutation)
        let restored = try fileIdentity(book.url)
        guard restored.sha256 == journal.oldIdentity.sha256 else {
            throw ReaderNativePDFMutationError.commitFailed(
                "恢复完成后原 PDF 摘要仍不匹配"
            )
        }
        return recoveryReceipt(
            outcome: .rolledBack,
            ticket: journal.ticket,
            localBookID: journal.localBookID,
            pageCount: journal.oldPageCount,
            identity: restored
        )
    }

    func acknowledgeRecovery(
        book: ReaderLocalBookAccess,
        ticket expectedTicket: String
    ) async throws {
        let journalURL = try mutationJournalURL(
            originalURL: book.url,
            localBookID: book.record.id
        )
        guard FileManager.default.fileExists(atPath: journalURL.path) else {
            removePrepared(
                ticket: expectedTicket,
                localBookID: book.record.id
            )
            return
        }
        let journal = try loadJournal(
            from: journalURL,
            expectedLocalBookID: book.record.id
        )
        guard journal.ticket == expectedTicket else {
            throw ReaderNativePDFMutationError.commitFailed(
                "拒绝清理另一项 PDF 改页 journal"
            )
        }
        let lease = Self.ocrLease(from: journal)
        if journal.stagedContentSHA256 != nil {
            let mutation = try mutation(
                from: journal,
                originalURL: book.url,
                journalURL: journalURL
            )
            try await cleanupMutationArtifacts(mutation)
        } else {
            let stem = ".bw-pdf-mutation-\(journal.ticket)"
            let directory = book.url.deletingLastPathComponent()
            for suffix in [".staging.pdf", ".backup.pdf"] {
                let url = directory.appendingPathComponent(stem + suffix)
                if FileManager.default.fileExists(atPath: url.path) {
                    try FileManager.default.removeItem(at: url)
                }
            }
            try await ocrStore.cleanupPDFMutationArtifacts(
                lease: lease,
                ticket: journal.ticket,
                stagedContentSHA256: nil
            )
            try FileManager.default.removeItem(at: journalURL)
        }
        removePrepared(
            ticket: journal.ticket,
            localBookID: journal.localBookID
        )
    }

    private func recoveryReceipt(
        outcome: ReaderNativePDFMutationRecoveryReceipt.Outcome,
        ticket: String?,
        localBookID: String,
        pageCount: Int,
        identity: FileIdentity
    ) -> ReaderNativePDFMutationRecoveryReceipt {
        ReaderNativePDFMutationRecoveryReceipt(
            outcome: outcome,
            ticket: ticket,
            localBookID: localBookID,
            contentSHA256: identity.sha256,
            pageCount: pageCount,
            modifiedAt: identity.modifiedAt,
            byteCount: identity.byteCount
        )
    }

    private func removePrepared(ticket: String, localBookID: String) {
        prepared.removeValue(forKey: ticket)
        if preparedTicketByBook[localBookID] == ticket {
            preparedTicketByBook.removeValue(forKey: localBookID)
        }
    }

    private func markRollingBack(_ mutation: PreparedMutation) throws {
        guard FileManager.default.fileExists(atPath: mutation.journalURL.path)
        else { return }
        var journal = try loadJournal(
            from: mutation.journalURL,
            expectedLocalBookID: mutation.receipt.localBookID
        )
        journal.phase = .rollingBack
        try writeJournal(journal, to: mutation.journalURL)
    }

    private func rollbackPreparedMutation(
        _ mutation: PreparedMutation
    ) async throws {
        let current = try fileIdentity(mutation.originalURL)
        if current.sha256 == mutation.receipt.stagedContentSHA256 {
            try restoreBackup(mutation)
        } else if current.sha256 != mutation.oldIdentity.sha256 {
            throw ReaderNativePDFMutationError.commitFailed(
                "当前 PDF 既不是原摘要也不是 staging 摘要"
            )
        }
        let journal = try loadJournal(
            from: mutation.journalURL,
            expectedLocalBookID: mutation.receipt.localBookID
        )
        try await ocrStore.rollbackPDFMutation(
            lease: mutation.ocrLease,
            ticket: mutation.receipt.ticket,
            stagedContentSHA256: mutation.receipt.stagedContentSHA256,
            hadTarget: journal.hadOCRTarget,
            mayHaveInstalled: journal.phase != .preparing
                && journal.phase != .staged
        )
    }

    private func cleanupMutationArtifacts(
        _ mutation: PreparedMutation
    ) async throws {
        let failedReplacementURL = mutation.originalURL
            .deletingLastPathComponent()
            .appendingPathComponent(
                ".bw-pdf-mutation-"
                    + mutation.receipt.ticket + ".failed.pdf",
                isDirectory: false
            )
        for url in [
            mutation.stagingURL,
            mutation.backupURL,
            failedReplacementURL,
        ] where FileManager.default.fileExists(atPath: url.path) {
            try FileManager.default.removeItem(at: url)
        }
        try await ocrStore.cleanupPDFMutationArtifacts(
            lease: mutation.ocrLease,
            ticket: mutation.receipt.ticket,
            stagedContentSHA256: mutation.receipt.stagedContentSHA256
        )
        if FileManager.default.fileExists(atPath: mutation.journalURL.path) {
            try FileManager.default.removeItem(at: mutation.journalURL)
        }
    }

    private func restoreBackup(_ mutation: PreparedMutation) throws {
        guard FileManager.default.fileExists(atPath: mutation.backupURL.path)
        else {
            throw ReaderNativePDFMutationError.commitFailed(
                "原 PDF 备份不存在，不能自动回滚"
            )
        }
        let failedName = ".bw-pdf-mutation-"
            + mutation.receipt.ticket + ".failed.pdf"
        _ = try FileManager.default.replaceItemAt(
            mutation.originalURL,
            withItemAt: mutation.backupURL,
            backupItemName: failedName,
            options: []
        )
        let failedURL = mutation.originalURL.deletingLastPathComponent()
            .appendingPathComponent(failedName, isDirectory: false)
        try? FileManager.default.removeItem(at: failedURL)
        let restored = try fileIdentity(mutation.originalURL)
        guard restored.sha256 == mutation.oldIdentity.sha256 else {
            throw ReaderNativePDFMutationError.commitFailed(
                "回滚后的 PDF 摘要不匹配"
            )
        }
    }

    private func mutation(
        from journal: DurableJournal,
        originalURL: URL,
        journalURL: URL
    ) throws -> PreparedMutation {
        guard let stagedContentSHA256 = journal.stagedContentSHA256 else {
            throw ReaderNativePDFMutationError.commitFailed(
                "原生 journal 尚未记录 staging 摘要"
            )
        }
        let directory = originalURL.deletingLastPathComponent()
        let siblingStem = ".bw-pdf-mutation-\(journal.ticket)"
        return PreparedMutation(
            receipt: ReaderNativePDFMutationPreparedReceipt(
                ticket: journal.ticket,
                requestID: journal.requestID,
                localBookID: journal.localBookID,
                operation: journal.operation,
                pivotPage: journal.pivotPage,
                oldPageCount: journal.oldPageCount,
                newPageCount: journal.newPageCount,
                oldContentSHA256: journal.oldIdentity.sha256,
                stagedContentSHA256: stagedContentSHA256,
                warnings: []
            ),
            originalURL: originalURL,
            stagingURL: directory.appendingPathComponent(
                siblingStem + ".staging.pdf"
            ),
            backupURL: directory.appendingPathComponent(
                siblingStem + ".backup.pdf"
            ),
            oldIdentity: journal.oldIdentity,
            journalURL: journalURL,
            ocrLease: Self.ocrLease(from: journal),
            replacementIdentity: nil
        )
    }

    private static func ocrLease(
        from journal: DurableJournal
    ) -> NativeBookOCRPDFMutationLease {
        NativeBookOCRPDFMutationLease(
            bookID: journal.localBookID,
            token: journal.ocrLeaseToken,
            oldContentSHA256: journal.oldIdentity.sha256
        )
    }

    private func writeJournal(
        _ journal: DurableJournal,
        to url: URL
    ) throws {
        guard journal.schema == Self.journalSchema,
              journal.ticket.range(
                of: #"^npmt_[a-f0-9]{32}$"#,
                options: .regularExpression
              ) != nil,
              journal.ocrLeaseToken.range(
                of: #"^[a-f0-9]{32}$"#,
                options: .regularExpression
              ) != nil,
              journal.localBookID.range(
                of: #"^localbook-[a-f0-9]{64}$"#,
                options: .regularExpression
              ) != nil else {
            throw ReaderNativePDFMutationError.commitFailed(
                "持久 journal 身份无效"
            )
        }
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .millisecondsSince1970
        encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(journal).write(to: url, options: .atomic)
    }

    private func mutationJournalURL(
        originalURL: URL,
        localBookID: String
    ) throws -> URL {
        guard localBookID.range(
            of: #"^localbook-[a-f0-9]{64}$"#,
            options: .regularExpression
        ) != nil else {
            throw ReaderNativePDFMutationError.invalidRequest
        }
        return originalURL.deletingLastPathComponent().appendingPathComponent(
            ".bw-pdf-mutation-\(localBookID).journal.json",
            isDirectory: false
        )
    }

    private func loadJournal(
        from url: URL,
        expectedLocalBookID: String
    ) throws -> DurableJournal {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .millisecondsSince1970
        let journal = try decoder.decode(
            DurableJournal.self,
            from: Data(contentsOf: url)
        )
        guard journal.schema == Self.journalSchema,
              journal.localBookID == expectedLocalBookID,
              journal.ticket.range(
                of: #"^npmt_[a-f0-9]{32}$"#,
                options: .regularExpression
              ) != nil,
              journal.oldIdentity.sha256.range(
                of: #"^[a-f0-9]{64}$"#,
                options: .regularExpression
              ) != nil,
              journal.stagedContentSHA256 == nil
                || journal.stagedContentSHA256!.range(
                    of: #"^[a-f0-9]{64}$"#,
                    options: .regularExpression
                ) != nil else {
            throw ReaderNativePDFMutationError.commitFailed(
                "持久 journal 内容无效"
            )
        }
        return journal
    }

    private static func migrateOCRContentDirectory(
        _ staging: URL,
        stagedContentSHA256: String,
        operation: ReaderNativePDFMutationOperation,
        pivotPage: Int
    ) throws {
        try? FileManager.default.removeItem(
            at: staging.appendingPathComponent("imports", isDirectory: true)
        )
        try migrateOCRPageDirectory(
            staging.appendingPathComponent("pages", isDirectory: true),
            stagedContentSHA256: stagedContentSHA256,
            operation: operation,
            pivotPage: pivotPage,
            selectionCorrections: false
        )
        try migrateOCRPageDirectory(
            staging.appendingPathComponent(
                "overrides/manual",
                isDirectory: true
            ),
            stagedContentSHA256: stagedContentSHA256,
            operation: operation,
            pivotPage: pivotPage,
            selectionCorrections: false
        )
        try migrateOCRPageDirectory(
            staging.appendingPathComponent(
                "overrides/selection",
                isDirectory: true
            ),
            stagedContentSHA256: stagedContentSHA256,
            operation: operation,
            pivotPage: pivotPage,
            selectionCorrections: true
        )
        var availableLayers = Set<NativeBookOCRLayerID>([.embedded])
        if try ocrPageCount(
            staging.appendingPathComponent("pages", isDirectory: true)
        ) > 0 {
            availableLayers.insert(.legacy)
        }
        for layer in NativeBookOCRLayerID.allCases where layer != .legacy {
            let layerDirectory = staging
                .appendingPathComponent("layers", isDirectory: true)
                .appendingPathComponent(layer.rawValue, isDirectory: true)
            let pagesDirectory = layerDirectory.appendingPathComponent(
                "pages",
                isDirectory: true
            )
            try migrateOCRPageDirectory(
                pagesDirectory,
                stagedContentSHA256: stagedContentSHA256,
                operation: operation,
                pivotPage: pivotPage,
                selectionCorrections: false
            )
            let pageCount = try ocrPageCount(pagesDirectory)
            if layer == .embedded {
                continue
            }
            let metadataURL = layerDirectory.appendingPathComponent(
                "metadata.json",
                isDirectory: false
            )
            if pageCount == 0 {
                try? FileManager.default.removeItem(at: layerDirectory)
                continue
            }
            guard FileManager.default.fileExists(atPath: metadataURL.path) else {
                throw ReaderNativePDFMutationError.stagingFailed(
                    "本机 OCR 文字层缺少元数据"
                )
            }
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .millisecondsSince1970
            let metadata = try decoder.decode(
                NativeBookOCRLayerMetadata.self,
                from: Data(contentsOf: metadataURL)
            )
            guard metadata.schema == NativeBookOCRLayerMetadata.schema,
                  metadata.layer == layer else {
                throw ReaderNativePDFMutationError.stagingFailed(
                    "本机 OCR 文字层元数据身份不匹配"
                )
            }
            let migratedMetadata = NativeBookOCRLayerMetadata(
                schema: metadata.schema,
                contentSHA256: stagedContentSHA256.lowercased(),
                layer: layer,
                engine: metadata.engine,
                executor: metadata.executor,
                processingProfile: metadata.processingProfile,
                revision: metadata.revision + "+pdf-page-anchor/1",
                pageCount: pageCount,
                updatedAt: Date()
            )
            let encoder = JSONEncoder()
            encoder.dateEncodingStrategy = .millisecondsSince1970
            encoder.outputFormatting = [.sortedKeys]
            try encoder.encode(migratedMetadata).write(
                to: metadataURL,
                options: .atomic
            )
            availableLayers.insert(layer)
        }
        let selectionURL = staging.appendingPathComponent(
            "active-layer.json",
            isDirectory: false
        )
        if FileManager.default.fileExists(atPath: selectionURL.path) {
            let decoder = JSONDecoder()
            decoder.dateDecodingStrategy = .millisecondsSince1970
            let selection = try decoder.decode(
                NativeBookOCRLayerSelection.self,
                from: Data(contentsOf: selectionURL)
            )
            guard selection.schema == NativeBookOCRLayerSelection.schema else {
                throw ReaderNativePDFMutationError.stagingFailed(
                    "本机 OCR 文字层选择记录无效"
                )
            }
            let migratedSelection = NativeBookOCRLayerSelection(
                schema: selection.schema,
                contentSHA256: stagedContentSHA256.lowercased(),
                selected: availableLayers.contains(selection.selected)
                    ? selection.selected : .embedded,
                updatedAt: Date(),
                // 迁移不改变"这是不是用户自己选的"，原样带过去；只有回落到
                // 内嵌层时才不再算用户拍板（那一层不是他挑的）。
                chosenByUser: availableLayers.contains(selection.selected)
                    ? selection.chosenByUser : false
            )
            let encoder = JSONEncoder()
            encoder.dateEncodingStrategy = .millisecondsSince1970
            encoder.outputFormatting = [.sortedKeys]
            try encoder.encode(migratedSelection).write(
                to: selectionURL,
                options: .atomic
            )
        }
    }

    private static func ocrPageCount(_ directory: URL) throws -> Int {
        guard FileManager.default.fileExists(atPath: directory.path) else {
            return 0
        }
        return try FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ).filter {
            $0.pathExtension == "json"
                && ocrPageNumber($0.lastPathComponent) != nil
        }.count
    }

    private static func migrateOCRPageDirectory(
        _ directory: URL,
        stagedContentSHA256: String,
        operation: ReaderNativePDFMutationOperation,
        pivotPage: Int,
        selectionCorrections: Bool
    ) throws {
        guard FileManager.default.fileExists(atPath: directory.path) else {
            return
        }
        let migrated = directory.deletingLastPathComponent()
            .appendingPathComponent(
                directory.lastPathComponent + ".migrated",
                isDirectory: true
            )
        try? FileManager.default.removeItem(at: migrated)
        try FileManager.default.createDirectory(
            at: migrated,
            withIntermediateDirectories: true
        )
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .millisecondsSince1970
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .millisecondsSince1970
        encoder.outputFormatting = [.sortedKeys]
        do {
            let files = try FileManager.default.contentsOfDirectory(
                at: directory,
                includingPropertiesForKeys: [
                    .isRegularFileKey,
                    .isSymbolicLinkKey,
                ],
                options: [.skipsHiddenFiles]
            )
            for file in files where file.pathExtension == "json" {
                let values = try file.resourceValues(forKeys: [
                    .isRegularFileKey,
                    .isSymbolicLinkKey,
                ])
                guard values.isRegularFile == true,
                      values.isSymbolicLink != true,
                      let oldPage = Self.ocrPageNumber(file.lastPathComponent),
                      let newPage = Self.ocrPageMap(
                        oldPage,
                        operation: operation,
                        pivotPage: pivotPage
                      ) else {
                    continue
                }
                let data: Data
                if selectionCorrections {
                    let value = try decoder.decode(
                        NativeBookOCRSelectionCorrectionEnvelope.self,
                        from: Data(contentsOf: file)
                    )
                    guard value.page == oldPage else {
                        throw ReaderNativePDFMutationError.stagingFailed(
                            "本机 OCR 选区校正页身份不匹配"
                        )
                    }
                    data = try encoder.encode(
                        Self.migratedSelectionCorrections(
                            value,
                            contentSHA256: stagedContentSHA256,
                            page: newPage
                        )
                    )
                } else {
                    let value = try decoder.decode(
                        NativeBookOCRPageCharacters.self,
                        from: Data(contentsOf: file)
                    )
                    guard value.page == oldPage else {
                        throw ReaderNativePDFMutationError.stagingFailed(
                            "本机 OCR 页身份不匹配"
                        )
                    }
                    data = try encoder.encode(
                        try Self.migratedOCRPage(
                            value,
                            contentSHA256: stagedContentSHA256,
                            page: newPage
                        )
                    )
                }
                let target = migrated.appendingPathComponent(
                    String(format: "p%06d.json", newPage),
                    isDirectory: false
                )
                try data.write(to: target, options: .atomic)
            }
            try FileManager.default.removeItem(at: directory)
            try FileManager.default.moveItem(at: migrated, to: directory)
        } catch {
            try? FileManager.default.removeItem(at: migrated)
            throw error
        }
    }

    private static func ocrPageNumber(_ name: String) -> Int? {
        guard let match = name.range(
            of: #"^p([0-9]{6})\.json$"#,
            options: .regularExpression
        ) else { return nil }
        let stem = String(name[match]).dropFirst().dropLast(5)
        return Int(stem)
    }

    private static func ocrPageMap(
        _ page: Int,
        operation: ReaderNativePDFMutationOperation,
        pivotPage: Int
    ) -> Int? {
        switch operation {
        case .insert:
            return page >= pivotPage ? page + 1 : page
        case .delete:
            if page == pivotPage { return nil }
            return page > pivotPage ? page - 1 : page
        case .edit:
            return page == pivotPage ? nil : page
        }
    }

    private static func migratedOCRPage(
        _ value: NativeBookOCRPageCharacters,
        contentSHA256: String,
        page: Int
    ) throws -> NativeBookOCRPageCharacters {
        let digestSeed = [
            "reader-native-page-geometry-migrated/1",
            value.geometryDigest,
            contentSHA256.lowercased(),
            String(page),
        ].joined(separator: "\u{0}")
        let geometryDigest = SHA256.hash(data: Data(digestSeed.utf8)).map {
            String(format: "%02x", $0)
        }.joined()
        let formulaRegions = try value.formulaRegions.map {
            try migratedFormulaRegion(
                $0,
                oldPage: value.page,
                newPage: page
            )
        }
        return NativeBookOCRPageCharacters(
            schema: value.schema,
            contentSHA256: contentSHA256.lowercased(),
            page: page,
            pageWidth: value.pageWidth,
            pageHeight: value.pageHeight,
            rotation: value.rotation,
            geometryDigest: geometryDigest,
            engineRevision: value.engineRevision + "+pdf-page-anchor/1",
            status: value.status,
            source: value.source,
            chars: value.chars,
            layout: value.layout,
            furigana: value.furigana,
            wordSegmentation: value.wordSegmentation,
            characterGeometry: value.characterGeometry,
            formulaCoverage: value.formulaCoverage,
            formulaRegions: formulaRegions,
            createdAt: value.createdAt,
            error: value.error,
            textAuthority: value.textAuthority
        )
    }

    private static func migratedFormulaRegion(
        _ value: NativeBookOCRFormulaRegion,
        oldPage: Int,
        newPage: Int
    ) throws -> NativeBookOCRFormulaRegion {
        let prefix: String
        if value.id.hasPrefix("pi-formula-p") {
            prefix = "pi-formula-p"
        } else if value.id.hasPrefix("formula-p") {
            prefix = "formula-p"
        } else {
            throw ReaderNativePDFMutationError.stagingFailed(
                "本机 OCR 公式区域 ID 不支持安全迁移：\(value.id)"
            )
        }
        let expectedPrefix = "\(prefix)\(oldPage)-"
        guard value.id.hasPrefix(expectedPrefix) else {
            throw ReaderNativePDFMutationError.stagingFailed(
                "本机 OCR 公式区域页身份不匹配：\(value.id)"
            )
        }
        let suffix = value.id.dropFirst(expectedPrefix.count)
        guard !suffix.isEmpty else {
            throw ReaderNativePDFMutationError.stagingFailed(
                "本机 OCR 公式区域 ID 缺少稳定后缀：\(value.id)"
            )
        }
        return NativeBookOCRFormulaRegion(
            id: "\(prefix)\(newPage)-\(suffix)",
            x0: value.x0,
            y0: value.y0,
            x1: value.x1,
            y1: value.y1,
            state: value.state,
            latex: value.latex,
            multiline: value.multiline,
            error: value.error
        )
    }

    private static func migratedSelectionCorrections(
        _ value: NativeBookOCRSelectionCorrectionEnvelope,
        contentSHA256: String,
        page: Int
    ) -> NativeBookOCRSelectionCorrectionEnvelope {
        NativeBookOCRSelectionCorrectionEnvelope(
            schema: value.schema,
            contentSHA256: contentSHA256.lowercased(),
            page: page,
            corrections: value.corrections.map { correction in
                NativeBookOCRSelectionCorrection(
                    schema: correction.schema,
                    id: correction.id,
                    contentSHA256: contentSHA256.lowercased(),
                    page: page,
                    bbox: correction.bbox,
                    text: correction.text,
                    chars: correction.chars,
                    createdAt: correction.createdAt
                )
            }
        )
    }

    private func fileIdentity(_ url: URL) throws -> FileIdentity {
        let values = try url.resourceValues(forKeys: [
            .isRegularFileKey,
            .isSymbolicLinkKey,
            .fileSizeKey,
            .contentModificationDateKey,
        ])
        guard values.isRegularFile == true,
              values.isSymbolicLink != true,
              let byteCount = values.fileSize,
              byteCount > 0,
              let modifiedAt = values.contentModificationDate else {
            throw ReaderNativePDFMutationError.bookChanged
        }
        return FileIdentity(
            byteCount: Int64(byteCount),
            modifiedAt: modifiedAt,
            sha256: try Self.sha256(of: url)
        )
    }

    private func referencePageSize(
        document: PDFDocument,
        insertionIndex: Int
    ) -> CGSize {
        let index = min(max(insertionIndex == document.pageCount
            ? insertionIndex - 1 : insertionIndex, 0), document.pageCount - 1)
        return document.page(at: index).map {
            Self.normalizedPageSize($0.bounds(for: .mediaBox).size)
        } ?? CGSize(width: 612, height: 792)
    }

    private func renderedPage(
        size: CGSize,
        title: String,
        markdown: String
    ) throws -> RenderedPage {
        let bounds = CGRect(origin: .zero, size: Self.normalizedPageSize(size))
        let renderer = UIGraphicsPDFRenderer(bounds: bounds)
        let text = Self.renderedPlainText(title: title, markdown: markdown)
        let margin = max(24, min(bounds.width, bounds.height) * 0.06)
        let textBounds = CGRect(
            x: margin,
            y: margin,
            width: bounds.width - margin * 2,
            height: bounds.height - margin * 3
        )
        let measured = Self.renderedAttributedText(
            title: title,
            text: text,
            scale: 1
        )
        let measuredHeight = measured.boundingRect(
            with: CGSize(
                width: textBounds.width,
                height: CGFloat.greatestFiniteMagnitude
            ),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            context: nil
        ).height
        let scale = measuredHeight > textBounds.height
            ? max(0.25, min(1, textBounds.height / measuredHeight))
            : 1
        let attributed = Self.renderedAttributedText(
            title: title,
            text: text,
            scale: scale
        )
        let finalHeight = attributed.boundingRect(
            with: CGSize(
                width: textBounds.width,
                height: CGFloat.greatestFiniteMagnitude
            ),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            context: nil
        ).height
        var warnings: [String] = []
        if scale < 0.999 {
            warnings.append(
                "内容较多，排版缩小到 \(Int(scale * 100))%"
            )
        }
        if finalHeight > textBounds.height + 1 {
            warnings.append("内容超长，末尾可能未写入 PDF")
        }
        let data = renderer.pdfData { context in
            context.beginPage()
            UIColor.white.setFill()
            context.cgContext.fill(bounds)
            attributed.draw(
                with: textBounds,
                options: [.usesLineFragmentOrigin, .usesFontLeading],
                context: nil
            )
            NSAttributedString(
                // BWPAGE 是校验锚点：CJK 字符经 PDF 提取会变形成部首区
                // 码点（⻚ 在 CJK 部首补充区甚至没有 NFKC 映射），只有
                // ASCII 标记提取永不变形。
                string: "— 用户插入页 · BWPAGE —",
                attributes: [
                    .font: UIFont.systemFont(ofSize: 9),
                    .foregroundColor: UIColor.secondaryLabel,
                ]
            ).draw(at: CGPoint(x: margin, y: bounds.height - margin * 1.4))
        }
        guard let document = PDFDocument(data: data),
              let page = document.page(at: 0),
              Self.probeComparableText(page.string ?? "")
                  .contains("BWPAGE") else {
            throw ReaderNativePDFMutationError.stagingFailed(
                "无法生成带文字层的 PDF 页面"
            )
        }
        return RenderedPage(page: page, warnings: warnings)
    }

    /// PDF 文字提取的码点归一：NFKC 把康熙部首变体映回统一汉字，
    /// 再剥除提取时散布的空白 —— 校验比较的两端都必须过这一层。
    private static func probeComparableText(_ value: String) -> String {
        return value.precomposedStringWithCompatibilityMapping
            .filter { !$0.isWhitespace }
    }

    private static func renderedAttributedText(
        title: String,
        text: String,
        scale: CGFloat
    ) -> NSAttributedString {
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byWordWrapping
        paragraph.lineSpacing = 4 * scale
        let attributed = NSMutableAttributedString()
        if !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            attributed.append(NSAttributedString(
                string: title.trimmingCharacters(in: .whitespacesAndNewlines)
                    + "\n\n",
                attributes: [
                    .font: UIFont.systemFont(
                        ofSize: 22 * scale,
                        weight: .semibold
                    ),
                    .foregroundColor: UIColor.label,
                    .paragraphStyle: paragraph,
                ]
            ))
        }
        attributed.append(NSAttributedString(
            string: text,
            attributes: [
                .font: UIFont.systemFont(ofSize: 15 * scale),
                .foregroundColor: UIColor.label,
                .paragraphStyle: paragraph,
            ]
        ))
        return attributed
    }

    private static func renderedPlainText(
        title: String,
        markdown: String
    ) -> String {
        let plain = markdown
            .replacingOccurrences(
                of: #"(?m)^\s{0,3}#{1,6}\s+"#,
                with: "",
                options: .regularExpression
            )
            .replacingOccurrences(
                of: #"(?m)^\s*(?:[-*+] |\d+[.)]\s+)"#,
                with: "• ",
                options: .regularExpression
            )
            .replacingOccurrences(
                of: #"[*_`]+"#,
                with: "",
                options: .regularExpression
            )
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if plain.isEmpty && title.trimmingCharacters(
            in: .whitespacesAndNewlines
        ).isEmpty {
            return "用户插入页"
        }
        return plain
    }

    private static func normalizedPageSize(_ size: CGSize) -> CGSize {
        guard size.width.isFinite, size.height.isFinite,
              size.width >= 144, size.height >= 144,
              size.width <= 14_400, size.height <= 14_400 else {
            return CGSize(width: 612, height: 792)
        }
        return size
    }

    private static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let bytes = try handle.read(upToCount: 1_048_576) ?? Data()
            if bytes.isEmpty { break }
            hasher.update(data: bytes)
        }
        return hasher.finalize().map {
            String(format: "%02x", $0)
        }.joined()
    }

    private static func randomHex(byteCount: Int) -> String {
        var generator = SystemRandomNumberGenerator()
        return (0..<byteCount).map { _ in
            String(format: "%02x", UInt8.random(in: .min ... .max, using: &generator))
        }.joined()
    }
}

/// Strict main-frame-only WK bridge. The bridge parses a closed command
/// schema and delegates native document lifecycle changes back to the host.
@MainActor
final class ReaderNativePDFMutationBridge:
    NSObject,
    WKScriptMessageHandlerWithReply
{
    static let messageName = "bwNativePDFMutation"
    static let requestContract = "reader-native-pdf-mutation-request/1"
    static let responseContract = "reader-native-pdf-mutation-response/1"

    typealias CommandHandler = @MainActor (
        ReaderNativePDFMutationCommand
    ) async throws -> [String: Any]

    private weak var webView: WKWebView?
    private let trustedBaseURL: URL
    private let commandHandler: CommandHandler

    init(
        webView: WKWebView,
        trustedBaseURL: URL,
        commandHandler: @escaping CommandHandler
    ) {
        self.webView = webView
        self.trustedBaseURL = trustedBaseURL
        self.commandHandler = commandHandler
        super.init()
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        guard message.name == Self.messageName,
              message.frameInfo.isMainFrame,
              let webView,
              message.webView === webView,
              isTrusted(webView.url),
              isTrusted(message.frameInfo.request.url) else {
            replyHandler(nil, ReaderNativePDFMutationError
                .untrustedSource.localizedDescription)
            return
        }
        let command: ReaderNativePDFMutationCommand
        do {
            command = try Self.parse(message.body)
        } catch {
            replyHandler(nil, ReaderNativePDFMutationError
                .invalidRequest.localizedDescription)
            return
        }
        Task { @MainActor [commandHandler] in
            do {
                let value = try await commandHandler(command)
                replyHandler(value, nil)
            } catch {
                replyHandler(nil, error.localizedDescription)
            }
        }
    }

    private func isTrusted(_ url: URL?) -> Bool {
        guard let url else { return false }
        return url.scheme?.lowercased() == trustedBaseURL.scheme?.lowercased()
            && url.host?.lowercased() == trustedBaseURL.host?.lowercased()
            && url.port == trustedBaseURL.port
            && url.path.hasPrefix(trustedBaseURL.path)
            && (url.path.hasSuffix("/shells/pdf.html"))
    }

    private static func parse(_ value: Any) throws
        -> ReaderNativePDFMutationCommand
    {
        guard let body = value as? [String: Any],
              body["contract"] as? String == requestContract,
              let action = body["action"] as? String,
              let requestID = body["requestId"] as? String,
              requestID.range(
                of: #"^npm_[a-f0-9]{24}$"#,
                options: .regularExpression
              ) != nil,
              let localBookID = body["localBookId"] as? String,
              localBookID.range(
                of: #"^localbook-[a-f0-9]{64}$"#,
                options: .regularExpression
              ) != nil else {
            throw ReaderNativePDFMutationError.invalidRequest
        }
        switch action {
        case "prepare":
            guard Set(body.keys) == Set([
                "contract", "action", "requestId", "localBookId",
                "operation", "after", "page", "title", "markdown",
            ]),
            let rawOperation = body["operation"] as? String,
            let operation = ReaderNativePDFMutationOperation(
                rawValue: rawOperation
            ),
            let title = body["title"] as? String,
            let markdown = body["markdown"] as? String,
            title.utf8.count <= 512,
            markdown.utf8.count <= 400_000 else {
                throw ReaderNativePDFMutationError.invalidRequest
            }
            let after = try optionalInteger(body["after"], minimum: 0)
            let page = try optionalInteger(body["page"], minimum: 1)
            guard (operation == .insert && after != nil && page == nil)
                    || (operation != .insert && after == nil && page != nil)
            else {
                throw ReaderNativePDFMutationError.invalidRequest
            }
            return .prepare(ReaderNativePDFMutationPrepareRequest(
                requestID: requestID,
                localBookID: localBookID,
                operation: operation,
                after: after,
                page: page,
                title: title,
                markdown: markdown
            ))
        case "commit", "finalize", "cancel":
            guard Set(body.keys) == Set([
                "contract", "action", "requestId", "localBookId", "ticket",
            ]),
            let ticket = body["ticket"] as? String,
            ticket.range(
                of: #"^npmt_[a-f0-9]{32}$"#,
                options: .regularExpression
            ) != nil else {
                throw ReaderNativePDFMutationError.invalidRequest
            }
            if action == "commit" {
                return .commit(
                    requestID: requestID,
                    localBookID: localBookID,
                    ticket: ticket
                )
            }
            if action == "finalize" {
                return .finalize(
                    requestID: requestID,
                    localBookID: localBookID,
                    ticket: ticket
                )
            }
            return .cancel(
                    requestID: requestID,
                    localBookID: localBookID,
                    ticket: ticket
                )
        case "recover":
            guard Set(body.keys) == Set([
                "contract", "action", "requestId", "localBookId", "ticket",
                "oldContentSHA256", "stagedContentSHA256",
            ]) else {
                throw ReaderNativePDFMutationError.invalidRequest
            }
            let ticket = try optionalString(
                body["ticket"],
                pattern: #"^npmt_[a-f0-9]{32}$"#
            )
            let oldContentSHA256 = try optionalString(
                body["oldContentSHA256"],
                pattern: #"^[a-f0-9]{64}$"#
            )
            let stagedContentSHA256 = try optionalString(
                body["stagedContentSHA256"],
                pattern: #"^[a-f0-9]{64}$"#
            )
            guard (ticket == nil && oldContentSHA256 == nil
                    && stagedContentSHA256 == nil)
                    || (ticket != nil && oldContentSHA256 != nil
                        && stagedContentSHA256 != nil) else {
                throw ReaderNativePDFMutationError.invalidRequest
            }
            return .recover(
                requestID: requestID,
                localBookID: localBookID,
                ticket: ticket,
                oldContentSHA256: oldContentSHA256,
                stagedContentSHA256: stagedContentSHA256
            )
        default:
            throw ReaderNativePDFMutationError.invalidRequest
        }
    }

    private static func optionalInteger(
        _ value: Any?,
        minimum: Int
    ) throws -> Int? {
        if value is NSNull { return nil }
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite,
              number.doubleValue.rounded() == number.doubleValue,
              number.doubleValue >= Double(minimum),
              number.doubleValue <= 10_000_000 else {
            throw ReaderNativePDFMutationError.invalidRequest
        }
        return number.intValue
    }

    private static func optionalString(
        _ value: Any?,
        pattern: String
    ) throws -> String? {
        if value is NSNull { return nil }
        guard let string = value as? String,
              string.range(
                of: pattern,
                options: .regularExpression
              ) != nil else {
            throw ReaderNativePDFMutationError.invalidRequest
        }
        return string
    }
}

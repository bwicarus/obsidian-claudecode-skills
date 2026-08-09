import CryptoKit
import Foundation

struct NativeBookOCRPDFMutationLease: Codable, Equatable, Sendable {
    let bookID: String
    let token: String
    let oldContentSHA256: String
}

struct NativeBookOCRPDFMutationStageReceipt: Sendable {
    let hasSource: Bool
    let hadTarget: Bool
}

actor NativeBookOCRSidecarStore {
    static let shared = NativeBookOCRSidecarStore()

    private static let maximumAttachments = 5_001
    private static let maximumAttachmentBytes = 32 * 1_024 * 1_024
    private static let maximumBundleBytes = 512 * 1_024 * 1_024

    private let fileManager: FileManager
    private let rootURL: URL
    private var pdfMutationLeases: [String: NativeBookOCRPDFMutationLease] = [:]
    private var pdfMutationLeasesByDigest:
        [String: [String: NativeBookOCRPDFMutationLease]] = [:]
    private var pdfMutationTargetLeaseByDigest:
        [String: NativeBookOCRPDFMutationLease] = [:]

    init(
        fileManager: FileManager = .default,
        rootURL: URL? = nil
    ) {
        self.fileManager = fileManager
        let applicationSupport = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first!
        self.rootURL = rootURL ?? applicationSupport.appendingPathComponent(
            "BWReader/NativeBookOCR",
            isDirectory: true
        )
    }

    /// The store actor is the serialization point for every OCR sidecar write.
    /// Calling this after the manager has drained its tasks also waits for all
    /// earlier store messages before fencing later writes for this book.
    func beginPDFMutationLease(
        _ lease: NativeBookOCRPDFMutationLease
    ) throws {
        try validatePDFMutationLease(lease)
        if let active = pdfMutationLeases[lease.bookID] {
            guard active == lease else {
                throw NativeBookOCRError.storage(
                    "本书已有另一项 PDF 改页 OCR 租约"
                )
            }
            return
        }
        try registerPDFMutationDigestLease(
            lease.oldContentSHA256,
            lease: lease
        )
        pdfMutationLeases[lease.bookID] = lease
    }

    func stagePDFMutation(
        lease: NativeBookOCRPDFMutationLease,
        ticket: String,
        stagedContentSHA256: String,
        transform: @Sendable (URL) throws -> Void
    ) throws -> NativeBookOCRPDFMutationStageReceipt {
        try assertPDFMutationWriteAllowed(
            bookID: lease.bookID,
            mutationLease: lease
        )
        try validateContentSHA256(stagedContentSHA256)
        try validatePDFMutationTicket(ticket)
        let stagedDigest = stagedContentSHA256.lowercased()
        if let active = pdfMutationTargetLeaseByDigest[stagedDigest],
           active != lease {
            throw NativeBookOCRError.storage(
                "目标 PDF 内容已有另一项 OCR 改页租约"
            )
        }
        try registerPDFMutationDigestLease(stagedDigest, lease: lease)
        pdfMutationTargetLeaseByDigest[stagedDigest] = lease

        let contentRoot = rootURL.appendingPathComponent(
            "content",
            isDirectory: true
        )
        let source = contentRoot.appendingPathComponent(
            lease.oldContentSHA256.lowercased(),
            isDirectory: true
        )
        let staging = contentRoot.appendingPathComponent(
            ".bw-pdf-mutation-\(ticket).staging",
            isDirectory: true
        )
        let target = contentRoot.appendingPathComponent(
            stagedDigest,
            isDirectory: true
        )
        let backup = contentRoot.appendingPathComponent(
            ".bw-pdf-mutation-\(ticket).backup",
            isDirectory: true
        )
        let hasSource = lease.oldContentSHA256.lowercased() != stagedDigest
            && fileManager.fileExists(atPath: source.path)
        let hadTarget = fileManager.fileExists(atPath: target.path)
        guard hasSource else {
            return NativeBookOCRPDFMutationStageReceipt(
                hasSource: false,
                hadTarget: hadTarget
            )
        }
        do {
            try fileManager.createDirectory(
                at: contentRoot,
                withIntermediateDirectories: true
            )
            guard !fileManager.fileExists(atPath: staging.path),
                  !fileManager.fileExists(atPath: backup.path) else {
                throw NativeBookOCRError.storage(
                    "本机 OCR staging/backup 已存在"
                )
            }
            try fileManager.copyItem(at: source, to: staging)
            try transform(staging)
            return NativeBookOCRPDFMutationStageReceipt(
                hasSource: true,
                hadTarget: hadTarget
            )
        } catch {
            try? fileManager.removeItem(at: staging)
            throw error
        }
    }

    func installPDFMutation(
        lease: NativeBookOCRPDFMutationLease,
        ticket: String,
        stagedContentSHA256: String,
        hasSource: Bool,
        hadTarget: Bool
    ) throws {
        try assertPDFMutationFileAccess(
            lease: lease,
            ticket: ticket,
            stagedContentSHA256: stagedContentSHA256
        )
        guard hasSource else { return }
        let urls = pdfMutationURLs(
            ticket: ticket,
            stagedContentSHA256: stagedContentSHA256
        )
        guard fileManager.fileExists(atPath: urls.staging.path) else {
            throw NativeBookOCRError.storage("本机 OCR staging 不存在")
        }
        if hadTarget {
            guard fileManager.fileExists(atPath: urls.target.path),
                  !fileManager.fileExists(atPath: urls.backup.path) else {
                throw NativeBookOCRError.storage(
                    "本机 OCR 目标/备份状态不一致"
                )
            }
            try fileManager.moveItem(at: urls.target, to: urls.backup)
        } else if fileManager.fileExists(atPath: urls.target.path) {
            throw NativeBookOCRError.storage(
                "本机 OCR 目标在事务期间意外出现"
            )
        }
        try fileManager.moveItem(at: urls.staging, to: urls.target)
    }

    func rollbackPDFMutation(
        lease: NativeBookOCRPDFMutationLease,
        ticket: String,
        stagedContentSHA256: String,
        hadTarget: Bool,
        mayHaveInstalled: Bool
    ) throws {
        try assertPDFMutationFileAccess(
            lease: lease,
            ticket: ticket,
            stagedContentSHA256: stagedContentSHA256
        )
        let urls = pdfMutationURLs(
            ticket: ticket,
            stagedContentSHA256: stagedContentSHA256
        )
        if fileManager.fileExists(atPath: urls.backup.path) {
            if fileManager.fileExists(atPath: urls.target.path) {
                try fileManager.removeItem(at: urls.target)
            }
            try fileManager.moveItem(at: urls.backup, to: urls.target)
        } else if !hadTarget, mayHaveInstalled,
                  fileManager.fileExists(atPath: urls.target.path) {
            try fileManager.removeItem(at: urls.target)
        }
        try? fileManager.removeItem(at: urls.staging)
    }

    func cleanupPDFMutationArtifacts(
        lease: NativeBookOCRPDFMutationLease,
        ticket: String,
        stagedContentSHA256: String?
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: lease.bookID,
            mutationLease: lease
        )
        try validatePDFMutationTicket(ticket)
        let contentRoot = rootURL.appendingPathComponent(
            "content",
            isDirectory: true
        )
        for suffix in [".staging", ".backup"] {
            let url = contentRoot.appendingPathComponent(
                ".bw-pdf-mutation-\(ticket)\(suffix)",
                isDirectory: true
            )
            if fileManager.fileExists(atPath: url.path) {
                try fileManager.removeItem(at: url)
            }
        }
        if let stagedContentSHA256 {
            try validateContentSHA256(stagedContentSHA256)
        }
    }

    func finishPDFMutationLease(
        _ lease: NativeBookOCRPDFMutationLease
    ) throws {
        try validatePDFMutationLease(lease)
        guard let active = pdfMutationLeases[lease.bookID] else {
            let conflicting = pdfMutationLeasesByDigest.values.contains {
                $0[lease.bookID] != nil
            } || pdfMutationTargetLeaseByDigest.values.contains {
                $0.bookID == lease.bookID
            }
            guard !conflicting else {
                throw NativeBookOCRError.storage(
                    "PDF 改页 OCR 租约索引状态不一致"
                )
            }
            return
        }
        guard active == lease else {
            throw NativeBookOCRError.storage(
                "拒绝结束另一项 PDF 改页 OCR 租约"
            )
        }
        pdfMutationLeases.removeValue(forKey: lease.bookID)
        for digest in Array(pdfMutationLeasesByDigest.keys) {
            guard var leases = pdfMutationLeasesByDigest[digest],
                  leases[lease.bookID] == lease else { continue }
            leases.removeValue(forKey: lease.bookID)
            if leases.isEmpty {
                pdfMutationLeasesByDigest.removeValue(forKey: digest)
            } else {
                pdfMutationLeasesByDigest[digest] = leases
            }
        }
        pdfMutationTargetLeaseByDigest =
            pdfMutationTargetLeaseByDigest.filter { $0.value != lease }
    }

    func page(
        contentSHA256: String,
        page: Int
    ) throws -> NativeBookOCRPageCharacters? {
        try validateContentSHA256(contentSHA256)
        guard page >= 1 else { return nil }
        let base = try basePage(contentSHA256: contentSHA256, page: page)
        let manual = try manualPage(contentSHA256: contentSHA256, page: page)
        let corrections = try selectionCorrections(
            contentSHA256: contentSHA256,
            page: page
        )
        guard let selected = manual ?? base else {
            if !corrections.isEmpty {
                throw NativeBookOCRError.storage("选区校正缺少基础文字页")
            }
            return nil
        }
        return Self.applyingSelectionCorrections(corrections, to: selected)
    }

    func basePage(
        contentSHA256: String,
        page: Int
    ) throws -> NativeBookOCRPageCharacters? {
        let url = pageURL(
            contentSHA256: contentSHA256,
            page: page,
            beneath: contentDirectory(contentSHA256)
        )
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        do {
            let value = try decoder().decode(
                NativeBookOCRPageCharacters.self,
                from: Data(contentsOf: url)
            )
            guard value.schema == NativeBookOCRPageCharacters.schema,
                  value.contentSHA256 == contentSHA256.lowercased(),
                  value.page == page else {
                throw NativeBookOCRError.storage("页 sidecar 身份不匹配")
            }
            return value
        } catch let error as NativeBookOCRError {
            throw error
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func writeManualPageOverride(
        _ value: NativeBookOCRPageCharacters,
        bookID: String
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: value.contentSHA256
        )
        try validateContentSHA256(value.contentSHA256)
        guard value.schema == NativeBookOCRPageCharacters.schema,
              value.page >= 1,
              value.pageWidth > 0,
              value.pageHeight > 0,
              value.textAuthority == .localOverride else {
            throw NativeBookOCRError.storage("单页手动覆盖结构无效")
        }
        let url = manualPageURL(
            contentSHA256: value.contentSHA256,
            page: value.page
        )
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(value).write(to: url, options: .atomic)
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func clearManualPageOverride(
        bookID: String,
        contentSHA256: String,
        page: Int
    ) throws -> Bool {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: contentSHA256
        )
        try validateContentSHA256(contentSHA256)
        guard page >= 1 else { return false }
        let url = manualPageURL(contentSHA256: contentSHA256, page: page)
        guard fileManager.fileExists(atPath: url.path) else { return false }
        do {
            try fileManager.removeItem(at: url)
            return true
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func appendSelectionCorrection(
        _ correction: NativeBookOCRSelectionCorrection,
        bookID: String
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: correction.contentSHA256
        )
        try validateContentSHA256(correction.contentSHA256)
        guard correction.schema == NativeBookOCRSelectionCorrection.schema,
              correction.page >= 1,
              correction.id.range(
                of: #"^ocrfix-[0-9a-f]{32}$"#,
                options: .regularExpression
              ) != nil,
              correction.bbox.count == 4,
              correction.bbox.allSatisfy({ $0.isFinite && $0 >= 0 }),
              correction.bbox[2] > correction.bbox[0],
              correction.bbox[3] > correction.bbox[1],
              !correction.text.trimmingCharacters(
                in: .whitespacesAndNewlines
              ).isEmpty,
              !correction.chars.isEmpty else {
            throw NativeBookOCRError.storage("选区校正结构无效")
        }
        var corrections = try selectionCorrections(
            contentSHA256: correction.contentSHA256,
            page: correction.page
        )
        guard corrections.count < 256,
              !corrections.contains(where: { $0.id == correction.id }) else {
            throw NativeBookOCRError.storage("选区校正数量或身份无效")
        }
        corrections.append(correction)
        let envelope = NativeBookOCRSelectionCorrectionEnvelope(
            schema: NativeBookOCRSelectionCorrectionEnvelope.schema,
            contentSHA256: correction.contentSHA256.lowercased(),
            page: correction.page,
            corrections: corrections
        )
        let url = selectionCorrectionURL(
            contentSHA256: correction.contentSHA256,
            page: correction.page
        )
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(envelope).write(to: url, options: .atomic)
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    private func manualPage(
        contentSHA256: String,
        page: Int
    ) throws -> NativeBookOCRPageCharacters? {
        let url = manualPageURL(contentSHA256: contentSHA256, page: page)
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        do {
            let value = try decoder().decode(
                NativeBookOCRPageCharacters.self,
                from: Data(contentsOf: url)
            )
            guard value.schema == NativeBookOCRPageCharacters.schema,
                  value.contentSHA256 == contentSHA256.lowercased(),
                  value.page == page,
                  value.textAuthority == .localOverride else {
                throw NativeBookOCRError.storage("单页手动覆盖身份不匹配")
            }
            return value
        } catch let error as NativeBookOCRError {
            throw error
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    private func selectionCorrections(
        contentSHA256: String,
        page: Int
    ) throws -> [NativeBookOCRSelectionCorrection] {
        let url = selectionCorrectionURL(
            contentSHA256: contentSHA256,
            page: page
        )
        guard fileManager.fileExists(atPath: url.path) else { return [] }
        do {
            let value = try decoder().decode(
                NativeBookOCRSelectionCorrectionEnvelope.self,
                from: Data(contentsOf: url)
            )
            guard value.schema == NativeBookOCRSelectionCorrectionEnvelope.schema,
                  value.contentSHA256 == contentSHA256.lowercased(),
                  value.page == page,
                  value.corrections.count <= 256,
                  value.corrections.allSatisfy({ correction in
                    correction.schema == NativeBookOCRSelectionCorrection.schema
                        && correction.contentSHA256 == contentSHA256.lowercased()
                        && correction.page == page
                        && correction.bbox.count == 4
                        && !correction.chars.isEmpty
                  }) else {
                throw NativeBookOCRError.storage("选区校正身份不匹配")
            }
            return value.corrections
        } catch let error as NativeBookOCRError {
            throw error
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    private static func applyingSelectionCorrections(
        _ corrections: [NativeBookOCRSelectionCorrection],
        to initial: NativeBookOCRPageCharacters
    ) -> NativeBookOCRPageCharacters {
        guard !corrections.isEmpty else { return initial }
        var chars = initial.chars
        var furigana = initial.furigana
        var formulaRegions = initial.formulaRegions
        var latest = initial.createdAt
        var revisionParts = [initial.engineRevision]
        for correction in corrections.sorted(by: { $0.createdAt < $1.createdAt }) {
            let box = correction.bbox
            func contains(_ x0: Double, _ y0: Double, _ x1: Double, _ y1: Double) -> Bool {
                let x = (x0 + x1) / 2
                let y = (y0 + y1) / 2
                return x >= box[0] && x <= box[2]
                    && y >= box[1] && y <= box[3]
            }
            let firstRemoved = chars.firstIndex(where: {
                contains($0.x0, $0.y0, $0.x1, $0.y1)
            }) ?? chars.count
            chars.removeAll(where: {
                contains($0.x0, $0.y0, $0.x1, $0.y1)
            })
            chars.insert(
                contentsOf: correction.chars,
                at: min(firstRemoved, chars.count)
            )
            furigana.removeAll(where: { item in
                guard let x0 = item.x0, let y0 = item.y0,
                      let x1 = item.x1, let y1 = item.y1 else { return false }
                return contains(x0, y0, x1, y1)
            })
            formulaRegions.removeAll(where: { region in
                !(region.x1 < box[0] || region.x0 > box[2]
                    || region.y1 < box[1] || region.y0 > box[3])
            })
            latest = max(latest, correction.createdAt)
            revisionParts.append("selection:\(correction.id)")
        }
        let meaningful = chars.filter { $0.sp == 0 }
        return NativeBookOCRPageCharacters(
            schema: initial.schema,
            contentSHA256: initial.contentSHA256,
            page: initial.page,
            pageWidth: initial.pageWidth,
            pageHeight: initial.pageHeight,
            rotation: initial.rotation,
            geometryDigest: initial.geometryDigest,
            engineRevision: revisionParts.joined(separator: "+"),
            status: meaningful.isEmpty ? .readyEmpty : .ready,
            source: .apple,
            chars: chars,
            furigana: furigana,
            wordSegmentation: initial.wordSegmentation,
            characterGeometry: initial.characterGeometry,
            formulaCoverage: formulaRegions.isEmpty
                ? .unavailable : initial.formulaCoverage,
            formulaRegions: formulaRegions,
            createdAt: latest,
            error: nil,
            textAuthority: .localOverride
        )
    }

    func pages(contentSHA256: String) throws -> [NativeBookOCRPageCharacters] {
        try validateContentSHA256(contentSHA256)
        let directory = contentDirectory(contentSHA256)
            .appendingPathComponent("pages", isDirectory: true)
        guard fileManager.fileExists(atPath: directory.path) else { return [] }
        do {
            return try fileManager.contentsOfDirectory(
                at: directory,
                includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey],
                options: [.skipsHiddenFiles]
            ).filter { url in
                guard url.pathExtension == "json" else { return false }
                let values = try? url.resourceValues(
                    forKeys: [.isRegularFileKey, .isSymbolicLinkKey]
                )
                return values?.isRegularFile == true
                    && values?.isSymbolicLink != true
            }.compactMap { url in
                guard let value = try? decoder().decode(
                    NativeBookOCRPageCharacters.self,
                    from: Data(contentsOf: url)
                ), value.schema == NativeBookOCRPageCharacters.schema,
                   value.contentSHA256 == contentSHA256.lowercased(),
                   value.page >= 1 else {
                    return nil
                }
                return value
            }.sorted { $0.page < $1.page }
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func effectivePages(
        contentSHA256: String
    ) throws -> [NativeBookOCRPageCharacters] {
        try pages(contentSHA256: contentSHA256).map { base in
            try page(contentSHA256: contentSHA256, page: base.page) ?? base
        }
    }

    func writePage(
        _ value: NativeBookOCRPageCharacters,
        bookID: String
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: value.contentSHA256
        )
        try validateContentSHA256(value.contentSHA256)
        guard value.schema == NativeBookOCRPageCharacters.schema,
              value.page >= 1,
              value.pageWidth > 0,
              value.pageHeight > 0 else {
            throw NativeBookOCRError.storage("页 sidecar 结构无效")
        }
        let contentDirectory = contentDirectory(value.contentSHA256)
        let url = pageURL(
            contentSHA256: value.contentSHA256,
            page: value.page,
            beneath: contentDirectory
        )
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(value).write(to: url, options: .atomic)
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func writeStatus(
        _ status: NativeBookOCRBookStatus,
        mutationLease: NativeBookOCRPDFMutationLease? = nil
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: status.bookID,
            contentSHA256: status.contentSHA256.isEmpty
                ? nil : status.contentSHA256,
            mutationLease: mutationLease
        )
        guard status.schema == NativeBookOCRBookStatus.schema,
              !status.bookID.isEmpty,
              status.contentSHA256.isEmpty
                || Self.isSHA256(status.contentSHA256) else {
            throw NativeBookOCRError.storage("任务状态无效")
        }
        let url = statusURL(bookID: status.bookID)
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(status).write(to: url, options: .atomic)
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    func loadStatuses() throws -> [NativeBookOCRBookStatus] {
        let directory = rootURL.appendingPathComponent("jobs", isDirectory: true)
        guard fileManager.fileExists(atPath: directory.path) else { return [] }
        do {
            return try fileManager.contentsOfDirectory(
                at: directory,
                includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey],
                options: [.skipsHiddenFiles]
            ).compactMap { url in
                let values = try? url.resourceValues(
                    forKeys: [.isRegularFileKey, .isSymbolicLinkKey]
                )
                guard url.pathExtension == "json",
                      values?.isRegularFile == true,
                      values?.isSymbolicLink != true,
                      let status = try? decoder().decode(
                        NativeBookOCRBookStatus.self,
                        from: Data(contentsOf: url)
                      ), status.schema == NativeBookOCRBookStatus.schema,
                      !status.bookID.isEmpty,
                      status.contentSHA256.isEmpty
                        || Self.isSHA256(status.contentSHA256) else {
                    return nil
                }
                return status.state == .running
                    ? NativeBookOCRBookStatus(
                        schema: status.schema,
                        bookID: status.bookID,
                        contentSHA256: status.contentSHA256,
                        state: .paused,
                        source: status.source,
                        totalPages: status.totalPages,
                        currentPage: nil,
                        textProgress: status.textProgress,
                        wordProgress: status.wordProgress,
                        formulaProgress: status.formulaProgress,
                        formulaPendingRegions: status.formulaPendingRegions,
                        formulaFailedRegions: status.formulaFailedRegions,
                        message: "App 上次退出时已保存进度，可继续处理",
                        updatedAt: Date()
                      )
                    : status
            }
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    /// A content-addressed receipt survives view/model recreation and lets the
    /// network coordinator skip downloading the same immutable Pi revision.
    /// A corrupt receipt is an error rather than a cache miss: silently
    /// re-importing bytes would hide local storage damage.
    func hasImportedRevision(
        contentSHA256: String,
        revision: String
    ) throws -> Bool {
        try validateContentSHA256(contentSHA256)
        try validateRevision(revision)
        let url = importReceiptURL(
            revision: revision,
            beneath: contentDirectory(contentSHA256)
        )
        guard fileManager.fileExists(atPath: url.path) else { return false }
        do {
            let values = try url.resourceValues(forKeys: [
                .isRegularFileKey, .isSymbolicLinkKey,
            ])
            guard values.isRegularFile == true,
                  values.isSymbolicLink != true else {
                throw NativeBookOCRError.storage("导入回执文件无效")
            }
            let receipt = try decoder().decode(
                ImportedRevisionReceipt.self,
                from: Data(contentsOf: url)
            )
            guard receipt.schema == ImportedRevisionReceipt.schema,
                  receipt.contentSHA256 == contentSHA256.lowercased(),
                  receipt.revision == revision else {
                throw NativeBookOCRError.storage("导入回执身份不匹配")
            }
            return true
        } catch let error as NativeBookOCRError {
            throw error
        } catch {
            throw NativeBookOCRError.storage(error.localizedDescription)
        }
    }

    /// Imports a complete immutable Pi attachment revision. All bytes and all
    /// payload identities are validated before a staged content directory is
    /// exchanged, so a failed download cannot partially replace local OCR.
    /// The network layer supplies bytes by opaque attachmentId; downloadUrl is
    /// validated metadata and is never opened by this store.
    func importDerivedAttachments(
        bookID: String,
        expectedContentSHA256: String,
        manifest: NativeBookOCRDerivedAttachmentManifest,
        files: [String: Data]
    ) throws -> NativeBookOCRImportResult {
        try assertPDFMutationWriteAllowed(
            bookID: bookID,
            contentSHA256: expectedContentSHA256
        )
        try validateContentSHA256(expectedContentSHA256)
        guard expectedContentSHA256.caseInsensitiveCompare(
            manifest.contentSha256
        ) == .orderedSame else {
            throw NativeBookOCRError.invalidAttachment("本机书籍摘要与附件不匹配")
        }
        try validate(manifest: manifest, files: files)
        let contentSHA256 = manifest.contentSha256.lowercased()
        let target = contentDirectory(contentSHA256)
        var importedPages: [Int] = []
        var importedFormulaPages: [Int] = []
        var incomingPages: [Int: NativeBookOCRPageCharacters] = [:]
        var formulaEnvelope: PiFormulaEnvelope?

        for entry in manifest.files {
            guard let data = files[entry.attachmentId] else {
                throw NativeBookOCRError.invalidAttachment(
                    "缺少附件 \(entry.attachmentId)"
                )
            }
            switch entry.kind {
            case "ocr-page-chars":
                let value = try decoder().decode(PiPageCharacters.self, from: data)
                let expectedAttachmentID = String(
                    format: "ocr-page-%06d",
                    value.pageNumber
                )
                guard entry.attachmentId == expectedAttachmentID,
                      entry.page.map({ $0 == value.pageNumber }) ?? true else {
                    throw NativeBookOCRError.invalidAttachment(
                        "文字页附件页码不匹配"
                    )
                }
                let converted = try convertPiPage(
                    value,
                    expectedContentSHA256: contentSHA256,
                    expectedBookID: manifest.bookId
                )
                guard incomingPages.updateValue(
                    converted,
                    forKey: converted.page
                ) == nil else {
                    throw NativeBookOCRError.invalidAttachment("页附件重复")
                }
                importedPages.append(converted.page)
            case "ocr-formula-regions":
                guard formulaEnvelope == nil else {
                    throw NativeBookOCRError.invalidAttachment("公式附件重复")
                }
                formulaEnvelope = try decoder().decode(
                    PiFormulaEnvelope.self,
                    from: data
                )
            default:
                throw NativeBookOCRError.invalidAttachment(
                    "不支持的附件类型 \(entry.kind)"
                )
            }
        }

        var mergedPages: [Int: NativeBookOCRPageCharacters] = [:]
        for value in try pages(contentSHA256: contentSHA256) {
            guard mergedPages.updateValue(value, forKey: value.page) == nil else {
                throw NativeBookOCRError.invalidAttachment("本机页 sidecar 重复")
            }
        }
        for (page, value) in incomingPages {
            mergedPages[page] = value
        }
        if let formulaEnvelope {
            guard formulaEnvelope.schema == PiFormulaEnvelope.schema,
                  formulaEnvelope.bookId == manifest.bookId,
                  formulaEnvelope.contentSha256 == contentSHA256 else {
                throw NativeBookOCRError.invalidAttachment("公式附件身份不匹配")
            }
            let grouped = Dictionary(grouping: formulaEnvelope.formulas, by: \.page)
            for (pageNumber, formulas) in grouped {
                guard let page = mergedPages[pageNumber] else {
                    throw NativeBookOCRError.invalidAttachment(
                        "公式页 \(pageNumber) 没有对应文字页几何"
                    )
                }
                let regions = try formulas.enumerated().map { offset, formula in
                    try convertPiFormula(
                        formula,
                        offset: offset,
                        page: page
                    )
                }
                let attachment = NativeBookOCRFormulaAttachment(
                    schema: NativeBookOCRFormulaAttachment.schema,
                    contentSHA256: contentSHA256,
                    page: pageNumber,
                    geometryDigest: page.geometryDigest,
                    engineRevision: "pi-formula/1",
                    formulaCoverage: .complete,
                    formulaRegions: regions,
                    createdAt: Date()
                )
                mergedPages[pageNumber] = page.replacingFormulaAttachment(
                    attachment,
                    source: .pi
                )
                importedFormulaPages.append(pageNumber)
            }
            // A valid formula export is the complete result for this immutable
            // revision. Imported text pages absent from its region list were
            // examined and contain no detected formulas.
            for pageNumber in incomingPages.keys where grouped[pageNumber] == nil {
                guard let page = mergedPages[pageNumber] else { continue }
                let attachment = NativeBookOCRFormulaAttachment(
                    schema: NativeBookOCRFormulaAttachment.schema,
                    contentSHA256: contentSHA256,
                    page: pageNumber,
                    geometryDigest: page.geometryDigest,
                    engineRevision: "pi-formula/1",
                    formulaCoverage: .complete,
                    formulaRegions: [],
                    createdAt: Date()
                )
                mergedPages[pageNumber] = page.replacingFormulaAttachment(
                    attachment,
                    source: .pi
                )
                importedFormulaPages.append(pageNumber)
            }
        }

        let stagingRoot = rootURL.appendingPathComponent(
            ".import-\(UUID().uuidString)",
            isDirectory: true
        )
        do {
            try fileManager.createDirectory(
                at: rootURL,
                withIntermediateDirectories: true
            )
            if fileManager.fileExists(atPath: target.path) {
                try fileManager.copyItem(at: target, to: stagingRoot)
            } else {
                try fileManager.createDirectory(
                    at: stagingRoot,
                    withIntermediateDirectories: true
                )
            }
            let stagingPages = stagingRoot.appendingPathComponent(
                "pages",
                isDirectory: true
            )
            if fileManager.fileExists(atPath: stagingPages.path) {
                try fileManager.removeItem(at: stagingPages)
            }
            try fileManager.createDirectory(
                at: stagingPages,
                withIntermediateDirectories: true
            )
            for value in mergedPages.values {
                let url = pageURL(
                    contentSHA256: contentSHA256,
                    page: value.page,
                    beneath: stagingRoot
                )
                try fileManager.createDirectory(
                    at: url.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                try encoder().encode(value).write(to: url, options: .atomic)
            }
            let receipt = ImportedRevisionReceipt(
                schema: ImportedRevisionReceipt.schema,
                contentSHA256: contentSHA256,
                revision: manifest.revision,
                importedAt: Date()
            )
            let receiptURL = importReceiptURL(
                revision: manifest.revision,
                beneath: stagingRoot
            )
            try fileManager.createDirectory(
                at: receiptURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try encoder().encode(receipt).write(to: receiptURL, options: .atomic)
            if fileManager.fileExists(atPath: target.path) {
                _ = try fileManager.replaceItemAt(
                    target,
                    withItemAt: stagingRoot,
                    backupItemName: nil,
                    options: []
                )
            } else {
                try fileManager.createDirectory(
                    at: target.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                try fileManager.moveItem(at: stagingRoot, to: target)
            }
        } catch {
            try? fileManager.removeItem(at: stagingRoot)
            if let nativeError = error as? NativeBookOCRError {
                throw nativeError
            }
            throw NativeBookOCRError.invalidAttachment(error.localizedDescription)
        }
        return NativeBookOCRImportResult(
            contentSHA256: contentSHA256,
            importedPages: importedPages.sorted(),
            importedFormulaPages: Array(Set(importedFormulaPages)).sorted()
        )
    }

    private struct ImportedRevisionReceipt: Codable {
        static let schema = "reader-native-book-ocr-import-receipt/1"

        let schema: String
        let contentSHA256: String
        let revision: String
        let importedAt: Date
    }

    private struct PiPageCharacters: Decodable {
        static let schema = "reader-page-chars/1"

        let schema: String
        let bookId: String
        let contentSha256: String
        let engine: String
        let pageNumber: Int
        let pageWidth: Double
        let pageHeight: Double
        let imageWidth: Int
        let imageHeight: Int
        let chars: [NativeBookOCRCharacter]
        let furigana: [NativeBookOCRFurigana]
        let tokenized: Bool?
        let generatedAtEpochMs: Int64

        enum CodingKeys: String, CodingKey {
            case schema, bookId, contentSha256, engine, pageNumber
            case pageWidth = "page_w"
            case pageHeight = "page_h"
            case imageWidth, imageHeight, chars, furigana, tokenized
            case generatedAtEpochMs
        }
    }

    private struct PiFormulaEnvelope: Decodable {
        static let schema = "reader-formula-regions/1"

        let schema: String
        let bookId: String
        let contentSha256: String
        let formulas: [Formula]

        struct Formula: Decodable {
            let page: Int
            let bbox: [Double]
            let latex: String?
            let multiline: Bool?
        }
    }

    private func convertPiPage(
        _ value: PiPageCharacters,
        expectedContentSHA256: String,
        expectedBookID: String
    ) throws -> NativeBookOCRPageCharacters {
        guard value.schema == PiPageCharacters.schema,
              value.bookId == expectedBookID,
              value.contentSha256 == expectedContentSHA256,
              value.pageNumber >= 1,
              value.pageWidth.isFinite, value.pageHeight.isFinite,
              value.pageWidth > 0, value.pageHeight > 0,
              value.imageWidth > 0, value.imageHeight > 0,
              ["vision", "manga"].contains(value.engine) else {
            throw NativeBookOCRError.invalidAttachment("文字页附件身份或几何无效")
        }
        guard value.chars.count <= 250_000,
              value.chars.allSatisfy({ character in
                !character.c.isEmpty
                    && character.c.count <= 16
                    && character.x0.isFinite && character.y0.isFinite
                    && character.x1.isFinite && character.y1.isFinite
                    && character.x0 <= character.x1
                    && character.y0 <= character.y1
                    && character.x0 >= -1 && character.y0 >= -1
                    && character.x1 <= value.pageWidth + 1
                    && character.y1 <= value.pageHeight + 1
              }) else {
            throw NativeBookOCRError.invalidAttachment("文字页字符层无效")
        }
        let geometry = NativeBookOCRPageGeometry(
            page: value.pageNumber,
            cropBoxX: 0,
            cropBoxY: 0,
            pageWidth: value.pageWidth,
            pageHeight: value.pageHeight,
            rotation: 0,
            renderPixelWidth: value.imageWidth,
            renderPixelHeight: value.imageHeight
        )
        let assigned = value.chars.filter { $0.sp == 0 && $0.w >= 0 }.count
        let meaningful = value.chars.filter { $0.sp == 0 }.count
        let segmentation: NativeBookOCRWordSegmentationState
        if meaningful == 0 || assigned == 0 {
            segmentation = .unavailable
        } else if assigned < meaningful {
            segmentation = .partial
        } else {
            segmentation = .ready
        }
        let revision = "pi-\(value.engine)/1"
        return NativeBookOCRPageCharacters(
            schema: NativeBookOCRPageCharacters.schema,
            contentSHA256: expectedContentSHA256,
            page: value.pageNumber,
            pageWidth: value.pageWidth,
            pageHeight: value.pageHeight,
            rotation: 0,
            geometryDigest: NativeBookOCRProcessor.geometryDigest(
                contentSHA256: expectedContentSHA256,
                geometry: geometry,
                engineRevision: revision
            ),
            engineRevision: revision,
            status: meaningful == 0 ? .readyEmpty : .ready,
            source: .pi,
            chars: value.chars,
            furigana: value.furigana,
            wordSegmentation: segmentation,
            // The Pi manga engine currently divides a recognized line box
            // across characters. Keep that approximation visible instead of
            // claiming exact per-character geometry.
            characterGeometry: value.engine == "vision" ? .exact : .estimated,
            formulaCoverage: .unknown,
            formulaRegions: [],
            createdAt: Date(
                timeIntervalSince1970: Double(value.generatedAtEpochMs) / 1_000
            ),
            error: nil
        )
    }

    private func convertPiFormula(
        _ value: PiFormulaEnvelope.Formula,
        offset: Int,
        page: NativeBookOCRPageCharacters
    ) throws -> NativeBookOCRFormulaRegion {
        guard value.page == page.page,
              value.bbox.count == 4,
              value.bbox.allSatisfy({ $0.isFinite && $0 >= 0 && $0 <= 1 }),
              value.bbox[0] < value.bbox[2],
              value.bbox[1] < value.bbox[3],
              (value.latex?.count ?? 0) <= 20_000 else {
            throw NativeBookOCRError.invalidAttachment("公式区域无效")
        }
        let latex = value.latex?.trimmingCharacters(in: .whitespacesAndNewlines)
        return NativeBookOCRFormulaRegion(
            id: "pi-formula-p\(page.page)-\(offset + 1)",
            x0: value.bbox[0] * page.pageWidth,
            y0: value.bbox[1] * page.pageHeight,
            x1: value.bbox[2] * page.pageWidth,
            y1: value.bbox[3] * page.pageHeight,
            state: latex?.isEmpty == false ? .ready : .failed,
            latex: latex?.isEmpty == false ? latex : nil,
            multiline: value.multiline,
            error: latex?.isEmpty == false ? nil : "Pi 未能识别该公式"
        )
    }

    private func validate(
        manifest: NativeBookOCRDerivedAttachmentManifest,
        files: [String: Data]
    ) throws {
        try validateContentSHA256(manifest.contentSha256)
        guard manifest.contract == NativeBookOCRDerivedAttachmentManifest.contract,
              manifest.schema == 1,
              !manifest.bookId.isEmpty,
              manifest.category == "derived",
              manifest.mergePolicy == "immutable",
              manifest.revision.range(
                of: #"^ocr_[0-9a-f]{20}$"#,
                options: .regularExpression
              ) != nil,
              !manifest.files.isEmpty,
              manifest.files.count <= Self.maximumAttachments,
              files.count == manifest.files.count else {
            throw NativeBookOCRError.invalidAttachment("附件清单无效")
        }
        var seen = Set<String>()
        var aggregateBytes: Int64 = 0
        for entry in manifest.files {
            guard seen.insert(entry.attachmentId).inserted,
                  entry.category == "derived",
                  entry.mergePolicy == "immutable",
                  entry.mediaType == "application/json",
                  entry.size >= 0,
                  entry.size <= Self.maximumAttachmentBytes,
                  Self.isSHA256(entry.sha256),
                  let data = files[entry.attachmentId],
                  Int64(data.count) == entry.size,
                  Self.sha256(data) == entry.sha256.lowercased(),
                  ["ocr-page-chars", "ocr-formula-regions"].contains(entry.kind),
                  entry.page.map { (1...100_000).contains($0) } ?? true,
                  Self.isValidAttachmentID(entry.attachmentId),
                  entry.downloadUrl == Self.expectedDownloadURL(
                    manifest: manifest,
                    attachmentID: entry.attachmentId
                  ) else {
                throw NativeBookOCRError.invalidAttachment(
                    "附件 \(entry.attachmentId) 校验失败"
                )
            }
            let (nextAggregate, overflow) = aggregateBytes.addingReportingOverflow(
                entry.size
            )
            guard !overflow, nextAggregate <= Self.maximumBundleBytes else {
                throw NativeBookOCRError.invalidAttachment("附件总大小超出限制")
            }
            aggregateBytes = nextAggregate
        }
    }

    private func contentDirectory(_ contentSHA256: String) -> URL {
        rootURL.appendingPathComponent("content", isDirectory: true)
            .appendingPathComponent(contentSHA256.lowercased(), isDirectory: true)
    }

    private func pageURL(
        contentSHA256: String,
        page: Int,
        beneath directory: URL
    ) -> URL {
        directory.appendingPathComponent("pages", isDirectory: true)
            .appendingPathComponent(
                String(format: "p%06d.json", page),
                isDirectory: false
            )
    }

    private func manualPageURL(
        contentSHA256: String,
        page: Int
    ) -> URL {
        contentDirectory(contentSHA256)
            .appendingPathComponent("overrides/manual", isDirectory: true)
            .appendingPathComponent(
                String(format: "p%06d.json", page),
                isDirectory: false
            )
    }

    private func selectionCorrectionURL(
        contentSHA256: String,
        page: Int
    ) -> URL {
        contentDirectory(contentSHA256)
            .appendingPathComponent("overrides/selection", isDirectory: true)
            .appendingPathComponent(
                String(format: "p%06d.json", page),
                isDirectory: false
            )
    }

    private func statusURL(bookID: String) -> URL {
        let digest = Self.sha256(Data(bookID.utf8))
        return rootURL.appendingPathComponent("jobs", isDirectory: true)
            .appendingPathComponent("\(digest).json", isDirectory: false)
    }

    private func importReceiptURL(
        revision: String,
        beneath directory: URL
    ) -> URL {
        directory.appendingPathComponent("imports", isDirectory: true)
            .appendingPathComponent("\(revision).json", isDirectory: false)
    }

    private func validateContentSHA256(_ value: String) throws {
        guard Self.isSHA256(value) else {
            throw NativeBookOCRError.invalidContentSHA256
        }
    }

    private func validateRevision(_ value: String) throws {
        guard value.range(
            of: #"^ocr_[0-9a-f]{20}$"#,
            options: .regularExpression
        ) != nil else {
            throw NativeBookOCRError.invalidAttachment("附件修订号无效")
        }
    }

    private func validatePDFMutationLease(
        _ lease: NativeBookOCRPDFMutationLease
    ) throws {
        guard lease.bookID.range(
            of: #"^localbook-[a-f0-9]{64}$"#,
            options: .regularExpression
        ) != nil,
        lease.token.range(
            of: #"^[a-f0-9]{32}$"#,
            options: .regularExpression
        ) != nil,
        Self.isSHA256(lease.oldContentSHA256) else {
            throw NativeBookOCRError.storage("PDF 改页 OCR 租约身份无效")
        }
    }

    private func registerPDFMutationDigestLease(
        _ contentSHA256: String,
        lease: NativeBookOCRPDFMutationLease
    ) throws {
        try validateContentSHA256(contentSHA256)
        let digest = contentSHA256.lowercased()
        var leases = pdfMutationLeasesByDigest[digest] ?? [:]
        if let active = leases[lease.bookID], active != lease {
            throw NativeBookOCRError.storage(
                "本书的 PDF 摘要已有另一项 OCR 改页租约"
            )
        }
        leases[lease.bookID] = lease
        pdfMutationLeasesByDigest[digest] = leases
    }

    private func assertPDFMutationWriteAllowed(
        bookID: String,
        contentSHA256: String? = nil,
        mutationLease: NativeBookOCRPDFMutationLease? = nil
    ) throws {
        if let mutationLease {
            try validatePDFMutationLease(mutationLease)
            guard mutationLease.bookID == bookID,
                  pdfMutationLeases[bookID] == mutationLease else {
                throw NativeBookOCRError.storage(
                    "PDF 改页 OCR 租约已失效"
                )
            }
            if let contentSHA256 {
                try validateContentSHA256(contentSHA256)
                guard pdfMutationLeasesByDigest[
                    contentSHA256.lowercased()
                ]?[bookID] == mutationLease else {
                    throw NativeBookOCRError.storage(
                        "PDF 改页 OCR 摘要租约已失效"
                    )
                }
            }
            return
        }
        guard pdfMutationLeases[bookID] == nil else {
            throw NativeBookOCRError.storage(
                "PDF 改页期间拒绝并发 OCR 写入"
            )
        }
        if let contentSHA256 {
            try validateContentSHA256(contentSHA256)
            let digest = contentSHA256.lowercased()
            guard pdfMutationLeasesByDigest[digest]?[bookID] == nil else {
                throw NativeBookOCRError.storage(
                    "本书的 PDF 内容正在改页，拒绝并发 OCR 写入"
                )
            }
            guard pdfMutationTargetLeaseByDigest[digest] == nil else {
                throw NativeBookOCRError.storage(
                    "目标 PDF OCR sidecar 正在原子替换，拒绝并发写入"
                )
            }
        }
    }

    private func validatePDFMutationTicket(_ ticket: String) throws {
        guard ticket.range(
            of: #"^npmt_[a-f0-9]{32}$"#,
            options: .regularExpression
        ) != nil else {
            throw NativeBookOCRError.storage("PDF 改页 ticket 无效")
        }
    }

    private func assertPDFMutationFileAccess(
        lease: NativeBookOCRPDFMutationLease,
        ticket: String,
        stagedContentSHA256: String
    ) throws {
        try assertPDFMutationWriteAllowed(
            bookID: lease.bookID,
            mutationLease: lease
        )
        try validatePDFMutationTicket(ticket)
        try validateContentSHA256(stagedContentSHA256)
        let stagedDigest = stagedContentSHA256.lowercased()
        guard pdfMutationLeasesByDigest[stagedDigest]?[lease.bookID] == lease,
              pdfMutationTargetLeaseByDigest[stagedDigest] == lease else {
            throw NativeBookOCRError.storage(
                "目标 PDF OCR 摘要租约已失效"
            )
        }
    }

    private func pdfMutationURLs(
        ticket: String,
        stagedContentSHA256: String
    ) -> (staging: URL, target: URL, backup: URL) {
        let contentRoot = rootURL.appendingPathComponent(
            "content",
            isDirectory: true
        )
        return (
            staging: contentRoot.appendingPathComponent(
                ".bw-pdf-mutation-\(ticket).staging",
                isDirectory: true
            ),
            target: contentRoot.appendingPathComponent(
                stagedContentSHA256.lowercased(),
                isDirectory: true
            ),
            backup: contentRoot.appendingPathComponent(
                ".bw-pdf-mutation-\(ticket).backup",
                isDirectory: true
            )
        )
    }

    private static func isSHA256(_ value: String) -> Bool {
        value.range(
            of: #"^[0-9a-fA-F]{64}$"#,
            options: .regularExpression
        ) != nil
    }

    private static func isValidAttachmentID(_ value: String) -> Bool {
        value == "ocr-formulas"
            || value.range(
                of: #"^ocr-page-[0-9]{6}$"#,
                options: .regularExpression
            ) != nil
    }

    private static func expectedDownloadURL(
        manifest: NativeBookOCRDerivedAttachmentManifest,
        attachmentID: String
    ) -> String {
        "/pdf/api/library/attachments/\(manifest.bookId)/\(attachmentID)"
            + "?contentSha256=\(manifest.contentSha256)"
            + "&revision=\(manifest.revision)"
    }

    private static func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .millisecondsSince1970
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }

    private func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .millisecondsSince1970
        return decoder
    }
}

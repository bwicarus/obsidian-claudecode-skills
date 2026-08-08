import CryptoKit
import Foundation

actor NativeBookOCRSidecarStore {
    private static let maximumAttachments = 5_001
    private static let maximumAttachmentBytes = 32 * 1_024 * 1_024
    private static let maximumBundleBytes = 512 * 1_024 * 1_024

    private let fileManager: FileManager
    private let rootURL: URL

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

    func page(
        contentSHA256: String,
        page: Int
    ) throws -> NativeBookOCRPageCharacters? {
        try validateContentSHA256(contentSHA256)
        guard page >= 1 else { return nil }
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

    func writePage(_ value: NativeBookOCRPageCharacters) throws {
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

    func writeStatus(_ status: NativeBookOCRBookStatus) throws {
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
        expectedContentSHA256: String,
        manifest: NativeBookOCRDerivedAttachmentManifest,
        files: [String: Data]
    ) throws -> NativeBookOCRImportResult {
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

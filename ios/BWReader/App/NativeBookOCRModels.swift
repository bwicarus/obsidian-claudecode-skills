import Foundation

enum NativeBookOCRSource: String, Codable, Sendable {
    case apple
    case pi
}

enum NativeBookOCRJobState: String, Codable, Sendable {
    case idle
    case running
    case paused
    case completed
    case failed
    case cancelled
}

enum NativeBookOCRPageState: String, Codable, Sendable {
    case idle
    case pending
    case ready
    case readyEmpty
    case failed
}

enum NativeBookOCRWordSegmentationState: String, Codable, Sendable {
    case ready
    case partial
    case unavailable
}

enum NativeBookOCRCharacterGeometryState: String, Codable, Sendable {
    case exact
    case estimated
    case unavailable
}

/// Indicates whether a native page is only supplemental analysis or is the
/// user-selected text authority for this page. The latter is intentionally
/// explicit so PDF.js embedded text cannot silently hide a persisted manual
/// re-OCR or selection correction.
enum NativeBookOCRTextAuthority: String, Codable, Sendable {
    case supplemental
    case localOverride = "local-override"
}

enum NativeBookOCRFormulaCoverage: String, Codable, Sendable {
    /// No formula pass has examined this page yet.
    case unknown
    /// Apple Vision can recognize ordinary text but cannot claim formula
    /// coverage or produce LaTeX by itself.
    case unavailable
    /// Candidate regions are known, but one or more still await the existing
    /// formula pipeline.
    case partial
    /// Every known formula region has a terminal ready/failed result.
    case complete
}

enum NativeBookOCRFormulaState: String, Codable, Sendable {
    case pending
    case ready
    case failed
}

struct NativeBookOCRStageProgress: Codable, Equatable, Sendable {
    let total: Int
    let completed: Int
    let pending: Int
    let failed: Int
    let unavailable: Int

    static let empty = NativeBookOCRStageProgress(
        total: 0,
        completed: 0,
        pending: 0,
        failed: 0,
        unavailable: 0
    )
}

struct NativeBookOCRBookStatus: Codable, Equatable, Sendable {
    static let schema = "reader-native-book-ocr-status/1"

    let schema: String
    let bookID: String
    let contentSHA256: String
    let state: NativeBookOCRJobState
    let source: NativeBookOCRSource?
    let totalPages: Int
    let currentPage: Int?
    let textProgress: NativeBookOCRStageProgress
    let wordProgress: NativeBookOCRStageProgress
    let formulaProgress: NativeBookOCRStageProgress
    let formulaPendingRegions: Int
    let formulaFailedRegions: Int
    let message: String?
    let updatedAt: Date

    var progress: Double {
        guard totalPages > 0 else { return 0 }
        return min(
            1,
            Double(textProgress.completed + textProgress.failed)
                / Double(totalPages)
        )
    }

    var canPause: Bool { state == .running }
    var canResume: Bool { state == .paused }
    var canRetry: Bool {
        state == .failed || state == .cancelled || textProgress.failed > 0
    }

    static func idle(bookID: String) -> NativeBookOCRBookStatus {
        NativeBookOCRBookStatus(
            schema: schema,
            bookID: bookID,
            contentSHA256: "",
            state: .idle,
            source: nil,
            totalPages: 0,
            currentPage: nil,
            textProgress: .empty,
            wordProgress: .empty,
            formulaProgress: .empty,
            formulaPendingRegions: 0,
            formulaFailedRegions: 0,
            message: nil,
            updatedAt: Date()
        )
    }
}

struct NativeBookOCRCharacter: Codable, Equatable, Sendable {
    var c: String
    var x0: Double
    var y0: Double
    var x1: Double
    var y1: Double
    var sp: Int
    var w: Int
    var b: Int
    var bk: Int

    private enum CodingKeys: String, CodingKey {
        case c, x0, y0, x1, y1, sp, w, b, bk
    }

    init(
        c: String,
        x0: Double,
        y0: Double,
        x1: Double,
        y1: Double,
        sp: Int,
        w: Int,
        b: Int,
        bk: Int
    ) {
        self.c = c
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
        self.sp = sp
        self.w = w
        self.b = b
        self.bk = bk
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        c = try values.decode(String.self, forKey: .c)
        x0 = try values.decode(Double.self, forKey: .x0)
        y0 = try values.decode(Double.self, forKey: .y0)
        x1 = try values.decode(Double.self, forKey: .x1)
        y1 = try values.decode(Double.self, forKey: .y1)
        sp = try values.decodeIfPresent(Int.self, forKey: .sp) ?? 0
        w = try values.decodeIfPresent(Int.self, forKey: .w) ?? -1
        b = try values.decodeIfPresent(Int.self, forKey: .b) ?? 0
        bk = try values.decodeIfPresent(Int.self, forKey: .bk) ?? -1
    }
}

struct NativeBookOCRFormulaRegion: Codable, Equatable, Sendable {
    let id: String
    let x0: Double
    let y0: Double
    let x1: Double
    let y1: Double
    let state: NativeBookOCRFormulaState
    let latex: String?
    let multiline: Bool?
    let error: String?
}

struct NativeBookOCRPageCharacters: Codable, Equatable, Sendable {
    static let schema = "reader-native-page-characters/1"

    let schema: String
    let contentSHA256: String
    let page: Int
    let pageWidth: Double
    let pageHeight: Double
    let rotation: Int
    let geometryDigest: String
    let engineRevision: String
    let status: NativeBookOCRPageState
    let source: NativeBookOCRSource?
    let chars: [NativeBookOCRCharacter]
    let furigana: [NativeBookOCRFurigana]
    let wordSegmentation: NativeBookOCRWordSegmentationState
    let characterGeometry: NativeBookOCRCharacterGeometryState
    let formulaCoverage: NativeBookOCRFormulaCoverage
    let formulaRegions: [NativeBookOCRFormulaRegion]
    let createdAt: Date
    let error: String?
    var textAuthority: NativeBookOCRTextAuthority? = nil

    enum CodingKeys: String, CodingKey {
        case schema
        case contentSHA256 = "content_sha256"
        case page
        case pageWidth = "page_w"
        case pageHeight = "page_h"
        case rotation
        case geometryDigest = "geometry_digest"
        case engineRevision = "engine_revision"
        case status
        case source
        case chars
        case furigana
        case wordSegmentation = "word_segmentation"
        case characterGeometry = "character_geometry"
        case formulaCoverage = "formula_coverage"
        case formulaRegions = "formula_regions"
        case createdAt = "created_at"
        case error
        case textAuthority = "text_authority"
    }

    func replacingFormulaAttachment(
        _ attachment: NativeBookOCRFormulaAttachment,
        source: NativeBookOCRSource
    ) -> NativeBookOCRPageCharacters {
        NativeBookOCRPageCharacters(
            schema: schema,
            contentSHA256: contentSHA256,
            page: page,
            pageWidth: pageWidth,
            pageHeight: pageHeight,
            rotation: rotation,
            geometryDigest: geometryDigest,
            engineRevision: engineRevision,
            status: status,
            source: source,
            chars: chars,
            furigana: furigana,
            wordSegmentation: wordSegmentation,
            characterGeometry: characterGeometry,
            formulaCoverage: attachment.formulaCoverage,
            formulaRegions: attachment.formulaRegions,
            createdAt: max(createdAt, attachment.createdAt),
            error: error,
            textAuthority: textAuthority
        )
    }

    func replacingSource(_ source: NativeBookOCRSource) -> NativeBookOCRPageCharacters {
        NativeBookOCRPageCharacters(
            schema: schema,
            contentSHA256: contentSHA256,
            page: page,
            pageWidth: pageWidth,
            pageHeight: pageHeight,
            rotation: rotation,
            geometryDigest: geometryDigest,
            engineRevision: engineRevision,
            status: status,
            source: source,
            chars: chars,
            furigana: furigana,
            wordSegmentation: wordSegmentation,
            characterGeometry: characterGeometry,
            formulaCoverage: formulaCoverage,
            formulaRegions: formulaRegions,
            createdAt: createdAt,
            error: error,
            textAuthority: textAuthority
        )
    }

    func replacingTextAuthority(
        _ authority: NativeBookOCRTextAuthority,
        engineRevision: String? = nil
    ) -> NativeBookOCRPageCharacters {
        NativeBookOCRPageCharacters(
            schema: schema,
            contentSHA256: contentSHA256,
            page: page,
            pageWidth: pageWidth,
            pageHeight: pageHeight,
            rotation: rotation,
            geometryDigest: geometryDigest,
            engineRevision: engineRevision ?? self.engineRevision,
            status: status,
            source: source,
            chars: chars,
            furigana: furigana,
            wordSegmentation: wordSegmentation,
            characterGeometry: characterGeometry,
            formulaCoverage: formulaCoverage,
            formulaRegions: formulaRegions,
            createdAt: createdAt,
            error: error,
            textAuthority: authority
        )
    }
}

struct NativeBookOCRSelectionCorrection: Codable, Equatable, Sendable {
    static let schema = "reader-native-selection-correction/1"

    let schema: String
    let id: String
    let contentSHA256: String
    let page: Int
    let bbox: [Double]
    let text: String
    let chars: [NativeBookOCRCharacter]
    let createdAt: Date

    enum CodingKeys: String, CodingKey {
        case schema, id, page, bbox, text, chars
        case contentSHA256 = "content_sha256"
        case createdAt = "created_at"
    }
}

struct NativeBookOCRSelectionCorrectionEnvelope: Codable, Equatable, Sendable {
    static let schema = "reader-native-selection-corrections/1"

    let schema: String
    let contentSHA256: String
    let page: Int
    let corrections: [NativeBookOCRSelectionCorrection]

    enum CodingKeys: String, CodingKey {
        case schema, page, corrections
        case contentSHA256 = "content_sha256"
    }
}

struct NativeBookOCRSelectionResult: Equatable, Sendable {
    let page: NativeBookOCRPageCharacters
    let text: String
}

struct NativeBookOCRClearResult: Equatable, Sendable {
    let page: NativeBookOCRPageCharacters?
    let cleared: Bool
}

/// Kept structurally compatible with the existing page-chars response. The
/// native first pass does not synthesize readings, so this array is normally
/// empty until a dedicated furigana attachment is imported.
struct NativeBookOCRFurigana: Codable, Equatable, Sendable {
    let wd: String?
    let rt: String?
    let x0: Double?
    let y0: Double?
    let x1: Double?
    let y1: Double?
}

struct NativeBookOCRFormulaAttachment: Codable, Equatable, Sendable {
    static let schema = "reader-native-formula-sidecar/1"

    let schema: String
    let contentSHA256: String
    let page: Int
    let geometryDigest: String
    let engineRevision: String
    let formulaCoverage: NativeBookOCRFormulaCoverage
    let formulaRegions: [NativeBookOCRFormulaRegion]
    let createdAt: Date

    enum CodingKeys: String, CodingKey {
        case schema
        case contentSHA256 = "content_sha256"
        case page
        case geometryDigest = "geometry_digest"
        case engineRevision = "engine_revision"
        case formulaCoverage = "formula_coverage"
        case formulaRegions = "formula_regions"
        case createdAt = "created_at"
    }
}

struct NativeBookOCRSearchHit: Codable, Equatable, Sendable {
    let page: Int
    let text: String
    let firstCharacter: Int
    let lastCharacter: Int
    let rects: [[Double]]
}

struct NativeBookOCRSearchResult: Codable, Equatable, Sendable {
    let matches: [NativeBookOCRSearchHit]
    let total: Int
    let pages: [Int]
    let incomplete: Bool
}

struct NativeBookOCRUpdate: Codable, Equatable, Sendable {
    static let contract = "reader-native-page-text-update/1"

    let contract: String
    let bookID: String
    let page: Int?
    let status: NativeBookOCRBookStatus
}

struct NativeBookOCRDerivedAttachmentManifest: Codable, Sendable {
    static let contract = "reader-book-attachments/1"

    let contract: String
    let schema: Int
    let bookId: String
    let contentSha256: String
    let revision: String
    let category: String
    let mergePolicy: String
    let files: [File]

    init(
        contract: String,
        schema: Int = 1,
        bookId: String,
        contentSha256: String,
        revision: String,
        category: String = "derived",
        mergePolicy: String = "immutable",
        files: [File]
    ) {
        self.contract = contract
        self.schema = schema
        self.bookId = bookId
        self.contentSha256 = contentSha256
        self.revision = revision
        self.category = category
        self.mergePolicy = mergePolicy
        self.files = files
    }

    struct File: Codable, Sendable {
        let attachmentId: String
        let kind: String
        let category: String
        let mergePolicy: String
        let mediaType: String
        let size: Int64
        let sha256: String
        let downloadUrl: String
        let page: Int?
    }
}

struct NativeBookOCRImportResult: Equatable, Sendable {
    let contentSHA256: String
    let importedPages: [Int]
    let importedFormulaPages: [Int]
}

struct NativeBookOCRConfiguration: Equatable, Sendable {
    static let engineRevision = "apple-vision-structured/1"

    var renderLongEdgePixels = 3_200
    var minimumEmbeddedCharacters = 24
    var minimumVisionConfidence: Float = 0.22
    var recognitionLanguages = ["ja-JP", "zh-Hans", "zh-Hant", "en-US"]
    var usesLanguageCorrection = true
}

enum NativeBookOCRError: LocalizedError {
    case pdfRequired
    case invalidContentSHA256
    case unreadableBook
    case pageUnavailable
    case unsupported
    case unsupportedPageRotation(Int)
    case invalidSelection
    case noRecognizedText
    case lowConfidence
    case storage(String)
    case invalidAttachment(String)

    var errorDescription: String? {
        switch self {
        case .pdfRequired:
            return "本机文字预处理目前只支持 PDF"
        case .invalidContentSHA256:
            return "书籍完整内容摘要无效，请重新扫描本机书库"
        case .unreadableBook:
            return "无法读取本机 PDF"
        case .pageUnavailable:
            return "PDF 页码不可用"
        case .unsupported:
            return "这台设备不支持 Apple 本机文字识别"
        case .unsupportedPageRotation(let rotation):
            return "PDF 页面旋转角度不受支持（\(rotation)°）"
        case .invalidSelection:
            return "文字识别选区无效或超出当前页面"
        case .noRecognizedText:
            return "本页没有识别到可用文字"
        case .lowConfidence:
            return "本页文字识别质量不足，可手动选择 Pi 预处理"
        case .storage(let detail):
            return "保存本机文字预处理结果失败：\(detail)"
        case .invalidAttachment(let detail):
            return "导入 Pi 预处理附件失败：\(detail)"
        }
    }
}

import Foundation

enum NativeBookOCRSource: String, Codable, Sendable {
    case apple
    case pi
    case pc
}

/// A user-selectable base text/analysis layer. `legacy` is the pre-layered
/// `content/<digest>/pages` directory and remains in place so upgrades never
/// rewrite or discard an existing result. `embedded` is normally served by
/// PDF.js; the native store only materializes an embedded page when a manual
/// selection correction needs a base to overlay.
enum NativeBookOCRLayerID: String, Codable, CaseIterable, Hashable, Identifiable, Sendable {
    case embedded
    case legacy
    case appleVision = "apple-vision"
    case pi
    case pc

    var id: String { rawValue }

    var title: String {
        switch self {
        case .embedded: return "PDF 原文字层"
        case .legacy: return "现有兼容结果"
        case .appleVision: return "本机 Vision"
        case .pi: return "服务器预处理"
        case .pc: return "PC 高质量预处理"
        }
    }
}

struct NativeBookOCRLayerMetadata: Codable, Equatable, Identifiable, Sendable {
    static let schema = "reader-native-book-ocr-layer/1"

    let schema: String
    let contentSHA256: String
    let layer: NativeBookOCRLayerID
    let engine: String
    let executor: String?
    let processingProfile: String?
    let revision: String
    let pageCount: Int
    let updatedAt: Date

    var id: NativeBookOCRLayerID { layer }
}

struct NativeBookOCRLayerSelection: Codable, Equatable, Sendable {
    static let schema = "reader-native-book-ocr-layer-selection/1"

    let schema: String
    let contentSHA256: String
    let selected: NativeBookOCRLayerID
    let updatedAt: Date
    /// 这个选择是不是用户自己点的。
    ///
    /// 可选而不是 Bool：老的选择文件里没有这个字段，解出来是 nil，语义正好是
    /// "没主动选过" —— 不需要迁移，也不会把历史选择误判成用户拍板过的。
    let chosenByUser: Bool?
}

struct NativeBookOCRLayerState: Equatable, Sendable {
    let contentSHA256: String
    let selected: NativeBookOCRLayerID
    let available: [NativeBookOCRLayerMetadata]

    func metadata(for layer: NativeBookOCRLayerID) -> NativeBookOCRLayerMetadata? {
        available.first(where: { $0.layer == layer })
    }
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
    var line: Int?
    var vertical: Bool?

    private enum CodingKeys: String, CodingKey {
        case c, x0, y0, x1, y1, sp, w, b, bk, line, vertical
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
        bk: Int,
        line: Int? = nil,
        vertical: Bool? = nil
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
        self.line = line
        self.vertical = vertical
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
        line = try values.decodeIfPresent(Int.self, forKey: .line)
        vertical = try values.decodeIfPresent(Bool.self, forKey: .vertical)
    }
}

private struct NativeBookOCRAnyCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int?

    init?(stringValue: String) {
        self.stringValue = stringValue
        intValue = nil
    }

    init?(intValue: Int) {
        stringValue = String(intValue)
        self.intValue = intValue
    }
}

private func rejectUnknownOCRFields<Key: CodingKey & CaseIterable>(
    from decoder: Decoder,
    allowedBy _: Key.Type
) throws where Key.AllCases: Sequence {
    let container = try decoder.container(
        keyedBy: NativeBookOCRAnyCodingKey.self
    )
    let allowed = Set(Key.allCases.map(\.stringValue))
    let unknown = container.allKeys.map(\.stringValue).filter {
        !allowed.contains($0)
    }.sorted()
    guard unknown.isEmpty else {
        throw DecodingError.dataCorrupted(
            DecodingError.Context(
                codingPath: decoder.codingPath,
                debugDescription: "Unknown OCR layout fields: \(unknown.joined(separator: ", "))"
            )
        )
    }
}

enum NativeBookOCRPageLayoutTextSource: String, Codable, Equatable, Sendable {
    case vision
    case unavailable
}

enum NativeBookOCRPageLayoutSource: String, Codable, Equatable, Sendable {
    case manga
    case ruledTable = "ruled-table"
    case vision
}

enum NativeBookOCRPageLayoutMode: String, Codable, Equatable, Sendable {
    case manga
    case table
    case vision
    case fallback
}

enum NativeBookOCRPageReadingDirection: String, Codable, Equatable, Sendable {
    case leftToRight = "ltr"
    case rightToLeft = "rtl"
}

enum NativeBookOCRPageLayoutConfidence: String, Codable, Equatable, Sendable {
    case high
    case low
    case fallback
}

enum NativeBookOCRPageLayoutRegionKind: String, Codable, Equatable, Sendable {
    case mangaRegion = "manga-region"
    case visionSupplement = "vision-supplement"
    case tableCell = "table-cell"
    case visionBlock = "vision-block"
}

struct NativeBookOCRPageLayoutRegion: Codable, Equatable, Sendable {
    let id: Int
    let kind: NativeBookOCRPageLayoutRegionKind
    let order: Int
    let bounds: [Double]
    let ranges: [[Int]]
    let gridRow: Int
    let gridColumn: Int
    let rowSpan: Int
    let columnSpan: Int
    let vertical: Bool
    let tableId: Int?
    let row: Int?
    let column: Int?

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case id, kind, order, bounds, ranges, gridRow, gridColumn
        case rowSpan, columnSpan, vertical, tableId, row, column
    }

    init(from decoder: Decoder) throws {
        try rejectUnknownOCRFields(from: decoder, allowedBy: CodingKeys.self)
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(Int.self, forKey: .id)
        kind = try values.decode(
            NativeBookOCRPageLayoutRegionKind.self,
            forKey: .kind
        )
        order = try values.decode(Int.self, forKey: .order)
        bounds = try values.decode([Double].self, forKey: .bounds)
        ranges = try values.decode([[Int]].self, forKey: .ranges)
        gridRow = try values.decode(Int.self, forKey: .gridRow)
        gridColumn = try values.decode(Int.self, forKey: .gridColumn)
        rowSpan = try values.decode(Int.self, forKey: .rowSpan)
        columnSpan = try values.decode(Int.self, forKey: .columnSpan)
        vertical = try values.decode(Bool.self, forKey: .vertical)
        guard values.contains(.tableId),
              values.contains(.row),
              values.contains(.column) else {
            throw DecodingError.keyNotFound(
                !values.contains(.tableId)
                    ? CodingKeys.tableId
                    : (!values.contains(.row) ? CodingKeys.row : CodingKeys.column),
                DecodingError.Context(
                    codingPath: decoder.codingPath,
                    debugDescription: "OCR layout nullable fields are required"
                )
            )
        }
        tableId = try values.decodeIfPresent(Int.self, forKey: .tableId)
        row = try values.decodeIfPresent(Int.self, forKey: .row)
        column = try values.decodeIfPresent(Int.self, forKey: .column)
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(id, forKey: .id)
        try values.encode(kind, forKey: .kind)
        try values.encode(order, forKey: .order)
        try values.encode(bounds, forKey: .bounds)
        try values.encode(ranges, forKey: .ranges)
        try values.encode(gridRow, forKey: .gridRow)
        try values.encode(gridColumn, forKey: .gridColumn)
        try values.encode(rowSpan, forKey: .rowSpan)
        try values.encode(columnSpan, forKey: .columnSpan)
        try values.encode(vertical, forKey: .vertical)
        if let tableId {
            try values.encode(tableId, forKey: .tableId)
        } else {
            try values.encodeNil(forKey: .tableId)
        }
        if let row {
            try values.encode(row, forKey: .row)
        } else {
            try values.encodeNil(forKey: .row)
        }
        if let column {
            try values.encode(column, forKey: .column)
        } else {
            try values.encodeNil(forKey: .column)
        }
    }
}

struct NativeBookOCRPageLayoutTable: Codable, Equatable, Sendable {
    let id: Int
    let rows: Int
    let columns: Int
    let xEdges: [Double]
    let yEdges: [Double]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case id, rows, columns, xEdges, yEdges
    }

    init(from decoder: Decoder) throws {
        try rejectUnknownOCRFields(from: decoder, allowedBy: CodingKeys.self)
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(Int.self, forKey: .id)
        rows = try values.decode(Int.self, forKey: .rows)
        columns = try values.decode(Int.self, forKey: .columns)
        xEdges = try values.decode([Double].self, forKey: .xEdges)
        yEdges = try values.decode([Double].self, forKey: .yEdges)
    }
}

struct NativeBookOCRPageLayout: Codable, Equatable, Sendable {
    static let schema = "reader-page-layout/1"

    let schema: String
    let textSource: NativeBookOCRPageLayoutTextSource
    let layoutSource: NativeBookOCRPageLayoutSource
    let mode: NativeBookOCRPageLayoutMode
    let readingDirection: NativeBookOCRPageReadingDirection
    let confidence: NativeBookOCRPageLayoutConfidence
    let gridColumns: Int
    let gridRows: Int
    let regions: [NativeBookOCRPageLayoutRegion]
    let tables: [NativeBookOCRPageLayoutTable]

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schema, textSource, layoutSource, mode, readingDirection
        case confidence, gridColumns, gridRows, regions, tables
    }

    init(from decoder: Decoder) throws {
        try rejectUnknownOCRFields(from: decoder, allowedBy: CodingKeys.self)
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schema = try values.decode(String.self, forKey: .schema)
        textSource = try values.decode(
            NativeBookOCRPageLayoutTextSource.self,
            forKey: .textSource
        )
        layoutSource = try values.decode(
            NativeBookOCRPageLayoutSource.self,
            forKey: .layoutSource
        )
        mode = try values.decode(
            NativeBookOCRPageLayoutMode.self,
            forKey: .mode
        )
        readingDirection = try values.decode(
            NativeBookOCRPageReadingDirection.self,
            forKey: .readingDirection
        )
        confidence = try values.decode(
            NativeBookOCRPageLayoutConfidence.self,
            forKey: .confidence
        )
        gridColumns = try values.decode(Int.self, forKey: .gridColumns)
        gridRows = try values.decode(Int.self, forKey: .gridRows)
        regions = try values.decode(
            [NativeBookOCRPageLayoutRegion].self,
            forKey: .regions
        )
        tables = try values.decode(
            [NativeBookOCRPageLayoutTable].self,
            forKey: .tables
        )
        if textSource == .unavailable {
            guard mode == .fallback,
                  confidence == .fallback,
                  layoutSource == .vision,
                  gridRows == 0,
                  regions.isEmpty,
                  tables.isEmpty else {
                throw DecodingError.dataCorruptedError(
                    forKey: .layoutSource,
                    in: values,
                    debugDescription: "Unavailable OCR layout must use the Vision fallback shape"
                )
            }
        }
        var totalTableCells = 0
        for table in tables {
            let (tableCells, overflow) = table.rows.multipliedReportingOverflow(
                by: table.columns
            )
            guard table.rows > 0,
                  table.columns >= 2,
                  !overflow,
                  tableCells <= 16_384 - totalTableCells else {
                throw DecodingError.dataCorruptedError(
                    forKey: .tables,
                    in: values,
                    debugDescription: "OCR layout tables exceed the safe cell limit"
                )
            }
            totalTableCells += tableCells
        }
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
    let layout: NativeBookOCRPageLayout?
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
        case layout
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
            layout: layout,
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
            layout: layout,
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
            layout: layout,
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

    /// Carries the independently imported Pi formula result across a refresh
    /// of the Apple Vision base text. The new text and geometry identity stay
    /// authoritative; only the formula attachment is retained.
    func preservingPiFormulaAttachment(
        from previous: NativeBookOCRPageCharacters?
    ) -> NativeBookOCRPageCharacters {
        guard let previous,
              previous.contentSHA256 == contentSHA256,
              previous.page == page,
              previous.source == .pi,
              previous.formulaCoverage == .complete else {
            return self
        }
        return NativeBookOCRPageCharacters(
            schema: schema,
            contentSHA256: contentSHA256,
            page: page,
            pageWidth: pageWidth,
            pageHeight: pageHeight,
            rotation: rotation,
            geometryDigest: geometryDigest,
            engineRevision: engineRevision,
            status: status,
            source: .pi,
            chars: chars,
            layout: layout,
            furigana: furigana,
            wordSegmentation: wordSegmentation,
            characterGeometry: characterGeometry,
            formulaCoverage: previous.formulaCoverage,
            formulaRegions: previous.formulaRegions,
            createdAt: max(createdAt, previous.createdAt),
            error: error,
            textAuthority: textAuthority
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
    let engine: String?
    let executor: String?
    let processingProfile: String?
    let category: String
    let mergePolicy: String
    let files: [File]

    init(
        contract: String,
        schema: Int = 1,
        bookId: String,
        contentSha256: String,
        revision: String,
        engine: String? = nil,
        executor: String? = nil,
        processingProfile: String? = nil,
        category: String = "derived",
        mergePolicy: String = "immutable",
        files: [File]
    ) {
        self.contract = contract
        self.schema = schema
        self.bookId = bookId
        self.contentSha256 = contentSha256
        self.revision = revision
        self.engine = engine
        self.executor = executor
        self.processingProfile = processingProfile
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
    let layer: NativeBookOCRLayerID
    let importedPages: [Int]
    let importedFormulaPages: [Int]
}

struct NativeBookOCRConfiguration: Equatable, Sendable {
    static let engineRevision = "apple-vision-structured/2"
    static let manualEngineRevision = "apple-vision-manual/2"

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
            return "本页文字识别质量不足，可手动选择服务器预处理"
        case .storage(let detail):
            return "保存本机文字预处理结果失败：\(detail)"
        case .invalidAttachment(let detail):
            return "导入服务器预处理附件失败：\(detail)"
        }
    }
}

/// 收藏词组表（2026-09-02）：文字层存储服务每一页时按它把词组合并成一个词。
/// 全局一份（不按书），由 runtime 经 `phrases-set` 推来；Pi 已退出这条线路。
struct NativeBookOCRPhraseList: Codable, Equatable, Sendable {
    static let schema = "reader-native-phrase-list/1"
    let schema: String
    let phrases: [String]
    let updatedAt: Date
}

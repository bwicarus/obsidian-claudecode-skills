import PDFKit
import Combine
import SwiftUI
import UIKit

/// The native PDF viewport owns only display and coordinates. Original OCR
/// character indexes, book identity and persisted overlays remain authoritative.
/// Wiring its ports replaces the document renderer, not the data repositories.
@MainActor
final class ReaderNativePDFDocument: NSObject, ObservableObject, PDFPageOverlayViewProvider, UIGestureRecognizerDelegate {
    struct Position: Equatable {
        let page: Int
        let scale: CGFloat
        let visiblePages: [Int]
        let fraction: CGFloat
        let mode: String
        let spreadOffset: Int
        let crop: ReaderNativePDFCrop?
    }
    struct CharacterSelection {
        let bookID: String
        let contentSHA256: String
        let page: Int
        let geometryDigest: String
        let indexes: [Int]
        let text: String
        let sentence: String
        let rects: [CGRect]
    }
    /// 一条划线。
    ///
    /// ⚠ 以前这里只有 rect + color，每个矩形拆成独立一条 —— 于是**点上去不知道点的是
    /// 哪一条**，改色/备注/删除全都做不了。身份（id）和备注必须跟着进来：接管后
    /// .hl-layer 不存在，原生是唯一能点到划线的地方。
    struct Highlight: Identifiable {
        let id: String
        let page: Int
        let rects: [CGRect]          // 归一化
        let color: Color
        let colorKey: String         // 原始色值；空 = 「无色」虚框（只有备注的那种）
        let note: String
        let text: String
    }
    /// ⚠ 用具名类型而不是元组：`ForEach(_:id: \.id)` 的 keypath 在元组标签上
    /// 根本不成立（编译不过）。
    struct CardMarker: Identifiable {
        let id: String
        let rects: [CGRect]
        /// 分类色调 —— 原版 `WORD_CARD_TONES`（rc-stickynote）四选一。
        let tone: UIColor
        /// 这张卡正展开着（原版 `.pgmark.on`：描边加深 + 外晕）。
        let open: Bool
    }

    struct NoteGeometry {
        let id: String
        let page: Int
        let rect: CGRect
        let bindingRects: [CGRect]
        let bindingQuality: String?
        let bindingMatches: Int
        let bindingUnresolved: Bool
    }

    /// 点正文里的卡片锁定框 → 展开那张卡。由 App 接到阅读器上。
    var onOpenCard: ((String) -> Void)?

    let view = ReaderNativePDFView()
    @Published private(set) var position = Position(page: 1, scale: 1, visiblePages: [], fraction: 0, mode: "continuous", spreadOffset: 0, crop: nil)
    @Published private(set) var error: String?
    @Published private(set) var ready = false
    @Published private(set) var geometryRevision = 0
    @Published private(set) var ink: [Int: [ReaderNativeCardStroke]] = [:] { didSet { refreshDecorations() } }
    @Published private(set) var highlights: [Int: [Highlight]] = [:] { didSet { refreshDecorations() } }
    /// 生词下划线。rects 是点坐标（与高亮同一空间），由网页那侧算好该画哪些 ——
    /// 「已掌握的不画」牵涉共享仓库、本地覆盖和服务端 label 的收敛顺序，
    /// 判据留在 `_vocabMarksForDisplay` 一处，这里只负责画。
    @Published private(set) var vocabMarks: [Int: [VocabMark]] = [:] { didSet { refreshDecorations() } }

    struct VocabMark {
        let slug: String
        let rects: [CGRect]        // 归一化，便于 viewRect 直接换算
    }

    /// 由壳按可见页填。传 nil 表示这一页还没取到，保留旧的别闪。
    func setVocabMarks(_ marks: [VocabMark]?, page: Int) {
        guard let marks else { return }
        vocabMarks[page] = marks
    }

    func clearVocabMarks() { vocabMarks = [:] }

    /// 振假名：已掌握的词不注音（与网页那侧 `__masteredFuri` 同一份数据）。
    /// `enabled == false` 表示振假名整体关着 —— 那时一个都不画，跟"这一页没有
    /// 已掌握的词"不是一回事。
    @Published private(set) var furiganaEnabled: [Int: Bool] = [:] { didSet { refreshDecorations() } }
    @Published private(set) var furiganaMastered: [Int: Set<String>] = [:] { didSet { refreshDecorations() } }

    func setFuriganaMastered(_ words: [String]?, enabled: Bool, page: Int) {
        furiganaEnabled[page] = enabled
        furiganaMastered[page] = Set(words ?? [])
    }

    /// 这一页要画的振假名条目（点坐标，来自原生字符层自带的 furigana）。
    func furigana(page: Int) -> [NativeBookOCRFurigana] {
        guard furiganaEnabled[page] == true, let chars = characterPages[page] else { return [] }
        let mastered = furiganaMastered[page] ?? []
        return chars.furigana.filter { item in
            guard let rt = item.rt, !rt.isEmpty else { return false }
            if let word = item.wd, mastered.contains(word) { return false }
            return true
        }
    }

    /// 生词句子：含未掌握词的整句，网页那侧画成排线框 + 行首一个「译」按钮。
    /// rects 归一化；text 留着，点「译」时直接送进翻译，不必再回网页问一次。
    struct VocabSentence: Identifiable {
        let id: String
        let index: Int
        let page: Int
        let text: String
        let rects: [CGRect]
    }
    @Published private(set) var vocabSentences: [Int: [VocabSentence]] = [:] { didSet { refreshDecorations() } }

    /// 整页正文（按阅读顺序）。
    ///
    /// ⚠ 用的是**同一个选区核心**（`pdf-selection-core.js`）的 range(0, n-1)，
    /// 与网页那侧 `_charsRangeToText(chars, 0, n-1)` 是同一套分块/阅读顺序规则 ——
    /// 自己按字符数组拼字符串会在表格/多栏页上给出另一种顺序。
    /// 结果缓存：一页的正文不会变，而滚动时这条路每帧都可能被问到。
    private var pageTexts: [Int: String] = [:]

    func pageText(_ page: Int) -> String? {
        if let cached = pageTexts[page] { return cached }
        guard let chars = characterPages[page], !chars.chars.isEmpty,
              let core = selectionCores[page],
              let value = try? core.range(from: 0, to: chars.chars.count - 1) else { return nil }
        if pageTexts.count > 24 { pageTexts.removeAll() }
        pageTexts[page] = value.text
        return value.text
    }

    func setVocabSentences(_ sentences: [VocabSentence], page: Int) {
        vocabSentences[page] = sentences
    }

    /// 整页翻译（译页）的一个译文片段：一行译文落在原文那一行的**字框顶部留白**里
    /// ——「行间对照」而不是遮住原文。切分/分配/字号全在网页那侧算好
    /// （`_pageTranslateSlices`），这里只按页面缩放画。
    ///
    /// ⚠ `fontScale` 是**按页高归一化**的字号，不是 pt：画的时候乘回该页在屏幕上的
    /// 高度，缩放才跟着页面走。存 pt 的话放大页面译文就还是小的。
    struct TranslationSlice {
        let origin: CGPoint        // 归一化，左上
        let width: Double          // 归一化
        let fontScale: Double      // 归一化字号（× 页面屏幕高度 = 实际字号）
        let text: String
    }
    @Published private(set) var translationSlices: [Int: [TranslationSlice]] = [:] { didSet { refreshDecorations() } }

    func setTranslationSlices(_ slices: [TranslationSlice], page: Int) {
        translationSlices[page] = slices
    }

    /// 一张插图：徽标锚点 + 图框（都归一化）+ 已经生成好的描述。
    /// ⚠ `badge` 可能为空 —— 服务端还没算好锚点。DOM 那侧此时会试四个角并避开正文，
    /// 那要文字层；接管后退成图框右上角内缩，位置与网页不保证一致（记在这里，
    /// 不要以为是 bug）。
    struct Figure: Identifiable {
        let id: String
        let page: Int
        let box: CGRect            // 归一化
        let badge: CGPoint?        // 归一化，徽标中心
        let caption: String
        let desc: String
        let group: Bool
        var attached: Bool
    }
    @Published private(set) var figures: [Int: [Figure]] = [:] { didSet { refreshDecorations() } }

    func setFigures(_ items: [Figure], page: Int) {
        figures[page] = items
    }

    func setFigureAttached(_ attached: Bool, id: String, page: Int) {
        guard var items = figures[page], let index = items.firstIndex(where: { $0.id == id }) else { return }
        items[index].attached = attached
        figures[page] = items
    }

    /// 句子配色：与网页 `SENT_COLORS` 一一对应，按序号取模。
    /// ⚠ 顺序也要一致 —— 同一页同一句在两个表面上必须是同一个颜色，否则
    /// 「刚才那句绿的」在另一个表面上指的是别的句子。
    static func sentenceStroke(_ index: Int) -> Color {
        let palette: [Color] = [
            Color(red: 0.85, green: 0.47, blue: 0.02),   // #d97706 橙
            Color(red: 0.02, green: 0.59, blue: 0.41),   // #059669 绿
            Color(red: 0.15, green: 0.39, blue: 0.92),   // #2563eb 蓝
            Color(red: 0.58, green: 0.20, blue: 0.92),   // #9333ea 紫
            Color(red: 0.86, green: 0.15, blue: 0.47),   // #db2777 粉
            Color(red: 0.03, green: 0.57, blue: 0.70),   // #0891b2 青
        ]
        return palette[((index % palette.count) + palette.count) % palette.count]
    }

    /// 搜索命中：跳过去之后在那一页把命中处亮出来，几秒后自动淡掉。
    ///
    /// ⚠ 网页那条路（`_highlightSearchResultsOnPage`）要 `__charBoxes`，原生接管时
    /// 那一页根本没渲 —— 它会轮询 4.8 秒然后把待办标记清掉，命中永远不亮。
    /// 原生这侧有自己的字符层，自己找自己画。
    @Published private(set) var searchHits: [Int: [CGRect]] = [:] { didSet { refreshDecorations() } }
    private var searchHitExpiry: Task<Void, Never>?

    func highlightSearchHits(query: String, page: Int) {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !needle.isEmpty, needle.count <= 200,
              let chars = characterPages[page], chars.pageWidth > 0, chars.pageHeight > 0 else { return }
        // 与网页同一口径：整页文字拼起来找子串（大小写不敏感），再把命中区间的
        // 字符按行合并成矩形。
        let glyphs = chars.chars
        let text = glyphs.map { $0.c.lowercased() }.joined()
        var rects: [CGRect] = []
        var cursor = text.startIndex
        while let found = text.range(of: needle, range: cursor..<text.endIndex) {
            let start = text.distance(from: text.startIndex, to: found.lowerBound)
            let end = text.distance(from: text.startIndex, to: found.upperBound)
            if start >= 0, end <= glyphs.count, start < end {
                rects.append(contentsOf: Self.mergeRowRects(Array(glyphs[start..<end]),
                                                            width: chars.pageWidth,
                                                            height: chars.pageHeight))
            }
            cursor = found.upperBound
            if rects.count > 400 { break }
        }
        guard !rects.isEmpty else { return }
        searchHits[page] = rects
        searchHitExpiry?.cancel()
        searchHitExpiry = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(6))
            guard let self, !Task.isCancelled else { return }
            self.searchHits = [:]
        }
    }

    /// 同一行相邻的字合成一条矩形（与网页 `_buildRectsFromCharRange` 同口径）。
    private static func mergeRowRects(_ glyphs: [NativeBookOCRCharacter],
                                      width: Double, height: Double) -> [CGRect] {
        var out: [CGRect] = []
        var current: CGRect?
        for glyph in glyphs where glyph.sp == 0 {
            let box = CGRect(x: glyph.x0 / width, y: glyph.y0 / height,
                             width: (glyph.x1 - glyph.x0) / width,
                             height: (glyph.y1 - glyph.y0) / height)
            guard box.width > 0, box.height > 0 else { continue }
            if var cur = current, abs(box.maxY - cur.maxY) <= cur.height * 0.6, box.minX >= cur.minX {
                cur = CGRect(x: cur.minX, y: min(cur.minY, box.minY),
                             width: max(cur.maxX, box.maxX) - cur.minX,
                             height: max(cur.height, box.height))
                current = cur
            } else {
                if let cur = current { out.append(cur) }
                current = box
            }
        }
        if let cur = current { out.append(cur) }
        return out
    }

    /// 这一页的点坐标尺寸（来自原生字符层）。拿它把点坐标换成归一化。
    func characterPageSize(_ page: Int) -> (width: Double, height: Double)? {
        guard let chars = characterPages[page], chars.pageWidth > 0, chars.pageHeight > 0 else { return nil }
        return (chars.pageWidth, chars.pageHeight)
    }
    /// Original records, including IDs, card state, media payload and private ink.
    /// This is a read-only projection; editing still uses the original repository.
    @Published private(set) var notes: [[String: Any]] = []
    var onPosition: ((Position) -> Void)?
    var onSelection: (([CharacterSelection]) -> Void)?
    var onGeometry: (() -> Void)?
    /// 选区菜单里点了划线。带上**点坐标的矩形和页面尺寸** —— 原生这侧自己就能拼出
    /// 完整的高亮记录，不必再让网页层把那一页渲出来取 `__charBoxes`
    /// （`_pdfExactTextPage` 要求 `dataset.loaded === '1'`，那正是双份渲染的来源）。
    /// 矩形格式与阅读器存储一致：[x0, y0, x1, y1]，PDF 点，左上原点。
    struct HighlightRequest {
        let page: Int
        let text: String
        let sentence: String
        let color: String
        let rects: [[Double]]
        let pageWidth: Double
        let pageHeight: Double
    }
    var onHighlight: ((HighlightRequest) -> Void)?
    /// 选区菜单里点了查词/翻译：(页码, 原文, "dict" | "translate")。
    /// 页码、选中串、**所在整句**、模式。
    /// ⚠ 整句不是可选的装饰：同一个词在不同句子里释义不同，网页那侧查词也是带着
    /// 它走的；解释更是靠它把短选区换成整句，否则 AI 只会抱怨"内容不完整"。
    var onLookup: ((Int, String, String, String) -> Void)?
    /// 页码、整句、焦点串。
    var onGrammar: ((Int, String, String) -> Void)?
    /// 点了已有划线。
    var onEditHighlight: ((Highlight) -> Void)?
    /// 页码 + 要重新识别的点坐标矩形。
    var onRecognize: ((Int, CGRect) -> Void)?
    private var access: ReaderLocalBookAccess?
    private var digest = ""
    private var generation = UUID()
    private var observations: [NSObjectProtocol] = []
    private var selectionTask: Task<Void, Never>?
    private var scrollObservation: NSKeyValueObservation?
    private var offsetObservation: NSKeyValueObservation?
    private weak var observedScroll: UIScrollView?
    private var pendingPage: (page: Int, fraction: CGFloat)?
    private var domainHeaders: [ReaderBookUserStateDomainName: (revision: Int64, digest: String)] = [:]
    private var lastPageFrames: [Int: CGRect] = [:]
    private var lastViewBounds = CGRect.null
    /// 用原生字符层把一段原文定位到某页上，返回**点坐标**的矩形 + 页面尺寸。
    ///
    /// ⚠ 存在的理由：网页那几个 AI 划线入口（`__bwReaderHighlightExactText` 等）
    /// 靠 `_pdfExactTextPage` 取 `__charBoxes`，而那要求该页**在网页里渲出来**。
    /// 原生接管正文后网页不再批量渲染，这条路就成了唯一不必渲染的定位方式。
    /// 页还没取到字符层时返回 nil —— 由调用方决定是等还是退回旧路，这里不猜。
    func resolveBinding(page: Int, text: String) -> [String: Any]? {
        guard let chars = characterPages[page], let core = selectionCores[page],
              chars.pageWidth > 0, chars.pageHeight > 0,
              let value = try? core.binding(["text": text]) else { return nil }
        // core 给的 rects 已被 Swift 侧按页宽高归一化（见 ReaderNativePDFSelection），
        // 存储要的是点，这里乘回去 —— 与选区划线同一口径。
        let rects = value.rects.map { rect -> [Double] in
            [rect.minX * chars.pageWidth, rect.minY * chars.pageHeight,
             rect.maxX * chars.pageWidth, rect.maxY * chars.pageHeight]
        }
        guard !rects.isEmpty else { return nil }
        return [
            "page": page, "text": value.text, "indexes": value.indexes,
            "rects": rects, "pageWidth": chars.pageWidth, "pageHeight": chars.pageHeight,
            "quality": value.quality ?? "", "matches": value.matches,
        ]
    }

    private var characterPages: [Int: NativeBookOCRPageCharacters] = [:]
    private var selectionCores: [Int: ReaderNativePDFSelection] = [:]
    private var characterReads: [Int: Task<Void, Never>] = [:]
    private var characterReadTickets: [Int: UUID] = [:]
    private var unavailableCharacterPages = Set<Int>()
    private var textOverlays: [Int: ReaderNativePDFTextOverlay] = [:]
    private var ocrUpdates: AnyCancellable?
    private var customSelection = false
    private var displayCrop: ReaderNativePDFCrop?
    private var followsWidth = true
    private var lastFittedWidth: CGFloat = 0
    private var fittedScale: CGFloat = 0

    override init() {
        super.init()
        view.backgroundColor = UIColor(ReaderNativeTheme.canvas)
        view.displayMode = .singlePageContinuous
        view.displayDirection = .vertical
        view.displayBox = .cropBox
        view.autoScales = false
        view.minScaleFactor = 0.18
        view.maxScaleFactor = 16
        view.pageOverlayViewProvider = self
        let clearTap = UITapGestureRecognizer(target: self, action: #selector(clearSelectionOnBlankTap(_:)))
        clearTap.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        clearTap.cancelsTouchesInView = false
        clearTap.delegate = self
        view.addGestureRecognizer(clearTap)
        view.onLayout = { [weak self] in
            Task { @MainActor in self?.layoutChanged() }
        }
        for name in [Notification.Name.PDFViewPageChanged, .PDFViewScaleChanged] {
            observations.append(NotificationCenter.default.addObserver(forName: name, object: view, queue: .main) { [weak self] _ in
                Task { @MainActor in
                    if name == .PDFViewScaleChanged, let self, self.fittedScale > 0,
                       abs(self.view.scaleFactor - self.fittedScale) > 0.005 {
                        self.followsWidth = false
                    }
                    self?.publishPosition()
                }
            })
        }
        observations.append(NotificationCenter.default.addObserver(forName: .PDFViewSelectionChanged, object: view, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.selectionChanged() }
        })
        ocrUpdates = NativeBookOCRManager.shared.$lastUpdate.compactMap { $0 }.sink { [weak self] update in
            Task { @MainActor in
                guard let self, update.bookID == self.access?.record.id else { return }
                self.clearSelection()
                if let page = update.page {
                    // ⚠ pageText 的缓存也要跟着失效：OCR 改的正是这一页的文字，
                    //   不清的话助手拿到的还是识别前那版 —— 而且它是静默的。
                    self.pageTexts[page] = nil
                    self.characterPages[page] = nil; self.unavailableCharacterPages.remove(page)
                    self.selectionCores[page] = nil; self.textOverlays[page]?.selectionCore = nil
                    self.characterReads[page]?.cancel(); self.characterReads[page] = nil
                    self.characterReadTickets[page] = nil
                    self.textOverlays[page]?.characters = nil
                } else {
                    self.pageTexts = [:]
                    self.characterPages = [:]; self.unavailableCharacterPages = []
                    self.selectionCores = [:]
                    self.textOverlays.values.forEach { $0.selectionCore = nil }
                    self.characterReads.values.forEach { $0.cancel() }; self.characterReads = [:]
                    self.characterReadTickets = [:]
                    self.textOverlays.values.forEach { $0.characters = nil }
                }
                self.loadVisibleCharacterPages()
            }
        }
    }

    deinit {
        observations.forEach(NotificationCenter.default.removeObserver)
        selectionTask?.cancel()
        characterReads.values.forEach { $0.cancel() }
    }

    func open(_ access: ReaderLocalBookAccess, contentSHA256: String, page: Int, fraction: CGFloat = 0) throws {
        guard access.record.format == .pdf,
              contentSHA256.range(of: "^[0-9a-fA-F]{64}$", options: .regularExpression) != nil else {
            throw ReaderLocalLibraryError.bookUnavailable
        }
        guard let document = PDFDocument(url: access.url), !document.isLocked, document.pageCount > 0 else {
            throw ReaderLocalLibraryError.bookUnavailable
        }
        close()
        self.access = access // Retain the original security-scoped lease.
        digest = contentSHA256.lowercased()
        pendingPage = (min(document.pageCount, max(1, page)), fraction.isFinite ? min(1, max(0, fraction)) : 0)
        view.document = document
        ready = true
        view.setNeedsLayout()
    }

    func close() {
        generation = UUID()
        selectionTask?.cancel(); selectionTask = nil
        scrollObservation = nil; offsetObservation = nil; observedScroll = nil
        view.document = nil
        view.displayBox = .cropBox; displayCrop = nil
        followsWidth = true; lastFittedWidth = 0; fittedScale = 0
        access = nil; digest = ""; pendingPage = nil
        ready = false; error = nil
        domainHeaders = [:]; ink = [:]; highlights = [:]; notes = []
        lastPageFrames = [:]; lastViewBounds = .null
        characterReads.values.forEach { $0.cancel() }; characterReads = [:]
        characterReadTickets = [:]
        characterPages = [:]; selectionCores = [:]; textOverlays = [:]; unavailableCharacterPages = []; customSelection = false
        onSelection?([])
    }

    func matches(bookID: String, contentSHA256: String) -> Bool {
        ready && access?.record.id == bookID && digest == contentSHA256.lowercased()
    }

    /// Read-only projection of the original atomic export. It neither imports
    /// nor rewrites notes, and rejects old or conflicting domain revisions.
    func applyOverlays(_ domains: [ReaderBookUserStateDomainPayload], bookID: String, contentSHA256: String) throws {
        guard access?.record.id == bookID, digest == contentSHA256.lowercased(), ready else {
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        let required: Set<ReaderBookUserStateDomainName> = [.ink, .closedRegions, .highlights]
        let selected = domains.filter { required.contains($0.name) }
        guard Set(selected.map(\.name)) == required, selected.count == required.count else {
            throw ReaderBookUserStateWebAdapterError.invalidResponse
        }
        var nextInk: [Int: [ReaderNativeCardStroke]] = [:]
        var nextHighlights: [Int: [Highlight]] = [:]
        for domain in selected {
            _ = try ReaderBookUserStatePackageCodec.validateDomainPayload(domain)
            if let previous = domainHeaders[domain.name] {
                guard domain.revision >= previous.revision,
                      domain.revision != previous.revision || domain.digest == previous.digest else {
                    throw ReaderBookUserStateWebAdapterError.contextChanged
                }
            }
            let data = try JSONSerialization.jsonObject(with: Data(domain.payloadJson.utf8)) as? [String: Any] ?? [:]
            if domain.name == .highlights {
                for value in data["pdf"] as? [[String: Any]] ?? [] {
                    guard let number = (value["page"] as? NSNumber)?.intValue, number > 0,
                          let page = view.document?.page(at: number - 1) else { continue }
                    let size = displayedSize(page)
                    let width = (value["page_w"] as? NSNumber)?.doubleValue ?? Double(size.width)
                    let height = (value["page_h"] as? NSNumber)?.doubleValue ?? Double(size.height)
                    guard width.isFinite, height.isFinite, width > 0, height > 0 else { continue }
                    // 空 color 是「无色」划线（只有备注那种），网页画虚框。不要拿黄色兜底，
                    // 那会把用户刻意取消掉的颜色又涂回去。
                    let hex = value["color"] as? String ?? ""
                    let color = ReaderNativeCardStroke(["pts": [[0,0]], "c": hex.isEmpty ? "#fff59d" : hex])?.color ?? .yellow
                    var boxes: [CGRect] = []
                    for rect in value["rects"] as? [[NSNumber]] ?? [] {
                        guard rect.count == 4, rect.allSatisfy({ $0.doubleValue.isFinite }) else { continue }
                        let box = CGRect(x: rect[0].doubleValue / width, y: rect[1].doubleValue / height,
                                         width: (rect[2].doubleValue - rect[0].doubleValue) / width,
                                         height: (rect[3].doubleValue - rect[1].doubleValue) / height)
                        if box.width > 0, box.height > 0 { boxes.append(box) }
                    }
                    guard !boxes.isEmpty else { continue }
                    let id = value["id"] as? String ?? ""
                    nextHighlights[number, default: []].append(
                        Highlight(id: id.isEmpty ? "\(number):\(boxes.count):\(hex)" : id,
                                  page: number, rects: boxes, color: color, colorKey: hex,
                                  note: value["note"] as? String ?? "",
                                  text: value["text"] as? String ?? value["sentence"] as? String ?? ""))
                }
            } else {
                for (surface, strokes) in data["pdf"] as? [String: [[String: Any]]] ?? [:] {
                    guard let number = Int(surface), number > 0, number <= (view.document?.pageCount ?? 0) else { continue }
                    nextInk[number, default: []].append(contentsOf: strokes.compactMap(ReaderNativeCardStroke.init))
                }
            }
        }
        // Commit all three projections together, after every digest is checked.
        for domain in selected { domainHeaders[domain.name] = (domain.revision, domain.digest) }
        ink = nextInk; highlights = nextHighlights
    }

    func go(to page: Int, fraction: CGFloat = 0) throws {
        guard let document = view.document, page >= 1, page <= document.pageCount,
              fraction.isFinite, fraction >= 0, fraction <= 1,
              let target = document.page(at: page - 1) else { throw NativeBookOCRError.pageUnavailable }
        view.go(to: target)
        if fraction > 0 {
            let box = view.convert(target.bounds(for: .cropBox), from: target).standardized
            let point = view.convert(CGPoint(x: box.minX, y: box.minY + fraction * box.height), to: target)
            view.go(to: PDFDestination(page: target, at: point))
        }
        publishPosition()
    }

    /// 行首「译」与图徽标的回调（原来是视口 SwiftUI 层的参数，现在按钮长在页面里）。
    var onTranslateSentence: ((VocabSentence) -> Void)?
    var onOpenFigure: ((Figure) -> Void)?

    /// 页面装饰（划线 / 生词句排线 / 搜索命中 / 生词下划线 / 振假名 / 译文 / 已带入的图 / 墨迹）
    /// 与行首按钮，全部画在**每一页自己的 overlay** 里。
    ///
    /// ⚠ 原来它们画在 PDFView 之上的一张 SwiftUI Canvas 里，靠 `geometryRevision`
    ///   在滚动后重画 —— 必然慢一帧，滚动时就是残影（2026-09-23 用户："无论是卡片
    ///   还是那个线框都还是会随着滚动留下残影"）。overlay 是页面的子视图，跟页面同一帧走。
    private func refreshDecorations() {
        for (number, overlay) in textOverlays {
            overlay.decorationButtons = decorationButtons(page: number)
            overlay.setNeedsDisplay()
        }
    }

    /// 把最新的锁定框推给每一页的 overlay。
    /// ⚠ 便签变了、字符层刚加载完，都要重推 —— 否则框要等到那一页重新挂 overlay
    /// 才出现（翻回来才看得见，等于"有时有有时没有"）。
    private func refreshCardMarkers() {
        for (number, overlay) in textOverlays {
            overlay.cardMarkers = cardMarkers(page: number)
        }
    }

    func applyNotes(_ domain: ReaderBookUserStateDomainPayload, bookID: String, contentSHA256: String) throws {
        guard domain.name == .notes, access?.record.id == bookID,
              digest == contentSHA256.lowercased(), ready else { throw ReaderBookUserStateWebAdapterError.contextChanged }
        _ = try ReaderBookUserStatePackageCodec.validateDomainPayload(domain)
        if let previous = domainHeaders[.notes] {
            guard domain.revision >= previous.revision,
                  domain.revision != previous.revision || domain.digest == previous.digest else {
                throw ReaderBookUserStateWebAdapterError.contextChanged
            }
        }
        guard let records = try JSONSerialization.jsonObject(with: Data(domain.payloadJson.utf8)) as? [[String: Any]] else {
            throw ReaderBookUserStateWebAdapterError.invalidResponse
        }
        // Do not silently drop a malformed/duplicate original ID. Unrenderable
        // payloads remain intact so their type can be migrated independently.
        let ids = records.compactMap { $0["id"] as? String }
        guard ids.count == records.count, Set(ids).count == ids.count, ids.allSatisfy({ !$0.isEmpty }) else {
            throw ReaderBookUserStateWebAdapterError.invalidResponse
        }
        notes = records
        domainHeaders[.notes] = (domain.revision, domain.digest)
        refreshCardMarkers()
    }

    /// Resolve saved anchors through PDFKit and the original character-binding
    /// rules. No DOM frame, CSS zoom or newly assigned card identity is involved.
    func noteGeometry(_ note: [String: Any], presentationSize: CGSize? = nil, in target: UIView? = nil) -> NoteGeometry? {
        guard let id = note["id"] as? String, let anchor = note["anchor"] as? [String: Any],
              anchor["kind"] as? String == "pdf", let number = anchor["page"] as? NSNumber,
              number.doubleValue == Double(number.intValue),
              let pageRect = viewRect(normalized: CGRect(x: 0, y: 0, width: 1, height: 1), page: number.intValue, in: target) else { return nil }
        let payload = note["card"] as? [String: Any] ?? note["html"] as? [String: Any] ?? [:]
        let x = (anchor["x"] as? NSNumber)?.doubleValue ?? 0
        let y = (anchor["y"] as? NSNumber)?.doubleValue ?? 0
        let w = (note["w"] as? NSNumber)?.doubleValue ?? 300
        let h = (note["h"] as? NSNumber)?.doubleValue ?? 180
        let base = (payload["base_w"] as? NSNumber)?.doubleValue ?? 0
        guard [x, y, w, h, base].allSatisfy(\.isFinite), w > 0, h > 0 else { return nil }
        let collapsed = note["collapsed"] as? Bool == true || ["dot", "min"].contains(payload["form"] as? String ?? "")
        let ratio = base > 0 ? pageRect.width / base : 1
        let preferred = presentationSize ?? CGSize(width: max(140, w * ratio), height: h * ratio)
        guard preferred.width.isFinite, preferred.height.isFinite, preferred.width > 0, preferred.height > 0 else { return nil }
        let rect = CGRect(x: pageRect.minX + min(1, max(0, x)) * pageRect.width,
                          y: pageRect.minY + min(1, max(0, y)) * pageRect.height,
                          width: collapsed ? 44 : preferred.width, height: collapsed ? 44 : preferred.height)
        let bind = payload["bind"] as? [String: Any]
        var resolved: ReaderNativePDFSelection.Value?
        var bindingRects: [CGRect] = []
        if let bind, bind["kind"] as? String == "page-chars", let boundPage = bind["page"] as? NSNumber,
           boundPage.doubleValue == Double(boundPage.intValue), let core = selectionCores[boundPage.intValue] {
            resolved = try? core.binding(bind)
            bindingRects = resolved?.rects.compactMap { viewRect(normalized: $0, page: boundPage.intValue, in: target) } ?? []
        }
        return NoteGeometry(id: id, page: number.intValue, rect: rect, bindingRects: bindingRects,
                            bindingQuality: resolved?.quality, bindingMatches: resolved?.matches ?? 0,
                            bindingUnresolved: bind?["kind"] as? String == "page-chars" && bindingRects.isEmpty)
    }

    /// 绑定在正文上的卡片「锁定框」——**按页**给出，坐标系就是 PDFView 自己的。
    ///
    /// ⚠ 这些框必须画在跟着页面滚的那一层里（Canvas / 真控件层）。原来它们画在
    /// `ReaderNativePageCards`（整个工作区之上的另一层 SwiftUI overlay）里、按
    /// **窗口坐标**摆位，于是滚动时总慢半拍、还留残影，点也点不中
    /// （2026-09-22 用户连报两次："没跟紧画面而是有延迟还卡顿"、"还是有残影，
    /// 而且点击后根本打不开卡片"）。
    func cardMarkers(page: Int) -> [CardMarker] {
        notes.compactMap { note -> CardMarker? in
            guard let id = note["id"] as? String,
                  let payload = note["card"] as? [String: Any] ?? note["html"] as? [String: Any],
                  let bind = payload["bind"] as? [String: Any],
                  bind["kind"] as? String == "page-chars",
                  let bound = bind["page"] as? NSNumber, bound.intValue == page,
                  let core = selectionCores[page],
                  let value = try? core.binding(bind) else { return nil }
            // ⚠ 给**归一化**框，不给 view 坐标：消费方是页面自己的 overlay view，
            //   它用 project 投影到自己的坐标系，然后跟着页面一起滚。
            guard !value.rects.isEmpty else { return nil }
            let slot = note["card"] is [String: Any] ? "card" : "html"
            return CardMarker(id: id, rects: value.rects,
                              tone: ReaderNativeMarkerStyle.tone(payload, slot: slot),
                              open: openCardIDs.contains(id))
        }
    }

    /// 正展开着的词锚卡。由页卡层按 placement.open 推进来；变了就重画锁定框。
    var openCardIDs: Set<String> = [] {
        didSet { if openCardIDs != oldValue { refreshCardMarkers() } }
    }

    fileprivate func drawDecorations(page number: Int, context: CGContext, project: (CGRect) -> CGRect?) {
        guard let frame = project(CGRect(x: 0, y: 0, width: 1, height: 1)) else { return }
        context.saveGState()
        defer { context.restoreGState() }
        context.clip(to: frame)
        for highlight in highlights[number] ?? [] {
            for normalized in highlight.rects {
                guard let rect = project(normalized) else { continue }
                if highlight.colorKey.isEmpty {
                    // 「无色」划线：网页画虚框（只有备注、不涂色）。
                    context.setStrokeColor(UIColor(ReaderNativeTheme.accent).withAlphaComponent(0.7).cgColor)
                    context.setLineWidth(1)
                    context.setLineDash(phase: 0, lengths: [3, 2])
                    context.stroke(rect)
                    context.setLineDash(phase: 0, lengths: [])
                } else {
                    context.setFillColor(UIColor(highlight.color).withAlphaComponent(0.3).cgColor)
                    context.fill(rect)
                }
            }
        }
        // 生词句子：135° 排线 + 细边框（网页 repeating-linear-gradient 的同一观感）。
        for sentence in vocabSentences[number] ?? [] {
            let stroke = UIColor(ReaderNativePDFDocument.sentenceStroke(sentence.index))
            for normalized in sentence.rects {
                guard let rect = project(normalized), rect.width > 1, rect.height > 1 else { continue }
                context.saveGState()
                context.clip(to: rect)
                context.setStrokeColor(stroke.withAlphaComponent(0.33).cgColor)
                context.setLineWidth(1)
                var x = rect.minX - rect.height
                while x < rect.maxX {
                    context.move(to: CGPoint(x: x, y: rect.maxY))
                    context.addLine(to: CGPoint(x: x + rect.height, y: rect.minY))
                    x += 4
                }
                context.strokePath()
                context.restoreGState()
                context.setStrokeColor(stroke.withAlphaComponent(0.45).cgColor)
                context.setLineWidth(0.8)
                context.stroke(rect)
            }
        }
        // 搜索命中：黄底。
        context.setFillColor(UIColor.yellow.withAlphaComponent(0.38).cgColor)
        for normalized in searchHits[number] ?? [] {
            if let rect = project(normalized) { context.fill(rect) }
        }
        // 生词下划线画在字底（与网页一致：y1 再下移 1pt）。
        for mark in vocabMarks[number] ?? [] {
            let thickness = ReaderNativeVocabPalette.thickness(mark.slug)
            guard thickness > 0 else { continue }
            context.setFillColor(UIColor(ReaderNativeVocabPalette.color(mark.slug)).cgColor)
            for normalized in mark.rects {
                guard let rect = project(normalized) else { continue }
                context.fill(CGRect(x: rect.minX, y: rect.maxY, width: rect.width, height: thickness))
            }
        }
        // 振假名：字号与位置沿用网页 _makeRubySpan（fs = max(7, min(词高*0.36, 词宽/读音字数))，
        // top = y0 - fs*0.34）。
        if let size = characterPageSize(number) {
            let centered = NSMutableParagraphStyle()
            centered.alignment = .center
            for item in furigana(page: number) {
                guard let rt = item.rt, let x0 = item.x0, let y0 = item.y0, let x1 = item.x1, let y1 = item.y1,
                      let box = project(CGRect(x: x0 / size.width, y: y0 / size.height,
                                               width: (x1 - x0) / size.width, height: (y1 - y0) / size.height))
                else { continue }
                let w = max(6, box.width), h = max(6, box.height)
                let fontSize = max(7, min(h * 0.36, w / CGFloat(max(1, rt.count))))
                (rt as NSString).draw(
                    in: CGRect(x: box.minX, y: max(frame.minY, box.minY - fontSize * 0.34), width: w,
                               height: fontSize * 1.2),
                    withAttributes: [.font: UIFont.systemFont(ofSize: fontSize),
                                     .foregroundColor: UIColor(ReaderNativeTheme.ink),
                                     .paragraphStyle: centered])
            }
        }
        // 整页翻译：行间小字，白底半透明 + 深蓝 600 字重 + 左对齐（照 .page-tr-rt）。
        let translationInk = UIColor(red: 0.043, green: 0.239, blue: 0.569, alpha: 1)
        for slice in translationSlices[number] ?? [] {
            let fontSize = slice.fontScale * frame.height
            guard fontSize >= 4 else { continue }
            let box = CGRect(x: frame.minX + slice.origin.x * frame.width, y: frame.minY + slice.origin.y * frame.height,
                             width: slice.width * frame.width, height: fontSize * 1.2)
            context.setFillColor(UIColor.white.withAlphaComponent(0.86).cgColor)
            context.addPath(UIBezierPath(roundedRect: box.insetBy(dx: -1, dy: 0), cornerRadius: 2).cgPath)
            context.fillPath()
            let font = UIFont.systemFont(ofSize: fontSize, weight: .semibold)
            (slice.text as NSString).draw(at: CGPoint(x: box.minX, y: box.midY - font.lineHeight / 2),
                                          withAttributes: [.font: font, .foregroundColor: translationInk])
        }
        // 已带入助手的图：持久绿框（.fig-hl-sel）。
        let green = UIColor(red: 0.188, green: 0.820, blue: 0.345, alpha: 1)
        for figure in figures[number] ?? [] where figure.attached {
            guard let rect = project(figure.box) else { continue }
            let path = UIBezierPath(roundedRect: rect, cornerRadius: 7)
            context.setFillColor(green.withAlphaComponent(0.12).cgColor)
            context.addPath(path.cgPath); context.fillPath()
            context.setStrokeColor(green.withAlphaComponent(0.95).cgColor)
            context.setLineWidth(2.5)
            context.addPath(path.cgPath); context.strokePath()
        }
        for stroke in ink[number] ?? [] {
            ReaderNativeInkDrawing.draw(stroke, in: frame, cgContext: context)
        }
    }

    /// 行首「译」与图徽标 —— 真控件，长在页面 overlay 里。
    fileprivate func decorationButtons(page number: Int) -> [ReaderNativePageButton] {
        var buttons: [ReaderNativePageButton] = []
        for sentence in vocabSentences[number] ?? [] {
            guard let first = sentence.rects.first else { continue }
            buttons.append(ReaderNativePageButton(
                id: "tr-" + sentence.id, kind: .translate, anchor: first, badge: nil,
                tint: UIColor(ReaderNativePDFDocument.sentenceStroke(sentence.index)),
                label: "翻译整句") { [weak self] in self?.onTranslateSentence?(sentence) })
        }
        for figure in figures[number] ?? [] {
            buttons.append(ReaderNativePageButton(
                id: "fig-" + figure.id, kind: .figure, anchor: figure.box, badge: figure.badge,
                tint: figure.attached ? UIColor(red: 0.188, green: 0.820, blue: 0.345, alpha: 1)
                                      : UIColor(ReaderNativeTheme.accent),
                label: figure.caption.isEmpty ? "图说明" : figure.caption) { [weak self] in self?.onOpenFigure?(figure) })
        }
        return buttons
    }

    func setSpread(_ enabled: Bool, firstPageAlone: Bool) {
        setLayout(mode: enabled ? "spread" : "continuous", firstPageAlone: firstPageAlone)
    }

    var layoutMode: String {
        switch view.displayMode {
        case .singlePage: return "single"
        case .twoUp, .twoUpContinuous: return "spread"
        default: return "continuous"
        }
    }

    func setScale(_ scale: CGFloat) {
        guard scale.isFinite, scale > 0 else { return }
        followsWidth = false
        view.autoScales = false
        view.scaleFactor = max(view.minScaleFactor, min(view.maxScaleFactor, scale))
        publishPosition()
    }

    /// Apply the original per-book percentages to a display-only box. The PDF
    /// file and its crop box stay untouched, so OCR/card/ink coordinates retain
    /// their original identity. The view's document is never written to disk.
    func setCrop(_ crop: ReaderNativePDFCrop?) throws {
        guard let document = view.document else { throw NativeBookOCRError.pageUnavailable }
        let destination = view.currentDestination
        if let crop {
            var boxes: [(PDFPage, CGRect)] = []
            for index in 0..<document.pageCount {
                guard let page = document.page(at: index), let box = crop.bounds(for: page) else { throw NativeBookOCRError.pageUnavailable }
                boxes.append((page, box))
            }
            // Validate every page before changing any display box.
            boxes.forEach { $0.0.setBounds($0.1, for: .artBox) }
        }
        displayCrop = crop
        view.displayBox = crop == nil ? .cropBox : .artBox
        view.layoutDocumentView()
        if let destination { view.go(to: destination) }
        if view.bounds.width > 0 { fitWidth() }
        geometryChanged()
    }

    func setLayout(mode: String, firstPageAlone: Bool) {
        let destination = view.currentDestination
        view.displaysAsBook = firstPageAlone
        view.displayMode = mode == "spread" ? .twoUpContinuous : (mode == "single" ? .singlePage : .singlePageContinuous)
        view.autoScales = false
        if let destination { view.go(to: destination) }
        publishPosition()
    }

    func fitWidth() {
        guard let page = view.currentPage else { return }
        let size = displayedSize(page)
        guard size.width > 0, view.bounds.width > 0 else { return }
        view.autoScales = false
        let columns: CGFloat = (view.displayMode == .twoUpContinuous || view.displayMode == .twoUp) ? 2 : 1
        let width = size.width * (displayCrop?.width ?? 1)
        let fitted = (view.bounds.width - 16) / (width * columns)
        followsWidth = true; lastFittedWidth = view.bounds.width
        view.maxScaleFactor = max(view.maxScaleFactor, fitted)
        fittedScale = max(view.minScaleFactor, min(view.maxScaleFactor, fitted))
        view.scaleFactor = fittedScale
        publishPosition()
    }

    /// Canonical OCR/ink coordinates are top-left after the PDF's declared
    /// rotation. PDFKit converts the crop box, including its nonzero origin;
    /// applying the page rotation again here would mirror 90/270 degree pages.
    func viewRect(normalized rect: CGRect, page number: Int, in target: UIView? = nil) -> CGRect? {
        guard number > 0, rect.isFiniteRect, let page = view.document?.page(at: number - 1) else { return nil }
        let box = view.convert(page.bounds(for: .cropBox), from: page).standardized
        guard box.isFiniteRect, box.width > 0, box.height > 0 else { return nil }
        let result = CGRect(x: box.minX + rect.minX * box.width, y: box.minY + rect.minY * box.height,
                            width: rect.width * box.width, height: rect.height * box.height)
        return target.map { view.convert(result, to: $0) } ?? result
    }

    func canonicalPoint(_ point: CGPoint, from source: UIView) -> (page: Int, point: CGPoint)? {
        let local = source.convert(point, to: view)
        guard let document = view.document, let page = view.page(for: local, nearest: false) else { return nil }
        let box = view.convert(page.bounds(for: .cropBox), from: page).standardized
        let displayed = view.convert(page.bounds(for: view.displayBox), from: page).standardized
        guard box.isFiniteRect, box.width > 0, box.height > 0, displayed.contains(local) else { return nil }
        return (document.index(for: page) + 1, CGPoint(x: (local.x - box.minX) / box.width, y: (local.y - box.minY) / box.height))
    }

    /// 拖卡时的落点预览：**与松手落点同源**。
    ///
    /// ⚠ 不要改走网页的 `anchorFromPoint`：那条路把落点当网页视口坐标，
    ///   原生接管正文后视口里根本没有那一页 —— 会出现"预览说钉这儿、
    ///   松手却钉别处"。这里跟 `moveNativeCard` 一样先 `canonicalPoint` 定页，
    ///   再用**同一份** pdf-selection-core 认词，判据一字不差。
    ///
    /// 语义沿用网页那版（rc-stickynote #51）：认得出词就给词框（光带＝绑定内容），
    /// 认不出就给一条横线（＝插入位置）。返回 `view` 自己的坐标系。
    func dropPreview(_ local: CGPoint) -> ReaderNativeDropPreview? {
        guard let placed = canonicalPoint(local, from: view) else { return nil }
        // 光带 = 松手后会钉住的那个**词**（与 wordBind 同一套规则，预览说什么松手就是什么）。
        if let word = wordBind(at: local), let core = selectionCores[placed.page],
           let value = try? core.exact(word.indexes), !value.rects.isEmpty {
            let rects = value.rects.compactMap { viewRect(normalized: $0, page: placed.page) }
            if !rects.isEmpty { return ReaderNativeDropPreview(rects: rects, line: nil) }
        }
        // 字符层还没加载完的页也要有反馈 —— 否则拖过去就是一片什么都没有，
        // 跟"这里钉不住"长得一模一样。
        let y = max(0, min(1, placed.point.y))
        guard let line = viewRect(normalized: CGRect(x: 0, y: y, width: 1, height: 0.0015),
                                  page: placed.page) else { return nil }
        return ReaderNativeDropPreview(rects: [], line: line)
    }

    struct WordBind {
        let page: Int
        let indexes: [Int]
        let text: String
        /// 与网页 wordBindFromPoint 同形：{kind:'page-chars', page, from, to, text, ois}。
        var payload: [String: Any] {
            ["kind": "page-chars", "page": page, "from": indexes.first ?? 0, "to": indexes.last ?? 0,
             "text": String(text.prefix(200)), "ois": Array(indexes.prefix(512))]
        }
    }

    /// 落点处的词 —— 逐条照网页 `noteWordRect`（27-rc-adapter.js）：
    /// 先找**落点左侧、同一行**最近的字（行带 = ±0.75 字高，至少 14）；同行没有才退全局最近；
    /// 离得太远（屏幕上 > 48 点）算没认到；再按同一个词 id（w）、同一区块聚成整词。
    ///
    /// ⚠ 原生自己认，不交给网页：网页按它自己的视口坐标 elementFromPoint 找页，而原生接管后
    ///   网页视口跟屏幕上的页对不上（2026-09-23：拖卡后词锚跑到「インフルエンザ」「よっ」上）。
    func wordBind(at local: CGPoint) -> WordBind? {
        guard let placed = canonicalPoint(local, from: view),
              let chars = characterPages[placed.page], chars.pageWidth > 0, chars.pageHeight > 0 else { return nil }
        let px = Double(placed.point.x) * chars.pageWidth, py = Double(placed.point.y) * chars.pageHeight
        var best: Int?, bestDistance = Double.greatestFiniteMagnitude
        var row: Int?, rowDistance = Double.greatestFiniteMagnitude
        for (index, char) in chars.chars.enumerated() where char.sp == 0 && char.x1 > char.x0 {
            let cx = (char.x0 + char.x1) / 2, cy = (char.y0 + char.y1) / 2
            let height = max(char.y1 - char.y0, 1)
            if abs(cy - py) <= max(height, 14) * 0.75, cx <= px, px - cx < rowDistance {
                rowDistance = px - cx; row = index
            }
            let d = (cx - px) * (cx - px) + (cy - py) * (cy - py)
            if d < bestDistance { bestDistance = d; best = index }
        }
        guard let hit = row ?? best else { return nil }
        let distance = row != nil ? rowDistance : bestDistance.squareRoot()
        // 屏幕上超过 48 点就算没落在词上（网页同一阈值，单位是屏幕像素）。
        let pointsPerUnit = Double(view.scaleFactor)
        guard distance * pointsPerUnit <= 48 else { return nil }
        let target = chars.chars[hit]
        let indexes = target.w >= 0
            ? chars.chars.indices.filter { chars.chars[$0].w == target.w && chars.chars[$0].b == target.b && chars.chars[$0].sp == 0 }
            : [hit]
        guard !indexes.isEmpty else { return nil }
        let text = indexes.map { chars.chars[$0].c }.joined()
        guard !text.isEmpty else { return nil }
        return WordBind(page: placed.page, indexes: indexes, text: text)
    }

    /// 页码 + 页内归一化坐标（与 canonicalPoint 同源）。
    func pagePoint(at local: CGPoint) -> (page: Int, point: CGPoint)? {
        canonicalPoint(local, from: view)
    }

    private func layoutChanged() {    private func layoutChanged() {
        // Observe actual native scrolling; no timer polls or web scroll relay.
        func firstScroll(_ root: UIView) -> UIScrollView? {
            if let scroll = root as? UIScrollView { return scroll }
            for child in root.subviews { if let scroll = firstScroll(child) { return scroll } }
            return nil
        }
        if let scroll = firstScroll(view), scroll !== observedScroll {
            observedScroll = scroll
            offsetObservation = scroll.observe(\.contentOffset, options: [.new]) { [weak self] _, _ in
                Task { @MainActor in self?.geometryChanged() }
            }
            scrollObservation = scroll.observe(\.contentSize, options: [.new]) { [weak self] _, _ in
                Task { @MainActor in self?.geometryChanged() }
            }
        }
        if let page = pendingPage, view.bounds.width > 0, view.bounds.height > 0 {
            pendingPage = nil
            try? go(to: page.page, fraction: page.fraction)
            if followsWidth { fitWidth(); try? go(to: page.page, fraction: page.fraction) }
        } else if followsWidth, view.bounds.width > 16, abs(view.bounds.width - lastFittedWidth) > 0.5 {
            let anchor = position
            fitWidth()
            try? go(to: anchor.page, fraction: anchor.fraction)
        }
        geometryChanged()
    }

    private func publishPosition() {
        updatePosition()
        geometryChanged()
    }

    private func updatePosition() {
        guard let document = view.document, let page = view.currentPage else { return }
        let box = view.convert(page.bounds(for: .cropBox), from: page).standardized
        let fraction = box.height > 0 ? min(1, max(0, (view.bounds.minY - box.minY) / box.height)) : 0
        let next = Position(page: document.index(for: page) + 1, scale: view.scaleFactor,
                            visiblePages: view.visiblePages.map { document.index(for: $0) + 1 }.sorted(), fraction: fraction,
                            mode: layoutMode, spreadOffset: view.displaysAsBook ? 1 : 0, crop: displayCrop)
        if next != position { position = next; onPosition?(next) }
    }

    private func geometryChanged() {
        var frames: [Int: CGRect] = [:]
        if let document = view.document {
            for page in view.visiblePages {
                frames[document.index(for: page) + 1] = view.convert(page.bounds(for: .cropBox), from: page)
            }
        }
        guard frames != lastPageFrames || view.bounds != lastViewBounds else { return }
        lastPageFrames = frames; lastViewBounds = view.bounds
        geometryRevision &+= 1; onGeometry?()
        loadVisibleCharacterPages()
        // Content offset can change within the same PDF page without a page
        // notification. Publish the page-relative reading anchor as well.
        updatePosition()
    }

    private func selectionChanged() {
        if customSelection && view.currentSelection == nil { return }
        if customSelection { textOverlays.values.forEach { $0.clearSelection() }; customSelection = false }
        selectionTask?.cancel()
        guard let selection = view.currentSelection, let document = view.document, let access else {
            onSelection?([]); return
        }
        let ticket = generation, digest = digest
        // Capture line rectangles before awaiting OCR. Never re-read a newer
        // PDFSelection and accidentally attribute it to an earlier gesture.
        let pages = selection.pages.map { page in
            (document.index(for: page) + 1, page,
             selection.selectionsByLine().filter { $0.pages.contains(page) }.map { $0.bounds(for: page) })
        }
        selectionTask = Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                var result: [CharacterSelection] = []
                for (number, page, lineBoxes) in pages {
                    try Task.checkCancellation()
                    guard let chars = try await NativeBookOCRManager.shared.readerPageCharacters(book: access, expectedContentSHA256: digest, page: number),
                          chars.contentSHA256.lowercased() == digest, chars.status == .ready,
                          chars.pageWidth > 0, chars.pageHeight > 0 else {
                        throw NativeBookOCRError.pageUnavailable
                    }
                    guard generation == ticket, self.access === access else { return }
                    let pageBox = view.convert(page.bounds(for: .cropBox), from: page).standardized
                    guard pageBox.isFiniteRect, pageBox.width > 0, pageBox.height > 0 else { return }
                    let displayed = displayedSize(page)
                    guard abs(Double(displayed.width / displayed.height) - chars.pageWidth / chars.pageHeight) < 0.005 else {
                        throw NativeBookOCRError.pageUnavailable
                    }
                    let boxes = lineBoxes.map { box -> CGRect in
                        let projected = view.convert(box, from: page).standardized
                        return CGRect(x: (projected.minX - pageBox.minX) / pageBox.width,
                                      y: (projected.minY - pageBox.minY) / pageBox.height,
                                      width: projected.width / pageBox.width, height: projected.height / pageBox.height)
                    }
                    let indexes = chars.chars.indices.filter { index in
                        let char = chars.chars[index]
                        let box = CGRect(x: char.x0 / chars.pageWidth, y: char.y0 / chars.pageHeight,
                                         width: (char.x1 - char.x0) / chars.pageWidth, height: (char.y1 - char.y0) / chars.pageHeight)
                        guard box.isFiniteRect, box.width > 0, box.height > 0 else { return false }
                        return boxes.contains { $0.contains(CGPoint(x: box.midX, y: box.midY)) }
                    }
                    let sameLayer = characterPages[number]?.geometryDigest == chars.geometryDigest &&
                                    characterPages[number]?.engineRevision == chars.engineRevision
                    let core = try (sameLayer ? selectionCores[number] : nil) ?? ReaderNativePDFSelection(chars)
                    guard let resolved = try core.exact(indexes) else { throw NativeBookOCRError.pageUnavailable }
                    result.append(CharacterSelection(bookID: access.record.id, contentSHA256: digest, page: number,
                        geometryDigest: chars.geometryDigest, indexes: resolved.indexes,
                        text: resolved.text, sentence: resolved.sentence, rects: resolved.rects))
                }
                guard generation == ticket, !Task.isCancelled else { return }
                error = nil; onSelection?(result)
            } catch is CancellationError { return }
            catch {
                guard generation == ticket, !Task.isCancelled else { return }
                self.error = "当前文字层尚不能确认这段选区的位置，请等待文字层就绪。"
                onSelection?([])
            }
        }
    }

    private func displayedSize(_ page: PDFPage) -> CGSize {
        let box = page.bounds(for: .cropBox)
        let rotation = (page.rotation % 360 + 360) % 360
        return rotation == 90 || rotation == 270 ? CGSize(width: box.height, height: box.width) : box.size
    }

    func clearSelection() {
        selectionTask?.cancel(); customSelection = false
        textOverlays.values.forEach { $0.clearSelection() }
        view.clearSelection(); onSelection?([])
    }

    // A scan has no PDFKit text selection to dismiss. Observe a finger tap on
    // blank paper without consuming scrolling, Pencil, handles or text taps.
    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        guard customSelection else { return false }
        if let touched = touch.view, touched is UIControl || touched is ReaderNativePDFSelectionHandle { return false }
        return !textOverlays.values.contains { overlay in
            overlay.window != nil && overlay.point(inside: touch.location(in: overlay), with: nil)
        }
    }
    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                           shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool { true }
    @objc private func clearSelectionOnBlankTap(_ gesture: UITapGestureRecognizer) {
        if gesture.state == .ended { clearSelection() }
    }

    func pdfView(_ view: PDFView, overlayViewFor page: PDFPage) -> UIView? {
        guard let document = view.document, page.document === document else { return nil }
        let number = document.index(for: page) + 1
        if let current = textOverlays[number] { return current }
        let overlay = ReaderNativePDFTextOverlay()
        overlay.characters = characterPages[number]
        overlay.selectionCore = selectionCores[number]
        overlay.canonicalPoint = { [weak self, weak overlay] point in
            guard let self, let overlay, let resolved = self.canonicalPoint(point, from: overlay), resolved.page == number else { return nil }
            return resolved.point
        }
        overlay.project = { [weak self, weak overlay] rect in
            guard let self, let overlay else { return nil }
            return self.viewRect(normalized: rect, page: number, in: overlay)
        }
        overlay.cardMarkers = cardMarkers(page: number)
        overlay.onOpenCard = { [weak self] id in self?.onOpenCard?(id) }
        overlay.decorate = { [weak self] context, project in
            self?.drawDecorations(page: number, context: context, project: project)
        }
        overlay.decorationButtons = decorationButtons(page: number)
        overlay.onSelect = { [weak self] value in self?.acceptOCRSelection(value, page: number) }
        overlay.onError = { [weak self] in self?.error = "当前文字层无法确认这段选区的位置。" }
        overlay.onHighlight = { [weak self] value, color in
            guard let self, let chars = self.characterPages[number] else { return }
            // 归一化矩形还原成点坐标：选区核心本来给的就是点，Swift 侧只为了绘制
            // 才除过一次（ReaderNativePDFSelection: x0/width …）。存储要的是点。
            let rects = value.rects.map { rect -> [Double] in
                [rect.minX * chars.pageWidth, rect.minY * chars.pageHeight,
                 rect.maxX * chars.pageWidth, rect.maxY * chars.pageHeight]
            }
            self.onHighlight?(HighlightRequest(
                page: number, text: value.text, sentence: value.sentence, color: color,
                rects: rects, pageWidth: chars.pageWidth, pageHeight: chars.pageHeight))
        }
        overlay.highlightAt = { [weak self] point in
            guard let self, let canonical = overlay.canonicalPoint?(point),
                  let size = self.characterPageSize(number), size.width > 0, size.height > 0 else { return nil }
            let normalized = CGPoint(x: canonical.x / size.width, y: canonical.y / size.height)
            // 后画的在上面 —— 重叠时取最后一条，与网页 z 顺序一致。
            return self.highlights[number]?.last(where: { highlight in
                highlight.rects.contains { $0.insetBy(dx: -0.002, dy: -0.002).contains(normalized) }
            })?.id
        }
        overlay.onEditHighlight = { [weak self] id in
            guard let self, let highlight = self.highlights[number]?.first(where: { $0.id == id }) else { return }
            self.onEditHighlight?(highlight)
        }
        overlay.onRecognize = { [weak self] rect in
            self?.onRecognize?(number, rect)
        }
        overlay.onGrammar = { [weak self] value in
            self?.onGrammar?(number, value.sentence, value.text)
        }
        overlay.onLookup = { [weak self] value, mode in
            self?.onLookup?(number, value.text, value.sentence, mode)
        }
        // PDFKit owns embedded text selection. The overlay supplies native
        // interaction only for scanned pages or a user-selected OCR override.
        overlay.embeddedText = !(page.string?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true)
        textOverlays[number] = overlay
        return overlay
    }

    func pdfView(_ view: PDFView, willEndDisplayingOverlayView overlayView: UIView, for page: PDFPage) {
        guard let document = view.document, page.document === document else { return }
        let number = document.index(for: page) + 1
        textOverlays[number] = nil
    }

    private func loadVisibleCharacterPages() {
        guard let access, let document = view.document else { return }
        let ticket = generation, digest = digest
        let visible = Set(view.visiblePages.map { document.index(for: $0) + 1 })
        // Keep a small local working set. Cache eviction never deletes sidecars.
        if characterPages.count > 12 {
            characterPages = characterPages.filter { visible.contains($0.key) }
            selectionCores = selectionCores.filter { visible.contains($0.key) }
        }
        for number in visible where characterPages[number] == nil && characterReads[number] == nil && !unavailableCharacterPages.contains(number) {
            let readTicket = UUID()
            characterReadTickets[number] = readTicket
            characterReads[number] = Task { @MainActor [weak self] in
                guard let self else { return }
                defer {
                    if generation == ticket && characterReadTickets[number] == readTicket {
                        characterReads[number] = nil; characterReadTickets[number] = nil
                    }
                }
                do {
                    let value = try await NativeBookOCRManager.shared.readerPageCharacters(book: access, expectedContentSHA256: digest, page: number)
                    guard generation == ticket, self.access === access, characterReadTickets[number] == readTicket, !Task.isCancelled else { return }
                    guard let value, value.contentSHA256.lowercased() == digest, value.status == .ready else {
                        unavailableCharacterPages.insert(number); return
                    }
                    let core = try ReaderNativePDFSelection(value)
                    characterPages[number] = value; selectionCores[number] = core
                    textOverlays[number]?.characters = value
                    textOverlays[number]?.selectionCore = core
                    // 字符层到位了，这一页的锁定框才解得出来 —— 立刻补上，
                    // 否则要等下次挂 overlay 才出现。
                    textOverlays[number]?.cardMarkers = cardMarkers(page: number)
                } catch {
                    guard generation == ticket, !Task.isCancelled else { return }
                    unavailableCharacterPages.insert(number)
                    self.error = "本页文字层读取失败：\(error.localizedDescription)"
                }
            }
        }
    }

    private func acceptOCRSelection(_ selected: ReaderNativePDFSelection.Value, page: Int) {
        guard let access, let chars = characterPages[page], !selected.indexes.isEmpty,
              selected.indexes.allSatisfy({ chars.chars.indices.contains($0) }), chars.contentSHA256.lowercased() == digest else { return }
        customSelection = true; selectionTask?.cancel(); view.clearSelection()
        for (number, overlay) in textOverlays where number != page { overlay.clearSelection() }
        onSelection?([CharacterSelection(bookID: access.record.id, contentSHA256: digest, page: page,
            geometryDigest: chars.geometryDigest, indexes: selected.indexes,
            text: selected.text, sentence: selected.sentence, rects: selected.rects)])
    }
}

@MainActor
private final class ReaderNativePDFTextOverlay: UIView, UIEditMenuInteractionDelegate, UIGestureRecognizerDelegate {
    var characters: NativeBookOCRPageCharacters? { didSet { clearSelection() } }
    var selectionCore: ReaderNativePDFSelection? { didSet { clearSelection() } }
    var embeddedText = false
    var canonicalPoint: ((CGPoint) -> CGPoint?)?
    var project: ((CGRect) -> CGRect?)?
    var onSelect: ((ReaderNativePDFSelection.Value) -> Void)?
    var onError: (() -> Void)?
    /// 选区菜单里的「划线」。颜色是四支笔的键名（yellow/green/blue/pink）。
    /// ⚠ 这四个键必须与阅读器色板一致：那是**用户自己的墨水**，存在他的笔记里，
    /// 改名或改值等于改写既有数据。
    var onHighlight: ((ReaderNativePDFSelection.Value, String) -> Void)?
    /// 查词 / 整段翻译：(选中, "dict" | "translate")。取数在阅读器那侧，这里只发起。
    var onLookup: ((ReaderNativePDFSelection.Value, String) -> Void)?
    var onGrammar: ((ReaderNativePDFSelection.Value) -> Void)?
    /// 重新识别这块区域（点坐标的并集矩形）。
    var onRecognize: ((CGRect) -> Void)?
    /// 绑定到正文的卡片「锁定框」（页内归一化坐标）。
    ///
    /// ⚠⚠ 必须画在**这一层**。它是 PDFKit 给每一页的 overlay view，作为页面的
    /// 子视图**跟着页面一起滚**，一帧都不用重算。此前两版分别画在
    /// ReaderNativePageCards（按窗口坐标）和 ReaderNativePDFViewport 的 Canvas
    /// （按 geometryRevision 重画）—— 都是"滚动时不断重新渲染"，于是留残影
    /// （2026-09-22 用户连报三次）。
    var cardMarkers: [ReaderNativePDFDocument.CardMarker] = [] { didSet { setNeedsDisplay() } }
    /// 点锁定框 → 展开那张卡。
    var onOpenCard: ((String) -> Void)?
    /// 页面装饰的绘制（由文档提供，见 ReaderNativePDFDocument.drawDecorations）。
    var decorate: ((CGContext, (CGRect) -> CGRect?) -> Void)?
    /// 行首「译」/ 图徽标。变了就重建子视图，位置在 layoutSubviews 里按当前缩放算。
    var decorationButtons: [ReaderNativePageButton] = [] {
        didSet {
            guard decorationButtons.map(\.signature) != oldValue.map(\.signature) else { return }
            buttonViews.forEach { $0.removeFromSuperview() }
            buttonViews = decorationButtons.map { spec in
                let button = ReaderNativePageButtonView(spec: spec)
                insertSubview(button, belowSubview: leadingHandle)
                return button
            }
            setNeedsLayout()
        }
    }
    private var buttonViews: [ReaderNativePageButtonView] = []
    private var start: Int?
    private var selected: ReaderNativePDFSelection.Value?
    private let leadingHandle = ReaderNativePDFSelectionHandle()
    private let trailingHandle = ReaderNativePDFSelectionHandle()
    private var handleAnchor: Int?
    private lazy var editMenu = UIEditMenuInteraction(delegate: self)

    override init(frame: CGRect) {
        super.init(frame: frame)
        isOpaque = false; backgroundColor = .clear
        // 缩放时 PDFKit 改的是 overlay 的尺寸：必须整页重画，不能拉伸旧位图。
        contentMode = .redraw
        let gesture = UILongPressGestureRecognizer(target: self, action: #selector(selectText(_:)))
        gesture.minimumPressDuration = 0.3
        gesture.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        gesture.delegate = self
        addGestureRecognizer(gesture)
        let tap = UITapGestureRecognizer(target: self, action: #selector(tapText(_:)))
        tap.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        tap.delegate = self; tap.require(toFail: gesture)
        addGestureRecognizer(tap)
        addInteraction(editMenu)
        for (index, handle) in [leadingHandle, trailingHandle].enumerated() {
            handle.tag = index; handle.isHidden = true
            handle.accessibilityLabel = index == 0 ? "选区起点" : "选区终点"
            let pan = UIPanGestureRecognizer(target: self, action: #selector(moveHandle(_:)))
            pan.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
            handle.addGestureRecognizer(pan); addSubview(handle)
        }
    }
    required init?(coder: NSCoder) { return nil }
    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        guard let touched = touch.view else { return true }
        if touched is UIControl { return false }   // 「译」/ 图徽标自己处理点击
        return !touched.isDescendant(of: leadingHandle) && !touched.isDescendant(of: trailingHandle)
    }
    override func didMoveToWindow() {
        super.didMoveToWindow()
        var parent = superview
        while let current = parent {
            if let scroll = current as? UIScrollView {
                for handle in [leadingHandle, trailingHandle] {
                    if let pan = handle.gestureRecognizers?.first { scroll.panGestureRecognizer.require(toFail: pan) }
                }
                break
            }
            parent = current.superview
        }
    }
    override func layoutSubviews() {
        super.layoutSubviews(); updateHandles(); layoutButtons(); setNeedsDisplay()
    }
    private func layoutButtons() {
        guard let page = project?(CGRect(x: 0, y: 0, width: 1, height: 1)) else { return }
        for view in buttonViews {
            guard let anchor = project?(view.spec.anchor) else { view.isHidden = true; continue }
            view.place(anchor: anchor, page: page, badge: view.spec.badge)
        }
    }
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        // 锁定框与页内按钮在**任何**页上都要接得住点击 —— 包括有文字层的 PDF 页。
        // ⚠ 原来这里第一句就是"没有自建选区就放行"，于是文字层 PDF 上的锁定框
        //   一律点不到（点击直接落到 PDFKit）。
        if buttonViews.contains(where: { !$0.isHidden && $0.frame.contains(point) }) { return true }
        if cardMarkerAt(point) != nil { return true }
        guard let characters, !embeddedText || characters.textAuthority == .localOverride else { return false }
        if [leadingHandle, trailingHandle].contains(where: { !$0.isHidden && $0.frame.contains(point) }) { return true }
        return hit(point) != nil
    }
    private func hit(_ point: CGPoint) -> Int? {
        guard let p = canonicalPoint?(point) else { return nil }
        return selectionCore?.hit(p)
    }
    /// 点到已有划线时返回它的 id。接管后 .hl-layer 不存在，原生是唯一能点到划线的地方。
    var highlightAt: ((CGPoint) -> String?)?
    var onEditHighlight: ((String) -> Void)?

    @objc private func tapText(_ gesture: UITapGestureRecognizer) {
        let location = gesture.location(in: self)
        // 点在已有划线上 → 开它的编辑面板（与网页「点划线弹浮层」同一个意思），
        // 而不是把那一个字选起来。⚠ 顺序不能反：先 resolve 再判断的话，菜单已经
        // 弹出来了，编辑面板会叠在它上面。
        // 点在卡片锁定框上 → 展开那张卡。⚠ 排在划线之前：绑卡的那一段往往同时
        //   也划了线，先判划线的话卡永远打不开。
        if let id = cardMarkerAt(location) { onOpenCard?(id); return }
        if let id = highlightAt?(location) { onEditHighlight?(id); return }
        guard let index = hit(location) else { return }
        resolve(index, index); showMenu()
    }
    @objc private func selectText(_ gesture: UILongPressGestureRecognizer) {
        guard let selectionCore else { return }
        switch gesture.state {
        case .began:
            editMenu.dismissMenu()
            start = hit(gesture.location(in: self))
            if let start { resolve(start, start) }
        case .changed, .ended:
            if let start, let point = canonicalPoint?(gesture.location(in: self)),
               let end = selectionCore.hit(point, anchor: start, exactOnly: false) { resolve(start, end) }
            if gesture.state == .ended { self.start = nil; showMenu() }
        case .cancelled, .failed: start = nil
        default: break
        }
    }
    private func resolve(_ start: Int, _ end: Int) {
        do {
            guard let value = try selectionCore?.range(from: start, to: end), value.indexes != selected?.indexes else { return }
            display(value)
        } catch { onError?() }
    }
    private func display(_ value: ReaderNativePDFSelection.Value) {
        selected = value; updateHandles(); setNeedsDisplay(); onSelect?(value)
    }
    private func updateHandles() {
        guard let selected, let chars = characters,
              let first = selected.indexes.first, let last = selected.indexes.last,
              chars.chars.indices.contains(first), chars.chars.indices.contains(last) else {
            leadingHandle.isHidden = true; trailingHandle.isHidden = true; return
        }
        for (index, pair) in [(leadingHandle, chars.chars[first]), (trailingHandle, chars.chars[last])].enumerated() {
            let (handle, char) = pair
            let normalized = CGRect(x: char.x0 / chars.pageWidth, y: char.y0 / chars.pageHeight,
                                    width: (char.x1-char.x0) / chars.pageWidth, height: (char.y1-char.y0) / chars.pageHeight)
            guard let rect = project?(normalized) else { handle.isHidden = true; continue }
            let vertical = char.vertical == true
            let point = vertical ? CGPoint(x: rect.midX, y: index == 0 ? rect.minY : rect.maxY)
                                 : CGPoint(x: index == 0 ? rect.minX : rect.maxX, y: index == 0 ? rect.minY : rect.maxY)
            handle.frame = CGRect(x: point.x - 22, y: point.y - 22, width: 44, height: 44)
            handle.isHidden = false
        }
    }
    @objc private func moveHandle(_ gesture: UIPanGestureRecognizer) {
        guard let selected, let core = selectionCore else { return }
        if gesture.state == .began {
            editMenu.dismissMenu()
            handleAnchor = gesture.view === leadingHandle ? selected.indexes.last : selected.indexes.first
        }
        if gesture.state == .began || gesture.state == .changed || gesture.state == .ended,
           let anchor = handleAnchor, let point = canonicalPoint?(gesture.location(in: self)),
           let end = core.hit(point, anchor: anchor, exactOnly: false) { resolve(anchor, end) }
        if gesture.state == .ended { handleAnchor = nil; showMenu() }
        if gesture.state == .cancelled || gesture.state == .failed { handleAnchor = nil }
    }
    private func showMenu() {
        guard let first = selected?.rects.first, let rect = project?(first) else { return }
        editMenu.presentEditMenu(with: UIEditMenuConfiguration(identifier: nil, sourcePoint: CGPoint(x: rect.midX, y: rect.minY)))
    }
    func editMenuInteraction(_ interaction: UIEditMenuInteraction, menuFor configuration: UIEditMenuConfiguration,
                             suggestedActions: [UIMenuElement]) -> UIMenu? {
        guard let value = selected else { return nil }
        return UIMenu(children: [
            UIAction(title: "复制", image: UIImage(systemName: "doc.on.doc")) { [weak self] _ in
                guard self?.selected?.indexes == value.indexes else { return }
                UIPasteboard.general.string = value.text
            },
            UIAction(title: "选整句", image: UIImage(systemName: "text.quote")) { [weak self] _ in
                guard let self, selected?.indexes == value.indexes else { return }
                do { if let sentence = try selectionCore?.sentence(value.indexes) { display(sentence) } }
                catch { onError?() }
            },
            UIAction(title: "查词", image: UIImage(systemName: "character.book.closed")) { [weak self] _ in
                guard let self, self.selected?.indexes == value.indexes else { return }
                self.onLookup?(value, "dict")
            },
            UIAction(title: "翻译", image: UIImage(systemName: "translate")) { [weak self] _ in
                guard let self, self.selected?.indexes == value.indexes else { return }
                self.onLookup?(value, "translate")
            },
            UIAction(title: "OCR", image: UIImage(systemName: "text.viewfinder")) { [weak self] _ in
                guard let self, self.selected?.indexes == value.indexes else { return }
                // 文字层坏掉（乱码/上标错/缺符号）时对这块重新识别。
                // bbox 用选区各矩形的并集，点坐标 —— 接管后网页那侧算不出它。
                var union = CGRect.null
                for rect in value.rects { union = union.union(rect) }
                guard !union.isNull, union.width >= 0.5, union.height >= 0.5 else { return }
                self.onRecognize?(union)
            },
            UIAction(title: "词组", image: UIImage(systemName: "text.badge.star")) { [weak self] _ in
                guard let self, self.selected?.indexes == value.indexes else { return }
                self.onLookup?(value, "phrase")
            },
            UIAction(title: "解释", image: UIImage(systemName: "lightbulb")) { [weak self] _ in
                guard let self, self.selected?.indexes == value.indexes else { return }
                self.onLookup?(value, "explain")
            },
            UIAction(title: "语法", image: UIImage(systemName: "chart.bar.doc.horizontal")) { [weak self] _ in
                guard let self, self.selected?.indexes == value.indexes else { return }
                // 分析对象是**整句**，焦点是选中的那一段 —— 与网页那侧同一口径
                // （它也是先 _expandSentenceFromRange 取整句再把选中串当 focus）。
                self.onGrammar?(value)
            },
            // 划线走阅读器自己的 __bwReaderHighlightExactText —— 与 AI 划线同一条
            // 路径、同一套存储。不在原生这边另写一套保存逻辑。
            UIMenu(title: "划线", image: UIImage(systemName: "highlighter"), children: [
                highlightAction("黄", key: "yellow", value: value),
                highlightAction("绿", key: "green", value: value),
                highlightAction("蓝", key: "blue", value: value),
                highlightAction("粉", key: "pink", value: value),
            ]),
        ])
    }

    private func highlightAction(
        _ title: String, key: String, value: ReaderNativePDFSelection.Value
    ) -> UIAction {
        UIAction(title: title) { [weak self] _ in
            // 菜单弹出到点下去之间选区可能已经变了；只对当时那一段生效。
            guard let self, self.selected?.indexes == value.indexes else { return }
            self.onHighlight?(value, key)
            self.clearSelection()
        }
    }
    func clearSelection() {
        start = nil; handleAnchor = nil; selected = nil
        editMenu.dismissMenu(); updateHandles(); setNeedsDisplay()
    }
    override func draw(_ rect: CGRect) {
        guard let context = UIGraphicsGetCurrentContext() else { return }
        // 装饰最底下（划线 / 排线 / 下划线 / 振假名 / 译文 / 墨迹），锁定框压在它们上面。
        if let project { decorate?(context, project) }
        // 锁定框先画：选区高亮压在它上面才看得出"这一段既绑着卡、又正被选中"。
        // ⚠ 观感逐项照原版 .pgmark / .pgmark-n（pdf-styles.css + 34-bindcard 的 _bindTone）：
        //   透明底、2pt 分类色描边、框外放 2pt；展开时描边加深 + 2.5pt 外晕；
        //   右上角外侧一枚序号，白色光晕而不是实心底（实心块会盖住相邻字）。
        //   上一版是"淡填充 + 固定青色细线"，用户："太细颜色太浅"。
        let pad = ReaderNativeMarkerStyle.pad
        for marker in cardMarkers {
            let style = ReaderNativeMarkerStyle(tone: marker.tone)
            for normalized in marker.rects {
                guard let box = project?(normalized) else { continue }
                if marker.open {
                    let halo = UIBezierPath(roundedRect: box.insetBy(dx: -(pad + 3.25), dy: -(pad + 3.25)),
                                            cornerRadius: 6.25)
                    context.setStrokeColor(style.halo.cgColor)
                    context.setLineWidth(2.5)
                    context.addPath(halo.cgPath); context.strokePath()
                }
                let path = UIBezierPath(roundedRect: box.insetBy(dx: -(pad + 1), dy: -(pad + 1)), cornerRadius: 4)
                context.setStrokeColor((marker.open ? style.ink : style.border).cgColor)
                context.setLineWidth(2)
                context.addPath(path.cgPath); context.strokePath()
            }
        }
        let digits = UIFont.monospacedDigitSystemFont(ofSize: 9.5, weight: .bold)
        for (marker, number) in numberedMarkers() {
            guard let last = marker.rects.last, let box = project?(last) else { continue }
            let style = ReaderNativeMarkerStyle(tone: marker.tone)
            let label = String(number) as NSString
            let origin = CGPoint(x: box.maxX + pad + 1, y: box.minY - pad - 4)
            // 白色光晕：先描一圈粗白边再填字，等价于原版那串 text-shadow。
            label.draw(at: origin, withAttributes: [.font: digits, .strokeColor: UIColor.white, .strokeWidth: 7])
            label.draw(at: origin, withAttributes: [.font: digits, .foregroundColor: style.ink])
        }
        guard let selected else { return }
        context.setFillColor(UIColor.systemTeal.withAlphaComponent(0.22).cgColor)
        for normalized in selected.rects {
            if let box = project?(normalized) { context.fill(box) }
        }
    }

    /// 点中了哪个锁定框。⚠ 命中范围放宽 6pt：一行字的框只有十几点高，
    /// 按原尺寸判定基本点不中。
    /// 本页序号：按被锚词的**顶边**先行后列排 —— 照原版 `_renumberMarks`。
    /// 同行判据取行高一半（至少 6pt），固定阈值会把同一行判成上下关系。
    private func numberedMarkers() -> [(ReaderNativePDFDocument.CardMarker, Int)] {
        let placed: [(ReaderNativePDFDocument.CardMarker, CGRect)] = cardMarkers.compactMap { marker in
            guard let last = marker.rects.last, let box = project?(last) else { return nil }
            return (marker, box)
        }
        let tolerance = max(6, (placed.map { $0.1.height }.max() ?? 0) * 0.5)
        let sorted = placed.sorted { a, b in
            abs(a.1.minY - b.1.minY) > tolerance ? a.1.minY < b.1.minY : a.1.minX < b.1.minX
        }
        return sorted.enumerated().map { ($0.element.0, $0.offset + 1) }
    }

    private func cardMarkerAt(_ point: CGPoint) -> String? {
        for marker in cardMarkers {
            for normalized in marker.rects {
                guard let box = project?(normalized) else { continue }
                if box.insetBy(dx: -6, dy: -6).contains(point) { return marker.id }
            }
        }
        return nil
    }
}

@MainActor
private final class ReaderNativePDFSelectionHandle: UIView {
    override init(frame: CGRect) {
        super.init(frame: frame); backgroundColor = .clear; isOpaque = false
    }
    required init?(coder: NSCoder) { return nil }
    override func draw(_ rect: CGRect) {
        UIColor.systemTeal.setFill()
        UIBezierPath(ovalIn: CGRect(x: bounds.midX - 5, y: bounds.midY - 5, width: 10, height: 10)).fill()
        UIBezierPath(rect: CGRect(x: bounds.midX - 1, y: bounds.midY - 11, width: 2, height: 22)).fill()
    }
}

@MainActor
final class ReaderNativePDFView: PDFView {
    var onLayout: (() -> Void)?
    override func layoutSubviews() { super.layoutSubviews(); onLayout?() }
}

private struct ReaderNativePDFSurface: UIViewRepresentable {
    @ObservedObject var document: ReaderNativePDFDocument
    func makeUIView(context: Context) -> ReaderNativePDFView { document.view }
    func updateUIView(_ uiView: ReaderNativePDFView, context: Context) { }
}

struct ReaderNativePDFViewport: View {
    @ObservedObject var document: ReaderNativePDFDocument
    /// 点行首的「译」：把整句交给原生翻译面板。
    var onTranslateSentence: ((ReaderNativePDFDocument.VocabSentence) -> Void)?
    /// 点图徽标 → 打开原生描述面板（描述文本是服务端早就生成好的，不在这里烧额度）。
    var onOpenFigure: ((ReaderNativePDFDocument.Figure) -> Void)?
    var body: some View {
        // ⚠ 这一层**不再画任何东西**。划线、排线、下划线、振假名、译文、墨迹、锁定框、
        //   行首「译」与图徽标，全都在每一页自己的 overlay 里（ReaderNativePDFTextOverlay）。
        //   原来它们是 PDFView 之上的一张 SwiftUI Canvas + 两层按钮，按 geometryRevision
        //   在滚动后重画/重排 —— 慢一帧，就是残影（2026-09-22、09-23 用户连报）。
        ReaderNativePDFSurface(document: document)
            .onAppear {
                document.onTranslateSentence = onTranslateSentence
                document.onOpenFigure = onOpenFigure
            }
            .clipped()
    }
}

private extension CGRect {
    var isFiniteRect: Bool { [minX, minY, width, height].allSatisfy(\.isFinite) && !isNull && !isInfinite }
}

/// 生词下划线的四档颜色与粗细。
///
/// ⚠ 取值与 `pdf-styles.css` 的 `.vocab-underline.m-*` 一一对应 —— 那是唯一来源。
/// 掌握档在网页那侧是 `display:none`，这里对应不画（高度 0）。
enum ReaderNativeVocabPalette {
    static func color(_ slug: String) -> Color {
        switch slug {
        case "new": return Color(red: 0.96, green: 0.62, blue: 0.04)          // #f59e0b
        case "learning": return Color(red: 0.98, green: 0.57, blue: 0.24).opacity(0.92)  // #fb923c
        case "seen": return Color(red: 0.98, green: 0.80, blue: 0.08).opacity(0.85)      // #facc15
        case "known": return Color(red: 0.64, green: 0.90, blue: 0.21).opacity(0.65)     // #a3e635
        default: return .clear                                                 // 掌握/未知：不画
        }
    }

    static func thickness(_ slug: String) -> CGFloat {
        switch slug {
        case "new": return 2.5
        case "learning": return 2.3
        case "seen": return 2
        case "known": return 1.5
        default: return 0
        }
    }
}


/// 拖卡落点预览的几何。坐标系由产出方说明（文档内是 `view`，模型层转成窗口坐标）。
struct ReaderNativeDropPreview: Equatable {
    var rects: [CGRect] = []
    var line: CGRect?
}

/// 只为落点预览存在的小模型。
///
/// ⚠ 它单独存在的理由就一条：预览在拖动期间每秒要发十来次，而阅读器主模型
/// 被工作区、视口、页卡层一起观察 —— 挂在那上面等于每秒把**每一张卡**重算十来遍。
@MainActor
final class ReaderNativeDropPreviewModel: ObservableObject {
    @Published var preview: ReaderNativeDropPreview?
}

/// 卡片锁定框的观感 —— 照原版 `_bindTone`（34-bindcard.js）的配方：
///   --pm-b = 色调 60% 混 #2a2440（平时描边）
///   --pm-i = 色调 22% 混 #14101f（展开描边 / 序号字色）
///   --pm-h = 色调 30% 透明（展开外晕）
/// ⚠ 不直接用色调原色：在纸上只有 1.6~2.5:1，够不到图形元素的 3:1。
struct ReaderNativeMarkerStyle {
    static let pad: CGFloat = 2
    /// 原版 WORD_CARD_TONES。
    static let tones: [String: UIColor] = [
        "text": hex(0xbf5af2), "qa": hex(0x7dd3fc), "image": hex(0x34d399), "number": hex(0xff9f0a),
    ]

    let border: UIColor
    let ink: UIColor
    let halo: UIColor

    init(tone: UIColor) {
        border = Self.mix(tone, 0.60, Self.hex(0x2a2440))
        ink = Self.mix(tone, 0.22, Self.hex(0x14101f))
        halo = tone.withAlphaComponent(0.30)
    }

    /// 分类 —— 逐条照原版 `wordCardPresentation` + `wordCardCategory`：
    /// 学习卡（card 槽）结构本身就是问答，默认 qa；通用 HTML 卡默认 text。
    static func tone(_ payload: [String: Any], slot: String) -> UIColor {
        let raw = ((payload["category"] as? String) ?? (payload["kind"] as? String) ?? "").lowercased()
        let label = slot == "card" ? "🎴 卡片" : ((payload["label"] as? String) ?? "卡片")
        let text = raw + " " + label
        func has(_ pattern: String) -> Bool { text.range(of: pattern, options: .regularExpression) != nil }
        let category: String
        if has("image|images|video|配图|图片|图像|视频") { category = "image" }
        else if has("number|numeric|metric|weather|数值|数字|数据|统计|温度|价格") { category = "number" }
        else if has("qa|question|anki|quiz|问答|考点|出题|题目|学习卡") { category = "qa" }
        else if has("text|文字|背景|辨析|摘要|翻译|解释|新闻") { category = "text" }
        else {
            switch ((payload["type"] as? String) ?? "").lowercased() {
            case "#c77dff", "#34d399", "#ff7a59": category = "image"
            case "#39d98a", "#7dd3fc": category = "qa"
            case "#2dd4bf", "#ff9f0a": category = "number"
            default: category = slot == "card" ? "qa" : "text"
            }
        }
        return tones[category] ?? hex(0xbf5af2)
    }

    private static func hex(_ value: UInt32) -> UIColor {
        UIColor(red: CGFloat((value >> 16) & 255) / 255, green: CGFloat((value >> 8) & 255) / 255,
                blue: CGFloat(value & 255) / 255, alpha: 1)
    }

    /// CSS `color-mix(in srgb, a p%, b)`。
    private static func mix(_ a: UIColor, _ p: CGFloat, _ b: UIColor) -> UIColor {
        var ar: CGFloat = 0, ag: CGFloat = 0, ab: CGFloat = 0, aa: CGFloat = 0
        var br: CGFloat = 0, bg: CGFloat = 0, bb: CGFloat = 0, ba: CGFloat = 0
        a.getRed(&ar, green: &ag, blue: &ab, alpha: &aa)
        b.getRed(&br, green: &bg, blue: &bb, alpha: &ba)
        return UIColor(red: ar * p + br * (1 - p), green: ag * p + bg * (1 - p),
                       blue: ab * p + bb * (1 - p), alpha: 1)
    }
}

/// 页内按钮的规格（行首「译」/ 图徽标）。位置用归一化锚点，由 overlay 按当前缩放摆。
struct ReaderNativePageButton {
    enum Kind { case translate, figure }
    let id: String
    let kind: Kind
    let anchor: CGRect          // 归一化：译 = 句子首行框；图 = 图框
    let badge: CGPoint?         // 归一化：服务端预算好的徽标中心（图）
    let tint: UIColor
    let label: String
    let action: () -> Void
    var signature: String { id + "|" + label + "|" + tint.description }
}

private final class ReaderNativePageButtonView: UIButton {
    let spec: ReaderNativePageButton

    init(spec: ReaderNativePageButton) {
        self.spec = spec
        super.init(frame: .zero)
        accessibilityLabel = spec.label
        switch spec.kind {
        case .translate:
            setTitle("译", for: .normal)
            setTitleColor(spec.tint, for: .normal)
            backgroundColor = UIColor.systemBackground.withAlphaComponent(0.72)
            layer.cornerRadius = 4
        case .figure:
            setImage(UIImage(systemName: "photo",
                             withConfiguration: UIImage.SymbolConfiguration(pointSize: 13, weight: .semibold)),
                     for: .normal)
            tintColor = .white
            backgroundColor = spec.tint
        }
        addAction(UIAction { [weak self] _ in self?.spec.action() }, for: .touchUpInside)
    }
    required init?(coder: NSCoder) { return nil }

    /// 译：贴在句子首行左侧外沿，边长随行高（14~26）；图：服务端锚点优先，缺了退图框右上角内缩，夹进页面内。
    func place(anchor: CGRect, page: CGRect, badge: CGPoint?) {
        switch spec.kind {
        case .translate:
            guard anchor.height > 8 else { isHidden = true; return }
            let side = min(26, max(14, anchor.height))
            titleLabel?.font = .systemFont(ofSize: side * 0.6, weight: .semibold)
            frame = CGRect(x: anchor.minX - side * 1.1, y: anchor.midY - side / 2, width: side, height: side)
        case .figure:
            let side: CGFloat = 26, half = side / 2
            let center = badge.map { CGPoint(x: page.minX + $0.x * page.width, y: page.minY + $0.y * page.height) }
                ?? CGPoint(x: anchor.maxX - side * 0.7, y: anchor.minY + side * 0.7)
            let x = min(max(page.minX + half, center.x), page.maxX - half)
            let y = min(max(page.minY + half, center.y), page.maxY - half)
            frame = CGRect(x: x - half, y: y - half, width: side, height: side)
            layer.cornerRadius = half
        }
        isHidden = false
    }
}

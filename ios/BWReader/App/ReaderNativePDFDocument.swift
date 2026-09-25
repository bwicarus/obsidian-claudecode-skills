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
        var number = 0
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
    /// ⚠ 不 @Published：它随滚动逐帧变（页内比例）。发布出去的话，所有观察这份文档的
    ///   SwiftUI 视图每一帧都要重算 —— 2026-09-23 用户报"卡顿"，病根之一。
    ///   要知道位置变了的（导航桥）走 onPosition。
    private(set) var position = Position(page: 1, scale: 1, visiblePages: [], fraction: 0, mode: "continuous", spreadOffset: 0, crop: nil)
    /// 最近一次手指/笔触到阅读区的时间（停留统计的挂机判据）。
    fileprivate(set) var lastInteractionAt = Date()
    @Published private(set) var error: String?
    @Published private(set) var ready = false
    private(set) var notesContentRevision: UInt64 = 0
    /// 每一次布局/滚动都 +1。⚠ 不 @Published，理由同 position。
    private(set) var geometryRevision = 0
    /// 只在**缩放或重排**时变（纯滚动不变）：文档层卡片按它重算 —— 它们按文档坐标摆，
    /// 滚动时由宿主同帧平移，SwiftUI 什么都不用算。
    @Published private(set) var layoutRevision = 0
    /// 滚动停下来 0.25 秒后变一次：按窗口坐标登记的东西（卡片的笔迹面）按它重登。
    @Published private(set) var settledRevision = 0
    private var lastLayoutKey: [CGFloat] = []
    private var settleTask: Task<Void, Never>?
    @Published private(set) var ink: [Int: [ReaderNativeCardStroke]] = [:] { didSet { refreshDecorations() } }
    @Published private(set) var highlights: [Int: [Highlight]] = [:] { didSet { refreshDecorations() } }
    /// 生词下划线由原生词汇投影计算；文档只画归一化矩形。
    @Published private(set) var vocabMarks: [Int: [VocabMark]] = [:] { didSet { refreshDecorations() } }

    struct VocabMark: Equatable {
        let slug: String
        let rects: [CGRect]        // 归一化，便于 viewRect 直接换算
    }

    /// 由壳按可见页填。传 nil 表示这一页还没取到，保留旧的别闪。
    func setVocabMarks(_ marks: [VocabMark]?, page: Int) {
        guard let marks, vocabMarks[page] != marks else { return }
        vocabMarks[page] = marks
    }

    func clearVocabMarks() { vocabMarks = [:] }

    /// 振假名：已掌握的词不注音（与网页那侧 `__masteredFuri` 同一份数据）。
    /// `enabled == false` 表示振假名整体关着 —— 那时一个都不画，跟"这一页没有
    /// 已掌握的词"不是一回事。
    @Published private(set) var furiganaEnabled: [Int: Bool] = [:] { didSet { refreshDecorations() } }
    @Published private(set) var furiganaMastered: [Int: Set<String>] = [:] { didSet { refreshDecorations() } }

    func setFuriganaMastered(_ words: [String]?, enabled: Bool, page: Int) {
        if furiganaEnabled[page] != enabled { furiganaEnabled[page] = enabled }
        let mastered = Set(words ?? [])
        if furiganaMastered[page] != mastered { furiganaMastered[page] = mastered }
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
    struct VocabSentence: Identifiable, Equatable {
        let id: String
        let index: Int
        let page: Int
        let text: String
        let rects: [CGRect]
    }
    @Published private(set) var vocabSentences: [Int: [VocabSentence]] = [:] { didSet { refreshDecorations() } }

    /// 整页正文（按阅读顺序）。
    ///
    /// 使用原生 ReaderNativePDFTextGeometry 的 range(0, n-1)，通过跨端对照
    /// 保持网页 `_charsRangeToText` 的分块/阅读顺序规则 ——
    /// 自己按字符数组拼字符串会在表格/多栏页上给出另一种顺序。
    /// 结果缓存：一页的正文不会变，而滚动时这条路每帧都可能被问到。
    private var pageTexts: [Int: String] = [:]

    /// Tokenizer changes invalidate derived character/word data, never PDF
    /// pixels, annotations or their source character indices.
    func invalidateTokenization() {
        pageTexts = [:]; characterPages = [:]; unavailableCharacterPages = []
        selectionCores = [:]
        characterReads.values.forEach { $0.cancel() }; characterReads = [:]
        characterReadTickets = [:]
        textOverlays.values.forEach { $0.selectionCore = nil; $0.characters = nil }
        loadVisibleCharacterPages()
    }

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
        guard vocabSentences[page] != sentences else { return }
        vocabSentences[page] = sentences
    }

    /// 整页翻译（译页）的一个译文片段：一行译文落在原文那一行的**字框顶部留白**里
    /// ——「行间对照」而不是遮住原文。ReaderNativePageTranslation 计算
    /// 切分、分配和字号，这里只按页面缩放画。
    ///
    /// ⚠ `fontScale` 是**按页高归一化**的字号，不是 pt：画的时候乘回该页在屏幕上的
    /// 高度，缩放才跟着页面走。存 pt 的话放大页面译文就还是小的。
    struct TranslationSlice: Equatable {
        let origin: CGPoint        // 归一化，左上
        let width: Double          // 归一化
        let fontScale: Double      // 归一化字号（× 页面屏幕高度 = 实际字号）
        let text: String
    }
    @Published private(set) var translationSlices: [Int: [TranslationSlice]] = [:] { didSet { refreshDecorations() } }

    func setTranslationSlices(_ slices: [TranslationSlice], page: Int) {
        guard translationSlices[page] != slices else { return }
        translationSlices[page] = slices
    }

    /// 一张插图：徽标锚点 + 图框（都归一化）+ 已经生成好的描述。
    /// ⚠ `badge` 可能为空 —— 服务端还没算好锚点。DOM 那侧此时会试四个角并避开正文，
    /// 那要文字层；接管后退成图框右上角内缩，位置与网页不保证一致（记在这里，
    /// 不要以为是 bug）。
    struct Figure: Identifiable, Equatable {
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
        guard figures[page] != items else { return }
        figures[page] = items
    }

    func setFigureAttached(_ attached: Bool, id: String, page: Int) {
        guard var items = figures[page], let index = items.firstIndex(where: { $0.id == id }) else { return }
        guard items[index].attached != attached else { return }
        items[index].attached = attached
        figures[page] = items
    }

    /// Only derived decoration caches are evicted; canonical notes, strokes,
    /// selections and explicit figure attachments keep their own lifetimes.
    func retainPageDecorations(_ pages: Set<Int>) {
        if vocabMarks.keys.contains(where: { !pages.contains($0) }) { vocabMarks = vocabMarks.filter { pages.contains($0.key) } }
        if vocabSentences.keys.contains(where: { !pages.contains($0) }) { vocabSentences = vocabSentences.filter { pages.contains($0.key) } }
        if translationSlices.keys.contains(where: { !pages.contains($0) }) { translationSlices = translationSlices.filter { pages.contains($0.key) } }
        if furiganaEnabled.keys.contains(where: { !pages.contains($0) }) { furiganaEnabled = furiganaEnabled.filter { pages.contains($0.key) } }
        if furiganaMastered.keys.contains(where: { !pages.contains($0) }) { furiganaMastered = furiganaMastered.filter { pages.contains($0.key) } }
        if figures.keys.contains(where: { !pages.contains($0) }) { figures = figures.filter { pages.contains($0.key) } }
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
    /// 选区窗口（原版 `#sel-toolbar`）此刻显示的选区。nil = 不显示。
    struct SelectionPanel: Equatable {
        let id: UUID
        let page: Int
        let text: String
        let sentence: String
    }
    @Published private(set) var selectionPanel: SelectionPanel?
    /// 窗口该贴的选区框（窗口坐标）。滚动途中为 nil —— 先藏起来，停下后再按新位置摆。
    @Published private(set) var selectionPanelAnchor: CGRect?
    private weak var selectionPanelOverlay: ReaderNativePDFTextOverlay?
    /// 「搜索」「对话」：要出阅读区的两件事交给阅读器。
    var onSelectionSearch: ((String) -> Void)?
    var onSelectionChat: ((Int, String, String) -> Void)?

    fileprivate func selectionPanelChanged(overlay: ReaderNativePDFTextOverlay?, page: Int,
                                           value: ReaderNativePDFSelection.Value?) {
        guard let overlay, let value else {
            // 别的页清自己的选区时也会报 nil —— 只有当前窗口所属那一页清了才收起。
            if overlay == nil || selectionPanelOverlay === overlay {
                selectionPanel = nil; selectionPanelAnchor = nil; selectionPanelOverlay = nil
            }
            return
        }
        selectionPanelOverlay = overlay
        selectionPanel = SelectionPanel(id: UUID(), page: page, text: value.text, sentence: value.sentence)
        selectionPanelAnchor = overlay.selectionWindowRect()
    }

    /// 最近一次查词时那个词在窗口里的框（贴词小框按它摆）。
    private(set) var lastLookupAnchor: CGRect?
    /// 最近一次查词的那个词：页码 + 页内归一化矩形（慢词的等待高亮画在这里）。
    private(set) var lastLookupPage = 0
    private(set) var lastLookupRects: [CGRect] = []

    /// 查词等待高亮（原版 15-phrase-wordpop「单击查词的等待表现」）：
    /// 慢词不弹挡视线的"查词中"框，而是让那个词**呼吸**；结果到了转常亮，点它才出小框。
    /// 多个可并存，各查各的。
    struct PendingLookup: Equatable { let id: UUID; let page: Int; let rects: [CGRect]; var ready: Bool }
    private(set) var pendingLookups: [PendingLookup] = [] {
        didSet { refreshDecorations(); updateLookupPulse() }
    }
    var onOpenPendingLookup: ((UUID) -> Void)?
    private var lookupPulse: Timer?

    func addPendingLookup(id: UUID, page: Int, rects: [CGRect]) {
        guard !rects.isEmpty else { return }
        pendingLookups.append(PendingLookup(id: id, page: page, rects: rects, ready: false))
    }
    func markPendingLookupReady(_ id: UUID) {
        guard let index = pendingLookups.firstIndex(where: { $0.id == id }) else { return }
        pendingLookups[index].ready = true
    }
    func removePendingLookup(_ id: UUID) { pendingLookups.removeAll { $0.id == id } }

    /// 这条等待高亮此刻在窗口里的框（点开时小框贴着它摆 —— 可能已经滚过）。
    func pendingLookupWindowRect(_ id: UUID) -> CGRect? {
        guard let item = pendingLookups.first(where: { $0.id == id }) else { return nil }
        var union = CGRect.null
        for rect in item.rects {
            if let box = viewRect(normalized: rect, page: item.page) { union = union.union(box) }
        }
        return union.isNull ? nil : view.convert(union, to: nil)
    }

    /// 还在查的那几个词要一直呼吸：只重画它们所在的页，约 12 帧/秒。
    private func updateLookupPulse() {
        let breathing = Set(pendingLookups.filter { !$0.ready }.map(\.page))
        if breathing.isEmpty { lookupPulse?.invalidate(); lookupPulse = nil; return }
        guard lookupPulse == nil else { return }
        lookupPulse = Timer.scheduledTimer(withTimeInterval: 1.0 / 12, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                for page in Set(self.pendingLookups.filter { !$0.ready }.map(\.page)) {
                    self.textOverlays[page]?.setNeedsDisplay()
                }
            }
        }
    }
    /// 贴在正文上的临时东西（查词小框）该收起了：开始滚动 / 点了空白处。
    var onDismissTransient: (() -> Void)?

    /// 只收起窗口，选区留着（查词/翻译等打开面板后）。
    func dismissSelectionPanel() {
        selectionPanel = nil; selectionPanelAnchor = nil
    }

    /// 选区窗口上的按钮。动作实现全在文字层里（与原来菜单同一套），这里只转交。
    func performSelectionAction(_ key: String) {
        selectionPanelOverlay?.perform(key)
    }

    /// 诊断出口：写进回传服务器的客户端日志（由阅读器接上）。
    /// ⚠ 这台 iPad 摸不到，"选中弹的是系统菜单""点卡没反应"这类问题只能靠现场自己说出来。
    var onDiagnostic: ((String) -> Void)?
    private var diagnosticAt: [String: Date] = [:]
    /// 同一类诊断 2 秒内只记一条，别让拖选区刷屏。
    private func diagnose(_ key: String, _ line: @autoclosure () -> String) {
        let now = Date()
        if let last = diagnosticAt[key], now.timeIntervalSince(last) < 2 { return }
        diagnosticAt[key] = now
        onDiagnostic?(line())
    }
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

    /// Text/source requests do not need a visible PDFPage, a raster image or a
    /// web text layer. Load the same authoritative sidecar used by selection.
    func sourceCharacters(page: Int) async throws -> [String: Any] {
        guard let access, page > 0, page <= (view.document?.pageCount ?? 0) else {
            throw NativeBookOCRError.pageUnavailable
        }
        let ticket = generation, expectedDigest = digest
        let value = try await NativeBookOCRManager.shared.readerPageCharacters(
            book: access, expectedContentSHA256: expectedDigest, page: page)
        guard generation == ticket, self.access === access, !Task.isCancelled else {
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        guard let value, value.contentSHA256.lowercased() == expectedDigest,
              value.status == .ready || value.status == .readyEmpty else {
            throw NativeBookOCRError.pageUnavailable
        }
        let raw = try JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as? [String: Any] ?? [:]
        return ["ok": true, "page": page, "chars": raw["chars"] ?? [],
                "pageWidth": value.pageWidth, "pageHeight": value.pageHeight,
                "revision": NativeBookOCRBridge.pageRevision(value),
                "source": value.source?.rawValue ?? "embedded",
                "engine_revision": value.engineRevision,
                "characterGeometry": value.characterGeometry.rawValue,
                "layout": raw["layout"] ?? NSNull()]
    }

    func prepareBinding(page: Int, text: String) async throws -> [String: Any]? {
        guard let access, page > 0, page <= (view.document?.pageCount ?? 0) else {
            throw NativeBookOCRError.pageUnavailable
        }
        let ticket = generation, expectedDigest = digest
        let value = try await NativeBookOCRManager.shared.readerPageCharacters(
            book: access, expectedContentSHA256: expectedDigest, page: page)
        guard generation == ticket, self.access === access, !Task.isCancelled else {
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        guard let value, value.contentSHA256.lowercased() == expectedDigest, value.status == .ready else {
            throw NativeBookOCRError.pageUnavailable
        }
        if characterPages[page]?.geometryDigest != value.geometryDigest
            || characterPages[page]?.engineRevision != value.engineRevision || selectionCores[page] == nil {
            characterPages[page] = value
            selectionCores[page] = try ReaderNativePDFSelection(value)
        }
        trimCharacterCache(keeping: page)
        return resolveBinding(page: page, text: text)
    }

    private var characterPages: [Int: NativeBookOCRPageCharacters] = [:]
    private var selectionCores: [Int: ReaderNativePDFSelection] = [:]
    private var characterReads: [Int: Task<Void, Never>] = [:]
    private var characterReadTickets: [Int: UUID] = [:]

    private func trimCharacterCache(keeping page: Int? = nil) {
        guard characterPages.count > 12, let document = view.document else { return }
        var keep = Set(view.visiblePages.map { document.index(for: $0) + 1 })
        if let page { keep.insert(page) }
        characterPages = characterPages.filter { keep.contains($0.key) }
        selectionCores = selectionCores.filter { keep.contains($0.key) }
    }
    private var unavailableCharacterPages = Set<Int>()
    /// 这一页暂时没读到字符（空 / 还在处理 / 读失败）时的退避重试。
    /// ⚠ 以前第一次没读到就永久记进 unavailableCharacterPages：原生正文比网页挂得早，
    ///   刚打开那几秒 OCR 管理器还没激活这本书的摘要，问什么都是空 —— 于是一开始看见的
    ///   那几页整个会话都没有原生文字层，选中只能落到 PDFKit 的系统菜单上。
    ///   网页那侧同一条数据对 idle/pending 也是不缓存、下次再问（native-local-runtime
    ///   nativePageForPage 那段注释）。
    private var characterRetry: [Int: (attempts: Int, after: Date)] = [:]
    private var characterRetryScheduled = false
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
        // 停留统计的「有人在操作」判据：手指或笔一碰就记时间，然后立刻 fail 放行，
        // 不参与任何手势竞争（ReaderNativeDwellTracker 60s 无操作即停表）。
        view.addGestureRecognizer(ReaderInteractionRecorder { [weak self] in self?.lastInteractionAt = Date() })
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
        lookupPulse?.invalidate()
        settleTask?.cancel()
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
        lookupPulse?.invalidate(); lookupPulse = nil
        pendingLookups = []
        settleTask?.cancel(); settleTask = nil
        lastLookupAnchor = nil; lastLookupPage = 0; lastLookupRects = []
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
        characterRetry = [:]
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
        var changed = false
        for domain in selected {
            _ = try ReaderBookUserStatePackageCodec.validateDomainPayload(domain, localExport: true)
            if let previous = domainHeaders[domain.name] {
                guard domain.revision >= previous.revision,
                      domain.revision != previous.revision || domain.digest == previous.digest else {
                    throw ReaderBookUserStateWebAdapterError.contextChanged
                }
            }
            if domainHeaders[domain.name]?.digest != domain.digest { changed = true }
        }
        // A position/selection event may arrive with the same data. Validate
        // the envelope, then avoid reparsing and invalidating every PDF overlay.
        if !changed {
            for domain in selected { domainHeaders[domain.name] = (domain.revision, domain.digest) }
            return
        }
        var nextInk: [Int: [ReaderNativeCardStroke]] = [:]
        var nextHighlights: [Int: [Highlight]] = [:]
        for domain in selected {
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
        _ = try ReaderBookUserStatePackageCodec.validateDomainPayload(domain, localExport: true)
        if let previous = domainHeaders[.notes] {
            guard domain.revision >= previous.revision,
                  domain.revision != previous.revision || domain.digest == previous.digest else {
                throw ReaderBookUserStateWebAdapterError.contextChanged
            }
            if domain.digest == previous.digest {
                domainHeaders[.notes] = (domain.revision, domain.digest)
                return
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
        notesContentRevision &+= 1
        domainHeaders[.notes] = (domain.revision, domain.digest)
        refreshCardMarkers()
    }

    /// Resolve saved anchors through PDFKit and the original character-binding
    /// rules. No DOM frame, CSS zoom or newly assigned card identity is involved.
    /// `expanded`：按完全展开的尺寸算（点锁定框打开的词锚卡总是完全展开，见原版 forceOpenCardFull）。
    func noteGeometry(_ note: [String: Any], presentationSize: CGSize? = nil, in target: UIView? = nil,
                      expanded: Bool = false) -> NoteGeometry? {
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
        let collapsed = !expanded && (note["collapsed"] as? Bool == true || ["dot", "min"].contains(payload["form"] as? String ?? ""))
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
        guard let characters = characterPages[page] else { return [] }
        let markers = notes.compactMap { note -> CardMarker? in
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
        let boxes = markers.map { marker in marker.rects.map { rect in
            CGRect(x:rect.minX * characters.pageWidth,y:rect.minY * characters.pageHeight,
                   width:rect.width * characters.pageWidth,height:rect.height * characters.pageHeight)
            }
        }
        return ReaderNativePDFContext.ordered(boxes).enumerated().map { number,index in
            var marker = markers[index]; marker.number = number + 1; return marker
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
        // 查词等待高亮：还在查 = 呼吸（淡入淡出 1.2s 一周），查好了 = 常亮等人点。
        let pulse = 0.5 + 0.5 * sin(Date().timeIntervalSinceReferenceDate * 2 * .pi / 1.2)
        for item in pendingLookups where item.page == number {
            let alpha = item.ready ? 0.38 : 0.12 + 0.28 * pulse
            context.setFillColor(UIColor(red: 0.04, green: 0.52, blue: 1, alpha: alpha).cgColor)
            for normalized in item.rects {
                if let rect = project(normalized) {
                    context.addPath(UIBezierPath(roundedRect: rect.insetBy(dx: -1.5, dy: -1), cornerRadius: 3).cgPath)
                    context.fillPath()
                }
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
    ///   松手却钉别处"。这里跟 `nativeDropTarget` 一样先 `canonicalPoint` 定页，
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

    private func layoutChanged() {
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
        let content = view.documentView?.bounds.size ?? .zero
        let layoutKey: [CGFloat] = [view.scaleFactor, content.width, content.height, view.bounds.width, view.bounds.height]
        if layoutKey != lastLayoutKey { lastLayoutKey = layoutKey; layoutRevision &+= 1 }
        if selectionPanelAnchor != nil { selectionPanelAnchor = nil }
        if settleTask == nil { onDismissTransient?() }   // 一次滚动只报一次（开始时）
        settleTask?.cancel()
        settleTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 250_000_000)
            guard let self, !Task.isCancelled else { return }
            self.settleTask = nil
            self.settledRevision &+= 1
            if self.selectionPanel != nil {
                self.selectionPanelAnchor = self.selectionPanelOverlay?.selectionWindowRect()
            }
        }
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
        diagnose("sel-pdfkit", "[native-sel] pdfkit pages=" + pages.map { String($0.0) }.joined(separator: ",")
                 + " chars=" + pages.map { characterPages[$0.0] != nil ? "y" : "n" }.joined(separator: ","))
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
        onDismissTransient?()
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
        overlay.onPanel = { [weak self, weak overlay] value in
            self?.selectionPanelChanged(overlay: overlay, page: number, value: value)
        }
        overlay.onSearch = { [weak self] value in self?.onSelectionSearch?(value.text) }
        overlay.onChat = { [weak self] value in self?.onSelectionChat?(number, value.text, value.sentence) }
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
        overlay.pendingLookupAt = { [weak self] point in
            guard let self, let canonical = overlay.canonicalPoint?(point),
                  let size = self.characterPageSize(number), size.width > 0, size.height > 0 else { return nil }
            let normalized = CGPoint(x: canonical.x / size.width, y: canonical.y / size.height)
            return self.pendingLookups.last(where: { item in
                item.page == number && item.rects.contains { $0.insetBy(dx: -0.004, dy: -0.004).contains(normalized) }
            })?.id
        }
        overlay.onOpenPendingLookup = { [weak self] id in self?.onOpenPendingLookup?(id) }
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
        overlay.onLookup = { [weak self, weak overlay] value, mode in
            // 记下这个词在屏幕上的位置：查词结果按原版那样贴着词弹小框。
            self?.lastLookupAnchor = overlay?.selectionWindowRect()
            self?.lastLookupPage = number
            self?.lastLookupRects = value.rects
            // Freeze the query and anchor before consuming the selection. Calling the
            // document-wide clearSelection here would cancel the new lookup itself.
            overlay?.clearSelection()
            self?.onLookup?(number, value.text, value.sentence, mode)
        }
        // 有字符数据的页一律由原生文字层接选区（我们自己的选区菜单）；PDFKit 自带的选择
        // 只在这一页拿不到字符数据时兜底。
        // ⚠ 以前是"有嵌入文字层就交给 PDFKit"：OCR 结果嵌成隐形文字层的书每一页都算
        //   "有嵌入文字"，于是整本书弹的都是系统菜单（Copy / Look Up / Translate），
        //   我们的查词、翻译、划线、解释一个都没有（2026-09-23 用户截图）。
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
        trimCharacterCache()
        let now = Date()
        for number in visible where characterPages[number] == nil && characterReads[number] == nil
            && !unavailableCharacterPages.contains(number) && (characterRetry[number]?.after ?? .distantPast) <= now {
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
                        let why = value.map { "status=\($0.status)" } ?? "nil"
                        if characterReadFailed(number) { onDiagnostic?("[native-chars] p=\(number) gave up: " + why) }
                        else { diagnose("chars-miss-\(number)", "[native-chars] p=\(number) miss: " + why) }
                        return
                    }
                    let core = try ReaderNativePDFSelection(value)
                    if characterRetry[number] != nil {
                        onDiagnostic?("[native-chars] p=\(number) ok after retry n=\(value.chars.count)")
                    }
                    characterRetry[number] = nil
                    characterPages[number] = value; selectionCores[number] = core
                    textOverlays[number]?.characters = value
                    textOverlays[number]?.selectionCore = core
                    // 字符层到位了，这一页的锁定框才解得出来 —— 立刻补上，
                    // 否则要等下次挂 overlay 才出现。
                    textOverlays[number]?.cardMarkers = cardMarkers(page: number)
                    // 这一页的字符尺寸到了，生词下划线/振假名这时才取得动 —— 让阅读器补一次
                    // （页面停着不动时不会再有布局回调）。
                    onGeometry?()
                } catch {
                    guard generation == ticket, !Task.isCancelled else { return }
                    if characterReadFailed(number) {
                        self.error = "本页文字层读取失败：\(error.localizedDescription)"
                    }
                }
            }
        }
    }

    /// 记一次没读到；返回 true = 这一页放弃了（重试到上限）。
    /// 退避 1.5s → 3s → 6s → 12s → 24s，第 6 次仍没有才判定这一页没有文字层。
    @discardableResult
    private func characterReadFailed(_ number: Int) -> Bool {
        let attempts = (characterRetry[number]?.attempts ?? 0) + 1
        guard attempts < 6 else {
            characterRetry[number] = nil
            unavailableCharacterPages.insert(number)
            return true
        }
        let delay = 1.5 * pow(2, Double(attempts - 1))
        characterRetry[number] = (attempts, Date().addingTimeInterval(delay))
        // 页面停着不动也要重试：不排这一下的话，只有下次滚动/布局才会再问。
        if !characterRetryScheduled {
            characterRetryScheduled = true
            let ticket = generation
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                guard let self else { return }
                self.characterRetryScheduled = false
                guard self.generation == ticket else { return }
                self.loadVisibleCharacterPages()
            }
        }
        return false
    }

    private func acceptOCRSelection(_ selected: ReaderNativePDFSelection.Value, page: Int) {
        guard let access, let chars = characterPages[page], !selected.indexes.isEmpty,
              selected.indexes.allSatisfy({ chars.chars.indices.contains($0) }), chars.contentSHA256.lowercased() == digest else { return }
        diagnose("sel-overlay", "[native-sel] overlay page=\(page) chars=\(selected.indexes.count)")
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
    /// 选区定下来 / 清掉：交给文档去显示原版那种选区窗口（nil = 收起）。
    var onPanel: ((ReaderNativePDFSelection.Value?) -> Void)?
    var onSearch: ((ReaderNativePDFSelection.Value) -> Void)?
    var onChat: ((ReaderNativePDFSelection.Value) -> Void)?
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
        guard characters != nil else { return false }
        if [leadingHandle, trailingHandle].contains(where: { !$0.isHidden && $0.frame.contains(point) }) { return true }
        return hit(point) != nil
    }
    private func hit(_ point: CGPoint) -> Int? {
        guard let p = canonicalPoint?(point) else { return nil }
        return selectionCore?.hit(p)
    }
    /// 点到已有划线时返回它的 id。接管后 .hl-layer 不存在，原生是唯一能点到划线的地方。
    var highlightAt: ((CGPoint) -> String?)?
    /// 点到查词等待高亮时返回它的 id（点它 = 打开那个词的结果）。
    var pendingLookupAt: ((CGPoint) -> UUID?)?
    var onOpenPendingLookup: ((UUID) -> Void)?
    var onEditHighlight: ((String) -> Void)?

    @objc private func tapText(_ gesture: UITapGestureRecognizer) {
        let location = gesture.location(in: self)
        // 点在已有划线上 → 开它的编辑面板（与网页「点划线弹浮层」同一个意思），
        // 而不是把那一个字选起来。⚠ 顺序不能反：先 resolve 再判断的话，菜单已经
        // 弹出来了，编辑面板会叠在它上面。
        // 点在卡片锁定框上 → 展开那张卡。⚠ 排在划线之前：绑卡的那一段往往同时
        //   也划了线，先判划线的话卡永远打不开。
        if let id = pendingLookupAt?(location) { onOpenPendingLookup?(id); return }
        if let id = cardMarkerAt(location) { onOpenCard?(id); return }
        if let id = highlightAt?(location) { onEditHighlight?(id); return }
        guard let index = hit(location) else { return }
        // 单击一个词 = 直接查词（原版 15-phrase-wordpop「单击单词 → 单词小框」），
        // 不先弹一排按钮让人再点一次。拖选 / 长按才出选区窗口。
        resolve(index, index)
        if let value = selected { onPanel?(nil); onLookup?(value, "dict") }
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
    /// 选区定下来：出原版那种选区窗口（`#sel-toolbar`：左色板 + 右预览/按钮），
    /// 不再弹系统编辑菜单 —— 2026-09-23 用户："这和我们之前设计的不一样"。
    private func showMenu() {
        guard selected != nil else { return }
        onPanel?(selected)
    }

    /// 选区在窗口里的框（选区窗口贴着它摆）。
    func selectionWindowRect() -> CGRect? {
        guard let selected, window != nil else { return nil }
        var union = CGRect.null
        for rect in selected.rects { if let projected = project?(rect) { union = union.union(projected) } }
        guard !union.isNull else { return nil }
        return convert(union, to: nil)
    }

    /// 选区窗口上的按钮。实现与原来菜单里的同名动作逐一相同。
    func perform(_ key: String) {
        guard let value = selected else { return }
        switch key {
        case "copy":
            UIPasteboard.general.string = value.text
        case "dict", "translate", "phrase", "explain":
            onLookup?(value, key)
        case "grammar":
            onGrammar?(value)
        case "ocr":
            var union = CGRect.null
            for rect in value.rects { union = union.union(rect) }
            guard !union.isNull, union.width >= 0.5, union.height >= 0.5 else { return }
            onRecognize?(union)
        case "search":
            onSearch?(value)
        case "chat":
            onChat?(value)
        default:
            if key.hasPrefix("highlight:") {
                onHighlight?(value, String(key.dropFirst("highlight:".count)))
                clearSelection()
            }
        }
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
        let had = selected != nil
        start = nil; handleAnchor = nil; selected = nil
        editMenu.dismissMenu(); updateHandles(); setNeedsDisplay()
        if had { onPanel?(nil) }
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
    /// Use the source-space order also sent to the assistant. Viewport scaling
    /// must not change the identity addressed by a visible number.
    private func numberedMarkers() -> [(ReaderNativePDFDocument.CardMarker, Int)] {
        cardMarkers.map { ($0,$0.number) }
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
final class ReaderNativePDFView: PDFView, UIDropInteractionDelegate {
    var onLayout: (() -> Void)?
    /// Resolve the page at release, before asynchronous item-provider decoding.
    var cardDropReceiver: ((CGPoint) -> ((ReaderNativeCardTransfer) -> Void)?)?
    var onCardDropError: ((String) -> Void)?
    private lazy var cardDropInteraction = UIDropInteraction(delegate: self)
    private var dropStrippedAt = Date.distantPast
    override func layoutSubviews() {
        super.layoutSubviews()
        if cardDropInteraction.view == nil { addInteraction(cardDropInteraction) }
        stripDropInteractions()
        onLayout?()
    }

    func dropInteraction(_ interaction: UIDropInteraction, canHandle session: UIDropSession) -> Bool {
        session.localDragSession != nil && session.items.count == 1 &&
            session.hasItemsConforming(toTypeIdentifiers: [ReaderNativeCardTransfer.contentType.identifier])
    }

    func dropInteraction(_ interaction: UIDropInteraction, sessionDidUpdate session: UIDropSession) -> UIDropProposal {
        UIDropProposal(operation: cardDropReceiver?(session.location(in: self)) == nil ? .forbidden : .copy)
    }

    func dropInteraction(_ interaction: UIDropInteraction, performDrop session: UIDropSession) {
        guard let receive = cardDropReceiver?(session.location(in: self)), let item = session.items.first else { return }
        item.itemProvider.loadDataRepresentation(forTypeIdentifier: ReaderNativeCardTransfer.contentType.identifier) { [weak self] data, error in
            Task { @MainActor in
                guard let self else { return }
                guard let data, data.count <= 16_384,
                      let payload = try? JSONDecoder().decode(ReaderNativeCardTransfer.self, from: data),
                      !payload.actionID.isEmpty else {
                    self.onCardDropError?(error?.localizedDescription ?? "这张卡片的拖放数据不可用，请重试。")
                    return
                }
                receive(payload)
            }
        }
    }

    /// PDFKit's internal receivers do not understand Reader card handles.
    /// Keep our receiver on PDFView; the outer SwiftUI receiver serves EPUB.
    private func stripDropInteractions() {
        let now = Date()
        guard now.timeIntervalSince(dropStrippedAt) > 1 else { return }
        dropStrippedAt = now
        func strip(_ view: UIView, depth: Int) {
            for interaction in view.interactions where interaction is UIDropInteraction && interaction !== cardDropInteraction {
                view.removeInteraction(interaction)
            }
            guard depth < 6 else { return }
            for child in view.subviews { strip(child, depth: depth + 1) }
        }
        strip(self, depth: 0)
    }
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

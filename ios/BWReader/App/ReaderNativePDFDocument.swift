import PDFKit
import Combine
import SwiftUI
import UIKit

/// The native PDF viewport owns only display and coordinates. Original OCR
/// character indexes, book identity and persisted overlays remain authoritative.
/// Wiring its ports replaces the document renderer, not the data repositories.
@MainActor
final class ReaderNativePDFDocument: NSObject, ObservableObject, PDFPageOverlayViewProvider {
    struct Position: Equatable {
        let page: Int
        let scale: CGFloat
        let visiblePages: [Int]
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
    struct Highlight {
        let rect: CGRect
        let color: Color
    }

    let view = ReaderNativePDFView()
    @Published private(set) var position = Position(page: 1, scale: 1, visiblePages: [])
    @Published private(set) var error: String?
    @Published private(set) var ready = false
    @Published private(set) var geometryRevision = 0
    @Published private(set) var ink: [Int: [ReaderNativeCardStroke]] = [:]
    @Published private(set) var highlights: [Int: [Highlight]] = [:]
    var onPosition: ((Position) -> Void)?
    var onSelection: (([CharacterSelection]) -> Void)?
    var onGeometry: (() -> Void)?
    private var access: ReaderLocalBookAccess?
    private var digest = ""
    private var generation = UUID()
    private var observations: [NSObjectProtocol] = []
    private var selectionTask: Task<Void, Never>?
    private var scrollObservation: NSKeyValueObservation?
    private var offsetObservation: NSKeyValueObservation?
    private weak var observedScroll: UIScrollView?
    private var pendingPage: Int?
    private var domainHeaders: [ReaderBookUserStateDomainName: (revision: Int64, digest: String)] = [:]
    private var lastPageFrames: [Int: CGRect] = [:]
    private var lastViewBounds = CGRect.null
    private var characterPages: [Int: NativeBookOCRPageCharacters] = [:]
    private var selectionCores: [Int: ReaderNativePDFSelection] = [:]
    private var characterReads: [Int: Task<Void, Never>] = [:]
    private var characterReadTickets: [Int: UUID] = [:]
    private var unavailableCharacterPages = Set<Int>()
    private var textOverlays: [Int: ReaderNativePDFTextOverlay] = [:]
    private var ocrUpdates: AnyCancellable?
    private var customSelection = false

    override init() {
        super.init()
        view.backgroundColor = UIColor(ReaderNativeTheme.canvas)
        view.displayMode = .singlePageContinuous
        view.displayDirection = .vertical
        view.displayBox = .cropBox
        view.autoScales = true
        view.pageOverlayViewProvider = self
        view.onLayout = { [weak self] in
            Task { @MainActor in self?.layoutChanged() }
        }
        for name in [Notification.Name.PDFViewPageChanged, .PDFViewScaleChanged] {
            observations.append(NotificationCenter.default.addObserver(forName: name, object: view, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.publishPosition() }
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
                    self.characterPages[page] = nil; self.unavailableCharacterPages.remove(page)
                    self.selectionCores[page] = nil; self.textOverlays[page]?.selectionCore = nil
                    self.characterReads[page]?.cancel(); self.characterReads[page] = nil
                    self.characterReadTickets[page] = nil
                    self.textOverlays[page]?.characters = nil
                } else {
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

    func open(_ access: ReaderLocalBookAccess, contentSHA256: String, page: Int) throws {
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
        pendingPage = min(document.pageCount, max(1, page))
        view.document = document
        ready = true
        view.setNeedsLayout()
    }

    func close() {
        generation = UUID()
        selectionTask?.cancel(); selectionTask = nil
        scrollObservation = nil; offsetObservation = nil; observedScroll = nil
        view.document = nil
        access = nil; digest = ""; pendingPage = nil
        ready = false; error = nil
        domainHeaders = [:]; ink = [:]; highlights = [:]
        lastPageFrames = [:]; lastViewBounds = .null
        characterReads.values.forEach { $0.cancel() }; characterReads = [:]
        characterReadTickets = [:]
        characterPages = [:]; selectionCores = [:]; textOverlays = [:]; unavailableCharacterPages = []; customSelection = false
        onSelection?([])
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
                    let hex = value["color"] as? String ?? "#fff59d"
                    let color = ReaderNativeCardStroke(["pts": [[0,0]], "c": hex])?.color ?? .yellow
                    for rect in value["rects"] as? [[NSNumber]] ?? [] {
                        guard rect.count == 4, rect.allSatisfy({ $0.doubleValue.isFinite }) else { continue }
                        let box = CGRect(x: rect[0].doubleValue / width, y: rect[1].doubleValue / height,
                                         width: (rect[2].doubleValue - rect[0].doubleValue) / width,
                                         height: (rect[3].doubleValue - rect[1].doubleValue) / height)
                        if box.width > 0, box.height > 0 {
                            nextHighlights[number, default: []].append(Highlight(rect: box, color: color))
                        }
                    }
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

    func go(to page: Int) throws {
        guard let document = view.document, page >= 1, page <= document.pageCount,
              let target = document.page(at: page - 1) else { throw NativeBookOCRError.pageUnavailable }
        view.go(to: target)
        publishPosition()
    }

    func setSpread(_ enabled: Bool, firstPageAlone: Bool) {
        let destination = view.currentDestination
        view.displaysAsBook = firstPageAlone
        view.displayMode = enabled ? .twoUpContinuous : .singlePageContinuous
        view.autoScales = true
        if let destination { view.go(to: destination) }
        publishPosition()
    }

    func fitWidth() {
        guard let page = view.currentPage else { return }
        let size = displayedSize(page)
        guard size.width > 0, view.bounds.width > 0 else { return }
        view.autoScales = false
        let columns: CGFloat = view.displayMode == .twoUpContinuous ? 2 : 1
        view.scaleFactor = max(view.minScaleFactor, min(view.maxScaleFactor, (view.bounds.width - 16) / (size.width * columns)))
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
        guard box.isFiniteRect, box.width > 0, box.height > 0, box.contains(local) else { return nil }
        return (document.index(for: page) + 1, CGPoint(x: (local.x - box.minX) / box.width, y: (local.y - box.minY) / box.height))
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
            try? go(to: page)
        }
        geometryChanged()
    }

    private func publishPosition() {
        guard let document = view.document, let page = view.currentPage else { return }
        let next = Position(page: document.index(for: page) + 1, scale: view.scaleFactor,
                            visiblePages: view.visiblePages.map { document.index(for: $0) + 1 })
        if next != position { position = next; onPosition?(next) }
        geometryChanged()
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
        overlay.onSelect = { [weak self] value in self?.acceptOCRSelection(value, page: number) }
        overlay.onError = { [weak self] in self?.error = "当前文字层无法确认这段选区的位置。" }
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
private final class ReaderNativePDFTextOverlay: UIView {
    var characters: NativeBookOCRPageCharacters? { didSet { clearSelection() } }
    var selectionCore: ReaderNativePDFSelection? { didSet { clearSelection() } }
    var embeddedText = false
    var canonicalPoint: ((CGPoint) -> CGPoint?)?
    var project: ((CGRect) -> CGRect?)?
    var onSelect: ((ReaderNativePDFSelection.Value) -> Void)?
    var onError: (() -> Void)?
    private var start: Int?
    private var selected: ReaderNativePDFSelection.Value?

    override init(frame: CGRect) {
        super.init(frame: frame)
        isOpaque = false; backgroundColor = .clear
        let gesture = UILongPressGestureRecognizer(target: self, action: #selector(selectText(_:)))
        gesture.minimumPressDuration = 0.3
        gesture.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        addGestureRecognizer(gesture)
    }
    required init?(coder: NSCoder) { return nil }
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        guard let characters, !embeddedText || characters.textAuthority == .localOverride else { return false }
        return hit(point) != nil
    }
    private func hit(_ point: CGPoint) -> Int? {
        guard let p = canonicalPoint?(point) else { return nil }
        return selectionCore?.hit(p)
    }
    @objc private func selectText(_ gesture: UILongPressGestureRecognizer) {
        guard let selectionCore else { return }
        switch gesture.state {
        case .began:
            start = hit(gesture.location(in: self))
            if let start { resolve(start, start) }
        case .changed, .ended:
            if let start, let point = canonicalPoint?(gesture.location(in: self)),
               let end = selectionCore.hit(point, anchor: start, exactOnly: false) { resolve(start, end) }
            if gesture.state == .ended { self.start = nil }
        case .cancelled, .failed: start = nil
        default: break
        }
    }
    private func resolve(_ start: Int, _ end: Int) {
        do {
            guard let value = try selectionCore?.range(from: start, to: end), value.indexes != selected?.indexes else { return }
            selected = value; setNeedsDisplay(); onSelect?(value)
        } catch { onError?() }
    }
    func clearSelection() { start = nil; selected = nil; setNeedsDisplay() }
    override func draw(_ rect: CGRect) {
        guard let selected, let context = UIGraphicsGetCurrentContext() else { return }
        context.setFillColor(UIColor.systemTeal.withAlphaComponent(0.22).cgColor)
        for normalized in selected.rects {
            if let box = project?(normalized) { context.fill(box) }
        }
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
    var body: some View {
        ZStack {
            ReaderNativePDFSurface(document: document)
            Canvas { context, _ in
                let _ = document.geometryRevision
                for page in document.view.visiblePages {
                    guard let owner = document.view.document else { continue }
                    let number = owner.index(for: page) + 1
                    guard let frame = document.viewRect(normalized: CGRect(x: 0,y: 0,width: 1,height: 1), page: number) else { continue }
                    var pageContext = context
                    pageContext.clip(to: Path(frame))
                    for highlight in document.highlights[number] ?? [] {
                        if let rect = document.viewRect(normalized: highlight.rect, page: number) {
                            pageContext.fill(Path(rect), with: .color(highlight.color.opacity(0.3)))
                        }
                    }
                    for stroke in document.ink[number] ?? [] {
                        ReaderNativeInkDrawing.draw(stroke, in: frame, context: &pageContext)
                    }
                }
            }.allowsHitTesting(false)
        }.clipped()
    }
}

private extension CGRect {
    var isFiniteRect: Bool { [minX, minY, width, height].allSatisfy(\.isFinite) && !isNull && !isInfinite }
}

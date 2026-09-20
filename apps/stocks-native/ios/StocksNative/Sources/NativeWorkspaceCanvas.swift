import SwiftUI
import UIKit

/// The scroll view owns positions, while stable hosting controllers own each chart's state.
struct NativeWorkspaceCanvas: UIViewControllerRepresentable {
    let page: WorkspacePage
    var isEditing = true
    var inkContext: StockWorkspaceInkContext? = nil
    var onInkSnapshot: ((StockWorkspaceInkSnapshot) -> Void)? = nil
    let content: (WorkspaceCard) -> AnyView
    let minimumContentSize: (WorkspaceCard, CGFloat) -> CGSize
    let onCommit: ([WorkspaceCard]) -> Void

    func makeUIViewController(context: Context) -> NativeWorkspaceCanvasController {
        let controller = NativeWorkspaceCanvasController()
        controller.update(page: page, layoutEditing: isEditing, content: content,
                          minimumContentSize: minimumContentSize, onCommit: onCommit,
                          inkContext: inkContext, onInkSnapshot: onInkSnapshot)
        return controller
    }

    func updateUIViewController(_ controller: NativeWorkspaceCanvasController, context: Context) {
        controller.update(page: page, layoutEditing: isEditing, content: content,
                          minimumContentSize: minimumContentSize, onCommit: onCommit,
                          inkContext: inkContext, onInkSnapshot: onInkSnapshot)
    }
}

@MainActor
final class NativeWorkspaceCanvasController: UIViewController, UIGestureRecognizerDelegate, UIPencilInteractionDelegate {
    private let scrollView = UIScrollView()
    private let canvas = UIView()
    private let previewLayer = CAShapeLayer()
    private let targetLayer = CAShapeLayer()
    private let guideLayer = CAShapeLayer()
    private let dropLabel = UILabel()
    private var hosts: [String: WorkspaceCardHost] = [:]
    private var splitters: [UIView] = []
    private var page: WorkspacePage?
    private var cards: [WorkspaceCard] = []
    private var layoutEditing = true
    private var content: ((WorkspaceCard) -> AnyView)?
    private var minimumContentSize: ((WorkspaceCard, CGFloat) -> CGSize)?
    private var constraints = WorkspaceGridConstraints.unrestricted
    private var onCommit: (([WorkspaceCard]) -> Void)?
    private let inkSettings = StockWorkspaceInkSettings()
    private var inkContext: StockWorkspaceInkContext?
    private var onInkSnapshot: ((StockWorkspaceInkSnapshot) -> Void)?
    private var changedInkCards = Set<String>()
    private var inkSnapshotWork: DispatchWorkItem?
    private var interaction: WorkspaceCanvasInteraction?
    private var displayLink: CADisplayLink?
    private var displayLinkProxy: WorkspaceCanvasDisplayLinkProxy?
    private var lastTimestamp: CFTimeInterval = 0
    private var lastViewportPoint = CGPoint.zero
    private var edgeHoverStartedAt: CFTimeInterval?
    private var edgeHoverDirection: CGFloat = 0
    private var pendingUpdate = false
    private var laidOutWidth: CGFloat = 0
    private let margin: CGFloat = 18

    private var gap: CGFloat { WorkspaceGridEngine.gap }
    private var rowPitch: CGFloat { WorkspaceGridEngine.rowHeight + gap }
    private var canvasWidth: CGFloat { max(1, scrollView.bounds.width) }
    private var columnPitch: CGFloat { max(1, (canvasWidth - margin * 2 + gap) / CGFloat(WorkspaceGridEngine.columnCount)) }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(AppStyle.canvas)
        scrollView.backgroundColor = .clear
        scrollView.alwaysBounceVertical = true
        scrollView.alwaysBounceHorizontal = false
        scrollView.isDirectionalLockEnabled = true
        scrollView.contentInsetAdjustmentBehavior = .never
        scrollView.keyboardDismissMode = .onDrag
        // Pencil belongs to the card's ink canvas; only fingers/trackpads scroll.
        scrollView.panGestureRecognizer.allowedTouchTypes = [
            NSNumber(value: UITouch.TouchType.direct.rawValue),
            NSNumber(value: UITouch.TouchType.indirectPointer.rawValue)
        ]
        scrollView.delaysContentTouches = false
        view.addSubview(scrollView)
        let pencilInteraction = UIPencilInteraction()
        pencilInteraction.delegate = self
        view.addInteraction(pencilInteraction)
        canvas.backgroundColor = .clear
        scrollView.addSubview(canvas)
        previewLayer.fillColor = UIColor(AppStyle.accent).withAlphaComponent(0.06).cgColor
        previewLayer.strokeColor = UIColor(AppStyle.accent).withAlphaComponent(0.35).cgColor
        previewLayer.lineWidth = 1
        targetLayer.fillColor = UIColor(AppStyle.accent).withAlphaComponent(0.23).cgColor
        targetLayer.strokeColor = UIColor(AppStyle.accent).cgColor
        targetLayer.lineWidth = 2
        targetLayer.lineDashPattern = [5, 4]
        guideLayer.strokeColor = UIColor(AppStyle.accent).cgColor
        guideLayer.lineWidth = 2
        guideLayer.fillColor = UIColor.clear.cgColor
        canvas.layer.addSublayer(previewLayer)
        canvas.layer.addSublayer(targetLayer)
        canvas.layer.addSublayer(guideLayer)
        dropLabel.font = .preferredFont(forTextStyle: .caption1)
        dropLabel.textColor = UIColor(AppStyle.accent)
        dropLabel.backgroundColor = UIColor.systemBackground.withAlphaComponent(0.94)
        dropLabel.textAlignment = .center
        dropLabel.layer.cornerRadius = 8
        dropLabel.clipsToBounds = true
        dropLabel.isUserInteractionEnabled = false
        dropLabel.isHidden = true
        view.addSubview(dropLabel)
        installHosts()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        scrollView.frame = view.bounds
        if scrollView.contentOffset.x != 0 {
            scrollView.setContentOffset(CGPoint(x: 0, y: scrollView.contentOffset.y), animated: false)
        }
        if abs(laidOutWidth - canvasWidth) > 0.5 {
            if interaction != nil { finishInteraction(commit: false) }
            laidOutWidth = canvasWidth
            let source = page?.cards ?? cards
            constraints = makeConstraints(for: source)
            cards = WorkspaceGridEngine.normalized(source, constraints: constraints)
            layoutHosts()
        } else if interaction == nil {
            updateContentSize()
        }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        finishInteraction(commit: false)
        inkSnapshotWork?.cancel()
        changedInkCards.removeAll()
        hosts.values.forEach { $0.ink.save() }
    }

    deinit { displayLink?.invalidate() }

    func pencilInteractionDidTap(_ interaction: UIPencilInteraction) { inkSettings.pencilDoubleTap() }

    @available(iOS 17.5, *)
    func pencilInteraction(_ interaction: UIPencilInteraction, didReceiveTap tap: UIPencilInteraction.Tap) {
        inkSettings.pencilDoubleTap()
    }

    @available(iOS 17.5, *)
    func pencilInteraction(_ interaction: UIPencilInteraction, didReceiveSqueeze squeeze: UIPencilInteraction.Squeeze) {
        guard squeeze.phase == .ended, view.window != nil else { return }
        // Reader routes Pencil Pro squeeze separately from tip long-press.
        showInkSettings(source: view, point: CGPoint(x: view.bounds.midX, y: view.bounds.midY))
    }

    func update(page: WorkspacePage, layoutEditing: Bool, content: @escaping (WorkspaceCard) -> AnyView,
                minimumContentSize: @escaping (WorkspaceCard, CGFloat) -> CGSize,
                onCommit: @escaping ([WorkspaceCard]) -> Void,
                inkContext: StockWorkspaceInkContext?,
                onInkSnapshot: ((StockWorkspaceInkSnapshot) -> Void)?) {
        let changedPage = self.page?.id != page.id
        let changedInkIdentity = self.inkContext?.stockCode != inkContext?.stockCode
            || self.inkContext?.scopeID != inkContext?.scopeID
            || self.inkContext?.cardScopeIDs != inkContext?.cardScopeIDs
        if changedPage || changedInkIdentity {
            inkSnapshotWork?.cancel()
            changedInkCards.removeAll()
        }
        if interaction != nil, changedInkIdentity { finishInteraction(commit: false) }
        self.inkContext = inkContext
        self.onInkSnapshot = onInkSnapshot
        if interaction != nil, changedPage || !layoutEditing { finishInteraction(commit: false) }
        self.page = page
        self.layoutEditing = layoutEditing
        self.content = content
        self.minimumContentSize = minimumContentSize
        self.onCommit = onCommit
        if interaction != nil {
            // Market updates continue inside observed SwiftUI cards; replacing roots while
            // dragging would unnecessarily rebuild expensive chart descriptions every frame.
            pendingUpdate = true
            return
        }
        constraints = makeConstraints(for: page.cards)
        cards = WorkspaceGridEngine.normalized(page.cards, constraints: constraints)
        guard isViewLoaded else { return }
        if changedPage { scrollView.setContentOffset(.zero, animated: false) }
        installHosts()
        layoutHosts()
    }

    private func installHosts() {
        guard let page, let content else { return }
        let visible = cards.filter(\.isVisible)
        let wanted = Set(visible.map { "\(page.id):\($0.id)" })
        for key in Array(hosts.keys) where !wanted.contains(key) {
            guard let host = hosts.removeValue(forKey: key) else { continue }
            host.ink.save()
            host.controller.willMove(toParent: nil)
            host.removeFromSuperview()
            host.controller.removeFromParent()
        }
        for card in visible {
            let key = "\(page.id):\(card.id)"
            if let host = hosts[key] {
                host.controller.rootView = content(card)
                host.setEditing(layoutEditing)
            } else {
                let controller = UIHostingController(rootView: content(card))
                // The canvas already accounts for screen insets. Each card must use
                // its whole local rectangle, including cards near the screen edge.
                controller.safeAreaRegions = []
                addChild(controller)
                let host = WorkspaceCardHost(controller: controller, kind: card.kind, inkSettings: inkSettings)
                host.accessibilityIdentifier = "workspace.card.\(page.id).\(card.id)"
                canvas.addSubview(host)
                controller.didMove(toParent: self)
                host.grip.addGestureRecognizer(makePan(.move(card.id)))
                let resize = makePan(.resize(card.id))
                host.resizeGrip.addGestureRecognizer(resize)
                host.setEditing(layoutEditing)
                hosts[key] = host
            }
            if let host = hosts[key] {
                let scope = inkContext.map {
                    "\($0.stockCode)|\(page.id)|\(card.id)|\($0.cardScopeIDs[card.id] ?? $0.scopeID)"
                }
                host.ink.configure(scope: scope)
                host.ink.onChanged = { [weak self] in self?.inkChanged(card.id) }
                host.ink.onShowSettings = { [weak self] source, point in
                    self?.showInkSettings(source: source, point: point)
                }
            }
        }
    }

    private func showInkSettings(source: UIView, point: CGPoint) {
        guard presentedViewController == nil else { return }
        let palette = UIHostingController(rootView: StockWorkspaceInkPalette(settings: inkSettings))
        palette.modalPresentationStyle = .popover
        palette.preferredContentSize = CGSize(width: 310, height: 290)
        palette.popoverPresentationController?.sourceView = source
        palette.popoverPresentationController?.sourceRect = CGRect(x: point.x, y: point.y, width: 1, height: 1)
        palette.popoverPresentationController?.permittedArrowDirections = [.up, .down]
        present(palette, animated: true)
    }

    private func inkChanged(_ cardID: String) {
        guard inkContext != nil else { return }
        changedInkCards.insert(cardID)
        inkSnapshotWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.captureInkSnapshot() }
        inkSnapshotWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.62, execute: work)
    }

    private func captureInkSnapshot() {
        guard let context = inkContext, !changedInkCards.isEmpty,
              interaction == nil, view.window != nil else { return }
        guard !hosts.values.contains(where: { $0.ink.isDrawing }) else {
            let work = DispatchWorkItem { [weak self] in self?.captureInkSnapshot() }
            inkSnapshotWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.7, execute: work)
            return
        }
        let changed = changedInkCards
        changedInkCards.removeAll()
        let visibleRect = scrollView.convert(scrollView.bounds, to: canvas)
        let visible = cards.filter { $0.isVisible && host(for: $0.id)?.frame.intersects(visibleRect) == true }
        let affected = visible.filter { changed.contains($0.id) }
        guard !affected.isEmpty else { return }
        let inkCards = affected.filter { host(for: $0.id)?.ink.hasInk == true }
        // An erase-to-empty event must replace the old server-side visual context.
        if inkCards.isEmpty {
            onInkSnapshot?(StockWorkspaceInkSnapshot(id: UUID().uuidString, stockCode: context.stockCode,
                scopeID: context.scopeID, sourceTime: context.sourceTime, capturedAt: Date(),
                cardIDs: affected.map(\.id), inkCardIDs: [], bounds: [:], jpegBase64: nil, cleared: true))
            return
        }
        let markedBounds = inkCards.compactMap { host(for: $0.id)?.frame }.reduce(CGRect.null) { $0.union($1) }
        let neighbors = visible.filter { !changed.contains($0.id) }.map { card -> (WorkspaceCard, CGFloat) in
            guard let rect = host(for: card.id)?.frame else { return (card, .greatestFiniteMagnitude) }
            let dx = max(0, max(markedBounds.minX - rect.maxX, rect.minX - markedBounds.maxX))
            let dy = max(0, max(markedBounds.minY - rect.maxY, rect.minY - markedBounds.maxY))
            return (card, dx * dx + dy * dy)
        }.sorted { $0.1 < $1.1 }.map { $0.0 }
        let selected = Array((inkCards + neighbors).prefix(3))
        let captureBounds = selected.compactMap { host(for: $0.id)?.frame.intersection(visibleRect) }
            .reduce(CGRect.null) { $0.union($1) }
        guard !captureBounds.isNull, captureBounds.width > 1, captureBounds.height > 1 else { return }
        let scale = min(1, 1024 / max(captureBounds.width, captureBounds.height))
        let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = true
        let imageSize = CGSize(width: captureBounds.width * scale, height: captureBounds.height * scale)
        let renderer = UIGraphicsImageRenderer(size: imageSize, format: format)
        let image = renderer.image { rendererContext in
            UIColor.systemBackground.setFill()
            rendererContext.fill(CGRect(origin: .zero, size: imageSize))
            let cg = rendererContext.cgContext
            cg.scaleBy(x: scale, y: scale)
            cg.translateBy(x: -captureBounds.minX, y: -captureBounds.minY)
            for card in selected {
                guard let host = host(for: card.id) else { continue }
                cg.saveGState()
                cg.clip(to: host.frame.intersection(visibleRect))
                cg.translateBy(x: host.frame.minX, y: host.frame.minY)
                let wasHidden = host.ink.isHidden
                host.ink.isHidden = true
                if !host.drawHierarchy(in: host.bounds, afterScreenUpdates: false) { host.layer.render(in: cg) }
                host.ink.isHidden = wasHidden
                if !wasHidden { host.ink.renderInk(in: host.ink.frame) }
                cg.restoreGState()
            }
        }
        guard let jpeg = Self.boundedInkJPEG(image) else { return }
        let bounds = Dictionary(uniqueKeysWithValues: selected.compactMap { card -> (String, StockWorkspaceInkRect)? in
            guard let ink = host(for: card.id)?.ink, ink.hasInk else { return nil }
            return (card.id, StockWorkspaceInkRect(ink.inkBounds))
        })
        onInkSnapshot?(StockWorkspaceInkSnapshot(id: UUID().uuidString, stockCode: context.stockCode,
            scopeID: context.scopeID, sourceTime: context.sourceTime, capturedAt: Date(),
            cardIDs: selected.map(\.id), inkCardIDs: selected.filter { host(for: $0.id)?.ink.hasInk == true }.map(\.id), bounds: bounds,
            jpegBase64: jpeg.base64EncodedString(), cleared: false))
    }

    private static func boundedInkJPEG(_ source: UIImage) -> Data? {
        var image = source
        for _ in 0..<3 {
            for quality in [0.72, 0.52, 0.34] {
                if let data = image.jpegData(compressionQuality: quality), data.count <= 220_000 { return data }
            }
            let size = CGSize(width: image.size.width * 0.75, height: image.size.height * 0.75)
            let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = true
            image = UIGraphicsImageRenderer(size: size, format: format).image { _ in
                image.draw(in: CGRect(origin: .zero, size: size))
            }
        }
        return nil
    }

    private func host(for id: String) -> WorkspaceCardHost? {
        guard let page else { return nil }
        return hosts["\(page.id):\(id)"]
    }

    private func frame(for rect: WorkspaceGridRect) -> CGRect {
        CGRect(x: margin + CGFloat(rect.column) * columnPitch,
               y: margin + CGFloat(rect.row) * rowPitch,
               width: max(1, CGFloat(rect.width) * columnPitch - gap),
               height: max(1, CGFloat(rect.height) * rowPitch - gap))
    }

    private func makeConstraints(for cards: [WorkspaceCard]) -> WorkspaceGridConstraints {
        guard let minimumContentSize else { return .unrestricted }
        var result = WorkspaceGridConstraints(columnPitch: columnPitch, rowPitch: rowPitch)
        for card in cards {
            let minimumWidth = minimumContentSize(card, canvasWidth - margin * 2).width
            result.minimumColumns[card.id] = min(WorkspaceGridEngine.columnCount,
                max(1, Int(ceil((minimumWidth + gap) / columnPitch))))
            var rows: [Int: Int] = [:]
            for columns in 1...WorkspaceGridEngine.columnCount {
                let width = max(1, CGFloat(columns) * columnPitch - gap)
                // Reserve the content inset even when locked; the touch target stays 24pt.
                let height = minimumContentSize(card, width).height + WorkspaceCardHost.headerInset(for: card.kind)
                rows[columns] = max(1, Int(ceil((height + gap) / rowPitch)))
            }
            result.minimumRows[card.id] = rows
        }
        return result
    }

    private func continuousGrid(_ frame: CGRect) -> CGRect {
        CGRect(x: (frame.minX - margin) / columnPitch, y: (frame.minY - margin) / rowPitch,
               width: (frame.width + gap) / columnPitch, height: (frame.height + gap) / rowPitch)
    }

    private func roundedGrid(_ rect: CGRect) -> WorkspaceGridRect {
        WorkspaceGridRect(column: Int(rect.minX.rounded()), row: Int(rect.minY.rounded()),
                          width: max(1, Int(rect.width.rounded())), height: max(1, Int(rect.height.rounded())))
    }

    private func layoutHosts() {
        for card in cards where card.isVisible {
            guard let rect = card.grid, let host = host(for: card.id) else { continue }
            host.frame = frame(for: rect)
        }
        updateContentSize()
        rebuildSplitters()
    }

    private func updateContentSize(extraRows: Int = 0) {
        let maxRow = cards.filter(\.isVisible).compactMap(\.grid).map(\.maxRow).max() ?? 1
        let height = max(scrollView.bounds.height + 1, margin * 2 + CGFloat(maxRow + extraRows) * rowPitch)
        canvas.frame = CGRect(x: 0, y: 0, width: canvasWidth, height: height)
        scrollView.contentSize = canvas.bounds.size
    }

    private func rebuildSplitters() {
        splitters.forEach { $0.removeFromSuperview() }
        splitters.removeAll()
        guard layoutEditing, interaction == nil else { return }
        for edge in WorkspaceGridEngine.sharedEdges(cards, constraints: constraints) {
            let splitter = WorkspaceSplitterView(vertical: edge.axis == .vertical)
            if edge.axis == .vertical {
                let x = margin + CGFloat(edge.coordinate) * columnPitch - gap / 2
                splitter.frame = CGRect(x: x - 12, y: margin + CGFloat(edge.rangeStart) * rowPitch,
                                        width: 24, height: max(24, CGFloat(edge.rangeEnd - edge.rangeStart) * rowPitch - gap))
            } else {
                let y = margin + CGFloat(edge.coordinate) * rowPitch - gap / 2
                splitter.frame = CGRect(x: margin + CGFloat(edge.rangeStart) * columnPitch, y: y - 12,
                                        width: max(24, CGFloat(edge.rangeEnd - edge.rangeStart) * columnPitch - gap), height: 24)
            }
            splitter.addGestureRecognizer(makePan(.split(edge)))
            canvas.addSubview(splitter)
            splitters.append(splitter)
        }
        // Install last so a junction wins hit testing over either one-axis edge.
        for junction in WorkspaceGridEngine.sharedJunctions(cards, constraints: constraints) {
            let handle = WorkspaceJunctionView()
            let x = margin + CGFloat(junction.vertical.coordinate) * columnPitch - gap / 2
            let y = margin + CGFloat(junction.horizontal.coordinate) * rowPitch - gap / 2
            handle.frame = CGRect(x: x - 22, y: y - 22, width: 44, height: 44)
            handle.accessibilityIdentifier = "workspace.junction.\(junction.id)"
            handle.addGestureRecognizer(makePan(.junction(junction)))
            canvas.addSubview(handle)
            splitters.append(handle)
        }
    }

    private func makePan(_ operation: WorkspaceCanvasOperation) -> WorkspaceCanvasPan {
        let pan = WorkspaceCanvasPan(operation: operation, target: self, action: #selector(didPan(_:)))
        pan.minimumNumberOfTouches = 1
        pan.maximumNumberOfTouches = 1
        pan.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue),
                                 NSNumber(value: UITouch.TouchType.indirectPointer.rawValue)]
        pan.delegate = self
        pan.cancelsTouchesInView = true
        return pan
    }

    func gestureRecognizerShouldBegin(_ gestureRecognizer: UIGestureRecognizer) -> Bool {
        layoutEditing && interaction == nil
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        layoutEditing && touch.type != .pencil
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                           shouldBeRequiredToFailBy otherGestureRecognizer: UIGestureRecognizer) -> Bool {
        // An edit touch belongs to one card/edge from the start. Blank-canvas
        // touches still scroll, without a permanent dependency on transient handles.
        gestureRecognizer is WorkspaceCanvasPan && otherGestureRecognizer === scrollView.panGestureRecognizer
    }

    @objc private func didPan(_ pan: WorkspaceCanvasPan) {
        lastViewportPoint = pan.location(in: view)
        switch pan.state {
        case .began:
            beginInteraction(pan.operation, point: pan.location(in: canvas),
                             translation: pan.translation(in: canvas))
            updateInteraction(point: pan.location(in: canvas))
        case .changed:
            updateInteraction(point: pan.location(in: canvas))
        case .ended:
            updateInteraction(point: pan.location(in: canvas))
            finishInteraction(commit: view.bounds.contains(lastViewportPoint))
        case .cancelled, .failed:
            finishInteraction(commit: false)
        default: break
        }
    }

    private func beginInteraction(_ operation: WorkspaceCanvasOperation, point: CGPoint, translation: CGPoint) {
        guard interaction == nil else { return }
        let startPoint = CGPoint(x: point.x - translation.x, y: point.y - translation.y)
        var ghosts: [String: UIView] = [:]
        for id in operation.cardIDs {
            guard let host = host(for: id) else { continue }
            let ghost = host.snapshotView(afterScreenUpdates: false) ?? UIView(frame: host.bounds)
            ghost.frame = host.frame
            ghost.backgroundColor = .white
            ghost.isUserInteractionEnabled = false
            ghost.alpha = 0.62
            ghost.layer.cornerRadius = 20
            ghost.layer.shadowColor = UIColor.black.cgColor
            ghost.layer.shadowOpacity = 0.15
            ghost.layer.shadowRadius = 14
            ghost.layer.shadowOffset = CGSize(width: 0, height: 5)
            canvas.addSubview(ghost)
            host.alpha = 0.12
            ghosts[id] = ghost
        }
        guard !ghosts.isEmpty else { return }
        let value = WorkspaceCanvasInteraction(operation: operation, initialCards: cards, startPoint: startPoint, ghosts: ghosts)
        interaction = value
        scrollView.isScrollEnabled = false
        splitters.compactMap { $0 as? WorkspaceSplitterView }.forEach { $0.setLineVisible(false) }
        splitters.compactMap { $0 as? WorkspaceJunctionView }.forEach { $0.setMarkVisible(false) }
        updateContentSize(extraRows: Int(ceil(scrollView.bounds.height / rowPitch)))
        canvas.layer.addSublayer(previewLayer)
        canvas.layer.addSublayer(targetLayer)
        canvas.layer.addSublayer(guideLayer)
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        let proxy = WorkspaceCanvasDisplayLinkProxy(owner: self)
        displayLinkProxy = proxy
        let link = CADisplayLink(target: proxy, selector: #selector(WorkspaceCanvasDisplayLinkProxy.tick(_:)))
        link.add(to: .main, forMode: .common)
        displayLink = link
        lastTimestamp = 0
        edgeHoverStartedAt = nil
        edgeHoverDirection = 0
    }

    private func updateInteraction(point: CGPoint) {
        guard let interaction else { return }
        let delta = CGPoint(x: point.x - interaction.startPoint.x, y: point.y - interaction.startPoint.y)
        var preview = interaction.initialCards
        var snap: WorkspaceGridSnapTarget?
        interaction.dropMessage = nil
        switch interaction.operation {
        case .move(let id):
            guard let original = interaction.initialCards.first(where: { $0.id == id })?.grid else { return }
            let start = frame(for: original)
            let proposed = start.offsetBy(dx: delta.x, dy: delta.y)
            interaction.ghosts[id]?.frame = proposed
            let grid = continuousGrid(proposed)
            let insertion = insertionTarget(at: point, in: interaction.initialCards, excluding: id,
                                            previous: interaction.lastSnap)
            snap = insertion ?? WorkspaceGridEngine.snapTarget(for: grid, in: interaction.initialCards, excluding: id,
                                                               columnTolerance: Double(28 / columnPitch), rowTolerance: Double(28 / rowPitch),
                                                               constraints: constraints)
            if let insertion, let targetID = insertion.primaryID {
                preview = WorkspaceGridEngine.insert(interaction.initialCards, id: id, targetID: targetID,
                                                      edge: insertion.edge, constraints: constraints)
                let horizontal = insertion.edge == .left || insertion.edge == .right
                if horizontal && constraints.columns(for: id) + constraints.columns(for: targetID) > WorkspaceGridEngine.columnCount {
                    interaction.dropMessage = "宽度不足，松手保持原位"
                } else {
                    let side: String
                    switch insertion.edge {
                    case .top: side = "上方"
                    case .bottom: side = "下方"
                    case .left: side = "左侧"
                    case .right: side = "右侧"
                    }
                    interaction.dropMessage = "松手放在" + side
                }
            } else if let snap, snap.primaryID != nil {
                // Dock against the original peer coordinates. Moving first would push
                // an overlapping peer away before its edge can be used as the anchor.
                preview = WorkspaceGridEngine.dock(interaction.initialCards, id: id, target: snap, constraints: constraints)
            } else {
                var proposedRect = roundedGrid(grid)
                if let snap {
                    if snap.edge == .left { proposedRect.column = 0 }
                    else if snap.edge == .right { proposedRect.column = WorkspaceGridEngine.columnCount - proposedRect.width }
                    else if snap.edge == .top { proposedRect.row = 0 }
                }
                preview = WorkspaceGridEngine.move(interaction.initialCards, id: id, to: proposedRect, constraints: constraints)
            }
        case .resize(let id):
            guard let original = interaction.initialCards.first(where: { $0.id == id })?.grid else { return }
            var proposed = frame(for: original)
            proposed.size.width = max(columnPitch, proposed.width + delta.x)
            proposed.size.height = max(rowPitch, proposed.height + delta.y)
            var grid = continuousGrid(proposed)
            let resizeSnap = resizeSnapTarget(for: grid, in: interaction.initialCards, excluding: id)
            if let resizeSnap, resizeSnap.edge == .left, CGFloat(resizeSnap.coordinate) > grid.minX {
                grid.size.width = CGFloat(resizeSnap.coordinate) - grid.minX
                snap = resizeSnap
            } else if let resizeSnap, resizeSnap.edge == .top, CGFloat(resizeSnap.coordinate) > grid.minY {
                grid.size.height = CGFloat(resizeSnap.coordinate) - grid.minY
                snap = resizeSnap
            } else if let resizeSnap, resizeSnap.primaryID == nil, resizeSnap.edge == .right {
                grid.size.width = CGFloat(resizeSnap.coordinate) - grid.minX
                snap = resizeSnap
            }
            preview = WorkspaceGridEngine.resize(interaction.initialCards, id: id, to: roundedGrid(grid), constraints: constraints)
            if let resolved = preview.first(where: { $0.id == id })?.grid { interaction.ghosts[id]?.frame = frame(for: resolved) }
        case .split(let edge):
            let shift = edge.axis == .vertical ? delta.x / columnPitch : delta.y / rowPitch
            preview = WorkspaceGridEngine.resizeSharedEdge(interaction.initialCards, edge: edge,
                                                           to: edge.coordinate + Int(shift.rounded()), constraints: constraints)
            for id in edge.leadingIDs + edge.trailingIDs {
                if let rect = preview.first(where: { $0.id == id })?.grid { interaction.ghosts[id]?.frame = frame(for: rect) }
            }
        case .junction(let junction):
            preview = WorkspaceGridEngine.resizeSharedJunction(interaction.initialCards, junction: junction,
                column: junction.vertical.coordinate + Int((delta.x / columnPitch).rounded()),
                row: junction.horizontal.coordinate + Int((delta.y / rowPitch).rounded()), constraints: constraints)
            for id in junction.cardIDs {
                if let rect = preview.first(where: { $0.id == id })?.grid { interaction.ghosts[id]?.frame = frame(for: rect) }
            }
        }
        interaction.preview = preview
        let previewBottom = preview.filter(\.isVisible).compactMap(\.grid).map(\.maxRow).max() ?? 0
        let neededHeight = margin * 2 + CGFloat(previewBottom) * rowPitch + scrollView.bounds.height / 2
        if neededHeight > canvas.bounds.height {
            canvas.frame.size.height = neededHeight
            scrollView.contentSize = canvas.bounds.size
        }
        drawPreview(preview, activeIDs: interaction.operation.cardIDs, snap: snap)
        if interaction.lastSnap != snap {
            if snap != nil { UISelectionFeedbackGenerator().selectionChanged() }
            interaction.lastSnap = snap
        }
    }

    private func insertionTarget(at point: CGPoint, in cards: [WorkspaceCard], excluding id: String,
                                 previous: WorkspaceGridSnapTarget?) -> WorkspaceGridSnapTarget? {
        guard point.x >= 0, point.x <= canvasWidth, point.y >= 0 else { return nil }
        let candidates = cards.filter { $0.isVisible && $0.id != id }.compactMap { card -> (WorkspaceCard, CGRect)? in
            guard let grid = card.grid else { return nil }
            let rect = frame(for: grid)
            return rect.insetBy(dx: -18, dy: -18).contains(point) ? (card, rect) : nil
        }
        // Prefer the card under the finger over an adjacent card's expanded edge.
        guard let (peer, rect) = candidates.min(by: { lhs, rhs in
            func distance(_ rect: CGRect) -> CGFloat {
                let dx = max(max(rect.minX - point.x, 0), point.x - rect.maxX)
                let dy = max(max(rect.minY - point.y, 0), point.y - rect.maxY)
                return dx * dx + dy * dy
            }
            return distance(lhs.1) < distance(rhs.1)
        }), let grid = peer.grid else { return nil }
        let distances: [(WorkspaceGridEdge, CGFloat)] = [
            (.top, abs(point.y - rect.minY)), (.bottom, abs(point.y - rect.maxY)),
            (.left, abs(point.x - rect.minX)), (.right, abs(point.x - rect.maxX))
        ]
        guard let closest = distances.min(by: { $0.1 < $1.1 }) else { return nil }
        var edge = closest.0
        if let previous, previous.primaryID == peer.id,
           let oldDistance = distances.first(where: { $0.0 == previous.edge })?.1,
           oldDistance <= closest.1 + 14 { edge = previous.edge }
        let coordinate: Int
        switch edge {
        case .top: coordinate = grid.row
        case .bottom: coordinate = grid.maxRow
        case .left: coordinate = grid.column
        case .right: coordinate = grid.maxColumn
        }
        return WorkspaceGridSnapTarget(edge: edge, coordinate: coordinate, peerIDs: [peer.id], primaryID: peer.id)
    }

    private func resizeSnapTarget(for rect: CGRect, in cards: [WorkspaceCard], excluding id: String) -> WorkspaceGridSnapTarget? {
        var best: WorkspaceGridSnapTarget?
        var bestDistance: CGFloat = 28
        func consider(_ edge: WorkspaceGridEdge, coordinate: Int, distance: CGFloat, peer: String?) {
            guard distance <= bestDistance else { return }
            bestDistance = distance
            best = WorkspaceGridSnapTarget(edge: edge, coordinate: coordinate,
                                           peerIDs: peer.map { [$0] } ?? [], primaryID: peer)
        }
        for card in cards where card.isVisible && card.id != id {
            guard let other = card.grid else { continue }
            if rect.maxY > CGFloat(other.row), rect.minY < CGFloat(other.maxRow), CGFloat(other.column) > rect.minX {
                consider(.left, coordinate: other.column,
                         distance: abs(rect.maxX - CGFloat(other.column)) * columnPitch, peer: card.id)
            }
            if rect.maxX > CGFloat(other.column), rect.minX < CGFloat(other.maxColumn), CGFloat(other.row) > rect.minY {
                consider(.top, coordinate: other.row,
                         distance: abs(rect.maxY - CGFloat(other.row)) * rowPitch, peer: card.id)
            }
        }
        consider(.right, coordinate: WorkspaceGridEngine.columnCount,
                 distance: abs(rect.maxX - CGFloat(WorkspaceGridEngine.columnCount)) * columnPitch, peer: nil)
        return best
    }

    private func drawPreview(_ preview: [WorkspaceCard], activeIDs: [String], snap: WorkspaceGridSnapTarget?) {
        let path = UIBezierPath()
        let targetPath = UIBezierPath()
        for card in preview where card.isVisible {
            guard let rect = card.grid else { continue }
            let outline = UIBezierPath(roundedRect: frame(for: rect), cornerRadius: 20)
            if activeIDs.contains(card.id) { targetPath.append(outline) }
            else if cards.first(where: { $0.id == card.id })?.grid != rect { path.append(outline) }
        }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        previewLayer.path = path.cgPath
        targetLayer.path = targetPath.cgPath
        let guide = UIBezierPath()
        if let snap {
            if let peerID = snap.primaryID, let rect = preview.first(where: { $0.id == peerID })?.grid {
                let peer = frame(for: rect)
                switch snap.edge {
                case .left, .right:
                    let x = snap.edge == .left ? peer.minX - gap / 2 : peer.maxX + gap / 2
                    guide.move(to: CGPoint(x: x, y: peer.minY))
                    guide.addLine(to: CGPoint(x: x, y: peer.maxY))
                case .top, .bottom:
                    let y = snap.edge == .top ? peer.minY - gap / 2 : peer.maxY + gap / 2
                    guide.move(to: CGPoint(x: peer.minX, y: y))
                    guide.addLine(to: CGPoint(x: peer.maxX, y: y))
                }
            } else if snap.edge == .left || snap.edge == .right {
                let x = margin + CGFloat(snap.coordinate) * columnPitch - gap / 2
                guide.move(to: CGPoint(x: x, y: scrollView.contentOffset.y + 8))
                guide.addLine(to: CGPoint(x: x, y: scrollView.contentOffset.y + scrollView.bounds.height - 8))
            } else {
                let y = margin + CGFloat(snap.coordinate) * rowPitch - gap / 2
                guide.move(to: CGPoint(x: margin, y: y))
                guide.addLine(to: CGPoint(x: canvasWidth - margin, y: y))
            }
        }
        guideLayer.path = guide.cgPath
        CATransaction.commit()
        if let interaction, case .move(let id) = interaction.operation,
           let rect = preview.first(where: { $0.id == id })?.grid {
            dropLabel.text = interaction.dropMessage ?? "松手放到这里"
            dropLabel.sizeToFit()
            let width = min(view.bounds.width - 16, dropLabel.bounds.width + 20)
            let target = canvas.convert(frame(for: rect), to: view)
            dropLabel.frame = CGRect(x: min(max(8, target.midX - width / 2), max(8, view.bounds.width - width - 8)),
                                     y: min(max(8, target.minY + 10), max(8, view.bounds.height - 36)),
                                     width: max(1, width), height: 28)
            dropLabel.isHidden = false
        } else { dropLabel.isHidden = true }
    }

    private func finishInteraction(commit: Bool) {
        guard let interaction else { return }
        self.interaction = nil
        displayLink?.invalidate()
        displayLink = nil
        displayLinkProxy = nil
        edgeHoverStartedAt = nil
        edgeHoverDirection = 0
        for (id, ghost) in interaction.ghosts {
            host(for: id)?.frame = ghost.frame
            ghost.removeFromSuperview()
        }
        hosts.values.forEach { $0.alpha = 1 }
        scrollView.isScrollEnabled = true
        previewLayer.path = nil
        targetLayer.path = nil
        guideLayer.path = nil
        dropLabel.isHidden = true
        if commit, let preview = interaction.preview {
            cards = preview
            settleHosts()
            if preview != interaction.initialCards { onCommit?(preview) }
        } else {
            cards = interaction.initialCards
            settleHosts()
        }
        if pendingUpdate {
            pendingUpdate = false
            constraints = makeConstraints(for: cards)
            cards = WorkspaceGridEngine.normalized(cards, constraints: constraints)
            installHosts()
            layoutHosts()
        }
        if let changedCard = changedInkCards.first { inkChanged(changedCard) }
    }

    private func settleHosts() {
        UIView.animate(withDuration: 0.26, delay: 0, usingSpringWithDamping: 0.9, initialSpringVelocity: 0,
                       options: [.beginFromCurrentState, .allowUserInteraction]) {
            self.layoutHosts()
        }
    }

    fileprivate func autoScroll(_ link: CADisplayLink) {
        guard let interaction, case .move = interaction.operation else { return }
        let elapsed = lastTimestamp == 0 ? 1.0 / 60.0 : min(link.timestamp - lastTimestamp, 0.05)
        lastTimestamp = link.timestamp
        let band: CGFloat = 28
        func speed(at coordinate: CGFloat, extent: CGFloat) -> CGFloat {
            if coordinate < band { return -260 * min(1, max(0, (band - coordinate) / band)) }
            if coordinate > extent - band { return 260 * min(1, max(0, (coordinate - extent + band) / band)) }
            return 0
        }
        let velocity = view.bounds.contains(lastViewportPoint) ? speed(at: lastViewportPoint.y, extent: view.bounds.height) : 0
        let direction: CGFloat = velocity == 0 ? 0 : (velocity < 0 ? -1 : 1)
        guard direction != 0 else { edgeHoverStartedAt = nil; edgeHoverDirection = 0; return }
        if edgeHoverDirection != direction || edgeHoverStartedAt == nil {
            edgeHoverDirection = direction; edgeHoverStartedAt = link.timestamp; return
        }
        guard link.timestamp - (edgeHoverStartedAt ?? link.timestamp) >= 0.35 else { return }
        let maximumY = max(0, scrollView.contentSize.height - scrollView.bounds.height)
        let next = CGPoint(x: 0, y: min(maximumY, max(0, scrollView.contentOffset.y + velocity * CGFloat(elapsed))))
        guard next != scrollView.contentOffset else { return }
        scrollView.setContentOffset(next, animated: false)
        updateInteraction(point: canvas.convert(lastViewportPoint, from: view))
    }
}

private enum WorkspaceCanvasOperation {
    case move(String), resize(String), split(WorkspaceGridSharedEdge), junction(WorkspaceGridJunction)
    var cardIDs: [String] {
        switch self {
        case .move(let id), .resize(let id): [id]
        case .split(let edge): Array(Set(edge.leadingIDs + edge.trailingIDs))
        case .junction(let junction): Array(junction.cardIDs)
        }
    }
}

private final class WorkspaceCanvasPan: UIPanGestureRecognizer {
    let operation: WorkspaceCanvasOperation
    init(operation: WorkspaceCanvasOperation, target: Any?, action: Selector?) {
        self.operation = operation
        super.init(target: target, action: action)
    }
}

private final class WorkspaceCanvasInteraction {
    let operation: WorkspaceCanvasOperation
    let initialCards: [WorkspaceCard]
    let startPoint: CGPoint
    let ghosts: [String: UIView]
    var preview: [WorkspaceCard]?
    var lastSnap: WorkspaceGridSnapTarget?
    var dropMessage: String?
    init(operation: WorkspaceCanvasOperation, initialCards: [WorkspaceCard], startPoint: CGPoint, ghosts: [String: UIView]) {
        self.operation = operation
        self.initialCards = initialCards
        self.startPoint = startPoint
        self.ghosts = ghosts
    }
}

@MainActor
private final class WorkspaceCanvasDisplayLinkProxy: NSObject {
    weak var owner: NativeWorkspaceCanvasController?
    init(owner: NativeWorkspaceCanvasController) { self.owner = owner }
    @objc func tick(_ link: CADisplayLink) { owner?.autoScroll(link) }
}

private final class WorkspaceCardHost: UIView {
    let controller: UIHostingController<AnyView>
    let grip = WorkspaceCardDragGrip()
    let resizeGrip = UIView()
    let ink: StockWorkspaceInkSurface
    private let gripMark = UIView()
    private let resizeMark = UIImageView(image: UIImage(systemName: "arrow.up.left.and.arrow.down.right"))
    private let contentInset: CGFloat
    private var editing = true
    private var frozenContent: UIView?

    static func headerInset(for kind: WorkspaceCardKind) -> CGFloat {
        switch kind {
        case .chart, .kline, .intraday, .klineChips: 24
        default: 16
        }
    }

    init(controller: UIHostingController<AnyView>, kind: WorkspaceCardKind, inkSettings: StockWorkspaceInkSettings) {
        self.controller = controller
        ink = StockWorkspaceInkSurface(settings: inkSettings)
        contentInset = WorkspaceCardHost.headerInset(for: kind)
        super.init(frame: .zero)
        backgroundColor = .white
        layer.cornerRadius = 20
        clipsToBounds = true
        controller.view.backgroundColor = .clear
        addSubview(controller.view)
        addSubview(grip)
        grip.accessibilityLabel = "拖动卡片顶部调整位置"
        grip.isAccessibilityElement = true
        gripMark.backgroundColor = UIColor.secondaryLabel.withAlphaComponent(0.28)
        gripMark.layer.cornerRadius = 2
        grip.addSubview(gripMark)
        addSubview(resizeGrip)
        resizeGrip.accessibilityLabel = "拖动调整卡片大小"
        resizeGrip.isAccessibilityElement = true
        resizeMark.tintColor = UIColor.secondaryLabel.withAlphaComponent(0.5)
        resizeMark.contentMode = .scaleAspectFit
        resizeGrip.addSubview(resizeMark)
        addSubview(ink)
        ink.onDrawingStateChanged = { [weak self] active in self?.freezeContent(active) }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    private func freezeContent(_ active: Bool) {
        if active {
            guard frozenContent == nil, controller.view.bounds.width > 1, controller.view.bounds.height > 1 else { return }
            let frozen: UIView
            if let snapshot = controller.view.snapshotView(afterScreenUpdates: false) {
                frozen = snapshot
            } else {
                let sourceSize = controller.view.bounds.size
                let scale = min(1, 1024 / max(sourceSize.width, sourceSize.height))
                let format = UIGraphicsImageRendererFormat(); format.scale = 1; format.opaque = true
                let size = CGSize(width: sourceSize.width * scale, height: sourceSize.height * scale)
                let image = UIGraphicsImageRenderer(size: size, format: format).image { context in
                    context.cgContext.scaleBy(x: scale, y: scale)
                    controller.view.layer.render(in: context.cgContext)
                }
                frozen = UIImageView(image: image)
            }
            frozen.frame = controller.view.frame
            frozen.isUserInteractionEnabled = false
            insertSubview(frozen, belowSubview: ink)
            frozenContent = frozen
        } else {
            frozenContent?.removeFromSuperview()
            frozenContent = nil
            setNeedsLayout()
            layoutIfNeeded()
        }
    }

    func setEditing(_ editing: Bool) {
        guard editing != self.editing else { return }
        self.editing = editing
        grip.isHidden = !editing
        resizeGrip.isHidden = !editing
        setNeedsLayout()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        // A shallow, wide handle leaves room for content without covering chart controls.
        // Locking the layout also removes the now-unused handle band.
        let headerHeight: CGFloat = editing ? contentInset : 0
        controller.view.frame = CGRect(x: 0, y: headerHeight, width: bounds.width,
                                       height: max(1, bounds.height - headerHeight))
        if frozenContent == nil { ink.layoutInContentFrame(controller.view.frame) }
        // Data cards already have top padding: the extra touch area only covers that padding.
        grip.frame = CGRect(x: 16, y: 0, width: max(0, bounds.width - 32), height: editing ? 24 : 0)
        gripMark.frame = CGRect(x: (grip.bounds.width - 30) / 2, y: contentInset == 16 ? 5 : 8, width: 30, height: 4)
        resizeGrip.frame = CGRect(x: max(0, bounds.width - 44), y: max(0, bounds.height - 44), width: 44, height: 44)
        resizeMark.frame = CGRect(x: 20, y: 20, width: 14, height: 14)
    }
}

/// Only the top handle moves a card; Pencil input passes through to its ink layer.
private final class WorkspaceCardDragGrip: UIView {
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        if event?.allTouches?.contains(where: { $0.type == .pencil }) == true { return false }
        return super.point(inside: point, with: event)
    }
}

private final class WorkspaceJunctionView: UIView {
    private let mark = UIImageView(image: UIImage(systemName: "arrow.up.and.down.and.arrow.left.and.right"))

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .clear
        mark.tintColor = UIColor.secondaryLabel.withAlphaComponent(0.5)
        mark.contentMode = .scaleAspectFit
        mark.isUserInteractionEnabled = false
        addSubview(mark)
        isAccessibilityElement = true
        accessibilityLabel = "向任意方向拖动，同时调整相邻卡片大小"
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        if event?.allTouches?.contains(where: { $0.type == .pencil }) == true { return false }
        return super.point(inside: point, with: event)
    }
    func setMarkVisible(_ visible: Bool) { mark.alpha = visible ? 1 : 0 }

    override func layoutSubviews() {
        super.layoutSubviews()
        mark.frame = CGRect(x: (bounds.width - 16) / 2, y: (bounds.height - 16) / 2, width: 16, height: 16)
    }
}

private final class WorkspaceSplitterView: UIView {
    private let vertical: Bool
    private let line = UIView()
    init(vertical: Bool) {
        self.vertical = vertical
        super.init(frame: .zero)
        backgroundColor = .clear
        line.backgroundColor = UIColor.secondaryLabel.withAlphaComponent(0.20)
        line.layer.cornerRadius = 1
        line.isUserInteractionEnabled = false
        addSubview(line)
        isAccessibilityElement = true
        accessibilityLabel = vertical ? "拖动调整左右卡片比例" : "拖动调整上下卡片比例"
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        if event?.allTouches?.contains(where: { $0.type == .pencil }) == true { return false }
        return super.point(inside: point, with: event)
    }
    func setLineVisible(_ visible: Bool) { line.alpha = visible ? 1 : 0 }
    override func layoutSubviews() {
        super.layoutSubviews()
        if vertical { line.frame = CGRect(x: (bounds.width - 2) / 2, y: max(0, (bounds.height - 28) / 2), width: 2, height: min(28, bounds.height)) }
        else { line.frame = CGRect(x: max(0, (bounds.width - 28) / 2), y: (bounds.height - 2) / 2, width: min(28, bounds.width), height: 2) }
    }
}

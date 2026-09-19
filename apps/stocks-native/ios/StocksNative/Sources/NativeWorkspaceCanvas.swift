import SwiftUI
import UIKit

/// The scroll view owns positions, while stable hosting controllers own each chart's state.
struct NativeWorkspaceCanvas: UIViewControllerRepresentable {
    let page: WorkspacePage
    var isEditing = true
    let content: (WorkspaceCard) -> AnyView
    let onCommit: ([WorkspaceCard]) -> Void

    func makeUIViewController(context: Context) -> NativeWorkspaceCanvasController {
        let controller = NativeWorkspaceCanvasController()
        controller.update(page: page, layoutEditing: isEditing, content: content, onCommit: onCommit)
        return controller
    }

    func updateUIViewController(_ controller: NativeWorkspaceCanvasController, context: Context) {
        controller.update(page: page, layoutEditing: isEditing, content: content, onCommit: onCommit)
    }
}

@MainActor
final class NativeWorkspaceCanvasController: UIViewController, UIGestureRecognizerDelegate {
    private let scrollView = UIScrollView()
    private let canvas = UIView()
    private let previewLayer = CAShapeLayer()
    private let targetLayer = CAShapeLayer()
    private let guideLayer = CAShapeLayer()
    private var hosts: [String: WorkspaceCardHost] = [:]
    private var splitters: [UIView] = []
    private var page: WorkspacePage?
    private var cards: [WorkspaceCard] = []
    private var layoutEditing = true
    private var content: ((WorkspaceCard) -> AnyView)?
    private var onCommit: (([WorkspaceCard]) -> Void)?
    private var interaction: WorkspaceCanvasInteraction?
    private var displayLink: CADisplayLink?
    private var displayLinkProxy: WorkspaceCanvasDisplayLinkProxy?
    private var lastTimestamp: CFTimeInterval = 0
    private var lastViewportPoint = CGPoint.zero
    private var pendingUpdate = false
    private var laidOutWidth: CGFloat = 0
    private let margin: CGFloat = 18
    private let minimumCanvasWidth: CGFloat = 720

    private var gap: CGFloat { WorkspaceGridEngine.gap }
    private var rowPitch: CGFloat { WorkspaceGridEngine.rowHeight + gap }
    private var canvasWidth: CGFloat { max(minimumCanvasWidth, scrollView.bounds.width) }
    private var columnPitch: CGFloat { (canvasWidth - margin * 2 + gap) / CGFloat(WorkspaceGridEngine.columnCount) }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(AppStyle.canvas)
        scrollView.backgroundColor = .clear
        scrollView.alwaysBounceVertical = true
        scrollView.alwaysBounceHorizontal = false
        scrollView.isDirectionalLockEnabled = false
        scrollView.contentInsetAdjustmentBehavior = .never
        scrollView.keyboardDismissMode = .onDrag
        view.addSubview(scrollView)
        canvas.backgroundColor = .clear
        scrollView.addSubview(canvas)
        previewLayer.fillColor = UIColor.clear.cgColor
        previewLayer.strokeColor = UIColor(AppStyle.accent).withAlphaComponent(0.25).cgColor
        previewLayer.lineWidth = 1
        targetLayer.fillColor = UIColor(AppStyle.accent).withAlphaComponent(0.09).cgColor
        targetLayer.strokeColor = UIColor(AppStyle.accent).cgColor
        targetLayer.lineWidth = 2
        targetLayer.lineDashPattern = [5, 4]
        guideLayer.strokeColor = UIColor(AppStyle.accent).cgColor
        guideLayer.lineWidth = 2
        guideLayer.fillColor = UIColor.clear.cgColor
        canvas.layer.addSublayer(previewLayer)
        canvas.layer.addSublayer(targetLayer)
        canvas.layer.addSublayer(guideLayer)
        installHosts()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        scrollView.frame = view.bounds
        if abs(laidOutWidth - canvasWidth) > 0.5 {
            if interaction != nil { finishInteraction(commit: false) }
            laidOutWidth = canvasWidth
            layoutHosts()
        } else if interaction == nil {
            updateContentSize()
        }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        finishInteraction(commit: false)
    }

    deinit { displayLink?.invalidate() }

    func update(page: WorkspacePage, layoutEditing: Bool, content: @escaping (WorkspaceCard) -> AnyView,
                onCommit: @escaping ([WorkspaceCard]) -> Void) {
        let changedPage = self.page?.id != page.id
        if interaction != nil, changedPage || !layoutEditing { finishInteraction(commit: false) }
        self.page = page
        self.layoutEditing = layoutEditing
        self.content = content
        self.onCommit = onCommit
        if interaction != nil {
            // Market updates continue inside observed SwiftUI cards; replacing roots while
            // dragging would unnecessarily rebuild expensive chart descriptions every frame.
            pendingUpdate = true
            return
        }
        cards = WorkspaceGridEngine.normalized(page.cards)
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
                addChild(controller)
                let host = WorkspaceCardHost(controller: controller)
                host.accessibilityIdentifier = "workspace.card.\(page.id).\(card.id)"
                canvas.addSubview(host)
                controller.didMove(toParent: self)
                let move = makePan(.move(card.id))
                host.grip.addGestureRecognizer(move)
                let resize = makePan(.resize(card.id))
                host.resizeGrip.addGestureRecognizer(resize)
                host.setEditing(layoutEditing)
                hosts[key] = host
            }
        }
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
        for edge in WorkspaceGridEngine.sharedEdges(cards) {
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
                           shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool {
        // The dedicated handle wins as soon as it begins, when scrolling is disabled.
        // No retained failure dependencies are added for transient shared-edge handles.
        otherGestureRecognizer === scrollView.panGestureRecognizer
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
    }

    private func updateInteraction(point: CGPoint) {
        guard let interaction else { return }
        let delta = CGPoint(x: point.x - interaction.startPoint.x, y: point.y - interaction.startPoint.y)
        var preview = interaction.initialCards
        var snap: WorkspaceGridSnapTarget?
        switch interaction.operation {
        case .move(let id):
            guard let original = interaction.initialCards.first(where: { $0.id == id })?.grid else { return }
            let start = frame(for: original)
            let proposed = start.offsetBy(dx: delta.x, dy: delta.y)
            interaction.ghosts[id]?.frame = proposed
            let grid = continuousGrid(proposed)
            snap = WorkspaceGridEngine.snapTarget(for: grid, in: interaction.initialCards, excluding: id,
                                                 columnTolerance: Double(28 / columnPitch), rowTolerance: Double(28 / rowPitch))
            if let snap, snap.primaryID != nil {
                // Dock against the original peer coordinates. Moving first would push
                // an overlapping peer away before its edge can be used as the anchor.
                preview = WorkspaceGridEngine.dock(interaction.initialCards, id: id, target: snap)
            } else {
                var proposedRect = roundedGrid(grid)
                if let snap {
                    if snap.edge == .left { proposedRect.column = 0 }
                    else if snap.edge == .right { proposedRect.column = WorkspaceGridEngine.columnCount - proposedRect.width }
                    else if snap.edge == .top { proposedRect.row = 0 }
                }
                preview = WorkspaceGridEngine.move(interaction.initialCards, id: id, to: proposedRect)
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
            preview = WorkspaceGridEngine.resize(interaction.initialCards, id: id, to: roundedGrid(grid))
            if let resolved = preview.first(where: { $0.id == id })?.grid { interaction.ghosts[id]?.frame = frame(for: resolved) }
        case .split(let edge):
            let shift = edge.axis == .vertical ? delta.x / columnPitch : delta.y / rowPitch
            preview = WorkspaceGridEngine.resizeSharedEdge(interaction.initialCards, edge: edge,
                                                           to: edge.coordinate + Int(shift.rounded()))
            for id in edge.leadingIDs + edge.trailingIDs {
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
            if snap.edge == .left || snap.edge == .right {
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
    }

    private func finishInteraction(commit: Bool) {
        guard let interaction else { return }
        self.interaction = nil
        displayLink?.invalidate()
        displayLink = nil
        displayLinkProxy = nil
        for (id, ghost) in interaction.ghosts {
            host(for: id)?.frame = ghost.frame
            ghost.removeFromSuperview()
        }
        hosts.values.forEach { $0.alpha = 1 }
        scrollView.isScrollEnabled = true
        previewLayer.path = nil
        targetLayer.path = nil
        guideLayer.path = nil
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
            installHosts()
        }
    }

    private func settleHosts() {
        UIView.animate(withDuration: 0.26, delay: 0, usingSpringWithDamping: 0.9, initialSpringVelocity: 0,
                       options: [.beginFromCurrentState, .allowUserInteraction]) {
            self.layoutHosts()
        }
    }

    fileprivate func autoScroll(_ link: CADisplayLink) {
        guard interaction != nil else { return }
        let elapsed = lastTimestamp == 0 ? 1.0 / 60.0 : min(link.timestamp - lastTimestamp, 0.05)
        lastTimestamp = link.timestamp
        let band: CGFloat = 64
        func speed(at coordinate: CGFloat, extent: CGFloat) -> CGFloat {
            if coordinate < band { return -420 * min(1, max(0, (band - coordinate) / band)) }
            if coordinate > extent - band { return 420 * min(1, max(0, (coordinate - extent + band) / band)) }
            return 0
        }
        let velocity = CGPoint(x: speed(at: lastViewportPoint.x, extent: view.bounds.width),
                               y: speed(at: lastViewportPoint.y, extent: view.bounds.height))
        guard velocity != .zero else { return }
        let maximumX = max(0, scrollView.contentSize.width - scrollView.bounds.width)
        let maximumY = max(0, scrollView.contentSize.height - scrollView.bounds.height)
        let next = CGPoint(x: min(maximumX, max(0, scrollView.contentOffset.x + velocity.x * CGFloat(elapsed))),
                           y: min(maximumY, max(0, scrollView.contentOffset.y + velocity.y * CGFloat(elapsed))))
        guard next != scrollView.contentOffset else { return }
        scrollView.setContentOffset(next, animated: false)
        updateInteraction(point: canvas.convert(lastViewportPoint, from: view))
    }
}

private enum WorkspaceCanvasOperation {
    case move(String), resize(String), split(WorkspaceGridSharedEdge)
    var cardIDs: [String] {
        switch self {
        case .move(let id), .resize(let id): [id]
        case .split(let edge): Array(Set(edge.leadingIDs + edge.trailingIDs))
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
    let grip = UIView()
    let resizeGrip = UIView()
    private let gripMark = UIView()
    private let resizeMark = UIImageView(image: UIImage(systemName: "arrow.up.left.and.arrow.down.right"))
    private var editing = true

    init(controller: UIHostingController<AnyView>) {
        self.controller = controller
        super.init(frame: .zero)
        backgroundColor = .white
        layer.cornerRadius = 20
        clipsToBounds = true
        controller.view.backgroundColor = .clear
        addSubview(controller.view)
        addSubview(grip)
        grip.accessibilityLabel = "拖动移动卡片"
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
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func setEditing(_ editing: Bool) {
        guard editing != self.editing else { return }
        self.editing = editing
        grip.isHidden = !editing
        resizeGrip.isHidden = !editing
        setNeedsLayout()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        let headerHeight: CGFloat = 28
        controller.view.frame = CGRect(x: 0, y: headerHeight, width: bounds.width,
                                       height: max(1, bounds.height - headerHeight))
        grip.frame = CGRect(x: max(0, (bounds.width - 96) / 2), y: 0, width: min(96, bounds.width), height: 44)
        gripMark.frame = CGRect(x: (grip.bounds.width - 30) / 2, y: 11, width: 30, height: 4)
        resizeGrip.frame = CGRect(x: max(0, bounds.width - 44), y: max(0, bounds.height - 44), width: 44, height: 44)
        resizeMark.frame = CGRect(x: 20, y: 20, width: 14, height: 14)
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
    func setLineVisible(_ visible: Bool) { line.alpha = visible ? 1 : 0 }
    override func layoutSubviews() {
        super.layoutSubviews()
        if vertical { line.frame = CGRect(x: (bounds.width - 2) / 2, y: max(0, (bounds.height - 28) / 2), width: 2, height: min(28, bounds.height)) }
        else { line.frame = CGRect(x: max(0, (bounds.width - 28) / 2), y: (bounds.height - 2) / 2, width: min(28, bounds.width), height: 2) }
    }
}

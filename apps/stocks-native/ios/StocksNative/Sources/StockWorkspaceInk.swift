import Combine
import CryptoKit
import PencilKit
import SwiftUI
import UIKit

struct StockWorkspaceInkContext: Equatable {
    let stockCode: String
    let scopeID: String
    var sourceTime: String? = nil
    var cardScopeIDs: [String: String] = [:]
}

struct StockWorkspaceInkRect: Encodable {
    let x: CGFloat
    let y: CGFloat
    let width: CGFloat
    let height: CGFloat

    init(_ rect: CGRect) {
        x = rect.minX; y = rect.minY; width = rect.width; height = rect.height
    }
}

/// Bounds use the card content's local points, before image downsampling.
struct StockWorkspaceInkSnapshot: Encodable {
    let id: String
    let stockCode: String
    let scopeID: String
    let sourceTime: String?
    let capturedAt: Date
    let cardIDs: [String]
    let inkCardIDs: [String]
    let bounds: [String: StockWorkspaceInkRect]
    let jpegBase64: String?
    let cleared: Bool
}

@MainActor
final class StockWorkspaceInkSettings: ObservableObject {
    enum Mode: String { case pen, selection, eraser }
    @Published var mode: Mode = .pen
    @Published var color: Color = .red
    @Published var width: Double = 3
    @Published var storageError: String?
    weak var activeSurface: StockWorkspaceInkSurface?
    private var previousMode: Mode = .pen
    private var lastDoubleTap: TimeInterval = 0

    func toggleEraser() {
        if mode == .eraser { mode = previousMode }
        else { previousMode = mode; mode = .eraser }
    }

    func toggleSelection() {
        mode = mode == .selection ? .pen : .selection
        previousMode = mode
    }

    func pencilDoubleTap(from surface: StockWorkspaceInkSurface) {
        guard activeSurface === surface else { return }
        let now = ProcessInfo.processInfo.systemUptime
        guard now - lastDoubleTap > 0.25 else { return }
        lastDoubleTap = now
        toggleEraser()
    }
}

struct StockWorkspaceInkPalette: View {
    @ObservedObject var settings: StockWorkspaceInkSettings

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("手写设置").font(.headline)
            HStack(spacing: 10) {
                Button { settings.toggleSelection() } label: {
                    Label(settings.mode == .selection ? "选区笔" : "普通笔",
                          systemImage: settings.mode == .selection ? "lasso" : "pencil.tip")
                }
                .buttonStyle(.borderedProminent)
                Button { settings.toggleEraser() } label: {
                    Image(systemName: settings.mode == .eraser ? "eraser.fill" : "eraser")
                }
                .buttonStyle(.bordered)
                .accessibilityLabel("切换橡皮擦")
            }
            ColorPicker("颜色", selection: $settings.color, supportsOpacity: false)
            HStack {
                Text("粗细")
                Slider(value: $settings.width, in: 1...12, step: 0.5)
                Text(String(format: "%.1f", settings.width)).monospacedDigit().frame(width: 30)
            }
            Text("落笔即可写；双击笔身切换橡皮，长按笔尖打开设置。选区笔圈出需要讨论的内容。")
                .font(.caption).foregroundStyle(.secondary)
            if let error = settings.storageError {
                Text(error).font(.caption).foregroundStyle(.red)
            }
        }
        .padding(20).frame(width: 310)
    }
}

/// Apple Pencil alone participates in hit testing; fingers reach the card/chart below.
private final class StockPencilCanvas: PKCanvasView {
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        guard super.point(inside: point, with: event) else { return false }
        return event?.type == .hover || event?.allTouches?.contains(where: { $0.type == .pencil }) == true
    }
}

@MainActor
final class StockWorkspaceInkSurface: UIView, PKCanvasViewDelegate, UIPencilInteractionDelegate, UIGestureRecognizerDelegate {
    private let pencil = StockPencilCanvas()
    private let selectionLayer = CAShapeLayer()
    private let settings: StockWorkspaceInkSettings
    private var settingsObservation: AnyCancellable?
    private var scope: String?
    private var appliedKey: String?
    private var applying = false
    private var drawingActive = false
    private var awaitingToolCommit = false
    private var strokeStart = PKDrawing()
    private var selectionPoints: [CGPoint] = []
    private var selectionBounds = CGRect.null
    private var pendingChange: DispatchWorkItem?
    private lazy var selectionGesture = UIPanGestureRecognizer(target: self, action: #selector(selectRegion(_:)))
    var onChanged: (() -> Void)?
    var onDrawingStateChanged: ((Bool) -> Void)?
    var onShowSettings: ((UIView, CGPoint) -> Void)?

    init(settings: StockWorkspaceInkSettings) {
        self.settings = settings
        super.init(frame: .zero)
        backgroundColor = .clear
        isHidden = true
        pencil.backgroundColor = .clear
        pencil.isOpaque = false
        pencil.isScrollEnabled = false
        pencil.drawingPolicy = .pencilOnly
        pencil.delegate = self
        addSubview(pencil)
        selectionLayer.strokeColor = UIColor.systemBlue.cgColor
        selectionLayer.fillColor = UIColor.systemBlue.withAlphaComponent(0.08).cgColor
        selectionLayer.lineWidth = 2
        selectionLayer.lineDashPattern = [5, 4]
        pencil.layer.addSublayer(selectionLayer)
        selectionGesture.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.pencil.rawValue)]
        selectionGesture.maximumNumberOfTouches = 1
        selectionGesture.delegate = self
        pencil.addGestureRecognizer(selectionGesture)
        let hold = UILongPressGestureRecognizer(target: self, action: #selector(showSettings(_:)))
        hold.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.pencil.rawValue)]
        hold.minimumPressDuration = 0.65
        hold.allowableMovement = 6
        hold.delegate = self
        pencil.addGestureRecognizer(hold)
        let hover = UIHoverGestureRecognizer(target: self, action: #selector(pencilHovered(_:)))
        hover.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.pencil.rawValue)]
        pencil.addGestureRecognizer(hover)
        let interaction = UIPencilInteraction()
        interaction.delegate = self
        pencil.addInteraction(interaction)
        settingsObservation = settings.objectWillChange.sink { [weak self] _ in
            DispatchQueue.main.async { [weak self] in self?.applyTool() }
        }
        applyTool()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        guard super.point(inside: point, with: event) else { return false }
        return event?.type == .hover || event?.allTouches?.contains(where: { $0.type == .pencil }) == true
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        // Keep the same screen coordinate system until PencilKit commits the stroke.
        guard !isDrawing else { return }
        pencil.frame = bounds
        selectionLayer.frame = bounds
        applyIdentity()
    }

    func configure(scope: String?) {
        guard self.scope != scope else { return }
        self.scope = scope
        applyIdentity()
    }

    private func applyIdentity() {
        // Screen-space ink is intentionally isolated by viewport AND content size.
        // A new chart range or resized card never reinterprets old points as data anchors.
        let key = scope.map { "\($0)|\(Int(bounds.width.rounded()))x\(Int(bounds.height.rounded()))" }
        guard bounds.width > 1, bounds.height > 1, appliedKey != key else { return }
        // The host freezes the visible card while the Pencil is down. Defer a new
        // price domain / stock / viewport until the complete old stroke is saved.
        guard !isDrawing else { return }
        save()
        pendingChange?.cancel(); pendingChange = nil
        drawingActive = false
        awaitingToolCommit = false
        appliedKey = key
        applying = true
        pencil.drawingGestureRecognizer.isEnabled = false
        pencil.drawing = key.flatMap { StockWorkspaceInkStore.load($0) } ?? PKDrawing()
        clearSelection()
        applying = false
        isHidden = key == nil
        applyTool()
        if key != nil { scheduleChange(saveDrawing: false) }
    }

    func save() {
        guard let appliedKey, !applying else { return }
        do { try StockWorkspaceInkStore.save(pencil.drawing, key: appliedKey) }
        catch { settings.storageError = "笔迹未保存：\(error.localizedDescription)" }
    }

    var inkBounds: CGRect {
        let drawingBounds = pencil.drawing.strokes.isEmpty ? CGRect.null : pencil.drawing.bounds
        let combined = drawingBounds.union(selectionBounds).intersection(bounds)
        return combined.isNull || combined.isEmpty ? .null : combined
    }

    var hasInk: Bool { !inkBounds.isNull }
    var isDrawing: Bool {
        drawingActive || awaitingToolCommit || selectionGesture.state == .began || selectionGesture.state == .changed
    }

    func renderInk(in rect: CGRect) {
        if !pencil.drawing.strokes.isEmpty {
            pencil.drawing.image(from: bounds, scale: 1).draw(in: rect)
        }
        if let path = selectionLayer.path, let context = UIGraphicsGetCurrentContext() {
            context.saveGState()
            context.translateBy(x: rect.minX, y: rect.minY)
            context.scaleBy(x: rect.width / max(1, bounds.width), y: rect.height / max(1, bounds.height))
            context.addPath(path)
            context.setStrokeColor(UIColor.systemBlue.cgColor)
            context.setFillColor(UIColor.systemBlue.withAlphaComponent(0.08).cgColor)
            context.setLineWidth(2)
            context.setLineDash(phase: 0, lengths: [5, 4])
            context.drawPath(using: .fillStroke)
            context.restoreGState()
        }
    }

    private func applyTool() {
        guard !applying else { return }
        switch settings.mode {
        case .pen: pencil.tool = PKInkingTool(.pen, color: UIColor(settings.color), width: CGFloat(settings.width))
        case .eraser: pencil.tool = PKEraserTool(.vector)
        case .selection: pencil.tool = PKInkingTool(.pen, color: .clear, width: 1)
        }
        pencil.drawingGestureRecognizer.isEnabled = settings.mode != .selection && appliedKey != nil
        selectionGesture.isEnabled = settings.mode == .selection && appliedKey != nil
    }

    func canvasViewDidBeginUsingTool(_ canvasView: PKCanvasView) {
        guard !applying else { return }
        pendingChange?.cancel()
        settings.activeSurface = self
        drawingActive = true
        awaitingToolCommit = true
        strokeStart = canvasView.drawing
        clearSelection()
        onDrawingStateChanged?(true)
    }

    func canvasViewDidEndUsingTool(_ canvasView: PKCanvasView) {
        guard !applying, awaitingToolCommit else { return }
        drawingActive = false
        scheduleChange()
    }

    func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
        if !applying && !drawingActive && awaitingToolCommit { scheduleChange() }
    }

    private func scheduleChange(saveDrawing: Bool = true) {
        guard !applying, appliedKey != nil else { return }
        pendingChange?.cancel()
        let key = appliedKey
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.drawingActive, self.appliedKey == key else { return }
            self.awaitingToolCommit = false
            if saveDrawing { self.save() }
            self.onDrawingStateChanged?(false)
            self.setNeedsLayout()
            self.layoutIfNeeded()
            self.applyIdentity()
            // If a deferred identity was installed, its own stable callback wins.
            if self.appliedKey == key { self.onChanged?() }
        }
        pendingChange = work
        // PencilKit may send its final drawing update just after the tool-end callback.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.08, execute: work)
    }

    func pencilInteractionDidTap(_ interaction: UIPencilInteraction) { settings.pencilDoubleTap(from: self) }

    @available(iOS 17.5, *)
    func pencilInteraction(_ interaction: UIPencilInteraction, didReceiveTap tap: UIPencilInteraction.Tap) {
        settings.pencilDoubleTap(from: self)
    }

    @objc private func pencilHovered(_ hover: UIHoverGestureRecognizer) {
        if hover.state == .began || hover.state == .changed { settings.activeSurface = self }
    }

    @objc private func showSettings(_ hold: UILongPressGestureRecognizer) {
        guard hold.state == .began else { return }
        settings.activeSurface = self
        let wasDrawing = drawingActive
        applying = true
        pencil.drawingGestureRecognizer.isEnabled = false
        if wasDrawing { pencil.drawing = strokeStart }
        drawingActive = false
        awaitingToolCommit = false
        pendingChange?.cancel()
        applying = false
        save()
        onDrawingStateChanged?(false)
        applyIdentity()
        applyTool()
        onShowSettings?(self, hold.location(in: self))
    }

    @objc private func selectRegion(_ gesture: UIPanGestureRecognizer) {
        let point = gesture.location(in: pencil)
        switch gesture.state {
        case .began:
            pendingChange?.cancel()
            settings.activeSurface = self
            selectionPoints = [point]
            onDrawingStateChanged?(true)
        case .changed:
            if selectionPoints.count < 2048 { selectionPoints.append(point) }
        case .ended:
            awaitingToolCommit = true
            selectionPoints.append(point)
        case .cancelled, .failed:
            awaitingToolCommit = true
            clearSelection()
            scheduleChange()
            return
        default: return
        }
        let path = UIBezierPath()
        if let first = selectionPoints.first {
            path.move(to: first)
            selectionPoints.dropFirst().forEach { path.addLine(to: $0) }
            if gesture.state == .ended { path.close() }
        }
        selectionLayer.path = path.cgPath
        selectionBounds = path.bounds.intersection(bounds)
        if gesture.state == .ended {
            if selectionBounds.width < 8 || selectionBounds.height < 8 { clearSelection() }
            scheduleChange()
        }
    }

    private func clearSelection() {
        selectionPoints.removeAll(keepingCapacity: true)
        selectionLayer.path = nil
        selectionBounds = .null
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                           shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool {
        gestureRecognizer is UILongPressGestureRecognizer || otherGestureRecognizer is UILongPressGestureRecognizer
    }
}

private enum StockWorkspaceInkStore {
    private static var directory: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("StocksWorkspaceInk-v1", isDirectory: true)
    }

    private static func url(_ key: String) -> URL {
        let name = SHA256.hash(data: Data(key.utf8)).map { String(format: "%02x", $0) }.joined()
        return directory.appendingPathComponent(name).appendingPathExtension("drawing")
    }

    static func load(_ key: String) -> PKDrawing? {
        let file = url(key)
        guard let size = try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize,
              size <= 4 * 1024 * 1024, let data = try? Data(contentsOf: file) else { return nil }
        return try? PKDrawing(data: data)
    }

    static func save(_ drawing: PKDrawing, key: String) throws {
        let file = url(key)
        if drawing.strokes.isEmpty {
            if FileManager.default.fileExists(atPath: file.path) { try FileManager.default.removeItem(at: file) }
            return
        }
        let data = drawing.dataRepresentation()
        guard data.count <= 4 * 1024 * 1024 else {
            throw NSError(domain: "StocksInk", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "此窗口笔迹过多，请擦除部分笔迹后重试。"])
        }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var folder = directory
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try? folder.setResourceValues(values)
        try data.write(to: file, options: [.atomic, .completeFileProtectionUnlessOpen])
        prune()
    }

    private static func prune() {
        let keys: Set<URLResourceKey> = [.contentModificationDateKey, .fileSizeKey]
        guard let files = try? FileManager.default.contentsOfDirectory(at: directory,
                    includingPropertiesForKeys: Array(keys), options: [.skipsHiddenFiles]) else { return }
        let entries = files.compactMap { file -> (URL, Date, Int)? in
            guard file.pathExtension == "drawing", let values = try? file.resourceValues(forKeys: keys) else { return nil }
            return (file, values.contentModificationDate ?? .distantPast, values.fileSize ?? 0)
        }.sorted { $0.1 > $1.1 }
        var total = 0
        for (index, entry) in entries.enumerated() {
            total += entry.2
            if index >= 120 || total > 32 * 1024 * 1024 || entry.1 < Date().addingTimeInterval(-90 * 86400) {
                try? FileManager.default.removeItem(at: entry.0)
            }
        }
    }
}

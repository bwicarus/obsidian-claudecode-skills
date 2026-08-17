import Foundation
import PencilKit
import QuartzCore
import SwiftUI
import UIKit

struct NativeInkSurface: Equatable {
    let id: String
    let rect: CGRect
    let exclusions: [CGRect]

    func contains(_ point: CGPoint) -> Bool {
        rect.contains(point) && !exclusions.contains(where: { $0.contains(point) })
    }

    func canonicalPoint(_ point: CGPoint) -> [CGFloat]? {
        guard contains(point), rect.width > 0, rect.height > 0 else {
            return nil
        }
        return [
            (point.x - rect.minX) / rect.width,
            (point.y - rect.minY) / rect.height,
        ]
    }
}

struct NativeInkLayout: Equatable {
    static let empty = NativeInkLayout(documentToken: nil, surfaces: [])

    let documentToken: String?
    let surfaces: [NativeInkSurface]

    func surface(at point: CGPoint) -> NativeInkSurface? {
        surfaces.last(where: { $0.contains(point) })
    }
}

@MainActor
final class NativePencilInkController: ObservableObject {
    enum Tool {
        case pen
        case eraser
        case selection
    }

    @Published private(set) var tool: Tool = .pen
    @Published private(set) var layout = NativeInkLayout.empty
    @Published private(set) var lastError: String?
    @Published private(set) var retryRequest = 0
    @Published private(set) var documentGeneration = 0
    @Published private(set) var pendingOperationCount = 0
    @Published private(set) var paletteVisible = false
    @Published var colorHex = "#ff3b30"
    @Published var width: CGFloat = 4
    @Published private(set) var paletteAnchor: CGPoint?
    @Published private(set) var recentPencilAnchor: CGPoint?
    private var previousDrawingTool: Tool = .pen

    var canDraw: Bool { !layout.surfaces.isEmpty }
    var hasPendingOperations: Bool { pendingOperationCount > 0 }

    func toggleEraser() {
        if tool == .eraser {
            tool = previousDrawingTool == .eraser ? .pen : previousDrawingTool
        } else {
            previousDrawingTool = tool
            tool = .eraser
        }
    }

    func toggleSelection() {
        tool = tool == .selection ? .pen : .selection
        previousDrawingTool = tool
    }

    func select(_ value: Tool) {
        tool = value
        if value != .eraser { previousDrawingTool = value }
    }

    func showPalette() {
        if paletteVisible {
            paletteVisible = false
            return
        }
        paletteAnchor = recentPencilAnchor
            ?? NativePencilSettings.shared.launcherAnchor
        paletteVisible = true
    }

    func updateRecentPencilAnchor(_ point: CGPoint, in bounds: CGRect) {
        guard bounds.width > 0, bounds.height > 0 else { return }
        recentPencilAnchor = CGPoint(
            x: min(1, max(0, point.x / bounds.width)),
            y: min(1, max(0, point.y / bounds.height))
        )
    }

    func report(_ error: Error) {
        lastError = error.localizedDescription
    }

    func retry() {
        lastError = nil
        retryRequest &+= 1
    }

    func clearError() {
        lastError = nil
    }

    func updatePendingOperationCount(_ count: Int) {
        pendingOperationCount = max(0, count)
    }

    func reportNavigationBlocked() {
        lastError = "笔迹正在保存，请稍后再切换书籍"
    }

    func reportAbandonedOperations(_ count: Int) {
        lastError = "页面已切换，\(count) 组未确认笔迹未能写入；请返回原页重画"
    }

    func invalidateDocument() {
        layout = .empty
        documentGeneration &+= 1
        lastError = nil
    }

    func updateLayout(from body: [String: Any]) {
        guard
            body["type"] as? String == "layout",
            let documentToken = body["documentToken"] as? String,
            !documentToken.isEmpty,
            documentToken.count <= 96
        else {
            return
        }
        let rawSurfaces = body["surfaces"] as? [[String: Any]] ?? []
        if layout.documentToken != documentToken {
            documentGeneration &+= 1
            lastError = nil
        }
        layout = NativeInkLayout(
            documentToken: documentToken,
            surfaces: rawSurfaces.compactMap { raw in
            guard
                let id = raw["id"] as? String,
                !id.isEmpty,
                let rect = Self.rect(from: raw["rect"])
            else {
                return nil
            }
            let exclusions = (raw["exclusions"] as? [[String: Any]] ?? [])
                .compactMap { Self.rect(from: $0) }
            return NativeInkSurface(
                id: id,
                rect: rect,
                exclusions: exclusions
            )
        })
    }

    private static func rect(from value: Any?) -> CGRect? {
        guard let raw = value as? [String: Any] else { return nil }
        func number(_ key: String) -> CGFloat? {
            if let value = raw[key] as? NSNumber {
                return CGFloat(truncating: value)
            }
            return nil
        }
        guard
            let x = number("x"),
            let y = number("y"),
            let width = number("width"),
            let height = number("height"),
            width > 0,
            height > 0
        else {
            return nil
        }
        return CGRect(x: x, y: y, width: width, height: height)
    }
}

private struct NativeInkSegment {
    let surfaceId: String
    let color: String?
    let width: CGFloat?
    let points: [[CGFloat]]
}

private enum NativeInkOperationKind: String {
    case commit
    case erase
    case createRegion
}

private enum NativePencilHostError: LocalizedError {
    case rejected(String)

    var errorDescription: String? {
        switch self {
        case .rejected(let code):
            return "网页笔迹接收失败：\(code)"
        }
    }
}

private struct NativeInkOperation {
    let id: String
    let documentToken: String
    let kind: NativeInkOperationKind
    let segments: [NativeInkSegment]
    let canvasStrokeCount: Int
    let regionId: String?
    let createdAtEpochMs: Int?

    init(
        id: String,
        documentToken: String,
        kind: NativeInkOperationKind,
        segments: [NativeInkSegment],
        canvasStrokeCount: Int,
        regionId: String? = nil,
        createdAtEpochMs: Int? = nil
    ) {
        self.id = id
        self.documentToken = documentToken
        self.kind = kind
        self.segments = segments
        self.canvasStrokeCount = canvasStrokeCount
        self.regionId = regionId
        self.createdAtEpochMs = createdAtEpochMs
    }
}

@MainActor
struct NativePencilLiveOverlay: View {
    let reader: ReaderWebViewModel
    @ObservedObject var controller: NativePencilInkController
    @ObservedObject private var settings = NativePencilSettings.shared
    @State private var launcherDragOrigin: CGPoint?
    @State private var draggedLauncherAnchor: CGPoint?

    private let colors = ["#ff3b30", "#007aff", "#111111", "#34c759"]

    private var toolIcon: String {
        switch controller.tool {
        case .pen: return "pencil.tip.crop.circle"
        case .eraser: return "eraser"
        case .selection: return "lasso"
        }
    }

    /// 收起成球时用的图标：笔换成**不自带圆圈**的 `pencil.tip`。
    /// `pencil.tip.crop.circle` 自己画了一个圆环,套进圆形球里就是两层圆,
    /// 而套进原先的圆角矩形里更糟 —— 圆环被矩形边切掉一截,看着像多了一层
    /// 方向不对的覆盖层。球本身已经是圆,图标就不该再带圆。
    private var collapsedToolIcon: String {
        controller.tool == .pen ? "pencil.tip" : toolIcon
    }

    private func palettePosition(in size: CGSize) -> CGPoint {
        let anchor = controller.paletteAnchor ?? CGPoint(x: 0.84, y: 0.78)
        let inset = min(132, size.width / 2)
        let minimumY: CGFloat = 104
        let rawY = anchor.y * size.height - 132
        return CGPoint(
            x: min(max(inset, anchor.x * size.width), max(inset, size.width - inset)),
            y: min(max(minimumY, rawY), max(minimumY, size.height - 72))
        )
    }

    private func launcherPosition(in size: CGSize) -> CGPoint {
        let anchor = draggedLauncherAnchor ?? settings.launcherAnchor
        let inset: CGFloat = 28
        return CGPoint(
            x: min(max(inset, anchor.x * size.width), max(inset, size.width - inset)),
            y: min(max(inset, anchor.y * size.height), max(inset, size.height - inset))
        )
    }

    private func normalizedLauncherAnchor(
        from position: CGPoint,
        in size: CGSize
    ) -> CGPoint {
        guard size.width > 0, size.height > 0 else {
            return settings.launcherAnchor
        }
        let inset: CGFloat = 28
        let clamped = CGPoint(
            x: min(max(inset, position.x), max(inset, size.width - inset)),
            y: min(max(inset, position.y), max(inset, size.height - inset))
        )
        return CGPoint(
            x: clamped.x / size.width,
            y: clamped.y / size.height
        )
    }

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                NativePencilCanvasRepresentable(
                    reader: reader,
                    controller: controller,
                    selectedTool: controller.tool,
                    selectedColorHex: controller.colorHex,
                    selectedWidth: controller.width
                )

                if controller.canDraw, controller.paletteVisible {
                    VStack(alignment: .trailing, spacing: 10) {
                        VStack(spacing: 10) {
                            HStack(spacing: 12) {
                                ForEach(colors, id: \.self) { color in
                                    Button {
                                        controller.colorHex = color
                                        controller.select(.pen)
                                    } label: {
                                        Circle()
                                            .fill(Color(uiColor: UIColor(bwHex: color)))
                                            .frame(width: 24, height: 24)
                                            .overlay {
                                                if controller.colorHex == color {
                                                    Circle().stroke(.white, lineWidth: 2)
                                                }
                                            }
                                    }
                                    .buttonStyle(.plain)
                                }
                            }

                            HStack(spacing: 10) {
                                Button {
                                    controller.select(.pen)
                                } label: {
                                    Image(systemName: "pencil.tip")
                                }
                                .buttonStyle(.borderedProminent)
                                .tint(controller.tool == .pen ? .blue : .gray)

                                Button {
                                    controller.select(.eraser)
                                } label: {
                                    Image(systemName: "eraser")
                                }
                                .buttonStyle(.borderedProminent)
                                .tint(controller.tool == .eraser ? .blue : .gray)

                                Button {
                                    controller.select(.selection)
                                } label: {
                                    Image(systemName: "lasso")
                                }
                                .buttonStyle(.borderedProminent)
                                .tint(controller.tool == .selection ? .blue : .gray)
                                .accessibilityLabel("选区笔")
                            }

                            HStack {
                                Image(systemName: "line.diagonal")
                                Slider(value: $controller.width, in: 1...16)
                                    .frame(width: 130)
                            }
                        }
                        .padding(12)
                        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))

                        Button {
                            controller.showPalette()
                        } label: {
                            Image(systemName: toolIcon)
                                .font(.title2)
                                .frame(width: 42, height: 42)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.blue)
                    }
                    .fixedSize()
                    .position(palettePosition(in: geometry.size))
                } else if controller.canDraw {
                    // 收起态 = 一枚正圆。`.borderedProminent` 画的是圆角矩形,而图标
                    // `pencil.tip.crop.circle` 自带圆环 —— 圆环被矩形边切掉一截,看着像多了
                    // 一层方向不对的覆盖层。这里自己画底:Circle 背景 + 不带圆环的图标,
                    // 并且把命中形状也设成 Circle,让可点区域和看到的圆一致。
                    Button {
                        controller.showPalette()
                    } label: {
                        Image(systemName: collapsedToolIcon)
                            .font(.title3)
                            .foregroundStyle(.white)
                            .frame(width: 52, height: 52)
                            .background(Color.accentColor, in: Circle())
                            .shadow(color: .black.opacity(0.26), radius: 7, y: 3)
                    }
                    .buttonStyle(.plain)
                    .contentShape(Circle())
                    .position(launcherPosition(in: geometry.size))
                    .gesture(
                        DragGesture(minimumDistance: 8)
                            .onChanged { value in
                                let origin = launcherDragOrigin
                                    ?? launcherPosition(in: geometry.size)
                                if launcherDragOrigin == nil {
                                    launcherDragOrigin = origin
                                }
                                draggedLauncherAnchor = normalizedLauncherAnchor(
                                    from: CGPoint(
                                        x: origin.x + value.translation.width,
                                        y: origin.y + value.translation.height
                                    ),
                                    in: geometry.size
                                )
                            }
                            .onEnded { _ in
                                if let anchor = draggedLauncherAnchor {
                                    settings.setLauncherAnchor(anchor)
                                }
                                launcherDragOrigin = nil
                                draggedLauncherAnchor = nil
                            }
                    )
                }
            }
        }
    }
}

@MainActor
private struct NativePencilCanvasRepresentable: UIViewRepresentable {
    let reader: ReaderWebViewModel
    @ObservedObject var controller: NativePencilInkController
    let selectedTool: NativePencilInkController.Tool
    let selectedColorHex: String
    let selectedWidth: CGFloat

    func makeCoordinator() -> Coordinator {
        Coordinator(reader: reader, controller: controller)
    }

    func makeUIView(context: Context) -> NativePencilPassthroughCanvas {
        let canvas = NativePencilPassthroughCanvas()
        canvas.delegate = context.coordinator
        canvas.backgroundColor = .clear
        canvas.isOpaque = false
        canvas.isScrollEnabled = false
        canvas.drawingPolicy = .pencilOnly
        canvas.captureRule = { [weak controller] point, event, bounds in
            // Pencil hover has no touch in UIEvent.allTouches. Let the hover
            // recognizer receive it; allowedTouchTypes below still limits the
            // recognizer itself to Apple Pencil.
            if event?.type == .hover { return true }
            guard
                let controller,
                bounds.width > 0,
                bounds.height > 0,
                event?.allTouches?.contains(where: { $0.type == .pencil }) == true
            else {
                return false
            }
            let normalized = CGPoint(
                x: point.x / bounds.width,
                y: point.y / bounds.height
            )
            return controller.layout.surface(at: normalized) != nil
        }

        let pencilPath = UIPanGestureRecognizer(
            target: context.coordinator,
            action: #selector(Coordinator.trackPencilPath(_:))
        )
        pencilPath.allowedTouchTypes = [
            NSNumber(value: UITouch.TouchType.pencil.rawValue)
        ]
        pencilPath.cancelsTouchesInView = true
        pencilPath.delegate = context.coordinator
        canvas.addGestureRecognizer(pencilPath)

        let pencilHover = UIHoverGestureRecognizer(
            target: context.coordinator,
            action: #selector(Coordinator.trackPencilHover(_:))
        )
        pencilHover.allowedTouchTypes = [
            NSNumber(value: UITouch.TouchType.pencil.rawValue)
        ]
        canvas.addGestureRecognizer(pencilHover)
        context.coordinator.canvas = canvas
        context.coordinator.pathGesture = pencilPath
        reader.bindNativeVisualCaptureCanvas(canvas)
        context.coordinator.installSelectionPreview(on: canvas)
        context.coordinator.applyState(
            to: canvas,
            tool: selectedTool,
            colorHex: selectedColorHex,
            width: selectedWidth
        )
        return canvas
    }

    func updateUIView(
        _ canvas: NativePencilPassthroughCanvas,
        context: Context
    ) {
        reader.bindNativeVisualCaptureCanvas(canvas)
        context.coordinator.controller = controller
        context.coordinator.applyState(
            to: canvas,
            tool: selectedTool,
            colorHex: selectedColorHex,
            width: selectedWidth
        )
        context.coordinator.retryIfRequested()
    }

    static func dismantleUIView(
        _ canvas: NativePencilPassthroughCanvas,
        coordinator: Coordinator
    ) {
        coordinator.reader.unbindNativeVisualCaptureCanvas(canvas)
    }

    @MainActor
    final class Coordinator: NSObject, PKCanvasViewDelegate,
        UIGestureRecognizerDelegate
    {
        let reader: ReaderWebViewModel
        var controller: NativePencilInkController
        weak var canvas: PKCanvasView?
        weak var pathGesture: UIPanGestureRecognizer?

        private var appliedTool: NativePencilInkController.Tool?
        private var appliedColor = ""
        private var appliedWidth: CGFloat = 0
        private var appliedWebFallbackGeneration = -1
        private var appliedWebFallbackTool: NativePencilInkController.Tool?
        private var appliedWebFallbackColor = ""
        private var appliedWebFallbackWidth: CGFloat = 0
        private var appliedRetryRequest = 0
        private var appliedDocumentGeneration = 0
        private var canvasToolActive = false
        private var activeCanvasTool: NativePencilInkController.Tool?
        private var pathGestureActive = false
        private var activePathTool: NativePencilInkController.Tool?
        private var resetCanvasWhenIdle = false
        private var queuedStrokeCount = 0
        private var confirmedStrokeCount = 0
        private var strokeLayout = NativeInkLayout.empty
        private var strokeColor = "#ff3b30"
        private var strokeWidth: CGFloat = 4
        private var eraserPoints: [[CGFloat]] = []
        private var selectionPreviewPoints: [CGPoint] = []
        private var eraserLayout = NativeInkLayout.empty
        private var eraserDrawingSnapshot: PKDrawing?
        private let selectionShapeLayer = CAShapeLayer()
        private var pending: [NativeInkOperation] = []
        private var pumpTask: Task<Void, Never>?

        init(
            reader: ReaderWebViewModel,
            controller: NativePencilInkController
        ) {
            self.reader = reader
            self.controller = controller
            appliedDocumentGeneration = controller.documentGeneration
        }

        func installSelectionPreview(on canvas: PKCanvasView) {
            selectionShapeLayer.fillColor = UIColor.systemCyan
                .withAlphaComponent(0.10).cgColor
            selectionShapeLayer.strokeColor = UIColor.systemCyan.cgColor
            selectionShapeLayer.lineWidth = 3
            selectionShapeLayer.lineCap = .round
            selectionShapeLayer.lineJoin = .round
            selectionShapeLayer.shadowColor = UIColor.black.cgColor
            selectionShapeLayer.shadowOpacity = 0.35
            selectionShapeLayer.shadowRadius = 1.5
            selectionShapeLayer.shadowOffset = .zero
            selectionShapeLayer.zPosition = 10_000
            selectionShapeLayer.isHidden = true
            canvas.layer.addSublayer(selectionShapeLayer)
        }

        func applyState(
            to canvas: PKCanvasView,
            tool: NativePencilInkController.Tool,
            colorHex: String,
            width requestedWidth: CGFloat
        ) {
            synchronizeDocumentGeneration(on: canvas)
            let width = max(1, min(requestedWidth, 16))
            pathGesture?.isEnabled = tool != .pen
            canvas.drawingGestureRecognizer.isEnabled = tool == .pen
            selectionShapeLayer.frame = canvas.bounds
            selectionShapeLayer.isHidden = tool != .selection
            if tool != .selection {
                clearSelectionPreview()
            }
            // The web ink engine is the fail-safe owner whenever PencilKit's
            // hit-test declines a stroke. Keep both owners on the same style
            // so that falling back never collapses to the old red/2.5 line.
            if appliedWebFallbackGeneration != appliedDocumentGeneration
                || appliedWebFallbackTool != tool
                || appliedWebFallbackColor != colorHex
                || appliedWebFallbackWidth != width
            {
                appliedWebFallbackGeneration = appliedDocumentGeneration
                appliedWebFallbackTool = tool
                appliedWebFallbackColor = colorHex
                appliedWebFallbackWidth = width
                reader.synchronizeWebInkFallbackStyle(
                    tool: tool,
                    colorHex: colorHex,
                    width: width
                )
            }
            guard
                appliedTool != tool
                    || appliedColor != colorHex
                    || appliedWidth != width
            else {
                return
            }
            appliedTool = tool
            appliedColor = colorHex
            appliedWidth = width
            switch tool {
            case .pen:
                canvas.tool = PKInkingTool(
                    .pen,
                    color: UIColor(bwHex: colorHex),
                    width: width
                )
            case .eraser:
                canvas.tool = PKEraserTool(.vector)
            case .selection:
                // A dedicated Pencil-only recognizer owns selection paths.
                // Keeping PencilKit's drawing recognizer disabled avoids the
                // two recognizers racing for the same stroke.
                canvas.tool = PKInkingTool(.pen, color: .clear, width: 1)
            }
        }

        func retryIfRequested() {
            guard appliedRetryRequest != controller.retryRequest else { return }
            appliedRetryRequest = controller.retryRequest
            pump()
        }

        func canvasViewDidBeginUsingTool(_ canvasView: PKCanvasView) {
            synchronizeDocumentGeneration(on: canvasView)
            canvasToolActive = true
            activeCanvasTool = controller.tool
            if activeCanvasTool == .pen {
                strokeLayout = controller.layout
                strokeColor = controller.colorHex
                strokeWidth = max(1, min(controller.width, 16))
                // Freeze the selected style at the beginning of this stroke.
                // PencilKit's sampled stroke colour/first-point size is not a
                // reliable round-trip source on iPad and previously collapsed
                // persisted strokes back to red/4.
            } else if eraserDrawingSnapshot == nil {
                eraserDrawingSnapshot = canvasView.drawing
            }
        }

        func canvasViewDidEndUsingTool(_ canvasView: PKCanvasView) {
            defer {
                canvasToolActive = false
                activeCanvasTool = nil
                finishEraserCanvasIfIdle()
                finishDeferredCanvasWorkIfIdle()
            }
            guard activeCanvasTool == .pen else { return }
            let allStrokes = canvasView.drawing.strokes
            if let lastPoint = allStrokes.last?.path.last?.location {
                controller.updateRecentPencilAnchor(
                    lastPoint,
                    in: canvasView.bounds
                )
            }
            let start = min(queuedStrokeCount, allStrokes.count)
            let newStrokes = Array(allStrokes.dropFirst(start))
            guard !newStrokes.isEmpty else { return }

            guard
                let documentToken = strokeLayout.documentToken,
                documentToken == controller.layout.documentToken
            else {
                canvasView.drawing = PKDrawing(
                    strokes: Array(allStrokes.prefix(start))
                )
                return
            }

            let segments = newStrokes.flatMap {
                canonicalSegments(
                    for: $0,
                    layout: strokeLayout,
                    canvasBounds: canvasView.bounds,
                    color: strokeColor,
                    width: strokeWidth
                )
            }
            guard !segments.isEmpty else {
                canvasView.drawing = PKDrawing(
                    strokes: Array(allStrokes.prefix(start))
                )
                return
            }
            queuedStrokeCount += newStrokes.count
            enqueue(NativeInkOperation(
                id: UUID().uuidString,
                documentToken: documentToken,
                kind: .commit,
                segments: segments,
                canvasStrokeCount: newStrokes.count
            ))
        }

        private func canonicalSegments(
            for stroke: PKStroke,
            layout: NativeInkLayout,
            canvasBounds: CGRect,
            color: String,
            width: CGFloat
        ) -> [NativeInkSegment] {
            guard canvasBounds.width > 0, canvasBounds.height > 0 else {
                return []
            }
            var output: [NativeInkSegment] = []
            var currentSurface: NativeInkSurface?
            var currentPoints: [[CGFloat]] = []

            func flush() {
                guard let surface = currentSurface, currentPoints.count >= 2 else {
                    currentPoints = []
                    currentSurface = nil
                    return
                }
                output.append(NativeInkSegment(
                    surfaceId: surface.id,
                    color: color,
                    width: max(1, min(width, 16)),
                    points: currentPoints
                ))
                currentPoints = []
                currentSurface = nil
            }

            for sample in stroke.path {
                let normalized = CGPoint(
                    x: sample.location.x / canvasBounds.width,
                    y: sample.location.y / canvasBounds.height
                )
                guard
                    let surface = layout.surface(at: normalized),
                    let point = surface.canonicalPoint(normalized)
                else {
                    flush()
                    continue
                }
                if currentSurface?.id != surface.id {
                    flush()
                    currentSurface = surface
                }
                if let last = currentPoints.last {
                    let dx = point[0] - last[0]
                    let dy = point[1] - last[1]
                    if dx * dx + dy * dy < 0.000_004 { continue }
                }
                currentPoints.append(point)
            }
            flush()
            return output
        }

        @objc func trackPencilPath(_ gesture: UIPanGestureRecognizer) {
            guard let canvas else { return }
            let bounds = canvas.bounds
            guard bounds.width > 0, bounds.height > 0 else { return }
            let location = gesture.location(in: canvas)
            if gesture.state == .ended {
                controller.updateRecentPencilAnchor(location, in: bounds)
            }
            let point = [
                location.x / bounds.width,
                location.y / bounds.height,
            ]
            switch gesture.state {
            case .began:
                guard controller.tool != .pen else { return }
                synchronizeDocumentGeneration(on: canvas)
                pathGestureActive = true
                activePathTool = controller.tool
                if eraserDrawingSnapshot == nil {
                    eraserDrawingSnapshot = canvas.drawing
                }
                eraserLayout = controller.layout
                eraserPoints = [point]
                if activePathTool == .selection {
                    selectionPreviewPoints = [location]
                    updateSelectionPreview()
                }
            case .changed:
                guard pathGestureActive else { return }
                if let last = eraserPoints.last {
                    let dx = point[0] - last[0]
                    let dy = point[1] - last[1]
                    if dx * dx + dy * dy < 0.000_004 { return }
                }
                eraserPoints.append(point)
                if activePathTool == .selection {
                    selectionPreviewPoints.append(location)
                    updateSelectionPreview()
                }
            case .ended, .cancelled, .failed:
                guard pathGestureActive, let pathTool = activePathTool else {
                    return
                }
                eraserPoints.append(point)
                if pathTool == .selection {
                    selectionPreviewPoints.append(location)
                    updateSelectionPreview()
                }
                var segments = canonicalEraserSegments(
                    points: eraserPoints,
                    layout: eraserLayout
                )
                eraserPoints = []
                if pathTool == .selection {
                    segments = closedRegionSegments(from: segments)
                }
                if
                    !segments.isEmpty,
                    let documentToken = eraserLayout.documentToken,
                    documentToken == controller.layout.documentToken,
                    pathTool == .eraser || gesture.state == .ended
                {
                    let regionId = pathTool == .selection
                        ? "rg_" + UUID().uuidString.lowercased()
                        : nil
                    enqueue(NativeInkOperation(
                        id: UUID().uuidString,
                        documentToken: documentToken,
                        kind: pathTool == .selection ? .createRegion : .erase,
                        segments: segments,
                        canvasStrokeCount: 0,
                        regionId: regionId,
                        createdAtEpochMs: regionId == nil ? nil : Int(
                            Date().timeIntervalSince1970 * 1_000
                        )
                    ))
                }
                pathGestureActive = false
                activePathTool = nil
                clearSelectionPreview()
                finishEraserCanvasIfIdle()
                finishDeferredCanvasWorkIfIdle()
            default:
                break
            }
        }

        private func updateSelectionPreview() {
            if let canvas {
                selectionShapeLayer.frame = canvas.bounds
            }
            selectionShapeLayer.isHidden = false
            let path = UIBezierPath()
            guard let first = selectionPreviewPoints.first else {
                selectionShapeLayer.path = nil
                return
            }
            path.move(to: first)
            for point in selectionPreviewPoints.dropFirst() {
                path.addLine(to: point)
            }
            if selectionPreviewPoints.count >= 3 {
                path.close()
            }
            selectionShapeLayer.path = path.cgPath
        }

        private func clearSelectionPreview() {
            selectionPreviewPoints.removeAll(keepingCapacity: true)
            selectionShapeLayer.path = nil
            selectionShapeLayer.isHidden = controller.tool != .selection
        }

        @objc func trackPencilHover(_ gesture: UIHoverGestureRecognizer) {
            guard let canvas else { return }
            switch gesture.state {
            case .began, .changed, .ended:
                controller.updateRecentPencilAnchor(
                    gesture.location(in: canvas),
                    in: canvas.bounds
                )
            default:
                break
            }
        }

        private func closedRegionSegments(
            from segments: [NativeInkSegment]
        ) -> [NativeInkSegment] {
            segments.compactMap { segment in
                var points = Array(segment.points.prefix(511))
                guard points.count >= 3, let first = points.first else {
                    return nil
                }
                if let last = points.last {
                    let separated = abs(first[0] - last[0]) > 0.000_1
                        || abs(first[1] - last[1]) > 0.000_1
                    if separated { points.append(first) }
                }
                return NativeInkSegment(
                    surfaceId: segment.surfaceId,
                    color: "#0a84ff",
                    width: 2,
                    points: points
                )
            }
        }

        private func canonicalEraserSegments(
            points: [[CGFloat]],
            layout: NativeInkLayout
        ) -> [NativeInkSegment] {
            var output: [NativeInkSegment] = []
            var currentSurface: NativeInkSurface?
            var currentPoints: [[CGFloat]] = []

            func flush() {
                guard let surface = currentSurface, !currentPoints.isEmpty else {
                    currentPoints = []
                    currentSurface = nil
                    return
                }
                output.append(NativeInkSegment(
                    surfaceId: surface.id,
                    color: nil,
                    width: nil,
                    points: currentPoints
                ))
                currentPoints = []
                currentSurface = nil
            }

            for raw in points where raw.count >= 2 {
                let normalized = CGPoint(x: raw[0], y: raw[1])
                guard
                    let surface = layout.surface(at: normalized),
                    let point = surface.canonicalPoint(normalized)
                else {
                    flush()
                    continue
                }
                if currentSurface?.id != surface.id {
                    flush()
                    currentSurface = surface
                }
                currentPoints.append(point)
            }
            flush()
            return output
        }

        private func enqueue(_ operation: NativeInkOperation) {
            reader.signalNativePencilOperationPending(operation)
            pending.append(operation)
            controller.updatePendingOperationCount(pending.count)
            pump()
        }

        private var interactionActive: Bool {
            canvasToolActive || pathGestureActive
        }

        private func synchronizeDocumentGeneration(on canvas: PKCanvasView) {
            guard appliedDocumentGeneration != controller.documentGeneration else {
                return
            }
            appliedDocumentGeneration = controller.documentGeneration
            let abandonedOperations = pending
            let abandoned = abandonedOperations.count
            pending.removeAll()
            abandonedOperations.forEach {
                reader.signalNativePencilOperationCancelled($0)
            }
            pumpTask?.cancel()
            queuedStrokeCount = 0
            confirmedStrokeCount = 0
            eraserPoints = []
            clearSelectionPreview()
            eraserLayout = .empty
            pathGestureActive = false
            activePathTool = nil
            strokeLayout = .empty
            controller.updatePendingOperationCount(0)
            if abandoned > 0 {
                controller.reportAbandonedOperations(abandoned)
            } else {
                controller.clearError()
            }
            if interactionActive {
                resetCanvasWhenIdle = true
            } else {
                canvas.drawing = PKDrawing()
                eraserDrawingSnapshot = nil
                resetCanvasWhenIdle = false
            }
        }

        private func finishEraserCanvasIfIdle() {
            guard !interactionActive, let canvas else { return }
            if let snapshot = eraserDrawingSnapshot {
                canvas.drawing = snapshot
                eraserDrawingSnapshot = nil
            }
        }

        private func finishDeferredCanvasWorkIfIdle() {
            guard !interactionActive, let canvas else { return }
            if resetCanvasWhenIdle {
                canvas.drawing = PKDrawing()
                queuedStrokeCount = 0
                confirmedStrokeCount = 0
                eraserDrawingSnapshot = nil
                resetCanvasWhenIdle = false
                return
            }
            guard confirmedStrokeCount > 0 else { return }
            let count = min(
                confirmedStrokeCount,
                canvas.drawing.strokes.count
            )
            canvas.drawing = PKDrawing(strokes: Array(
                canvas.drawing.strokes.dropFirst(count)
            ))
            queuedStrokeCount = max(0, queuedStrokeCount - count)
            confirmedStrokeCount -= count
        }

        private func pump() {
            guard pumpTask == nil, !pending.isEmpty else { return }
            pumpTask = Task { @MainActor [weak self] in
                guard let self else { return }
                while let operation = self.pending.first {
                    let generation = self.appliedDocumentGeneration
                    do {
                        try await self.reader.applyNativePencilOperation(operation)
                        guard
                            !Task.isCancelled,
                            generation == self.appliedDocumentGeneration,
                            self.pending.first?.id == operation.id
                        else {
                            break
                        }
                        self.confirmedStrokeCount += operation.canvasStrokeCount
                        self.pending.removeFirst()
                        self.controller.updatePendingOperationCount(
                            self.pending.count
                        )
                        self.controller.clearError()
                        self.finishDeferredCanvasWorkIfIdle()
                    } catch {
                        if Task.isCancelled
                            || generation != self.appliedDocumentGeneration
                        {
                            break
                        }
                        self.reader.signalNativePencilOperationCancelled(operation)
                        self.controller.report(error)
                        break
                    }
                }
                self.pumpTask = nil
                if !self.pending.isEmpty, self.controller.lastError == nil {
                    self.pump()
                }
            }
        }

        func gestureRecognizer(
            _ gestureRecognizer: UIGestureRecognizer,
            shouldRecognizeSimultaneouslyWith otherGestureRecognizer:
                UIGestureRecognizer
        ) -> Bool {
            if let pathGesture,
                gestureRecognizer === pathGesture
                    || otherGestureRecognizer === pathGesture
            {
                return false
            }
            return true
        }

        func gestureRecognizerShouldBegin(
            _ gestureRecognizer: UIGestureRecognizer
        ) -> Bool {
            guard let pathGesture, gestureRecognizer === pathGesture else {
                return true
            }
            return controller.tool != .pen
        }
    }
}

final class NativePencilPassthroughCanvas: PKCanvasView {
    var captureRule: ((CGPoint, UIEvent?, CGRect) -> Bool)?

    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        guard super.point(inside: point, with: event) else { return false }
        return captureRule?(point, event, bounds) == true
    }
}

extension UIColor {
    convenience init(bwHex value: String) {
        let clean = value.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var rgb: UInt64 = 0
        Scanner(string: clean).scanHexInt64(&rgb)
        if clean.count == 6 {
            self.init(
                red: CGFloat((rgb >> 16) & 0xff) / 255,
                green: CGFloat((rgb >> 8) & 0xff) / 255,
                blue: CGFloat(rgb & 0xff) / 255,
                alpha: 1
            )
        } else {
            self.init(red: 1, green: 0.23, blue: 0.19, alpha: 1)
        }
    }

    var bwHexString: String {
        var red: CGFloat = 1
        var green: CGFloat = 0
        var blue: CGFloat = 0
        var alpha: CGFloat = 1
        guard getRed(&red, green: &green, blue: &blue, alpha: &alpha) else {
            return "#ff3b30"
        }
        return String(
            format: "#%02x%02x%02x",
            Int(red * 255),
            Int(green * 255),
            Int(blue * 255)
        )
    }
}

@MainActor
fileprivate extension ReaderWebViewModel {
    func synchronizeWebInkFallbackStyle(
        tool: NativePencilInkController.Tool,
        colorHex: String,
        width: CGFloat
    ) {
        let safeColor = colorHex.range(
            of: #"^#[0-9a-fA-F]{6}$"#,
            options: .regularExpression
        ) == nil ? "#ff3b30" : colorHex
        let safeWidth = max(1, min(width, 16))
        let webTool: String
        switch tool {
        case .pen: webTool = "pen"
        case .eraser: webTool = "eraser"
        case .selection: webTool = "region"
        }
        webView.evaluateJavaScript(
            """
            (() => {
              const color = \(String(reflecting: safeColor));
              const width = \(safeWidth);
              const tool = \(String(reflecting: webTool));
              if (typeof _ink === "object") {
                _ink.color = color;
                _ink.width = width;
                _ink.tool = tool;
              }
              if (typeof _epInk === "object") {
                _epInk.color = color;
                _epInk.width = width;
                _epInk.tool = tool;
              }
              if (
                window.RC &&
                RC.stickynote &&
                typeof RC.stickynote.synchronizeInkToolStyle === "function"
              ) {
                RC.stickynote.synchronizeInkToolStyle(tool, color, width);
              }
              document.querySelectorAll(
                "#ink-toolbar button[data-tool], " +
                "#ep-ink-toolbar button[data-itool], " +
                ".bw-ink-tools button[data-tool]"
              ).forEach((button) => {
                const value = button.getAttribute("data-tool")
                  || button.getAttribute("data-itool");
                button.classList.toggle(
                  "on",
                  value === tool || (tool === "region" && value === "selection")
                );
              });
            })();
            """,
            completionHandler: nil
        )
    }

    func signalNativePencilOperationPending(_ operation: NativeInkOperation) {
        signalNativePencilOperation(
            event: "rc:inkpending",
            operation: operation
        )
    }

    func signalNativePencilOperationCancelled(_ operation: NativeInkOperation) {
        signalNativePencilOperation(
            event: "rc:inkcancel",
            operation: operation
        )
    }

    private func signalNativePencilOperation(
        event: String,
        operation: NativeInkOperation
    ) {
        let surfaceIds = Array(Set(operation.segments.map(\.surfaceId))).sorted()
        guard
            JSONSerialization.isValidJSONObject(surfaceIds),
            let data = try? JSONSerialization.data(withJSONObject: surfaceIds),
            let surfaceLiteral = String(data: data, encoding: .utf8)
        else { return }
        webView.evaluateJavaScript(
            """
            window.dispatchEvent(new CustomEvent(\(String(reflecting: event)), {
              detail: {
                source: 'native-pencil',
                opId: \(String(reflecting: operation.id)),
                surfaceIds: \(surfaceLiteral)
              }
            }));
            """,
            completionHandler: nil
        )
    }

    func applyNativePencilOperation(
        _ operation: NativeInkOperation
    ) async throws {
        let segments: [[String: Any]] = operation.segments.map { segment in
            var value: [String: Any] = [
                "surfaceId": segment.surfaceId,
                "points": segment.points,
            ]
            if let color = segment.color { value["color"] = color }
            if let width = segment.width { value["width"] = width }
            return value
        }
        var payload: [String: Any] = [
            "opId": operation.id,
            "documentToken": operation.documentToken,
            "segments": segments,
        ]
        if let regionId = operation.regionId {
            payload["regionId"] = regionId
        }
        if let createdAtEpochMs = operation.createdAtEpochMs {
            payload["createdAtEpochMs"] = createdAtEpochMs
        }
        try await callNativeInkHost(operation.kind.rawValue, payload: payload)
    }

    func callNativeInkHost(
        _ action: String,
        payload: [String: Any]
    ) async throws {
        guard JSONSerialization.isValidJSONObject(payload) else {
            throw NativeReaderCaptureError.invalidPagePayload
        }
        let data = try JSONSerialization.data(withJSONObject: payload)
        guard let literal = String(data: data, encoding: .utf8) else {
            throw NativeReaderCaptureError.invalidPagePayload
        }
        let script = """
        (() => {
          const host = window.__bwNativeInkHost;
          if (!host || typeof host.\(action) !== "function") {
            return JSON.stringify({ok:false,error:"native_ink_unavailable"});
          }
          return JSON.stringify(host.\(action)(\(literal)) || {});
        })()
        """
        let raw: Any = try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Any, Error>) in
            webView.evaluateJavaScript(script) { value, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if let value {
                    continuation.resume(returning: value)
                } else {
                    continuation.resume(
                        throwing: NativeReaderCaptureError.invalidPagePayload
                    )
                }
            }
        }
        guard
            let json = raw as? String,
            let resultData = json.data(using: .utf8),
            let result = try JSONSerialization.jsonObject(with: resultData)
                as? [String: Any]
        else {
            throw NativeReaderCaptureError.pageUnavailable
        }
        guard result["ok"] as? Bool == true else {
            throw NativePencilHostError.rejected(
                result["error"] as? String ?? "unknown"
            )
        }
    }
}

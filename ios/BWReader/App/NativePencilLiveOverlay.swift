import Foundation
import PencilKit
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
    }

    @Published private(set) var tool: Tool = .pen
    @Published private(set) var layout = NativeInkLayout.empty
    @Published private(set) var lastError: String?
    @Published private(set) var retryRequest = 0
    @Published private(set) var documentGeneration = 0
    @Published private(set) var pendingOperationCount = 0
    @Published var paletteVisible = false
    @Published var colorHex = "#ff3b30"
    @Published var width: CGFloat = 4

    var canDraw: Bool { !layout.surfaces.isEmpty }
    var hasPendingOperations: Bool { pendingOperationCount > 0 }

    func toggleEraser() {
        tool = tool == .pen ? .eraser : .pen
    }

    func select(_ value: Tool) {
        tool = value
    }

    func showPalette() {
        paletteVisible.toggle()
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
}

@MainActor
struct NativePencilLiveOverlay: View {
    let reader: ReaderWebViewModel
    @ObservedObject var controller: NativePencilInkController

    private let colors = ["#ff3b30", "#007aff", "#111111", "#34c759"]

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            NativePencilCanvasRepresentable(
                reader: reader,
                controller: controller
            )

            if controller.canDraw {
                VStack(alignment: .trailing, spacing: 10) {
                    if controller.paletteVisible {
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
                            }

                            HStack {
                                Image(systemName: "line.diagonal")
                                Slider(value: $controller.width, in: 1...16)
                                    .frame(width: 130)
                            }
                        }
                        .padding(12)
                        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
                    }

                    Button {
                        controller.showPalette()
                    } label: {
                        Image(systemName: controller.tool == .eraser ? "eraser" : "pencil.tip.crop.circle")
                            .font(.title2)
                            .frame(width: 42, height: 42)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.blue)
                }
                .padding(.trailing, 14)
                .padding(.bottom, 86)
            }
        }
    }
}

@MainActor
private struct NativePencilCanvasRepresentable: UIViewRepresentable {
    let reader: ReaderWebViewModel
    @ObservedObject var controller: NativePencilInkController

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

        let eraserPath = UIPanGestureRecognizer(
            target: context.coordinator,
            action: #selector(Coordinator.trackPencilPath(_:))
        )
        eraserPath.allowedTouchTypes = [
            NSNumber(value: UITouch.TouchType.pencil.rawValue)
        ]
        eraserPath.cancelsTouchesInView = false
        eraserPath.delegate = context.coordinator
        canvas.addGestureRecognizer(eraserPath)
        context.coordinator.canvas = canvas
        context.coordinator.applyState(to: canvas)
        return canvas
    }

    func updateUIView(
        _ canvas: NativePencilPassthroughCanvas,
        context: Context
    ) {
        context.coordinator.controller = controller
        context.coordinator.applyState(to: canvas)
        context.coordinator.retryIfRequested()
    }

    @MainActor
    final class Coordinator: NSObject, PKCanvasViewDelegate,
        UIGestureRecognizerDelegate
    {
        let reader: ReaderWebViewModel
        var controller: NativePencilInkController
        weak var canvas: PKCanvasView?

        private var appliedTool: NativePencilInkController.Tool?
        private var appliedColor = ""
        private var appliedWidth: CGFloat = 0
        private var appliedRetryRequest = 0
        private var appliedDocumentGeneration = 0
        private var canvasToolActive = false
        private var activeCanvasTool: NativePencilInkController.Tool?
        private var eraserGestureActive = false
        private var resetCanvasWhenIdle = false
        private var queuedStrokeCount = 0
        private var confirmedStrokeCount = 0
        private var strokeLayout = NativeInkLayout.empty
        private var eraserPoints: [[CGFloat]] = []
        private var eraserLayout = NativeInkLayout.empty
        private var eraserDrawingSnapshot: PKDrawing?
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

        func applyState(to canvas: PKCanvasView) {
            synchronizeDocumentGeneration(on: canvas)
            let width = max(1, min(controller.width, 16))
            guard
                appliedTool != controller.tool
                    || appliedColor != controller.colorHex
                    || appliedWidth != width
            else {
                return
            }
            appliedTool = controller.tool
            appliedColor = controller.colorHex
            appliedWidth = width
            switch controller.tool {
            case .pen:
                canvas.tool = PKInkingTool(
                    .pen,
                    color: UIColor(bwHex: controller.colorHex),
                    width: width
                )
            case .eraser:
                canvas.tool = PKEraserTool(.vector)
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
                    canvasBounds: canvasView.bounds
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
            canvasBounds: CGRect
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
                    color: stroke.ink.color.bwHexString,
                    width: max(1, min(stroke.path.first?.size.width ?? 4, 20)),
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
            let point = [
                location.x / bounds.width,
                location.y / bounds.height,
            ]
            switch gesture.state {
            case .began:
                guard controller.tool == .eraser else { return }
                synchronizeDocumentGeneration(on: canvas)
                eraserGestureActive = true
                if eraserDrawingSnapshot == nil {
                    eraserDrawingSnapshot = canvas.drawing
                }
                eraserLayout = controller.layout
                eraserPoints = [point]
            case .changed:
                guard eraserGestureActive else { return }
                if let last = eraserPoints.last {
                    let dx = point[0] - last[0]
                    let dy = point[1] - last[1]
                    if dx * dx + dy * dy < 0.000_004 { return }
                }
                eraserPoints.append(point)
            case .ended, .cancelled, .failed:
                guard eraserGestureActive else { return }
                eraserPoints.append(point)
                let segments = canonicalEraserSegments(
                    points: eraserPoints,
                    layout: eraserLayout
                )
                eraserPoints = []
                if
                    !segments.isEmpty,
                    let documentToken = eraserLayout.documentToken,
                    documentToken == controller.layout.documentToken
                {
                    enqueue(NativeInkOperation(
                        id: UUID().uuidString,
                        documentToken: documentToken,
                        kind: .erase,
                        segments: segments,
                        canvasStrokeCount: 0
                    ))
                }
                eraserGestureActive = false
                finishEraserCanvasIfIdle()
                finishDeferredCanvasWorkIfIdle()
            default:
                break
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
            pending.append(operation)
            controller.updatePendingOperationCount(pending.count)
            pump()
        }

        private var interactionActive: Bool {
            canvasToolActive || eraserGestureActive
        }

        private func synchronizeDocumentGeneration(on canvas: PKCanvasView) {
            guard appliedDocumentGeneration != controller.documentGeneration else {
                return
            }
            appliedDocumentGeneration = controller.documentGeneration
            let abandoned = pending.count
            pending.removeAll()
            pumpTask?.cancel()
            queuedStrokeCount = 0
            confirmedStrokeCount = 0
            eraserPoints = []
            eraserLayout = .empty
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
            true
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

private extension UIColor {
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
        try await callNativeInkHost(
            operation.kind.rawValue,
            payload: [
                "opId": operation.id,
                "documentToken": operation.documentToken,
                "segments": segments,
            ]
        )
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

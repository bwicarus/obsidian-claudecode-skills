import Foundation
import SwiftUI

enum AnnotationTool: String, CaseIterable, Identifiable {
    case pen
    case line
    case arrow
    case note
    case eraser

    var id: String { rawValue }
    var title: String {
        switch self {
        case .pen: return "画笔"
        case .line: return "直线"
        case .arrow: return "箭头"
        case .note: return "文字"
        case .eraser: return "橡皮"
        }
    }
    var symbol: String {
        switch self {
        case .pen: return "pencil.tip"
        case .line: return "line.diagonal"
        case .arrow: return "arrow.up.right"
        case .note: return "text.bubble"
        case .eraser: return "eraser"
        }
    }
}

struct AnnotationPoint: Codable, Hashable {
    let x: Double
    let y: Double

    init(x: Double, y: Double) {
        self.x = min(max(x, 0), 1)
        self.y = min(max(y, 0), 1)
    }
}

/// The array index is the chart's x coordinate. Time keys keep anchors stable
/// when earlier candles are added or removed from the local data window.
struct ChartAnnotationViewport: Hashable {
    let period: String
    let times: [String]
    let xDomain: ClosedRange<Double>
    let yDomain: ClosedRange<Double>

    var isUsable: Bool {
        !period.isEmpty && !times.isEmpty &&
        xDomain.lowerBound.isFinite && xDomain.upperBound.isFinite &&
        yDomain.lowerBound.isFinite && yDomain.upperBound.isFinite &&
        xDomain.upperBound > xDomain.lowerBound && yDomain.upperBound > yDomain.lowerBound
    }

    fileprivate func anchor(for point: AnnotationPoint) -> AnnotationDataAnchor? {
        guard isUsable else { return nil }
        let x = xDomain.lowerBound + point.x * (xDomain.upperBound - xDomain.lowerBound)
        let base = min(max(Int(floor(x)), 0), times.count - 1)
        guard !times[base].isEmpty else { return nil }
        return AnnotationDataAnchor(time: times[base], indexOffset: x - Double(base),
                                    price: yDomain.upperBound - point.y * (yDomain.upperBound - yDomain.lowerBound))
    }
}

struct AnnotationDataAnchor: Codable, Hashable {
    let time: String
    let indexOffset: Double
    let price: Double
}

struct ChartAnnotation: Codable, Identifiable {
    enum Kind: String, Codable { case pen, line, arrow, note }

    let id: UUID
    let kind: Kind
    let points: [AnnotationPoint]
    let text: String?
    let color: String
    let createdAt: Date
    // Optional fields preserve the existing JSON format and unlocated drawings.
    let period: String?
    let dataAnchors: [AnnotationDataAnchor]?

    init(kind: Kind, points: [AnnotationPoint], text: String? = nil, color: String = "accent") {
        id = UUID()
        self.kind = kind
        self.points = points
        self.text = text
        self.color = color
        createdAt = Date()
        period = nil
        dataAnchors = nil
    }

    fileprivate init(anchoring value: ChartAnnotation, in viewport: ChartAnnotationViewport,
                     anchors: [AnnotationDataAnchor]) {
        id = value.id
        kind = value.kind
        points = value.points
        text = value.text
        color = value.color
        createdAt = value.createdAt
        period = viewport.period
        dataAnchors = anchors
    }
}

fileprivate struct ProjectedAnnotation {
    let annotation: ChartAnnotation
    // Unclamped plot coordinates, so clipping never moves an offscreen stroke
    // onto the chart edge or turns a crossing segment into a different line.
    let points: [CGPoint]
    let visibleSegments: [(CGPoint, CGPoint)]

    var voiceStart: AnnotationPoint? {
        let point = annotation.kind == .note ? points.first : visibleSegments.first?.0
        return point.map { AnnotationPoint(x: $0.x, y: $0.y) }
    }

    var voiceEnd: AnnotationPoint? {
        guard annotation.kind != .note, let point = visibleSegments.last?.1 else { return nil }
        return AnnotationPoint(x: point.x, y: point.y)
    }
}

private func containsPlotPoint(_ point: CGPoint) -> Bool {
    point.x >= 0 && point.x <= 1 && point.y >= 0 && point.y <= 1
}

/// Liang-Barsky clipping to the normalized plot, also used for hit testing and
/// voice context. A segment crossing the plot is visible even if both ends exit it.
private func clippedPlotSegment(_ start: CGPoint, _ end: CGPoint) -> (CGPoint, CGPoint)? {
    let dx = end.x - start.x
    let dy = end.y - start.y
    let p = [-dx, dx, -dy, dy]
    let q = [start.x, 1 - start.x, start.y, 1 - start.y]
    var lower: CGFloat = 0
    var upper: CGFloat = 1
    for index in 0..<4 {
        if abs(p[index]) < 1e-12 {
            if q[index] < 0 { return nil }
        } else {
            let ratio = q[index] / p[index]
            if p[index] < 0 { lower = max(lower, ratio) }
            else { upper = min(upper, ratio) }
            if lower > upper { return nil }
        }
    }
    return (CGPoint(x: start.x + lower * dx, y: start.y + lower * dy),
            CGPoint(x: start.x + upper * dx, y: start.y + upper * dy))
}

private func distanceToSegment(_ point: CGPoint, _ start: CGPoint, _ end: CGPoint) -> CGFloat {
    let dx = end.x - start.x
    let dy = end.y - start.y
    let lengthSquared = dx * dx + dy * dy
    guard lengthSquared > 1e-12 else { return hypot(point.x - start.x, point.y - start.y) }
    let ratio = min(max(((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared, 0), 1)
    return hypot(point.x - start.x - ratio * dx, point.y - start.y - ratio * dy)
}

struct CapabilityAction {
    let id: String
    let capability: String
    let operation: String
    let stockCode: String?
    let text: String?
    let color: String?
    let start: AnnotationPoint?
    let end: AnnotationPoint?
}

struct CapabilityResult {
    let success: Bool
    let message: String
}

@MainActor
final class AnnotationStore: ObservableObject {
    @Published private var storage: [String: [ChartAnnotation]] = [:]
    var onChange: ((String, String) -> Void)?
    private let fileURL: URL?
    private let maximumPerStock = 240
    private var activeViewports: [String: ChartAnnotationViewport] = [:]

    init() {
        let manager = FileManager.default
        if let root = try? manager.url(for: .applicationSupportDirectory,
                                       in: .userDomainMask,
                                       appropriateFor: nil,
                                       create: true) {
            let directory = root.appendingPathComponent("StocksNative", isDirectory: true)
            try? manager.createDirectory(at: directory, withIntermediateDirectories: true)
            fileURL = directory.appendingPathComponent("chart-annotations-v1.json")
        } else {
            fileURL = nil
        }
        if let fileURL,
           let data = try? Data(contentsOf: fileURL),
           let decoded = try? JSONDecoder().decode([String: [ChartAnnotation]].self, from: data) {
            storage = decoded
        }
    }

    func annotations(for stockCode: String) -> [ChartAnnotation] {
        storage[stockCode] ?? []
    }

    func legacyAnnotationCount(for stockCode: String) -> Int {
        annotations(for: stockCode).filter { $0.period == nil || $0.dataAnchors == nil }.count
    }

    func setActiveViewport(_ viewport: ChartAnnotationViewport?, for stockCode: String) {
        activeViewports[stockCode] = viewport?.isUsable == true ? viewport : nil
    }

    func clearActiveViewport(_ viewport: ChartAnnotationViewport?, for stockCode: String) {
        if activeViewports[stockCode] == viewport { activeViewports.removeValue(forKey: stockCode) }
    }

    fileprivate func projectedAnnotations(for stockCode: String,
                                          viewport: ChartAnnotationViewport?) -> [ProjectedAnnotation] {
        guard let viewport = viewport ?? activeViewports[stockCode], viewport.isUsable else { return [] }
        var indices: [String: Int] = [:]
        for (index, time) in viewport.times.enumerated() where indices[time] == nil { indices[time] = index }
        return annotations(for: stockCode).compactMap { annotation in
            guard annotation.period == viewport.period,
                  let anchors = annotation.dataAnchors,
                  anchors.count == annotation.points.count, !anchors.isEmpty else { return nil }
            var points: [CGPoint] = []
            for anchor in anchors {
                guard let index = indices[anchor.time], anchor.indexOffset.isFinite, anchor.price.isFinite else { return nil }
                let x = (Double(index) + anchor.indexOffset - viewport.xDomain.lowerBound) /
                    (viewport.xDomain.upperBound - viewport.xDomain.lowerBound)
                let y = (viewport.yDomain.upperBound - anchor.price) /
                    (viewport.yDomain.upperBound - viewport.yDomain.lowerBound)
                guard x.isFinite, y.isFinite else { return nil }
                points.append(CGPoint(x: x, y: y))
            }
            if annotation.kind == .note {
                guard let point = points.first, containsPlotPoint(point) else { return nil }
                return ProjectedAnnotation(annotation: annotation, points: points, visibleSegments: [])
            }
            let segments = zip(points, points.dropFirst()).compactMap { clippedPlotSegment($0.0, $0.1) }
            guard !segments.isEmpty else { return nil }
            return ProjectedAnnotation(annotation: annotation, points: points, visibleSegments: segments)
        }
    }

    func voiceContext(stockCode: String, editing: Bool, tool: AnnotationTool,
                      viewport: ChartAnnotationViewport? = nil) -> VoiceAnnotationContext {
        let values = projectedAnnotations(for: stockCode, viewport: viewport)
        let structured = values.filter { $0.annotation.kind != .pen }
        let items = structured.suffix(3).map {
            VoiceAnnotationItem(id: $0.annotation.id.uuidString, kind: $0.annotation.kind.rawValue,
                                text: $0.annotation.text.map { String($0.prefix(80)) }, color: $0.annotation.color,
                                start: $0.voiceStart, end: $0.voiceEnd)
        }
        return VoiceAnnotationContext(stockCode: stockCode, surfaceID: "chart-overlay",
                                      editing: editing, tool: tool.rawValue,
                                      structuredCount: structured.count,
                                      freehandStrokeCount: values.count - structured.count,
                                      selectionSupported: false, items: items)
    }

    @discardableResult
    func add(_ annotation: ChartAnnotation, to stockCode: String,
             viewport: ChartAnnotationViewport? = nil) -> Bool {
        guard let viewport = viewport ?? activeViewports[stockCode], viewport.isUsable,
              !annotation.points.isEmpty else { return false }
        let anchors = annotation.points.compactMap { viewport.anchor(for: $0) }
        guard anchors.count == annotation.points.count else { return false }
        let anchored = ChartAnnotation(anchoring: annotation, in: viewport, anchors: anchors)
        var values = storage[stockCode] ?? []
        values.append(anchored)
        // Unlocated legacy drawings are retained until the user explicitly
        // clears or undoes them; adding new drawings never migrates/deletes them.
        var overflow = values.filter { $0.dataAnchors != nil }.count - maximumPerStock
        if overflow > 0 {
            values.removeAll { value in
                guard overflow > 0, value.dataAnchors != nil else { return false }
                overflow -= 1
                return true
            }
        }
        storage[stockCode] = values
        save()
        onChange?(stockCode, "添加\(annotation.kind == .pen ? "笔迹" : "标注")")
        return true
    }

    @discardableResult
    func undo(stockCode: String) -> Bool {
        guard var values = storage[stockCode], !values.isEmpty else { return false }
        values.removeLast()
        storage[stockCode] = values
        save()
        onChange?(stockCode, "撤销标注")
        return true
    }

    @discardableResult
    func clear(stockCode: String) -> Bool {
        guard !(storage[stockCode] ?? []).isEmpty else { return false }
        storage[stockCode] = []
        save()
        onChange?(stockCode, "清除标注")
        return true
    }

    @discardableResult
    func erase(near point: AnnotationPoint, stockCode: String,
               viewport: ChartAnnotationViewport? = nil) -> Bool {
        guard var values = storage[stockCode], !values.isEmpty else { return false }
        let target = CGPoint(x: point.x, y: point.y)
        var bestID: UUID?
        var bestDistance: CGFloat = 0.075
        for projected in projectedAnnotations(for: stockCode, viewport: viewport) {
            let distance: CGFloat
            if projected.annotation.kind == .note, let candidate = projected.points.first {
                distance = hypot(candidate.x - target.x, candidate.y - target.y)
            } else {
                distance = projected.visibleSegments.map { distanceToSegment(target, $0.0, $0.1) }.min() ?? .infinity
            }
            if distance <= bestDistance {
                bestDistance = distance
                bestID = projected.annotation.id
            }
        }
        guard let bestID, let bestIndex = values.firstIndex(where: { $0.id == bestID }) else { return false }
        values.remove(at: bestIndex)
        storage[stockCode] = values
        save()
        onChange?(stockCode, "擦除标注")
        return true
    }

    func perform(_ action: CapabilityAction, selectedStockCode: String?) -> CapabilityResult {
        guard action.capability == "chart.annotation" else {
            return CapabilityResult(success: false, message: "此版本不支持该界面能力。")
        }
        guard let code = action.stockCode ?? selectedStockCode, !code.isEmpty else {
            return CapabilityResult(success: false, message: "请先选择一只股票。")
        }
        let color = Self.allowedColor(action.color)
        if ["add_note", "add_line", "add_arrow"].contains(action.operation) {
            guard code == selectedStockCode, activeViewports[code]?.isUsable == true else {
                return CapabilityResult(success: false, message: "请先打开这只股票的图表，再添加标注。")
            }
        }
        switch action.operation {
        case "add_note":
            let value = (action.text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty, let start = action.start else {
                return CapabilityResult(success: false, message: "文字和位置不能为空。")
            }
            guard add(ChartAnnotation(kind: .note, points: [start], text: String(value.prefix(80)), color: color), to: code) else {
                return CapabilityResult(success: false, message: "当前图表坐标不可用，请重新打开图表。")
            }
            return CapabilityResult(success: true, message: "已在 \(code) 图表加入文字标注。")
        case "add_line", "add_arrow":
            guard let start = action.start, let end = action.end else {
                return CapabilityResult(success: false, message: "起点和终点不能为空。")
            }
            let kind: ChartAnnotation.Kind = action.operation == "add_line" ? .line : .arrow
            guard add(ChartAnnotation(kind: kind, points: [start, end], color: color), to: code) else {
                return CapabilityResult(success: false, message: "当前图表坐标不可用，请重新打开图表。")
            }
            return CapabilityResult(success: true, message: "已在 \(code) 图表加入\(kind == .line ? "直线" : "箭头")。")
        case "undo":
            let changed = undo(stockCode: code)
            return CapabilityResult(success: changed, message: changed ? "已撤销 \(code) 的上一条标注。" : "当前图表没有可撤销的标注。")
        case "clear":
            let changed = clear(stockCode: code)
            return CapabilityResult(success: changed, message: changed ? "已清除 \(code) 的全部标注。" : "当前图表没有标注。")
        default:
            return CapabilityResult(success: false, message: "不支持这个标注动作。")
        }
    }

    private static func allowedColor(_ color: String?) -> String {
        let value = color ?? "accent"
        return ["accent", "red", "orange", "blue"].contains(value) ? value : "accent"
    }

    private func save() {
        guard let fileURL, let data = try? JSONEncoder().encode(storage) else { return }
        try? data.write(to: fileURL, options: [.atomic])
    }
}

struct NativeAnnotationCanvas: View {
    @ObservedObject var store: AnnotationStore
    let stockCode: String
    let tool: AnnotationTool
    let color: String
    let isEditing: Bool
    let viewport: ChartAnnotationViewport?
    @State private var current: [AnnotationPoint] = []
    @State private var pendingNote: AnnotationPoint?
    @State private var noteText = ""
    @State private var showingNoteEditor = false
    @State private var registeredStockCode: String?
    @State private var registeredViewport: ChartAnnotationViewport?

    init(store: AnnotationStore, stockCode: String, tool: AnnotationTool, color: String,
         isEditing: Bool, viewport: ChartAnnotationViewport? = nil) {
        self.store = store
        self.stockCode = stockCode
        self.tool = tool
        self.color = color
        self.isEditing = isEditing
        self.viewport = viewport
    }

    var body: some View {
        GeometryReader { geometry in
            Canvas { context, size in
                context.clip(to: Path(CGRect(origin: .zero, size: size)))
                for projected in store.projectedAnnotations(for: stockCode, viewport: viewport) {
                    draw(projected.annotation, points: projected.points, in: &context, size: size)
                }
                if !current.isEmpty, tool != .eraser, tool != .note {
                    let kind: ChartAnnotation.Kind = tool == .line ? .line : (tool == .arrow ? .arrow : .pen)
                    draw(ChartAnnotation(kind: kind, points: current, color: color),
                         points: current.map { CGPoint(x: $0.x, y: $0.y) }, in: &context, size: size)
                }
            }
            .clipped()
            .contentShape(Rectangle())
            .allowsHitTesting(isEditing && viewport?.isUsable == true)
            .gesture(drawingGesture(size: geometry.size))
            .accessibilityLabel(isEditing ? "图表标注画布" : "图表标注")
            .accessibilityHint(isEditing ? "使用手指或 Apple Pencil 绘制" : "")
        }
        .onAppear { registerViewport() }
        .onChange(of: viewport) { _, _ in
            resetDrawing()
            registerViewport()
        }
        .onChange(of: stockCode) { _, _ in
            resetDrawing()
            registerViewport()
        }
        .onChange(of: isEditing) { _, _ in resetDrawing() }
        .onDisappear {
            if let registeredStockCode { store.clearActiveViewport(registeredViewport, for: registeredStockCode) }
        }
        .alert("添加文字标注", isPresented: $showingNoteEditor) {
            TextField("标注内容", text: $noteText)
            Button("取消", role: .cancel) { pendingNote = nil; noteText = "" }
            Button("添加") {
                let value = noteText.trimmingCharacters(in: .whitespacesAndNewlines)
                if let point = pendingNote, !value.isEmpty {
                    store.add(ChartAnnotation(kind: .note, points: [point], text: String(value.prefix(80)), color: color),
                              to: stockCode, viewport: viewport)
                }
                pendingNote = nil
                noteText = ""
            }
        }
    }

    private func registerViewport() {
        if let registeredStockCode { store.clearActiveViewport(registeredViewport, for: registeredStockCode) }
        store.setActiveViewport(viewport, for: stockCode)
        registeredStockCode = stockCode
        registeredViewport = viewport
    }

    private func resetDrawing() {
        current = []
        pendingNote = nil
        noteText = ""
        showingNoteEditor = false
    }

    private func drawingGesture(size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 0, coordinateSpace: .local)
            .onChanged { value in
                guard isEditing, viewport?.isUsable == true, size.width > 0, size.height > 0 else { return }
                let point = normalized(value.location, size: size)
                switch tool {
                case .pen:
                    if current.last.map({ hypot($0.x - point.x, $0.y - point.y) > 0.004 }) ?? true { current.append(point) }
                case .line, .arrow:
                    if current.isEmpty { current = [normalized(value.startLocation, size: size), point] }
                    else if current.count == 1 { current.append(point) }
                    else { current[1] = point }
                case .note, .eraser: break
                }
            }
            .onEnded { value in
                guard isEditing, viewport?.isUsable == true, size.width > 0, size.height > 0 else { current = []; return }
                let end = normalized(value.location, size: size)
                switch tool {
                case .pen:
                    if current.count > 1 { store.add(ChartAnnotation(kind: .pen, points: current, color: color), to: stockCode, viewport: viewport) }
                case .line:
                    if current.count == 2 { store.add(ChartAnnotation(kind: .line, points: current, color: color), to: stockCode, viewport: viewport) }
                case .arrow:
                    if current.count == 2 { store.add(ChartAnnotation(kind: .arrow, points: current, color: color), to: stockCode, viewport: viewport) }
                case .note:
                    pendingNote = end
                    showingNoteEditor = true
                case .eraser:
                    _ = store.erase(near: end, stockCode: stockCode, viewport: viewport)
                }
                current = []
            }
    }

    private func normalized(_ point: CGPoint, size: CGSize) -> AnnotationPoint {
        AnnotationPoint(x: point.x / size.width, y: point.y / size.height)
    }

    private func draw(_ annotation: ChartAnnotation, points normalizedPoints: [CGPoint],
                      in context: inout GraphicsContext, size: CGSize) {
        guard let first = normalizedPoints.first else { return }
        let tint = annotationColor(annotation.color)
        let points = normalizedPoints.map { CGPoint(x: $0.x * size.width, y: $0.y * size.height) }
        switch annotation.kind {
        case .pen:
            guard points.count > 1 else { return }
            var path = Path()
            path.move(to: points[0])
            points.dropFirst().forEach { path.addLine(to: $0) }
            context.stroke(path, with: .color(tint), style: StrokeStyle(lineWidth: 2.6, lineCap: .round, lineJoin: .round))
        case .line, .arrow:
            guard let end = points.last, points.count > 1 else { return }
            var path = Path()
            path.move(to: points[0])
            path.addLine(to: end)
            if annotation.kind == .arrow {
                let angle = atan2(end.y - points[0].y, end.x - points[0].x)
                let length: CGFloat = 12
                path.move(to: end)
                path.addLine(to: CGPoint(x: end.x - length * cos(angle - .pi / 6), y: end.y - length * sin(angle - .pi / 6)))
                path.move(to: end)
                path.addLine(to: CGPoint(x: end.x - length * cos(angle + .pi / 6), y: end.y - length * sin(angle + .pi / 6)))
            }
            context.stroke(path, with: .color(tint), style: StrokeStyle(lineWidth: 2.2, lineCap: .round, lineJoin: .round))
        case .note:
            let origin = CGPoint(x: first.x * size.width, y: first.y * size.height)
            let value = annotation.text ?? "标注"
            let width = min(max(CGFloat(value.count) * 13 + 22, 76), 220)
            let bubble = CGRect(x: min(origin.x, max(0, size.width - width)),
                                y: min(origin.y, max(0, size.height - 34)), width: width, height: 30)
            context.fill(Path(roundedRect: bubble, cornerRadius: 8), with: .color(tint.opacity(0.92)))
            let text = context.resolve(Text(value).font(.caption2.weight(.semibold)).foregroundStyle(.white))
            context.draw(text, at: CGPoint(x: bubble.minX + 10, y: bubble.midY), anchor: .leading)
        }
    }

    private func annotationColor(_ name: String) -> Color {
        switch name {
        case "red": return AppStyle.up
        case "orange": return .orange
        case "blue": return .blue
        default: return AppStyle.accent
        }
    }
}

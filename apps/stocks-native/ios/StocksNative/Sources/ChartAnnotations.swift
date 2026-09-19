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

struct ChartAnnotation: Codable, Identifiable {
    enum Kind: String, Codable { case pen, line, arrow, note }

    let id: UUID
    let kind: Kind
    let points: [AnnotationPoint]
    let text: String?
    let color: String
    let createdAt: Date

    init(kind: Kind, points: [AnnotationPoint], text: String? = nil, color: String = "accent") {
        id = UUID()
        self.kind = kind
        self.points = points
        self.text = text
        self.color = color
        createdAt = Date()
    }
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
    private let fileURL: URL?
    private let maximumPerStock = 240

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

    func add(_ annotation: ChartAnnotation, to stockCode: String) {
        var values = storage[stockCode] ?? []
        values.append(annotation)
        if values.count > maximumPerStock { values.removeFirst(values.count - maximumPerStock) }
        storage[stockCode] = values
        save()
    }

    @discardableResult
    func undo(stockCode: String) -> Bool {
        guard var values = storage[stockCode], !values.isEmpty else { return false }
        values.removeLast()
        storage[stockCode] = values
        save()
        return true
    }

    @discardableResult
    func clear(stockCode: String) -> Bool {
        guard !(storage[stockCode] ?? []).isEmpty else { return false }
        storage[stockCode] = []
        save()
        return true
    }

    @discardableResult
    func erase(near point: AnnotationPoint, stockCode: String) -> Bool {
        guard var values = storage[stockCode], !values.isEmpty else { return false }
        let threshold = 0.075
        var bestIndex: Int?
        var bestDistance = threshold
        for (index, annotation) in values.enumerated() {
            for candidate in annotation.points {
                let distance = hypot(candidate.x - point.x, candidate.y - point.y)
                if distance <= bestDistance {
                    bestDistance = distance
                    bestIndex = index
                }
            }
        }
        guard let bestIndex else { return false }
        values.remove(at: bestIndex)
        storage[stockCode] = values
        save()
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
        switch action.operation {
        case "add_note":
            let value = (action.text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty, let start = action.start else {
                return CapabilityResult(success: false, message: "文字和位置不能为空。")
            }
            add(ChartAnnotation(kind: .note, points: [start], text: String(value.prefix(80)), color: color), to: code)
            return CapabilityResult(success: true, message: "已在 \(code) 图表加入文字标注。")
        case "add_line", "add_arrow":
            guard let start = action.start, let end = action.end else {
                return CapabilityResult(success: false, message: "起点和终点不能为空。")
            }
            let kind: ChartAnnotation.Kind = action.operation == "add_line" ? .line : .arrow
            add(ChartAnnotation(kind: kind, points: [start, end], color: color), to: code)
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
    @State private var current: [AnnotationPoint] = []
    @State private var pendingNote: AnnotationPoint?
    @State private var noteText = ""
    @State private var showingNoteEditor = false

    var body: some View {
        GeometryReader { geometry in
            Canvas { context, size in
                for annotation in store.annotations(for: stockCode) {
                    draw(annotation, in: &context, size: size)
                }
                if !current.isEmpty, tool != .eraser, tool != .note {
                    let kind: ChartAnnotation.Kind = tool == .line ? .line : (tool == .arrow ? .arrow : .pen)
                    draw(ChartAnnotation(kind: kind, points: current, color: color), in: &context, size: size)
                }
            }
            .contentShape(Rectangle())
            .allowsHitTesting(isEditing)
            .gesture(drawingGesture(size: geometry.size))
            .accessibilityLabel(isEditing ? "图表标注画布" : "图表标注")
            .accessibilityHint(isEditing ? "使用手指或 Apple Pencil 绘制" : "")
        }
        .alert("添加文字标注", isPresented: $showingNoteEditor) {
            TextField("标注内容", text: $noteText)
            Button("取消", role: .cancel) { pendingNote = nil; noteText = "" }
            Button("添加") {
                let value = noteText.trimmingCharacters(in: .whitespacesAndNewlines)
                if let point = pendingNote, !value.isEmpty {
                    store.add(ChartAnnotation(kind: .note, points: [point], text: String(value.prefix(80)), color: color), to: stockCode)
                }
                pendingNote = nil
                noteText = ""
            }
        }
    }

    private func drawingGesture(size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 0, coordinateSpace: .local)
            .onChanged { value in
                guard isEditing, size.width > 0, size.height > 0 else { return }
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
                guard isEditing, size.width > 0, size.height > 0 else { current = []; return }
                let end = normalized(value.location, size: size)
                switch tool {
                case .pen:
                    if current.count > 1 { store.add(ChartAnnotation(kind: .pen, points: current, color: color), to: stockCode) }
                case .line:
                    if current.count == 2 { store.add(ChartAnnotation(kind: .line, points: current, color: color), to: stockCode) }
                case .arrow:
                    if current.count == 2 { store.add(ChartAnnotation(kind: .arrow, points: current, color: color), to: stockCode) }
                case .note:
                    pendingNote = end
                    showingNoteEditor = true
                case .eraser:
                    _ = store.erase(near: end, stockCode: stockCode)
                }
                current = []
            }
    }

    private func normalized(_ point: CGPoint, size: CGSize) -> AnnotationPoint {
        AnnotationPoint(x: point.x / size.width, y: point.y / size.height)
    }

    private func draw(_ annotation: ChartAnnotation, in context: inout GraphicsContext, size: CGSize) {
        guard let first = annotation.points.first else { return }
        let tint = annotationColor(annotation.color)
        let points = annotation.points.map { CGPoint(x: $0.x * size.width, y: $0.y * size.height) }
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

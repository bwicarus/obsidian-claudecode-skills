import PencilKit
import SwiftUI
import UIKit

extension NativePencilGestureMapping {
    var displayName: String {
        switch self {
        case .followSystem:
            return "跟随 iPad 设置"
        case .toggleEraser:
            return "切换画笔与橡皮"
        case .toggleSelection:
            return "切换画笔与选区笔"
        case .showPalette:
            return "显示绘图工具"
        case .disabled:
            return "不执行操作"
        }
    }
}

@MainActor
struct NativePencilSettingsSection: View {
    @ObservedObject private var settings: NativePencilSettings

    init(settings: NativePencilSettings = .shared) {
        self.settings = settings
    }

    var body: some View {
        Section("Apple Pencil") {
            Picker("双击", selection: mappingBinding(for: \.doubleTap)) {
                ForEach(NativePencilGestureMapping.allCases) { mapping in
                    Text(mapping.displayName).tag(mapping.rawValue)
                }
            }
            Picker("挤压", selection: mappingBinding(for: \.squeeze)) {
                ForEach(NativePencilGestureMapping.allCases) { mapping in
                    Text(mapping.displayName).tag(mapping.rawValue)
                }
            }
            Text("“跟随 iPad 设置”会尊重系统里为 Apple Pencil 选择的动作；这些设置只改变阅读器中的手势，不改变网页墨迹格式。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func mappingBinding(
        for keyPath: ReferenceWritableKeyPath<
            NativePencilSettings,
            NativePencilGestureMapping
        >
    ) -> Binding<String> {
        Binding(
            get: { settings[keyPath: keyPath].rawValue },
            set: { rawValue in
                guard let mapping = NativePencilGestureMapping(
                    rawValue: rawValue
                ) else { return }
                settings[keyPath: keyPath] = mapping
            }
        )
    }
}

@MainActor
final class NativePencilAnnotationSession: ObservableObject {
    let sourceImage: UIImage
    @Published var drawing = PKDrawing()
    @Published var canvasSize: CGSize = .zero

    init(sourceImage: UIImage) {
        self.sourceImage = sourceImage
    }

    func clear() {
        drawing = PKDrawing()
    }

    func renderedImage() -> UIImage? {
        guard canvasSize.width >= 1, canvasSize.height >= 1 else {
            return nil
        }
        let bounds = CGRect(origin: .zero, size: canvasSize)
        let format = UIGraphicsImageRendererFormat()
        format.scale = UIScreen.main.scale
        format.opaque = true
        let renderer = UIGraphicsImageRenderer(size: canvasSize, format: format)
        return renderer.image { context in
            context.cgContext.setFillColor(UIColor.black.cgColor)
            context.cgContext.fill(bounds)
            sourceImage.draw(in: Self.aspectFitRect(
                imageSize: sourceImage.size,
                bounds: bounds
            ))
            drawing.image(from: bounds, scale: format.scale).draw(in: bounds)
        }
    }

    private static func aspectFitRect(
        imageSize: CGSize,
        bounds: CGRect
    ) -> CGRect {
        guard imageSize.width > 0, imageSize.height > 0 else {
            return bounds
        }
        let scale = min(
            bounds.width / imageSize.width,
            bounds.height / imageSize.height
        )
        let size = CGSize(
            width: imageSize.width * scale,
            height: imageSize.height * scale
        )
        return CGRect(
            x: bounds.midX - size.width / 2,
            y: bounds.midY - size.height / 2,
            width: size.width,
            height: size.height
        )
    }
}

struct NativePencilAnnotationEditor: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var session: NativePencilAnnotationSession
    @State private var selectedTool = NativePencilAnnotationTool.pen
    @State private var selectedColorHex = "#ff3b30"
    @State private var selectedWidth: CGFloat = 4
    let onSave: (UIImage) -> Void

    private let colors = ["#ff3b30", "#007aff", "#111111", "#34c759"]

    init(image: UIImage, onSave: @escaping (UIImage) -> Void) {
        _session = StateObject(
            wrappedValue: NativePencilAnnotationSession(sourceImage: image)
        )
        self.onSave = onSave
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                GeometryReader { proxy in
                    ZStack {
                        Color.black
                        Image(uiImage: session.sourceImage)
                            .resizable()
                            .scaledToFit()
                        NativePencilCanvas(
                            drawing: $session.drawing,
                            canvasSize: $session.canvasSize,
                            tool: selectedTool,
                            colorHex: selectedColorHex,
                            width: selectedWidth
                        )
                    }
                    .onAppear {
                        session.canvasSize = proxy.size
                    }
                    .onChange(of: proxy.size) { _, value in
                        session.canvasSize = value
                    }
                }

                HStack(spacing: 12) {
                    Button {
                        selectedTool = .pen
                    } label: {
                        Image(systemName: "pencil.tip")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(selectedTool == .pen ? .blue : .gray)

                    Button {
                        selectedTool = .eraser
                    } label: {
                        Image(systemName: "eraser")
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(selectedTool == .eraser ? .blue : .gray)

                    ForEach(colors, id: \.self) { color in
                        Button {
                            selectedColorHex = color
                            selectedTool = .pen
                        } label: {
                            Circle()
                                .fill(Color(uiColor: UIColor(bwHex: color)))
                                .frame(width: 24, height: 24)
                                .overlay {
                                    if selectedColorHex == color {
                                        Circle().stroke(.white, lineWidth: 2)
                                    }
                                }
                        }
                        .buttonStyle(.plain)
                    }

                    Image(systemName: "line.diagonal")
                    Slider(value: $selectedWidth, in: 1...16)
                        .frame(maxWidth: 180)
                    Text("\(Int(selectedWidth))")
                        .font(.caption.monospacedDigit())
                        .frame(width: 24)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(.ultraThinMaterial)
            }
            .background(Color.black.ignoresSafeArea())
            .navigationTitle("标注当前视口")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .topBarLeading) {
                    Button("清空") { session.clear() }
                        .disabled(session.drawing.strokes.isEmpty)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        guard let image = session.renderedImage() else { return }
                        onSave(image)
                        dismiss()
                    }
                }
            }
        }
    }
}

private enum NativePencilAnnotationTool: Equatable {
    case pen
    case eraser
}

private struct NativePencilCanvas: UIViewRepresentable {
    @Binding var drawing: PKDrawing
    @Binding var canvasSize: CGSize
    let tool: NativePencilAnnotationTool
    let colorHex: String
    let width: CGFloat

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeUIView(context: Context) -> PKCanvasView {
        let canvas = PKCanvasView()
        canvas.delegate = context.coordinator
        canvas.backgroundColor = .clear
        canvas.isOpaque = false
        canvas.drawingPolicy = .pencilOnly
        context.coordinator.applySelectedTool(
            to: canvas,
            tool: tool,
            colorHex: colorHex,
            width: width
        )
        return canvas
    }

    func updateUIView(_ canvas: PKCanvasView, context: Context) {
        context.coordinator.parent = self
        if canvas.drawing.dataRepresentation() != drawing.dataRepresentation() {
            canvas.drawing = drawing
        }
        if canvasSize != canvas.bounds.size, !canvas.bounds.isEmpty {
            DispatchQueue.main.async {
                canvasSize = canvas.bounds.size
            }
        }
        context.coordinator.applySelectedTool(
            to: canvas,
            tool: tool,
            colorHex: colorHex,
            width: width
        )
    }

    final class Coordinator: NSObject, PKCanvasViewDelegate {
        var parent: NativePencilCanvas
        private var appliedTool: NativePencilAnnotationTool?
        private var appliedColorHex = ""
        private var appliedWidth: CGFloat = 0

        init(parent: NativePencilCanvas) {
            self.parent = parent
        }

        func applySelectedTool(
            to canvas: PKCanvasView,
            tool: NativePencilAnnotationTool,
            colorHex: String,
            width requestedWidth: CGFloat
        ) {
            let width = max(1, min(requestedWidth, 16))
            guard
                appliedTool != tool
                    || appliedColorHex != colorHex
                    || appliedWidth != width
            else {
                return
            }
            appliedTool = tool
            appliedColorHex = colorHex
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
            }
        }

        func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
            parent.drawing = canvasView.drawing
            parent.canvasSize = canvasView.bounds.size
        }
    }
}

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
    let onSave: (UIImage) -> Void

    init(image: UIImage, onSave: @escaping (UIImage) -> Void) {
        _session = StateObject(
            wrappedValue: NativePencilAnnotationSession(sourceImage: image)
        )
        self.onSave = onSave
    }

    var body: some View {
        NavigationStack {
            GeometryReader { proxy in
                ZStack {
                    Color.black
                    Image(uiImage: session.sourceImage)
                        .resizable()
                        .scaledToFit()
                    NativePencilCanvas(
                        drawing: $session.drawing,
                        canvasSize: $session.canvasSize
                    )
                }
                .onAppear {
                    session.canvasSize = proxy.size
                }
                .onChange(of: proxy.size) { _, value in
                    session.canvasSize = value
                }
            }
            .ignoresSafeArea(edges: .bottom)
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

private struct NativePencilCanvas: UIViewRepresentable {
    @Binding var drawing: PKDrawing
    @Binding var canvasSize: CGSize

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeUIView(context: Context) -> PKCanvasView {
        let canvas = PKCanvasView()
        canvas.delegate = context.coordinator
        canvas.backgroundColor = .clear
        canvas.isOpaque = false
        canvas.drawingPolicy = .pencilOnly
        canvas.tool = PKInkingTool(.pen, color: .systemRed, width: 4)
        context.coordinator.attachToolPicker(to: canvas)
        return canvas
    }

    func updateUIView(_ canvas: PKCanvasView, context: Context) {
        if canvas.drawing.dataRepresentation() != drawing.dataRepresentation() {
            canvas.drawing = drawing
        }
        if canvasSize != canvas.bounds.size, !canvas.bounds.isEmpty {
            DispatchQueue.main.async {
                canvasSize = canvas.bounds.size
            }
        }
        context.coordinator.attachToolPicker(to: canvas)
    }

    final class Coordinator: NSObject, PKCanvasViewDelegate {
        var parent: NativePencilCanvas
        private let toolPicker = PKToolPicker()
        private weak var attachedCanvas: PKCanvasView?

        init(parent: NativePencilCanvas) {
            self.parent = parent
        }

        func attachToolPicker(to canvas: PKCanvasView) {
            guard attachedCanvas !== canvas || !canvas.isFirstResponder else {
                return
            }
            DispatchQueue.main.async { [weak self, weak canvas] in
                guard let self, let canvas, canvas.window != nil else { return }
                self.attachedCanvas = canvas
                self.toolPicker.addObserver(canvas)
                self.toolPicker.setVisible(true, forFirstResponder: canvas)
                canvas.becomeFirstResponder()
            }
        }

        func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
            parent.drawing = canvasView.drawing
            parent.canvasSize = canvasView.bounds.size
        }
    }
}

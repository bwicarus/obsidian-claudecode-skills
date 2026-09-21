import SwiftUI
import UIKit
import ImageIO

struct ReaderNativeImageItem: Identifiable {
    let id: String
    let title: String
    let source: String
    let sourceURL: URL?
    let selected: Bool
    let isMap: Bool
    let selectID: String
    let removeID: String

    init?(_ value: [String: Any]) {
        guard let id = value["mediaID"] as? String else { return nil }
        self.id = id
        title = value["title"] as? String ?? ""
        source = value["source"] as? String ?? ""
        let url = URL(string: value["sourceURL"] as? String ?? "")
        sourceURL = url?.scheme == "https" ? url : nil
        selected = value["selected"] as? Bool ?? false
        isMap = value["isMap"] as? Bool ?? false
        selectID = value["selectID"] as? String ?? ""
        removeID = value["removeID"] as? String ?? ""
    }
}

@MainActor
struct ReaderNativeImageCard: View {
    let item: ReaderNativeImageItem
    @ObservedObject var model: ReaderNativeConversationModel
    @State private var image: UIImage?
    @State private var failed = false
    @State private var viewing = false
    @State private var attempt = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let image {
                Button { viewing = true } label: {
                    Image(uiImage: image).resizable().scaledToFit()
                        .frame(maxWidth: .infinity, maxHeight: 230)
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                }.buttonStyle(.plain).accessibilityLabel("查看图片：\(item.title)")
            } else if failed {
                VStack(spacing: 8) {
                    Label("图片暂时无法读取", systemImage: "photo.badge.exclamationmark")
                    Button("重新加载") { attempt += 1 }
                }.font(.caption).frame(maxWidth: .infinity, minHeight: 96)
            } else {
                ProgressView().frame(maxWidth: .infinity, minHeight: 96)
            }
            if !item.title.isEmpty { Text(item.title).font(.subheadline).textSelection(.enabled) }
            HStack(spacing: 12) {
                if !item.selectID.isEmpty {
                    Button {
                        Task { await model.perform("liveAction", parameters: ["actionId": item.selectID]) }
                    } label: {
                        Label(item.selected ? "已带入" : "带入对话", systemImage: item.selected ? "checkmark.circle.fill" : "plus.bubble")
                    }.disabled(model.isPerforming("liveAction"))
                }
                Spacer(minLength: 0)
                if let url = item.sourceURL {
                    Link(item.source.isEmpty ? "来源" : item.source, destination: url).lineLimit(1)
                }
                if !item.removeID.isEmpty {
                    Button(role: .destructive) {
                        Task { await model.perform("liveAction", parameters: ["actionId": item.removeID]) }
                    } label: { Image(systemName: "xmark.circle") }
                        .accessibilityLabel("移除这张图片")
                        .disabled(model.isPerforming("liveAction"))
                }
            }.font(.caption).buttonStyle(.borderless)
            if item.isMap {
                Text("地图交互尚待迁移，目前可查看原图。")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .task(id: "\(model.scope):\(item.id):\(attempt)") {
            failed = false
            do {
                let bytes = try await model.imageData(item.id)
                try Task.checkCancellation()
                guard let source = CGImageSourceCreateWithData(bytes as CFData, nil),
                      let thumbnail = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                        kCGImageSourceCreateThumbnailFromImageAlways: true,
                        kCGImageSourceCreateThumbnailWithTransform: true,
                        kCGImageSourceThumbnailMaxPixelSize: 2_400
                      ] as CFDictionary) else { throw URLError(.cannotDecodeContentData) }
                image = UIImage(cgImage: thumbnail)
            } catch is CancellationError { }
            catch { if !Task.isCancelled { failed = true } }
        }
        .sheet(isPresented: $viewing) {
            NavigationStack {
                if let image { ReaderNativeZoomImage(image: image).background(Color.black) }
            }
            .overlay(alignment: .topTrailing) {
                Button("完成") { viewing = false }
                    .buttonStyle(.borderedProminent).padding()
            }
        }
    }
}

/// UIScrollView owns pinch and pan; the card itself never adds nested vertical
/// scrolling to the conversation. Double-tap switches fit / detail size.
@MainActor
private struct ReaderNativeZoomImage: UIViewRepresentable {
    let image: UIImage
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> ZoomScrollView {
        let view = ZoomScrollView()
        view.delegate = context.coordinator
        view.minimumZoomScale = 1
        view.maximumZoomScale = 6
        view.imageView.image = image
        view.addSubview(view.imageView)
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.doubleTap(_:)))
        tap.numberOfTapsRequired = 2
        view.addGestureRecognizer(tap)
        return view
    }
    func updateUIView(_ uiView: ZoomScrollView, context: Context) { }

    final class Coordinator: NSObject, UIScrollViewDelegate {
        func viewForZooming(in scrollView: UIScrollView) -> UIView? { (scrollView as? ZoomScrollView)?.imageView }
        @objc func doubleTap(_ recognizer: UITapGestureRecognizer) {
            guard let view = recognizer.view as? ZoomScrollView else { return }
            if view.zoomScale > 1.01 { view.setZoomScale(1, animated: true) }
            else {
                let point = recognizer.location(in: view.imageView)
                let size = CGSize(width: view.bounds.width / 3, height: view.bounds.height / 3)
                view.zoom(to: CGRect(x: point.x - size.width / 2, y: point.y - size.height / 2,
                                     width: size.width, height: size.height), animated: true)
            }
        }
    }

    final class ZoomScrollView: UIScrollView {
        let imageView = UIImageView()
        private var fittedSize = CGSize.zero
        override func layoutSubviews() {
            super.layoutSubviews()
            guard bounds.size != fittedSize, bounds.width > 0, bounds.height > 0 else { return }
            fittedSize = bounds.size
            setZoomScale(1, animated: false)
            imageView.contentMode = .scaleAspectFit
            imageView.frame = CGRect(origin: .zero, size: bounds.size)
            contentSize = bounds.size
        }
    }
}

import SwiftUI
import UIKit

/// 钉在 PDF 页上的卡片层 —— **跟 PDF 同一帧滚动、同一帧缩放**。
///
/// 为什么需要它：原来卡片画在阅读区之上的一层 SwiftUI 里，按窗口坐标摆，
/// 靠 `geometryRevision` 在滚动之后重排。SwiftUI 的重排总在下一帧，于是滚动时卡片
/// 永远慢一帧 —— 用户看到的就是残影（2026-09-22、09-23 连报："卡片还是会随着
/// 滚动留下残影"、"没有真正嵌入画面"）。
///
/// 做法：卡片按 **PDF 文档视图的坐标**摆（这组坐标不随滚动变化），宿主视图在
/// PDF 滚动视图的 KVO 回调里**同步**改自己的平移与缩放，跟 PDFKit 挪页面是同一次
/// 提交。滚动期间 SwiftUI 什么都不用重算。
///
/// ⚠ 它**不在** PDF 的滚动视图里面，而是叠在 PDFView 之上的兄弟层 —— 放进滚动视图
///   的话，卡片的拖动手势会跟翻页滚动抢。触摸只在卡片范围内接住，其余一律放行给 PDF。
struct ReaderNativeDocumentCardLayer: UIViewControllerRepresentable {
    let document: ReaderNativePDFDocument
    let reader: ReaderWebViewModel
    let model: ReaderNativeConversationModel

    func makeUIViewController(context: Context) -> ReaderNativeDocumentCardController {
        ReaderNativeDocumentCardController(document: document, reader: reader, model: model)
    }

    static func dismantleUIViewController(_ controller: ReaderNativeDocumentCardController, coordinator: ()) {
        controller.unmount()
    }

    func updateUIViewController(_ controller: ReaderNativeDocumentCardController, context: Context) {
        controller.follow()
    }
}

/// 文档层的共享状态：当前缩放、可视区大小（都按文档坐标），以及卡片占了哪些地方（接触摸用）。
@MainActor
final class ReaderNativeDocumentLayerState: ObservableObject {
    @Published var scale: CGFloat = 1
    @Published var viewport: CGSize = .zero
    /// 各张卡当前的框（文档坐标），由 SwiftUI 那边用 preference 报上来。
    var cardFrames: [CGRect] = []
    /// 文档坐标 → 窗口坐标。
    var toWindow: (CGPoint) -> CGPoint = { $0 }
}

@MainActor
final class ReaderNativeDocumentCardController: UIViewController {
    private let document: ReaderNativePDFDocument
    private weak var reader: ReaderWebViewModel?
    private let state: ReaderNativeDocumentLayerState
    private let hosting: UIHostingController<ReaderNativeAnchoredCards>
    private weak var observedScroll: UIScrollView?
    private var observations: [NSKeyValueObservation] = []

    init(document: ReaderNativePDFDocument, reader: ReaderWebViewModel, model: ReaderNativeConversationModel) {
        self.document = document
        self.reader = reader
        // ⚠ 先建局部变量：super.init 之前不能读 self.state。
        let state = ReaderNativeDocumentLayerState()
        self.state = state
        hosting = UIHostingController(rootView: ReaderNativeAnchoredCards(
            reader: reader, model: model, document: document, layer: state))
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { return nil }

    override func loadView() {
        let container = ReaderNativeDocumentCardContainer()
        container.frames = { [weak self] in self?.state.cardFrames ?? [] }
        view = container
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .clear
        view.clipsToBounds = true
        addChild(hosting)
        hosting.view.backgroundColor = .clear
        if #available(iOS 16.4, *) { hosting.safeAreaRegions = [] }
        hosting.view.translatesAutoresizingMaskIntoConstraints = true
        hosting.view.autoresizingMask = []
        // 锚点放在左上角：position 就是文档原点在本层里的位置，transform 只管缩放。
        hosting.view.layer.anchorPoint = .zero
        view.addSubview(hosting.view)
        hosting.didMove(toParent: self)
        (view as? ReaderNativeDocumentCardContainer)?.target = hosting.view
        state.toWindow = { [weak self] point in
            guard let target = self?.hosting.view else { return point }
            return target.convert(point, to: nil)
        }
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        if reader?.documentCardLayerMounted == false { reader?.documentCardLayerMounted = true }
    }

    func unmount() {
        observations = []
        if reader?.documentCardLayerMounted == true { reader?.documentCardLayerMounted = false }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        follow()
    }

    /// 找到 PDF 的滚动视图并**同步**跟随它。
    ///
    /// ⚠ 回调里必须同步 sync()，不能 `Task { @MainActor in … }` —— 那会推到下一轮，
    ///   正好又是慢一帧（ReaderNativePDFDocument 自己的几何监听就是那么写的，
    ///   对它无所谓，对这一层就是残影）。
    func follow() {
        if let scroll = document.view.documentView?.superview as? UIScrollView, scroll !== observedScroll {
            observedScroll = scroll
            let update: (UIScrollView) -> Void = { [weak self] _ in
                MainActor.assumeIsolated { self?.sync() }
            }
            observations = [
                scroll.observe(\.contentOffset, options: [.new]) { scroll, _ in update(scroll) },
                scroll.observe(\.zoomScale, options: [.new]) { scroll, _ in update(scroll) },
                scroll.observe(\.contentSize, options: [.new]) { scroll, _ in update(scroll) },
                scroll.observe(\.bounds, options: [.new]) { scroll, _ in update(scroll) },
            ]
        }
        sync()
    }

    private func sync() {
        guard isViewLoaded, let content = document.view.documentView else { return }
        let size = content.bounds.size
        guard size.width > 0, size.height > 0 else { return }
        let origin = content.convert(CGPoint(x: content.bounds.minX, y: content.bounds.minY), to: view)
        let unit = content.convert(CGRect(x: content.bounds.minX, y: content.bounds.minY, width: 1000, height: 1000),
                                   to: view)
        let scale = max(0.01, unit.width / 1000)
        if hosting.view.bounds.size != size { hosting.view.bounds = CGRect(origin: .zero, size: size) }
        hosting.view.layer.position = origin
        hosting.view.transform = CGAffineTransform(scaleX: scale, y: scale)
        // 下面两项只在变了时才写（它们是 @Published，写了就触发 SwiftUI 重算）。
        if abs(state.scale - scale) > 0.001 { state.scale = scale }
        let viewport = CGSize(width: view.bounds.width / scale, height: view.bounds.height / scale)
        if abs(state.viewport.width - viewport.width) > 0.5 || abs(state.viewport.height - viewport.height) > 0.5 {
            state.viewport = viewport
        }
    }
}

/// 只在卡片上接触摸，其余放行给下面的 PDF（选字、翻页、点锁定框）。
///
/// ⚠ 不能靠"命中的是不是宿主视图本身"来判断：SwiftUI 的内容不是 UIView，
///   点在卡上和点在空白处，`hitTest` 返回的都是宿主视图。只能按卡片的框判断。
private final class ReaderNativeDocumentCardContainer: UIView {
    weak var target: UIView?
    var frames: () -> [CGRect] = { [] }

    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        guard let target else { return false }
        let local = convert(point, to: target)
        return frames().contains { $0.insetBy(dx: -4, dy: -4).contains(local) }
    }
}

/// 文档层里的卡片（坐标 = PDF 文档视图坐标）。
@MainActor
struct ReaderNativeAnchoredCards: View {
    @ObservedObject var reader: ReaderWebViewModel
    @ObservedObject var model: ReaderNativeConversationModel
    @ObservedObject var document: ReaderNativePDFDocument
    @ObservedObject var layer: ReaderNativeDocumentLayerState

    static let space = "reader-doc"

    var body: some View {
        // 缩放/重排后位置会变，要重算 —— 读一次 geometryRevision 订阅它。
        // （纯滚动时算出来的位置不变，SwiftUI 比较后什么都不会动。）
        let _ = document.geometryRevision
        ZStack(alignment: .topLeading) {
            Color.clear
            ForEach(model.placements) { item in
                card(item)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .coordinateSpace(name: Self.space)
        .onPreferenceChange(ReaderNativeCardFramesKey.self) { frames in
            layer.cardFrames = frames
        }
    }

    @ViewBuilder
    private func card(_ item: ReaderNativePagePlacement) -> some View {
        if item.visible, !item.floating,
           let rect = reader.nativePageCardDocumentRect(id: item.noteID, size: item.size) {
            ReaderNativePlacedCard(item: item, reader: reader, model: model, rect: rect,
                                   available: layer.viewport, space: .named(Self.space),
                                   unitScale: layer.scale, toWindow: layer.toWindow)
                .background(GeometryReader { proxy in
                    Color.clear.preference(key: ReaderNativeCardFramesKey.self,
                                           value: [proxy.frame(in: .named(Self.space))])
                })
                .offset(x: rect.minX, y: rect.minY)
        }
    }
}

struct ReaderNativeCardFramesKey: PreferenceKey {
    static let defaultValue: [CGRect] = []
    static func reduce(value: inout [CGRect], nextValue: () -> [CGRect]) {
        value.append(contentsOf: nextValue())
    }
}

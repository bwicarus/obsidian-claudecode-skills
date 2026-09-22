import SwiftUI

struct ReaderNativePagePlacement: Identifiable {
    let id: String
    let title: String
    let rect: CGRect
    let bound: Bool
    let collapsed: Bool
    let floating: Bool
    let visible: Bool
    let open: Bool
    let markers: [ReaderNativePageMarker]
    let controls: [String: String]
    let parts: [ReaderNativeConversationPart]
    let ink: [ReaderNativeCardStroke]
    let inkAspectRatio: CGFloat
    let inkGeometry: String
    let size: CGSize?
    /// 卡面本身的用色与磨砂强度 —— 跟网页那版 applyColor 同一组值。
    let surfaceColor: Color
    let surfaceHex: String
    let surfaceOpacity: Double
    let surfaceBlur: Double

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String,
              let box = value["rect"] as? [String: NSNumber],
              let x = box["x"]?.doubleValue, let y = box["y"]?.doubleValue,
              let w = box["width"]?.doubleValue, let h = box["height"]?.doubleValue,
              [x, y, w, h].allSatisfy({ $0.isFinite }), w >= 0, h >= 0 else { return nil }
        self.id = id
        title = value["title"] as? String ?? "卡片"
        rect = CGRect(x: x, y: y, width: w, height: h)
        bound = value["bound"] as? Bool ?? false
        collapsed = value["collapsed"] as? Bool ?? false
        floating = value["floating"] as? Bool ?? false
        visible = value["visible"] as? Bool ?? true
        open = value["open"] as? Bool ?? false
        markers = (value["markers"] as? [[String: Any]] ?? []).compactMap(ReaderNativePageMarker.init)
        controls = value["controls"] as? [String: String] ?? [:]
        parts = (value["parts"] as? [[String: Any]] ?? []).compactMap(ReaderNativeConversationPart.init)
        let drawing = value["ink"] as? [String: Any] ?? [:]
        ink = (drawing["strokes"] as? [[String: Any]] ?? []).compactMap(ReaderNativeCardStroke.init)
        inkAspectRatio = (drawing["aspectRatio"] as? NSNumber).map { CGFloat(truncating: $0) } ?? 0
        inkGeometry = drawing["geometry"] as? String ?? ""
        if let size = value["size"] as? [String: NSNumber], let w = size["width"]?.doubleValue,
           let h = size["height"]?.doubleValue, w.isFinite, h.isFinite, w > 0, h > 0 {
            self.size = CGSize(width: w, height: h)
        } else { self.size = nil }
        // ⚠ 卡面 = rgba(便签色, α) + 磨砂，**卡片本身就是那层玻璃**。
        //   不能在一个不透明底色后面再套一层玻璃：什么都透不出来，还要付
        //   实时背景重采样的钱（2026-09-22 用户："我的卡片本身就是半透明的，
        //   你直接改造卡片本身"）。
        let surface = value["surface"] as? [String: Any] ?? [:]
        surfaceHex = surface["color"] as? String ?? "#ffffff"
        surfaceColor = ReaderNativePagePlacement.color(surface["color"] as? String)
        surfaceOpacity = min(1, max(0.3, (surface["opacity"] as? NSNumber)?.doubleValue ?? 0.72))
        surfaceBlur = min(24, max(0, (surface["blur"] as? NSNumber)?.doubleValue ?? 10))
    }

    /// 深底 → 浅字。判据与网页那版 `isDarkBg` **逐字一致**：W3C 相对亮度，
    /// 阈值 0.55，hex 解析失败按浅底（＝深字，也是它的现状语义）。
    /// 差一点就会出现"同一张卡在两个表面上字色相反"。
    var prefersLightText: Bool {
        guard let rgb = ReaderNativePagePlacement.rgb(surfaceHex) else { return false }
        func channel(_ value: Double) -> Double {
            let v = value / 255
            return v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * channel(rgb.0) + 0.7152 * channel(rgb.1) + 0.0722 * channel(rgb.2) < 0.55
    }

    private static func rgb(_ hex: String) -> (Double, Double, Double)? {
        var text = hex.trimmingCharacters(in: CharacterSet(charactersIn: "# "))
        if text.count == 3 { text = text.map { "\($0)\($0)" }.joined() }
        guard text.count == 6, let value = UInt32(text, radix: 16) else { return nil }
        return (Double((value >> 16) & 255), Double((value >> 8) & 255), Double(value & 255))
    }

    /// `#rgb` / `#rrggbb` → Color。解不出来用网页那边的默认白便签色。
    private static func color(_ hex: String?) -> Color {
        var text = (hex ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "# "))
        if text.count == 3 { text = text.map { "\($0)\($0)" }.joined() }
        guard text.count == 6, let value = UInt32(text, radix: 16) else { return .white }
        return Color(red: Double((value >> 16) & 255) / 255,
                     green: Double((value >> 8) & 255) / 255,
                     blue: Double(value & 255) / 255)
    }
}

struct ReaderNativePageMarker: Identifiable {
    let id: String
    let number: String
    let outline: Bool
    let rect: CGRect
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String,
              let box = value["rect"] as? [String: NSNumber],
              let x = box["x"]?.doubleValue, let y = box["y"]?.doubleValue,
              let w = box["width"]?.doubleValue, let h = box["height"]?.doubleValue,
              [x, y, w, h].allSatisfy({ $0.isFinite }), w > 0, h > 0 else { return nil }
        self.id = id
        number = value["number"] as? String ?? ""
        outline = value["kind"] as? String == "outline"
        rect = CGRect(x: x, y: y, width: w, height: h)
    }
}

@MainActor
struct ReaderNativePageCards: View {
    @ObservedObject var reader: ReaderWebViewModel
    @ObservedObject var model: ReaderNativeConversationModel

    var body: some View {
        GeometryReader { geometry in
            // ⚠ 原生正文滚动/缩放时页卡要跟着动，而 SwiftUI 观察不到 PDFView 内部的
            //   变化。document 每次布局都会 bump geometryRevision —— 把它读进来，
            //   这一层才会重算。没有原生文档时按原样渲染，行为不变。
            if let document = reader.nativePDFDocument {
                ReaderNativeGeometryTracker(document: document) { _ in cards(in: geometry) }
            } else {
                cards(in: geometry)
            }
        }
        .clipped()
    }

    /// 打开/收起一张词锚卡。
    ///
    /// ⚠ 失败要**出声**。之前这里是 `Task { await model.perform(...) }` 把结果丢掉，
    /// 而 `model.error` 只在侧栏里显示 —— 侧栏多半没开。于是用户看到的就是
    /// “点了没反应”，而我们连它报没报错都不知道（2026-09-22 实报）。
    private func openBoundCard(_ item: ReaderNativePagePlacement) {
        guard let id = item.controls["toggleBound"] else {
            reader.showTransientNotice("这张卡没有可用的展开操作。")
            return
        }
        Task {
            if await model.perform("liveAction", parameters: ["actionId": id]) == false {
                reader.showTransientNotice(model.error ?? "卡片没能打开，请重试。")
            }
        }
    }

    @ViewBuilder
    private func cards(in geometry: GeometryProxy) -> some View {
        ZStack(alignment: .topLeading) {
                // 松手会锁在哪 —— 光带＝钉在这段内容上，横线＝钉在这个版面位置。
                // ⚠ 自成一层：它每秒更新十来次，混在这一层里就会把每张卡一起重算。
                ReaderNativeDropPreviewLayer(model: reader.cardDropPreviews,
                                             origin: geometry.frame(in: .global).origin)
                ForEach(model.placements) { item in
                    // ⚠ 原生正文接管时坐标必须来自 PDFKit 解锚：网页那套 rect 是从
                    //   DOM 推的，而接管后网页不渲页、滚动也不同步，它已经不对应
                    //   屏幕上的任何东西。拿不到才退回网页那条路。
                    let nativeMarkers = reader.nativePageMarkerRects(
                        id: item.id, in: geometry.frame(in: .global))
                    ForEach(Array(item.markers.enumerated()), id: \.element.id) { index, marker in
                        let box = nativeMarkers.map { index < $0.count ? $0[index] : .zero }
                            ?? reader.nativePageCardRect(marker.rect, in: geometry.frame(in: .global))
                        Button { openBoundCard(item) } label: {
                            ZStack {
                                if marker.outline {
                                    RoundedRectangle(cornerRadius: 3)
                                        .stroke(ReaderNativeTheme.accent.opacity(item.open ? 1 : 0.65), lineWidth: 1.2)
                                } else {
                                    Text(marker.number.isEmpty ? "•" : marker.number)
                                        .font(.system(size: max(9, min(14, box.height)), weight: .semibold))
                                        .foregroundStyle(ReaderNativeTheme.accent)
                                }
                            }.frame(width: box.width, height: box.height).contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel((item.open ? "收起" : "展开") + item.title + "，标记 " + marker.number)
                        .offset(x: box.minX, y: box.minY)
                    }
                    // ⚠ 词锚标记（.pgmark / 序号）是网页在 pgbind-layer 里画的，要
                    //   __charBoxes 才画得出来 —— 接管后一个都没有，于是 item.markers
                    //   是空的：**这张卡钉在正文哪一段，屏幕上完全看不出来**。
                    //   原生自己解得出那几个框，就用它们补一个描边（没有序号，
                    //   因为序号是网页排的，这里不去猜一个可能对不上的号）。
                    // ⚠ 原生正文自己画锁定框（ReaderNativePDFViewport 的 Canvas +
                    //   真控件层，跟着页面滚），这一层就不要再画一遍 ——
                    //   两份同时在，滚动时就是"有残影"，点击也落在慢半拍的那份上
                    //   （2026-09-22 用户连报两次）。
                    if reader.nativePDFDocument == nil,
                       item.markers.isEmpty, item.bound, let boxes = nativeMarkers, !boxes.isEmpty {
                        ForEach(Array(boxes.enumerated()), id: \.offset) { _, box in
                            Button { openBoundCard(item) } label: {
                                RoundedRectangle(cornerRadius: 3)
                                    .stroke(ReaderNativeTheme.accent.opacity(item.open ? 1 : 0.65), lineWidth: 1.2)
                                    .frame(width: box.width, height: box.height)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel((item.open ? "收起" : "展开") + item.title)
                            .offset(x: box.minX, y: box.minY)
                        }
                    }
                    // 卡身同理：原生接管时用 PDFKit 解出来的位置和尺寸
                    // （noteGeometry 会按页宽/base_w 的比例缩放，并处理折叠态）。
                    let rect = reader.nativePageCardGeometry(
                        id: item.id, size: item.size, in: geometry.frame(in: .global))
                        ?? reader.nativePageCardRect(item.rect, in: geometry.frame(in: .global))
                    if item.visible && rect.maxX > 0 && rect.maxY > 0 && rect.minX < geometry.size.width && rect.minY < geometry.size.height {
                        ReaderNativePlacedCard(item: item, reader: reader, model: model,
                                               origin: geometry.frame(in: .global).origin,
                                               rect: rect, available: geometry.size)
                            .offset(x: rect.minX, y: rect.minY)
                            .zIndex(10)
                    }
                }
        }
    }
}

/// 把 PDFView 内部的布局变化接到 SwiftUI 上。
/// document 每次布局都会 bump `geometryRevision`；把它读进 body，这一层就会重算。
@MainActor
private struct ReaderNativeGeometryTracker<Content: View>: View {
    @ObservedObject var document: ReaderNativePDFDocument
    @ViewBuilder let content: (Int) -> Content
    var body: some View { content(document.geometryRevision) }
}

@MainActor
private struct ReaderNativePlacedCard: View {
    let item: ReaderNativePagePlacement
    @ObservedObject var reader: ReaderWebViewModel
    @ObservedObject var model: ReaderNativeConversationModel
    let origin: CGPoint
    let rect: CGRect
    let available: CGSize
    @GestureState private var translation: CGSize = .zero
    /// 松手到新位置回来之间的**暂态位移**。
    ///
    /// ⚠ 没有它，`@GestureState` 在松手那一刻就归零，而写回是异步的 ——
    /// 卡会先"弹回原位"再跳到新位置，看着就像拖动没生效（2026-09-22 实报）。
    /// 网页那版同一处也是这么做的：拖拽期间的 transform 保留到 reanchor 落地
    /// （rc-stickynote 开头那段注释）。新几何一到就清掉。
    @State private var committed: CGSize?
    @State private var confirmRemoval = false
    @State private var operationError: String?
    @State private var lastTouch = Date.distantPast
    @State private var resizing: CGSize?
    @State private var resizeStart: CGSize?

    private var savedSize: CGSize? {
        item.size.map { reader.nativePageCardRect(CGRect(origin: .zero, size: $0), in: .zero).size }
    }
    private var width: CGFloat { item.collapsed ? 44 : min(max(180, (resizing ?? savedSize)?.width ?? rect.width), max(44, available.width)) }
    private var bodyHeight: CGFloat? {
        (resizing ?? savedSize).map { max(64, min($0.height, available.height) - 36) }
    }

    /// 正在拖。⚠ 这一刻**不要搬活卡片**：它里面有富文本（UITextView + SwiftSoup）、
    /// 墨迹层，外面还套着 Liquid Glass（实时背景重采样）。每帧搬一次就是每帧
    /// 重算这些东西 —— 2026-09-22 用户："手指拖动移动距离 10，他实际移动 3"。
    ///
    /// 系统自己的拖动（UIDragInteraction，股票/文件那种）搬的是**事先截好的快照**，
    /// 跟视图多贵无关。我们这里做同一件事的最省办法：拖动期间换成一张影子
    /// —— 跟网页那版 `vc-drag-ghost`（克隆 + 源卡淡到 .22）是同一个设计。
    private var dragging: Bool { translation != .zero }

    @ViewBuilder private var ghost: some View {
        Label(item.title, systemImage: item.bound ? "pin.fill" : "line.3.horizontal")
            .font(.caption.weight(.medium)).lineLimit(1)
            .padding(.horizontal, 12).padding(.vertical, 10)
            .frame(width: width, alignment: .leading)
            .foregroundStyle(item.prefersLightText ? Color.white : Color.black.opacity(0.88))
            // 影子用**不透明**的便签色：拖动时它在正文上飞，半透明反而看不清自己。
            .background(item.surfaceColor.opacity(max(0.85, item.surfaceOpacity)),
                        in: RoundedRectangle(cornerRadius: item.collapsed ? 22 : 14))
            .overlay(RoundedRectangle(cornerRadius: item.collapsed ? 22 : 14)
                .stroke(Color.black.opacity(0.28), lineWidth: 1))
            // 浮起特效照原版 .rc-note-lift：微放大 + 轻微透明 + 更深的影。
            .scaleEffect(1.03, anchor: .topLeading)
            .opacity(0.92)
    }

    var body: some View {
        // ⚠ 用 ZStack 而不是两条并列语句：后者在 ViewBuilder 里会成为 TupleView，
        //   而 TupleView 自己不负责布局。
        //   `.offset` 不参与布局，所以影子拖多远都不会把这个 ZStack 撑大。
        ZStack(alignment: .topLeading) {
            card.opacity(dragging ? 0.22 : 1)
            if dragging {
                ghost.offset(translation)
                    .shadow(color: .black.opacity(0.18), radius: 16, y: 4)
                    .allowsHitTesting(false)
            }
        }
    }

    private var card: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                if item.collapsed {
                    Button { run("expand") } label: {
                        Image(systemName: item.bound ? "pin.fill" : "rectangle.on.rectangle")
                            .frame(width: 44, height: 44)
                    }.accessibilityLabel("展开" + item.title)
                        .simultaneousGesture(moveGesture)
                } else {
                    Label(item.title, systemImage: item.bound ? "pin.fill" : "line.3.horizontal")
                        .font(.caption.weight(.medium)).lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                        .gesture(moveGesture)
                    Button { run("collapse") } label: { Image(systemName: "minus") }
                        .accessibilityLabel("收起卡片")
                    Menu {
                        if !item.bound && !item.floating {
                            Button("锚定到正文", systemImage: "pin") { run("anchor") }
                        }
                        Button(item.floating ? "关闭浮动卡片" : "移除这处卡片", systemImage: "trash", role: .destructive) { confirmRemoval = true }
                    } label: { Image(systemName: "ellipsis").frame(width: 28, height: 32) }
                }
            }
            .padding(.horizontal, item.collapsed ? 0 : 10)
            .frame(minHeight: item.collapsed ? 44 : 36)
            if !item.collapsed {
                Divider()
                ScrollView {
                    ReaderNativeConversationArtifacts(parts: item.parts, model: model).padding(8)
                }
                .frame(height: bodyHeight)
                .frame(maxHeight: bodyHeight ?? max(120, min(460, min(rect.height, available.height - 40))))
                .overlay {
                    if let inkID = item.controls["ink"] {
                        ReaderNativeCardInkLayer(item: item, reader: reader, actionID: inkID)
                    }
                }
            }
        }
        .frame(width: width)
        // 卡面 = 便签色 + 磨砂，**卡片本身就是那层玻璃**（见 readerNoteSurface）。
        .readerNoteSurface(item.surfaceColor, opacity: item.surfaceOpacity, blur: item.surfaceBlur,
                           in: RoundedRectangle(cornerRadius: item.collapsed ? 22 : 14))
        // 描边照原版 .rc-note-body：一道近黑的细边，不是主题强调色。
        .overlay(RoundedRectangle(cornerRadius: item.collapsed ? 22 : 14)
            .stroke(Color.black.opacity(0.22), lineWidth: 1))
        .foregroundStyle(item.prefersLightText ? Color.white : Color.black.opacity(0.88))
        .overlay(alignment: .bottomTrailing) {
            if !item.collapsed, item.controls["resize"] != nil {
                Image(systemName: "arrow.up.left.and.arrow.down.right")
                    .font(.caption).foregroundStyle(ReaderNativeTheme.muted)
                    .frame(width: 36, height: 36)
                    .background(ReaderNativeTheme.card.opacity(0.9), in: RoundedRectangle(cornerRadius: 10))
                    .contentShape(Rectangle()).gesture(resizeGesture)
                    .accessibilityLabel("调整卡片大小")
                    .accessibilityAdjustableAction { direction in
                        let current = savedSize ?? rect.size
                        saveSize(CGSize(width: current.width + (direction == .increment ? 30 : -30),
                                        height: current.height + (direction == .increment ? 30 : -30)))
                    }
            }
        }
        .shadow(color: .black.opacity(0.12), radius: 8, y: 3)
        // 拖动中位移加在**影子**上（见 body），这里只保留松手到新几何之间的暂态位移。
        .offset(committed ?? .zero)
        // 手势被打断时 GestureState 会自己归零，而 onEnded 不一定来 ——
        // 不擦的话预览会留在屏幕上，看着像"钉在那儿了"。
        .onChange(of: translation) { _, value in if value == .zero { reader.clearCardDropPreview() } }
        .onChange(of: rect) { _, _ in committed = nil }
        .simultaneousGesture(DragGesture(minimumDistance: 0).onChanged { _ in
            if item.floating, Date().timeIntervalSince(lastTouch) > 2 {
                lastTouch = Date()
                if let id = item.controls["touch"], !model.isPerforming("liveAction") {
                    Task { await model.touchPageCard(id) }
                }
            }
        })
        .disabled(model.isPerforming("liveAction"))
        .confirmationDialog("仅移除这处书页卡片，原卡和学习记录会保留。", isPresented: $confirmRemoval, titleVisibility: .visible) {
            Button("移除", role: .destructive) { run("remove") }
        }
        .alert("卡片操作未完成", isPresented: Binding(get: { operationError != nil }, set: { if !$0 { operationError = nil } })) {
            Button("好") { operationError = nil }
        } message: { Text(operationError ?? "") }
    }

    private func dropPoint(_ translation: CGSize) -> CGPoint {
        CGPoint(x: origin.x + rect.minX + translation.width + 1,
                y: origin.y + rect.minY + translation.height + 1)
    }

    private var moveGesture: some Gesture {
        DragGesture(minimumDistance: 6)
            .updating($translation) { value, state, _ in state = value.translation }
            // ⚠ 探测点＝**卡左上角**（+1 避开自身边框），不是手指 —— 那才是钉入点。
            //   跟下面 onEnded 用同一个式子：预览与落点必须是同一个点。
            .onChanged { value in reader.previewCardDrop(windowPoint: dropPoint(value.translation)) }
            .onEnded { value in
                reader.clearCardDropPreview()
                let scope = model.scope
                let point = dropPoint(value.translation)
                committed = value.translation
                Task {
                    // 原生正文接管时走原生锚点：落点要换成**页内**归一化坐标。
                    // 网页那条路把它当网页视口坐标，而接管后视口里没有那一页 ——
                    // 卡会飞到别处。原生写失败才退回去。
                    if await reader.moveNativeCard(id: item.id, windowPoint: point) {
                        // ⚠ 兜一手：暂态位移本来靠"新几何到了"来清（onChange(of: rect)）。
                        //   可要是落点跟原位几乎一样，rect 不变、那一下就永远不会来，
                        //   卡片会一直画在偏移后的位置上。等一拍还没来就自己清。
                        try? await Task.sleep(nanoseconds: 1_200_000_000)
                        committed = nil
                        return
                    }
                    // ⚠ 这里以前是 `guard … else { return }`：拖了一下、卡弹回去、
                    //   一个字都没有。用户看到的就是"拖不动"，而我们连它为什么
                    //   没动都不知道。落点定不下来就说出来。
                    guard let action = item.controls["move"] else {
                        committed = nil
                        reader.showTransientNotice("这张卡不能挪到这里。")
                        return
                    }
                    await reader.placeNativeConversationCard(actionID: action, scope: scope, windowPoint: point)
                    guard model.scope == scope else { return }
                    if let failure = model.error {
                        committed = nil
                        operationError = failure
                    }
                }
            }
    }

    private var resizeGesture: some Gesture {
        DragGesture(minimumDistance: 3)
            .onChanged { value in
                if resizeStart == nil { resizeStart = savedSize ?? rect.size }
                let base = resizeStart ?? rect.size
                resizing = boundedSize(CGSize(width: base.width + value.translation.width, height: base.height + value.translation.height))
            }
            .onEnded { _ in
                if let resizing { saveSize(resizing) }
                resizeStart = nil
            }
    }

    private func boundedSize(_ size: CGSize) -> CGSize {
        CGSize(width: min(max(180, size.width), min(720, available.width)),
               height: min(max(100, size.height), min(720, available.height)))
    }

    private func saveSize(_ value: CGSize) {
        let scope = model.scope, value = boundedSize(value)
        resizing = value
        Task {
            // 原生接管时按卡片自身单位存（屏幕尺寸 ÷ 页宽/base_w 的比例）；
            // 否则每缩放一次书，卡片尺寸就被记错一次。
            if await reader.resizeNativeCard(id: item.id, size: value) {
                if scope == model.scope { resizing = nil }
                return
            }
            guard let action = item.controls["resize"] else {
                if scope == model.scope { resizing = nil }
                return
            }
            let saved = await reader.resizeNativeConversationCard(actionID: action, scope: scope, size: value)
            if scope == model.scope {
                resizing = nil
                if !saved { operationError = model.error ?? "尺寸尚未保存，请重试。" }
            }
        }
    }

    private func run(_ key: String) {
        guard let id = item.controls[key] else { return }
        Task {
            if !(await model.perform("liveAction", parameters: ["actionId": id])) {
                operationError = model.error ?? "操作未确认，请重试。"
            }
        }
    }
}

/// 落点预览。单独一层、单独一个模型 —— 见 ReaderNativeDropPreviewModel 的说明。
@MainActor
private struct ReaderNativeDropPreviewLayer: View {
    @ObservedObject var model: ReaderNativeDropPreviewModel
    let origin: CGPoint

    var body: some View {
        if let preview = model.preview {
            ForEach(Array(preview.rects.enumerated()), id: \.offset) { _, box in
                RoundedRectangle(cornerRadius: 3)
                    .fill(ReaderNativeTheme.accent.opacity(0.22))
                    .overlay(RoundedRectangle(cornerRadius: 3)
                        .stroke(ReaderNativeTheme.accent.opacity(0.75), lineWidth: 1))
                    .frame(width: box.width, height: box.height)
                    .offset(x: box.minX - origin.x, y: box.minY - origin.y)
                    .allowsHitTesting(false)
            }
            if let line = preview.line {
                Capsule().fill(ReaderNativeTheme.accent.opacity(0.75))
                    .frame(width: line.width, height: 2)
                    .offset(x: line.minX - origin.x, y: line.minY - origin.y)
                    .allowsHitTesting(false)
            }
        }
    }
}

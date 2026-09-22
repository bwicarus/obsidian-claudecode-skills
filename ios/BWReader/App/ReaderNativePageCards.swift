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
                    if item.markers.isEmpty, item.bound, let boxes = nativeMarkers, !boxes.isEmpty {
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

    var body: some View {
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
        .background(ReaderNativeTheme.card, in: RoundedRectangle(cornerRadius: item.collapsed ? 22 : 14))
        .overlay(RoundedRectangle(cornerRadius: item.collapsed ? 22 : 14).stroke(ReaderNativeTheme.accent.opacity(0.2)))
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
        .shadow(color: .black.opacity(0.12), radius: translation == .zero ? 8 : 16, y: 3)
        .offset(translation)
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

    private var moveGesture: some Gesture {
        DragGesture(minimumDistance: 6)
            .updating($translation) { value, state, _ in state = value.translation }
            .onEnded { value in
                let scope = model.scope
                let point = CGPoint(x: origin.x + rect.minX + value.translation.width + 1,
                                    y: origin.y + rect.minY + value.translation.height + 1)
                Task {
                    // 原生正文接管时走原生锚点：落点要换成**页内**归一化坐标。
                    // 网页那条路把它当网页视口坐标，而接管后视口里没有那一页 ——
                    // 卡会飞到别处。原生写失败才退回去。
                    if await reader.moveNativeCard(id: item.id, windowPoint: point) { return }
                    guard let action = item.controls["move"] else { return }
                    await reader.placeNativeConversationCard(actionID: action, scope: scope, windowPoint: point)
                    if model.scope == scope { operationError = model.error }
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

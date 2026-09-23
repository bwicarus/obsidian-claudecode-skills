import SwiftUI

struct ReaderNativePagePlacement: Identifiable {
    let id: String
    /// 便签自己的 id —— 原生正文解锚（卡位 / 锁定框 / 点框开卡）用它。
    /// ⚠ 不能拿 `id` 去比：那是按代际哈希出来的 `placement-…`，跟便签 id
    /// 永远对不上。2026-09-23 之前就是这么比的，于是页内锁定框一点就是
    /// "还没加载好"，卡身也一直退回网页坐标（滚动不跟手）。
    let noteID: String
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
    /// 卡片色调（原版 `--vc-tc`）。卡面、描边、辉光、卡头字色全由它推出。
    ///
    /// ⚠ **不是便签色。** 卡片式便签在网页里便签壳是全透明的（rc-stickynote：
    /// `.rc-note-hascard .rc-note-body{background:transparent!important}`），
    /// 看得见的是里面那张 `.vc-card.vc-typed`。上一版拿便签色当卡面，
    /// 于是卡是近乎纯黑的一块（2026-09-23 用户截图："和我原来做的完全不一样"）。
    let tone: UIColor
    /// 'dot'（圆角方标记）/ 'min'（长条）/ 'full'（方块）。
    /// ⚠ 不能只看 collapsed —— 那把三态压成两态，圆点和长条就长得一样了。
    let form: String
    /// 原生按便签数据现造的（网页没挂这张卡）。没有网页控件：开合、删除走原生。
    let nativeOnly: Bool
    /// 钉在正文上。⚠ 钉住的卡**不进长条态**（用户 2026-08-18 拍板：概要与锚点
    /// 重复），所以它的形态循环是 标记 ⇄ 方块 两态，不是三态。
    let pinned: Bool

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String,
              let box = value["rect"] as? [String: NSNumber],
              let x = box["x"]?.doubleValue, let y = box["y"]?.doubleValue,
              let w = box["width"]?.doubleValue, let h = box["height"]?.doubleValue,
              [x, y, w, h].allSatisfy({ $0.isFinite }), w >= 0, h >= 0 else { return nil }
        self.id = id
        noteID = value["noteId"] as? String ?? ""
        nativeOnly = value["nativeOnly"] as? Bool ?? false
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
        tone = ReaderNativePagePlacement.hex(value["tone"] as? String)
            ?? UIColor(red: 0xbf / 255, green: 0x5a / 255, blue: 0xf2 / 255, alpha: 1)
        let raw = value["form"] as? String ?? (value["collapsed"] as? Bool == true ? "dot" : "full")
        form = ["dot", "min", "full"].contains(raw) ? raw : "full"
        pinned = value["pinned"] as? Bool ?? (value["bound"] as? Bool ?? false)
    }

    /// 网页没挂这张卡时，按便签数据现造一份（只支持 html 槽 —— 现有页卡全是这种）。
    /// 色调取法与网页 wordCardPresentation 一致：词锚卡按分类，自由卡用自己的 type。
    init?(nativeNote note: [String: Any], open: Bool) {
        guard let id = note["id"] as? String, !id.isEmpty,
              let html = note["html"] as? [String: Any] else { return nil }
        let label = (html["label"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "卡片"
        let bound = (html["bind"] as? [String: Any])?["kind"] as? String == "page-chars"
        let tone = bound ? ReaderNativePagePlacement.hexString(ReaderNativeMarkerStyle.tone(html, slot: "html"))
                         : (html["type"] as? String ?? "")
        let zero = NSNumber(value: 0)
        let part: [String: Any] = [
            "id": "native-" + id + "-html", "kind": "general", "title": label, "text": "", "status": "saved",
            "data": ["text": html["content"] as? String ?? "",
                     "format": html["isHtml"] as? Bool == true ? "html" : "text"],
        ]
        self.init([
            "id": "native-" + id, "noteId": id, "nativeOnly": true, "title": label,
            "rect": ["x": zero, "y": zero, "width": zero, "height": zero],
            "bound": bound, "collapsed": false, "floating": false, "visible": true, "open": open,
            "controls": [String: String](), "parts": [part], "tone": tone, "form": "full", "pinned": bound,
        ])
    }

    private static func hexString(_ color: UIColor) -> String {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        color.getRed(&r, green: &g, blue: &b, alpha: &a)
        return String(format: "#%02x%02x%02x", Int(round(r * 255)), Int(round(g * 255)), Int(round(b * 255)))
    }

    /// `#rrggbb` / `#rgb` → UIColor；解不出来返回 nil。
    private static func hex(_ text: String?) -> UIColor? {
        var value = (text ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "# "))
        if value.count == 3 { value = value.map { "\($0)\($0)" }.joined() }
        guard value.count == 6, let number = UInt32(value, radix: 16) else { return nil }
        return UIColor(red: CGFloat((number >> 16) & 255) / 255, green: CGFloat((number >> 8) & 255) / 255,
                       blue: CGFloat(number & 255) / 255, alpha: 1)
    }
}

/// 原版 `.vc-card.vc-typed` 的配方（rc-voicecall）：
///   背景 = color-mix(色调 15%, rgba(28,30,34,.9))；描边 = 色调 42%；
///   阴影 = 0 16px 42px rgba(0,0,0,.5) + 0 0 22px -8px 色调 55%；卡头字色 = 色调。
struct ReaderNativeCardFinish {
    let tone: Color
    let fill: Color
    let border: Color
    let glow: Color

    init(_ tone: UIColor) {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        tone.getRed(&r, green: &g, blue: &b, alpha: &a)
        // color-mix 在带透明度时按预乘插值，再除回去。
        let alpha: Double = 0.15 + 0.9 * 0.85
        func channel(_ t: CGFloat, _ base: Double) -> Double {
            let premultiplied: Double = 0.15 * Double(t) + 0.85 * 0.9 * base / 255
            return premultiplied / alpha
        }
        self.tone = Color(uiColor: tone)
        fill = Color(red: channel(r, 28), green: channel(g, 30), blue: channel(b, 34)).opacity(alpha)
        border = Color(uiColor: tone).opacity(0.42)
        glow = Color(uiColor: tone).opacity(0.55)
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
        // 展开着的词锚卡 → 页内锁定框画成「打开」态（原版 .pgmark.on）。
        .onChange(of: openBoundIDs, initial: true) { _, ids in
            reader.nativePDFDocument?.openCardIDs = ids
        }
    }

    private var openBoundIDs: Set<String> {
        Set(model.placements.filter { $0.bound && $0.open }.map(\.noteID)).union(reader.nativeOpenBoundNotes)
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

    // ⚠ 拆成几个小函数不是为了好看：整段写在一个 ViewBuilder 里，CI 上 Swift
    //   直接报"无法在合理时间内完成类型检查"（2026-09-23 那次构建就挂在这里）。
    @ViewBuilder
    private func cards(in geometry: GeometryProxy) -> some View {
        let frame = geometry.frame(in: .global)
        ZStack(alignment: .topLeading) {
            // 松手会锁在哪 —— 光带＝钉在这段内容上，横线＝钉在这个版面位置。
            // ⚠ 自成一层：它每秒更新十来次，混在这一层里就会把每张卡一起重算。
            ReaderNativeDropPreviewLayer(model: reader.cardDropPreviews, origin: frame.origin)
            // 边缘投放区（删除 / 收藏）。⚠ 判据用**手指**位置，不是卡左上角。
            ReaderNativeCardDropZones(drag: reader.cardDrag, origin: frame.origin, size: geometry.size)
            ForEach(model.placements) { item in
                placement(item, frame: frame, size: geometry.size)
            }
        }
        // 投放区判据要知道这一层在窗口里的位置（手指是按窗口坐标记的）。
        .onAppear { reader.cardDrag.screenFrame = frame }
        .onChange(of: frame) { _, value in reader.cardDrag.screenFrame = value }
    }

    @ViewBuilder
    private func placement(_ item: ReaderNativePagePlacement, frame: CGRect, size: CGSize) -> some View {
        // ⚠ 原生正文接管时**一个标记都不在这层画**：锁定框与序号都由 PDFKit
        //   页内 overlay 画（ReaderNativePDFTextOverlay），跟着页面一起滚。
        //   这层是按窗口坐标摆的，滚动时永远慢一帧 —— 2026-09-23 用户截图里
        //   那条"细、浅、带残影"的青线加序号就是这里画的网页那份标记。
        //   只有没有原生正文（网页在渲页）时，这层才替网页标记接点击。
        if reader.nativePDFDocument == nil {
            webMarkers(item, frame: frame)
        }
        // 钉在正文上的卡由文档层画（ReaderNativeDocumentCardLayer，跟着 PDF 同一帧滚）。
        // 这一层只剩浮动卡，以及原生几何解不出来时的网页坐标退路。
        if !reader.drawsInDocumentLayer(item) {
            let rect = cardRect(item, frame: frame)
            if item.visible && rect.maxX > 0 && rect.maxY > 0 && rect.minX < size.width && rect.minY < size.height {
                ReaderNativePlacedCard(item: item, reader: reader, model: model, rect: rect, available: size,
                                       space: .global, unitScale: 1,
                                       toWindow: { point in point })
                    .offset(x: rect.minX, y: rect.minY)
                    .zIndex(10)
            }
        }
    }

    private func cardRect(_ item: ReaderNativePagePlacement, frame: CGRect) -> CGRect {
        reader.nativePageCardGeometry(id: item.noteID, size: item.size, in: frame)
            ?? reader.nativePageCardRect(item.rect, in: frame)
    }

    /// 网页标记（.pgmark 描边 / 序号）的可点替身 —— 位置是网页 DOM 给的窗口坐标。
    private func webMarkerBoxes(_ item: ReaderNativePagePlacement, frame: CGRect) -> [(marker: ReaderNativePageMarker, box: CGRect)] {
        item.markers.map { marker in (marker, reader.nativePageCardRect(marker.rect, in: frame)) }
    }

    @ViewBuilder
    private func webMarkers(_ item: ReaderNativePagePlacement, frame: CGRect) -> some View {
        let placed = webMarkerBoxes(item, frame: frame)
        ForEach(placed.indices, id: \.self) { index in
            webMarker(item, marker: placed[index].marker, box: placed[index].box)
        }
    }

    private func webMarker(_ item: ReaderNativePagePlacement, marker: ReaderNativePageMarker, box: CGRect) -> some View {
        let size: CGFloat = max(9, min(14, box.height))
        return Button { openBoundCard(item) } label: {
            ZStack {
                if marker.outline {
                    RoundedRectangle(cornerRadius: 3)
                        .stroke(ReaderNativeTheme.accent.opacity(item.open ? 1 : 0.65), lineWidth: 1.2)
                } else {
                    Text(marker.number.isEmpty ? "•" : marker.number)
                        .font(.system(size: size, weight: .semibold))
                        .foregroundStyle(ReaderNativeTheme.accent)
                }
            }
            .frame(width: box.width, height: box.height)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel((item.open ? "收起" : "展开") + item.title + "，标记 " + marker.number)
        .offset(x: box.minX, y: box.minY)
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
struct ReaderNativePlacedCard: View {
    let item: ReaderNativePagePlacement
    @ObservedObject var reader: ReaderWebViewModel
    @ObservedObject var model: ReaderNativeConversationModel
    let rect: CGRect
    let available: CGSize
    /// 手势用的坐标系：屏幕层是 .global，文档层是它自己的命名坐标系。
    let space: CoordinateSpace
    /// 卡片本地单位 → 屏幕点（文档层 = 当前缩放；屏幕层 = 1）。改尺寸落库要用屏幕点。
    let unitScale: CGFloat
    /// 本地坐标 → 窗口坐标。落点、投放区判据都按窗口坐标算。
    let toWindow: (CGPoint) -> CGPoint
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

    /// 「加入上下文」的控件 id 与当前选中态。
    ///
    /// ⚠ 原版在卡上是**长按**触发（rc-voicecall 的 pinBind，阈值 LP_MS = 600），
    /// 原生这边此前只把它做成了侧栏里的一个按钮 —— 手势没了
    /// （2026-09-22 用户："长按卡片选中的操作也没有"）。
    private var contextAction: String? {
        item.parts.compactMap { $0.string("pinId") }.first { !$0.isEmpty }
    }
    private var contextSelected: Bool {
        item.parts.contains { $0.data["pinned"] as? Bool == true }
    }

    private func toggleContext() {
        guard let action = contextAction else {
            reader.showTransientNotice("这张卡不能带入对话。")
            return
        }
        let wasSelected = contextSelected
        Task {
            if await model.perform("liveAction", parameters: ["actionId": action]) == false {
                reader.showTransientNotice(model.error ?? "没能改变选中状态，请重试。")
            } else {
                reader.showTransientNotice(wasSelected ? "已从对话中移出" : "已带入对话")
            }
        }
    }

    /// 下一个形态。⚠ 裁剪规则与网页 `_cardForm` 一致：钉住的卡跳过长条。
    ///   「形态循环按宿主裁剪，而不是给每个宿主另造一套」——那句注释就在原版里。
    private var nextForm: String {
        switch item.form {
        case "dot": return item.pinned ? "full" : "min"
        case "min": return "full"
        default: return "dot"
        }
    }

    private var finish: ReaderNativeCardFinish { ReaderNativeCardFinish(item.tone) }
    /// 圆点态照 .vc-card.vc-dot：没有卡面、描边与阴影，只剩那枚标记。
    private var isDot: Bool { item.form == "dot" }
    private var surfaceFill: Color { isDot ? Color.clear : finish.fill }
    private var surfaceBorder: Color { isDot ? Color.clear : finish.border }
    private var dropShadow: Color { isDot ? Color.clear : Color.black.opacity(0.45) }
    private var toneGlow: Color { isDot ? Color.clear : finish.glow.opacity(0.6) }
    private var pickedRing: Color { ReaderNativeCardDropZone.dock }

    /// 圆点态的那枚标记：40×40 圆角方（半径 13），照原版 `.vc-card-dot`：
    /// 底 = 色调 14% 混 rgba(22,26,38,.38)，图标用色调。
    /// ⚠ 只在圆点态出现 —— 原版展开后「左上角那枚标记按钮**不再显示**（用户要求）；
    ///   形态切换改点头部」（rc-voicecall 那条注释原话）。
    private var formMarker: some View {
        Button { runForm(nextForm) } label: {
            Image(systemName: item.bound ? "pin.fill" : "rectangle.on.rectangle")
                .font(.system(size: 17, weight: .medium))
                .foregroundStyle(finish.tone)
                .frame(width: 40, height: 40)
                .background(finish.tone.opacity(0.14), in: RoundedRectangle(cornerRadius: 13))
                .background(Color(red: 22 / 255, green: 26 / 255, blue: 38 / 255).opacity(0.38),
                            in: RoundedRectangle(cornerRadius: 13))
                .overlay(RoundedRectangle(cornerRadius: 13).stroke(finish.border, lineWidth: 0.5))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("展开卡片")
        .accessibilityHint(item.pinned ? "在标记与展开之间切换" : "圆 / 长条 / 方块")
    }

    private func runForm(_ value: String) {
        guard let action = item.controls["form"] else {
            reader.showTransientNotice("这张卡不能切换形态。")
            return
        }
        Task {
            if await model.perform("liveAction",
                                   parameters: ["actionId": action, "value": value]) == false {
                reader.showTransientNotice(model.error ?? "形态没能切换，请重试。")
            }
        }
    }

    /// 点卡头：词锚卡 = 收起回词上（原版点标记同一动作）；自由卡 = 形态循环。
    private func tapHeader() {
        if item.bound {
            if item.nativeOnly { reader.openNativeBoundCard(noteID: item.noteID) }   // 原生自己开的，原生自己收
            else { run("collapse") }
        } else { runForm(nextForm) }
    }

    private var savedSize: CGSize? {
        // 文档层：rect 由 noteGeometry 算出，**已经含了**保存过的尺寸，而且单位就是
        // 这一层的单位；再按屏幕尺寸换一遍会差一个缩放倍数。
        if unitScale != 1 || space != .global { return nil }   // rect 已按便签 w/h 算好
        return item.size.map { reader.nativePageCardRect(CGRect(origin: .zero, size: $0), in: .zero).size }
    }
    /// 壳宽照原版 `_formW`：圆点 40 / 长条 300 / 方块按卡片自己的宽。
    private var width: CGFloat {
        switch item.form {
        case "dot": return 40
        case "min": return min(300, max(180, available.width - 32))
        default: return min(max(180, (resizing ?? savedSize)?.width ?? rect.width), max(44, available.width))
        }
    }
    /// 圆角：圆点态 13（与 .vc-card-dot 同值），其余 16（.vc-card{border-radius:16px}）。
    private var corner: CGFloat { item.form == "dot" ? 13 : 16 }
    private var bodyHeight: CGFloat? {
        (resizing ?? savedSize).map { max(64, min($0.height, available.height) - 41) }
    }

    /// 正在拖。整张卡本身跟着手指走 —— 不再留一张淡掉的原卡在原位。
    /// ⚠ 上一版是"原卡淡到 .22 + 只拖一条标题影子"，用户看到的就是一块残影
    ///   （2026-09-23："长按移动时留下一个残影"）。拖动慢的真凶是每帧跑 JS 的落点
    ///   预览和挂在主模型上的发布（都已拆掉/限流），不是卡本身。
    private var dragging: Bool { translation != .zero }

    var body: some View {
        card
            // 浮起特效照原版 .rc-note-lift：微放大 + 更深的影。
            .scaleEffect(dragging ? 1.03 : 1, anchor: .topLeading)
            .shadow(color: .black.opacity(dragging ? 0.28 : 0), radius: 18, y: 8)
            // 动画只管浮起（缩放/阴影），不管跟手位移 —— 否则松手那一下会回弹。
            .animation(.easeOut(duration: 0.12), value: dragging)
            .offset(translation)
            .zIndex(dragging ? 100 : 0)
    }

    /// 卡头 `.vc-card-hd`：12px、色调字、左距 13，最小高 40。
    /// 右侧只放原版在这个状态下真有的那一个按钮：
    /// 词锚展开卡 = 右上角圆形垃圾桶（`.rc-note-word-open .rc-note-del`：26×26，
    /// 底 rgba(64,35,42,.82)，图标 #ffd8de）；自由卡 = 一枚同尺寸的「更多」。
    private var header: some View {
        HStack(spacing: 6) {
            Text(item.title)
                .font(.system(size: 12)).lineLimit(1)
                .foregroundStyle(finish.tone)
                .frame(maxWidth: .infinity, alignment: .leading)
            if item.bound {
                Button { confirmRemoval = true } label: {
                    Image(systemName: "trash.fill").font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color(red: 1, green: 0xd8 / 255, blue: 0xde / 255))
                        .frame(width: 26, height: 26)
                        .background(Color(red: 64 / 255, green: 35 / 255, blue: 42 / 255).opacity(0.82), in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("删除这张卡片")
            } else if item.form != "dot" {
                Menu {
                    if !item.floating {
                        Button("锚定到正文", systemImage: "pin") { run("anchor") }
                    }
                    Button(item.floating ? "关闭浮动卡片" : "移除这处卡片", systemImage: "trash", role: .destructive) {
                        confirmRemoval = true
                    }
                } label: {
                    Image(systemName: "ellipsis").font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color(red: 0xe8 / 255, green: 0xe8 / 255, blue: 0xee / 255))
                        .frame(width: 26, height: 26)
                        .background(Color.white.opacity(0.14), in: Circle())
                }
                .accessibilityLabel("卡片操作")
            }
        }
        .padding(.leading, 13).padding(.trailing, 7)
        .frame(minHeight: 40)
        .contentShape(Rectangle())
        .onTapGesture { tapHeader() }
        .gesture(moveGesture)
        .accessibilityAddTraits(.isButton)
        .accessibilityHint(item.bound ? "收起到正文" : "切换卡片形态")
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 0) {
            if isDot {
                // 收起态：**整张卡就是那枚标记**（原版 `.vc-card.vc-dot`）。
                formMarker.simultaneousGesture(moveGesture)
            } else {
                header
            }
            if item.form == "full" {
                // 原版卡头下那条分隔线（截图里贯穿卡宽的细线）。
                Rectangle().fill(Color.white.opacity(0.12)).frame(height: 0.5)
                ScrollView {
                    ReaderNativePageCardBody(parts: item.parts, model: model)
                        .padding(.horizontal, 13).padding(.top, 9).padding(.bottom, 12)
                }
                // 长按＝带入/移出对话，**只在卡身**（原版 pinBind 的长按目标是 .vc-card-bd，
                // 上面那条标题栏是拖动把手）。阈值取原版 LP_MS = 600ms；
                // simultaneousGesture 才不会吃掉卡内按钮的点击和正文滚动。
                .simultaneousGesture(
                    LongPressGesture(minimumDuration: 0.6).onEnded { _ in toggleContext() }
                )
                .frame(height: bodyHeight)
                .frame(maxHeight: bodyHeight ?? max(120, min(460, min(rect.height, available.height - 40))))
                .overlay {
                    if let inkID = item.controls["ink"] {
                        ReaderNativeCardInkLayer(item: item, reader: reader, actionID: inkID,
                                                 space: space, toWindow: toWindow)
                    }
                }
            }
        }
        .frame(width: width)
        .foregroundStyle(Color(uiColor: ReaderNativeCardInk.text))
        // 卡面：原版 .vc-card.vc-typed —— 色调 15% 混深灰、**不磨砂**
        // （`--vc-cardblur:none`，注释原话"去 blur 后加实"）。圆点态照 .vc-dot 近乎透明。
        .readerCardSurface(surfaceFill, glass: !isDot, in: RoundedRectangle(cornerRadius: corner))
        .overlay(RoundedRectangle(cornerRadius: corner).stroke(surfaceBorder, lineWidth: 0.5))
        .clipShape(RoundedRectangle(cornerRadius: corner))
        // 「已带入对话」：原版 .vc-picked 是卡外 2px 的 rgba(123,108,255,.85) —— 紫色卡上
        // 几乎看不见（2026-09-23 用户："选中时边框特效不够明显，特别是卡片本身为紫色时"）。
        // 加强成：卡外 2.5pt 选中环 + 环内一道白细线（跟任何色调都拉得开对比）+ 同色外发光
        // + 右上角对勾。⚠ 画在 clipShape 之后，否则外圈会被卡片自己裁掉一半。
        .overlay {
            if contextSelected {
                ZStack(alignment: .topTrailing) {
                    RoundedRectangle(cornerRadius: corner + 1.5)
                        .stroke(Color.white.opacity(0.9), lineWidth: 1)
                        .padding(-1.5)
                    RoundedRectangle(cornerRadius: corner + 4)
                        .stroke(pickedRing, lineWidth: 2.5)
                        .padding(-4)
                        .shadow(color: pickedRing.opacity(0.8), radius: 8)
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 18, weight: .semibold))
                        .symbolRenderingMode(.palette)
                        .foregroundStyle(Color.white, ReaderNativeCardDropZone.dock)
                        .background(Circle().fill(Color.white).padding(2))
                        .offset(x: 9, y: -9)
                        .accessibilityLabel("已带入对话")
                }
                .allowsHitTesting(false)
            }
        }
        .animation(.easeOut(duration: 0.15), value: contextSelected)
        .overlay(alignment: .bottomTrailing) {
            if item.form == "full", item.controls["resize"] != nil {
                Image(systemName: "arrow.up.left.and.arrow.down.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color.white.opacity(0.45))
                    .frame(width: 30, height: 30)
                    .contentShape(Rectangle()).gesture(resizeGesture)
                    .accessibilityLabel("调整卡片大小")
                    .accessibilityAdjustableAction { direction in
                        let current = savedSize ?? rect.size
                        saveSize(CGSize(width: current.width + (direction == .increment ? 30 : -30),
                                        height: current.height + (direction == .increment ? 30 : -30)))
                    }
            }
        }
        // 阴影 + 色调辉光（.vc-card.vc-typed 的两层 box-shadow）。
        .shadow(color: dropShadow, radius: 18, y: 12)
        .shadow(color: toneGlow, radius: 7)
        // 拖动中位移加在**影子**上（见 body），这里只保留松手到新几何之间的暂态位移。
        .offset(committed ?? .zero)
        // 手势被打断时 GestureState 自己归零而 onEnded 不一定来 —— 预览和投放区
        // 都得在这里擦掉，不然会留在屏幕上（红区一直亮着尤其吓人）。
        .onChange(of: translation) { _, value in
            if value == .zero { reader.clearCardDropPreview(); reader.cardDrag.finger = nil }
        }
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
            Button("移除", role: .destructive) {
                if item.nativeOnly { Task { _ = await reader.deleteNativeNote(noteID: item.noteID) } }
                else { run("remove") }
            }
        }
        .alert("卡片操作未完成", isPresented: Binding(get: { operationError != nil }, set: { if !$0 { operationError = nil } })) {
            Button("好") { operationError = nil }
        } message: { Text(operationError ?? "") }
    }

    private func dropPoint(_ translation: CGSize) -> CGPoint {
        toWindow(CGPoint(x: rect.minX + translation.width + 1, y: rect.minY + translation.height + 1))
    }

    /// 窗口坐标 → 屏幕卡片层的本地坐标（投放区是按那一层画的）。
    private func screenLocal(_ window: CGPoint) -> CGPoint {
        let frame = reader.cardDrag.screenFrame
        return CGPoint(x: window.x - frame.minX, y: window.y - frame.minY)
    }

    private var moveGesture: some Gesture {
        // ⚠ coordinateSpace: .global —— 投放区判据要的是**手指在屏幕上哪儿**。
        //   默认坐标系是手势所在那个小视图，拿来跟屏幕边缘比毫无意义。
        DragGesture(minimumDistance: 6, coordinateSpace: space)
            .updating($translation) { value, state, _ in state = value.translation }
            .onChanged { value in
                // 两个探测点，故意不同（原版就是这么分的）：
                // · 投放区（删除/收藏）看**手指**；
                // · 落点预览看**卡左上角**（+1 避开自身边框）—— 那才是钉入点。
                //   写成同一个会出现"看着在删除区、松手却钉在正文上"。
                reader.cardDrag.finger = toWindow(value.location)
                reader.previewCardDrop(windowPoint: dropPoint(value.translation))
            }
            .onEnded { value in
                reader.clearCardDropPreview()
                let released = screenLocal(toWindow(value.location))
                reader.cardDrag.finger = nil
                let scope = model.scope
                let point = dropPoint(value.translation)
                // 删除区 / 收藏区优先于"钉到正文"（原版 onHandleUp 的顺序）。
                if ReaderNativeCardDropZone.inTrash(released), let action = item.controls["trash"] {
                    committed = nil
                    Task {
                        if await model.perform("liveAction", parameters: ["actionId": action]) == false {
                            reader.showTransientNotice(model.error ?? "没能删除这张卡。")
                        }
                    }
                    return
                }
                if ReaderNativeCardDropZone.inTrash(released), item.nativeOnly {
                    committed = nil
                    Task { _ = await reader.deleteNativeNote(noteID: item.noteID) }
                    return
                }
                if ReaderNativeCardDropZone.inDock(released, screenHeight: reader.cardDrag.screenFrame.height),
                   let action = item.controls["favorite"] {
                    // 收藏是**复制**：原卡回原位，不改锚点（原版同一条注释）。
                    committed = nil
                    Task {
                        if await model.perform("liveAction", parameters: ["actionId": action]) == false {
                            reader.showTransientNotice(model.error ?? "这张卡没能加入收藏夹。")
                        } else {
                            reader.showTransientNotice("已收入收藏夹")
                        }
                    }
                    return
                }
                if item.bound {
                    // 钉在词上的卡：拖到哪个词就改绑到哪个词（原版规则），但**词由原生认**，
                    // 连同页内坐标一起交给网页的移动动作。
                    // ⚠ 以前交的是网页视口坐标，网页按它自己的视口找页 —— 与屏幕上的页对不上，
                    //   词锚被改到了别的词上（2026-09-23 实录：「インフルエンザ」「よっ」）。
                    guard let action = item.controls["move"],
                          let target = reader.nativeDropTarget(windowPoint: point) else {
                        committed = nil
                        if item.nativeOnly { reader.showTransientNotice("这张卡还没载入完整，暂时只能收起或删除。") }
                        return
                    }
                    committed = value.translation
                    Task {
                        if await model.perform("liveAction", parameters: ["actionId": action, "value": target]) == false {
                            reader.showTransientNotice(model.error ?? "这张卡没能挪到这里。")
                        }
                        try? await Task.sleep(nanoseconds: 1_200_000_000)
                        committed = nil
                    }
                    return
                }
                committed = value.translation
                Task {
                    // 原生正文接管时走原生锚点：落点要换成**页内**归一化坐标。
                    // 网页那条路把它当网页视口坐标，而接管后视口里没有那一页 ——
                    // 卡会飞到别处。原生写失败才退回去。
                    if await reader.moveNativeCard(id: item.noteID, windowPoint: point) {
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
            if await reader.resizeNativeCard(id: item.noteID,
                                             size: CGSize(width: value.width * unitScale,
                                                          height: value.height * unitScale)) {
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

/// 拖卡时出现在屏幕边缘的两块投放区。几何、配色与判据全部照搬网页那版
/// （rc-voicecall 的 `#vc-trash` / `#vc-dock-hint`，页卡用法见 rc-stickynote
/// onHandleMove/onHandleUp）：
///
/// · 左上角 126×92 的红色区＝删除，**整个拖动期间都在**，手指进区才变"烫"；
/// · 底边整条 150pt 的紫色渐变＝收藏，**只在手指进区时出现**（原版就是
///   `favorite.hint(inZone(...))`，不是一直亮着）。
///
/// ⚠ 判据用的是**手指**位置，不是卡左上角 —— 落点预览才用卡角。原版两个探测点
/// 就是不同的，写成同一个会让"看着在删除区、松手却钉在正文上"。
enum ReaderNativeCardDropZone {
    static let trashSize = CGSize(width: 126, height: 92)
    static let dockHeight: CGFloat = 150
    /// 手指进入判定的高度（130），比视觉高度（150）略小 —— 与原版一致。
    static let dockHitHeight: CGFloat = 130

    static func inTrash(_ point: CGPoint) -> Bool {
        point.x < trashSize.width && point.y < trashSize.height
    }
    static func inDock(_ point: CGPoint, screenHeight: CGFloat) -> Bool {
        screenHeight - point.y < dockHitHeight
    }

    static let danger = Color(red: 1, green: 0.271, blue: 0.227)      // #ff453a
    static let dock = Color(red: 0.482, green: 0.424, blue: 1)        // #7b6cff
}

@MainActor
struct ReaderNativeCardDropZones: View {
    @ObservedObject var drag: ReaderNativeCardDragState
    let origin: CGPoint
    let size: CGSize
    /// 手指（窗口坐标）换到这一层的本地坐标。
    private var finger: CGPoint? {
        drag.finger.map { CGPoint(x: $0.x - origin.x, y: $0.y - origin.y) }
    }

    var body: some View {
        if let finger {
            let hotTrash = ReaderNativeCardDropZone.inTrash(finger)
            let inDock = ReaderNativeCardDropZone.inDock(finger, screenHeight: size.height)
            ZStack(alignment: .topLeading) {
                // 收藏区：底边一条渐变，只在手指进区时出现。
                if inDock {
                    LinearGradient(
                        colors: [ReaderNativeCardDropZone.dock.opacity(0.38),
                                 ReaderNativeCardDropZone.dock.opacity(0.10),
                                 .clear],
                        startPoint: .bottom, endPoint: .top)
                        .frame(height: ReaderNativeCardDropZone.dockHeight)
                        .frame(maxHeight: .infinity, alignment: .bottom)
                        .overlay(alignment: .bottom) {
                            Label("收入收藏夹", systemImage: "star.fill")
                                .font(.caption.weight(.semibold)).foregroundStyle(.white)
                                .padding(.bottom, 18)
                        }
                }
                // 删除区：左上角，整个拖动期间都在；进区变"烫"。
                VStack(spacing: 4) {
                    Image(systemName: "trash")
                        .font(.system(size: 22, weight: .medium))
                        .scaleEffect(hotTrash ? 1.16 : 1)
                        .rotationEffect(.degrees(hotTrash ? -8 : 0))
                    Text("删除").font(.caption.weight(.semibold))
                }
                .foregroundStyle(.white)
                .frame(width: ReaderNativeCardDropZone.trashSize.width,
                       height: ReaderNativeCardDropZone.trashSize.height)
                .background(
                    LinearGradient(
                        colors: hotTrash
                            ? [ReaderNativeCardDropZone.danger, ReaderNativeCardDropZone.danger.opacity(0.8)]
                            : [ReaderNativeCardDropZone.danger.opacity(0.92),
                               ReaderNativeCardDropZone.danger.opacity(0.45)],
                        startPoint: .topLeading, endPoint: .bottomTrailing),
                    in: UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: 0,
                                               bottomTrailingRadius: 24, topTrailingRadius: 0))
                .overlay(
                    UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: 0,
                                           bottomTrailingRadius: 24, topTrailingRadius: 0)
                        .stroke(.white.opacity(hotTrash ? 0.35 : 0), lineWidth: 2))
                .scaleEffect(hotTrash ? 1.06 : 1, anchor: .topLeading)
                .shadow(color: ReaderNativeCardDropZone.danger.opacity(hotTrash ? 0.85 : 0),
                        radius: 22, y: 10)
                .opacity(0.97)
            }
            .frame(width: size.width, height: size.height, alignment: .topLeading)
            .allowsHitTesting(false)
            .animation(.easeOut(duration: 0.14), value: hotTrash)
            .animation(.easeOut(duration: 0.2), value: inDock)
        }
    }
}

/// 卡片拖动的共享状态：手指位置（窗口坐标）与屏幕卡片层在窗口里的位置。
///
/// ⚠ 单独一个小模型：拖动时每帧都在写，挂在阅读器主模型上等于每帧把所有卡重算一遍。
///   文档层（跟 PDF 滚）和屏幕层（投放区）各在一个宿主里，只能靠它共享。
@MainActor
final class ReaderNativeCardDragState: ObservableObject {
    @Published var finger: CGPoint?
    var screenFrame: CGRect = .zero
}

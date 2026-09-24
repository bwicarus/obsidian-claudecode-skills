import SwiftUI

/// The native projection of an existing server favorite, with stable identity.
struct ReaderNativeFavorite: Identifiable, Equatable {
    let id: String
    let label: String
    let isCards: Bool
    let text: String
    let preview: String
    let previewFormat: String
    let page: String
    let file: String
    let ts: Double
    var pinned: Bool
    let pendingConfirmation: Bool

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        label = value["label"] as? String ?? "收藏卡片"
        isCards = value["kind"] as? String == "cards"
        var brief = value["text"] as? String ?? ""
        var rendered = value["content"] as? String ?? ""
        var format = value["isHtml"] as? Bool == true ? "html" : "markdown"
        if isCards, let card = (value["cards"] as? [[String: Any]])?.first,
           let presentation = ReaderNativeCardPresentation.interaction(["card": card, "cardIndex": 0]),
           let front = ((presentation["presentation"] as? [String: Any])?["faces"] as? [[String: Any]])?.first {
            rendered = front["content"] as? String ?? ""
            format = front["format"] as? String ?? "markdown"
        }
        if brief.isEmpty {
            let cards = value["cards"] as? [[String: Any]] ?? []
            brief = cards.compactMap { $0["front"] as? String ?? $0["question"] as? String }.joined(separator: " / ")
        }
        text = brief.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
        preview = rendered.isEmpty ? brief : rendered
        previewFormat = format
        page = value["page"] as? String ?? ""
        file = value["file"] as? String ?? ""
        ts = (value["ts"] as? NSNumber)?.doubleValue ?? 0
        pinned = value["pinned"] as? Bool ?? false
        pendingConfirmation = value["pendingConfirmation"] as? Bool ?? false
    }

}

/// 原版配色（rc-voicecall 的 #vc-dock-btn / #vc-dock-panel 那组 CSS）。
private enum DockStyle {
    static let purple = Color(red: 0xbf / 255, green: 0x5a / 255, blue: 0xf2 / 255)      // --rc-purple
    static let indigo = Color(red: 0x5e / 255, green: 0x5c / 255, blue: 0xe6 / 255)      // --rc-indigo
    static let pick = Color(red: 123 / 255, green: 108 / 255, blue: 1)                   // rgba(123,108,255)
    static let title = Color(red: 0x9f / 255, green: 0xb0 / 255, blue: 0xcf / 255)
    static let label = Color(red: 0xc9 / 255, green: 0xbc / 255, blue: 1)
    static let body = Color(red: 0xaa / 255, green: 0xb6 / 255, blue: 0xcf / 255)
    static let tick = Color(red: 0x7f / 255, green: 0x8a / 255, blue: 0xa6 / 255)
    static let meta = Color(red: 0x6f / 255, green: 0x7d / 255, blue: 0x9e / 255)
    static let cardText = Color(red: 0xe4 / 255, green: 0xe9 / 255, blue: 0xf5 / 255)
    static let danger = Color(red: 0xe0 / 255, green: 0x46 / 255, blue: 0x3c / 255)
}

/// 原版 `#vc-dock-btn`：右下角 40pt 圆钮（紫色收纳图标），右上角小角标是数目；有存货才出现。
struct ReaderNativeFavoritesButton: View {
    @ObservedObject var reader: ReaderWebViewModel
    @ObservedObject var model: ReaderNativeConversationModel

    var body: some View {
        if model.favoritesCount > 0 {
            Button { reader.nativeFavoritesOpen.toggle() } label: {
                Image(systemName: "tray.and.arrow.down")
                    .font(.system(size: 16, weight: .medium))
                    .foregroundStyle(DockStyle.purple)
                    .frame(width: 40, height: 40)
                    .background(.ultraThinMaterial, in: Circle())
                    .background(Color(red: 40 / 255, green: 36 / 255, blue: 64 / 255).opacity(0.72), in: Circle())
                    .overlay(Circle().stroke(Color.white.opacity(0.16), lineWidth: 0.5))
                    .overlay(alignment: .topTrailing) {
                        Text("\(model.favoritesCount)")
                            .font(.system(size: 10, weight: .semibold).monospacedDigit())
                            .foregroundStyle(.white)
                            .padding(.horizontal, 4)
                            .frame(minWidth: 16, minHeight: 16)
                            .background(DockStyle.indigo, in: Capsule())
                            .offset(x: 4, y: -4)
                    }
                    .shadow(color: .black.opacity(0.4), radius: 13, y: 8)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("卡片收藏夹，\(model.favoritesCount) 张")
        }
    }
}

/// 原版 `#vc-dock-panel`：从屏幕底边升起的一条（最高 46%），顶上抓手；横向时间轴按天分组，
/// 天节点在轴上、每张卡上方一根竖线和具体时刻；居中那张最宽、字最多，越远越窄。
///   · 向上拖出一张卡 = 复制到书页（落点就是松手处）；
///   · 长按 = 带入 / 移出对话；
///   · 「选择」→ 点卡红标多选 →「删除所选」；「回收站」里点卡 = 恢复（1 天内）；
///   · 点面板外 = 收起（选择模式 / 回收站里除外）。
struct ReaderNativeFavoritesPanelLayer: View {
    @ObservedObject var reader: ReaderWebViewModel
    @State private var items: [ReaderNativeFavorite] = []
    @State private var trashItems: [ReaderNativeFavorite] = []
    @State private var loading = false
    @State private var selecting = false
    @State private var inTrash = false
    @State private var marked: Set<String> = []
    @State private var focused: String?
    @State private var ghost: (item: ReaderNativeFavorite, point: CGPoint)?
    @State private var panelTop: CGFloat = 0

    var body: some View {
        GeometryReader { geometry in
            if reader.nativeFavoritesOpen {
                ZStack(alignment: .bottom) {
                    // 点面板外收起（原版 _dock._outside；选择模式 / 回收站是明确的操作态，不误关）。
                    Color.black.opacity(0.001)
                        .onTapGesture { if !selecting && !inTrash { close() } }
                    panel(size: geometry.size)
                        .transition(.move(edge: .bottom))
                    if let ghost {
                        ghostCard(ghost.item)
                            .position(x: ghost.point.x - geometry.frame(in: .global).minX,
                                      y: ghost.point.y - geometry.frame(in: .global).minY - 30)
                            .allowsHitTesting(false)
                    }
                }
                .task { await reload() }
            }
        }
        .animation(.spring(response: 0.32, dampingFraction: 0.88), value: reader.nativeFavoritesOpen)
    }

    private func close() {
        reader.nativeFavoritesOpen = false
        selecting = false; inTrash = false; marked = []
    }

    private func reload() async {
        loading = true
        items = await reader.loadNativeFavorites().sorted { $0.ts < $1.ts }
        if focused == nil || !items.contains(where: { $0.id == focused }) { focused = items.last?.id }
        loading = false
    }

    // MARK: 面板

    private func panel(size: CGSize) -> some View {
        VStack(spacing: 0) {
            Capsule().fill(Color(red: 235 / 255, green: 235 / 255, blue: 245 / 255).opacity(0.30))
                .frame(width: 36, height: 5)
                .padding(.top, 6).padding(.bottom, 2)
            header
            timeline(width: size.width)
        }
        .frame(maxWidth: .infinity)
        .frame(maxHeight: size.height * 0.46, alignment: .top)
        .fixedSize(horizontal: false, vertical: true)
        .background(.ultraThinMaterial,
                    in: UnevenRoundedRectangle(topLeadingRadius: 14, bottomLeadingRadius: 0,
                                               bottomTrailingRadius: 0, topTrailingRadius: 14))
        .background(Color(red: 24 / 255, green: 24 / 255, blue: 30 / 255).opacity(0.82),
                    in: UnevenRoundedRectangle(topLeadingRadius: 14, bottomLeadingRadius: 0,
                                               bottomTrailingRadius: 0, topTrailingRadius: 14))
        .overlay(alignment: .top) { Rectangle().fill(Color.white.opacity(0.14)).frame(height: 0.5).padding(.horizontal, 14) }
        .shadow(color: .black.opacity(0.45), radius: 22, y: -14)
        .background(GeometryReader { proxy in
            Color.clear.onAppear { panelTop = proxy.frame(in: .global).minY }
                .onChange(of: proxy.frame(in: .global).minY) { _, value in panelTop = value }
        })
        .environment(\.colorScheme, .dark)
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text(inTrash ? "回收站（1 天内可恢复，点卡片=恢复）" : "卡片收藏夹（向上拖出=复制到屏幕）")
                .font(.system(size: 12)).foregroundStyle(DockStyle.title)
                .frame(maxWidth: .infinity, alignment: .leading)
            if inTrash {
                headerButton("← 返回") { inTrash = false }
            } else {
                headerButton(selecting ? "完成" : "选择", on: selecting) {
                    selecting.toggle(); marked = []
                }
                headerButton("回收站") {
                    inTrash = true; selecting = false; marked = []
                    Task { trashItems = await reader.loadNativeFavoritesTrash().sorted { $0.ts < $1.ts } }
                }
                if selecting {
                    headerButton("删除所选", danger: true) {
                        let ids = Array(marked)
                        guard !ids.isEmpty else { return }
                        Task {
                            if await reader.deleteNativeFavorites(ids) {
                                items.removeAll { ids.contains($0.id) }
                                marked = []; selecting = false
                            }
                        }
                    }
                }
            }
        }
        .padding(.horizontal, 14).padding(.top, 9).padding(.bottom, 4)
    }

    private func headerButton(_ title: String, on: Bool = false, danger: Bool = false,
                              action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.system(size: 12))
                .foregroundStyle(danger ? Color(red: 1, green: 0x8a / 255, blue: 0x80 / 255)
                                 : on ? .white : Color.white.opacity(0.6))
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(on ? Color(red: 0x2b / 255, green: 0x3a / 255, blue: 0x5f / 255) : Color.white.opacity(0.05),
                            in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8)
                    .stroke(danger ? Color(red: 0x7f / 255, green: 0x2a / 255, blue: 0x2a / 255) : Color.white.opacity(0.14),
                            lineWidth: 1))
        }
        .buttonStyle(.plain)
    }

    // MARK: 时间轴

    private enum Entry: Identifiable {
        case day(String)
        case card(ReaderNativeFavorite)
        var id: String {
            switch self {
            case .day(let label): return "day:" + label
            case .card(let item): return item.id
            }
        }
    }

    private static let dayFormat: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "M月d日"; return f
    }()
    private static let timeFormat: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "HH:mm"; return f
    }()

    private func entries(_ list: [ReaderNativeFavorite]) -> [Entry] {
        var out: [Entry] = []
        var lastDay = ""
        for item in list {
            let day = Self.dayFormat.string(from: Date(timeIntervalSince1970: item.ts))
            if day != lastDay { lastDay = day; out.append(.day(day)) }
            out.append(.card(item))
        }
        return out
    }

    @ViewBuilder
    private func timeline(width: CGFloat) -> some View {
        let list = inTrash ? trashItems : items
        if list.isEmpty {
            Text(loading ? "正在读取…" : inTrash ? "回收站是空的" : "空——把浮层卡拖到屏幕底边，或点卡片上的 ☆")
                .font(.system(size: 12)).foregroundStyle(DockStyle.tick)
                .frame(maxWidth: .infinity).padding(.vertical, 22)
        } else {
            let cards = list.map(\.id)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: 14) {
                    ForEach(entries(list)) { entry in
                        switch entry {
                        case .day(let label): dayNode(label)
                        case .card(let item):
                            cell(item, level: level(of: item.id, in: cards), width: width)
                        }
                    }
                }
                .scrollTargetLayout()
                .padding(.horizontal, 16).padding(.top, 14).padding(.bottom, 10)
            }
            .scrollPosition(id: $focused, anchor: .center)
            .animation(.easeOut(duration: 0.28), value: focused)
        }
    }

    /// 离居中那张几张远（原版 data-lvl：0 = 居中最宽，1 = 旁边，2 = 更远）。
    private func level(of id: String, in ids: [String]) -> Int {
        guard let focused, let a = ids.firstIndex(of: focused), let b = ids.firstIndex(of: id) else { return 1 }
        return min(2, abs(a - b))
    }

    private func dayNode(_ label: String) -> some View {
        Text(label)
            .font(.system(size: 11, weight: .semibold)).foregroundStyle(DockStyle.purple)
            .padding(.horizontal, 8)
            .overlay(alignment: .bottom) { Rectangle().fill(DockStyle.pick.opacity(0.5)).frame(height: 1) }
            .frame(height: 18)
            .padding(.trailing, 10).padding(.leading, 2)
    }

    private func cell(_ item: ReaderNativeFavorite, level: Int, width screenWidth: CGFloat) -> some View {
        let width: CGFloat = level == 0 ? min(screenWidth * 0.76, 330) : level == 1 ? 180 : 112
        let meta = [item.file, item.page.isEmpty ? "" : "p" + item.page].filter { !$0.isEmpty }.joined(separator: " · ")
        let isMarked = marked.contains(item.id)
        return VStack(spacing: 0) {
            Text(Self.timeFormat.string(from: Date(timeIntervalSince1970: item.ts)))
                .font(.system(size: 10)).foregroundStyle(DockStyle.tick)
            Rectangle().fill(DockStyle.pick.opacity(0.45)).frame(width: 1, height: 8)
                .padding(.top, 3).padding(.bottom, 4)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.label).font(.subheadline.weight(.semibold)).foregroundStyle(DockStyle.label)
                    .lineLimit(1)
                if item.pendingConfirmation {
                    Text("服务器保存未确认").font(.system(size: 10)).foregroundStyle(.orange)
                }
                if !item.preview.isEmpty {
                    Group {
                        if item.previewFormat == "html" {
                            ReaderNativeCardHTML(html: item.preview, onSelection: { _ in }, imageModel: reader.nativeConversation)
                        } else {
                            ReaderNativeRichDocument(content: item.preview, format: item.previewFormat,
                                imageModel: reader.nativeConversation,
                                font: .preferredFont(forTextStyle: level == 0 ? .body : .subheadline),
                                color: ReaderNativeCardInk.text)
                        }
                    }
                        .frame(maxHeight: level == 0 ? 230 : level == 1 ? 70 : 24, alignment: .top)
                        .clipped()
                        // This is a drag/tap preview; text selection must not steal the dock gesture.
                        .allowsHitTesting(false)
                }
                if !meta.isEmpty, level < 2 {
                    Text(meta).font(.system(size: 10)).foregroundStyle(DockStyle.meta).lineLimit(1).padding(.top, 1)
                }
            }
            .foregroundStyle(DockStyle.cardText)
            .padding(.horizontal, 14).padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.white.opacity(0.1), lineWidth: 0.5))
            // 已带入对话（原版 .vc-picked）/ 选择模式里被标红（.del-mark + 角上 ✕）。
            .overlay(RoundedRectangle(cornerRadius: 12)
                .stroke(isMarked ? DockStyle.danger.opacity(0.9) : item.pinned ? DockStyle.pick.opacity(0.85) : .clear,
                        lineWidth: 1.5))
            .overlay(alignment: .topTrailing) {
                if isMarked {
                    Image(systemName: "xmark").font(.system(size: 8, weight: .bold)).foregroundStyle(.white)
                        .frame(width: 16, height: 16).background(DockStyle.danger, in: Circle())
                        .offset(x: 6, y: -6)
                }
            }
            .contentShape(RoundedRectangle(cornerRadius: 12))
            .onTapGesture { tap(item) }
            // simultaneous：横滑仍归时间轴滚动，只有明显朝上的那一下才被认成"拖出"。
            .simultaneousGesture(inTrash || selecting ? nil : pullOut(item))
            .simultaneousGesture(inTrash || selecting ? nil : LongPressGesture(minimumDuration: 0.6).onEnded { _ in pin(item) })
        }
        .frame(width: width)
        .id(item.id)
        .animation(.easeOut(duration: 0.28), value: width)
    }

    private func tap(_ item: ReaderNativeFavorite) {
        if inTrash {
            Task {
                if await reader.restoreNativeFavorite(item.id) {
                    trashItems.removeAll { $0.id == item.id }
                    await reload()
                }
            }
        } else if selecting {
            if marked.contains(item.id) { marked.remove(item.id) } else { marked.insert(item.id) }
        } else {
            focused = item.id   // 点一张 = 把它挪到中间展开
        }
    }

    private func pin(_ item: ReaderNativeFavorite) {
        Task {
            guard let on = await reader.toggleNativeFavoritePin(item.id),
                  let index = items.firstIndex(where: { $0.id == item.id }) else { return }
            items[index].pinned = on
            reader.showTransientNotice(on ? "已带入对话" : "已从对话中移出")
        }
    }

    /// 向上拖出 = 复制到书页。横滑留给时间轴，只有**明显朝上**才算拖出（原版 touch-action:pan-x）。
    private func pullOut(_ item: ReaderNativeFavorite) -> some Gesture {
        DragGesture(minimumDistance: 12, coordinateSpace: .global)
            .onChanged { value in
                let up = -value.translation.height
                if ghost == nil {
                    guard up > 14, up > abs(value.translation.width) else { return }
                }
                ghost = (item, value.location)
            }
            .onEnded { value in
                defer { ghost = nil }
                guard ghost != nil, value.location.y < panelTop - 8 else { return }
                let point = value.location
                Task {
                    if await reader.placeNativeFavorite(item, windowPoint: point) {
                        reader.showTransientNotice("已复制到书页")
                        close()
                    }
                }
            }
    }

    private func ghostCard(_ item: ReaderNativeFavorite) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(item.label).font(.system(size: 12, weight: .semibold)).foregroundStyle(DockStyle.label)
            if !item.preview.isEmpty {
                ReaderNativeRichText(content: item.preview, format: item.previewFormat,
                    font: .preferredFont(forTextStyle: .subheadline), color: ReaderNativeCardInk.text)
                    .frame(maxHeight: 64, alignment: .top).clipped().allowsHitTesting(false)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .frame(width: 220, alignment: .leading)
        .background(Color(red: 30 / 255, green: 32 / 255, blue: 42 / 255).opacity(0.94), in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color(red: 126 / 255, green: 171 / 255, blue: 1).opacity(0.48), lineWidth: 1))
        .shadow(color: .black.opacity(0.55), radius: 25, y: 18)
        .environment(\.colorScheme, .dark)
    }
}

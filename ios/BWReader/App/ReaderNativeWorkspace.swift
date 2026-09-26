import SwiftUI

/// 固定项之间的分隔符，用「单元分隔符」而不是换行/逗号：工具标题就是网页按钮的
/// title，里面出现标点是常态，用常见字符当分隔符迟早把一个名字劈成两半。
///
/// ⚠ 放在类型外面是因为 `ReaderNativeWorkspace` 是**泛型**结构体，
/// Swift 不允许泛型类型里有 static 存储属性（2026-09-22 build 845 就红在这）。
private let readerPinnedToolSeparator: Character = "\u{1F}"

/// App navigation and conversation surround the existing document surface.
/// The WebView and Pencil canvas stay together so their coordinate system is unchanged.
@MainActor
struct ReaderNativeWorkspace<Document: View>: View {
    @ObservedObject var reader: ReaderWebViewModel
    @ObservedObject var conversation: ReaderNativeConversationModel
    @ObservedObject var voiceBridge: NativeVoiceBridge
    @Binding var enabled: Bool
    let openLibrary: () -> Void
    let openSettings: () -> Void
    let openDiagnostics: () -> Void
    let openFaultLog: () -> Void
    @ViewBuilder let document: () -> Document

    @AppStorage("reader.navigationCollapsed") private var navigationCollapsed = false
    @State private var dropTarget = false
    /// 侧栏宽度（点）。0 = 还没调过，按屏幕比例给默认值。
    /// ⚠ 存起来而不是每次重算：调过一次就该一直是那个宽度，
    ///   否则每次开侧栏都要重新拖一遍。
    @AppStorage("reader.sidebarWidth") private var sidebarWidth: Double = 0
    /// 钉在顶栏的阅读工具。存的是换行分隔的身份串。
    /// ⚠ 身份优先用网页按钮的 id（稳定）；没 id 的才退回标题 ——
    ///   不能用 actionId，它带着 scope，**换一本书就变**，固定会自己掉。
    @AppStorage("reader.pinnedTools") private var pinnedToolsRaw = ""

    private var pinnedToolIDs: [String] {
        pinnedToolsRaw.split(separator: readerPinnedToolSeparator).map(String.init).filter { !$0.isEmpty }
    }

    private var readingTools: [ReaderNativeControl] {
        reader.nativePDFDocument == nil ? conversation.readingTools :
            conversation.readingTools.filter { $0.key == "page" } + ReaderNativePDFToolbar.controls()
    }

    private func toolIdentity(_ control: ReaderNativeControl) -> String {
        control.key.isEmpty ? "t:" + control.title : "k:" + control.key
    }

    /// 顶栏上要画的那几个，按用户选的顺序。
    /// ⚠ 本书没有的工具就不画，但**不从设置里删** ——
    ///   换本书又有了就该回来，静静清掉才是真丢东西。
    private var pinnedTools: [ReaderNativeControl] {
        let wanted = pinnedToolIDs
        return readingTools
            .filter { $0.key != "page" && wanted.contains(toolIdentity($0)) }
            .sorted { a, b in
                (wanted.firstIndex(of: toolIdentity(a)) ?? 0) < (wanted.firstIndex(of: toolIdentity(b)) ?? 0)
            }
    }

    private func togglePinned(_ control: ReaderNativeControl) {
        let identity = toolIdentity(control)
        var list = pinnedToolIDs
        if let at = list.firstIndex(of: identity) { list.remove(at: at) } else { list.append(identity) }
        pinnedToolsRaw = list.joined(separator: String(readerPinnedToolSeparator))
    }

    private func runReadingTool(_ control: ReaderNativeControl) {
        if control.id.hasPrefix("native-pdf-") {
            let action = String(control.id.dropFirst("native-pdf-".count))
            if action == "library" { openLibrary(); return }
            Task { await reader.runNativePDFToolbar(action) }
            return
        }
        // ⚠ 新建便签在原生接管时**必须**走原生那条路：网页的
        // createAtCenter 靠 document.elementFromPoint 找落点，接管后一页都不在
        // DOM 里，七个候选点全落空 —— 便签没建，连"放不了"的 toast 也看不见。
        if control.key == "note-new", reader.nativePDFDocument != nil {
            reader.createNativeStickyNote()
            return
        }
        Task { await conversation.perform("liveAction", parameters: ["actionId": control.id]) }
    }

    private var nativeSidebarVisible: Bool {
        enabled && conversation.sidebarOpen && !conversation.legacyVisible
    }

    var body: some View {
        VStack(spacing: 0) {
            if !navigationCollapsed { navigationBar }
            GeometryReader { geometry in
                HStack(spacing: 0) {
                    GeometryReader { page in
                        document()
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .dropDestination(for: ReaderNativeCardTransfer.self) { values, location in
                                // ⚠ 这里以前一律 `return false` 就完事：拖过去、卡飞回侧栏、
                                //   一个字都没有。放不下也要说是为什么。
                                // 放下即收起虚线框：isTargeted 在成功放下后不一定回 false，
                                // 框会一直挂在正文上（2026-09-26 用户：「拖动后屏幕区域出现了虚线勾边」）。
                                dropTarget = false
                                reader.postClientLog("[card-drop] received n=\(values.count)")
                                guard let payload = values.first, page.size.width > 0, page.size.height > 0 else {
                                    reader.showTransientNotice("没能读出这张卡，请重试。")
                                    return false
                                }
                                guard payload.scope == conversation.scope else {
                                    reader.showTransientNotice("这张卡属于另一个会话，先切回去再拖。")
                                    return false
                                }
                                guard !payload.actionID.isEmpty else {
                                    reader.showTransientNotice("这张卡不支持放到书页上。")
                                    return false
                                }
                                Task {
                                    let frame = page.frame(in: .global)
                                    await reader.placeNativeConversationCard(
                                        actionID: payload.actionID, scope: payload.scope,
                                        windowPoint: CGPoint(x: frame.minX + location.x, y: frame.minY + location.y)
                                    )
                                    // 放置失败（落点不在正文、卡正文过大…）网页那侧会给理由，
                                    // 但那条信息只写在 model.error 里，侧栏不一定开着。
                                    if let failure = conversation.error { reader.showTransientNotice(failure) }
                                }
                                return true
                            } isTargeted: { dropTarget = $0 }
                            .overlay(alignment: .bottom) {
                                // EPUB 的选区操作条。PDF 不出 —— 它有自己的选区菜单，
                                // 两套都出就是同一个选区上下各一排按钮。
                                if enabled, reader.isEPUBBook, !conversation.readerSelectionText.isEmpty {
                                    ReaderNativeEPUBSelectionBar(
                                        text: conversation.readerSelectionText,
                                        colors: reader.epubHighlightColors
                                    ) { reader.performEPUBSelectionAction($0) }
                                }
                            }
                            .animation(.easeOut(duration: 0.18), value: conversation.readerSelectionText)
                            // 把手挂在**正文区**右缘：侧栏一开正文就窄了，把手自然
                            // 落在两者的交界上，跟网页那只的位置一致；侧栏关着时它
                            // 贴在屏幕右缘 —— 那正是用来打开侧栏的地方。
                            .overlay(alignment: .trailing) {
                                if enabled {
                                    ReaderNativeSidebarGrip(
                                        width: $sidebarWidth,
                                        available: geometry.size.width,
                                        open: nativeSidebarVisible,
                                        onToggle: { Task { await conversation.perform("toggleAssistant") } }
                                    )
                                    .disabled(!conversation.supports("toggleAssistant"))
                                }
                            }
                            .overlay {
                                if dropTarget {
                                    RoundedRectangle(cornerRadius: 8)
                                        .stroke(ReaderNativeTheme.accent, style: StrokeStyle(lineWidth: 2, dash: [6]))
                                        .padding(4).allowsHitTesting(false)
                                }
                            }
                    }
                    if nativeSidebarVisible {
                        ReaderNativeConversationView(
                            model: conversation, voiceBridge: voiceBridge,
                            onClose: { Task { await conversation.perform("toggleAssistant") } },
                            onDiagnostics: openDiagnostics,
                            onSettings: { Task { await conversation.perform("openModels") } }
                        )
                        .frame(width: ReaderNativeSidebarGrip.clamp(
                            sidebarWidth > 0 ? sidebarWidth : geometry.size.width * 0.36,
                            available: geometry.size.width))
                    }
                }
                // ⚠ 展开把手跟收起按钮**同一个位置**（顶部居中）。
                //   原来收起在右、展开在左，用户得满屏找它去了哪里
                //   （2026-09-22 用户：“打开在右边关闭在左边很蠢”）。
                // ⚠ 收起和展开是**同一个把手、同一个位置**（正文区顶部居中）。
                //   收起按钮原来长在顶栏**里面的右端** —— 一旦收起，它就跟顶栏
                //   一起消失，再出现时却在别处（2026-09-22 用户："上边栏关闭的
                //   按钮也应该在外部中间而不是内部右边"）。
                .overlay(alignment: .top) {
                    Button { navigationCollapsed.toggle() } label: {
                        Image(systemName: navigationCollapsed ? "chevron.down" : "chevron.up")
                            .font(.caption.weight(.semibold))
                            .frame(width: 44, height: 26)
                            .readerGlass(in: Capsule(), fallback: .regularMaterial)
                    }
                    .accessibilityLabel(navigationCollapsed ? "展开阅读顶栏" : "收起阅读顶栏")
                    .padding(6)
                }
            }
        }
        .background(ReaderNativeTheme.canvas)
        .foregroundStyle(ReaderNativeTheme.ink)
        .tint(ReaderNativeTheme.accent)
        .task(id: enabled) { await reader.setNativeConversationMode(enabled) }
        // 色板跟着书走：换书可能换了语言/设置，取一次就够（它只在有选中时才用得上）。
        .task(id: conversation.scope) { reader.refreshEPUBHighlightColors() }
        .sheet(item: $conversation.settingsPanel) { panel in
            ReaderNativeSettingsView(model: panel)
        }
        .sheet(item: $conversation.readingSettingsPanel) { panel in
            ReaderNativeReadingSettingsView(
                model: panel,
                nativePDFMountFailure: reader.nativePDFMountFailure,
                onCloudSyncChanged: { reader.setCloudSyncEnabled($0) }
            )
        }
        .sheet(item: $reader.nativeLookup) { panel in
            ReaderNativeLookupView(model: panel)
        }
        .sheet(item: $reader.nativeFigure) { panel in
            ReaderNativeFigureView(model: panel)
        }
        .sheet(item: $reader.nativeGrammar) { panel in
            ReaderNativeGrammarView(model: panel)
        }
        .sheet(item: $reader.nativeHighlightEditor) { panel in
            ReaderNativeHighlightEditor(model: panel)
        }
        .sheet(item: $reader.nativePDFToolPanel) { panel in
            ReaderNativePDFToolView(model: panel)
        }
        .sheet(item: $conversation.searchPanel) { panel in
            ReaderNativeSearchView(model: panel)
        }
    }

    private var navigationBar: some View {
        HStack(spacing: 10) {
            Button(action: openLibrary) {
                Label("书库", systemImage: "books.vertical")
                    .font(.subheadline.weight(.medium))
            }
            .accessibilityHint("打开本机与同步书库")
            if enabled {
                Text(conversation.title.isEmpty ? "阅读" : conversation.title)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(ReaderNativeTheme.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    if conversation.capabilities.contains("openSearch") {
                        Button {
                            Task { await conversation.perform("openSearch") }
                        } label: { Image(systemName: "magnifyingglass") }
                        .accessibilityLabel("搜索书籍")
                    }
                    if conversation.capabilities.contains("openTOC") {
                        Button {
                            Task { await conversation.perform("openTOC") }
                        } label: { Image(systemName: "list.bullet") }
                        .accessibilityLabel("书籍目录")
                        .popover(item: $conversation.tocPanel) { panel in
                            ReaderNativeTOCView(model: panel)
                                .frame(idealWidth: 340, idealHeight: 520)
                                .presentationCompactAdaptation(.sheet)
                        }
                    }
            } else {
                Spacer(minLength: 0)
                Button("启用原生导航") { enabled = true }
                    .font(.subheadline.weight(.medium))
            }
            if enabled, let page = conversation.readingTools.first(where: { $0.key == "page" }) {
                Button(page.title) {
                    Task { await conversation.perform("openNavigation") }
                }
                .font(.caption.monospacedDigit())
                .accessibilityLabel("跳转页码：" + page.title)
                .popover(item: $conversation.navigationPanel) { panel in
                    ReaderNativeNavigationView(model: panel)
                        .presentationCompactAdaptation(.popover)
                }
            }
            // 钉在顶栏上的工具（用户自己选）。
            // ⚠ 不由我挑"常用的几个" —— 每本书、每个人常用的不一样，
            //   挑错了就是"我要的那个又得点两下"。
            if enabled {
                ForEach(pinnedTools) { control in
                    Button(control.title) { runReadingTool(control) }
                        .font(.caption.weight(.medium))
                        .padding(.horizontal, 8).frame(height: 30)
                        .readerGlass(in: RoundedRectangle(cornerRadius: 9),
                                     fallback: ReaderNativeTheme.accentWash)
                        .disabled(control.disabled)
                        .accessibilityLabel(control.title)
                }
            }
            if enabled && !readingTools.isEmpty {
                Menu {
                    ForEach(readingTools.filter { $0.key != "page" }) { control in
                        Button(control.title) { runReadingTool(control) }
                            .disabled(control.disabled)
                    }
                    Divider()
                    Menu("固定到顶栏…") {
                        ForEach(readingTools.filter { $0.key != "page" }) { control in
                            Toggle(control.title, isOn: Binding(
                                get: { pinnedToolIDs.contains(toolIdentity(control)) },
                                set: { _ in togglePinned(control) }
                            ))
                        }
                    }
                } label: {
                    Image(systemName: "textformat.size")
                        .frame(width: 36, height: 36)
                }
                .accessibilityLabel("阅读工具：翻页、适应、翻译与批注")
            }
            if enabled && conversation.legacyVisible {
                Button("返回原生助手") {
                    Task { await conversation.perform("hideLegacy") }
                }.font(.caption)
            }
            Menu {
                Button("App 设置", systemImage: "slider.horizontal.3", action: openSettings)
                if conversation.capabilities.contains("openSettings") {
                    Button("阅读设置", systemImage: "textformat.size") {
                        Task { await conversation.perform("openSettings") }
                    }
                }
                if conversation.capabilities.contains("openModels") {
                    Button("模型与声音", systemImage: "waveform") {
                        Task { await conversation.perform("openModels") }
                    }
                }
                Button("通话诊断", systemImage: "waveform.path.ecg", action: openDiagnostics)
                // ⚠ 入口**总是在**，不只在"有待发送报告"时才出现。
                //   最难的一类情况恰恰是"一条报告都没有" ——
                //   那时候需要看的是面包屑和上报通道自己的状态。
                Button("故障现场", systemImage: "stethoscope", action: openFaultLog)
                // ⚠ “完整阅读界面”（showLegacy）入口已移除（2026-09-22 用户：
                //   “点击完整功能按钮后旧的侧边栏又出现了，不是说删除了么”）。
                //   旧界面在 App 上不再是一个用户可以主动进去的地方。
                //   底层 setLegacy 仍保留，因为搜索/复习那几个还没原生化的面要用它。
                Divider()
                Toggle("原生界面", isOn: $enabled)
            } label: {
                Image(systemName: "slider.horizontal.3")
                    .frame(minWidth: 32, minHeight: 36)
            }
            .accessibilityLabel("阅读与 App 设置")
            if enabled {
                Button {
                    Task { await conversation.perform("toggleAssistant") }
                } label: {
                    Image(systemName: conversation.sidebarOpen ? "sidebar.right" : "bubble.left.and.bubble.right")
                        .font(.title3)
                        .frame(width: 38, height: 36)
                        .background(conversation.sidebarOpen ? ReaderNativeTheme.accentWash : .clear,
                                    in: RoundedRectangle(cornerRadius: 11))
                }
                .accessibilityLabel(conversation.sidebarOpen ? "收起 AI 侧栏" : "展开 AI 侧栏")
                .disabled(!conversation.supports("toggleAssistant") || conversation.isPerforming("toggleAssistant"))
            }
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 14)
        .frame(height: 40)
        .background(ReaderNativeTheme.card)
    }
}

/// 侧栏把手 —— 照网页那只做（`#ep-side-handle`）：正文区右缘中间一枚竖排标签，
/// **点开关侧栏、拖调宽度**，侧栏关着的时候它照样在。
///
/// ⚠ 上一版只有"拖杆"，而且只在侧栏**已经打开**时才挂进 HStack —— 关着的时候
/// 屏幕上根本没有这个东西，于是"把手始终没看见"（2026-09-22 用户实报两次）。
/// 网页那只的第一职责本来就是**打开侧栏**，宽度是它附带的第二件事。
@MainActor
struct ReaderNativeSidebarGrip: View {
    @Binding var width: Double
    let available: CGFloat
    var open: Bool
    var onToggle: () -> Void
    @State private var startWidth: Double?
    @State private var dragged = false

    /// ⚠ 上下界跟着屏幕走：写死的最小值在窄屏上会把正文挤没。
    static func clamp(_ value: Double, available: CGFloat) -> CGFloat {
        let maximum = max(280, Double(available) - 320)
        return CGFloat(min(max(value, 280), maximum))
    }

    var body: some View {
        Text("助手 · 知识点")
            .font(.caption2.weight(.medium))
            .foregroundStyle(ReaderNativeTheme.ink.opacity(0.85))
            .lineLimit(1)
            .fixedSize()
            .rotationEffect(.degrees(90))
            .frame(width: 26, height: 116)
            .readerGlass(in: UnevenRoundedRectangle(
                topLeadingRadius: 14, bottomLeadingRadius: 14,
                bottomTrailingRadius: 0, topTrailingRadius: 0),
                         fallback: ReaderNativeTheme.accentWash)
            .overlay(alignment: .leading) {
                Capsule().fill(ReaderNativeTheme.muted.opacity(0.55))
                    .frame(width: 3, height: 34).padding(.leading, 3)
            }
            .contentShape(Rectangle())
            // ⚠ 拖动优先于点击：先判有没有真拖过，没拖过才算点击。
            //   两个手势分开挂会互相吞（拖到一半松手也触发 toggle）。
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        guard open else { return }
                        if abs(value.translation.width) < 3, !dragged { return }
                        dragged = true
                        if startWidth == nil {
                            startWidth = width > 0 ? width : Double(available) * 0.36
                        }
                        width = Double(Self.clamp((startWidth ?? 0) - Double(value.translation.width),
                                                  available: available))
                    }
                    .onEnded { _ in
                        if !dragged { onToggle() }
                        dragged = false
                        startWidth = nil
                    }
            )
            .accessibilityLabel(open ? "收起 AI 侧栏" : "展开 AI 侧栏")
            .accessibilityHint("左右拖动可调整侧栏宽度")
            .accessibilityAdjustableAction { direction in
                let base = width > 0 ? width : Double(available) * 0.36
                width = Double(Self.clamp(base + (direction == .increment ? 40 : -40), available: available))
            }
    }
}

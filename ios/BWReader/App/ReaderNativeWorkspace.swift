import SwiftUI

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
    @ViewBuilder let document: () -> Document

    @AppStorage("reader.navigationCollapsed") private var navigationCollapsed = false
    @State private var dropTarget = false

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
                                guard let payload = values.first,
                                      payload.scope == conversation.scope,
                                      !payload.actionID.isEmpty,
                                      page.size.width > 0, page.size.height > 0 else { return false }
                                Task {
                                    let frame = page.frame(in: .global)
                                    await reader.placeNativeConversationCard(
                                        actionID: payload.actionID, scope: payload.scope,
                                        windowPoint: CGPoint(x: frame.minX + location.x, y: frame.minY + location.y)
                                    )
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
                            .overlay {
                                if dropTarget {
                                    RoundedRectangle(cornerRadius: 8)
                                        .stroke(ReaderNativeTheme.accent, style: StrokeStyle(lineWidth: 2, dash: [6]))
                                        .padding(4).allowsHitTesting(false)
                                }
                            }
                    }
                    if nativeSidebarVisible {
                        Divider()
                        ReaderNativeConversationView(
                            model: conversation, voiceBridge: voiceBridge,
                            onClose: { Task { await conversation.perform("toggleAssistant") } },
                            onDiagnostics: openDiagnostics,
                            onSettings: { Task { await conversation.perform("openModels") } }
                        )
                        .frame(width: min(420, max(300, geometry.size.width * 0.36)))
                    }
                }
                .overlay(alignment: .topLeading) {
                    if navigationCollapsed {
                        Button { navigationCollapsed = false } label: {
                            Image(systemName: "chevron.down")
                                .font(.caption.weight(.semibold))
                                .frame(width: 44, height: 26)
                                .background(.regularMaterial, in: Capsule())
                        }
                        .accessibilityLabel("展开阅读顶栏")
                        .padding(6)
                    }
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
            if enabled && !conversation.readingTools.isEmpty {
                Menu {
                    ForEach(conversation.readingTools.filter { $0.key != "page" }) { control in
                        Button(control.title) {
                            // ⚠ 新建便签在原生接管时**必须**走原生那条路：网页的
                            // createAtCenter 靠 document.elementFromPoint 找落点，
                            // 接管后一页都不在 DOM 里，七个候选点全落空 —— 便签没建，
                            // 连"放不了"的 toast 也看不见。
                            if control.key == "note-new", reader.nativePDFDocument != nil {
                                reader.createNativeStickyNote()
                                return
                            }
                            Task { await conversation.perform("liveAction", parameters: ["actionId": control.id]) }
                        }
                        .disabled(control.disabled)
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
                if conversation.capabilities.contains("showLegacy") {
                    Button("完整阅读界面", systemImage: "rectangle.on.rectangle") {
                        Task { await conversation.perform("showLegacy") }
                    }
                }
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
            Button { navigationCollapsed = true } label: {
                Image(systemName: "chevron.up").frame(width: 36, height: 36)
            }
            .accessibilityLabel("收起阅读顶栏")
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 14)
        .frame(height: 40)
        .background(ReaderNativeTheme.card)
    }
}

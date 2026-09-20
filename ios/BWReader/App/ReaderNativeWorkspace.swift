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

    @AppStorage("reader.nativeAssistantVisible") private var assistantVisible = true
    @Environment(\.horizontalSizeClass) private var sizeClass

    var body: some View {
        GeometryReader { geometry in
            let wide = sizeClass == .regular && geometry.size.width >= 820
            let assistantWidth = min(400, max(310, geometry.size.width * 0.31))
            let showsAssistant = enabled && assistantVisible && !conversation.legacyVisible
            VStack(spacing: 0) {
                navigationBar
                Divider().overlay(ReaderNativeTheme.separator)
                ZStack(alignment: .trailing) {
                    document()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .padding(.trailing, showsAssistant && wide ? assistantWidth : 0)
                    if showsAssistant && !wide {
                        ReaderNativeTheme.ink.opacity(0.12)
                            .contentShape(Rectangle())
                            .onTapGesture { assistantVisible = false }
                    }
                    // Keep one mounted sidebar while rotating, inspecting an
                    // original artifact or closing it; drafts and scroll state survive.
                    assistant
                        .frame(width: wide ? assistantWidth : min(390, geometry.size.width))
                        .background(ReaderNativeTheme.canvas)
                        .overlay(alignment: .leading) { Divider().overlay(ReaderNativeTheme.separator) }
                        .shadow(color: .black.opacity(wide ? 0 : 0.12), radius: 16, x: -4)
                        .opacity(showsAssistant ? 1 : 0)
                        .allowsHitTesting(showsAssistant)
                        .accessibilityHidden(!showsAssistant)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .background(ReaderNativeTheme.canvas)
            .foregroundStyle(ReaderNativeTheme.ink)
            .tint(ReaderNativeTheme.accent)
            .task(id: enabled) { await reader.setNativeConversationMode(enabled) }
        }
    }

    private var assistant: some View {
        ReaderNativeConversationView(
            model: conversation,
            voiceBridge: voiceBridge,
            onClose: { withAnimation(.easeInOut(duration: 0.18)) { assistantVisible = false } },
            onDiagnostics: openDiagnostics,
            onSettings: {
                if conversation.capabilities.contains("openModels") {
                    Task { await conversation.perform("openModels") }
                } else { openSettings() }
            }
        )
        .background(ReaderNativeTheme.canvas)
    }

    private var navigationBar: some View {
        HStack(spacing: 12) {
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
                if conversation.legacyVisible {
                    Button {
                        Task {
                            if await conversation.perform("hideLegacy") { assistantVisible = true }
                        }
                    } label: {
                        Label("返回侧栏", systemImage: "sidebar.right")
                            .font(.subheadline)
                    }
                } else {
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
                    }
                }
            } else {
                Spacer(minLength: 0)
                Button("返回原生界面") { enabled = true; assistantVisible = true }
                    .font(.subheadline.weight(.medium))
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
                    if conversation.legacyVisible {
                        Task { await conversation.perform("hideLegacy"); assistantVisible = true }
                    } else {
                        withAnimation(.easeInOut(duration: 0.18)) { assistantVisible.toggle() }
                    }
                } label: {
                    Image(systemName: assistantVisible ? "sidebar.right" : "bubble.left.and.bubble.right")
                        .font(.title3)
                        .frame(width: 38, height: 36)
                        .background(assistantVisible ? ReaderNativeTheme.accentWash : .clear,
                                    in: RoundedRectangle(cornerRadius: 11))
                }
                .accessibilityLabel(assistantVisible ? "收起 AI 侧栏" : "展开 AI 侧栏")
            }
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 14)
        .frame(height: 48)
        .background(ReaderNativeTheme.card)
    }
}

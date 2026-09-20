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

    var body: some View {
            VStack(spacing: 0) {
                navigationBar
                Divider().overlay(ReaderNativeTheme.separator)
                // Cards, selection chips and book placements must share the
                // same live interaction surface. A SwiftUI preview cannot
                // replace the card/review state machine or its drag session.
                    document()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .background(ReaderNativeTheme.canvas)
            .foregroundStyle(ReaderNativeTheme.ink)
            .tint(ReaderNativeTheme.accent)
            .task(id: enabled) { await reader.setNativeConversationMode(enabled) }
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
            } else {
                Spacer(minLength: 0)
                Button("启用原生导航") { enabled = true }
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
                Toggle("原生导航", isOn: $enabled)
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
        .frame(height: 48)
        .background(ReaderNativeTheme.card)
    }
}

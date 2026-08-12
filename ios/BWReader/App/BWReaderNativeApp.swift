import CoreSpotlight
import SwiftUI
import WidgetKit

@main
struct BWReaderNativeApp: App {
    @StateObject private var voiceBridge = NativeVoiceBridge()
    @StateObject private var nativeCommandReceiver =
        ReaderNativeCommandReceiver()

    var body: some Scene {
        WindowGroup {
            ReaderRootView(
                voiceBridge: voiceBridge,
                nativeCommandReceiver: nativeCommandReceiver
            )
        }
    }
}

private struct ReaderRootView: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var reader = ReaderWebViewModel()
    @StateObject private var remoteLibrary = ReaderRemoteLibraryCoordinator()
    @ObservedObject var voiceBridge: NativeVoiceBridge
    @ObservedObject var nativeCommandReceiver: ReaderNativeCommandReceiver
    @State private var showsDiagnostics = false
    @State private var showsNativeTools = false
    @State private var showsLibrary = false
    @State private var startupResolutionPending = true
    @State private var startupRouteOverrideRequested = false
    @State private var libraryStartupNotice: String?
    @State private var nativeToolsInitialAction: ReaderNativeFeatureAction?

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            ReaderWebView(model: reader)
                .ignoresSafeArea(edges: .bottom)

            NativePencilLiveOverlay(
                reader: reader,
                controller: reader.nativePencilInk
            )
            .ignoresSafeArea(edges: .bottom)

            if reader.isLoading {
                ProgressView()
                    .tint(.white)
                    .padding(12)
                    .background(.ultraThinMaterial, in: Capsule())
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                    .padding(.top, 8)
                    .allowsHitTesting(false)
            }

            if startupResolutionPending && !reader.isLoading {
                ProgressView("正在恢复上次阅读")
                    .tint(.white)
                    .padding(14)
                    .background(.ultraThinMaterial, in: Capsule())
                    .allowsHitTesting(false)
            }

            if let message = reader.loadError {
                ReaderLoadError(message: message) {
                    reader.reload()
                }
            }

            if let notice = nativeCommandReceiver.notice {
                Text(notice)
                    .font(.footnote)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.top, 10)
                    .onTapGesture {
                        nativeCommandReceiver.dismissNotice()
                    }
                    .frame(
                        maxWidth: .infinity,
                        maxHeight: .infinity,
                        alignment: .top
                    )
            }


            if let message = reader.nativePencilInk.lastError {
                Text("Pencil 笔迹未保存：\(message)（点按重试）")
                    .font(.footnote)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.bottom, 14)
                    .onTapGesture {
                        reader.nativePencilInk.retry()
                    }
                    .frame(
                        maxWidth: .infinity,
                        maxHeight: .infinity,
                        alignment: .bottom
                    )
            }

            // Hidden, nonvisual support entry: long-press the bottom-left
            // corner for two seconds. It never changes Reader page behavior.
            Color.clear
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
                .onLongPressGesture(minimumDuration: 2) {
                    showsDiagnostics = true
                }
                .accessibilityLabel("打开电脑语音诊断")
                .frame(
                    maxWidth: .infinity,
                    maxHeight: .infinity,
                    alignment: .bottomLeading
                )
                .padding(4)

            HStack(spacing: 8) {
                Button {
                    showsLibrary = true
                } label: {
                    Image(systemName: "books.vertical")
                        .font(.system(size: 17, weight: .semibold))
                        .frame(width: 44, height: 44)
                        .background(.ultraThinMaterial, in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("打开书库")

                Button {
                    nativeToolsInitialAction = .openNativeTools
                    showsNativeTools = true
                } label: {
                    Image(systemName: "text.viewfinder")
                        .font(.system(size: 17, weight: .semibold))
                        .frame(width: 44, height: 44)
                        .background(.ultraThinMaterial, in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("打开原生阅读工具")
            }
            .frame(
                maxWidth: .infinity,
                maxHeight: .infinity,
                alignment: .topTrailing
            )
            .padding(.top, 8)
            .padding(.trailing, 10)

        }
        .preferredColorScheme(.dark)
        .task {
            voiceBridge.bind(reader: reader)
            nativeCommandReceiver.bind(
                reader: reader,
                voiceBridge: voiceBridge
            )
            reader.bind(remoteLibrary: remoteLibrary)
            let localLibrary = ReaderLocalLibraryManager.shared
            if localLibrary.isConfigured {
                let cookies = await reader.remoteLibraryCookies()
                await remoteLibrary.refresh(
                    cookies: cookies,
                    localLibrary: localLibrary
                )
            }
        }
        .task {
            await restoreInitialLocalBook()
        }
        .onOpenURL { url in
            if let route = ReaderNativeActivityRoute.parse(url) {
                handleNativeFeatureRoute(route)
            } else {
                nativeCommandReceiver.receive(url)
            }
        }
        .onContinueUserActivity(CSSearchableItemActionType) { activity in
            if let route = ReaderNativeActivityRoute.parse(activity) {
                handleNativeFeatureRoute(route)
            }
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            reader.setReaderScenePhase(phase)
            voiceBridge.setAppForeground(phase == .active)
        }
        .onReceive(reader.$libraryPresentationRequestID) { requestID in
            guard requestID != nil else { return }
            showsLibrary = true
        }
        .task {
            BWReaderAppShortcuts.updateAppShortcutParameters()
        }
        .task {
            while !Task.isCancelled {
                await ReaderLocalNotesManager.shared.drainPendingCreates()
                do {
                    try await Task.sleep(nanoseconds: 2_000_000_000)
                } catch {
                    return
                }
            }
        }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            consumePendingNativeFeatureRequest()
            await refreshNativeFeatureSnapshot()
            var secondsUntilSnapshotRefresh = 12
            while !Task.isCancelled {
                do {
                    try await Task.sleep(nanoseconds: 1_000_000_000)
                } catch {
                    return
                }
                consumePendingNativeFeatureRequest()
                secondsUntilSnapshotRefresh -= 1
                if secondsUntilSnapshotRefresh <= 0 {
                    await refreshNativeFeatureSnapshot()
                    secondsUntilSnapshotRefresh = 12
                }
            }
        }
        .sheet(isPresented: $showsDiagnostics) {
            NativeVoiceDiagnosticsView(bridge: voiceBridge)
        }
        .sheet(isPresented: $showsNativeTools) {
            NativeReaderToolsView(
                reader: reader,
                initialAction: nativeToolsInitialAction
            )
        }
        .sheet(isPresented: $showsLibrary) {
            ReaderLocalLibraryView(
                reader: reader,
                startupNotice: libraryStartupNotice,
                remote: remoteLibrary
            )
        }
    }

    @MainActor
    private func restoreInitialLocalBook() async {
        guard startupResolutionPending else { return }
        defer { startupResolutionPending = false }

        let store = ReaderLastLocalBookStore.shared
        guard let reference = store.load() else {
            if store.hasStoredValue {
                store.clear()
                libraryStartupNotice = "上次阅读记录已损坏，已清除；请重新打开一本书。"
            }
            reader.finishInitialBookDecision()
            showsLibrary = true
            return
        }

        let library = ReaderLocalLibraryManager.shared
        guard library.isConfigured else {
            failInitialRestore(
                "上次书库授权已失效；请重新选择书籍文件夹。"
            )
            return
        }
        guard reference.libraryID == library.stableLibraryID else {
            failInitialRestore(
                "当前书籍文件夹与上次书库不一致；请从当前书库重新打开。"
            )
            return
        }
        guard let book = library.books.first(where: {
            $0.id == reference.bookID && $0.libraryID == reference.libraryID
        }) else {
            failInitialRestore(
                "上次阅读的书已移动、删除或尚未进入本机索引；请刷新书库后重新打开。"
            )
            return
        }

        let restored = await reader.restoreLocalBook(book, library: library)
        guard !startupRouteOverrideRequested else { return }
        if restored {
            libraryStartupNotice = nil
            showsLibrary = false
        } else {
            failInitialRestore(
                "上次阅读的书无法打开，可能已移动或文件夹权限失效；请在书库中重新选择。"
            )
        }
    }

    @MainActor
    private func failInitialRestore(_ reason: String) {
        ReaderLastLocalBookStore.shared.clear()
        libraryStartupNotice = reason
        reader.finishInitialBookDecision()
        showsLibrary = true
    }

    @MainActor
    private func handleNativeFeatureRoute(_ route: ReaderNativeActivityRoute) {
        startupRouteOverrideRequested = true
        startupResolutionPending = false
        if let localBookID = route.localBookID,
           let localBook = ReaderLocalLibraryManager.shared.books.first(
            where: { $0.id == localBookID }
           ) {
            showsLibrary = false
            Task { @MainActor in
                _ = await reader.openLocalBook(
                    localBook,
                    library: ReaderLocalLibraryManager.shared
                )
            }
        } else {
            // Old Spotlight records without an opaque local book identity
            // return to the App-owned shelf. The main Reader never loads Pi.
            showsLibrary = true
        }
        switch route.action {
        case .openReader:
            break
        case .scanCurrentPage, .annotateCurrentPage, .openNativeTools:
            nativeToolsInitialAction = route.action
            showsNativeTools = true
        }
    }

    @MainActor
    private func consumePendingNativeFeatureRequest() {
        guard let request = ReaderNativeFeatureStore().consumePendingAction() else {
            return
        }
        handleNativeFeatureRoute(
            ReaderNativeActivityRoute(action: request.action)
        )
    }

    @MainActor
    private func refreshNativeFeatureSnapshot() async {
        do {
            let snapshot = try await reader.captureNativeReaderSnapshot()
            let changed = try ReaderNativeFeatureStore().writeSnapshot(snapshot)
            guard changed else { return }
            WidgetCenter.shared.reloadAllTimelines()
            try? await ReaderSpotlightIndex.indexCurrentSnapshot(snapshot)
        } catch {
            // A transient page load must never interfere with the Reader UI.
        }
    }
}

private struct NativeVoiceDiagnosticsView: View {
    @ObservedObject var bridge: NativeVoiceBridge
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section("当前状态") {
                    LabeledContent("App build", value: nativeAppBuildVersion)
                    LabeledContent("通话", value: bridge.state.title)
                    LabeledContent("WSS", value: bridge.socketState.rawValue)
                    LabeledContent("目标", value: bridge.activeTargetName)
                    LabeledContent("网络", value: bridge.networkSummary)
                    LabeledContent(
                        "麦克风",
                        value: bridge.microphoneMuted ? "已静音" : "工作中"
                    )
                    if let detail = bridge.state.detail {
                        Text(detail)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }

                Section("锁屏控制的等价操作") {
                    Button(
                        bridge.microphoneMuted ? "恢复麦克风" : "静音麦克风"
                    ) {
                        bridge.setMicrophoneMuted(!bridge.microphoneMuted)
                    }
                    .disabled(!bridge.state.isActive)

                    Button("结束电脑语音", role: .destructive) {
                        Task { @MainActor in
                            await bridge.stop()
                            dismiss()
                        }
                    }
                    .disabled(bridge.state.phase == .idle)
                }

                Section("最近协议与生命周期事件") {
                    if bridge.diagnostics.isEmpty {
                        Text("暂无记录")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(Array(bridge.diagnostics.reversed())) { entry in
                            VStack(alignment: .leading, spacing: 4) {
                                Text("[\(entry.category)] \(entry.message)")
                                    .font(.system(.caption, design: .monospaced))
                                    .textSelection(.enabled)
                                Text(entry.timestamp.formatted(
                                    date: .omitted,
                                    time: .standard
                                ))
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .navigationTitle("电脑语音诊断")
            .toolbar {
                ToolbarItemGroup(placement: .topBarLeading) {
                    Button("复制") {
                        UIPasteboard.general.string = bridge.diagnosticReport
                    }
                    Button("清空") {
                        bridge.clearDiagnostics()
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") {
                        dismiss()
                    }
                }
            }
        }
    }
}

private struct ReaderLoadError: View {
    let message: String
    let retry: () -> Void

    var body: some View {
        VStack(spacing: 14) {
            Text("阅读器加载失败")
                .font(.headline)
            Text(message)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Text("请确认 iPad 已连接 Tailscale。")
                .font(.caption)
                .foregroundStyle(.secondary)
            Button("重试", action: retry)
                .buttonStyle(.borderedProminent)
        }
        .padding(28)
        .frame(maxWidth: 360)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18))
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
    }
}

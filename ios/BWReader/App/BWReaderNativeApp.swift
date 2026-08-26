import CoreSpotlight
import SwiftUI
import UniformTypeIdentifiers
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
    @StateObject private var piOCR = ReaderPiOCRCoordinator.shared
    @ObservedObject var voiceBridge: NativeVoiceBridge
    @ObservedObject var nativeCommandReceiver: ReaderNativeCommandReceiver
    @State private var showsDiagnostics = false
    @State private var showsNativeTools = false
    @State private var showsLibrary = false
    // 设置面板「本机」tab 里的三个原生入口。它们各自弹**单一用途**的 UI ——
    // 而不是先打开那张 12 个 Section 的大表再让用户自己找。
    @State private var showsVaultPicker = false
    @State private var showsRealtimeKey = false
    @State private var showsPiLogin = false
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

            // 右上角两枚原生悬浮钮**都撤掉**（用户要求把它们并进我们自己的顶栏）：
            //   · 书库 → 顶栏「书籍」按钮，经 bwNativeAppPrefs.openLibrary 请求原生 sheet；
            //   · 原生工具 → 设置面板「本机」tab 里的「打开 App 原生设置」，
            //     走 bwNativeAppPrefs.openNativeTools。
            // 两条都不再依赖 URL 导航 —— 产品已本地化，书架是 SwiftUI，
            // 任何经过网络地址的路径都可能跑去打开不该存在的网页。
            // 它们还一直挡着侧栏顶部的按钮。
            EmptyView()
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
            if reader.handleAnkiMobileCallback(url) {
                return
            } else if let route = ReaderNativeActivityRoute.parse(url) {
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
        .onReceive(reader.$nativeToolsPresentationRequestID) { requestID in
            guard requestID != nil else { return }
            nativeToolsInitialAction = .openNativeTools
            showsNativeTools = true
        }
        .onReceive(reader.$vaultPickerPresentationRequestID) { requestID in
            guard requestID != nil else { return }
            showsVaultPicker = true
        }
        .onReceive(reader.$realtimeKeyPresentationRequestID) { requestID in
            guard requestID != nil else { return }
            showsRealtimeKey = true
        }
        .onReceive(reader.$piLoginPresentationRequestID) { requestID in
            guard requestID != nil else { return }
            showsPiLogin = true
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
            let cookies = await reader.remoteLibraryCookies()
            await piOCR.resumePendingImports(cookies: cookies)
            consumePendingNativeFeatureRequest()
            await refreshNativeFeatureSnapshot()
            reader.probeReaderSnapshotLink()
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
                    reader.probeReaderSnapshotLink()
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
                remote: remoteLibrary,
                piOCR: piOCR
            )
        }
        // 选文件夹**直接弹系统选择器**，不再套一层 sheet —— 用户 2026-08-18 要的
        // 「由 tab 触发原生 picker」就是这个形态。只有系统选择器产出的
        // security-scoped URL 能做 bookmarkData，网页拿不到也不该拿到。
        .fileImporter(
            isPresented: $showsVaultPicker,
            allowedContentTypes: [.folder]
        ) { result in
            switch result {
            case .success(let url):
                ReaderLocalNotesManager.shared.configureFolder(url)
            case .failure(let error):
                if (error as? CocoaError)?.code != .userCancelled {
                    ReaderLocalNotesManager.shared.reportError(error)
                }
            }
        }
        .sheet(isPresented: $showsRealtimeKey) {
            ReaderRealtimeKeyView()
        }
        .sheet(isPresented: $showsPiLogin) {
            ReaderPiLoginView(
                dataStore: reader.webView.configuration.websiteDataStore
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
        // 启动恢复此前抢在书库后台扫描之前：索引缓存里这本书的
        // byteCount/mtime 还是上一会话（真实插入页改写文件）之前的旧值，
        // 拿旧记录开书必然 BW_LOCAL_BOOK_CHANGED，每次重启都回到书库
        // （2026-08-25 横幅实锤）。先重扫（只是 stat 全部文件，很快），
        // 再查记录 —— 手动打开一直成功正是因为那时扫描早已完成。
        await library.rescan()
        for _ in 0..<100 where library.isScanning {
            try? await Task.sleep(nanoseconds: 100_000_000)
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
            // 打开失败但书就在索引里 —— **保留记忆**,下次启动再试;手动打开
            // 成功也会重写记忆。此前这里走 failInitialRestore(清记忆),后果是
            // 一次启动期抖动就让用户从此每次开 App 都要手动选书(2026-08-25
            // 实锤)。横幅带上真实原因,不再只给"可能已移动或权限失效"的猜测。
            let detail = reader.lastLocalBookOpenFailure
            libraryStartupNotice = detail.map {
                "上次阅读的书自动恢复失败：\($0)。可直接在书库中重新点开。"
            } ?? "上次阅读的书恢复超时（90 秒内未完成加载）；可直接在书库中重新点开。"
            reader.finishInitialBookDecision()
            showsLibrary = true
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
        // 深链没带书号时不再直接回书库（2026-08-26 用户：点小组件应该
        // 打开最后看的书，而不是让我重新选书）——先落回最近打开的书；
        // 只有连最近书都没有/找不到时才回书库。
        var targetBook: ReaderLocalBookRecord?
        if let localBookID = route.localBookID {
            targetBook = ReaderLocalLibraryManager.shared.books.first(
                where: { $0.id == localBookID })
        }
        if targetBook == nil,
           let last = ReaderLastLocalBookStore.shared.load() {
            targetBook = ReaderLocalLibraryManager.shared.books.first(
                where: { $0.id == last.bookID })
        }
        if let localBook = targetBook {
            showsLibrary = false
            Task { @MainActor in
                _ = await reader.openLocalBook(
                    localBook,
                    library: ReaderLocalLibraryManager.shared
                )
            }
        } else {
            // 连最近书都没有 —— 回 App 自有书架。The main Reader never
            // loads Pi.
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

import AuthenticationServices
import CryptoKit
import Security
import SwiftUI

enum AppStyle {
    static let canvas = Color(red: 0.965, green: 0.968, blue: 0.972)
    static let ink = Color(red: 0.13, green: 0.18, blue: 0.21)
    static let accent = Color(red: 0.13, green: 0.40, blue: 0.38)
    static let up = Color(red: 0.77, green: 0.23, blue: 0.27)
    static let down = Color(red: 0.12, green: 0.49, blue: 0.37)

    static func movement(_ value: Double?) -> Color {
        guard let value, value != 0 else { return .secondary }
        return value > 0 ? up : down
    }
    static func price(_ value: Double?) -> String { value.map { String(format: "%.2f", $0) } ?? "—" }
    static func change(_ value: Double?) -> String { value.map { String(format: "%+.2f%%", $0) } ?? "—" }
    static func percent(_ value: Double?) -> String { value.map { String(format: "%.2f%%", $0) } ?? "—" }
    static func compact(_ value: Double?) -> String {
        value?.formatted(.number.notation(.compactName).precision(.fractionLength(0...2))) ?? "—"
    }
}

@MainActor
struct StocksRootView: View {
    @ObservedObject var model: AppModel
    @StateObject private var selectionModel = StockSelectionModel()
    @StateObject private var monitoringModel = MonitoringModel()
    @Environment(\.horizontalSizeClass) private var sizeClass
    @Environment(\.scenePhase) private var scenePhase
    @State private var showingSettings = false
    @State private var showingCompactInspector = false
    @State private var showingWideInspector = false
    @State private var detailWidth: CGFloat = 0
    @State private var selectionSection: StockSelectionSection = .market
    @State private var selectionEditorPresented = false
    @State private var selectionOverlayPresented = false
    @State private var addingMarketCodes: [String]?
    @State private var selectionControlsHeight: CGFloat = 0
    @State private var monitoringDestination: MonitoringDestination?

    var body: some View {
        NavigationStack {
            GeometryReader { geometry in
                let showsInspector = sizeClass == .regular && geometry.size.width >= 900 && showingWideInspector
                HStack(spacing: 0) {
                    selectionResultsWorkspace
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    if showsInspector {
                        Divider()
                        StockAssistantInspector(
                            model: model,
                            onClose: { withAnimation(.easeInOut(duration: 0.2)) { showingWideInspector = false } }
                        )
                        .frame(width: min(max(geometry.size.width * 0.29, 310), 360))
                        .transition(.move(edge: .trailing).combined(with: .opacity))
                    }
                }
                .onAppear { detailWidth = geometry.size.width }
                .onChange(of: geometry.size.width) { _, width in detailWidth = width }
            }
            .background(AppStyle.canvas)
            .safeAreaInset(edge: .top, spacing: 0) {
                if model.isPaired, let notice = monitoringModel.banner {
                    MonitoringBanner(notice: notice, model: monitoringModel) {
                        monitoringDestination = .init(code: notice.stockCode, notificationId: notice.id)
                    }
                }
            }
            .navigationTitle("")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItemGroup(placement: .topBarLeading) {
                    Button { showingSettings = true } label: { Image(systemName: "slider.horizontal.3") }
                        .accessibilityLabel("设置与设备配对")
                    refreshStockListButton
                    if model.isPaired { monitoringButton }
                }
                ToolbarItem(placement: .principal) {
                    if model.isPaired { selectionNavigation }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    assistantToggle
                }
            }
        }
        .tint(AppStyle.accent)
        .sheet(isPresented: $showingSettings) { PairingView(model: model) }
        .sheet(item: $monitoringDestination) { destination in
            MonitoringView(model: monitoringModel, destination: destination, onOpenStock: { model.openStock($0) })
        }
        .sheet(isPresented: Binding(get: { addingMarketCodes != nil }, set: { if !$0 { addingMarketCodes = nil } })) {
            SelectionAddToGroupSheet(model: selectionModel, codes: addingMarketCodes ?? [])
        }
        .sheet(isPresented: $showingCompactInspector) {
            StockAssistantInspector(
                model: model,
                onClose: { showingCompactInspector = false }
            )
            .presentationDetents([.medium, .large])
            .presentationDragIndicator(.visible)
        }
        .task(id: inspectorContextID) {
            await model.updateInspectorContext(visible: contextInspectorIsVisible,
                                               mode: "assistant",
                                               presentation: showingCompactInspector ? "sheet" : "sidebar",
                                               settingsPresented: showingSettings)
        }
        .task(id: selectionScopeID) {
            monitoringDestination = nil
            await monitoringModel.connect(client: model.isPaired ? model.client : nil)
        }
        .task(id: "monitor:\(scenePhase):\(selectionScopeID)") {
            guard scenePhase == .active, model.isPaired else { return }
            await monitoringModel.connect(client: model.client)
            await monitoringModel.refresh()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(5)) } catch { return }
                await monitoringModel.refresh()
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .stocksOpenNotification)) { event in
            guard model.isPaired else { return }
            let code = event.userInfo?["code"] as? String
            let notificationID = event.userInfo?["notificationId"] as? String
            if let code { model.openStock(code) }
            monitoringDestination = .init(code: code, notificationId: notificationID)
            Task { await monitoringModel.refresh() }
        }
        .task(id: selectionScopeID) {
            selectionModel.onContextChange = { section, summary in
                Task { await model.updateSelectionContext(section: selectionSection.rawValue,
                                                          summary: section == selectionSection.rawValue || section == "selection_library" ? summary : nil,
                                                          editorPresented: selectionOverlayPresented || addingMarketCodes != nil) }
            }
            await selectionModel.connect(client: model.isPaired ? model.client : nil)
        }
        .onReceive(NotificationCenter.default.publisher(for: .stocksSelectionDidChange)) { _ in
            Task { await selectionModel.refreshFromExternalChange() }
        }
        .onChange(of: selectionSection) { _, section in
            Task { await model.updateSelectionContext(section: section.rawValue, summary: nil,
                                                      editorPresented: selectionOverlayPresented || addingMarketCodes != nil) }
        }
        .onChange(of: selectionOverlayPresented || addingMarketCodes != nil) { _, visible in
            Task { await model.updateSelectionContext(section: selectionSection.rawValue, summary: nil, editorPresented: visible) }
        }
        .task {
            await model.maintainCache()
            if model.isPaired {
                await model.loadOverview()
                await model.loadStocks()
            }
            else { showingSettings = true }
        }
        .task(id: model.query) {
            do { try await Task.sleep(nanoseconds: 300_000_000) } catch { return }
            await model.loadStocks()
        }
        .task(id: "\(model.detailPresented):\(model.selectedCode ?? "")") {
            guard model.detailPresented else { return }
            if let code = model.selectedCode {
                await model.voice.selectStock(code)
            }
            await model.loadDetail()
        }
        .task(id: "\(model.detailPresented):\(model.selectedCode ?? ""):\(model.chartPeriod.rawValue):\(model.klinePeriod.rawValue)") {
            guard model.detailPresented else { return }
            await model.loadChart()
        }
        .onChange(of: "\(model.chartPeriod.rawValue):\(model.klinePeriod.rawValue)") { _, _ in
            guard model.detailPresented else { return }
            Task {
                await model.publishVoiceContext(action: "切换图表：\(model.chartPeriod.title)，独立 K 线：\(model.klinePeriod.title)")
            }
        }
        .task(id: "\(scenePhase):\(model.detailPresented):\(model.selectedCode ?? "")") {
            guard scenePhase == .active, model.isPaired, model.detailPresented else { return }
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(10)) } catch { return }
                await model.refreshLiveData()
            }
        }
        .task(id: "\(scenePhase):\(selectionSection.rawValue):\(selectionModel.selectedGroupID ?? "")") {
            guard scenePhase == .active, model.isPaired, selectionSection == .watchlist else { return }
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(selectionModel.selectedGroup?.refreshInterval ?? 60)) } catch { return }
                await selectionModel.refreshVisibleGroup()
            }
        }
        .task(id: "quotes:\(scenePhase):\(selectionSection.rawValue):\(selectionModel.selectedGroupID ?? ""):\(selectionModel.selectedGroup?.realtimeEnabled ?? false)") {
            guard scenePhase == .active, model.isPaired, selectionSection == .watchlist else { return }
            while !Task.isCancelled {
                guard selectionModel.selectedGroup?.realtimeEnabled == true else { return }
                do { try await Task.sleep(for: .seconds(selectionModel.selectedGroup?.realtimeInterval ?? 5)) } catch { return }
                await selectionModel.refreshVisibleQuotes()
            }
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            StockNotificationCoordinator.shared.sceneChanged(active: phase == .active)
            if phase == .background, model.voice.isStarted, !StockNotificationCoordinator.shared.isCallActive {
                Task { await model.voice.stop() }
            }
        }
    }

    private var contextInspectorIsVisible: Bool {
        !showingSettings && (showingCompactInspector
            || (sizeClass == .regular && detailWidth >= 900 && showingWideInspector))
    }

    private var selectionScopeID: String {
        StockSelectionModel.scopeID(client: model.isPaired ? model.client : nil)
    }

    private var inspectorContextID: String {
        "\(contextInspectorIsVisible):\(showingCompactInspector):\(showingSettings)"
    }

    private var assistantToggle: some View {
        Button(action: toggleAssistant) {
            Label("AI", systemImage: contextInspectorIsVisible ? "bubble.left.and.bubble.right.fill" : "bubble.left.and.bubble.right")
        }
        .accessibilityLabel(contextInspectorIsVisible ? "关闭 AI 侧栏" : "打开 AI 侧栏")
    }

    private func toggleAssistant() {
        withAnimation(.easeInOut(duration: 0.2)) {
            if contextInspectorIsVisible {
                showingWideInspector = false
                showingCompactInspector = false
            } else if sizeClass == .regular && detailWidth >= 900 {
                showingCompactInspector = false
                showingWideInspector = true
            } else {
                showingWideInspector = false
                showingCompactInspector = true
            }
        }
    }

    private var selectionNavigation: some View {
        Picker("股票范围", selection: $selectionSection) {
            ForEach(StockSelectionSection.allCases) { section in Text(section.title).tag(section) }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .frame(width: sizeClass == .regular ? 300 : 200)
    }

    private var refreshStockListButton: some View {
        Button {
            Task {
                if selectionSection == .market { await model.loadStocks() }
                else { await selectionModel.refreshFromExternalChange() }
            }
        } label: { Image(systemName: "arrow.clockwise") }
        .disabled(!model.isPaired || model.isLoadingList)
        .accessibilityLabel("刷新股票列表")
    }

    private var monitoringButton: some View {
        Button {
            monitoringDestination = .init()
        } label: {
            ZStack(alignment: .topTrailing) {
                Image(systemName: monitoringModel.unreadCount > 0 ? "bell.badge" : "bell")
                if monitoringModel.unreadCount > 0 {
                    Text(monitoringModel.unreadCount > 99 ? "99+" : "\(monitoringModel.unreadCount)")
                        .font(.system(size: 9, weight: .bold)).foregroundStyle(.white)
                        .padding(.horizontal, 4).padding(.vertical, 2)
                        .background(AppStyle.up, in: Capsule()).offset(x: 9, y: -9)
                }
            }
        }
        .accessibilityLabel("盯盘与通知，\(monitoringModel.unreadCount) 条未读")
    }

    private func openMonitoring(_ code: String) {
        monitoringDestination = .init(code: code, tab: "rules")
    }

    private var selectionResultsWorkspace: some View {
        GeometryReader { geometry in
            ZStack(alignment: .topLeading) {
                // Usually this page fits, keeping all bubbles above the scrolling list.
                // Very long schemes can scroll the whole page instead of clipping conditions.
                ScrollView(.vertical) {
                    VStack(alignment: .leading, spacing: 0) {
                        if model.isPaired && selectionSection == .screener {
                            StockSelectionControls(model: selectionModel,
                                                   isScreenerActive: true,
                                                   editorPresented: selectionEditorPresented,
                                                   onActivate: {},
                                                   onEdit: { selectionEditorPresented = true })
                                .background {
                                    GeometryReader { controls in
                                        Color.clear.preference(key: ScreenerControlsHeight.self, value: controls.size.height)
                                    }
                                }
                            Divider()
                        }
                        stockList
                            .frame(width: model.detailPresented && geometry.size.width >= 820 ? 320 : geometry.size.width)
                            .frame(height: max(280, geometry.size.height - (model.isPaired && selectionSection == .screener ? selectionControlsHeight + 1 : 0)))
                    }
                    .frame(width: geometry.size.width, alignment: .leading)
                }
                .scrollBounceBehavior(.basedOnSize)
                .onPreferenceChange(ScreenerControlsHeight.self) { selectionControlsHeight = $0 }
                // Keep the mounted chart canvas and its local viewport when closing.
                // The panel uses the entire workspace, independently of bubble height.
                if model.selectedCode != nil {
                    StockDetailPanel(model: model, availableSize: geometry.size,
                                     onClose: { model.closeStockDetail() })
                        .opacity(model.detailPresented ? 1 : 0)
                        .allowsHitTesting(model.detailPresented)
                        .accessibilityHidden(!model.detailPresented)
                        .zIndex(1)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .clipped()
        }
    }

    private var stockList: some View {
        VStack(spacing: 0) {
            if model.isPaired && selectionSection != .market {
                StockSelectionSidebar(model: selectionModel, section: selectionSection,
                                      selectedStockCode: $model.selectedCode,
                                      editorPresented: $selectionEditorPresented,
                                      onOpenStock: { model.openStock($0) },
                                      onSelectStock: openStock,
                                      onOverlayChange: { selectionOverlayPresented = $0 },
                                      monitoring: monitoringModel, onMonitoring: openMonitoring)
            } else { marketStockList }
        }
    }

    private func openStock(_ code: String) {
        if model.detailPresented && model.selectedCode == code {
            model.closeStockDetail()
        } else {
            model.openStock(code)
        }
    }

    private var marketStockList: some View {
        VStack(spacing: 0) {
            if !model.isPaired {
                ContentUnavailableView {
                    Label("连接你的市场", systemImage: "chart.xyaxis.line")
                } description: {
                    Text("配对后读取 VPS 上的股票数据。")
                } actions: {
                    Button("配对此设备") { showingSettings = true }.buttonStyle(.borderedProminent)
                }
            } else {
                if let overview = model.overview { MarketPulseStrip(overview: overview) }
                if let error = model.listError {
                    Text(error).font(.caption).foregroundStyle(.red).padding(12).frame(maxWidth: .infinity, alignment: .leading)
                }
                List(model.stocks) { stock in
                    MonitoringStockRow(code: stock.code, name: stock.name, sector: stock.sector,
                                       price: stock.price, changePct: stock.changePct, volumeRatio: stock.volumeRatio,
                                       turnoverRate: stock.turnoverRate, turnover: stock.turnover, marketCap: stock.marketCap,
                                       summary: monitoringModel.library?.summary[stock.code],
                                       onOpen: { openStock(stock.code) }, onMonitoring: { openMonitoring(stock.code) })
                    .listRowBackground(model.detailPresented && model.selectedCode == stock.code ? AppStyle.accent.opacity(0.08) : Color.clear)
                    .listRowInsets(EdgeInsets(top: 4, leading: 12, bottom: 4, trailing: 12))
                    .contextMenu {
                        Button("打开股票") { model.openStock(stock.code) }
                        Button("盯盘规则", systemImage: "waveform.path.ecg") { openMonitoring(stock.code) }
                        Button("加入观察组", systemImage: "folder.badge.plus") { addingMarketCodes = [stock.code] }
                            .disabled(!selectionModel.canWrite)
                    }
                }
                .listStyle(.plain)
                .environment(\.defaultMinListRowHeight, 44)
                .overlay {
                    if model.isLoadingList && model.stocks.isEmpty { ProgressView("读取行情…") }
                    else if model.stocks.isEmpty && model.listError == nil {
                        ContentUnavailableView.search(text: model.query)
                    }
                }
                .refreshable { await model.loadStocks() }
                .searchable(text: $model.query, prompt: "代码或名称")
                HStack(spacing: 6) {
                    Text("数据时间").font(.caption2).foregroundStyle(.secondary)
                    Text(model.listAsOf ?? "服务器未提供时间").font(.caption2).monospacedDigit().lineLimit(1)
                }
                .padding(.horizontal, 12).padding(.vertical, 6).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

private struct ScreenerControlsHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

private struct MarketPulseStrip: View {
    let overview: MarketOverview
    var body: some View {
        SelectionBubbleFlow(spacing: 8) {
            pulse("涨", overview.rising, AppStyle.up)
            pulse("跌", overview.falling, AppStyle.down)
            pulse("平", overview.flat, .secondary)
            pulse("涨停", overview.limitUp, AppStyle.up)
            pulse("跌停", overview.limitDown, AppStyle.down)
            HStack(spacing: 3) {
                Text("成交").foregroundStyle(.secondary)
                Text(AppStyle.compact(overview.turnover)).monospacedDigit()
            }
            if let sector = overview.hotSectors.first {
                HStack(spacing: 3) {
                    Text("热 · \(sector.name)").lineLimit(1)
                    Text(AppStyle.change(sector.changePct)).foregroundStyle(AppStyle.movement(sector.changePct))
                }
            }
        }
        .font(.caption2)
        .padding(.horizontal, 12).padding(.vertical, 6)
        .background(.white)
    }

    private func pulse(_ title: String, _ value: Int, _ color: Color) -> some View {
        HStack(spacing: 3) {
            Text(title).foregroundStyle(.secondary)
            Text(String(value)).foregroundStyle(color).monospacedDigit().fontWeight(.semibold)
        }
        .font(.caption2)
    }
}

struct PairingView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var base = ""
    @State private var code = ""
    @State private var isPairing = false
    @State private var error: String?
    @State private var appleNonce: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Label(model.isPaired ? "此设备已配对" : "连接股票服务", systemImage: model.isPaired ? "checkmark.shield" : "link")
                        .foregroundStyle(AppStyle.accent)
                    Text("行情和语音通过你的 VPS 连接。设备凭证保存在本机钥匙串中。")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Section("服务连接") {
                    TextField("HTTPS 服务地址", text: $base)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    SignInWithAppleButton(.signIn) { request in
                        let nonce = randomNonce()
                        appleNonce = nonce
                        request.requestedScopes = [.fullName, .email]
                        request.nonce = SHA256.hash(data: Data(nonce.utf8)).map { String(format: "%02x", $0) }.joined()
                    } onCompletion: { result in
                        guard case .success(let authorization) = result,
                              let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
                              let tokenData = credential.identityToken,
                              let identityToken = String(data: tokenData, encoding: .utf8),
                              let nonce = appleNonce else {
                            if case .failure(let failure) = result { error = failure.localizedDescription }
                            return
                        }
                        isPairing = true
                        error = nil
                        Task {
                            do {
                                try await model.signInWithApple(base: base, identityToken: identityToken, rawNonce: nonce)
                                dismiss()
                            } catch { self.error = error.localizedDescription }
                            appleNonce = nil
                            isPairing = false
                        }
                    }
                    .signInWithAppleButtonStyle(.black)
                    .frame(height: 48)
                    .disabled(isPairing)
                    if let error { Text(error).foregroundStyle(.red).font(.footnote) }
                }
                Section {
                    DisclosureGroup("审核与开发连接") {
                        SecureField("配对码", text: $code)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        Button(model.isPaired ? "使用配对码重新连接" : "使用配对码连接") {
                            isPairing = true
                            error = nil
                            Task {
                                do {
                                    try await model.pair(base: base, code: code)
                                    code = ""
                                    dismiss()
                                } catch { self.error = error.localizedDescription }
                                isPairing = false
                            }
                        }
                        .disabled(isPairing || code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                } footer: {
                    Text("普通用户使用 Apple ID；配对码只用于审核受限账号和开发诊断。")
                }
                Section("设备") {
                    LabeledContent(
                        "版本",
                        value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.2.0"
                    )
                    Text(model.deviceID).font(.caption).monospaced().textSelection(.enabled)
                    if model.isPaired {
                        Button("移除此设备的凭证", role: .destructive) {
                            Task { await model.unpair() }
                        }
                        .disabled(isPairing)
                    }
                }
            }
            .navigationTitle("连接设置")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("完成") { dismiss() }.disabled(isPairing) } }
        }
        .tint(AppStyle.accent)
        .onAppear { base = model.baseURL }
        .interactiveDismissDisabled(isPairing)
    }

    private func randomNonce(length: Int = 32) -> String {
        let characters = Array("0123456789ABCDEFGHIJKLMNOPQRSTUVXYZabcdefghijklmnopqrstuvwxyz-._")
        var result = ""
        var remaining = length
        while remaining > 0 {
            var bytes = [UInt8](repeating: 0, count: 16)
            guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
                return UUID().uuidString.replacingOccurrences(of: "-", with: "")
            }
            for byte in bytes where remaining > 0 && Int(byte) < characters.count {
                result.append(characters[Int(byte)])
                remaining -= 1
            }
        }
        return result
    }
}

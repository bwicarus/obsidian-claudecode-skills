import Combine
import Foundation
import UIKit

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var baseURL: String
    @Published private(set) var isPaired: Bool
    @Published private(set) var isAIEnabled: Bool
    @Published var query = ""
    @Published var selectedCode: String? {
        didSet {
            if selectedCode != oldValue {
                voiceViewState.detailTab = "chart"
                latestChartSnapshot = nil
                chartSnapshotForKline = nil
                chipDistribution = nil
                chipRequestKey = nil
                chipGeneration = UUID()
                chipRefreshTask?.cancel()
            }
        }
    }
    @Published private(set) var stocks: [Stock] = []
    @Published private(set) var detail: StockResponse?
    @Published private(set) var liveStock: Stock?
    @Published private(set) var overview: MarketOverview?
    @Published private(set) var intraday: IntradayResponse?
    @Published private(set) var kline: KLineResponse?
    @Published private(set) var chipDistribution: ChipDistributionResponse?
    @Published private(set) var chartSnapshotForKline: VoiceChartContext?
    @Published var klinePeriod: ChartPeriod = .day {
        didSet {
            if klinePeriod != oldValue {
                if chartPeriod != .intraday && chartPeriod != klinePeriod { chartPeriod = klinePeriod }
                chartSnapshotForKline = nil
                if latestChartSnapshot?.chart.period != ChartPeriod.intraday.rawValue { latestChartSnapshot = nil }
                chipDistribution = nil
                chipRequestKey = nil
                chipGeneration = UUID()
                chipRefreshTask?.cancel()
            }
        }
    }
    @Published var chartPeriod: ChartPeriod = .intraday {
        didSet {
            if chartPeriod != oldValue {
                latestChartSnapshot = nil
                if chartPeriod != .intraday { klinePeriod = chartPeriod }
            }
        }
    }
    @Published private(set) var listAsOf: String?
    @Published private(set) var isLoadingList = false
    @Published private(set) var isLoadingDetail = false
    @Published private(set) var isLoadingChart = false
    @Published private(set) var listError: String?
    @Published private(set) var detailError: String?
    @Published private(set) var chartError: String?
    let deviceID: String
    let voice = VoiceSession()
    let annotations = AnnotationStore()
    let workspace = WorkspaceLayoutStore()
    private var listGeneration = UUID()
    private var detailGeneration = UUID()
    private var chartGeneration = UUID()
    private let cache = MarketCache.shared
    private var recentVoiceActions: [VoiceUIAction] = []
    private var voiceActionExpiryTask: Task<Void, Never>?
    private var voiceViewState = VoiceViewState()
    private var latestChartSnapshot: VoiceChartSnapshot?
    private var latestChartSourceID: String?
    private var chipGeneration = UUID()
    private var chipRequestKey: String?
    private var chipRefreshTask: Task<Void, Never>?

    init() {
        let initialBase = UserDefaults.standard.string(forKey: "stocksNative.baseURL") ?? "https://bwicarus.space/stocks-native"
        baseURL = initialBase
        let paired = Credentials.token(baseURL: initialBase) != nil
        isPaired = paired
        isAIEnabled = paired ? (UserDefaults.standard.object(forKey: "stocksNative.aiEnabled") as? Bool ?? true) : false
        let savedID = UserDefaults.standard.string(forKey: "stocksNative.deviceID") ?? UUID().uuidString
        deviceID = savedID
        UserDefaults.standard.set(savedID, forKey: "stocksNative.deviceID")
        voice.onStockSelected = { [weak self] code in self?.selectedCode = code }
        voice.onCapabilityAction = { [weak self] action in
            guard let self else { return CapabilityResult(success: false, message: "App 状态不可用。") }
            return self.annotations.perform(action, selectedStockCode: self.selectedCode)
        }
        annotations.onChange = { [weak self] code, operation in
            guard let self, code == self.selectedCode else { return }
            Task {
                guard code == self.selectedCode else { return }
                await self.publishVoiceContext(action: "图表标注：\(operation)", kind: "annotation")
            }
        }
    }

    var client: APIClient {
        // Every saved URL has passed normalizedBase; the bundled default is a fixed HTTPS URL.
        APIClient(baseURL: URL(string: baseURL)!, token: Credentials.token(baseURL: baseURL))
    }

    var displayedStock: Stock? {
        if liveStock?.code == selectedCode { return liveStock }
        return displayedDetail?.stock
    }

    var displayedDetail: StockResponse? {
        guard detail?.stock.code == selectedCode else { return nil }
        return detail
    }

    var displayedIntraday: IntradayResponse? {
        guard intraday?.code == selectedCode else { return nil }
        return intraday
    }

    var displayedKLine: KLineResponse? {
        guard let selectedCode, let kline,
              kline.code == selectedCode, kline.period == klinePeriod.rawValue else { return nil }
        return kline
    }

    var displayedCandles: [Candle] {
        displayedKlineCandles
    }

    var displayedKlineCandles: [Candle] {
        if let rows = displayedKLine?.rows, !rows.isEmpty { return rows }
        return klinePeriod == .day ? (displayedDetail?.candles ?? []) : []
    }

    var visibleWorkspaceKinds: Set<WorkspaceCardKind> {
        Set(workspace.layout.selectedPage?.visibleCards.map(\.kind) ?? [])
    }

    private func chartIsVisible(period: String) -> Bool {
        let kinds = visibleWorkspaceKinds
        if period == ChartPeriod.intraday.rawValue {
            return kinds.contains(.intraday) || (kinds.contains(.chart) && chartPeriod == .intraday)
        }
        return period == klinePeriod.rawValue && (kinds.contains(.kline) || kinds.contains(.klineChips)
            || (kinds.contains(.chart) && chartPeriod != .intraday))
    }

    func pair(base: String, code: String) async throws {
        let normalized = try APIClient.normalizedBase(base)
        let code = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !code.isEmpty else { throw AppError.message("请输入配对码。") }
        let pairClient = APIClient(baseURL: normalized, token: nil)
        let result = try await pairClient.pair(code: code, deviceID: deviceID, name: UIDevice.current.name)
        guard result.deviceId == deviceID, !result.token.isEmpty else { throw AppError.message("服务器返回的设备凭证不匹配。") }
        try Credentials.save(token: result.token, baseURL: normalized.absoluteString)
        isAIEnabled = result.aiEnabled ?? true
        UserDefaults.standard.set(isAIEnabled, forKey: "stocksNative.aiEnabled")
        await voice.stop()
        listGeneration = UUID()
        detailGeneration = UUID()
        chartGeneration = UUID()
        baseURL = normalized.absoluteString
        UserDefaults.standard.set(baseURL, forKey: "stocksNative.baseURL")
        isPaired = true
        stocks = []
        selectedCode = nil
        detail = nil
        liveStock = nil
        overview = nil
        intraday = nil
        kline = nil
        listAsOf = nil
        await loadStocks()
    }

    func signInWithApple(base: String, identityToken: String, rawNonce: String) async throws {
        let normalized = try APIClient.normalizedBase(base)
        let loginClient = APIClient(baseURL: normalized, token: nil)
        let result = try await loginClient.appleLogin(identityToken: identityToken, rawNonce: rawNonce,
                                                      deviceID: deviceID, name: UIDevice.current.name)
        guard result.deviceId == deviceID, !result.token.isEmpty else {
            throw AppError.message("服务器返回的设备凭证不匹配。")
        }
        try Credentials.save(token: result.token, baseURL: normalized.absoluteString)
        isAIEnabled = result.aiEnabled ?? true
        UserDefaults.standard.set(isAIEnabled, forKey: "stocksNative.aiEnabled")
        await voice.stop()
        listGeneration = UUID()
        detailGeneration = UUID()
        chartGeneration = UUID()
        baseURL = normalized.absoluteString
        UserDefaults.standard.set(baseURL, forKey: "stocksNative.baseURL")
        isPaired = true
        stocks = []
        selectedCode = nil
        detail = nil
        liveStock = nil
        await loadOverview()
        await loadStocks()
    }

    func unpair() async {
        await voice.stop()
        Credentials.delete(baseURL: baseURL)
        isPaired = false
        isAIEnabled = false
        UserDefaults.standard.removeObject(forKey: "stocksNative.aiEnabled")
        listGeneration = UUID()
        detailGeneration = UUID()
        chartGeneration = UUID()
        stocks = []
        selectedCode = nil
        detail = nil
        liveStock = nil
        overview = nil
        intraday = nil
        kline = nil
        listAsOf = nil
        listError = nil
        detailError = nil
        chartError = nil
        isLoadingList = false
        isLoadingDetail = false
        isLoadingChart = false
    }

    func loadStocks() async {
        guard isPaired else { return }
        let current = UUID()
        listGeneration = current
        isLoadingList = true
        listError = nil
        let cacheable = query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if cacheable, stocks.isEmpty,
           let cached = await cache.value(StocksResponse.self, for: "stocks", maxAge: 24 * 3600) {
            stocks = cached.items
            listAsOf = cached.asOf
            if selectedCode == nil { selectedCode = cached.items.first?.code }
        }
        do {
            let result = try await client.stocks(query: query)
            guard current == listGeneration, !Task.isCancelled else { return }
            stocks = result.items
            listAsOf = result.asOf
            if selectedCode == nil { selectedCode = result.items.first?.code }
            if cacheable { await cache.save(result, for: "stocks") }
            if !query.isEmpty { await publishVoiceContext(action: "搜索股票：\(query)") }
        } catch {
            guard current == listGeneration, !Task.isCancelled else { return }
            listError = error.localizedDescription
        }
        if current == listGeneration { isLoadingList = false }
    }

    func loadOverview() async {
        guard isPaired else { return }
        if overview == nil {
            overview = await cache.value(MarketOverview.self, for: "market-overview", maxAge: 24 * 3600)
        }
        do {
            let result = try await client.marketOverview()
            overview = result
            await cache.save(result, for: "market-overview")
            await publishVoiceContext()
        } catch {
            if overview == nil { listError = error.localizedDescription }
        }
    }

    func loadDetail() async {
        let current = UUID()
        detailGeneration = current
        guard isPaired, let code = selectedCode else {
            detail = nil
            liveStock = nil
            isLoadingDetail = false
            return
        }
        if detail?.stock.code != code {
            detail = nil
            liveStock = nil
            intraday = nil
            kline = nil
            if let cached = await cache.value(StockResponse.self, for: "detail-\(code)", maxAge: 7 * 24 * 3600) {
                detail = cached
                liveStock = cached.stock
            }
        }
        isLoadingDetail = true
        detailError = nil
        do {
            let result = try await client.stock(code: code)
            guard current == detailGeneration, selectedCode == code, !Task.isCancelled else { return }
            detail = result
            liveStock = result.stock
            await cache.save(result, for: "detail-\(code)")
            await publishVoiceContext()
        } catch {
            guard current == detailGeneration, !Task.isCancelled else { return }
            detailError = error.localizedDescription
        }
        if current == detailGeneration { isLoadingDetail = false }
    }

    func loadChart() async {
        let current = UUID()
        chartGeneration = current
        guard isPaired, let code = selectedCode else {
            intraday = nil
            kline = nil
            isLoadingChart = false
            return
        }
        let period = klinePeriod
        isLoadingChart = true
        chartError = nil
        let minuteKey = "chart-\(code)-intraday"
        let candleKey = "chart-\(code)-\(period.rawValue)"
        if intraday?.code != code {
            let cached = await cache.value(IntradayResponse.self, for: minuteKey, maxAge: 12 * 3600)
            guard current == chartGeneration, selectedCode == code, !Task.isCancelled else { return }
            intraday = cached
        }
        if kline?.code != code || kline?.period != period.rawValue {
            let cached = await cache.value(KLineResponse.self, for: candleKey, maxAge: 7 * 24 * 3600)
            guard current == chartGeneration, selectedCode == code, !Task.isCancelled else { return }
            kline = cached
        }
        // Fetch both series so independent intraday and K-line cards can coexist.
        async let minuteResult = client.intraday(code: code)
        async let candleResult = client.kline(code: code, period: period)
        do {
            let result = try await minuteResult
            guard current == chartGeneration, selectedCode == code, !Task.isCancelled else { return }
            intraday = result
            await cache.save(result, for: minuteKey)
        } catch {
            if current == chartGeneration { chartError = error.localizedDescription }
        }
        do {
            let result = try await candleResult
            guard current == chartGeneration, selectedCode == code,
                  klinePeriod == period, !Task.isCancelled else { return }
            kline = result
            await cache.save(result, for: candleKey)
        } catch {
            if current == chartGeneration { chartError = error.localizedDescription }
        }
        guard current == chartGeneration, !Task.isCancelled else { return }
        isLoadingChart = false
        scheduleChipRefresh()
        await publishVoiceContext()
    }

    func loadChips(start: String? = nil, end: String? = nil) async {
        guard isPaired, let code = selectedCode,
              !visibleWorkspaceKinds.isDisjoint(with: [.chipDistribution, .klineChips]) else { return }
        let key = "chips-\(code)-\(start ?? "recent")-\(end ?? "latest")"
        if chipRequestKey == key, chipDistribution?.code == code { return }
        let current = UUID()
        chipGeneration = current
        chipRequestKey = key
        let sameRange = chipDistribution?.code == code && chipDistribution?.start == start && chipDistribution?.end == end
        if !sameRange { chipDistribution = nil }
        if chipDistribution == nil, let cached = await cache.value(ChipDistributionResponse.self, for: key, maxAge: 24 * 3600) {
            guard current == chipGeneration, selectedCode == code, !Task.isCancelled else { return }
            chipDistribution = cached
        }
        do {
            let result = try await client.chips(code: code, start: start, end: end)
            guard current == chipGeneration, selectedCode == code, !Task.isCancelled else { return }
            chipDistribution = result
            await cache.save(result, for: key)
        } catch {
            guard current == chipGeneration, selectedCode == code, !Task.isCancelled else { return }
            chipRequestKey = nil
            if chipDistribution == nil {
                chipDistribution = ChipDistributionResponse(code: code, start: start, end: end, rows: [],
                    currentPrice: nil, averageCost: nil, winnerRate: nil, cost5: nil, cost95: nil,
                    concentration: nil, source: nil, warning: "筹码数据暂不可用，请稍后重试。")
            }
        }
    }

    private func scheduleChipRefresh() {
        chipRefreshTask?.cancel()
        guard !visibleWorkspaceKinds.isDisjoint(with: [.chipDistribution, .klineChips]) else { return }
        let snapshot = chartSnapshotForKline
        let code = selectedCode
        chipRefreshTask = Task { @MainActor [weak self] in
            do { try await Task.sleep(for: .milliseconds(180)) } catch { return }
            guard let self, self.selectedCode == code else { return }
            var start = snapshot?.firstVisibleTime.map { String($0.prefix(10)) }
            var end = snapshot?.lastVisibleTime.map { String($0.prefix(10)) }
            if self.klinePeriod.rawValue.hasPrefix("m"), self.klinePeriod != .month {
                let days = Array((self.displayedDetail?.candles ?? []).suffix(5))
                start = days.first?.time
                end = days.last?.time
            }
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
            formatter.dateFormat = "yyyy-MM-dd"
            if let last = end.flatMap(formatter.date(from:)), let first = start.flatMap(formatter.date(from:)),
               last.timeIntervalSince(first) > 549 * 86400 {
                start = formatter.string(from: last.addingTimeInterval(-549 * 86400))
            }
            await self.loadChips(start: start, end: end)
        }
    }

    func loadRealtime() async {
        guard isPaired, let code = selectedCode else { return }
        do {
            let result = try await client.realtime(codes: [code])
            guard selectedCode == code, !Task.isCancelled else { return }
            if let stock = result.items.first {
                liveStock = stock
                await publishVoiceContext()
            }
        } catch {
            // Keep the last honest snapshot visible; chart refresh surfaces connection failures.
        }
    }

    func refreshLiveData() async {
        await loadRealtime()
        let kinds = visibleWorkspaceKinds
        guard isPaired, let code = selectedCode else { return }
        if kinds.contains(.intraday) || (kinds.contains(.chart) && chartPeriod == .intraday) {
            do {
                let result = try await client.intraday(code: code)
                guard selectedCode == code, !Task.isCancelled else { return }
                intraday = result
                await cache.save(result, for: "chart-\(code)-intraday")
                await publishVoiceContext()
            } catch { chartError = error.localizedDescription }
        }
        if !kinds.isDisjoint(with: [.chipDistribution, .klineChips]) {
            chipRequestKey = nil
            scheduleChipRefresh()
        }
    }

    func maintainCache() async { await cache.clean() }

    func selectWorkspacePage(_ id: String) {
        do {
            try workspace.selectPage(id)
            latestChartSnapshot = nil
            chartSnapshotForKline = nil
            workspaceDidChange()
        } catch { detailError = "布局未保存：\(error.localizedDescription)" }
    }

    func workspaceDidChange() {
        if let snapshot = latestChartSnapshot, !chartIsVisible(period: snapshot.chart.period) {
            latestChartSnapshot = nil
        }
        if !chartIsVisible(period: klinePeriod.rawValue) { chartSnapshotForKline = nil }
        if visibleWorkspaceKinds.isDisjoint(with: [.chipDistribution, .klineChips]) {
            chipRefreshTask?.cancel()
            chipGeneration = UUID()
            chipRequestKey = nil
        } else { scheduleChipRefresh() }
        Task { await publishVoiceContext(action: "工作台：\(workspace.layout.selectedPage?.title ?? "")", kind: "panel") }
    }

    func updateInspectorContext(visible: Bool, mode: String, presentation: String, settingsPresented: Bool) async {
        var next = voiceViewState
        next.inspectorVisible = visible
        next.inspectorMode = visible ? mode : nil
        next.inspectorPresentation = visible ? presentation : "hidden"
        next.settingsPresented = settingsPresented
        guard next != voiceViewState else { return }
        let inspectorChanged = next.inspectorVisible != voiceViewState.inspectorVisible
            || next.inspectorMode != voiceViewState.inspectorMode
        voiceViewState = next
        let title = ["orderBook": "盘口", "analysis": "分析", "assistant": "AI"][mode] ?? mode
        let action = inspectorChanged ? (visible ? "打开侧栏：\(title)" : "关闭侧栏") : nil
        await publishVoiceContext(action: action, kind: "panel")
    }

    func updateChartContext(_ snapshot: VoiceChartSnapshot, sourceID: String = "primary") async {
        guard snapshot.chart.stockCode == selectedCode, chartIsVisible(period: snapshot.chart.period) else { return }
        if snapshot.chart.period == klinePeriod.rawValue {
            let previous = chartSnapshotForKline
            chartSnapshotForKline = snapshot.chart
            if previous?.firstVisibleTime != snapshot.chart.firstVisibleTime
                || previous?.lastVisibleTime != snapshot.chart.lastVisibleTime { scheduleChipRefresh() }
        }
        let previous = latestChartSnapshot
        // Synchronized companion charts report the same viewport. Their passive
        // annotation state must not replace the card the user is editing.
        if let previous, sourceID != latestChartSourceID,
           previous.chart == snapshot.chart, !snapshot.annotations.editing { return }
        guard snapshot != previous else {
            if snapshot.annotations.editing { latestChartSourceID = sourceID }
            return
        }
        // A periodic refresh of another card must not steal an explicit cursor selection.
        if let previous, previous.chart.period != snapshot.chart.period,
           previous.chart.selectionSource == "cursor", snapshot.chart.selectionSource == "latest" { return }
        latestChartSnapshot = snapshot
        latestChartSourceID = sourceID
        if snapshot.chart.selectionSource == "cursor",
           (snapshot.chart.selectedPoint?.time != previous?.chart.selectedPoint?.time
            || previous?.chart.selectionSource != "cursor"),
           let point = snapshot.chart.selectedPoint {
            await publishVoiceContext(action: "查看图表：\(point.time)", kind: "chart_selection")
        } else { await publishVoiceContext() }
    }

    func publishVoiceContext(action: String? = nil, kind: String? = nil) async {
        let kinds = visibleWorkspaceKinds
        let snapshot = !voiceViewState.settingsPresented
            && latestChartSnapshot?.chart.stockCode == selectedCode
            && latestChartSnapshot.map({ chartIsVisible(period: $0.chart.period) }) == true ? latestChartSnapshot : nil
        let contextPeriod = snapshot.flatMap { ChartPeriod(rawValue: $0.chart.period) } ?? chartPeriod
        let now = Date()
        let formatter = ISO8601DateFormatter()
        recentVoiceActions.removeAll {
            guard let occurred = formatter.date(from: $0.occurredAtUtc) else { return true }
            return now.timeIntervalSince(occurred) >= 30 || $0.stockCode != selectedCode
                || (isChartAction($0.kind) && $0.chartPeriod != contextPeriod.rawValue)
        }
        if let action, !action.isEmpty {
            let next = VoiceUIAction(id: UUID().uuidString,
                                     kind: kind ?? voiceActionKind(action),
                                     label: String(action.prefix(120)),
                                     occurredAtUtc: Date().ISO8601Format(),
                                     stockCode: selectedCode,
                                     chartPeriod: contextPeriod.rawValue)
            if let last = recentVoiceActions.last,
               last.kind == next.kind, last.label == next.label,
               last.stockCode == next.stockCode, last.chartPeriod == next.chartPeriod {
                recentVoiceActions[recentVoiceActions.count - 1] = next
            } else {
                recentVoiceActions.append(next)
            }
            recentVoiceActions = Array(recentVoiceActions.suffix(3))
        }
        scheduleVoiceActionExpiry(now: now, formatter: formatter)
        let stock = displayedStock
        let activeDetail = displayedDetail
        let activeIntraday = displayedIntraday
        var metrics: [String: String] = [:]
        if let value = stock?.price { metrics["price"] = String(format: "%.3f", value) }
        if let value = stock?.changePct { metrics["changePct"] = String(format: "%+.3f%%", value) }
        if let value = stock?.open { metrics["open"] = String(format: "%.3f", value) }
        if let value = stock?.high { metrics["high"] = String(format: "%.3f", value) }
        if let value = stock?.low { metrics["low"] = String(format: "%.3f", value) }
        if let value = stock?.turnover { metrics["turnover"] = String(format: "%.0f", value) }
        if let value = stock?.turnoverRate { metrics["turnoverRate"] = String(format: "%.3f%%", value) }
        if kinds.contains(.macd) {
            let points = NativeChartIndicators.series(candles: displayedKlineCandles, panel: activeDetail?.technical)
            let visible = NativeChartIndicators.visible(points, context: chartSnapshotForKline)
            if let value = NativeChartIndicators.inspected(visible, context: chartSnapshotForKline)?.histogram {
                metrics["macdHist"] = String(format: "%.4f", value)
            }
        }
        if kinds.contains(.fund), let value = activeDetail?.fund?.metrics.latestMainInflow { metrics["mainInflow"] = String(format: "%.0f", value) }
        var panels: [String] = []
        if voiceViewState.settingsPresented {
            panels = ["连接设置"]
        } else {
            if activeDetail != nil {
                panels = ["价格摘要"] + (workspace.layout.selectedPage?.visibleCards.map { $0.kind.title } ?? [])
            }
            if voiceViewState.inspectorVisible, let mode = voiceViewState.inspectorMode {
                panels.append(["orderBook": "盘口", "analysis": "分析摘要", "assistant": "AI 对话"][mode] ?? mode)
            }
        }
        let latestTime = contextPeriod == .intraday
            ? (snapshot?.chart.selectionSource == "latest" ? snapshot?.chart.lastVisibleTime : nil)
            : displayedKlineCandles.last?.time
        let annotationContext = snapshot?.annotations
        var contextViewState = voiceViewState
        contextViewState.detailTab = "chart"
        contextViewState.workspacePageID = workspace.layout.selectedPage?.id
        contextViewState.workspacePageTitle = workspace.layout.selectedPage?.title
        contextViewState.visibleCardIDs = voiceViewState.settingsPresented ? [] : (workspace.layout.selectedPage?.visibleCards.map { $0.kind.rawValue } ?? [])
        contextViewState.chartViewport = snapshot.map {
            VoiceChartViewport(firstVisibleTime: $0.chart.firstVisibleTime,
                               lastVisibleTime: $0.chart.lastVisibleTime,
                               visiblePointCount: $0.chart.visiblePointCount,
                               historicalSummary: $0.chart.selectionSource == "visible_end" ? $0.chart.selectedPoint : nil)
        }
        let orderBook = kinds.contains(.orderBook) && activeDetail != nil
            && !voiceViewState.settingsPresented && stock != nil
            ? VoiceOrderBookContext(bids: Array((stock?.bids ?? []).prefix(5)), asks: Array((stock?.asks ?? []).prefix(5))) : nil
        let quoteDate = stock?.quoteTime.map { String($0.prefix(10)) }
            ?? (activeIntraday?.tradeDate.isEmpty == false ? activeIntraday?.tradeDate : activeDetail?.asOf)
        let context = VoiceUIContext(screen: voiceViewState.settingsPresented ? "settings" : (selectedCode == nil ? "market_overview" : "stock_detail"),
                                     selectedCode: selectedCode, selectedName: stock?.name,
                                     quoteAsOf: quoteDate, quoteTime: stock?.quoteTime, quoteSource: stock?.quoteSource,
                                     observedAtUtc: Date().ISO8601Format(),
                                     chartPeriod: contextPeriod.title, latestPointTime: latestTime,
                                     metrics: metrics, visiblePanels: panels,
                                     recentActions: recentVoiceActions,
                                     chartPeriodID: contextPeriod.rawValue,
                                     viewState: contextViewState, chart: snapshot?.chart,
                                     annotations: annotationContext, orderBook: orderBook)
        await voice.updateContext(context)
    }

    private func scheduleVoiceActionExpiry(now: Date, formatter: ISO8601DateFormatter) {
        voiceActionExpiryTask?.cancel()
        voiceActionExpiryTask = nil
        guard let first = recentVoiceActions.first,
              let occurred = formatter.date(from: first.occurredAtUtc) else { return }
        let remaining = max(0.1, 30 - now.timeIntervalSince(occurred))
        voiceActionExpiryTask = Task { @MainActor [weak self] in
            do { try await Task.sleep(for: .seconds(remaining)) } catch { return }
            guard let self else { return }
            await self.publishVoiceContext()
        }
    }

    private func isChartAction(_ kind: String) -> Bool {
        ["chart_period", "chart_selection", "chart_range", "annotation"].contains(kind)
    }

    private func voiceActionKind(_ action: String) -> String {
        if action.hasPrefix("搜索股票") { return "search" }
        if action.hasPrefix("图表标注") { return "annotation" }
        if action.contains("周期") || action.contains("K线") || action.hasPrefix("切换图表") { return "chart_period" }
        if action.contains("面板") || action.contains("侧栏") { return "panel" }
        return "interaction"
    }
}

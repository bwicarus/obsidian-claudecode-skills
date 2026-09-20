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
                invalidateDetailRequests()
                detailPresented = selectedCode != nil
                voiceViewState.detailTab = "chart"
                latestChartSnapshot = nil
                chartSnapshotForKline = nil
                timelineWindow = nil
                isTimelineEditing = false
                if timelinePrecisionOverride == nil { chartPeriod = .intraday; klinePeriod = .m5 }
                chipDistribution = nil
                chipRequestKey = nil
                chipGeneration = UUID()
                chipRefreshTask?.cancel()
                publishDetailPresentationChange()
                if let code = selectedCode {
                    Task { [weak self] in await self?.refreshPlans(code: code) }
                }
            }
        }
    }
    @Published private(set) var detailPresented = false
    @Published private(set) var stocks: [Stock] = []
    @Published private(set) var detail: StockResponse?
    @Published private(set) var liveStock: Stock?
    @Published private(set) var overview: MarketOverview?
    @Published private(set) var intraday: IntradayResponse?
    @Published private(set) var kline: KLineResponse?
    @Published private(set) var chipDistribution: ChipDistributionResponse?
    @Published private(set) var chartSnapshotForKline: VoiceChartContext?
    @Published private(set) var timelineWindow: StockTimelineWindow?
    @Published private(set) var timelinePrecisionOverride: ChartPeriod?
    @Published private(set) var isTimelineEditing = false
    private var timelineCommitTask: Task<Void, Never>?
    @Published var klinePeriod: ChartPeriod = .m5 {
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
    @Published private(set) var savedPlans: [StockPlan] = []
    @Published private(set) var isLoadingPlans = false
    @Published private(set) var planError: String?
    @Published private(set) var archivingPlanIDs: Set<String> = []
    @Published private(set) var planScopeID = "signed-out"
    private var plansAccountScopeID: String?
    private var plansGeneration = UUID()
    private var planLoads: [String: UUID] = [:]
    private var pendingPlanArchives: [String: StockPlanArchiveRequest] = [:]
    let deviceID: String
    let voice = VoiceSession()
    let research = StockResearchStore()
    let annotations = AnnotationStore()
    let workspace = WorkspaceLayoutStore()
    private var listGeneration = UUID()
    private var detailGeneration = UUID()
    private var detailSessionGeneration = UUID()
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
        voice.onStockSelected = { [weak self] code in self?.openStock(code) }
        voice.onPlansChanged = { [weak self] id, code in
            guard let self else { return }
            let scope = self.planScopeID
            Task { [weak self] in
                guard let self, self.planScopeID == scope else { return }
                await self.refreshChangedPlan(id: id, code: code)
            }
        }
        voice.onReportsChanged = { [weak self] id, code in
            guard let self else { return }
            Task { [weak self] in await self?.research.changed(id: id, code: code) }
        }
        voice.onCapabilityAction = { [weak self] action in
            guard let self else { return CapabilityResult(success: false, message: "App 状态不可用。") }
            return self.annotations.perform(action, selectedStockCode: self.selectedCode)
        }
        annotations.onChange = { [weak self] code, operation in
            guard let self, self.detailPresented, code == self.selectedCode else { return }
            Task {
                guard self.detailPresented, code == self.selectedCode else { return }
                await self.publishVoiceContext(action: "图表标注：\(operation)", kind: "annotation")
            }
        }
        resetPlanState()
        Task { [weak self] in await self?.refreshPlans() }
    }

    var client: APIClient {
        // Every saved URL has passed normalizedBase; the bundled default is a fixed HTTPS URL.
        APIClient(baseURL: URL(string: baseURL)!, token: Credentials.token(baseURL: baseURL))
    }

    var currentAccountPlans: [StockPlan] {
        plansAccountScopeID == planScopeID ? savedPlans : []
    }

    var selectedStockPlans: [StockPlan] {
        currentAccountPlans.filter { $0.code == selectedCode }
    }

    func plans(for transcript: Transcript) -> [StockPlan] {
        guard transcript.role == "assistant" else { return [] }
        return currentAccountPlans.filter { plan in
            if let thread = plan.source.threadId, let currentThread = voice.threadID, thread != currentThread { return false }
            if let message = plan.source.messageId { return message == transcript.id }
            guard let turn = plan.source.turnId, turn == transcript.turnID else { return false }
            return voice.transcripts.last(where: { $0.role == "assistant" && $0.turnID == turn })?.id == transcript.id
        }
    }

    var unattachedSidebarPlans: [StockPlan] {
        guard let selectedCode else { return [] }
        let attached = Set(voice.transcripts.flatMap { plans(for: $0).map(\.id) })
        return Array(currentAccountPlans.filter { plan in
            !attached.contains(plan.id) && plan.status != "archived" &&
                plan.code == selectedCode
        }.prefix(5))
    }

    private func resetPlanState() {
        plansGeneration = UUID()
        planScopeID = StockSelectionModel.scopeID(client: isPaired ? client : nil)
        plansAccountScopeID = planScopeID
        research.configure(client: isPaired && isAIEnabled ? client : nil, scope: planScopeID)
        savedPlans = []
        planLoads = [:]
        pendingPlanArchives = [:]
        archivingPlanIDs = []
        isLoadingPlans = false
        planError = nil
    }

    private func mergePlans(_ incoming: [StockPlan]) {
        var byID = Dictionary(uniqueKeysWithValues: savedPlans.map { ($0.id, $0) })
        for plan in incoming {
            if let existing = byID[plan.id] {
                guard plan.revision >= existing.revision else { continue }
                // A source binding can arrive before an older list response.
                if plan.revision == existing.revision, existing.source.turnId != nil, plan.source.turnId == nil { continue }
            }
            byID[plan.id] = plan
        }
        savedPlans = byID.values.sorted {
            $0.createdAt == $1.createdAt ? $0.id > $1.id : $0.createdAt > $1.createdAt
        }
        research.mergePlans(incoming)
    }

    func refreshPlans(code: String? = nil) async {
        research.configure(client: isPaired && isAIEnabled ? client : nil, scope: planScopeID)
        if plansAccountScopeID != planScopeID { resetPlanState() }
        guard isPaired, isAIEnabled else { return }
        let scope = planScopeID, generation = plansGeneration, api = client
        let key = code ?? "*", ticket = UUID()
        planLoads[key] = ticket
        isLoadingPlans = true
        defer {
            if scope == planScopeID, generation == plansGeneration, planLoads[key] == ticket {
                planLoads.removeValue(forKey: key)
                isLoadingPlans = !planLoads.isEmpty
            }
        }
        do {
            let result = try await api.plans(code: code, includeArchived: true)
            guard scope == planScopeID, generation == plansGeneration, planLoads[key] == ticket, !Task.isCancelled else { return }
            mergePlans(result.items)
            planError = nil
        } catch {
            guard scope == planScopeID, generation == plansGeneration, planLoads[key] == ticket, !Task.isCancelled else { return }
            planError = error.localizedDescription
        }
    }

    private func refreshChangedPlan(id: String?, code: String?) async {
        if plansAccountScopeID != planScopeID { resetPlanState() }
        guard isPaired, isAIEnabled else { return }
        if let id {
            let scope = planScopeID, generation = plansGeneration, api = client
            do {
                let response = try await api.plan(id: id)
                guard scope == planScopeID, generation == plansGeneration else { return }
                mergePlans([response.plan])
                planError = nil
                await research.refresh(code: code ?? response.plan.code)
                return
            } catch {
                guard scope == planScopeID, generation == plansGeneration else { return }
                planError = error.localizedDescription
            }
        }
        await refreshPlans(code: code)
    }

    func archivePlan(_ plan: StockPlan) async {
        guard isPaired, isAIEnabled, plansAccountScopeID == planScopeID,
              plan.status != "archived", !archivingPlanIDs.contains(plan.id) else { return }
        let scope = planScopeID, generation = plansGeneration, api = client
        archivingPlanIDs.insert(plan.id)
        defer {
            if scope == planScopeID, generation == plansGeneration { archivingPlanIDs.remove(plan.id) }
        }
        do {
            if pendingPlanArchives[plan.id] == nil {
                let latest = try await api.plan(id: plan.id)
                guard scope == planScopeID, generation == plansGeneration else { return }
                mergePlans([latest.plan])
                if latest.plan.status == "archived" { planError = nil; return }
                pendingPlanArchives[plan.id] = .init(id: plan.id, requestId: UUID().uuidString, expectedRevision: latest.revision)
            }
            guard let pending = pendingPlanArchives[plan.id] else { return }
            let receipt = try await api.archivePlan(id: pending.id, requestID: pending.requestId, expectedRevision: pending.expectedRevision)
            guard scope == planScopeID, generation == plansGeneration else { return }
            guard receipt.success, receipt.requestId == pending.requestId, receipt.planId == plan.id else {
                planError = "服务器尚未确认归档，请重试确认。"
                return
            }
            pendingPlanArchives.removeValue(forKey: plan.id)
            mergePlans([receipt.plan])
            planError = nil
            await research.refresh(code: plan.code)
        } catch let failure as StockPlanAPIError {
            guard scope == planScopeID, generation == plansGeneration else { return }
            if (400..<500).contains(failure.status) { pendingPlanArchives.removeValue(forKey: plan.id) }
            planError = failure.message
        } catch {
            guard scope == planScopeID, generation == plansGeneration else { return }
            planError = "归档结果尚未确认，可重试：\(error.localizedDescription)"
        }
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

    private var timelineLatestDay: String {
        if let day = displayedIntraday?.tradeDate, !day.isEmpty { return day }
        return displayedDetail?.candles.last?.time ?? StockTimelineTime.day(Date())
    }

    var requestedTimelineWindow: StockTimelineWindow? {
        timelineWindow ?? StockTimelineWindow(first: "\(timelineLatestDay)T09:30", last: "\(timelineLatestDay)T15:00")
    }

    var timelineTimes: [String] {
        if chartPeriod == .intraday {
            guard let data = displayedIntraday, !data.rows.isEmpty else { return [] }
            return (0..<242).map { "\(data.tradeDate)T\(StockTimelineTime.sessionTime($0))" }
        }
        return displayedKlineCandles.map(\.time)
    }

    var timelineValues: [Double?] {
        if chartPeriod == .intraday {
            guard let data = displayedIntraday, !data.rows.isEmpty else { return [] }
            var values = [Double?](repeating: nil, count: 242)
            for point in data.rows {
                if let slot = StockTimelineTime.sessionSlot(point.time) { values[slot] = point.price }
            }
            return values
        }
        return displayedKlineCandles.map { Optional($0.close) }
    }

    var timelineIndexRange: Range<Int> {
        requestedTimelineWindow?.indices(in: timelineTimes) ?? 0..<0
    }

    var linkedKlineRange: Range<Int> {
        requestedTimelineWindow?.indices(in: displayedKlineCandles.map(\.time)) ?? 0..<0
    }

    var linkedIntradayRange: Range<Int> {
        guard let day = displayedIntraday?.tradeDate else { return 0..<0 }
        return requestedTimelineWindow?.indices(in: (0..<242).map { StockTimelineTime.sessionTime($0) }, tradeDate: day) ?? 0..<0
    }

    var linkedKlineContext: VoiceChartContext? {
        guard let code = selectedCode else { return nil }
        let candles = displayedKlineCandles
        let range = linkedKlineRange
        let cursor: VoiceChartPoint? = chartSnapshotForKline.flatMap { context in
            context.selectionSource == "cursor" && range.contains(where: { candles[$0].time == context.selectedPoint?.time })
                ? context.selectedPoint : nil
        }
        let last = range.last.map { candles[$0] }
        let point = cursor ?? last.map { VoiceChartPoint(time: $0.time, open: $0.open, high: $0.high,
                                                        low: $0.low, close: $0.close, volume: $0.volume) }
        return VoiceChartContext(stockCode: code, period: klinePeriod.rawValue, kind: "candles",
                                 firstVisibleTime: range.first.map { candles[$0].time }, lastVisibleTime: last?.time,
                                 visiblePointCount: range.count, selectedPoint: point,
                                 selectionSource: cursor != nil ? "cursor" : (range.upperBound == candles.count ? "latest" : "visible_end"))
    }

    var timelineRangeLabel: String {
        guard let window = requestedTimelineWindow else { return "等待行情" }
        let showTime = window.calendarDays <= 1.01
        return StockTimelineTime.label(window.lower, includesTime: showTime) + " – "
            + StockTimelineTime.label(window.upper, includesTime: showTime)
    }

    var timelineCoverageNote: String? {
        guard let window = requestedTimelineWindow else { return nil }
        if chartPeriod == .intraday {
            guard let data = displayedIntraday, let last = data.rows.last else { return "分时仅提供当前交易日，正在读取可用数据。" }
            if linkedIntradayRange.isEmpty { return "分时仅提供 \(StockChartLabels.day(data.tradeDate))，所选历史区间没有分时数据。" }
            if window.calendarDays > 1.01 { return "分时仅提供 \(StockChartLabels.day(data.tradeDate))，其余所选日期没有分时数据。" }
            return last.time < "15:00" ? "分时截至 \(StockChartLabels.detail(last.time, tradeDate: data.tradeDate))" : nil
        }
        guard let first = displayedKlineCandles.first?.time, let last = displayedKlineCandles.last?.time,
              let firstKey = StockTimelineTime.key(first), let lastKey = StockTimelineTime.key(last, endOfDay: true) else {
            return isLoadingChart ? "正在读取所选精度的行情…" : "所选精度暂无可用行情。"
        }
        let tolerance: TimeInterval = klinePeriod == .month ? 32 * 86400 : (klinePeriod == .week ? 8 * 86400 : 4 * 86400)
        let missingStart = (StockTimelineTime.date(firstKey)?.timeIntervalSince(StockTimelineTime.date(window.lower) ?? .distantFuture) ?? 0) > tolerance
        if linkedKlineRange.isEmpty || missingStart || window.upper < firstKey || window.lower > lastKey {
            return "当前已载入 \(StockChartLabels.detail(first)) – \(StockChartLabels.detail(last))；所选范围未完全覆盖。"
        }
        return nil
    }

    func previewTimelineRange(_ range: Range<Int>) {
        // Navigator layout normalization must never overwrite requested dates.
        guard isTimelineEditing, let first = range.first, let last = range.last,
              timelineTimes.indices.contains(first), timelineTimes.indices.contains(last) else { return }
        timelineWindow = StockTimelineWindow(first: timelineTimes[first], last: timelineTimes[last])
    }

    func timelineEditingChanged(_ editing: Bool) {
        if editing { timelineCommitTask?.cancel(); isTimelineEditing = true }
        else if isTimelineEditing { isTimelineEditing = false; commitTimelineChange() }
    }

    func selectTimelinePrecision(_ period: ChartPeriod?) {
        timelinePrecisionOverride = period
        commitTimelineChange()
    }

    func selectTimelineSpan(days: Int) {
        guard let key = StockTimelineTime.key(timelineLatestDay), let end = StockTimelineTime.date(key) else { return }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 8 * 3600)!
        let start: String
        if days == 1 { start = timelineLatestDay }
        else if days == 5, let first = displayedDetail?.candles.suffix(5).first?.time { start = first }
        else {
            let date = calendar.date(byAdding: .day, value: -(days == 5 ? 7 : days - 1), to: end) ?? end
            start = StockTimelineTime.day(date)
        }
        timelineWindow = days == 1
            ? StockTimelineWindow(first: "\(timelineLatestDay)T09:30", last: "\(timelineLatestDay)T15:00")
            : StockTimelineWindow(first: start, last: timelineLatestDay)
        commitTimelineChange()
    }

    func moveTimelineToLatest() {
        guard let window = requestedTimelineWindow,
              let oldEnd = StockTimelineTime.date(window.upper), let oldStart = StockTimelineTime.date(window.lower),
              let key = StockTimelineTime.key(window.calendarDays <= 1.01 ? (displayedIntraday?.rows.last?.time ?? "15:00") : timelineLatestDay,
                                               tradeDate: timelineLatestDay, endOfDay: true),
              let end = StockTimelineTime.date(key) else { return }
        let start = end.addingTimeInterval(-oldEnd.timeIntervalSince(oldStart))
        timelineWindow = StockTimelineWindow(first: StockTimelineTime.timestamp(start), last: StockTimelineTime.timestamp(end))
        commitTimelineChange()
    }

    private func commitTimelineChange() {
        let automaticPeriod = requestedTimelineWindow.flatMap { ChartPeriod(rawValue: $0.automaticPeriodID) } ?? .intraday
        var next = timelinePrecisionOverride ?? automaticPeriod
        if timelinePrecisionOverride == nil, next == .intraday,
           let window = requestedTimelineWindow, let currentDay = StockTimelineTime.key(timelineLatestDay),
           window.lower.prefix(8) != currentDay.prefix(8) { next = .m5 }
        if next == .intraday { chartPeriod = .intraday; klinePeriod = .m5 }
        else { chartPeriod = next }
        chartSnapshotForKline = nil
        latestChartSnapshot = nil
        timelineCommitTask?.cancel()
        let code = selectedCode
        timelineCommitTask = Task { @MainActor [weak self] in
            // Let chart views report the final viewport and annotation scope first.
            await Task.yield()
            guard let self, self.detailPresented, self.selectedCode == code, !Task.isCancelled else { return }
            self.scheduleChipRefresh()
            await self.publishVoiceContext(action: "调整时间范围：\(self.timelineRangeLabel)，\(self.chartPeriod.title)", kind: "chart_range")
        }
    }

    var visibleWorkspaceKinds: Set<WorkspaceCardKind> {
        guard detailPresented else { return [] }
        return Set(workspace.layout.selectedPage?.visibleCards.map(\.kind) ?? [])
    }

    func openStock(_ code: String) {
        if selectedCode != code {
            selectedCode = code
        } else if !detailPresented {
            invalidateDetailRequests()
            detailPresented = true
            publishDetailPresentationChange()
        }
    }

    func closeStockDetail() {
        guard detailPresented else { return }
        invalidateDetailRequests()
        detailPresented = false
        publishDetailPresentationChange()
    }

    private func invalidateDetailRequests() {
        timelineCommitTask?.cancel()
        isTimelineEditing = false
        detailSessionGeneration = UUID()
        detailGeneration = UUID()
        chartGeneration = UUID()
        chipGeneration = UUID()
        chipRequestKey = nil
        chipRefreshTask?.cancel()
        chipRefreshTask = nil
        isLoadingDetail = false
        isLoadingChart = false
        detailError = nil
        chartError = nil
    }

    private func publishDetailPresentationChange() {
        let generation = detailSessionGeneration
        let presented = detailPresented
        let code = selectedCode
        Task { @MainActor [weak self] in
            guard let self, self.detailSessionGeneration == generation,
                  self.detailPresented == presented, self.selectedCode == code else { return }
            await self.publishVoiceContext(action: presented ? "打开股票详情：\(code ?? "")" : "关闭股票详情", kind: "panel")
        }
    }

    private func resetAccountDetailState() {
        resetPlanState()
        invalidateDetailRequests()
        detailPresented = false
        selectedCode = nil
        detail = nil
        liveStock = nil
        intraday = nil
        kline = nil
        chipDistribution = nil
        latestChartSnapshot = nil
        latestChartSourceID = nil
        chartSnapshotForKline = nil
        recentVoiceActions = []
        voiceActionExpiryTask?.cancel()
        voiceActionExpiryTask = nil
    }

    private func chartIsVisible(period: String) -> Bool {
        let kinds = visibleWorkspaceKinds
        if period == ChartPeriod.intraday.rawValue {
            return kinds.contains(.intraday) || (kinds.contains(.chart) && chartPeriod == .intraday)
        }
        return period == klinePeriod.rawValue && (kinds.contains(.kline) || kinds.contains(.klineChips) || kinds.contains(.macd) || kinds.contains(.kdj)
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
        resetPlanState()
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
        resetAccountDetailState()
        overview = nil
        listAsOf = nil
        await loadStocks()
        await refreshPlans()
    }

    func signInWithApple(base: String, identityToken: String, rawNonce: String) async throws {
        let normalized = try APIClient.normalizedBase(base)
        let loginClient = APIClient(baseURL: normalized, token: Credentials.token(baseURL: normalized.absoluteString))
        let result = try await loginClient.appleLogin(identityToken: identityToken, rawNonce: rawNonce,
                                                      deviceID: deviceID, name: UIDevice.current.name)
        guard result.deviceId == deviceID, !result.token.isEmpty else {
            throw AppError.message("服务器返回的设备凭证不匹配。")
        }
        try Credentials.save(token: result.token, baseURL: normalized.absoluteString)
        resetPlanState()
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
        resetAccountDetailState()
        overview = nil
        listAsOf = nil
        await loadOverview()
        await loadStocks()
        await refreshPlans()
        if let warning = result.libraryMigrationWarning { listError = warning }
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
        resetAccountDetailState()
        overview = nil
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
        }
        do {
            let result = try await client.stocks(query: query)
            guard current == listGeneration, !Task.isCancelled else { return }
            stocks = result.items
            listAsOf = result.asOf
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
        defer { if current == detailGeneration { isLoadingDetail = false } }
        guard detailPresented else {
            isLoadingDetail = false
            return
        }
        guard isPaired, let code = selectedCode else {
            detail = nil
            liveStock = nil
            isLoadingDetail = false
            return
        }
        if detail?.stock.code != code {
            detail = nil
            liveStock = nil
            if intraday?.code != code { intraday = nil }
            if kline?.code != code { kline = nil }
            if let cached = await cache.value(StockResponse.self, for: "detail-\(code)", maxAge: 7 * 24 * 3600) {
                guard detailPresented, current == detailGeneration, selectedCode == code, !Task.isCancelled else { return }
                detail = cached
                liveStock = cached.stock
            }
        }
        guard detailPresented, current == detailGeneration, selectedCode == code, !Task.isCancelled else { return }
        isLoadingDetail = true
        detailError = nil
        do {
            let result = try await client.stock(code: code)
            guard detailPresented, current == detailGeneration, selectedCode == code, !Task.isCancelled else { return }
            detail = result
            liveStock = result.stock
            await cache.save(result, for: "detail-\(code)")
            await publishVoiceContext()
        } catch {
            guard detailPresented, current == detailGeneration, selectedCode == code, !Task.isCancelled else { return }
            detailError = error.localizedDescription
        }
    }

    func loadChart() async {
        let current = UUID()
        chartGeneration = current
        defer { if current == chartGeneration { isLoadingChart = false } }
        guard detailPresented else {
            isLoadingChart = false
            return
        }
        guard isPaired, let code = selectedCode else {
            intraday = nil
            kline = nil
            isLoadingChart = false
            return
        }
        let period = klinePeriod
        let isPrecisionChange = kline?.code == code && kline?.period != period.rawValue
        isLoadingChart = true
        chartError = nil
        let minuteKey = "chart-\(code)-intraday"
        let candleKey = "chart-\(code)-\(period.rawValue)"
        if intraday?.code != code {
            let cached = await cache.value(IntradayResponse.self, for: minuteKey, maxAge: 12 * 3600)
            guard detailPresented, current == chartGeneration, selectedCode == code, !Task.isCancelled else { return }
            intraday = cached
        }
        if kline?.code != code || kline?.period != period.rawValue {
            let cached = await cache.value(KLineResponse.self, for: candleKey, maxAge: 7 * 24 * 3600)
            guard detailPresented, current == chartGeneration, selectedCode == code, !Task.isCancelled else { return }
            kline = cached
        }
        guard detailPresented, current == chartGeneration, selectedCode == code,
              klinePeriod == period, !Task.isCancelled else { return }
        // Fetch both series so independent intraday and K-line cards can coexist.
        async let minuteResult = loadChartIntraday(code: code, reusingCurrent: isPrecisionChange)
        async let candleResult = client.kline(code: code, period: period, count: 320)
        do {
            let result = try await minuteResult
            guard detailPresented, current == chartGeneration, selectedCode == code, !Task.isCancelled else { return }
            intraday = result
            await cache.save(result, for: minuteKey)
        } catch {
            if detailPresented, current == chartGeneration, !Task.isCancelled { chartError = error.localizedDescription }
        }
        do {
            let result = try await candleResult
            guard detailPresented, current == chartGeneration, selectedCode == code,
                  klinePeriod == period, !Task.isCancelled else { return }
            kline = result
            await cache.save(result, for: candleKey)
        } catch {
            if detailPresented, current == chartGeneration, !Task.isCancelled { chartError = error.localizedDescription }
        }
        guard detailPresented, current == chartGeneration, selectedCode == code, !Task.isCancelled else { return }
        isLoadingChart = false
        scheduleChipRefresh()
        await publishVoiceContext()
    }

    private func loadChartIntraday(code: String, reusingCurrent: Bool) async throws -> IntradayResponse {
        if reusingCurrent, let current = displayedIntraday { return current }
        return try await client.intraday(code: code)
    }

    func loadChips(start: String? = nil, end: String? = nil) async {
        guard detailPresented, isPaired, let code = selectedCode,
              !visibleWorkspaceKinds.isDisjoint(with: [.chipDistribution, .klineChips]) else { return }
        let key = "chips-\(code)-\(start ?? "recent")-\(end ?? "latest")"
        if chipRequestKey == key, chipDistribution?.code == code { return }
        let current = UUID()
        chipGeneration = current
        chipRequestKey = key
        let sameRange = chipDistribution?.code == code && chipDistribution?.start == start && chipDistribution?.end == end
        if !sameRange { chipDistribution = nil }
        if chipDistribution == nil, let cached = await cache.value(ChipDistributionResponse.self, for: key, maxAge: 24 * 3600) {
            guard detailPresented, current == chipGeneration, selectedCode == code, !Task.isCancelled else { return }
            chipDistribution = cached
        }
        guard detailPresented, current == chipGeneration, selectedCode == code, !Task.isCancelled else { return }
        do {
            let result = try await client.chips(code: code, start: start, end: end)
            guard detailPresented, current == chipGeneration, selectedCode == code, !Task.isCancelled else { return }
            chipDistribution = result
            await cache.save(result, for: key)
        } catch {
            guard detailPresented, current == chipGeneration, selectedCode == code, !Task.isCancelled else { return }
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
        guard !isTimelineEditing else { return }
        guard !visibleWorkspaceKinds.isDisjoint(with: [.chipDistribution, .klineChips]) else { return }
        let snapshot = linkedKlineContext
        let code = selectedCode
        chipRefreshTask = Task { @MainActor [weak self] in
            do { try await Task.sleep(for: .milliseconds(180)) } catch { return }
            guard let self, self.detailPresented, self.selectedCode == code else { return }
            var start = snapshot?.firstVisibleTime.map { String($0.prefix(10)) }
            var end = snapshot?.lastVisibleTime.map { String($0.prefix(10)) }
            if let window = self.requestedTimelineWindow {
                start = StockTimelineTime.date(window.lower).map(StockTimelineTime.day)
                end = StockTimelineTime.date(window.upper).map(StockTimelineTime.day)
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
        guard detailPresented, isPaired, let code = selectedCode else { return }
        let session = detailSessionGeneration
        do {
            let result = try await client.realtime(codes: [code])
            guard detailPresented, session == detailSessionGeneration,
                  selectedCode == code, !Task.isCancelled else { return }
            if let stock = result.items.first {
                liveStock = stock
                await publishVoiceContext()
            }
        } catch {
            // Keep the last honest snapshot visible; chart refresh surfaces connection failures.
        }
    }

    func refreshLiveData() async {
        guard detailPresented, isPaired, let code = selectedCode else { return }
        let session = detailSessionGeneration
        await loadRealtime()
        guard detailPresented, session == detailSessionGeneration,
              selectedCode == code, !Task.isCancelled else { return }
        let kinds = visibleWorkspaceKinds
        if kinds.contains(.intraday) || (kinds.contains(.chart) && chartPeriod == .intraday) {
            do {
                let result = try await client.intraday(code: code)
                guard detailPresented, session == detailSessionGeneration,
                      selectedCode == code, !Task.isCancelled else { return }
                intraday = result
                await cache.save(result, for: "chart-\(code)-intraday")
                guard detailPresented, session == detailSessionGeneration,
                      selectedCode == code, !Task.isCancelled else { return }
                await publishVoiceContext()
            } catch {
                guard detailPresented, session == detailSessionGeneration,
                      selectedCode == code, !Task.isCancelled else { return }
                chartError = error.localizedDescription
            }
        }
        guard detailPresented, session == detailSessionGeneration,
              selectedCode == code, !Task.isCancelled else { return }
        if !kinds.isDisjoint(with: [.chipDistribution, .klineChips]) {
            chipRequestKey = nil
            scheduleChipRefresh()
        }
    }

    func maintainCache() async { await cache.clean() }

    func selectWorkspacePage(_ id: String) {
        guard workspace.layout.selectedPage?.id != id,
              workspace.layout.pages.contains(where: { $0.id == id }) else { return }
        do {
            try workspace.selectPage(id)
            latestChartSnapshot = nil
            chartSnapshotForKline = nil
            workspaceDidChange()
        } catch { detailError = "布局未保存：\(error.localizedDescription)" }
    }

    func workspaceDidChange() {
        guard detailPresented else { return }
        if let snapshot = latestChartSnapshot, !chartIsVisible(period: snapshot.chart.period) {
            latestChartSnapshot = nil
        }
        if !chartIsVisible(period: klinePeriod.rawValue) { chartSnapshotForKline = nil }
        if visibleWorkspaceKinds.isDisjoint(with: [.chipDistribution, .klineChips]) {
            chipRefreshTask?.cancel()
            chipGeneration = UUID()
            chipRequestKey = nil
        } else { scheduleChipRefresh() }
        let session = detailSessionGeneration
        Task { @MainActor [weak self] in
            guard let self, self.detailPresented, self.detailSessionGeneration == session else { return }
            await self.publishVoiceContext(action: "工作台：\(self.workspace.layout.selectedPage?.title ?? "")", kind: "panel")
        }
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

    func updateSelectionContext(section: String, summary: String?, editorPresented: Bool) async {
        var next = voiceViewState
        next.navigationSection = section
        next.selectionSummary = summary.map { String($0.prefix(240)) }
        next.selectionEditorPresented = editorPresented
        guard next != voiceViewState else { return }
        voiceViewState = next
        await publishVoiceContext(action: summary, kind: "selection_workspace")
    }

    func updateChartContext(_ snapshot: VoiceChartSnapshot, sourceID: String = "primary") async {
        guard !isTimelineEditing else { return }
        guard snapshot.chart.stockCode == selectedCode, chartIsVisible(period: snapshot.chart.period) else { return }
        if snapshot.chart.period == klinePeriod.rawValue {
            let previous = chartSnapshotForKline
            chartSnapshotForKline = snapshot.chart
            if previous?.firstVisibleTime != snapshot.chart.firstVisibleTime
                || previous?.lastVisibleTime != snapshot.chart.lastVisibleTime { scheduleChipRefresh() }
        }
        // A passive intraday companion must not replace the selected historical
        // timeline while the main candles / technical cards are visible.
        if snapshot.chart.period != chartPeriod.rawValue, chartIsVisible(period: chartPeriod.rawValue) { return }
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
        guard !isTimelineEditing else { return }
        let kinds = visibleWorkspaceKinds
        let detailObscured = !detailPresented || voiceViewState.settingsPresented || voiceViewState.selectionEditorPresented
        let snapshot = !detailObscured
            && latestChartSnapshot?.chart.stockCode == selectedCode
            && latestChartSnapshot.map({ chartIsVisible(period: $0.chart.period) }) == true ? latestChartSnapshot : nil
        let contextPeriod = snapshot.flatMap { ChartPeriod(rawValue: $0.chart.period) } ?? chartPeriod
        let now = Date()
        let formatter = ISO8601DateFormatter()
        recentVoiceActions.removeAll {
            guard let occurred = formatter.date(from: $0.occurredAtUtc) else { return true }
            return now.timeIntervalSince(occurred) >= 30 || $0.stockCode != selectedCode
                || (isChartAction($0.kind) && (detailObscured || $0.chartPeriod != contextPeriod.rawValue))
        }
        let actionKind = kind ?? action.map(voiceActionKind)
        if let action, !action.isEmpty, let actionKind,
           !detailObscured || !isChartAction(actionKind) {
            let next = VoiceUIAction(id: UUID().uuidString,
                                     kind: actionKind,
                                     label: String(action.prefix(120)),
                                     occurredAtUtc: Date().ISO8601Format(),
                                     stockCode: selectedCode,
                                     chartPeriod: detailObscured ? nil : contextPeriod.rawValue)
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
        let stock = detailObscured ? nil : displayedStock
        let activeDetail = detailObscured ? nil : displayedDetail
        let activeIntraday = detailObscured ? nil : displayedIntraday
        var metrics: [String: String] = [:]
        if let value = stock?.price { metrics["price"] = String(format: "%.3f", value) }
        if let value = stock?.changePct { metrics["changePct"] = String(format: "%+.3f%%", value) }
        if let value = stock?.open { metrics["open"] = String(format: "%.3f", value) }
        if let value = stock?.high { metrics["high"] = String(format: "%.3f", value) }
        if let value = stock?.low { metrics["low"] = String(format: "%.3f", value) }
        if let value = stock?.turnover { metrics["turnover"] = String(format: "%.0f", value) }
        if let value = stock?.turnoverRate { metrics["turnoverRate"] = String(format: "%.3f%%", value) }
        if !detailObscured, kinds.contains(.macd) {
            let points = NativeChartIndicators.series(candles: displayedKlineCandles,
                panel: klinePeriod == .day ? activeDetail?.technical : nil)
            let visible = NativeChartIndicators.visible(points, context: linkedKlineContext)
            if let value = NativeChartIndicators.inspected(visible, context: linkedKlineContext)?.histogram {
                metrics["macdHist"] = String(format: "%.4f", value)
            }
        }
        if kinds.contains(.fund), let value = activeDetail?.fund?.metrics.latestMainInflow { metrics["mainInflow"] = String(format: "%.0f", value) }
        var panels: [String] = []
        if voiceViewState.settingsPresented {
            panels = ["连接设置"]
        } else if voiceViewState.selectionEditorPresented {
            panels = ["选股与观察池编辑"]
        } else {
            if activeDetail != nil {
                panels = ["价格摘要"] + (workspace.layout.selectedPage?.visibleCards.map { $0.kind.title } ?? [])
            }
            if voiceViewState.inspectorVisible, let mode = voiceViewState.inspectorMode,
               mode == "assistant" || !detailObscured {
                panels.append(["orderBook": "盘口", "analysis": "分析摘要", "assistant": "AI 对话"][mode] ?? mode)
            }
            if let section = voiceViewState.navigationSection {
                panels.append(["market": "市场股票列表", "watchlist": "观察池", "screener": "选股器",
                               "selection_library": "筛选方案库"][section] ?? section)
            }
        }
        let latestTime = detailObscured ? nil : (contextPeriod == .intraday
            ? (snapshot?.chart.selectionSource == "latest" ? snapshot?.chart.lastVisibleTime : nil)
            : displayedKlineCandles.last?.time)
        let annotationContext = snapshot?.annotations
        var contextViewState = voiceViewState
        contextViewState.detailPresented = detailPresented
        contextViewState.detailTab = detailObscured ? "none" : "chart"
        contextViewState.visibilityScope = detailPresented ? "active_detail_panel" : "selection_workspace"
        contextViewState.workspacePageID = detailObscured ? nil : workspace.layout.selectedPage?.id
        contextViewState.inkScopeID = detailObscured ? nil : workspaceInkContext?.scopeID
        contextViewState.workspacePageTitle = detailObscured ? nil : workspace.layout.selectedPage?.title
        contextViewState.visibleCardIDs = detailObscured ? [] : (workspace.layout.selectedPage?.visibleCards.map { $0.kind.rawValue } ?? [])
        contextViewState.chartViewport = snapshot.map {
            VoiceChartViewport(firstVisibleTime: $0.chart.firstVisibleTime,
                               lastVisibleTime: $0.chart.lastVisibleTime,
                               visiblePointCount: $0.chart.visiblePointCount,
                               historicalSummary: $0.chart.selectionSource == "visible_end" ? $0.chart.selectedPoint : nil)
        }
        let orderBook = kinds.contains(.orderBook) && activeDetail != nil
            && !detailObscured && stock != nil
            ? VoiceOrderBookContext(bids: Array((stock?.bids ?? []).prefix(5)), asks: Array((stock?.asks ?? []).prefix(5))) : nil
        let quoteDate = stock?.quoteTime.map { String($0.prefix(10)) }
            ?? (activeIntraday?.tradeDate.isEmpty == false ? activeIntraday?.tradeDate : activeDetail?.asOf)
        let workspaceScreen = ["market": "market_overview", "watchlist": "watchlist",
                               "screener": "screener", "selection_library": "selection_library"][voiceViewState.navigationSection ?? "market"] ?? "market_overview"
        let context = VoiceUIContext(screen: voiceViewState.settingsPresented ? "settings" :
                                     (voiceViewState.selectionEditorPresented ? "selection_editor" :
                                      (detailPresented ? "stock_detail" : workspaceScreen)),
                                     selectedCode: selectedCode, selectedName: displayedStock?.name,
                                     quoteAsOf: quoteDate, quoteTime: stock?.quoteTime, quoteSource: stock?.quoteSource,
                                     observedAtUtc: Date().ISO8601Format(),
                                     chartPeriod: detailObscured ? "" : contextPeriod.title, latestPointTime: latestTime,
                                     metrics: metrics, visiblePanels: panels,
                                     recentActions: recentVoiceActions,
                                     chartPeriodID: detailObscured ? nil : contextPeriod.rawValue,
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

import Combine
import Foundation
import UIKit

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var baseURL: String
    @Published private(set) var isPaired: Bool
    @Published private(set) var isAIEnabled: Bool
    @Published var query = ""
    @Published var selectedCode: String?
    @Published private(set) var stocks: [Stock] = []
    @Published private(set) var detail: StockResponse?
    @Published private(set) var liveStock: Stock?
    @Published private(set) var overview: MarketOverview?
    @Published private(set) var intraday: IntradayResponse?
    @Published private(set) var kline: KLineResponse?
    @Published var chartPeriod: ChartPeriod = .intraday
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
    private var listGeneration = UUID()
    private var detailGeneration = UUID()
    private var chartGeneration = UUID()
    private let cache = MarketCache.shared
    private var recentVoiceActions: [VoiceUIAction] = []

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
            let result = self.annotations.perform(action, selectedStockCode: self.selectedCode)
            Task { await self.publishVoiceContext(action: "图表标注：\(action.operation)") }
            return result
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
        guard chartPeriod == .intraday, intraday?.code == selectedCode else { return nil }
        return intraday
    }

    var displayedKLine: KLineResponse? {
        guard let selectedCode, let kline,
              kline.code == selectedCode, kline.period == chartPeriod.rawValue else { return nil }
        return kline
    }

    var displayedCandles: [Candle] {
        if let rows = displayedKLine?.rows, !rows.isEmpty { return rows }
        return chartPeriod == .day ? (displayedDetail?.candles ?? []) : []
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
        let period = chartPeriod
        isLoadingChart = true
        chartError = nil
        let cacheKey = "chart-\(code)-\(period.rawValue)"
        do {
            if period == .intraday {
                if intraday?.code != code {
                    intraday = await cache.value(IntradayResponse.self, for: cacheKey, maxAge: 12 * 3600)
                }
                let result = try await client.intraday(code: code)
                guard current == chartGeneration, selectedCode == code,
                      chartPeriod == period, !Task.isCancelled else { return }
                intraday = result
                await cache.save(result, for: cacheKey)
                await publishVoiceContext()
            } else {
                if kline?.code != code || kline?.period != period.rawValue {
                    kline = await cache.value(KLineResponse.self, for: cacheKey, maxAge: 7 * 24 * 3600)
                }
                let result = try await client.kline(code: code, period: period)
                guard current == chartGeneration, selectedCode == code,
                      chartPeriod == period, !Task.isCancelled else { return }
                kline = result
                await cache.save(result, for: cacheKey)
                await publishVoiceContext()
            }
        } catch {
            guard current == chartGeneration, !Task.isCancelled else { return }
            chartError = error.localizedDescription
        }
        if current == chartGeneration { isLoadingChart = false }
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
        if chartPeriod == .intraday { await loadChart() }
    }

    func maintainCache() async {
        await cache.clean()
    }

    func publishVoiceContext(action: String? = nil) async {
        if let action, !action.isEmpty {
            let next = VoiceUIAction(id: UUID().uuidString,
                                     kind: voiceActionKind(action),
                                     label: String(action.prefix(120)),
                                     occurredAtUtc: Date().ISO8601Format(),
                                     stockCode: selectedCode,
                                     chartPeriod: chartPeriod.rawValue)
            if let last = recentVoiceActions.last,
               last.kind == next.kind, last.label == next.label,
               last.stockCode == next.stockCode, last.chartPeriod == next.chartPeriod {
                recentVoiceActions[recentVoiceActions.count - 1] = next
            } else {
                recentVoiceActions.append(next)
            }
            recentVoiceActions = Array(recentVoiceActions.suffix(3))
        }
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
        if let value = activeDetail?.technical?.metrics.macdHist { metrics["macdHist"] = String(format: "%.4f", value) }
        if let value = activeDetail?.fund?.metrics.latestMainInflow { metrics["mainInflow"] = String(format: "%.0f", value) }
        var panels = [chartPeriod.title, "行情指标"]
        if activeDetail?.technical != nil { panels.append("技术指标") }
        if activeDetail?.fund != nil { panels.append("资金动向") }
        if activeDetail?.chips != nil { panels.append("筹码分布") }
        if !(activeDetail?.peers ?? []).isEmpty { panels.append("同业对比") }
        if !(activeDetail?.announcements ?? []).isEmpty { panels.append("公司公告") }
        let latestTime = chartPeriod == .intraday ? activeIntraday?.rows.last?.time : displayedCandles.last?.time
        let scopedActions = recentVoiceActions.filter { $0.stockCode == selectedCode }
        let context = VoiceUIContext(screen: selectedCode == nil ? "market_overview" : "stock_detail",
                                     selectedCode: selectedCode, selectedName: stock?.name,
                                     quoteAsOf: activeIntraday?.tradeDate.isEmpty == false ? activeIntraday?.tradeDate : activeDetail?.asOf,
                                     observedAtUtc: Date().ISO8601Format(),
                                     chartPeriod: chartPeriod.title, latestPointTime: latestTime,
                                     metrics: metrics, visiblePanels: panels,
                                     recentActions: scopedActions)
        await voice.updateContext(context)
    }

    private func voiceActionKind(_ action: String) -> String {
        if action.hasPrefix("搜索股票") { return "search" }
        if action.hasPrefix("图表标注") { return "annotation" }
        if action.contains("周期") || action.contains("K线") { return "chart_period" }
        if action.contains("面板") || action.contains("侧栏") { return "panel" }
        return "interaction"
    }
}

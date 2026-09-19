import Foundation

struct OrderLevel: Codable, Hashable {
    let price: Double?
    let volume: Double?
}

struct Stock: Codable, Identifiable, Hashable {
    let code: String
    let name: String
    let price: Double?
    let changePct: Double?
    let changeAmount: Double?
    let turnover: Double?
    let turnoverRate: Double?
    let open: Double?
    let high: Double?
    let low: Double?
    let prevClose: Double?
    let volume: Double?
    let marketCap: Double?
    let floatMarketCap: Double?
    let amplitude: Double?
    let volumeRatio: Double?
    let peDynamic: Double?
    let pb: Double?
    let speed: Double?
    let change5m: Double?
    let change60d: Double?
    let changeYtd: Double?
    let upLimit: Double?
    let downLimit: Double?
    let innerVolume: Double?
    let outerVolume: Double?
    let bids: [OrderLevel]?
    let asks: [OrderLevel]?
    let sector: String?
    let quoteTime: String?
    let quoteSource: String?
    var id: String { code }
}

struct Candle: Codable, Identifiable {
    let time: String
    let open: Double
    let high: Double
    let low: Double
    let close: Double
    let volume: Double?
    var id: String { time }
}

struct IntradayPoint: Codable, Identifiable {
    let time: String
    let price: Double
    let volume: Double
    let averagePrice: Double?
    var id: String { time }
}

struct IntradayResponse: Codable {
    let code: String
    let tradeDate: String
    let previousClose: Double?
    let rows: [IntradayPoint]
    let warning: String?
}

struct KLineResponse: Codable {
    let code: String
    let period: String
    let rows: [Candle]
    let warning: String?
}

struct HotSector: Codable, Identifiable {
    let code: String?
    let name: String
    let changePct: Double?
    let netAmount: Double?
    let netAmountRate: Double?
    let tradeDate: String?
    var id: String { code ?? name }
}

struct MarketOverview: Codable {
    let asOf: String?
    let rising: Int
    let falling: Int
    let flat: Int
    let limitUp: Int
    let limitDown: Int
    let turnover: Double?
    let northMoney: Double?
    let southMoney: Double?
    let hotSectors: [HotSector]
}

struct TechnicalMetrics: Codable {
    let ma5: Double?
    let ma10: Double?
    let ma20: Double?
    let ma60: Double?
    let high60d: Double?
    let volumeRatio5d: Double?
    let macdDif: Double?
    let macdDea: Double?
    let macdHist: Double?
    let kdjK: Double?
    let kdjD: Double?
    let kdjCrossDaysAgo: Double?
    let profitRatio: Double?
    let chipConcentration: Double?

    enum CodingKeys: String, CodingKey {
        case ma5, ma10, ma20, ma60
        case high60d = "high_60d"
        case volumeRatio5d = "volume_ratio_5d"
        case macdDif = "macd_dif"
        case macdDea = "macd_dea"
        case macdHist = "macd_hist"
        case kdjK = "kdj_k"
        case kdjD = "kdj_d"
        case kdjCrossDaysAgo = "kdj_cross_days_ago"
        case profitRatio = "profit_ratio"
        case chipConcentration = "chip_concentration"
    }
}

struct TechnicalHistory: Codable, Identifiable {
    let tradeDate: String
    let macdDif: Double?
    let macdDea: Double?
    let macdHist: Double?
    let kdjK: Double?
    let kdjD: Double?
    var id: String { tradeDate }

    enum CodingKeys: String, CodingKey {
        case tradeDate
        case macdDif = "macd_dif"
        case macdDea = "macd_dea"
        case macdHist = "macd_hist"
        case kdjK = "kdj_k"
        case kdjD = "kdj_d"
    }
}

struct TechnicalPanel: Codable {
    let asOf: String?
    let metrics: TechnicalMetrics
    let history: [TechnicalHistory]
}

struct FundMetrics: Codable {
    let latestMainInflow: Double?
    let mainInflow5d: Double?
    let latestMainRatio: Double?
    let buySmallAmount: Double?
    let sellSmallAmount: Double?
    let buyMediumAmount: Double?
    let sellMediumAmount: Double?
    let buyLargeAmount: Double?
    let sellLargeAmount: Double?
    let buyExtraLargeAmount: Double?
    let sellExtraLargeAmount: Double?

    enum CodingKeys: String, CodingKey {
        case latestMainInflow = "latest_main_inflow"
        case mainInflow5d = "main_inflow_5d"
        case latestMainRatio = "latest_main_ratio"
        case buySmallAmount = "buy_sm_amount"
        case sellSmallAmount = "sell_sm_amount"
        case buyMediumAmount = "buy_md_amount"
        case sellMediumAmount = "sell_md_amount"
        case buyLargeAmount = "buy_lg_amount"
        case sellLargeAmount = "sell_lg_amount"
        case buyExtraLargeAmount = "buy_elg_amount"
        case sellExtraLargeAmount = "sell_elg_amount"
    }
}

struct FundHistory: Codable, Identifiable {
    let tradeDate: String
    let latestMainInflow: Double?
    let latestMainRatio: Double?
    let buySmallAmount: Double?
    let sellSmallAmount: Double?
    let buyMediumAmount: Double?
    let sellMediumAmount: Double?
    let buyLargeAmount: Double?
    let sellLargeAmount: Double?
    let buyExtraLargeAmount: Double?
    let sellExtraLargeAmount: Double?
    var id: String { tradeDate }

    enum CodingKeys: String, CodingKey {
        case tradeDate
        case latestMainInflow = "latest_main_inflow"
        case latestMainRatio = "latest_main_ratio"
        case buySmallAmount = "buy_sm_amount"
        case sellSmallAmount = "sell_sm_amount"
        case buyMediumAmount = "buy_md_amount"
        case sellMediumAmount = "sell_md_amount"
        case buyLargeAmount = "buy_lg_amount"
        case sellLargeAmount = "sell_lg_amount"
        case buyExtraLargeAmount = "buy_elg_amount"
        case sellExtraLargeAmount = "sell_elg_amount"
    }
}

struct ChipDistributionRow: Codable, Identifiable {
    let price: Double
    let percent: Double
    var id: Double { price }
}

struct ChipDistributionResponse: Codable {
    let code: String
    let start: String?
    let end: String?
    let rows: [ChipDistributionRow]
    let currentPrice: Double?
    let averageCost: Double?
    let winnerRate: Double?
    let cost5: Double?
    let cost95: Double?
    let concentration: Double?
    let source: String?
    let warning: String?
}

struct StockSignal: Codable, Identifiable {
    let tradeDate: String?
    let reason: String?
    let netAmount: Double?
    let netRate: Double?
    let rank: Int?
    let amount: Double?
    let buy: Double?
    let sell: Double?
    let upLimit: Double?
    let downLimit: Double?
    var id: String { "\(tradeDate ?? "")-\(reason ?? "")-\(rank ?? 0)" }
    enum CodingKeys: String, CodingKey {
        case tradeDate = "trade_date", reason, netAmount = "net_amount", netRate = "net_rate"
        case rank, amount, buy, sell, upLimit = "up_limit", downLimit = "down_limit"
    }
}

struct StockSignals: Codable {
    let topList: [StockSignal]?
    let northbound: [StockSignal]?
    let limits: [StockSignal]?
}

struct FundPanel: Codable {
    let asOf: String?
    let metrics: FundMetrics
    let history: [FundHistory]
}

struct ChipPanel: Codable {
    let asOf: String?
    let low: Double?
    let high: Double?
    let cost5: Double?
    let cost15: Double?
    let cost50: Double?
    let cost85: Double?
    let cost95: Double?
    let average: Double?
    let winnerRate: Double?
}

struct PeerStock: Codable, Identifiable {
    let code: String
    let name: String
    let price: Double?
    let changePct: Double?
    let turnoverRate: Double?
    let marketCap: Double?
    var id: String { code }
}

struct Announcement: Codable, Identifiable {
    let title: String?
    let date: String?
    let category: String?
    let url: String?
    var id: String { url ?? "\(date ?? "")-\(title ?? "")" }
}

struct StocksResponse: Codable {
    let asOf: String?
    let items: [Stock]
}

struct StockResponse: Codable {
    let asOf: String?
    let stock: Stock
    let candles: [Candle]
    let technical: TechnicalPanel?
    let fund: FundPanel?
    let chips: ChipPanel?
    let peers: [PeerStock]?
    let concepts: [String]?
    let announcements: [Announcement]?
    let signals: StockSignals?
}

struct RealtimeResponse: Codable {
    let items: [Stock]
    let warning: String?
}

enum ChartPeriod: String, CaseIterable, Identifiable {
    case intraday, m5, m15, m30, m60, day, week, month
    var id: String { rawValue }
    var title: String {
        switch self {
        case .intraday: return "分时"
        case .m5: return "5分"
        case .m15: return "15分"
        case .m30: return "30分"
        case .m60: return "60分"
        case .day: return "日K"
        case .week: return "周K"
        case .month: return "月K"
        }
    }
}

struct VoiceUIAction: Codable, Hashable, Identifiable {
    let id: String
    let kind: String
    let label: String
    let occurredAtUtc: String
    let stockCode: String?
    let chartPeriod: String?
}

struct VoiceUIContext: Codable {
    let screen: String
    let selectedCode: String?
    let selectedName: String?
    let quoteAsOf: String?
    let quoteTime: String?
    let quoteSource: String?
    let observedAtUtc: String
    let chartPeriod: String
    let latestPointTime: String?
    let metrics: [String: String]
    let visiblePanels: [String]
    let recentActions: [VoiceUIAction]
    let chartPeriodID: String?
    let viewState: VoiceViewState?
    let chart: VoiceChartContext?
    let annotations: VoiceAnnotationContext?
    let orderBook: VoiceOrderBookContext?
}

struct VoiceViewState: Codable, Hashable {
    var detailTab = "chart"
    var inspectorVisible = false
    var inspectorMode: String?
    var inspectorPresentation = "hidden"
    var settingsPresented = false
    // This describes the active tab, not individual cards hidden below its scroll viewport.
    var visibilityScope = "active_tab"
    var chartViewport: VoiceChartViewport?
    var workspacePageID: String?
    var workspacePageTitle: String?
    var visibleCardIDs: [String]?
}

struct VoiceChartViewport: Codable, Hashable {
    let firstVisibleTime: String?
    let lastVisibleTime: String?
    let visiblePointCount: Int
    // A historical window's summary is separate from the live market quote.
    let historicalSummary: VoiceChartPoint?
}

struct VoiceChartPoint: Codable, Hashable {
    let time: String
    var open: Double? = nil
    var high: Double? = nil
    var low: Double? = nil
    var close: Double? = nil
    var price: Double? = nil
    var volume: Double? = nil
}

struct VoiceChartContext: Codable, Hashable {
    let stockCode: String
    let period: String
    let kind: String
    let firstVisibleTime: String?
    let lastVisibleTime: String?
    let visiblePointCount: Int
    let selectedPoint: VoiceChartPoint?
    let selectionSource: String
}

struct VoiceAnnotationItem: Codable, Hashable {
    let id: String
    let kind: String
    let text: String?
    let color: String
    let start: AnnotationPoint?
    let end: AnnotationPoint?
}

struct VoiceAnnotationContext: Codable, Hashable {
    let stockCode: String
    let surfaceID: String
    let editing: Bool
    let tool: String
    let structuredCount: Int
    let freehandStrokeCount: Int
    let selectionSupported: Bool
    let items: [VoiceAnnotationItem]
}

struct VoiceChartSnapshot: Hashable {
    let chart: VoiceChartContext
    let annotations: VoiceAnnotationContext
}

struct VoiceOrderBookContext: Codable {
    let bids: [OrderLevel]
    let asks: [OrderLevel]
}

struct PairResponse: Decodable {
    let token: String
    let deviceId: String
    let aiEnabled: Bool?
}

struct VoiceEvent: Decodable {
    let type: String
    let state: String?
    let reason: String?
    let fatal: Bool?
    let sessionId: String?
    let threadId: String?
    let role: String?
    let text: String?
    let final: Bool?
    let messageId: String?
    let items: [VoiceHistoryItem]?
    let code: String?
    let message: String?
    let actionId: String?
    let capability: String?
    let operation: String?
    let color: String?
    let x: Double?
    let y: Double?
    let x2: Double?
    let y2: Double?
}

struct VoiceHistoryItem: Decodable {
    let id: String
    let role: String
    let text: String
}

struct Transcript: Identifiable {
    var id: String
    let role: String
    var text: String
    var isFinal: Bool

    init(id: String = UUID().uuidString, role: String, text: String, isFinal: Bool) {
        self.id = id
        self.role = role
        self.text = text
        self.isFinal = isFinal
    }
}

enum AppError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let text): return text }
    }
}

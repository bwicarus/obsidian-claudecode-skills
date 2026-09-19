import Foundation

struct Stock: Codable, Identifiable, Hashable {
    let code: String
    let name: String
    let price: Double?
    let changePct: Double?
    let turnover: Double?
    let sector: String?
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

struct StocksResponse: Decodable {
    let asOf: String?
    let items: [Stock]
}

struct StockResponse: Decodable {
    let asOf: String?
    let stock: Stock
    let candles: [Candle]
}

struct PairResponse: Decodable {
    let token: String
    let deviceId: String
}

struct VoiceEvent: Decodable {
    let type: String
    let state: String?
    let sessionId: String?
    let threadId: String?
    let role: String?
    let text: String?
    let final: Bool?
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

struct Transcript: Identifiable {
    let id = UUID()
    let role: String
    var text: String
    var isFinal: Bool
}

enum AppError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let text): return text }
    }
}

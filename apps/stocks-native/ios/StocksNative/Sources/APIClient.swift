import Foundation

struct APIClient {
    let baseURL: URL
    let token: String?

    static func normalizedBase(_ value: String) throws -> URL {
        guard var components = URLComponents(string: value.trimmingCharacters(in: .whitespacesAndNewlines)),
              components.scheme == "https", components.host != nil,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil else {
            throw AppError.message("请输入完整的 HTTPS 服务地址。")
        }
        while components.path.hasSuffix("/") { components.path.removeLast() }
        guard let url = components.url else { throw AppError.message("服务地址无效。") }
        return url
    }

    func pair(code: String, deviceID: String, name: String) async throws -> PairResponse {
        let payload = ["code": code, "deviceId": deviceID, "name": name]
        return try await request("api/pair", method: "POST", body: JSONEncoder().encode(payload))
    }

    func appleLogin(identityToken: String, rawNonce: String, deviceID: String, name: String) async throws -> PairResponse {
        let payload = ["identityToken": identityToken, "rawNonce": rawNonce,
                       "deviceId": deviceID, "name": name]
        return try await request("api/auth/apple", method: "POST", body: JSONEncoder().encode(payload))
    }

    func stocks(query: String) async throws -> StocksResponse {
        try await request("api/stocks", query: [URLQueryItem(name: "q", value: query), URLQueryItem(name: "limit", value: "50")])
    }

    func stock(code: String) async throws -> StockResponse {
        try await request("api/stocks/\(code)")
    }

    func marketOverview() async throws -> MarketOverview {
        try await request("api/market/overview")
    }

    func realtime(codes: [String]) async throws -> RealtimeResponse {
        try await request("api/realtime", query: [URLQueryItem(name: "codes", value: codes.joined(separator: ","))])
    }

    func intraday(code: String) async throws -> IntradayResponse {
        try await request("api/stocks/\(code)/intraday")
    }

    func chips(code: String, start: String? = nil, end: String? = nil) async throws -> ChipDistributionResponse {
        var query: [URLQueryItem] = []
        if let start { query.append(URLQueryItem(name: "start", value: start)) }
        if let end { query.append(URLQueryItem(name: "end", value: end)) }
        return try await request("api/stocks/\(code)/chips", query: query)
    }

    func kline(code: String, period: ChartPeriod, count: Int = 180) async throws -> KLineResponse {
        try await request("api/stocks/\(code)/kline", query: [
            URLQueryItem(name: "period", value: period.rawValue),
            URLQueryItem(name: "count", value: String(count)),
        ])
    }

    func selectionCatalog() async throws -> SelectionCatalog {
        try await request("api/selection/catalog")
    }

    func selectionLibrary() async throws -> SelectionLibrary {
        try await request("api/selection/library")
    }

    func evaluateSelection(_ definition: SelectionEvaluateRequest) async throws -> SelectionEvaluation {
        try await request("api/selection/evaluate", method: "POST", body: JSONEncoder().encode(definition))
    }

    func mutateSelection(_ mutation: SelectionMutation) async throws -> SelectionMutationReceipt {
        try await request("api/selection/mutate", method: "POST", body: JSONEncoder().encode(mutation))
    }

    func monitoringCatalog() async throws -> MonitoringCatalog {
        try await request("api/monitor/catalog")
    }

    func monitoringLibrary() async throws -> MonitoringLibrary {
        try await request("api/monitor/library")
    }

    func mutateMonitoring(_ mutation: MonitoringMutation) async throws -> MonitoringMutationReceipt {
        try await request("api/monitor/mutate", method: "POST", body: JSONEncoder().encode(mutation))
    }

    func recordMonitoringReceipt(id: String) async throws {
        let payload = MonitoringDeliveryReceipt(notificationId: id, channel: "visual", outcome: "displayed")
        let _: MonitoringDeliveryResponse = try await request("api/notifications/receipt", method: "POST", body: JSONEncoder().encode(payload))
    }

    func webSocketURL(deviceID: String) throws -> URL {
        var components = URLComponents(url: baseURL.appendingPathComponent("voice"), resolvingAgainstBaseURL: false)!
        components.scheme = "wss"
        components.queryItems = [URLQueryItem(name: "deviceId", value: deviceID)]
        guard let url = components.url else { throw AppError.message("语音地址无效。") }
        return url
    }

    private func request<T: Decodable>(_ path: String, method: String = "GET", body: Data? = nil,
                                       query: [URLQueryItem] = []) async throws -> T {
        var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { components.queryItems = query }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.timeoutInterval = 30
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw AppError.message("服务器未返回 HTTP 响应。") }
        guard (200..<300).contains(response.statusCode) else {
            if path.hasPrefix("api/monitor/") {
                let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
                throw MonitoringAPIError(status: response.statusCode,
                                         message: payload?["message"] as? String ?? payload?["error"] as? String
                                            ?? "请求失败（HTTP \(response.statusCode)）。")
            }
            if path.hasPrefix("api/selection/") {
                let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
                throw SelectionAPIError(status: response.statusCode, code: payload?["code"] as? String,
                                        message: payload?["message"] as? String ?? payload?["error"] as? String
                                            ?? "请求失败（HTTP \(response.statusCode)）。",
                                        revision: payload?["revision"] as? Int)
            }
            if response.statusCode == 401 { throw AppError.message("设备凭证无效或已过期，请重新配对。") }
            if response.statusCode == 403 { throw AppError.message("配对码不正确或设备访问被拒绝。") }
            let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let message = payload?["message"] as? String ?? payload?["error"] as? String
            throw AppError.message(message ?? "请求失败（HTTP \(response.statusCode)）。")
        }
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw AppError.message("数据格式与 App 不兼容：\(error.localizedDescription)") }
    }
}

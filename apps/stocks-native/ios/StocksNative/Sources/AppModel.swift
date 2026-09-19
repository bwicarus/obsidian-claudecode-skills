import Combine
import Foundation
import UIKit

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var baseURL: String
    @Published private(set) var isPaired: Bool
    @Published var query = ""
    @Published var selectedCode: String?
    @Published private(set) var stocks: [Stock] = []
    @Published private(set) var detail: StockResponse?
    @Published private(set) var listAsOf: String?
    @Published private(set) var isLoadingList = false
    @Published private(set) var isLoadingDetail = false
    @Published private(set) var listError: String?
    @Published private(set) var detailError: String?
    let deviceID: String
    let voice = VoiceSession()
    let annotations = AnnotationStore()
    private var listGeneration = UUID()
    private var detailGeneration = UUID()

    init() {
        let initialBase = UserDefaults.standard.string(forKey: "stocksNative.baseURL") ?? "https://bwicarus.space/stocks-native"
        baseURL = initialBase
        isPaired = Credentials.token(baseURL: initialBase) != nil
        let savedID = UserDefaults.standard.string(forKey: "stocksNative.deviceID") ?? UUID().uuidString
        deviceID = savedID
        UserDefaults.standard.set(savedID, forKey: "stocksNative.deviceID")
        voice.onStockSelected = { [weak self] code in self?.selectedCode = code }
        voice.onCapabilityAction = { [weak self] action in
            guard let self else { return CapabilityResult(success: false, message: "App 状态不可用。") }
            return self.annotations.perform(action, selectedStockCode: self.selectedCode)
        }
    }

    var client: APIClient {
        // Every saved URL has passed normalizedBase; the bundled default is a fixed HTTPS URL.
        APIClient(baseURL: URL(string: baseURL)!, token: Credentials.token(baseURL: baseURL))
    }

    func pair(base: String, code: String) async throws {
        let normalized = try APIClient.normalizedBase(base)
        let code = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !code.isEmpty else { throw AppError.message("请输入配对码。") }
        let pairClient = APIClient(baseURL: normalized, token: nil)
        let result = try await pairClient.pair(code: code, deviceID: deviceID, name: UIDevice.current.name)
        guard result.deviceId == deviceID, !result.token.isEmpty else { throw AppError.message("服务器返回的设备凭证不匹配。") }
        try Credentials.save(token: result.token, baseURL: normalized.absoluteString)
        await voice.stop()
        listGeneration = UUID()
        detailGeneration = UUID()
        baseURL = normalized.absoluteString
        UserDefaults.standard.set(baseURL, forKey: "stocksNative.baseURL")
        isPaired = true
        stocks = []
        selectedCode = nil
        detail = nil
        listAsOf = nil
        await loadStocks()
    }

    func unpair() async {
        await voice.stop()
        Credentials.delete(baseURL: baseURL)
        isPaired = false
        listGeneration = UUID()
        detailGeneration = UUID()
        stocks = []
        selectedCode = nil
        detail = nil
        listAsOf = nil
        listError = nil
        detailError = nil
        isLoadingList = false
        isLoadingDetail = false
    }

    func loadStocks() async {
        guard isPaired else { return }
        let current = UUID()
        listGeneration = current
        isLoadingList = true
        listError = nil
        do {
            let result = try await client.stocks(query: query)
            guard current == listGeneration, !Task.isCancelled else { return }
            stocks = result.items
            listAsOf = result.asOf
            if selectedCode == nil { selectedCode = result.items.first?.code }
        } catch {
            guard current == listGeneration, !Task.isCancelled else { return }
            listError = error.localizedDescription
        }
        if current == listGeneration { isLoadingList = false }
    }

    func loadDetail() async {
        let current = UUID()
        detailGeneration = current
        guard isPaired, let code = selectedCode else {
            detail = nil
            isLoadingDetail = false
            return
        }
        if detail?.stock.code != code { detail = nil }
        isLoadingDetail = true
        detailError = nil
        do {
            let result = try await client.stock(code: code)
            guard current == detailGeneration, selectedCode == code, !Task.isCancelled else { return }
            detail = result
        } catch {
            guard current == detailGeneration, !Task.isCancelled else { return }
            detailError = error.localizedDescription
        }
        if current == detailGeneration { isLoadingDetail = false }
    }
}

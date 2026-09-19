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

struct StocksRootView: View {
    @ObservedObject var model: AppModel
    @Environment(\.horizontalSizeClass) private var sizeClass
    @Environment(\.scenePhase) private var scenePhase
    @State private var showingSettings = false
    @State private var showingCompactVoice = false
    @State private var showingSidebar = true
    @State private var detailWidth: CGFloat = 0

    var body: some View {
        NavigationSplitView {
            stockList
                .navigationTitle("股票")
                .navigationSplitViewColumnWidth(min: 240, ideal: 290, max: 340)
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button { showingSettings = true } label: { Image(systemName: "slider.horizontal.3") }
                            .accessibilityLabel("设置与设备配对")
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { Task { await model.loadStocks() } } label: { Image(systemName: "arrow.clockwise") }
                            .disabled(!model.isPaired || model.isLoadingList)
                            .accessibilityLabel("刷新股票列表")
                    }
                }
        } detail: {
            GeometryReader { geometry in
                HStack(spacing: 0) {
                    StockDetailView(model: model)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    if sizeClass == .regular && geometry.size.width >= 760 && showingSidebar {
                        Divider()
                        VoiceSidebar(voice: model.voice, model: model)
                            .frame(width: 310)
                    }
                }
                .onAppear { detailWidth = geometry.size.width }
                .onChange(of: geometry.size.width) { _, width in detailWidth = width }
            }
            .background(AppStyle.canvas)
            .navigationTitle(model.displayedStock?.name ?? "市场概览")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        if sizeClass == .compact || detailWidth < 760 { showingCompactVoice = true }
                        else { withAnimation(.easeInOut(duration: 0.2)) { showingSidebar.toggle() } }
                    } label: { Image(systemName: "waveform") }
                        .accessibilityLabel("语音助手侧栏")
                }
            }
        }
        .tint(AppStyle.accent)
        .sheet(isPresented: $showingSettings) { PairingView(model: model) }
        .sheet(isPresented: $showingCompactVoice) {
            NavigationStack {
                VoiceSidebar(voice: model.voice, model: model)
                    .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("完成") { showingCompactVoice = false } } }
            }
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
        .task(id: model.selectedCode) {
            if let code = model.selectedCode { await model.voice.selectStock(code) }
            await model.loadDetail()
            if let code = model.selectedCode { await model.publishVoiceContext(action: "打开股票：\(code)") }
        }
        .task(id: "\(model.selectedCode ?? ""):\(model.chartPeriod.rawValue)") {
            await model.loadChart()
            await model.publishVoiceContext(action: "切换图表：\(model.chartPeriod.title)")
        }
        .task(id: scenePhase) {
            guard scenePhase == .active, model.isPaired else { return }
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(10)) } catch { return }
                await model.refreshLiveData()
            }
        }
        .onChange(of: scenePhase) { _, phase in
            // MVP does not promise background audio; explicitly release its session when backgrounded.
            if phase == .background, model.voice.isStarted { Task { await model.voice.stop() } }
        }
    }

    private var stockList: some View {
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
                List(model.stocks, selection: $model.selectedCode) { stock in
                    StockRow(stock: stock).tag(stock.code)
                }
                .listStyle(.sidebar)
                .overlay {
                    if model.isLoadingList && model.stocks.isEmpty { ProgressView("读取行情…") }
                    else if model.stocks.isEmpty && model.listError == nil {
                        ContentUnavailableView.search(text: model.query)
                    }
                }
                .refreshable { await model.loadStocks() }
                .searchable(text: $model.query, prompt: "代码或名称")
                VStack(alignment: .leading, spacing: 4) {
                    Text("数据时间").font(.caption2).foregroundStyle(.secondary)
                    Text(model.listAsOf ?? "服务器未提供时间").font(.caption2).monospacedDigit()
                }
                .padding(.horizontal, 18).padding(.vertical, 12).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

private struct StockRow: View {
    let stock: Stock
    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                Text(stock.name).font(.system(.body, design: .rounded, weight: .medium)).lineLimit(1)
                HStack(spacing: 6) {
                    Text(stock.code).monospaced()
                    if let sector = stock.sector, !sector.isEmpty {
                        Text("·").foregroundStyle(.tertiary)
                        Text(sector).lineLimit(1)
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 5) {
                Text(AppStyle.price(stock.price)).font(.system(.body, design: .rounded, weight: .semibold))
                Text(AppStyle.change(stock.changePct)).font(.caption).foregroundStyle(AppStyle.movement(stock.changePct))
                if let rate = stock.turnoverRate {
                    Text("换 \(AppStyle.percent(rate))").font(.caption2).foregroundStyle(.secondary)
                }
            }
            .monospacedDigit()
        }
        .padding(.vertical, 6)
    }
}

private struct MarketPulseStrip: View {
    let overview: MarketOverview
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                pulse("涨", overview.rising, AppStyle.up)
                pulse("跌", overview.falling, AppStyle.down)
                pulse("平", overview.flat, .secondary)
                Spacer(minLength: 4)
                VStack(alignment: .trailing, spacing: 2) {
                    Text("成交额").font(.caption2).foregroundStyle(.secondary)
                    Text(AppStyle.compact(overview.turnover)).font(.caption.monospacedDigit())
                }
            }
            HStack(spacing: 14) {
                Text("涨停 \(overview.limitUp)").foregroundStyle(AppStyle.up)
                Text("跌停 \(overview.limitDown)").foregroundStyle(AppStyle.down)
                if let sector = overview.hotSectors.first {
                    Spacer()
                    Text("热 · \(sector.name)").lineLimit(1)
                    Text(AppStyle.change(sector.changePct)).foregroundStyle(AppStyle.movement(sector.changePct))
                }
            }
            .font(.caption2)
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
        .background(.white)
    }

    private func pulse(_ title: String, _ value: Int, _ color: Color) -> some View {
        HStack(spacing: 3) {
            Text(title).foregroundStyle(.secondary)
            Text(String(value)).foregroundStyle(color).monospacedDigit().fontWeight(.semibold)
        }
        .font(.caption)
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
                    LabeledContent("版本", value: "0.2.2")
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

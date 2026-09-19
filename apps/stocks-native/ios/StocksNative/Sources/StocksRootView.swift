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
            .navigationTitle(model.detail?.stock.name ?? "市场概览")
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
            if model.isPaired { await model.loadStocks() }
            else { showingSettings = true }
        }
        .task(id: model.query) {
            do { try await Task.sleep(nanoseconds: 300_000_000) } catch { return }
            await model.loadStocks()
        }
        .task(id: model.selectedCode) {
            if let code = model.selectedCode { await model.voice.selectStock(code) }
            await model.loadDetail()
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
                Text(stock.code).font(.caption).monospaced().foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 5) {
                Text(AppStyle.price(stock.price)).font(.system(.body, design: .rounded, weight: .semibold))
                Text(AppStyle.change(stock.changePct)).font(.caption).foregroundStyle(AppStyle.movement(stock.changePct))
            }
            .monospacedDigit()
        }
        .padding(.vertical, 6)
    }
}

struct PairingView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var base = ""
    @State private var code = ""
    @State private var isPairing = false
    @State private var error: String?

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
                    SecureField("配对码", text: $code)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button {
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
                    } label: {
                        HStack {
                            Text(model.isPaired ? "重新配对" : "配对此设备")
                            Spacer()
                            if isPairing { ProgressView() } else { Image(systemName: "arrow.right") }
                        }
                    }
                    .disabled(isPairing || code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    if let error { Text(error).foregroundStyle(.red).font(.footnote) }
                }
                Section("设备") {
                    LabeledContent("版本", value: "0.2.0")
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
}

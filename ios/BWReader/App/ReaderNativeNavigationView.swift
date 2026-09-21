import SwiftUI

@MainActor
final class ReaderNativeNavigationModel: ObservableObject, Identifiable {
    let id = UUID()
    private let scope: String
    private let request: ([String: Any]) async -> [String: Any]
    @Published private(set) var state: [String: Any] = [:]
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    var position: Int { (state["position"] as? NSNumber)?.intValue ?? 1 }
    var total: Int { max(1, (state["total"] as? NSNumber)?.intValue ?? 1) }
    var firstDisplay: Int { (state["firstDisplay"] as? NSNumber)?.intValue ?? 1 }
    var display: Int { (state["display"] as? NSNumber)?.intValue ?? 1 }
    var unit: String { state["unit"] as? String ?? "页" }
    var totalLabel: String { state["totalLabel"] as? String ?? "" }
    var backLabel: String { state["backLabel"] as? String ?? "" }
    var ready: Bool { state["ready"] as? Bool == true }

    init(scope: String, request: @escaping ([String: Any]) async -> [String: Any]) {
        self.scope = scope
        self.request = request
    }

    func receive(_ state: [String: Any]) { self.state = state }

    func update(_ key: String? = nil, value: Any? = nil) async {
        guard !busy else { return }
        busy = true; error = nil
        defer { busy = false }
        var command: [String: Any] = ["action": key == nil ? "navigationRead" : "navigationAction", "scope": scope]
        if let key { command["text"] = key }
        if let value { command["value"] = value }
        let receipt = await request(command)
        guard !Task.isCancelled else { return }
        guard receipt["ok"] as? Bool == true, let state = receipt["value"] as? [String: Any] else {
            error = receipt["error"] as? String ?? "无法完成跳转，请重试。"
            return
        }
        self.state = state
    }
}

@MainActor
struct ReaderNativeNavigationView: View {
    @ObservedObject var model: ReaderNativeNavigationModel
    @State private var position = 1.0
    @State private var page = ""
    @State private var scrubbing = false

    var body: some View {
        VStack(spacing: 14) {
            HStack {
                Button { Task { await model.update("previous") } } label: { Image(systemName: "chevron.left") }
                    .disabled(model.busy || model.state["previous"] as? Bool != true)
                    .accessibilityLabel("上一" + model.unit)
                Spacer()
                Text("第 \(Int(position) + model.firstDisplay - 1) \(model.unit) / \(model.totalLabel)")
                    .font(.subheadline.monospacedDigit())
                Spacer()
                Button { Task { await model.update("next") } } label: { Image(systemName: "chevron.right") }
                    .disabled(model.busy || model.state["next"] as? Bool != true)
                    .accessibilityLabel("下一" + model.unit)
            }
            Slider(value: $position, in: 1...Double(max(2, model.total)), step: 1) { editing in
                scrubbing = editing
                if !editing { Task { await model.update("position", value: Int(position)) } }
            }
            .disabled(model.busy || !model.ready || model.total < 2)
            .accessibilityLabel("阅读位置")
            HStack {
                TextField("输入" + model.unit + "码", text: $page)
                    .textFieldStyle(.roundedBorder).keyboardType(.numbersAndPunctuation)
                    .onSubmit { Task { await model.update("page", value: page) } }
                Button("前往") { Task { await model.update("page", value: page) } }
                    .buttonStyle(.borderedProminent)
            }.disabled(model.busy || !model.ready)
            if !model.backLabel.isEmpty {
                Button(model.backLabel, systemImage: "arrow.uturn.backward") { Task { await model.update("back") } }
                    .disabled(model.busy)
            }
            if model.busy { ProgressView() }
            if let error = model.error { Text(error).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
        }
        .padding(18).frame(width: 320)
        .background(ReaderNativeTheme.canvas).tint(ReaderNativeTheme.accent)
        .task { await model.update(); sync() }
        .onChange(of: model.position) { _, _ in sync() }
        .onChange(of: model.busy) { _, busy in if !busy { sync() } }
    }

    private func sync() {
        guard !scrubbing else { return }
        position = Double(min(model.total, max(1, model.position)))
        page = String(model.display)
    }
}

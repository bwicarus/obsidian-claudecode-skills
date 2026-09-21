import SwiftUI
import UIKit

@MainActor
final class ReaderNativeReadingSettingsModel: ObservableObject, Identifiable {
    let id = UUID()
    private let scope: String
    private let request: ([String: Any]) async -> [String: Any]
    @Published private(set) var state: [String: Any] = [:]
    @Published private(set) var busy = false
    @Published private(set) var error: String?

    init(scope: String, request: @escaping ([String: Any]) async -> [String: Any]) {
        self.scope = scope
        self.request = request
    }

    func load() async { await transact(["action": "readingSettingsRead"]) }
    func save(_ key: String, _ value: Any) async {
        await transact(["action": "readingSettingsWrite", "key": key, "value": value])
    }
    private func transact(_ values: [String: Any]) async {
        guard !busy else { return }
        busy = true; error = nil
        defer { busy = false }
        var command = values; command["scope"] = scope
        let receipt = await request(command)
        guard !Task.isCancelled else { return }
        guard receipt["ok"] as? Bool == true, let value = receipt["value"] as? [String: Any] else {
            error = receipt["error"] as? String ?? "设置未确认保存，请刷新后核对。"
            return
        }
        state = value
    }
}

@MainActor
struct ReaderNativeReadingSettingsView: View {
    @ObservedObject var model: ReaderNativeReadingSettingsModel
    @Environment(\.dismiss) private var dismiss
    @State private var crop: [String: Double] = [:]
    @State private var colors: [String] = []
    @State private var newColor = Color.yellow

    var body: some View {
        NavigationStack {
            Form {
                if let error = model.error {
                    Section { Text(error).foregroundStyle(.red).textSelection(.enabled) }
                }
                if model.state.isEmpty { ProgressView("读取本书设置…") }
                else {
                    Section("阅读") {
                        settingToggle("生词下划线", key: "vocabulary")
                        settingToggle("点词翻译未掌握的内容", key: "clickTranslate")
                        settingToggle("随设备方向恢复阅读布局", key: "autoOrient")
                    }
                    Section {
                        language("英语", code: "en")
                        language("日语", code: "ja")
                        settingToggle("插图 AI 描述", key: "figures")
                    } header: { Text("本书语言与插图") }
                    footer: { Text("未选中的语言视为已掌握，免于翻译。开启插图描述后，沿用本书逐页生成的流程。") }
                    Section {
                        settingToggle("启用去边", key: "cropEnabled")
                        ForEach(["l", "r", "t", "b"], id: \.self) { key in
                            HStack {
                                Text(["l": "左", "r": "右", "t": "上", "b": "下"][key] ?? key)
                                    .frame(width: 24)
                                Slider(value: Binding(get: { crop[key] ?? 0 }, set: { crop[key] = $0 }), in: 0...45, step: 1)
                                Text("\(Int(crop[key] ?? 0))%")
                                    .monospacedDigit().frame(width: 44, alignment: .trailing)
                            }
                        }
                        Button("应用本书去边比例") { Task { await model.save("crop", crop) } }
                            .disabled(crop == savedCrop)
                    } header: { Text("去边阅读") }
                    footer: { Text("每边最多隐藏 45%。应用非零比例后自动启用，保留原文与批注坐标。") }
                    Section("语法结构") {
                        Picker("显示方式", selection: Binding(get: { model.state["grammar"] as? String ?? "components" }, set: { value in
                            Task { await model.save("grammar", value) }
                        })) {
                            Text("成分分块").tag("components")
                            Text("主干折叠").tag("skeleton")
                            Text("依存关系").tag("deps")
                            Text("结构树").tag("tree")
                        }
                    }
                    Section("高亮颜色") {
                        ForEach(colors.indices, id: \.self) { index in
                            HStack {
                                Circle().fill(Color(uiColor: UIColor.readerHex(colors[index]))).frame(width: 20, height: 20)
                                Text(colors[index].uppercased()).font(.body.monospaced())
                                Spacer()
                                Button("移除", systemImage: "minus.circle", role: .destructive) { colors.remove(at: index) }
                                    .labelStyle(.iconOnly).disabled(colors.count <= 1)
                            }
                        }
                        ColorPicker("添加颜色", selection: $newColor, supportsOpacity: false)
                        Button("加入色板") {
                            let value = UIColor(newColor).readerHexValue
                            if !colors.contains(value) && colors.count < 32 { colors.append(value) }
                        }
                        Button("保存色板") { Task { await model.save("colors", colors) } }
                            .disabled(colors == savedColors)
                        Button("恢复默认色板") { colors = ["#fff59d", "#a7f3d0", "#a3d4ff", "#fda4af"] }
                    }
                    Section("诊断") { settingToggle("显示阅读日志", key: "debug") }
                }
            }
            .disabled(model.busy)
            .scrollContentBackground(.hidden).background(ReaderNativeTheme.canvas)
            .navigationTitle("阅读设置").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
                ToolbarItem(placement: .topBarLeading) {
                    Button("刷新", systemImage: "arrow.clockwise") { Task { await model.load() } }.disabled(model.busy)
                }
            }
            .task { await model.load(); resetDrafts() }
            .onChange(of: savedCrop) { _, value in crop = value }
            .onChange(of: savedColors) { _, value in colors = value }
        }.tint(ReaderNativeTheme.accent)
    }

    private var savedCrop: [String: Double] {
        (model.state["crop"] as? [String: NSNumber] ?? [:]).mapValues(\.doubleValue)
    }
    private var savedColors: [String] { model.state["colors"] as? [String] ?? [] }
    private func resetDrafts() { crop = savedCrop; colors = savedColors }
    private func settingToggle(_ title: String, key: String) -> some View {
        Toggle(title, isOn: Binding(get: { model.state[key] as? Bool == true }, set: { value in
            Task { await model.save(key, value) }
        }))
    }
    private func language(_ title: String, code: String) -> some View {
        Toggle(title, isOn: Binding(get: { (model.state["languages"] as? [String] ?? []).contains(code) }, set: { on in
            var languages = model.state["languages"] as? [String] ?? []
            languages.removeAll { $0 == code }; if on { languages.append(code) }
            Task { await model.save("languages", languages) }
        }))
    }
}

private extension UIColor {
    static func readerHex(_ value: String) -> UIColor {
        var hex = value.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        if hex.count == 3 || hex.count == 4 { hex = hex.map { "\($0)\($0)" }.joined() }
        let number = UInt32(hex, radix: 16) ?? 0
        let rgb = hex.count == 8 ? number >> 8 : number
        let alpha = hex.count == 8 ? CGFloat(number & 255) / 255 : 1
        return UIColor(red: CGFloat((rgb >> 16) & 255) / 255, green: CGFloat((rgb >> 8) & 255) / 255,
                       blue: CGFloat(rgb & 255) / 255, alpha: alpha)
    }
    var readerHexValue: String {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        getRed(&r, green: &g, blue: &b, alpha: &a)
        return String(format: "#%02x%02x%02x", Int((r * 255).rounded()), Int((g * 255).rounded()), Int((b * 255).rounded()))
    }
}

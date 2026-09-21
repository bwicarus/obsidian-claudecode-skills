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
    /// 原生 PDF 主阅读区的开关（迁移中，默认关）。与 BWReaderNativeApp 同一个键。
    @AppStorage("reader.nativePDFRenderer") private var nativePDFRenderer = false
    /// iCloud 跨设备同步（默认关）。开关只驱动引擎起停；关掉不删云端也不删基线。
    @AppStorage("reader.iCloudSync") private var iCloudSync = false
    /// 数据落在 App 自己沙盒的 SQLite 里（默认关，迁移中）。
    /// ⚠ 翻开要**重开一次阅读器**才生效：库是启动时选定的，中途换不了。
    @AppStorage("reader.nativeDataStore") private var nativeDataStore = false
    /// 挂载失败时把原因摆在开关旁边 —— 否则只看到「开了但没变化」。
    /// 给默认值是为了不改既有调用点的写法。
    var nativePDFMountFailure: String? = nil
    /// 开关翻转时把它告诉阅读器（引擎的持有方在那边）。
    var onCloudSyncChanged: ((Bool) -> Void)? = nil

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
                            .disabled(model.state["figuresAvailable"] as? Bool != true)
                        ForEach(model.state["warnings"] as? [String] ?? [], id: \.self) { warning in
                            Text(warning).font(.caption).foregroundStyle(.secondary)
                        }
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
                    // 原生主阅读区（迁移中）。**默认关**：还没接齐的能力见下面的说明，
                    // 开着它就用不到那几项 —— 与其让人一头雾水，不如把边界写在开关旁边。
                    Section {
                        Toggle("用原生 PDFKit 渲染正文", isOn: $nativePDFRenderer)
                        if let failure = nativePDFMountFailure {
                            Label(failure, systemImage: "exclamationmark.triangle")
                                .font(.footnote).foregroundStyle(.orange)
                        }
                    } header: {
                        Text("原生阅读区（迁移中）")
                    } footer: {
                        // ⚠ 这段话是给人做决定用的，不是装饰：接齐一项就删一项。
                        //   写着"尚未接"而其实已经接了的话，用户会为了一个不存在的
                        //   限制一直关着它。
                        Text("开启后正文由 PDFKit 画，网页层退到后面继续管数据。"
                             + "已接：位置/翻页/布局/缩放/去边、选字、高亮与墨迹、"
                             + "查词/翻译/解释/词组/语法、划线与编辑、页卡、便签、"
                             + "整页翻译、图徽标、Pencil。"
                             + "尚未接：EPUB 仍整个用网页界面。")
                    }
                    Section {
                        Toggle("用 iCloud 在我的设备之间同步", isOn: $iCloudSync)
                            .onChange(of: iCloudSync) { _, on in onCloudSyncChanged?(on) }
                    } header: {
                        Text("同步")
                    } footer: {
                        // 说清楚"同步什么"和"按什么认书"，否则用户会拿两个不同版本的
                        // PDF 互相对照，然后以为同步坏了。
                        Text("同步高亮、便签、笔迹、插入页与阅读位置，走你自己的 iCloud"
                             + "（同一个 Apple 账号的设备之间）。"
                             + "按**书的内容**认书：同一个文件在两台设备上才对得上，"
                             + "重新导出或压缩过的版本算另一本。"
                             + "没登录 iCloud 时它安静地不工作，本地照常用。")
                    }
                    Section {
                        Toggle("本机数据库（迁移中）", isOn: $nativeDataStore)
                    } header: {
                        Text("存储")
                    } footer: {
                        // 把代价和生效时机写清楚 —— 否则用户翻了开关看不到任何变化，
                        // 会以为坏了又翻回去，来回几次刚好踩在迁移中途。
                        Text("把阅读数据改存在 App 自己的数据库里（原来在网页那层）。"
                             + "翻开后**重开一次阅读器**才生效，"
                             + "首次启动会把老数据搬过去，书多的话会多等几秒。"
                             + "搬家失败会自动关回去并报错，老数据不会被动。")
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

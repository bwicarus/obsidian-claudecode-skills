import SwiftUI

/// 插图描述面板（点图徽标弹出）。
///
/// 描述文本是**服务端早就生成好的**（夜间 figures-describe 那条流水线），这里既不
/// 生成也不排版判断，只显示 + 提供「带入助手」。Swift 持有选择；面板收到确认后
/// 更新按钮。发送明确的目标状态，使上下文投影中断后的重试不会反向取消选择。
@MainActor
final class ReaderNativeFigureModel: ObservableObject, Identifiable {
    let id: String
    let page: Int
    let caption: String
    let desc: String
    let group: Bool
    private let request: ([String: Any]) async -> [String: Any]

    @Published private(set) var attached: Bool
    @Published private(set) var busy = false
    @Published private(set) var error: String?

    init(figure: ReaderNativePDFDocument.Figure,
         request: @escaping ([String: Any]) async -> [String: Any]) {
        self.id = figure.id
        self.page = figure.page
        self.caption = figure.caption
        self.desc = figure.desc
        self.group = figure.group
        self.attached = figure.attached
        self.request = request
    }

    /// 带入 / 取消带入。回执给的 attached 才是事实 —— 本地先翻会和助手上下文对不上。
    var onAttachChanged: ((Bool) -> Void)?

    func toggleAttach() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        let receipt = await request([
            "action": "nativeFigureAttach",
            "value": ["id": id, "page": page, "attached": !attached],
        ])
        guard receipt["ok"] as? Bool == true else {
            error = receipt["error"] as? String ?? "带入失败，请重试。"
            return
        }
        attached = (receipt["value"] as? [String: Any])?["attached"] as? Bool ?? !attached
        onAttachChanged?(attached)
    }

    /// 描述是 Markdown。这里不引渲染器：按段落切开显示就够读，
    /// 顺手把行首的 `#`/`-`/`*` 去掉，免得满屏井号。
    var paragraphs: [String] {
        desc.split(separator: "\n", omittingEmptySubsequences: false)
            .map { line in
                var text = line.trimmingCharacters(in: .whitespaces)
                while let first = text.first, "#-*>".contains(first) {
                    text.removeFirst()
                    text = text.trimmingCharacters(in: .whitespaces)
                }
                return text
            }
            .filter { !$0.isEmpty }
    }
}

struct ReaderNativeFigureView: View {
    @ObservedObject var model: ReaderNativeFigureModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let error = model.error {
                        Label(error, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    }
                    if model.paragraphs.isEmpty {
                        Text("这张图还没有描述。夜间会自动生成，或在阅读设置里为本书开启「插图描述」。")
                            .foregroundStyle(ReaderNativeTheme.muted)
                    } else {
                        ForEach(Array(model.paragraphs.enumerated()), id: \.offset) { _, line in
                            Text(line).font(.callout).textSelection(.enabled)
                        }
                    }
                    Divider()
                    Button {
                        Task { await model.toggleAttach() }
                    } label: {
                        Label(model.attached ? "已带入助手（点此取消）" : (model.group ? "带入这个图组" : "带入这张图"),
                              systemImage: model.attached ? "checkmark.circle.fill" : "plus.circle")
                    }
                    .buttonStyle(.borderless)
                    .disabled(model.busy)
                    .accessibilityHint("带入后助手回答时会看到这张图")
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
            }
            .scrollContentBackground(.hidden)
            .background(ReaderNativeTheme.canvas)
            .navigationTitle(model.caption.isEmpty ? "图说明" : model.caption)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
        }
        .tint(ReaderNativeTheme.accent)
        .presentationDetents([.medium, .large])
    }
}

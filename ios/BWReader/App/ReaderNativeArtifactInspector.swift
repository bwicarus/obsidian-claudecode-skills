import SwiftUI

/// Read-only structured details. No web UI is opened and no tool is rerun.
@MainActor
struct ReaderNativeArtifactInspector: View {
    @ObservedObject var model: ReaderNativeConversationModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Group {
                if let detail = model.inspection {
                    if detail.loading {
                        ProgressView("读取详情…")
                    } else if let error = detail.error {
                        ContentUnavailableView("读取未完成", systemImage: "exclamationmark.circle", description: Text(error))
                    } else {
                        List {
                            if detail.kind == "tool" {
                                toolSections(detail.content)
                            } else {
                                Section {
                                    Text("原件数据完整保留。这里查看的是内容资料，不会重新生成或修改卡片。")
                                        .font(.subheadline).foregroundStyle(.secondary)
                                }
                                if !["anki", "fact", "general", "weather", "news", "images", "videos", "html"].contains(detail.kind) {
                                    Section {
                                        Label("此类型的原生交互尚未迁移", systemImage: "hammer")
                                            .foregroundStyle(.secondary)
                                    }
                                }
                                Section("内容资料") {
                                    // 原件（artifact）的内容是一段 HTML —— 就地渲染，
                                    // 而不是把标签当纯文本摆出来。这是「打开原件」不再
                                    // 掉进旧网页界面之后，原件唯一的落脚处。
                                    if detail.kind == "html", let html = detail.content["html"] as? String, !html.isEmpty {
                                        ReaderNativeRichDocument(content: html, format: "html", imageModel: model)
                                    } else {
                                        ReaderNativeInspectionValue(value: detail.content)
                                    }
                                }
                            }
                        }
                        .scrollContentBackground(.hidden)
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(ReaderNativeTheme.canvas)
            .navigationTitle(model.inspection?.title ?? "详情")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }
                }
            }
        }
        .tint(ReaderNativeTheme.accent)
    }

    @ViewBuilder
    private func toolSections(_ content: [String: Any]) -> some View {
        Section("调用") {
            ForEach(["tool", "label", "status", "model", "call_id", "task_id"], id: \.self) { key in
                if let value = content[key] {
                    LabeledContent(label(key)) {
                        Text(String(describing: value)).textSelection(.enabled)
                    }
                }
            }
        }
        if let args = content["args"] ?? content["arguments"] {
            Section("输入参数") { ReaderNativeInspectionValue(value: args) }
        }
        if let result = content["result"] ?? content["output"] {
            Section("返回结果") { ReaderNativeInspectionValue(value: result) }
        }
        if let error = content["error"] {
            Section("错误") {
                ReaderNativeInspectionValue(value: error).foregroundStyle(.red)
            }
        }
        if let steps = content["steps"] as? [Any], !steps.isEmpty {
            Section("步骤 · \(steps.count)") {
                ForEach(Array(steps.enumerated()), id: \.offset) { index, raw in
                    let step = raw as? [String: Any] ?? [:]
                    DisclosureGroup {
                        ReaderNativeInspectionValue(value: raw)
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("\(index + 1). \(step["label"] as? String ?? step["tool"] as? String ?? "步骤")")
                                .font(.subheadline).fixedSize(horizontal: false, vertical: true)
                            if let status = step["status"] as? String {
                                Text(status).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }
        Section {
            DisclosureGroup("完整记录") { ReaderNativeInspectionValue(value: content) }
        }
    }

    private func label(_ key: String) -> String {
        ["tool": "工具", "label": "任务", "status": "状态", "model": "模型",
         "call_id": "调用编号", "task_id": "任务编号"][key] ?? key
    }
}

@MainActor
private struct ReaderNativeInspectionValue: View {
    let value: Any
    @State private var expanded = false

    private var text: String {
        if let string = value as? String { return string }
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]),
              let string = String(data: data, encoding: .utf8) else { return String(describing: value) }
        return string
    }

    var body: some View {
        let full = text
        VStack(alignment: .leading, spacing: 8) {
            Text(expanded ? full : String(full.prefix(8_000)))
                .font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
            if full.count > 8_000 {
                Button(expanded ? "收起长内容" : "显示完整内容（\(full.count) 字符）") { expanded.toggle() }
                    .font(.caption)
            }
        }
    }
}

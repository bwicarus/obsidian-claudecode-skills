import SwiftUI
import UIKit

enum VoiceLogPrivacy {
    static func clean(_ text: String, limit: Int = 600) -> String {
        var value = text
        for pattern in [
            #"(?i)(?:https?|wss?)://[^\s<>\"']+"#,
            #"(?i)bearer\s+[^\s\"',;}]+"#,
            #"\b(?:sk-|eyJ)[A-Za-z0-9_.-]{15,}"#,
            #"(?i)[\"']?(?:access[_-]?token|refresh[_-]?token|token|api[_-]?key|authorization|password|secret)[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"#
        ] {
            value = value.replacingOccurrences(of: pattern, with: "[已隐藏]", options: .regularExpression)
        }
        return String(value.prefix(limit))
    }
}

struct VoiceDiagnosticEntry: Identifiable {
    let id = UUID()
    let timestamp = Date()
    let category: String
    let message: String
}

struct VoiceToolStep: Identifiable {
    let id: String
    var name: String
    var state: String
    var durationMs: Double?
    var summary: String?
    var error: String?
}

struct VoiceProcess: Identifiable {
    let id: String
    var requestID: String?
    var state = "running"
    var durationMs: Double?
    var error: String?
    var tools: [VoiceToolStep] = []

    var label: String {
        switch state {
        case "completed": return "已完成"
        case "failed", "rejected": return "未完成"
        case "interrupted": return "已中断"
        default: return "处理中"
        }
    }
}

struct VoiceProcessView: View {
    let process: VoiceProcess
    @State private var expanded = false

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(process.tools) { step in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(alignment: .top, spacing: 6) {
                            Image(systemName: icon(step.state))
                                .foregroundStyle(step.state == "failed" ? Color.red : Color.secondary)
                            Text(step.name).fontWeight(.medium)
                            Spacer(minLength: 4)
                            if let ms = step.durationMs { Text(seconds(ms)).foregroundStyle(.secondary) }
                        }
                        if let summary = step.summary, !summary.isEmpty { Text(summary).foregroundStyle(.secondary) }
                        if let error = step.error, !error.isEmpty { Text(error).foregroundStyle(.red) }
                    }
                    .textSelection(.enabled)
                }
                if let error = process.error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
                if process.tools.isEmpty && process.error == nil {
                    Text(process.state == "running" ? "正在准备回答…" : "本轮没有工具调用记录。")
                        .foregroundStyle(.secondary)
                }
            }
            .font(.caption).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 8)
        } label: {
            HStack(spacing: 6) {
                if process.state == "running" { ProgressView().controlSize(.mini) }
                Text("处理过程 · \(process.label)")
                if let ms = process.durationMs { Text(seconds(ms)).foregroundStyle(.secondary) }
            }
            .font(.caption)
        }
        .tint(.secondary)
        .padding(10)
        .background(AppStyle.canvas, in: RoundedRectangle(cornerRadius: 10))
    }

    private func seconds(_ milliseconds: Double) -> String {
        String(format: "%.1f 秒", max(0, milliseconds) / 1000)
    }

    private func icon(_ state: String) -> String {
        switch state {
        case "completed": return "checkmark.circle"
        case "failed": return "exclamationmark.circle"
        case "interrupted": return "pause.circle"
        default: return "ellipsis.circle"
        }
    }
}

struct VoiceDiagnosticsView: View {
    @ObservedObject var voice: VoiceSession
    @Environment(\.dismiss) private var dismiss
    @State private var copied = false

    var body: some View {
        NavigationStack {
            List {
                Section("当前链路") {
                    LabeledContent("构建版本", value: voice.buildVersion)
                    LabeledContent("通话", value: voice.state.rawValue)
                    LabeledContent("WebSocket", value: voice.socketSummary)
                    LabeledContent("音频", value: voice.audioSummary)
                    LabeledContent("音频包", value: "发送 \(voice.sentPackets) · 收到 \(voice.receivedPackets)")
                    Text("收到音频包表示数据到达 App，不代表扬声器已实际播放。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("最近事件") {
                    if voice.diagnostics.isEmpty { Text("暂无记录").foregroundStyle(.secondary) }
                    ForEach(Array(voice.diagnostics.reversed())) { entry in
                        VStack(alignment: .leading, spacing: 4) {
                            Text("[\(entry.category)] \(entry.message)")
                                .font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                            Text(entry.timestamp.formatted(date: .omitted, time: .standard))
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("通话诊断")
            .toolbar {
                ToolbarItemGroup(placement: .topBarLeading) {
                    Button(copied ? "已复制" : "复制") {
                        UIPasteboard.general.string = voice.diagnosticReport
                        copied = true
                    }
                    Button("清空") { voice.clearDiagnostics(); copied = false }
                }
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
        }
    }
}

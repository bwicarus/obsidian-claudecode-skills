import SwiftUI

/// 只做一件事的 sheet：输入 / 替换 OpenAI Key。
///
/// 2026-08-19 从 `NativeReaderToolsView` 的 12-Section 大表里摘出来。状态展示
/// （是否已配置、型号、保存时间）和「清除」都已经搬进网页设置面板的「本机」tab；
/// 留在原生这一侧的**只有输入**，因为这是唯一不能妥协的一条：
///
/// Key 只经 `SecureField` 进 Apple Keychain，**任何形式都不经过 JS** —— 桥的
/// 动作白名单里因此没有 `realtimeSave`，网页能做的只是请求"把这个 sheet 弹出来"。
struct ReaderRealtimeKeyView: View {
    @StateObject private var credentials = ReaderRealtimeCredentialManager.shared
    @State private var draft = ""
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SecureField("输入现有 OpenAI Key（sk-…）", text: $draft)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.system(.body, design: .monospaced))

                    Button {
                        Task {
                            await credentials.saveExistingKey(draft)
                            if credentials.errorMessage == nil,
                               credentials.status.isConfigured {
                                draft = ""
                                dismiss()
                            }
                        }
                    } label: {
                        Label(
                            credentials.isRunning
                                ? "正在保存并验证…"
                                : (credentials.status.isConfigured
                                    ? "替换 App Key"
                                    : "保存 App Key"),
                            systemImage: "key.fill"
                        )
                    }
                    .disabled(
                        credentials.isRunning ||
                        draft.trimmingCharacters(
                            in: .whitespacesAndNewlines
                        ).isEmpty
                    )
                } footer: {
                    Text("Key 只在这个 App 中输入并写入 Apple Keychain；Safari 扩展只由签名的原生进程共享读取。App 保存、启动通话、注入选区与发送笔迹/视口合成图都不连接服务器；Key 也不会进入 Reader 网页、扩展 JavaScript、代码、构建产物或日志。")
                }

                if let notice = credentials.notice {
                    Label(notice, systemImage: "checkmark.circle.fill")
                        .font(.footnote)
                        .foregroundStyle(.green)
                }
                if let error = credentials.errorMessage {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                }
            }
            .navigationTitle("OpenAI Key")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("完成") { dismiss() }
                }
            }
        }
    }
}

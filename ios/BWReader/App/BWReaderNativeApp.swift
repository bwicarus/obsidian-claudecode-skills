import SwiftUI

@main
struct BWReaderNativeApp: App {
    @StateObject private var voiceBridge = NativeVoiceBridge()
    @StateObject private var nativeCommandReceiver =
        ReaderNativeCommandReceiver()

    var body: some Scene {
        WindowGroup {
            ReaderRootView(
                voiceBridge: voiceBridge,
                nativeCommandReceiver: nativeCommandReceiver
            )
        }
    }
}

private struct ReaderRootView: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var reader = ReaderWebViewModel()
    @ObservedObject var voiceBridge: NativeVoiceBridge
    @ObservedObject var nativeCommandReceiver: ReaderNativeCommandReceiver
    @State private var showsDiagnostics = false

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            ReaderWebView(model: reader)
                .ignoresSafeArea(edges: .bottom)

            if reader.isLoading {
                ProgressView()
                    .tint(.white)
                    .padding(12)
                    .background(.ultraThinMaterial, in: Capsule())
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                    .padding(.top, 8)
                    .allowsHitTesting(false)
            }

            if let message = reader.loadError {
                ReaderLoadError(message: message) {
                    reader.reload()
                }
            }

            if let notice = nativeCommandReceiver.notice {
                Text(notice)
                    .font(.footnote)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.top, 10)
                    .onTapGesture {
                        nativeCommandReceiver.dismissNotice()
                    }
                    .frame(
                        maxWidth: .infinity,
                        maxHeight: .infinity,
                        alignment: .top
                    )
            }

            // Hidden, nonvisual support entry: long-press the bottom-left
            // corner for two seconds. It never changes Reader page behavior.
            Color.clear
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
                .onLongPressGesture(minimumDuration: 2) {
                    showsDiagnostics = true
                }
                .accessibilityLabel("打开电脑语音诊断")
                .frame(
                    maxWidth: .infinity,
                    maxHeight: .infinity,
                    alignment: .bottomLeading
                )
                .padding(4)

        }
        .preferredColorScheme(.dark)
        .task {
            voiceBridge.bind(reader: reader)
            nativeCommandReceiver.bind(
                reader: reader,
                voiceBridge: voiceBridge
            )
        }
        .onOpenURL { url in
            nativeCommandReceiver.receive(url)
        }
        .onChange(of: scenePhase, initial: true) { _, phase in
            reader.setReaderForeground(phase == .active)
        }
        .sheet(isPresented: $showsDiagnostics) {
            NativeVoiceDiagnosticsView(bridge: voiceBridge)
        }
    }
}

private struct NativeVoiceDiagnosticsView: View {
    @ObservedObject var bridge: NativeVoiceBridge
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section("当前状态") {
                    LabeledContent("App build", value: nativeAppBuildVersion)
                    LabeledContent("通话", value: bridge.state.title)
                    LabeledContent("WSS", value: bridge.socketState.rawValue)
                    LabeledContent("目标", value: bridge.activeTargetName)
                    LabeledContent("网络", value: bridge.networkSummary)
                    LabeledContent(
                        "麦克风",
                        value: bridge.microphoneMuted ? "已静音" : "工作中"
                    )
                    if let detail = bridge.state.detail {
                        Text(detail)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }

                Section("锁屏控制的等价操作") {
                    Button(
                        bridge.microphoneMuted ? "恢复麦克风" : "静音麦克风"
                    ) {
                        bridge.setMicrophoneMuted(!bridge.microphoneMuted)
                    }
                    .disabled(!bridge.state.isActive)

                    Button("结束电脑语音", role: .destructive) {
                        Task { @MainActor in
                            await bridge.stop()
                            dismiss()
                        }
                    }
                    .disabled(bridge.state.phase == .idle)
                }

                Section("最近协议与生命周期事件") {
                    if bridge.diagnostics.isEmpty {
                        Text("暂无记录")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(Array(bridge.diagnostics.reversed())) { entry in
                            VStack(alignment: .leading, spacing: 4) {
                                Text("[\(entry.category)] \(entry.message)")
                                    .font(.system(.caption, design: .monospaced))
                                    .textSelection(.enabled)
                                Text(entry.timestamp.formatted(
                                    date: .omitted,
                                    time: .standard
                                ))
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .navigationTitle("电脑语音诊断")
            .toolbar {
                ToolbarItemGroup(placement: .topBarLeading) {
                    Button("复制") {
                        UIPasteboard.general.string = bridge.diagnosticReport
                    }
                    Button("清空") {
                        bridge.clearDiagnostics()
                    }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") {
                        dismiss()
                    }
                }
            }
        }
    }
}

private struct ReaderLoadError: View {
    let message: String
    let retry: () -> Void

    var body: some View {
        VStack(spacing: 14) {
            Text("阅读器加载失败")
                .font(.headline)
            Text(message)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Text("请确认 iPad 已连接 Tailscale。")
                .font(.caption)
                .foregroundStyle(.secondary)
            Button("重试", action: retry)
                .buttonStyle(.borderedProminent)
        }
        .padding(28)
        .frame(maxWidth: 360)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18))
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
    }
}

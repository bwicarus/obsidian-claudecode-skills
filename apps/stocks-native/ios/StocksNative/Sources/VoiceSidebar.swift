import SwiftUI

struct VoiceSidebar: View {
    @ObservedObject var voice: VoiceSession
    @ObservedObject var model: AppModel
    @State private var draft = ""
    @State private var sendingText = false
    @State private var showDiagnostics = false

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Image(systemName: "waveform").font(.title3).foregroundStyle(AppStyle.accent)
                    Text("股票助手").font(.headline)
                    Spacer()
                    Button { showDiagnostics.toggle() } label: { Image(systemName: "info.circle").foregroundStyle(.secondary) }
                        .accessibilityLabel("语音连接详情")
                }
                HStack(spacing: 7) {
                    Circle().fill(voice.isConnected ? AppStyle.accent : Color.secondary.opacity(0.5)).frame(width: 6, height: 6)
                    Text(voice.state.rawValue).font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if voice.state == .connecting || voice.state == .preparing { ProgressView().controlSize(.mini) }
                }
                if let error = voice.error {
                    Text(error).font(.caption).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                }
                if model.isPaired && !model.isAIEnabled {
                    Label("审核账号未开放 AI 助手", systemImage: "lock.shield")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let code = voice.stockCode, voice.isStarted {
                    Text("当前股票 · \(code)").font(.caption).foregroundStyle(.secondary)
                }
                Button {
                    Task {
                        if voice.isStarted { await voice.stop() }
                        else {
                            await voice.start(client: model.client, deviceID: model.deviceID, stockCode: model.selectedCode)
                            await model.publishVoiceContext(action: "开始语音对话")
                        }
                    }
                } label: {
                    Label(voice.isStarted ? "结束通话" : "开始语音", systemImage: voice.isStarted ? "stop.fill" : "mic.fill")
                        .font(.subheadline.weight(.medium)).frame(maxWidth: .infinity).padding(.vertical, 6)
                }
                .buttonStyle(.borderedProminent)
                .tint(voice.isStarted ? Color.secondary : AppStyle.accent)
                .disabled(!model.isPaired || !model.isAIEnabled)
                if showDiagnostics {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("上行 \(voice.sentPackets) 包 · 下行 \(voice.receivedPackets) 包")
                        if let sessionID = voice.sessionID { Text("会话 \(sessionID)") }
                        if let threadID = voice.threadID { Text("线程 \(threadID)") }
                        Text("48 kHz · PCM16 · 单声道")
                    }
                    .font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                    .textSelection(.enabled)
                }
            }
            .padding(20)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 20) {
                        if !model.isAIEnabled {
                            VStack(alignment: .leading, spacing: 12) {
                                Text("AI 功能未开放")
                                    .font(.system(.title3, design: .rounded, weight: .medium))
                                    .foregroundStyle(AppStyle.ink)
                                Text("审核账号可验证股票搜索、原生 K 线、成交量和图表标注；AI 助手不会消耗任何额度。")
                                    .font(.subheadline).foregroundStyle(.secondary).lineSpacing(4)
                            }
                            .padding(.top, 16)
                        } else if voice.transcripts.isEmpty {
                            VStack(alignment: .leading, spacing: 12) {
                                Text("边看行情，边聊想法。")
                                    .font(.system(.title3, design: .rounded, weight: .medium))
                                    .foregroundStyle(AppStyle.ink)
                                Text("开始语音后，可以询问当前股票，也可以请助手打开另一只股票。")
                                    .font(.subheadline).foregroundStyle(.secondary).lineSpacing(4)
                            }
                            .padding(.top, 16)
                        }
                        ForEach(voice.transcripts) { message in
                            VStack(alignment: .leading, spacing: 7) {
                                Text(message.role == "user" ? "你" : "助手")
                                    .font(.caption.weight(.medium)).foregroundStyle(.secondary)
                                Text(message.text)
                                    .font(.subheadline).lineSpacing(5).textSelection(.enabled)
                                    .foregroundStyle(AppStyle.ink)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(message.role == "user" ? 12 : 0)
                            .background(message.role == "user" ? AppStyle.canvas : Color.clear,
                                        in: RoundedRectangle(cornerRadius: 12))
                            .id(message.id)
                        }
                        Color.clear.frame(height: 1).id("conversationBottom")
                    }
                    .padding(20)
                }
                .onChange(of: voice.transcripts.last?.text) { _, _ in
                    withAnimation(.easeOut(duration: 0.15)) { proxy.scrollTo("conversationBottom", anchor: .bottom) }
                }
            }
            Divider()
            HStack(alignment: .bottom, spacing: 10) {
                TextField("也可以输入文字…", text: $draft, axis: .vertical)
                    .lineLimit(1...5).font(.subheadline)
                    .disabled(!voice.isConnected || sendingText)
                Button {
                    let text = draft
                    sendingText = true
                    Task {
                        await voice.sendText(text)
                        if voice.isConnected { draft = "" }
                        sendingText = false
                    }
                } label: { Image(systemName: "arrow.up.circle.fill").font(.title2) }
                    .disabled(!voice.isConnected || sendingText || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityLabel("发送消息")
            }
            .padding(18)
        }
        .background(.white)
    }
}

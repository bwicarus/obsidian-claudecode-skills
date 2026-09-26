import SwiftUI

/// 设置 →「旁听与降噪」分页（2026-09-26）：环境旁听、通话多人时的人声隔离、用户声纹登记、诊断。
struct NativeAmbientSettingsSections: View {
    @ObservedObject private var listener = NativeAmbientListener.shared
    @ObservedObject private var gate = NativeNoisyGateMonitor.shared
    @ObservedObject private var log = NativeAmbientLog.shared
    @State private var gateEnabled = NativeNoisyVoiceGate.isEnabled
    @State private var promptIsolation = NativeNoisyVoiceGate.promptsSystemIsolation
    @State private var forceUserOnly = NativeNoisyVoiceGate.forceUserOnly
    /// 打开时间轴的开关由设置页顶层持有：这里是列表里的一行，旁听日志每几秒一刷就重建，
    /// 挂在这里的全屏面板会跟着被关掉（2026-09-27 用户：「点开面板会失败、自动关闭」）。
    @Binding var showTimeline: Bool
    @State private var candidates = Set(NativeSegmentTranscriber.candidateLocales)
    @State private var hasVoiceprint = NativeVoiceprint.exists
    @State private var enrolling = false
    @State private var enrollNote = ""
    @State private var showLog = false
    @State private var people = NativeVoiceprint.people()
    @State private var naming: NativeAmbientPipeline.HeardSpeaker?
    @State private var personName = ""
    @State private var peopleNote = ""

    var body: some View {
        Section {
            Button {
                showTimeline = true
            } label: {
                Label("对话时间轴与人物", systemImage: "person.2.wave.2")
            }
        } footer: {
            Text("多人说话按时间轴分块显示；点块看、改这个人的名字、介绍、AI 整理和对话历史。资料写在 Obsidian 的 KJ 人物页。")
        }
        voiceprintSection
        peopleSection
        ambientSection
        if !listener.feed.isEmpty { feedSection }
        gateSection
        diagnosticsSection
    }

    // MARK: 声纹

    private var voiceprintSection: some View {
        Section {
            LabeledContent("我的声纹", value: hasVoiceprint ? "已登记" : "未登记")
            if enrolling {
                VStack(alignment: .leading, spacing: 6) {
                    ProgressView("正在录音 \(Int(NativeVoiceprint.seconds)) 秒…")
                    Text("请用平常说话的音量朗读：\n「今天天气不错，我在读书，顺便做几张卡片复习一下昨天学过的内容。」")
                        .font(.callout)
                }
            } else {
                Button(hasVoiceprint ? "重新登记声纹" : "登记声纹（朗读 10 秒）") { enroll() }
                if hasVoiceprint {
                    Button("删除声纹", role: .destructive) {
                        NativeVoiceprint.delete()
                        hasVoiceprint = false
                        NativeAmbientLog.note("声纹：已删除")
                    }
                }
            }
            if !enrollNote.isEmpty { Text(enrollNote).font(.caption).foregroundStyle(.secondary) }
        } header: { Text("声纹") } footer: {
            Text("旁听里的「我」、通话降噪里「只放行你的声音」都靠它认人。安静的地方录，只存在本机。")
        }
    }

    private func enroll() {
        guard NativeAudioEngine.activeCallAudio == 0 else {
            enrollNote = "正在通话，麦克风归通话所有；挂断后再登记。"
            return
        }
        enrolling = true
        enrollNote = ""
        Task {
            do {
                try await listener.withMicrophoneReleased {
                    let voiced = try await NativeVoiceprint.record()
                    enrollNote = voiced >= NativeVoiceprint.minimumVoicedSeconds
                        ? String(format: "已保存（有效语音 %.1f 秒）。下次通话 / 旁听重启时生效。", voiced)
                        : String(format: "有效语音只有 %.1f 秒，没保存。请靠近一点、连续地读。", voiced)
                }
            } catch {
                enrollNote = "录音失败：\(error.localizedDescription)"
                NativeAmbientLog.note("声纹登记失败：\(error.localizedDescription)", level: "error")
            }
            hasVoiceprint = NativeVoiceprint.exists
            enrolling = false
        }
    }

    // MARK: 熟人

    private var peopleSection: some View {
        Section {
            ForEach(people) { person in
                LabeledContent(person.name, value: String(format: "%.0f 秒样本", person.seconds))
            }
            .onDelete { offsets in
                for index in offsets { NativeVoiceprint.deletePerson(people[index].id) }
                people = NativeVoiceprint.people()
                listener.peopleChanged()
                NativeAmbientLog.note("熟人声纹：已删除，分离器会重新预登记")
            }
            if listener.heardSpeakers.isEmpty {
                Text(listener.isEnabled ? "旁听到还没名字的人时，会出现在这里供你起名。" : "打开环境旁听后，听到的陌生人会出现在这里供你起名。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(listener.heardSpeakers) { speaker in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(speaker.label) · \(speaker.heardAt.formatted(date: .omitted, time: .shortened))")
                            .font(.caption.bold())
                        Text(speaker.sample).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    }
                    Spacer()
                    Button("起名") { personName = ""; naming = speaker }.buttonStyle(.bordered)
                }
            }
            if !peopleNote.isEmpty { Text(peopleNote).font(.caption).foregroundStyle(.secondary) }
        } header: { Text("熟人") } footer: {
            Text("给常听到的人起名后，旁听转写就写成「小王：……」而不是「说话人2：……」，jev 判断时也知道是谁在说。"
                 + "认人靠声纹特征比对（每个人说够 3 秒就比一次，之后再多说 10 秒复核），熟人数量不限。"
                 + "同名再起一次会追加样本，认得更准。同一时刻在场的人最多分出 4 个（分离模型的上限）。")
        }
        .alert("给\(naming?.label ?? "")起名", isPresented: Binding(get: { naming != nil }, set: { if !$0 { naming = nil } })) {
            TextField("名字（最多 20 字）", text: $personName)
            Button("取消", role: .cancel) { naming = nil }
            Button("保存") {
                guard let speaker = naming else { return }
                naming = nil
                let name = personName
                Task {
                    peopleNote = await listener.namePerson(speaker, name: name)
                    people = NativeVoiceprint.people()
                }
            }
        } message: { Text(naming?.sample ?? "") }
    }

    // MARK: 环境旁听

    private var ambientSection: some View {
        Section {
            Toggle("环境旁听", isOn: Binding(get: { listener.isEnabled }, set: { listener.setEnabled($0) }))
            Picker("我的语言", selection: $listener.locale) {
                ForEach(NativeSegmentTranscriber.allLocales, id: \.self) { code in
                    Text(NativeSegmentTranscriber.displayName(code)).tag(code)
                }
            }
            DisclosureGroup("推测别人语言时的候选") {
                ForEach(NativeSegmentTranscriber.allLocales, id: \.self) { code in
                    Toggle(NativeSegmentTranscriber.displayName(code), isOn: Binding(
                        get: { candidates.contains(code) },
                        set: { on in
                            if on { candidates.insert(code) } else { candidates.remove(code) }
                            NativeSegmentTranscriber.candidateLocales = NativeSegmentTranscriber.allLocales.filter { candidates.contains($0) }
                        }))
                }
            }
            LabeledContent("状态", value: listener.state)
            if listener.isEnabled {
                LabeledContent("最近 15 秒说话人数", value: "\(listener.speakersNow)")
                if !listener.partialText.isEmpty {
                    Text(listener.partialText).font(.caption).foregroundStyle(.secondary).lineLimit(3)
                }
                if !listener.lastJudgment.isEmpty {
                    Text("上一段：" + listener.lastJudgment).font(.caption).foregroundStyle(.secondary)
                }
                if let until = listener.dangerRecordingUntil, until > Date() {
                    Label("危险录音中，到 \(until.formatted(date: .omitted, time: .shortened))", systemImage: "record.circle")
                        .foregroundStyle(.red)
                }
            }
        } header: { Text("环境旁听") } footer: {
            Text("按每个人的开始和结束切成一段一段再转写：你用「我的语言」；人物页登记了语言的人用他的语言；"
                 + "没登记的在候选语言里各试一次、挑最可信的（标「推测」，人物页可一键确认）。候选越多越耗电。"
                 + "持续听周围的声音：本机转写 + 分出不同的人，每段话送到你的服务器由 jev 判断——"
                 + "有没有意义、有没有即时危险（有就持续录音 5 分钟并通知）、有没有要解决的疑问（交 AI 解答）、"
                 + "要不要记录（写进 Obsidian「AI助手专用/环境旁听」，重要的连原声存进「文件 → BWReader → 环境旁听」）。"
                 + "原声不离开设备，只有文字出网。它是独立模块，与语音开没开无关：通话时改接通话的上行继续判断。"
                 + "持续录音与识别比较耗电。")
        }
    }

    private var feedSection: some View {
        Section("服务器的旁听记录") {
            ForEach(Array(listener.feed.suffix(12).reversed().enumerated()), id: \.offset) { _, entry in
                VStack(alignment: .leading, spacing: 2) {
                    Text(Self.kindLabel(entry["kind"] as? String)).font(.caption.bold())
                    Text(entry["text"] as? String ?? "").font(.caption).lineLimit(6).textSelection(.enabled)
                }
            }
            Button("刷新") { Task { await listener.refreshFeed() } }
        }
    }

    private static func kindLabel(_ kind: String?) -> String {
        switch kind {
        case "answer": return "AI 解答"
        case "task": return "待办 / 约定"
        case "question": return "待跟进的疑问"
        case "summary": return "旁听摘要"
        default: return "一段对话"
        }
    }

    // MARK: 通话降噪

    private var gateSection: some View {
        Section {
            Toggle("只响应我的声音", isOn: $forceUserOnly)
                .disabled(!hasVoiceprint)
                .onChange(of: forceUserOnly) { _, value in NativeNoisyVoiceGate.forceUserOnly = value }
            Toggle("多人说话时自动只放行我的声音", isOn: $gateEnabled)
                .onChange(of: gateEnabled) { _, value in NativeNoisyVoiceGate.isEnabled = value }
            Toggle("同时提示开启系统「人声突显」", isOn: $promptIsolation)
                .onChange(of: promptIsolation) { _, value in NativeNoisyVoiceGate.promptsSystemIsolation = value }
            if gate.status.running {
                LabeledContent("本次通话", value: gateStatusText)
            }
        } header: { Text("嘈杂环境人声隔离") } footer: {
            Text("「只响应我的声音」：主动开启，通话一开始就只把你的声音发给 AI（需先登记声纹）。"
                 + "「自动」：只有检测到别人也在说话时才介入，30 秒只剩你一个人就退出。"
                 + "两者耗电一样（通话中分离模型本来就在跑），隔离期间上行多约 0.6 秒延迟；通话中切换立即生效。"
                 + "苹果的「人声突显」只能由你在控制中心切换，开了提示会在第一次进入隔离时把面板弹出来。")
        }
    }

    private var gateStatusText: String {
        let status = gate.status
        if !status.modelReady { return "正在加载分离模型…" }
        let who = status.usesVoiceprint ? "按声纹" : "无声纹"
        return status.engaged ? "隔离中（\(status.speakers) 人，\(who)）" : "监听中（\(status.speakers) 人，\(who)）"
    }

    // MARK: 诊断

    private var diagnosticsSection: some View {
        Section {
            DisclosureGroup("最近的旁听 / 降噪日志（\(log.lines.count)）", isExpanded: $showLog) {
                ForEach(Array(log.lines.suffix(60).reversed().enumerated()), id: \.offset) { _, line in
                    Text(line).font(.caption2.monospaced()).textSelection(.enabled)
                }
            }
        } footer: {
            Text("同样的日志也会送到服务器的 client-log。")
        }
    }
}

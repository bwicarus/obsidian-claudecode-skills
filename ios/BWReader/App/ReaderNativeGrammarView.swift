import SwiftUI

/// 语法分析面板（选区菜单「语法」）。
///
/// ⚠ **只显示**。跑哪些 KG、有没有跟踪中的节点、句子够不够长，全在 `RC.grammar`
/// 那一处（它缓存着 `_enabledBooks` / `_hasTracked`）。复制到这边必然漂移，
/// 表现是「网页说没开 KG、原生却自顾自跑了一次 AI」。
///
/// 网页那侧还画骨架图（依存树）。这里先出**译文 / 语法点 / 分词词性**三段 ——
/// 这三段是用户真正读的东西，骨架图是理解辅助，缺它不影响这一屏能不能用。
@MainActor
final class ReaderNativeGrammarModel: ObservableObject, Identifiable {
    let id = UUID()
    let sentence: String
    let focus: String
    private let request: ([String: Any]) async -> [String: Any]

    @Published private(set) var loading = true
    @Published private(set) var error: String?
    @Published private(set) var zh = ""
    @Published private(set) var points: [Point] = []
    @Published private(set) var tokens: [Token] = []

    struct Point: Identifiable {
        let id = UUID()
        let title: String
        let phrase: String
        let explanation: String
        let examples: [String]
    }

    struct Token: Identifiable {
        let id = UUID()
        let text: String
        let pos: String
    }

    init(sentence: String, focus: String,
         request: @escaping ([String: Any]) async -> [String: Any]) {
        self.sentence = sentence
        self.focus = focus
        self.request = request
    }

    func load() async {
        loading = true
        error = nil
        defer { loading = false }
        let receipt = await request([
            "action": "nativeGrammar",
            "value": ["sentence": sentence, "text": focus],
        ])
        guard receipt["ok"] as? Bool == true, let body = receipt["value"] as? [String: Any] else {
            // 「没启用 KG」「没有跟踪节点」这些是**可操作**的提示，原样显示，
            // 不要折成一句「分析失败」—— 那样用户不知道该去哪开。
            error = receipt["error"] as? String ?? "分析失败，请重试。"
            return
        }
        zh = body["zh"] as? String ?? ""
        points = (body["points"] as? [[String: Any]] ?? []).compactMap { row in
            let title = row["point"] as? String ?? row["node_name"] as? String ?? ""
            let explanation = row["explanation"] as? String ?? ""
            guard !title.isEmpty || !explanation.isEmpty else { return nil }
            return .init(title: title, phrase: row["phrase"] as? String ?? "",
                         explanation: explanation,
                         examples: (row["examples"] as? [Any] ?? []).compactMap { $0 as? String })
        }
        tokens = (body["tokens"] as? [[String: Any]] ?? []).compactMap { row in
            guard let text = row["text"] as? String ?? row["t"] as? String, !text.isEmpty else { return nil }
            return .init(text: text, pos: row["pos"] as? String ?? row["p"] as? String ?? "")
        }
    }
}

struct ReaderNativeGrammarView: View {
    @ObservedObject var model: ReaderNativeGrammarModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text(model.sentence).font(.callout).textSelection(.enabled)
                    if model.loading {
                        // AI 那段要十几秒，说清楚在等什么，不然像卡住了。
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("分析中（译文与语法点由 AI 生成，约十几秒）")
                                .font(.footnote).foregroundStyle(ReaderNativeTheme.muted)
                        }
                    } else if let error = model.error {
                        Label(error, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    }
                    if !model.zh.isEmpty {
                        Divider()
                        Text(model.zh).font(.body).textSelection(.enabled)
                    }
                    if !model.points.isEmpty {
                        Divider()
                        ForEach(model.points) { point in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(point.title).font(.subheadline.weight(.semibold))
                                if !point.phrase.isEmpty {
                                    Text(point.phrase).font(.footnote)
                                        .foregroundStyle(ReaderNativeTheme.accent)
                                }
                                Text(point.explanation).font(.callout)
                                ForEach(Array(point.examples.enumerated()), id: \.offset) { _, example in
                                    Text("· " + example).font(.footnote)
                                        .foregroundStyle(ReaderNativeTheme.muted)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    if !model.tokens.isEmpty {
                        Divider()
                        Text("分词与词性").font(.footnote).foregroundStyle(ReaderNativeTheme.muted)
                        ReaderNativeFlowRow(spacing: 6) {
                            ForEach(model.tokens) { token in
                                VStack(spacing: 1) {
                                    Text(token.text).font(.callout)
                                    if !token.pos.isEmpty {
                                        Text(token.pos).font(.system(size: 9))
                                            .foregroundStyle(ReaderNativeTheme.muted)
                                    }
                                }
                                .padding(.horizontal, 6).padding(.vertical, 3)
                                .background(ReaderNativeTheme.card, in: RoundedRectangle(cornerRadius: 5))
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
            }
            .scrollContentBackground(.hidden)
            .background(ReaderNativeTheme.canvas)
            .navigationTitle("语法")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
            .task { await model.load() }
        }
        .tint(ReaderNativeTheme.accent)
        .presentationDetents([.medium, .large])
    }
}

/// 换行流式布局：一行放不下就换行。SwiftUI 没有现成的，用 Layout 写一个最小实现。
struct ReaderNativeFlowRow: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? 320
        var x: CGFloat = 0, y: CGFloat = 0, lineHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > maxWidth, x > 0 {
                x = 0
                y += lineHeight + spacing
                lineHeight = 0
            }
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
        }
        return CGSize(width: maxWidth, height: y + lineHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, lineHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x + size.width > bounds.maxX, x > bounds.minX {
                x = bounds.minX
                y += lineHeight + spacing
                lineHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
        }
    }
}

import Foundation
import SwiftUI
import WidgetKit

/// 展示板的**手动操作**面（2026-09-05 用户要求的「以及提供手动操作的可能」）。
///
/// 板子的内容由电脑上的 AI / 固定程序写，这里是用户这一侧：看有哪些板子、
/// 谁建的为什么建、**停用**（`enabled` 是用户的开关，AI 不许动）、删掉不要的；
/// v2（同日改版）起每块板子里是一张张卡片 —— 这里同样能看每张卡、**逐张删**。
///
/// 走的是跟小组件同一个桥端点（`/reader-board/v1`，Tailscale 身份闸），
/// 卡片图同样按 sha 从 `/reader-board/card.png` 取 —— 设备端只当显示器，
/// 源头与渲染都在 Windows。
@MainActor
final class ReaderDisplayBoardManager: ObservableObject {
    struct Card: Identifiable, Equatable {
        var id: String
        var alt: String
        var sha: String
        var updatedAtMs: Int64

        var imageURL: URL? {
            // App 里的缩略条一律取方卡；宽卡是小组件少卡时用的。
            URL(string: "https://\(ReaderNativePiGateway.piHost)"
                + "/reader-board/card.png?sha=\(sha)&shape=square")
        }
    }

    struct Board: Identifiable, Equatable {
        var code: String
        var slug: String
        var title: String
        var note: String
        var enabled: Bool
        var updatedAtMs: Int64
        var autoClear: String
        var cards: [Card]

        var id: String { code }
    }

    @Published private(set) var boards: [Board] = []
    @Published private(set) var busy = false
    /// 失败一定要能看见：这块界面没有控制台，静默失败等于"按了没反应"。
    @Published private(set) var failure: String?

    private static let endpoint = URL(
        string: "https://\(ReaderNativePiGateway.piHost)/reader-board/v1"
    )!

    func refresh() async {
        // list 只给板子概要；每块板子的卡片要再 get 一次（最多 24 块，都很小）。
        guard let listed = await send(["op": "list"]) else { return }
        let summaries = (listed["boards"] as? [[String: Any]] ?? [])
        var out: [Board] = []
        for raw in summaries {
            guard var board = Self.decodeBoard(raw) else { continue }
            if let detail = await send(["op": "get", "code": board.code]),
               let full = detail["board"] as? [String: Any] {
                board.cards = (full["cards"] as? [[String: Any]] ?? [])
                    .compactMap(Self.decodeCard)
            }
            out.append(board)
        }
        boards = out
    }

    func setEnabled(_ board: Board, to wanted: Bool) async {
        _ = await send(["op": "enable", "code": board.code, "enabled": wanted])
        await refresh()
        // 停用/启用直接改变小组件该显示什么 —— 立刻刷，别等下一个 15 分钟。
        WidgetCenter.shared.reloadAllTimelines()
    }

    func delete(_ board: Board) async {
        _ = await send(["op": "delete", "code": board.code])
        await refresh()
        WidgetCenter.shared.reloadAllTimelines()
    }

    func clear(_ board: Board) async {
        _ = await send(["op": "clear", "code": board.code])
        await refresh()
        WidgetCenter.shared.reloadAllTimelines()
    }

    /// 每张卡固定的删除键（用户：「固定每张卡片有一个删除按键」）。
    func deleteCard(_ card: Card, in board: Board) async {
        _ = await send(["op": "cardDelete", "code": board.code, "id": card.id])
        await refresh()
        WidgetCenter.shared.reloadAllTimelines()
    }

    private static func decodeCard(_ raw: [String: Any]) -> Card? {
        guard let id = raw["id"] as? String, let sha = raw["sha"] as? String
        else { return nil }
        return Card(
            id: id,
            alt: raw["alt"] as? String ?? "",
            sha: sha,
            updatedAtMs: (raw["updatedAtMs"] as? NSNumber)?.int64Value ?? 0)
    }

    private static func decodeBoard(_ raw: [String: Any]) -> Board? {
        guard let code = raw["code"] as? String,
              let title = raw["title"] as? String else { return nil }
        let rule = raw["autoClear"] as? [String: Any] ?? [:]
        let kind = rule["kind"] as? String ?? "never"
        let describedRule: String
        switch kind {
        case "afterHours":
            let hours = (rule["hours"] as? NSNumber)?.doubleValue ?? 0
            describedRule = "卡片 \(Int(hours)) 小时不更新就撤掉"
        case "dailyAtLocal":
            describedRule = "每天 \(rule["hhmm"] as? String ?? "") 清空"
        default:
            describedRule = "不自动清"
        }
        return Board(
            code: code,
            slug: raw["slug"] as? String ?? "",
            title: title,
            note: raw["note"] as? String ?? "",
            enabled: (raw["enabled"] as? NSNumber)?.boolValue ?? true,
            updatedAtMs: (raw["updatedAtMs"] as? NSNumber)?.int64Value ?? 0,
            autoClear: describedRule,
            cards: [])
    }

    /// 成功回 payload；失败回 nil 并把原因放进 `failure`。
    private func send(_ body: [String: Any]) async -> [String: Any]? {
        busy = true
        defer { busy = false }
        var request = URLRequest(url: Self.endpoint)
        request.httpMethod = "POST"
        request.timeoutInterval = 12
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let payload = (try? JSONSerialization.jsonObject(with: data))
                as? [String: Any] ?? [:]
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard status == 200, payload["ok"] as? Bool == true else {
                // 桥的 detail 说得清是哪个字段、错在哪 —— 原样端到界面上，
                // 别折成"操作失败"（那等于把唯一有用的一句话丢掉）。
                failure = (payload["detail"] as? String)
                    ?? (payload["error"] as? String)
                    ?? "HTTP \(status)"
                return nil
            }
            failure = nil
            return payload
        } catch {
            failure = "连不上电脑（\(error.localizedDescription)）"
            return nil
        }
    }
}

struct ReaderDisplayBoardSection: View {
    @StateObject private var manager = ReaderDisplayBoardManager()
    @State private var loaded = false

    var body: some View {
        Section {
            if manager.boards.isEmpty {
                Text(
                    manager.failure == nil
                        ? "还没有展示板。在给 AI 的任务里说明要用展示板，它会申请一块。"
                        : "暂时读不到展示板。"
                )
                .font(.footnote)
                .foregroundStyle(.secondary)
            }
            ForEach(manager.boards) { board in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text(board.title).font(.body)
                        Spacer(minLength: 8)
                        Toggle("", isOn: Binding(
                            get: { board.enabled },
                            set: { wanted in
                                Task { await manager.setEnabled(board, to: wanted) }
                            }))
                        .labelsHidden()
                    }
                    if !board.note.isEmpty {
                        Text(board.note)
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Text("\(board.cards.count) 张卡 · \(board.autoClear)")
                        .font(.caption2).foregroundStyle(.tertiary)
                    if !board.cards.isEmpty {
                        cardStrip(board)
                    }
                    HStack(spacing: 14) {
                        Button("清空卡片") {
                            Task { await manager.clear(board) }
                        }
                        .font(.caption)
                        Button("删除这块板子", role: .destructive) {
                            Task { await manager.delete(board) }
                        }
                        .font(.caption)
                    }
                }
                .padding(.vertical, 2)
            }
            Button(manager.busy ? "读取中…" : "刷新展示板") {
                Task { await manager.refresh() }
            }
            .disabled(manager.busy)
            if let failure = manager.failure {
                Text(failure)
                    .font(.caption).foregroundStyle(.red)
            }
        } header: {
            Text("展示板")
        } footer: {
            Text("小组件上那块方格板。卡片由电脑上的任务写入并在电脑上渲染；开关只由你决定，AI 不会自己开；每张卡都能单独删。")
        }
        .task {
            guard !loaded else { return }
            loaded = true
            await manager.refresh()
        }
    }

    /// 横向一排卡片缩略图，每张右上角一个删除键 —— 跟小组件上那个是同一个动作。
    private func cardStrip(_ board: ReaderDisplayBoardManager.Board) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(board.cards) { card in
                    ZStack(alignment: .topTrailing) {
                        AsyncImage(url: card.imageURL) { phase in
                            switch phase {
                            case .success(let image):
                                image.resizable().aspectRatio(1, contentMode: .fill)
                            default:
                                ZStack {
                                    Color(uiColor: .secondarySystemBackground)
                                    Text(card.alt.isEmpty ? "渲染中…" : card.alt)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                        .multilineTextAlignment(.center)
                                        .padding(4)
                                }
                            }
                        }
                        .frame(width: 96, height: 96)
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                        Button {
                            Task { await manager.deleteCard(card, in: board) }
                        } label: {
                            Image(systemName: "xmark")
                                .font(.system(size: 9, weight: .bold))
                                .foregroundStyle(.white)
                                .padding(5)
                                .background(Color.black.opacity(0.55), in: Circle())
                        }
                        .buttonStyle(.plain)
                        .padding(4)
                        .accessibilityLabel("删除卡片 \(card.alt)")
                    }
                }
            }
            .padding(.vertical, 2)
        }
    }
}

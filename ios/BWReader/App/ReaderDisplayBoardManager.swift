import Foundation
import SwiftUI
import WidgetKit

/// 展示板的**手动操作**面（2026-09-05 用户要求的「以及提供手动操作的可能」）。
///
/// 板子的内容由电脑上的 AI / 固定程序写，这里是用户这一侧：看有哪些板子、
/// 谁建的为什么建、**停用**（`enabled` 是用户的开关，AI 不许动）、删掉不要的。
///
/// 走的是跟小组件同一个桥端点（`/reader-board/v1`，Tailscale 身份闸），
/// 不经阅读器 runtime —— 这是 App 自己的界面，不该借道网页层。
@MainActor
final class ReaderDisplayBoardManager: ObservableObject {
    struct Board: Identifiable, Equatable {
        var code: String
        var slug: String
        var title: String
        var note: String
        var enabled: Bool
        var updatedAtMs: Int64
        var sectionCount: Int
        var autoClear: String

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
        await send(["op": "list"]) { [weak self] payload in
            let raw = payload["boards"] as? [[String: Any]] ?? []
            self?.boards = raw.compactMap(Self.decode)
        }
    }

    func setEnabled(_ board: Board, to wanted: Bool) async {
        await send(["op": "enable", "code": board.code, "enabled": wanted])
        await refresh()
        // 停用/启用直接改变小组件该显示什么 —— 立刻刷，别等下一个 15 分钟。
        WidgetCenter.shared.reloadAllTimelines()
    }

    func delete(_ board: Board) async {
        await send(["op": "delete", "code": board.code])
        await refresh()
        WidgetCenter.shared.reloadAllTimelines()
    }

    func clear(_ board: Board) async {
        await send(["op": "clear", "code": board.code])
        await refresh()
        WidgetCenter.shared.reloadAllTimelines()
    }

    private static func decode(_ raw: [String: Any]) -> Board? {
        guard let code = raw["code"] as? String,
              let title = raw["title"] as? String else { return nil }
        let rule = raw["autoClear"] as? [String: Any] ?? [:]
        let kind = rule["kind"] as? String ?? "never"
        let describedRule: String
        switch kind {
        case "afterHours":
            let hours = (rule["hours"] as? NSNumber)?.doubleValue ?? 0
            describedRule = "分区 \(Int(hours)) 小时不更新就撤掉"
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
            sectionCount: (raw["sectionCount"] as? NSNumber)?.intValue ?? 0,
            autoClear: describedRule)
    }

    private func send(
        _ body: [String: Any],
        onSuccess: (([String: Any]) -> Void)? = nil
    ) async {
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
                return
            }
            failure = nil
            onSuccess?(payload)
        } catch {
            failure = "连不上电脑（\(error.localizedDescription)）"
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
                VStack(alignment: .leading, spacing: 4) {
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
                    Text("\(board.sectionCount) 个分区 · \(board.autoClear)")
                        .font(.caption2).foregroundStyle(.tertiary)
                    HStack(spacing: 14) {
                        Button("清空内容") {
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
            Text("小组件上那块分区板子。内容由电脑上的任务写入；开关只由你决定，AI 不会自己开。")
        }
        .task {
            guard !loaded else { return }
            loaded = true
            await manager.refresh()
        }
    }
}

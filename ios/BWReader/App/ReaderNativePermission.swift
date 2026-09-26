import SwiftUI

/// App 对话里的权限提升（2026-09-27 用户：「codex 软件中打开的对话有权限，只是 app 对话中的 ai 没有权限，
/// 我希望能有一个提升权限的请求确认后就能开启权限」）。
///
/// 语音核心那条 Codex 线程默认只读；它要越过沙盒（跑会写东西的命令、改文件）时发审批请求，
/// 语音核心挂起那一轮等这里的决定 —— 最多 10 分钟，超时按拒绝。
/// 「本对话都允许」= 这条线程改完整权限（与 Codex 桌面版同档），清空对话后回到只读；随时可收回。
/// 请求经 reader-events 的 `assistant-permission` 推来；面板打开时再 GET 一次，免得漏掉断线期间的。
@MainActor
final class ReaderNativePermissionCenter: ObservableObject {
    struct Request: Identifiable, Equatable {
        let id: String
        let kind: String
        let reason: String
        let command: String
        let grantRoot: String
        let expiresAt: Date
    }

    @Published private(set) var pending: [Request] = []
    @Published private(set) var elevated = false
    @Published private(set) var busy: Set<String> = []
    @Published var error: String?

    /// (路径, 方法, 请求体) → (HTTP 状态, JSON)。由阅读器接上服务器网关。
    var transport: ((String, String, [String: Any]?) async throws -> (Int, [String: Any]))?
    var log: ((String) -> Void)?

    func apply(_ state: [String: Any]) {
        elevated = state["elevated"] as? Bool ?? false
        let rows = state["pending"] as? [[String: Any]] ?? []
        pending = rows.compactMap(Self.request)
        busy.formIntersection(Set(pending.map(\.id)))
    }

    private static func request(_ row: [String: Any]) -> Request? {
        guard let id = row["id"] as? String, !id.isEmpty else { return nil }
        let fallback = Date().timeIntervalSince1970 + 600
        let expires = (row["expiresAt"] as? NSNumber)?.doubleValue ?? fallback
        return Request(id: id,
                       kind: row["kind"] as? String ?? "command",
                       reason: row["reason"] as? String ?? "",
                       command: row["command"] as? String ?? "",
                       grantRoot: row["grantRoot"] as? String ?? "",
                       expiresAt: Date(timeIntervalSince1970: expires))
    }

    func load() async {
        guard let transport else { return }
        do {
            let (status, value) = try await transport("/api/assistant/permission", "GET", nil)
            if status == 200 {
                apply(value)
            } else if status != 503 {
                // 503 = 语音核心没在跑，此时本来就不会有待确认的请求
                log?("权限：读取失败 HTTP \(status)")
            }
        } catch is CancellationError {
        } catch {
            log?("权限：读取失败 \(error.localizedDescription)")
        }
    }

    func decide(_ request: Request, _ decision: String) async {
        guard let transport, !busy.contains(request.id) else { return }
        busy.insert(request.id)
        defer { busy.remove(request.id) }
        do {
            let body: [String: Any] = ["id": request.id, "decision": decision]
            let (status, value) = try await transport("/api/assistant/permission/decide", "POST", body)
            if value["pending"] != nil { apply(value) }
            if status != 200 {
                let detail = (value["msg"] as? String) ?? (value["error"] as? String) ?? "HTTP \(status)"
                error = "权限确认没送到：" + detail
                log?("权限：决定未送达 \(decision) HTTP \(status) \(detail)")
            } else {
                log?("权限：\(decision) \(request.kind)")
            }
        } catch {
            self.error = "权限确认没送到：" + error.localizedDescription
            log?("权限：决定未送达 \(decision) \(error.localizedDescription)")
        }
    }

    func revoke() async {
        guard let transport else { return }
        do {
            let (status, value) = try await transport("/api/assistant/permission/revoke", "POST", [:])
            if status == 200 { apply(value) } else { error = "收回权限没送到（HTTP \(status)）" }
            log?("权限：收回 HTTP \(status)")
        } catch {
            self.error = "收回权限没送到：" + error.localizedDescription
        }
    }
}

/// 对话面板顶部：待确认的权限请求 + 「本对话已开启完整权限」提示条。
struct ReaderNativePermissionBanner: View {
    @ObservedObject var center: ReaderNativePermissionCenter

    var body: some View {
        VStack(spacing: 0) {
            ForEach(center.pending) { request in
                card(request)
                Divider()
            }
            if center.elevated {
                elevatedBar
                Divider()
            }
            if let error = center.error {
                errorBar(error)
                Divider()
            }
        }
        .task { await center.load() }
    }

    private func title(_ request: ReaderNativePermissionCenter.Request) -> String {
        switch request.kind {
        case "files": return "AI 请求修改文件"
        case "permissions": return "AI 请求额外权限"
        default: return "AI 请求执行命令"
        }
    }

    private func card(_ request: ReaderNativePermissionCenter.Request) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title(request), systemImage: "lock.open")
                .font(.subheadline.weight(.semibold))
            if !request.reason.isEmpty {
                Text(request.reason).font(.callout)
            }
            if !request.command.isEmpty || !request.grantRoot.isEmpty {
                Text(request.command.isEmpty ? request.grantRoot : request.command)
                    .font(.caption.monospaced())
                    .lineLimit(5)
                    .textSelection(.enabled)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(ReaderNativeTheme.card, in: RoundedRectangle(cornerRadius: 8))
            }
            buttons(request)
            HStack(spacing: 3) {
                Text("不确认的话")
                Text(request.expiresAt, style: .timer)
                Text("后自动拒绝")
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
        .padding(12)
        .background(ReaderNativeTheme.accentWash)
    }

    private func buttons(_ request: ReaderNativePermissionCenter.Request) -> some View {
        HStack(spacing: 8) {
            Button("允许这一次") { Task { await center.decide(request, "once") } }
                .buttonStyle(.borderedProminent)
            Button(request.kind == "permissions" ? "本对话都给" : "本对话都允许") {
                Task { await center.decide(request, "session") }
            }
            .buttonStyle(.bordered)
            Spacer(minLength: 0)
            Button("拒绝", role: .destructive) { Task { await center.decide(request, "deny") } }
                .buttonStyle(.bordered)
        }
        .font(.caption)
        .disabled(center.busy.contains(request.id))
    }

    private var elevatedBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "lock.open.fill").foregroundStyle(.orange)
            Text("本对话已开启完整权限（清空对话后恢复只读）").font(.caption)
            Spacer(minLength: 0)
            Button("收回") { Task { await center.revoke() } }.font(.caption)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private func errorBar(_ error: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.circle").foregroundStyle(.red)
            Text(error).font(.caption).frame(maxWidth: .infinity, alignment: .leading)
            Button { center.error = nil } label: { Image(systemName: "xmark") }
                .font(.caption)
                .accessibilityLabel("关闭提示")
        }
        .padding(12)
    }
}

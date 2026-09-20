import Foundation

/// Images use a separate bounded HTTP request, never the realtime audio socket.
@MainActor
final class VoiceInkUpload {
    var onStatus: ((String?) -> Void)?
    private var latest: (data: Data, code: String, scope: String)?
    private var connection: (client: APIClient, device: String, session: String)?
    private var task: Task<Void, Never>?
    private var generation = UUID()
    private var acknowledged: Data?
    private var activeCode: String?
    private var activeScope: String?
    private var sequence = 0

    func update(_ data: Data, code: String, scope: String) {
        guard data.count <= 340_000, code == activeCode, scope == activeScope else { return }
        guard var object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        sequence += 1
        object["sequence"] = sequence
        guard let sequenced = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
        latest = (sequenced, code, scope)
        flush()
    }

    func context(code: String?, scope: String?) {
        activeCode = code
        activeScope = scope
        if let latest, latest.code != code || latest.scope != scope {
            self.latest = nil
            cancel()
            onStatus?(nil)
        }
    }

    func connect(client: APIClient, device: String, session: String) {
        cancel()
        connection = (client, device, session)
        acknowledged = nil
        flush()
    }

    func disconnect() {
        connection = nil
        cancel()
        acknowledged = nil
    }

    private func cancel() {
        generation = UUID()
        task?.cancel()
        task = nil
    }

    private func flush() {
        guard task == nil, let latest, let connection, latest.data != acknowledged else { return }
        let current = generation
        onStatus?("笔迹已保存，正在同步")
        task = Task { [weak self] in
            var success = false
            for attempt in 0..<2 {
                if attempt > 0 { try? await Task.sleep(nanoseconds: 1_000_000_000) }
                guard !Task.isCancelled, let self, self.generation == current else { return }
                do {
                    var request = URLRequest(url: connection.client.baseURL.appendingPathComponent("api/voice/ink"))
                    request.httpMethod = "POST"
                    request.timeoutInterval = 8
                    request.httpBody = latest.data
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.setValue("Bearer \(connection.client.token ?? "")", forHTTPHeaderField: "Authorization")
                    request.setValue(connection.device, forHTTPHeaderField: "X-Device-ID")
                    request.setValue(connection.session, forHTTPHeaderField: "X-Voice-Session")
                    let (body, response) = try await URLSession.shared.data(for: request)
                    guard self.generation == current, !Task.isCancelled else { return }
                    guard (response as? HTTPURLResponse)?.statusCode == 200,
                          let receipt = try JSONSerialization.jsonObject(with: body) as? [String: Any],
                          let status = receipt["status"] as? String,
                          ["stored", "cleared"].contains(status) else { break }
                    self.acknowledged = latest.data
                    self.onStatus?(status == "cleared" ? nil : "笔迹已备好，提问时带入")
                    success = true
                    break
                } catch { if Task.isCancelled { return } }
            }
            guard let self, self.generation == current else { return }
            self.task = nil
            if !success { self.onStatus?("笔迹已保存在本机，暂未同步") }
            if self.latest?.data != latest.data { self.flush() }
        }
    }
}

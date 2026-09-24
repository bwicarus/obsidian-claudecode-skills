import Foundation
import CoreFoundation

/// Mutable live conversation state. The server still owns durable history;
/// Swift owns live drafts, invocation identity and reconciliation. Web adapters
/// receive committed projections solely to retain unmigrated action handles.
struct ReaderNativeTurnStore {
    typealias O = [String:Any]
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    struct Turn {
        var id: String
        var parts: [O] = []
        var drafts: [String:String] = [:]
        var currentDraft: String?
        var title = ""
        var status: O = ["text":"","done":true]
        var progress: O?
        var meta: O?
        var taskID: String?
        var orchestratorID: String?
        var cliPart: String?
        var live = false
        var final = false
        var streamVersion = 0
        var historyReplay = false
    }
    private(set) var turns: [String:Turn] = [:]
    private(set) var revision = 0
    private var streamVersion = 0
    private var current: String?

    func historyPayload(tid: String, mode: String, file: String, page: Int, absorb: [String]) throws -> O? {
        guard ["normal","review"].contains(mode), file.utf16.count <= 8192, page >= 0,
              absorb.count <= 256, absorb.allSatisfy({ !$0.isEmpty && $0.utf16.count <= 2048 }) else { throw Failure(message:"轮次保存参数无效") }
        let id = lookup(tid)
        guard let turn = turns[id] else { return nil }
        let parts = turn.parts.filter { $0["_streamDraft"] as? Bool != true && $0["_nativeID"] as? String != turn.currentDraft }.map { p -> O in
            var value = publicPart(p); if value["origin"] == nil { value["origin"] = "app" }; return value
        }
        if parts.isEmpty && absorb.isEmpty { return nil }
        var body: O = ["assistant":parts.filter { $0["kind"] as? String == "text" }.compactMap { $0["text"] as? String }.joined(separator:"\n\n"),
                       "parts":parts,"turn_id":tid,"via":"voice","upsert_only":1,"create_if_missing":1,
                       "assistant_mode":mode,"file":file,"page":page]
        if !absorb.isEmpty { body["absorb"] = absorb }
        return body
    }

    /// A completed realtime response carries its user utterance and recording
    /// along with the same committed parts. Never accept a caller's stale parts.
    func voicePayload(tid: String, metadata: O) throws -> O {
        guard let mode = metadata["assistant_mode"] as? String, ["normal","review"].contains(mode),
              let file = metadata["file"] as? String, file.utf16.count <= 8192,
              let page = metadata["page"] as? NSNumber, CFGetTypeID(page) != CFBooleanGetTypeID(),
              page.doubleValue >= 0, page.doubleValue <= 10_000_000, page.doubleValue.rounded() == page.doubleValue,
              let user = metadata["user"] as? String, let assistant = metadata["assistant"] as? String,
              user.utf16.count <= 100_000, assistant.utf16.count <= 2_000_000,
              !user.isEmpty || !assistant.isEmpty else { throw Failure(message:"语音轮次保存参数无效") }
        var body: O = ["user":user,"assistant":assistant,"file":file,"page":page,"via":"voice","assistant_mode":mode]
        if let clip = metadata["clip"] {
            guard let value = clip as? String, value.utf16.count <= 2048 else { throw Failure(message:"录音编号无效") }
            body["clip"] = value
        }
        if !tid.isEmpty, let history = try historyPayload(tid:tid,mode:mode,file:file,page:page.intValue,absorb:[]) {
            body["parts"] = history["parts"]; body["turn_id"] = history["turn_id"]
        }
        return body
    }

    mutating func apply(_ command: O) throws -> O {
        guard JSONSerialization.isValidJSONObject(command),
              try JSONSerialization.data(withJSONObject:command).count <= 8 * 1024 * 1024,
              let action = command["action"] as? String else { throw Failure(message:"轮次更新参数无效") }
        // A rejected command must not partially alter a draft or alias.
        var next = self
        let result = try next.mutate(action, command)
        self = next
        return result
    }

    private func string(_ raw: Any?, limit: Int = 2048) throws -> String {
        guard let value = raw as? String, !value.isEmpty, value.utf16.count <= limit else { throw Failure(message:"轮次身份无效") }
        return value
    }
    private func lookup(_ id: String) -> String {
        if turns[id] != nil { return id }
        return turns.first { $0.value.meta?["turnId"] as? String == id }?.key ?? id
    }
    private func part(_ raw: Any, origin: String? = nil) throws -> O {
        guard let value = raw as? O, let kind = value["kind"] as? String, !kind.isEmpty,
              kind.utf16.count <= 80 else { throw Failure(message:"消息部件无效") }
        var result = value.filter { !$0.key.hasPrefix("_") }
        if let sequence = result["seq"] {
            guard let n = sequence as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(),
                  n.doubleValue.isFinite, n.doubleValue.rounded() == n.doubleValue,
                  n.doubleValue >= 0, n.doubleValue <= 9_007_199_254_000_000 else {
                throw Failure(message:"消息部件序号无效")
            }
        }
        if let origin, result["origin"] == nil { result["origin"] = origin }
        result["_nativeID"] = UUID().uuidString
        return result
    }
    private func identity(_ p: O, index: Int) -> String {
        let kind = p["kind"] as? String ?? "", origin = p["origin"] as? String ?? "runner"
        func first(_ keys: [String]) -> String? {
            keys.compactMap { key -> String? in
                if let s = p[key] as? String, !s.isEmpty { return s }
                return nil
            }.first
        }
        if kind == "tool", let id = first(["call_id","callId","item_id","id"]) { return origin + ":tool:" + id }
        if let id = first(["item_id","id"]) { return origin + ":" + kind + ":" + id }
        if let id = first(["gid","cid"]) ?? ((p["card"] as? O)?["gid"] as? String) ?? ((p["card"] as? O)?["cid"] as? String) { return kind + ":entity:" + id }
        return kind + ":" + origin + ":" + String((p["seq"] as? NSNumber)?.intValue ?? index)
    }
    private func publicPart(_ value: O) -> O { value.filter { !$0.key.hasPrefix("_") } }
    private func draftKey(item: String, origin: String, role: String) -> String { origin + ":" + role + ":" + (item.isEmpty ? "legacy" : item) }
    private func stream(_ turn: inout Turn) {
        turn.live = true; turn.streamVersion = streamVersion + 1
    }
    private func append(_ value: O, to turn: inout Turn) {
        var p = value
        let next = (turn.parts.compactMap { ($0["seq"] as? NSNumber)?.intValue }.max() ?? -1) + 1
        let sequence = (p["seq"] as? NSNumber)?.intValue ?? next
        p["seq"] = sequence
        if p["kind"] as? String == "cards", (p["gid"] as? String ?? "").range(of:"^card_[a-f0-9]{4,64}$",options:.regularExpression) == nil {
            p["gid"] = Self.cardID(turn.id + ":" + String(sequence))
        }
        if p["kind"] as? String == "card", var card = p["card"] as? O, (card["cid"] as? String ?? "").isEmpty {
            card["cid"] = "tc_" + turn.id + "_" + String(sequence); p["card"] = card
        }
        if p["kind"] as? String == "tool" { turn.title = p["label"] as? String ?? p["tool"] as? String ?? "工具" }
        turn.parts.append(p)
    }
    private func absorb(_ incoming: O, into turn: inout Turn) -> Bool {
        guard incoming["kind"] as? String == "tool", let key = (incoming["tool"] as? String) ?? (incoming["label"] as? String), !key.isEmpty else { return false }
        func callID(_ p: O) -> String { ["call_id","callId","item_id","id"].compactMap { p[$0] as? String }.first(where:{ !$0.isEmpty }) ?? "" }
        func rich(_ p: O) -> Int { (p["result"] != nil && !(p["result"] is NSNull) ? 2 : 0) + (p["args"] != nil && !(p["args"] is NSNull) ? 1 : 0) }
        for index in turn.parts.indices.reversed() {
            var p = turn.parts[index]
            guard p["kind"] as? String == "tool", (p["tool"] as? String ?? p["label"] as? String) == key else { continue }
            let a = callID(p), b = callID(incoming)
            var replace = false
            if !a.isEmpty && !b.isEmpty {
                if let x = p["origin"] as? String, let y = incoming["origin"] as? String, x != y { continue }
                guard a == b else { continue }
                let terminal = ["completed","done","failed","error","cancelled"].contains(p["status"] as? String ?? "")
                replace = !(terminal && ["running","started","in_progress"].contains(incoming["status"] as? String ?? ""))
            } else {
                if rich(p) != 0 && rich(incoming) != 0 { continue }
                replace = rich(incoming) > rich(p)
            }
            if replace {
                for (key,value) in incoming where key != "seq" && !key.hasPrefix("_") { p[key] = value }
                turn.parts[index] = p
                turn.title = p["label"] as? String ?? p["tool"] as? String ?? "工具"
            }
            return true
        }
        return false
    }
    private func draft(_ text: String, item: String, origin: String, role: String, turn: inout Turn) {
        let key = draftKey(item:item,origin:origin,role:role)
        var target = turn.drafts[key]
        if target == nil {
            if !item.isEmpty {
                target = turn.parts.first { $0["kind"] as? String == "text" && $0["item_id"] as? String == item && ($0["origin"] as? String ?? "runner") == origin && ($0["role"] as? String ?? "assistant") == role }?["_nativeID"] as? String
            } else { target = turn.currentDraft }
        }
        if target == nil {
            target = UUID().uuidString
            var p: O = ["_nativeID":target!,"kind":"text","role":role,"origin":origin,"text":"","_streamText":true]
            if !item.isEmpty { p["item_id"] = item }
            append(p,to:&turn)
        }
        guard let index = turn.parts.firstIndex(where:{ $0["_nativeID"] as? String == target }) else { return }
        turn.parts[index]["text"] = text; turn.parts[index]["_streamDraft"] = true; turn.parts[index]["_draftKey"] = key
        turn.drafts[key] = target; turn.currentDraft = target; turn.final = false
        stream(&turn)
    }
    private func freeze(_ id: String?, turn: inout Turn) {
        guard let id, let index = turn.parts.firstIndex(where:{ $0["_nativeID"] as? String == id }) else { return }
        if (turn.parts[index]["text"] as? String ?? "").trimmingCharacters(in:.whitespacesAndNewlines).isEmpty { turn.parts.remove(at:index) }
        else { turn.parts[index]["_streamDraft"] = false }
        turn.drafts = turn.drafts.filter { $0.value != id }
        if turn.currentDraft == id { turn.currentDraft = nil }
    }
    private mutating func mutate(_ action: String, _ command: O) throws -> O {
        var changed = Set<String>(), removed: [String] = []
        if action == "reset" {
            removed = Array(turns.keys); turns = [:]; current = nil
        } else {
            let requested = try string(command["tid"]), id = lookup(requested)
            var turn = turns[id] ?? Turn(id:id)
            if turns[id] == nil && turns.count >= 10_000 { throw Failure(message:"对话轮次过多，请切换会话") }
            switch action {
            case "open":
                if turns[id] == nil {
                    turn.meta = command["meta"] as? O; turn.historyReplay = command["historyReplay"] as? Bool == true
                }
                current = id
            case "append":
                guard let raw = command["part"] else { throw Failure(message:"消息部件缺失") }
                let p = try part(raw)
                stream(&turn)
                if !absorb(p,into:&turn) {
                    if p["kind"] as? String == "hlcard", let file = p["file"] as? String,
                       let index = turn.parts.lastIndex(where:{ $0["kind"] as? String == "hlcard" && $0["file"] as? String == file }) {
                        turn.parts[index]["items"] = (turn.parts[index]["items"] as? [Any] ?? []) + (p["items"] as? [Any] ?? [])
                    } else { append(p,to:&turn) }
                }
            case "draft":
                guard let text = command["text"] as? String else { throw Failure(message:"对话草稿缺少正文") }
                draft(text,item:command["itemId"] as? String ?? "",origin:command["origin"] as? String ?? "app",role:command["role"] as? String ?? "assistant",turn:&turn)
            case "freeze":
                let draftID: String?
                if let item = command["itemId"] as? String { draftID = turn.drafts[draftKey(item:item,origin:command["origin"] as? String ?? "app",role:command["role"] as? String ?? "assistant")] }
                else { draftID = turn.currentDraft }
                freeze(draftID,turn:&turn)
            case "status": turn.status = ["text":command["text"] as? String ?? "","done":command["done"] as? Bool == true]
            case "idle": turn.status = ["text":"","done":true]
            case "title": turn.title = command["text"] as? String ?? ""
            case "busy": turn.title = command["text"] as? String ?? ""; turn.status = ["text":"处理中","done":false]
            case "task":
                turn.taskID = command["taskId"] as? String
                if let index = turn.parts.firstIndex(where:{ $0["_nativeID"] as? String == turn.cliPart }) { turn.parts[index]["task_id"] = turn.taskID }
            case "orchestrator": turn.orchestratorID = command["taskId"] as? String
            case "cli":
                guard let p = command["part"] as? O else { throw Failure(message:"任务进度缺失") }
                turn.title = p["label"] as? String ?? "任务"
                if turn.cliPart == nil {
                    turn.cliPart = UUID().uuidString
                    append(["_nativeID":turn.cliPart!,"kind":"tool","tool":"do_task","label":"","steps":[]],to:&turn)
                }
                if let index = turn.parts.firstIndex(where:{ $0["_nativeID"] as? String == turn.cliPart }) {
                    for key in ["label","steps","error"] { if let value = p[key] { turn.parts[index][key] = value } }
                    if let task = turn.taskID { turn.parts[index]["task_id"] = task }
                }
            case "progress":
                guard let event = command["event"] as? O else { throw Failure(message:"流程进度缺失") }
                var p = turn.progress ?? ["states":[]]
                var states = (p["states"] as? [Any] ?? []).map { $0 as? String }
                for key in ["skill","label"] { if let value = event[key] { p[key] = value } }
                let status = event["status"] as? String ?? "", value = status == "error" ? "err" : status == "done" ? "done" : "run"
                if let n = event["total"] as? NSNumber, n.doubleValue > 0 {
                    guard n.doubleValue.rounded() == n.doubleValue, n.intValue <= 10_000 else { throw Failure(message:"流程步数无效") }
                    if (p["total"] as? NSNumber)?.intValue != n.intValue { states = Array(repeating:nil,count:n.intValue); p["total"] = n.intValue }
                    let index = max(0,min(n.intValue,(event["step"] as? NSNumber)?.intValue ?? 1) - 1)
                    states[index] = value
                } else if status == "running" { states.append("run") }
                else if states.isEmpty { states.append(status == "error" ? "err" : "done") }
                else { states[states.count-1] = status == "error" ? "err" : "done" }
                p["states"] = states.map { $0 as Any? ?? NSNull() }; turn.progress = p
            case "import":
                guard let parts = command["parts"] as? [O] else { throw Failure(message:"历史轮次内容无效") }
                for raw in parts { let p = try part(raw); if !absorb(p,into:&turn) { append(p,to:&turn) } }
            case "reconcile": try reconcile(command,turn:&turn)
            case "operationState":
                guard let updates = command["parts"] as? [O] else { throw Failure(message:"操作状态无效") }
                for update in updates {
                    guard let index = turn.parts.firstIndex(where:{ $0["_nativeID"] as? String == update["id"] as? String }),
                          turn.parts[index]["kind"] as? String == "hlcard", let items = update["items"] as? [O] else { throw Failure(message:"操作记录已改变") }
                    // Only disposition may change here; anchors/operations remain owned by the committed part.
                    var currentItems = turn.parts[index]["items"] as? [O] ?? []
                    for value in items {
                        guard let i = value["index"] as? Int, currentItems.indices.contains(i) else { throw Failure(message:"操作记录位置已改变") }
                        currentItems[i]["undone"] = value["undone"] as? Bool == true
                        currentItems[i]["gone"] = value["gone"] as? Bool == true
                    }
                    turn.parts[index]["items"] = currentItems
                }
            case "rename":
                let target = try string(command["newTid"])
                if target != id {
                    if var existing = turns[target] {
                        for p in turn.parts {
                            if p["kind"] as? String == "text", let item = p["item_id"] as? String,
                               existing.parts.contains(where:{ $0["kind"] as? String == "text" && $0["item_id"] as? String == item && ($0["origin"] as? String ?? "runner") == (p["origin"] as? String ?? "runner") && ($0["role"] as? String ?? "assistant") == (p["role"] as? String ?? "assistant") }) { continue }
                            let partID = p["_nativeID"] as? String
                            if partID == turn.currentDraft && existing.currentDraft != nil { continue }
                            if !absorb(p,into:&existing) {
                                append(p,to:&existing)
                                if partID == turn.currentDraft { existing.currentDraft = partID }
                                if p["_streamDraft"] as? Bool == true, let key = p["_draftKey"] as? String { existing.drafts[key] = partID }
                            }
                        }
                        existing.live = existing.live || turn.live; existing.streamVersion = max(existing.streamVersion,turn.streamVersion)
                        if existing.currentDraft != nil { existing.final = false }
                        turn = existing
                    } else { turn.id = target }
                    removed.append(id); turns.removeValue(forKey:id)
                    if current == id { current = target }
                }
            case "drop": removed.append(id); turns.removeValue(forKey:id); if current == id { current = nil }
            default: throw Failure(message:"未知轮次操作")
            }
            if action != "drop" {
                guard turn.parts.count <= 10_000 else { throw Failure(message:"轮次内容过多") }
                streamVersion = max(streamVersion,turn.streamVersion)
                turns[turn.id] = turn; changed.insert(turn.id)
            }
        }
        revision += 1
        return ["revision":revision,"streamVersion":streamVersion,"current":current as Any? ?? NSNull(),
                "removed":removed,"turns":changed.sorted().compactMap { turns[$0].map(projection) }]
    }

    private func reconcile(_ command: O, turn: inout Turn) throws {
        guard let message = command["message"] as? O else { throw Failure(message:"历史消息缺失") }
        let options = command["options"] as? O ?? [:], final = options["final"] as? Bool == true
        let role = message["role"] as? String ?? "assistant", origin = message["origin"] as? String ?? options["origin"] as? String ?? "runner"
        let itemID = message["item_id"] as? String ?? "", tid = command["tid"] as? String ?? turn.id
        turn.meta = ["via":message["via"] ?? turn.meta?["via"] ?? "","threadId":message["thread_id"] ?? turn.meta?["threadId"] ?? "","turnId":tid]
        let incoming = message["parts"] as? [O] ?? []
        let textParts = incoming.filter { $0["kind"] as? String == "text" }
        var legacy = turn.parts.first { $0["kind"] as? String == "text" && $0["_streamText"] as? Bool == true && $0["item_id"] == nil }?["_nativeID"] as? String
        if role == "user" || incoming.isEmpty || (legacy != nil && !textParts.contains(where:{ $0["item_id"] != nil || $0["id"] != nil })) {
            if legacy == nil && incoming.isEmpty { legacy = turn.parts.first { $0["kind"] as? String == "text" && $0["item_id"] == nil }?["_nativeID"] as? String }
            if final || turn.drafts[draftKey(item:itemID,origin:origin,role:role)] == nil {
                if legacy != nil && itemID.isEmpty { turn.currentDraft = legacy }
                let raw = message["content"] as? String ?? ""
                draft(raw.isEmpty ? textParts.compactMap { $0["text"] as? String }.joined(separator:"\n\n") : raw,item:itemID,origin:origin,role:role,turn:&turn)
                if final { freeze(turn.drafts[draftKey(item:itemID,origin:origin,role:role)],turn:&turn) }
            }
        }
        for (index,raw) in incoming.enumerated() {
            if raw["kind"] as? String == "text", legacy != nil, raw["item_id"] == nil, raw["id"] == nil { continue }
            let p = try part(raw,origin:"runner")
            if absorb(p,into:&turn) { continue }
            if let position = turn.parts.indices.first(where:{ identity(turn.parts[$0],index:$0) == identity(p,index:index) }) {
                if p["kind"] as? String == "text" {
                    let completes = final && (itemID.isEmpty || p["item_id"] as? String == itemID || p["id"] as? String == itemID)
                    if turn.parts[position]["_streamDraft"] as? Bool != true || completes { turn.parts[position]["text"] = p["text"] as? String ?? "" }
                    turn.parts[position]["origin"] = p["origin"]
                    if completes { freeze(turn.parts[position]["_nativeID"] as? String,turn:&turn) }
                }
            } else { append(p,to:&turn) }
        }
        if final {
            if !itemID.isEmpty { freeze(turn.drafts[draftKey(item:itemID,origin:origin,role:role)],turn:&turn) }
            else { for id in Array(turn.drafts.values) { freeze(id,turn:&turn) }; freeze(turn.currentDraft,turn:&turn) }
            turn.status = ["text":"","done":true]; turn.final = turn.drafts.isEmpty
        }
        stream(&turn)
    }
    private func projection(_ t: Turn) -> O {
        let presentationParts = t.parts.map { p -> O in var v = publicPart(p); v["streaming"] = p["_streamDraft"] as? Bool == true; return v }
        let presentation: O = ["contract":"reader-turn-presentation/1","tid":t.id,
            "role":t.parts.contains { $0["kind"] as? String == "text" && $0["role"] as? String == "user" } ? "user" : "assistant",
            "parts":presentationParts,"title":t.title,"status":t.status,"progress":t.progress as Any? ?? NSNull(),
            "streaming":!t.drafts.isEmpty || (!(t.status["text"] as? String ?? "").isEmpty && t.status["done"] as? Bool != true)]
        return ["id":t.id,"parts":t.parts,"drafts":t.drafts,"draft":t.currentDraft as Any? ?? NSNull(),"cliPart":t.cliPart as Any? ?? NSNull(),
                "title":t.title,"status":t.status,"progress":t.progress as Any? ?? NSNull(),"meta":t.meta as Any? ?? NSNull(),
                "taskId":t.taskID as Any? ?? NSNull(),"orchTaskId":t.orchestratorID as Any? ?? NSNull(),"live":t.live,"final":t.final,
                "streamVersion":t.streamVersion,"historyReplay":t.historyReplay,"presentation":presentation,
                "persistedParts":t.parts.filter { $0["_streamDraft"] as? Bool != true && $0["_nativeID"] as? String != t.currentDraft }.map(publicPart)]
    }
    static func cardID(_ seed: String) -> String {
        var result = "card_"
        for lane in 0..<4 {
            var hash = UInt32(2_166_136_261) ^ (UInt32(lane) &* 2_654_435_761)
            for unit in seed.utf16 { hash = (hash ^ UInt32(unit)) &* 16_777_619 }
            result += String(format:"%08x",hash)
        }
        return result
    }
}

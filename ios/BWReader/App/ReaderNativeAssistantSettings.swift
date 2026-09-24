import Foundation

@MainActor
final class ReaderNativeAssistantSettings {
    typealias O = [String:Any]
    private var identity = ""
    private var models: O?
    private var voice: O?

    func perform(_ command: O, scope: String, gateway: ReaderNativeServerGateway,
                 surface: ReaderNativeInterfaceSurface, raw: (String) throws -> String?,
                 write: (String,String) async throws -> Void, refreshVoice: () async throws -> Void,
                 isCurrent: () -> Bool) async throws -> O {
        let epoch = gateway.contextRevision, key = scope + ":" + String(epoch)
        if identity != key { identity = key; models = nil; voice = nil }
        func current() throws {
            try Task.checkCancellation()
            guard identity == key, gateway.contextRevision == epoch, isCurrent() else { throw CancellationError() }
        }
        func request(_ path: String, body: O? = nil) async throws -> O {
            try current()
            let data = try body.map { try JSONSerialization.data(withJSONObject:$0) } ?? Data()
            let response = try await gateway.fetchData(path:path,method:body == nil ? "GET" : "POST",body:data,surface:surface)
            try current()
            guard (200..<300).contains(response.status), let result = try JSONSerialization.jsonObject(with:response.data) as? O,
                  result["ok"] as? Bool == true else { throw ReaderNativePreferences.Failure(message:"设置未获服务器确认，请重新读取核对") }
            return result
        }
        func fields(_ cfg: O) throws -> [O] {
            var saved: [String:String] = [:]
            for spec in ReaderNativeVoiceSettings.deviceSpecifications { saved[spec[0]] = try raw(spec[0]) }
            return ReaderNativeVoiceSettings.fields(cfg,device:saved)
        }
        try current()
        let section = command["section"] as? String ?? ""
        if command["action"] as? String == "settingsRead" {
            switch section {
            case "models": let value = try await request("/api/assistant/action-prefs"); models = value; return value
            case "profiles": return try await request("/api/assistant/pref-profiles")
            case "voice":
                let value = try await request("/api/assistant/voice-config")
                guard let cfg = value["cfg"] as? O else { throw ReaderNativePreferences.Failure(message:"语音配置格式无效") }
                voice = cfg; return ["cfg":cfg,"fields":try fields(cfg)]
            default: throw ReaderNativePreferences.Failure(message:"未知设置分类")
            }
        }
        switch section {
        case "models":
            guard let input = command["value"] as? O, let models else { throw ReaderNativePreferences.Failure(message:"请先读取当前模型配置") }
            let body = try Self.modelWrite(input,current:models)
            let result = try await request("/api/assistant/action-pref",body:body)
            if !(body["backend"] as? String ?? "").isEmpty, result["pref"] == nil || result["pref"] is NSNull {
                throw ReaderNativePreferences.Failure(message:"该模型组合未保存，请重新读取可用型号")
            }
            return result
        case "profiles":
            guard let op = command["op"] as? String, ["save","apply","delete"].contains(op),
                  let original = command["name"] as? String else { throw ReaderNativePreferences.Failure(message:"预设参数无效") }
            let name = original.trimmingCharacters(in:.whitespacesAndNewlines)
            guard !name.isEmpty, name.utf16.count <= 20, !original.hasPrefix("_") else { throw ReaderNativePreferences.Failure(message:"预设名称需为 1–20 个字符，且不能以下划线开头") }
            return try await request("/api/assistant/pref-profiles",body:["op":op,"name":name])
        case "voice":
            guard let cfg = voice, let name = command["key"] as? String, let value = command["value"] else { throw ReaderNativePreferences.Failure(message:"请先读取当前语音配置") }
            let device = command["device"] as? Bool == true
            guard let field = try fields(cfg).first(where:{ $0["key"] as? String == name && ($0["device"] as? Bool == true) == device }) else {
                throw ReaderNativePreferences.Failure(message:"当前不能修改这项设置")
            }
            try ReaderNativeVoiceSettings.validate(field,value)
            if device {
                let encoded = field["kind"] as? String == "toggle" ? ((value as? Bool == true) ? "1" : "0") : (value as? String ?? (value as? NSNumber)?.stringValue ?? "")
                try await write(name,encoded); try current(); return ["ok":true]
            }
            let result = try await request("/api/assistant/voice-config",body:[name:value])
            if name == "rt_tool_reply" { try await write("rc-voice-toolreply",value as? Bool == true ? "1" : "0") }
            try current(); try await refreshVoice(); try current()
            return result
        default: throw ReaderNativePreferences.Failure(message:"未知设置分类")
        }
    }

    static func modelWrite(_ value: O, current: O) throws -> O {
        guard let action = value["action"] as? String, let actions = current["actions"] as? O, actions[action] != nil,
              let catalog = current["catalog"] as? O else { throw ReaderNativePreferences.Failure(message:"任务配置已变化，请重新读取") }
        let backend = value["backend"] as? String ?? "", variant = value["variant"] as? String ?? "", depth = value["depth"] as? String ?? "", fast = value["fast"] as? Bool == true
        if !backend.isEmpty {
            let backends = (catalog["backends_by_action"] as? [String:[String]])?[action] ?? catalog["backends"] as? [String] ?? []
            let variants = (catalog["variants"] as? [String:[String]])?[backend] ?? []
            let base = variant.hasSuffix("@paid") ? String(variant.dropLast(5)) : variant
            let capabilities = (catalog["codex_capabilities"] as? [String:O])?[variant] ?? [:]
            let depths = backend == "codex" ? (catalog["codex_depths_by_model"] as? [String:[String]])?[variant] ?? (catalog["depths"] as? [String:[String]])?[backend] ?? [] : (catalog["depths"] as? [String:[String]])?[backend] ?? []
            let locked = (current["locked"] as? [String:[String]])?[action] ?? []
            guard backends.contains(backend), !locked.contains(backend), variants.contains(variant) || (backend == "gemini" && variants.contains(base)),
                  backend != "codex" || capabilities["selectable"] as? Bool == true, depths.contains(depth),
                  !fast || (backend == "codex" && capabilities["selectable"] as? Bool == true && (capabilities["fast"] as? Bool == true || capabilities["priority"] as? Bool == true)) else {
                throw ReaderNativePreferences.Failure(message:"不支持这个模型组合，未修改原设置")
            }
        }
        return ["action":action,"backend":backend,"variant":variant,"depth":depth,"fast":fast]
    }
}

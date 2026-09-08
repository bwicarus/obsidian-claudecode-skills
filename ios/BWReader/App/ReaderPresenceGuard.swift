import AVFoundation
import CoreLocation
import Foundation

/// 在场判断 + 自动静音（2026-09-08 用户：「如果我在外面没有带耳机的情况下
/// 如果 app 的语音是开启的其实我希望 app 可以自动静音」）。
///
/// 判据两条**都要成立**才静音：
///   ① 音频从内建扬声器/听筒出去（也就是没戴耳机）；
///   ② 当下的定位**确实**不在「家」这个区里。
///
/// 用户 2026-09-08 明确保留了第二条：「在家里我说的算，而且也只有我一个人」。
/// 所以这不是"没耳机就静音"，在家里没耳机照样出声。
///
/// ## 为什么判断在本机做
///
/// 出门那一刻要立刻生效。绕一趟 Windows 再回来早就出声了；而 Windows 那份
/// `current-place.json` 最旧可以是几小时前的 —— 拿它判"我现在是不是出门了"
/// 恰好会答错（刚出门时它还写着 home）。所以本机只要**判据**：几个坐标 +
/// 一个半径，自己拿当下的定位比一比。判据由桥在 `/reader-presence/v1` 的
/// 响应里下发（Python 导出，别名→状态的映射只存在那一处），本地缓存一份。
///
/// ## 「不知道」不静音
///
/// 定位读不到（没授权、没开记录开关、还没拿到第一个 fix）时**不静音**。
/// 不知道在哪 ≠ 在外面：他大部分时间在家，而错误地静音会让语音功能看起来
/// 是坏的，且没有任何提示说得出为什么。少静一次的代价小得多。
@MainActor
final class ReaderPresenceGuard {
    static let shared = ReaderPresenceGuard()

    /// 最近一次判断的结论与理由。这条链没有界面，出问题只能靠它。
    private(set) var lastNote: String = "尚未判断"
    /// 现在该不该把语音输出静掉。
    private(set) var shouldMuteVoiceOutput = false

    private static let zonesKey = "readerVoiceZonesCache"
    private static let endpoint = URL(
        string: "https://\(ReaderNativePiGateway.piHost)/reader-presence/v1")

    private var routeObserver: NSObjectProtocol?
    private var reporting = false

    private init() {
        // 路由一变就重判：耳机拔出的那一刻必须立刻静，这是整个功能的意义所在。
        routeObserver = NotificationCenter.default.addObserver(
            forName: AVAudioSession.routeChangeNotification,
            object: AVAudioSession.sharedInstance(),
            queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.reevaluate(reason: "音频路由变化")
            }
        }
        reevaluate(reason: "启动")
    }

    // ─────────────────────────── 音频走向 ───────────────────────────

    /// 当前输出走哪儿。取第一个输出口：同时接多个输出时系统只会用一个。
    static func currentAudioRoute() -> String {
        let outputs = AVAudioSession.sharedInstance().currentRoute.outputs
        guard let port = outputs.first else {
            // 一个输出口都没有（罕见，通常是会话还没激活）。当成"不知道"，
            // 而不知道在下游算"别静音"。
            return "other"
        }
        switch port.portType {
        case .builtInSpeaker: return "speaker"
        case .builtInReceiver: return "receiver"
        case .headphones, .headsetMic: return "headphones"
        case .bluetoothA2DP, .bluetoothLE, .bluetoothHFP: return "bluetooth"
        case .airPlay: return "airplay"
        case .usbAudio: return "usb"
        case .carAudio: return "carplay"
        default: return "other"
        }
    }

    /// 这个走向算不算"戴着耳机"。
    /// ⚠ 只有内建扬声器和听筒算**没戴**。不认识的走向一律算戴着 ——
    /// 认错成扬声器会把人的语音静掉，认错成耳机只是少静一次，代价不对等。
    static func routeIsPrivate(_ route: String) -> Bool {
        route != "speaker" && route != "receiver"
    }

    // ─────────────────────────── 语音区 ───────────────────────────

    private struct Zone {
        let state: String
        let latitude: Double
        let longitude: Double
    }

    private struct Zones {
        let hitRadiusMeters: Double
        let zones: [Zone]
    }

    /// 缓存的语音区。桥不可达时照样能判 —— 这正是要缓存的理由：
    /// 出门在外往往连不上家里的电脑，而那恰恰是最需要这个判断的时刻。
    private func cachedZones() -> Zones? {
        guard let raw = UserDefaults.standard.dictionary(forKey: Self.zonesKey)
        else { return nil }
        return Self.parseZones(raw)
    }

    private static func parseZones(_ raw: [String: Any]) -> Zones? {
        guard let list = raw["zones"] as? [[String: Any]] else { return nil }
        let radius = (raw["hitRadiusM"] as? Double) ?? 200.0
        let zones: [Zone] = list.compactMap { one in
            guard let state = one["state"] as? String,
                  let latitude = one["lat"] as? Double,
                  let longitude = one["lon"] as? Double else { return nil }
            return Zone(state: state, latitude: latitude, longitude: longitude)
        }
        // 空清单当**没有**判据（返回 nil）而不是"一个地点都没命名"：
        // 后者会让 App 认为自己永远在外面，一出声就静音。
        return zones.isEmpty ? nil : Zones(hitRadiusMeters: radius, zones: zones)
    }

    private func storeZones(_ raw: [String: Any]) {
        guard Self.parseZones(raw) != nil else { return }
        UserDefaults.standard.set(raw, forKey: Self.zonesKey)
    }

    // ─────────────────────────── 判断 ───────────────────────────

    /// (在不在某个已知区里, 区的状态)。定位或判据缺一样就返回 nil = 不知道。
    private func currentZoneState() -> String? {
        guard let zones = cachedZones() else { return nil }
        guard let snapshot = ReaderLocationProvider.shared.latest,
              let latitude = snapshot["lat"] as? Double,
              let longitude = snapshot["lon"] as? Double else { return nil }
        let here = CLLocation(latitude: latitude, longitude: longitude)
        var best: (state: String, distance: Double)?
        for zone in zones.zones {
            let distance = here.distance(from: CLLocation(
                latitude: zone.latitude, longitude: zone.longitude))
            if distance <= zones.hitRadiusMeters,
               best == nil || distance < best!.distance {
                best = (zone.state, distance)
            }
        }
        // 有判据也有定位，但不在任何区里 —— 这是**确实在外面**，不是不知道。
        return best?.state ?? "elsewhere"
    }

    /// 重新判一次，把结论和理由留在 `shouldMuteVoiceOutput` / `lastNote` 上。
    ///
    /// ⚠ 只更新状态，**不主动推给谁**：同时存在两个 NativeAudioEngine
    /// （语音会话和语音桥各一个），推的一方要记住有几个订阅者，
    /// 漏掉一个的表现是"有时静音有时不静"，极难查。改由引擎在路由变化
    /// 和开播时来问（见 NativeAudioEngine.applyPresenceMute）。
    func reevaluate(reason: String) {
        let route = Self.currentAudioRoute()
        if Self.routeIsPrivate(route) {
            shouldMuteVoiceOutput = false
            lastNote = "\(reason)：输出走 \(route)，戴着耳机，照常出声"
        } else if let state = currentZoneState() {
            // 只有"确实不在家"才静。在家里没戴耳机照常出声
            //（用户：「在家里我说的算，而且也只有我一个人」）。
            shouldMuteVoiceOutput = state != "home"
            lastNote = shouldMuteVoiceOutput
                ? "\(reason)：在外面（\(state)）且没戴耳机，已自动静音"
                : "\(reason)：在家且没戴耳机，照常出声"
        } else {
            // 判不出在哪就不静 —— 不知道 ≠ 在外面。
            shouldMuteVoiceOutput = false
            lastNote = "\(reason)：没戴耳机，但判不出在哪（缺定位或缺语音区），不静音"
        }
    }

    // ─────────────────────────── 上报 ───────────────────────────

    /// 把此刻的在场状态报给 Windows，并顺路把语音区判据取回来缓存。
    ///
    /// 上报是给 **AI 判断和 situation_triggers 的规则**用的副本，
    /// **不是**静音的控制回路 —— 静音已经在 `reevaluate` 里本地做完了。
    /// 所以这里失败一律安静降级，只把原因留在 lastNote 里。
    func report(foreground: Bool) async {
        guard !reporting, let endpoint = Self.endpoint else { return }
        reporting = true
        defer { reporting = false }

        let route = Self.currentAudioRoute()
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.timeoutInterval = 8
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "audioRoute": route,
            "foreground": foreground,
            "device": "iPhone",
            "atMs": Int(Date().timeIntervalSince1970 * 1000),
        ])
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let payload = (try? JSONSerialization.jsonObject(with: data))
                as? [String: Any] ?? [:]
            guard status == 200, payload["ok"] as? Bool == true else {
                // 桥的 detail 说得清错在哪个字段 —— 原样留住。
                lastNote = "在场上报被拒：" + ((payload["detail"] as? String)
                    ?? "HTTP \(status)")
                return
            }
            if let zones = payload["voiceZones"] as? [String: Any] {
                storeZones(zones)
                // 判据可能刚刚才第一次拿到，拿到就重判一次。
                reevaluate(reason: "语音区已更新")
            }
        } catch {
            lastNote = "在场上报失败（电脑不可达？）：\(error.localizedDescription)"
        }
    }
}

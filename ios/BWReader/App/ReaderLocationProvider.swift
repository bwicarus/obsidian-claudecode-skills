import CoreLocation
import Foundation

/// 活动账本的「地点」维度（references/activity-ledger-design.md §3.4，
/// 用户 2026-08-25 拍板：使用期间权限、精度到建筑物、带地名）。
///
/// 纪律：
/// - **开关先行**：`readerLocationRecordingEnabled` 为真才申请权限、才取位置。
///   默认关 —— 位置是隐私面最大的一列，"顺手为真"不可接受。
/// - **不连续追踪**：只在进前台/开书时 `requestLocation()` 取一次；
///   记录目标是"在哪栋楼学习"，不是轨迹。
/// - **后台只用显著位置变化**（2026-09-12 用户拍板加的第二档）：
///   `readerLocationBackgroundEnabled` 另开一个开关、另要一次「始终」权限。
///   用 `startMonitoringSignificantLocationChanges()` —— 移动约 500m /
///   基站切换才唤醒（app 被系统杀掉也会重新拉起），走基站与 WiFi、不开 GPS。
///   **不用** `startUpdatingLocation()`：那是连续轨迹，与上面那条相抵触。
///   动机是他报的："经常会出现位置记录过旧" —— 前台那条链搭在"翻页停留"
///   上走，不开 app 就没有位置。
/// - **反解节流**：位置较上次反解移动 >50m 才再次 reverse geocode
///   （CLGeocoder 有速率限制），地名缓存复用。
/// - 坐标与地名**两者都存**（evidence-quality-lessons：采集不可重来，
///   地名反解错了还能从坐标重来，反之不行）。
@MainActor
final class ReaderLocationProvider: NSObject, CLLocationManagerDelegate {
    static let shared = ReaderLocationProvider()

    private static let enabledKey = "readerLocationRecordingEnabled"
    /// 后台档的开关。与前台档**分开**：位置是隐私面最大的一列，
    /// "开了记录"不等于"同意一直被盯着"。
    private static let backgroundKey = "readerLocationBackgroundEnabled"
    /// 送不出去的定位先攒在这儿，下次能连上时补送。
    ///
    /// ⚠ 没有这条队列，"后台更新"会在**电脑没开机/睡了**时静默丢失 ——
    /// 而那恰恰是最常见的状况。后台唤醒只有十几秒，失败就没有第二次机会。
    private static let pendingKey = "readerLocationPendingFixes"
    private static let pendingLimit = 40
    private let manager = CLLocationManager()
    private let geocoder = CLGeocoder()
    private var lastGeocodedLocation: CLLocation?
    private var lastPlaceName: String?

    /// 最近一次定位的快照，形状与 JS 侧 `window.__BW_DEVICE_LOCATION__` 一致：
    /// {lat, lon, acc, name, at}。nil = 没有可用定位。
    private(set) var latest: [String: Any]?

    /// 位置更新时的回调（ReaderWebView 用它把快照推进页面全局变量）。
    var onUpdate: (([String: Any]) -> Void)?

    var isEnabled: Bool {
        UserDefaults.standard.bool(forKey: Self.enabledKey)
    }

    /// 「始终」权限到手了没。后台档开着但没这个权限时，它不会真的盯 ——
    /// 面板要能分清卡在开关还是卡在权限。
    var hasAlwaysAuthorization: Bool {
        manager.authorizationStatus == .authorizedAlways
    }

    var isAuthorized: Bool {
        switch manager.authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways:
            return true
        default:
            return false
        }
    }

    /// 后台档开着吗。**前台档关着时它一律无效** —— 不能出现
    /// "我关了位置记录，它还在后台报"这种事。
    var isBackgroundEnabled: Bool {
        isEnabled && UserDefaults.standard.bool(forKey: Self.backgroundKey)
    }

    func setBackgroundEnabled(_ value: Bool) {
        UserDefaults.standard.set(value, forKey: Self.backgroundKey)
        if value {
            manager.delegate = self
            // 「始终」权限要单独要一次；系统还会时不时提醒用户"某 app 在
            // 后台用了你的位置"，这是 iOS 的规矩，不是我们能绕开的。
            if manager.authorizationStatus == .authorizedWhenInUse {
                manager.requestAlwaysAuthorization()
            }
            startBackgroundMonitoringIfAllowed()
        } else {
            manager.stopMonitoringSignificantLocationChanges()
            manager.allowsBackgroundLocationUpdates = false
        }
    }

    /// 权限够了就开始盯；不够就什么都不做（等授权回调再来一次）。
    func startBackgroundMonitoringIfAllowed() {
        guard isBackgroundEnabled else { return }
        guard manager.authorizationStatus == .authorizedAlways else { return }
        guard CLLocationManager.significantLocationChangeMonitoringAvailable()
        else { return }
        manager.allowsBackgroundLocationUpdates = true
        manager.pausesLocationUpdatesAutomatically = false
        manager.startMonitoringSignificantLocationChanges()
    }

    func setEnabled(_ value: Bool) {
        UserDefaults.standard.set(value, forKey: Self.enabledKey)
        if value {
            refresh()
        } else {
            latest = nil
        }
    }

    /// 进前台/开书时调用：开关开着才动。一次性定位，不开连续更新。
    func refresh() {
        guard isEnabled else { return }
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyNearestTenMeters
        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
        case .authorizedWhenInUse, .authorizedAlways:
            manager.requestLocation()
        default:
            break
        }
    }

    nonisolated func locationManagerDidChangeAuthorization(
        _ manager: CLLocationManager
    ) {
        Task { @MainActor in
            if self.isEnabled, self.isAuthorized {
                self.manager.requestLocation()
            }
            // 「始终」是用户在系统里点的，回到这条回调才知道 —— 那时才真正
            // 开得起来。放在 setBackgroundEnabled 里一次性尝试是不够的。
            self.startBackgroundMonitoringIfAllowed()
        }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        didUpdateLocations locations: [CLLocation]
    ) {
        guard let location = locations.last else { return }
        Task { @MainActor in
            self.accept(location)
        }
    }

    nonisolated func locationManager(
        _ manager: CLLocationManager,
        didFailWithError error: Error
    ) {
        // 拿不到就保持上一次的快照；定位是增强，失败不打扰阅读。
    }

    private func accept(_ location: CLLocation) {
        // 后台档开着时，每一次定位都直接报给 Windows —— 后台没有 WebView，
        // 前台那条"挂在翻页停留上"的链在这里根本不存在。
        if isBackgroundEnabled {
            report(location)
        }
        var snapshot: [String: Any] = [
            "lat": location.coordinate.latitude,
            "lon": location.coordinate.longitude,
            "acc": max(0, location.horizontalAccuracy),
            "at": Int(location.timestamp.timeIntervalSince1970),
        ]
        if let name = lastPlaceName,
           let previous = lastGeocodedLocation,
           location.distance(from: previous) <= 50 {
            snapshot["name"] = name
            latest = snapshot
            onUpdate?(snapshot)
            return
        }
        latest = snapshot
        onUpdate?(snapshot)
        geocoder.cancelGeocode()
        geocoder.reverseGeocodeLocation(location) { [weak self] placemarks, _ in
            Task { @MainActor in
                guard let self else { return }
                guard let mark = placemarks?.first else { return }
                // 建筑物级优先：POI/建筑名 → 街道门牌 → 街区 → 城市。
                let name = [
                    mark.name,
                    mark.thoroughfare.flatMap { street in
                        mark.subThoroughfare.map { "\(street)\($0)" } ?? street
                    },
                    mark.subLocality,
                    mark.locality,
                ].compactMap { $0 }.first
                guard let name, !name.isEmpty else { return }
                self.lastPlaceName = String(name.prefix(80))
                self.lastGeocodedLocation = location
                if var current = self.latest {
                    current["name"] = self.lastPlaceName
                    self.latest = current
                    self.onUpdate?(current)
                }
            }
        }
    }

    // ── 原生直报 + 离线补送 ─────────────────────────────────────────
    //
    // ⚠ 报给谁：Windows 桥的 `/device/location`（它只记原始值，格式与
    //   目录规矩留在 Python 那侧）。桥不在线时**不能当作丢失** ——
    //   那台电脑睡着是常态，而定位采不回来。
    private static let endpoint = URL(
        string: "https://bwicarus-2.taile44d0c.ts.net/device/location"
    )!

    private func report(_ location: CLLocation) {
        var fix: [String: Any] = [
            "lat": location.coordinate.latitude,
            "lon": location.coordinate.longitude,
            "acc": max(0, location.horizontalAccuracy),
            "at": Int(location.timestamp.timeIntervalSince1970),
            // ⚠ 这一位是**我们在盯着**的声明，判新旧那一侧据此把
            //   "久没更新"读成"久没挪窝"而不是"不知道在哪"。
            //   只有真的开着监听才写 true。
            "watching": true,
        ]
        if let name = lastPlaceName { fix["name"] = name }
        var queue = pendingFixes()
        queue.append(fix)
        if queue.count > Self.pendingLimit {
            queue = Array(queue.suffix(Self.pendingLimit))
        }
        savePendingFixes(queue)
        flushPendingFixes()
    }

    private func pendingFixes() -> [[String: Any]] {
        (UserDefaults.standard.array(forKey: Self.pendingKey)
            as? [[String: Any]]) ?? []
    }

    private func savePendingFixes(_ value: [[String: Any]]) {
        UserDefaults.standard.set(value, forKey: Self.pendingKey)
    }

    /// 把攒下的定位逐条送出去；**送成一条才移除一条**。
    ///
    /// ⚠ 不批量发：桥那侧一条就是一条记录，批量要另定协议；而后台唤醒的
    ///   时间窗很短，多发几条 POST 比发明一套批量格式便宜得多。
    func flushPendingFixes() {
        let queue = pendingFixes()
        guard let first = queue.first else { return }
        guard let payload = try? JSONSerialization.data(
            withJSONObject: first) else {
            savePendingFixes(Array(queue.dropFirst()))
            return
        }
        var request = URLRequest(url: Self.endpoint)
        request.httpMethod = "POST"
        request.setValue(
            "application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = payload
        request.timeoutInterval = 12
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            Task { @MainActor in
                guard let self else { return }
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                guard (200..<300).contains(code) else { return }   // 留着下次补
                var rest = self.pendingFixes()
                if !rest.isEmpty { rest.removeFirst() }
                self.savePendingFixes(rest)
                if !rest.isEmpty { self.flushPendingFixes() }
            }
        }.resume()
    }
}

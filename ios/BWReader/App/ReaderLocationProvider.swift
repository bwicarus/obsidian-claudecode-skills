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
/// - **反解节流**：位置较上次反解移动 >50m 才再次 reverse geocode
///   （CLGeocoder 有速率限制），地名缓存复用。
/// - 坐标与地名**两者都存**（evidence-quality-lessons：采集不可重来，
///   地名反解错了还能从坐标重来，反之不行）。
@MainActor
final class ReaderLocationProvider: NSObject, CLLocationManagerDelegate {
    static let shared = ReaderLocationProvider()

    private static let enabledKey = "readerLocationRecordingEnabled"
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

    var isAuthorized: Bool {
        switch manager.authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways:
            return true
        default:
            return false
        }
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
}

import Combine
import CoreGraphics
import Foundation

enum NativePencilGestureMapping: String, CaseIterable, Identifiable {
    case followSystem = "follow-system"
    case toggleEraser = "toggle-eraser"
    case toggleSelection = "toggle-selection"
    case showPalette = "show-palette"
    case disabled

    var id: String { rawValue }
}

enum NativePencilLauncherPreset: String, CaseIterable, Identifiable {
    case topLeading = "top-leading"
    case topTrailing = "top-trailing"
    case bottomLeading = "bottom-leading"
    case bottomTrailing = "bottom-trailing"

    var id: String { rawValue }

    var anchor: CGPoint {
        switch self {
        case .topLeading:
            return CGPoint(x: 0.08, y: 0.16)
        case .topTrailing:
            return CGPoint(x: 0.92, y: 0.16)
        case .bottomLeading:
            return CGPoint(x: 0.08, y: 0.84)
        case .bottomTrailing:
            return CGPoint(x: 0.92, y: 0.84)
        }
    }
}

@MainActor
final class NativePencilSettings: ObservableObject {
    static let shared = NativePencilSettings()

    private enum Key {
        static let doubleTap = "native-pencil.double-tap"
        static let squeeze = "native-pencil.squeeze"
        static let launcherAnchorX = "native-pencil.launcher-anchor-x"
        static let launcherAnchorY = "native-pencil.launcher-anchor-y"
    }

    @Published var doubleTap: NativePencilGestureMapping {
        didSet { defaults.set(doubleTap.rawValue, forKey: Key.doubleTap) }
    }

    @Published var squeeze: NativePencilGestureMapping {
        didSet { defaults.set(squeeze.rawValue, forKey: Key.squeeze) }
    }

    @Published private(set) var launcherAnchor: CGPoint

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        doubleTap = Self.read(Key.doubleTap, from: defaults)
        squeeze = Self.read(Key.squeeze, from: defaults)
        launcherAnchor = Self.readLauncherAnchor(from: defaults)
    }

    func setLauncherAnchor(_ anchor: CGPoint) {
        let normalized = CGPoint(
            x: min(1, max(0, anchor.x)),
            y: min(1, max(0, anchor.y))
        )
        launcherAnchor = normalized
        defaults.set(Double(normalized.x), forKey: Key.launcherAnchorX)
        defaults.set(Double(normalized.y), forKey: Key.launcherAnchorY)
    }

    func setLauncherPreset(_ preset: NativePencilLauncherPreset) {
        setLauncherAnchor(preset.anchor)
    }

    func resetLauncherAnchor() {
        defaults.removeObject(forKey: Key.launcherAnchorX)
        defaults.removeObject(forKey: Key.launcherAnchorY)
        launcherAnchor = NativePencilLauncherPreset.bottomTrailing.anchor
    }

    private static func read(
        _ key: String,
        from defaults: UserDefaults
    ) -> NativePencilGestureMapping {
        guard
            let rawValue = defaults.string(forKey: key),
            let mapping = NativePencilGestureMapping(rawValue: rawValue)
        else {
            return .followSystem
        }
        return mapping
    }

    private static func readLauncherAnchor(from defaults: UserDefaults) -> CGPoint {
        guard
            defaults.object(forKey: Key.launcherAnchorX) != nil,
            defaults.object(forKey: Key.launcherAnchorY) != nil
        else {
            return NativePencilLauncherPreset.bottomTrailing.anchor
        }
        return CGPoint(
            x: min(1, max(0, defaults.double(forKey: Key.launcherAnchorX))),
            y: min(1, max(0, defaults.double(forKey: Key.launcherAnchorY)))
        )
    }
}

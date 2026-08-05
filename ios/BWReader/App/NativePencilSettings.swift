import Combine
import Foundation

enum NativePencilGestureMapping: String, CaseIterable, Identifiable {
    case followSystem = "follow-system"
    case toggleEraser = "toggle-eraser"
    case showPalette = "show-palette"
    case disabled

    var id: String { rawValue }
}

@MainActor
final class NativePencilSettings: ObservableObject {
    static let shared = NativePencilSettings()

    private enum Key {
        static let doubleTap = "native-pencil.double-tap"
        static let squeeze = "native-pencil.squeeze"
    }

    @Published var doubleTap: NativePencilGestureMapping {
        didSet { defaults.set(doubleTap.rawValue, forKey: Key.doubleTap) }
    }

    @Published var squeeze: NativePencilGestureMapping {
        didSet { defaults.set(squeeze.rawValue, forKey: Key.squeeze) }
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        doubleTap = Self.read(Key.doubleTap, from: defaults)
        squeeze = Self.read(Key.squeeze, from: defaults)
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
}

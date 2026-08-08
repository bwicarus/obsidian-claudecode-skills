import Combine
import Foundation

@MainActor
final class ReaderTextRecognitionPreferences: ObservableObject {
    static let shared = ReaderTextRecognitionPreferences()

    private enum Key {
        static let enabled = "reader.textRecognition.enabled"
        static let automaticLocal = "reader.textRecognition.automaticLocal"
    }

    @Published var isEnabled: Bool {
        didSet { defaults.set(isEnabled, forKey: Key.enabled) }
    }

    @Published var automaticLocalProcessingEnabled: Bool {
        didSet {
            defaults.set(
                automaticLocalProcessingEnabled,
                forKey: Key.automaticLocal
            )
        }
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        isEnabled = defaults.object(forKey: Key.enabled) as? Bool ?? true
        automaticLocalProcessingEnabled = defaults.bool(
            forKey: Key.automaticLocal
        )
    }
}

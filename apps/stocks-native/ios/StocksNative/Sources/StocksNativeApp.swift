import SwiftUI

@main
@MainActor
struct StocksNativeApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            StocksRootView(model: model)
                .preferredColorScheme(.light)
        }
    }
}

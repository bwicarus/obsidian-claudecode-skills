import SwiftUI

@main
@MainActor
struct StocksNativeApp: App {
    @StateObject private var model = StocksAppRuntime.model
    @UIApplicationDelegateAdaptor(StocksAppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            StocksRootView(model: model)
                .preferredColorScheme(.light)
        }
    }
}

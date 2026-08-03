import Foundation
import SafariServices

/// Native endpoint required by the Safari Web Extension target.
///
/// The extension currently uses WebExtension messaging only; it does not ask
/// the host app to perform privileged work. Keep this endpoint deterministic
/// and side-effect free if Safari probes it.
final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    func beginRequest(with context: NSExtensionContext) {
        let response = NSExtensionItem()
        response.userInfo = [
            SFExtensionMessageKey: ["ok": true]
        ]
        context.completeRequest(
            returningItems: [response],
            completionHandler: nil
        )
    }
}

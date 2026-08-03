import Foundation
import SafariServices

/// Narrow native bridge for the Safari Web Extension.
///
/// It never starts audio or performs a Windows action. `voice.toggle` only
/// records a short-lived command in the App Group. The containing app must be
/// opened with the returned custom URL and independently consume the matching
/// request before the existing voice bridge may act.
final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    private let store = ReaderNativeBridgeStore()

    func beginRequest(with context: NSExtensionContext) {
        guard
            let item = context.inputItems.first as? NSExtensionItem,
            let message = item.userInfo?[SFExtensionMessageKey]
                as? [String: Any]
        else {
            complete(
                context,
                response: failure(
                    action: "unknown",
                    requestID: "unknown",
                    code: "BW_NATIVE_MESSAGE_SCHEMA",
                    message: "原生消息格式无效"
                )
            )
            return
        }

        let action = message["action"] as? String ?? "unknown"
        let requestID = message["requestId"] as? String ?? "unknown"
        guard
            message["contract"] as? String
                == ReaderNativeBridgeContract.name,
            ReaderNativeBridgeContract.isSafeRequestID(requestID)
        else {
            complete(
                context,
                response: failure(
                    action: action,
                    requestID: requestID,
                    code: "BW_NATIVE_MESSAGE_CONTRACT",
                    message: "原生消息合同或请求号无效"
                )
            )
            return
        }

        switch action {
        case "capabilities":
            guard exactKeys(
                message,
                required: ["contract", "action", "requestId"]
            ) else {
                complete(
                    context,
                    response: schemaFailure(
                        action: action,
                        requestID: requestID
                    )
                )
                return
            }
            var response = baseResponse(
                action: action,
                requestID: requestID
            )
            response["actions"] = ReaderNativeBridgeContract.supportedActions
            response["appKinds"] = ReaderNativeBridgeContract.supportedAppKinds
            response["launchScheme"] = ReaderNativeBridgeContract.launchScheme
            response["containingApp"] =
                ReaderNativeBridgeContract.containingAppIdentifier
            response["requiresForegroundLaunch"] = true
            complete(context, response: response)

        case "voice.status":
            guard exactKeys(
                message,
                required: ["contract", "action", "requestId"]
            ) else {
                complete(
                    context,
                    response: schemaFailure(
                        action: action,
                        requestID: requestID
                    )
                )
                return
            }
            do {
                let status = try store.readStatus() ?? .idle
                var response = baseResponse(
                    action: action,
                    requestID: requestID
                )
                response["state"] = status.responseDictionary
                complete(context, response: response)
            } catch {
                complete(
                    context,
                    response: failure(
                        action: action,
                        requestID: requestID,
                        code: "BW_NATIVE_STATUS_UNAVAILABLE",
                        message: error.localizedDescription,
                        retryable: true
                    )
                )
            }

        case "voice.toggle":
            handleVoiceToggle(
                message,
                action: action,
                requestID: requestID,
                context: context
            )

        case "voice.context":
            guard exactKeys(
                message,
                required: ["contract", "action", "requestId", "webContext"]
            ), let webContext = decodeWebContext(message["webContext"])
            else {
                complete(context, response: schemaFailure(
                    action: action,
                    requestID: requestID
                ))
                return
            }
            do {
                try store.writeLatestWebContext(webContext)
                var response = baseResponse(action: action, requestID: requestID)
                response["state"] = (try store.readStatus() ?? .idle)
                    .responseDictionary
                complete(context, response: response)
            } catch {
                complete(context, response: bridgeFailure(
                    action: action,
                    requestID: requestID,
                    error: error
                ))
            }

        case "agent.status":
            guard exactKeys(
                message,
                required: ["contract", "action", "requestId"]
            ) else {
                complete(context, response: schemaFailure(
                    action: action,
                    requestID: requestID
                ))
                return
            }
            do {
                var response = baseResponse(action: action, requestID: requestID)
                response["state"] = (try store.readAgentStatus() ?? .idle)
                    .responseDictionary
                complete(context, response: response)
            } catch {
                complete(context, response: bridgeFailure(
                    action: action,
                    requestID: requestID,
                    error: error
                ))
            }

        case "agent.toggle":
            handleAgentToggle(
                message,
                action: action,
                requestID: requestID,
                context: context
            )

        case "agent.events":
            guard exactKeys(
                message,
                required: ["contract", "action", "requestId", "after"]
            ), let after = (message["after"] as? NSNumber)?.int64Value,
               after >= 0
            else {
                complete(context, response: schemaFailure(
                    action: action,
                    requestID: requestID
                ))
                return
            }
            do {
                let events = try store.readAgentEvents(after: after)
                var response = baseResponse(action: action, requestID: requestID)
                response["events"] = events.map(\.responseDictionary)
                response["cursor"] = events.last?.sequence ?? after
                complete(context, response: response)
            } catch {
                complete(context, response: bridgeFailure(
                    action: action,
                    requestID: requestID,
                    error: error
                ))
            }

        case "agent.command":
            handleAgentCommand(
                message,
                action: action,
                requestID: requestID,
                context: context
            )

        default:
            complete(
                context,
                response: failure(
                    action: action,
                    requestID: requestID,
                    code: "BW_NATIVE_ACTION_UNSUPPORTED",
                    message: "不支持的原生操作"
                )
            )
        }
    }

    private func handleVoiceToggle(
        _ message: [String: Any],
        action: String,
        requestID: String,
        context: NSExtensionContext
    ) {
        guard exactKeys(
            message,
            required: [
                "contract", "action", "requestId", "appKind", "webContext",
            ]
        ),
        let appKind = message["appKind"] as? String,
        ReaderNativeBridgeContract.supportedAppKinds.contains(appKind),
        let webContext = decodeWebContext(message["webContext"]),
        let launchURL = ReaderNativeBridgeContract.launchURL(
            requestID: requestID
        )
        else {
            complete(
                context,
                response: schemaFailure(
                    action: action,
                    requestID: requestID
                )
            )
            return
        }

        do {
            let command = ReaderNativePendingVoiceCommand(
                requestID: requestID,
                appKind: appKind,
                sourceURL: webContext.url,
                selectionText: webContext.selection,
                webContext: webContext
            )
            try store.writePending(command)
            try store.writeLatestWebContext(webContext)
            let status = try store.readStatus() ?? .idle
            var response = baseResponse(
                action: action,
                requestID: requestID
            )
            response["launchURL"] = launchURL.absoluteString
            response["opened"] = false
            response["state"] = status.responseDictionary
            complete(context, response: response)
        } catch {
            complete(
                context,
                response: failure(
                    action: action,
                    requestID: requestID,
                    code: "BW_NATIVE_COMMAND_STORE_FAILED",
                    message: error.localizedDescription,
                    retryable: true
                )
            )
        }
    }

    private func handleAgentToggle(
        _ message: [String: Any],
        action: String,
        requestID: String,
        context: NSExtensionContext
    ) {
        guard let command = message["command"] as? String,
              command == "start" || command == "stop"
        else {
            complete(context, response: schemaFailure(
                action: action,
                requestID: requestID
            ))
            return
        }
        let required: Set<String> = command == "start"
            ? ["contract", "action", "requestId", "command", "webContext"]
            : ["contract", "action", "requestId", "command"]
        guard exactKeys(message, required: required) else {
            complete(context, response: schemaFailure(
                action: action,
                requestID: requestID
            ))
            return
        }
        let webContext = command == "start"
            ? decodeWebContext(message["webContext"])
            : nil
        guard command != "start" || webContext != nil else {
            complete(context, response: schemaFailure(
                action: action,
                requestID: requestID
            ))
            return
        }
        do {
            var response = baseResponse(action: action, requestID: requestID)
            if command == "start" {
                let pending = ReaderNativePendingAgentToggle(
                    requestID: requestID,
                    command: command,
                    webContext: webContext
                )
                try store.writePendingAgentToggle(pending)
                response["launchURL"] = ReaderNativeBridgeContract.launchURL(
                    requestID: requestID,
                    host: "native-agent"
                )?.absoluteString
            } else {
                try store.writeAgentControl(ReaderNativeAgentControl(
                    requestID: requestID,
                    command: "stop"
                ))
            }
            response["state"] = (try store.readAgentStatus() ?? .idle)
                .responseDictionary
            complete(context, response: response)
        } catch {
            complete(context, response: bridgeFailure(
                action: action,
                requestID: requestID,
                error: error
            ))
        }
    }

    private func handleAgentCommand(
        _ message: [String: Any],
        action: String,
        requestID: String,
        context: NSExtensionContext
    ) {
        guard let command = message["command"] as? String,
              ["speak", "speak_done", "cancel"].contains(command)
        else {
            complete(context, response: schemaFailure(
                action: action,
                requestID: requestID
            ))
            return
        }
        let required: Set<String> = command == "speak"
            ? ["contract", "action", "requestId", "command", "text"]
            : ["contract", "action", "requestId", "command"]
        let optional: Set<String> = command == "speak" ? ["mood"] : []
        guard exactKeys(message, required: required, optional: optional),
              command != "speak" || validTextField(
                message["text"],
                maximumBytes: 32_768
              ),
              validTextField(message["mood"], maximumBytes: 256)
        else {
            complete(context, response: schemaFailure(
                action: action,
                requestID: requestID
            ))
            return
        }
        do {
            try store.writeAgentControl(ReaderNativeAgentControl(
                requestID: requestID,
                command: command,
                text: message["text"] as? String,
                mood: message["mood"] as? String
            ))
            var response = baseResponse(action: action, requestID: requestID)
            response["state"] = (try store.readAgentStatus() ?? .idle)
                .responseDictionary
            complete(context, response: response)
        } catch {
            complete(context, response: bridgeFailure(
                action: action,
                requestID: requestID,
                error: error
            ))
        }
    }

    private func decodeWebContext(_ value: Any?) -> ReaderNativeWebContext? {
        guard let value,
              JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value),
              let context = try? JSONDecoder().decode(
                ReaderNativeWebContext.self,
                from: data
              ),
              context.isValid
        else {
            return nil
        }
        return context
    }

    private func validSourceURLField(_ value: Any?) -> Bool {
        guard let value else {
            return true
        }
        guard
            let text = value as? String,
            text.utf8.count <= 2_048,
            let url = URL(string: text),
            let scheme = url.scheme?.lowercased(),
            scheme == "http" || scheme == "https",
            url.user == nil,
            url.password == nil
        else {
            return false
        }
        return true
    }

    private func validTextField(
        _ value: Any?,
        maximumBytes: Int
    ) -> Bool {
        guard let value else {
            return true
        }
        guard
            let text = value as? String,
            text.utf8.count <= maximumBytes
        else {
            return false
        }
        return true
    }

    private func bridgeFailure(
        action: String,
        requestID: String,
        error: Error
    ) -> [String: Any] {
        failure(
            action: action,
            requestID: requestID,
            code: "BW_NATIVE_BRIDGE_STORE_FAILED",
            message: error.localizedDescription,
            retryable: true
        )
    }

    private func exactKeys(
        _ message: [String: Any],
        required: Set<String>,
        optional: Set<String> = []
    ) -> Bool {
        let keys = Set(message.keys)
        return required.isSubset(of: keys)
            && keys.isSubset(of: required.union(optional))
    }

    private func baseResponse(
        action: String,
        requestID: String
    ) -> [String: Any] {
        [
            "contract": ReaderNativeBridgeContract.name,
            "action": action,
            "requestId": requestID,
            "ok": true,
        ]
    }

    private func schemaFailure(
        action: String,
        requestID: String
    ) -> [String: Any] {
        failure(
            action: action,
            requestID: requestID,
            code: "BW_NATIVE_MESSAGE_SCHEMA",
            message: "原生消息字段无效"
        )
    }

    private func failure(
        action: String,
        requestID: String,
        code: String,
        message: String,
        retryable: Bool = false
    ) -> [String: Any] {
        [
            "contract": ReaderNativeBridgeContract.name,
            "action": action,
            "requestId": requestID,
            "ok": false,
            "code": code,
            "error": message,
            "retryable": retryable,
        ]
    }

    private func complete(
        _ context: NSExtensionContext,
        response: [String: Any]
    ) {
        let item = NSExtensionItem()
        item.userInfo = [SFExtensionMessageKey: response]
        context.completeRequest(
            returningItems: [item],
            completionHandler: nil
        )
    }
}

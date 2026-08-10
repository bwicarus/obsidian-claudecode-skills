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

        case "realtime.status":
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
                let status = try ReaderRealtimeCredentialStore.shared.status()
                var response = baseResponse(
                    action: action,
                    requestID: requestID
                )
                response["configured"] = status.isConfigured
                response["model"] = status.model
                response["importedAt"] = status.importedAt.map {
                    Int64($0.timeIntervalSince1970 * 1_000)
                } ?? 0
                complete(context, response: response)
            } catch {
                complete(context, response: realtimeFailure(
                    action: action,
                    requestID: requestID,
                    error: error
                ))
            }

        case "realtime.mint":
            guard exactKeys(
                message,
                required: [
                    "contract", "action", "requestId", "file", "page",
                ]
            ),
            let file = message["file"] as? String,
            file.utf8.count <= 8_192,
            let pageNumber = message["page"] as? NSNumber,
            pageNumber.doubleValue.isFinite,
            pageNumber.doubleValue.rounded() == pageNumber.doubleValue,
            (0...10_000_000).contains(pageNumber.doubleValue)
            else {
                complete(context, response: schemaFailure(
                    action: action,
                    requestID: requestID
                ))
                return
            }
            Task {
                do {
                    let minted = try await ReaderRealtimeOpenAIClient
                        .mintClientSecret()
                    var response = baseResponse(
                        action: action,
                        requestID: requestID
                    )
                    response["clientSecret"] = minted.clientSecret
                    response["expiresAt"] = minted.expiresAt
                    response["model"] = minted.model
                    response["rtImage"] = minted.rtImage
                    response["compactTokens"] = minted.compactTokens
                    complete(context, response: response)
                } catch {
                    complete(context, response: realtimeFailure(
                        action: action,
                        requestID: requestID,
                        error: error
                    ))
                }
            }

        case "realtime.image":
            guard exactKeys(
                message,
                required: [
                    "contract", "action", "requestId", "callId",
                    "clientSecret", "tool", "mediaType", "b64",
                ]
            ),
            let callID = message["callId"] as? String,
            let clientSecret = message["clientSecret"] as? String,
            let tool = message["tool"] as? String,
            ["see_ink", "see_page", "see_figure"].contains(tool),
            let mediaType = message["mediaType"] as? String,
            ["image/jpeg", "image/png", "image/webp"].contains(mediaType),
            let base64 = message["b64"] as? String,
            (3_000...2_800_000).contains(base64.utf8.count)
            else {
                complete(context, response: schemaFailure(
                    action: action,
                    requestID: requestID
                ))
                return
            }
            Task {
                do {
                    try await ReaderRealtimeOpenAIClient.injectImage(
                        callID: callID,
                        clientSecret: clientSecret,
                        mediaType: mediaType,
                        base64: base64
                    )
                    complete(context, response: baseResponse(
                        action: action,
                        requestID: requestID
                    ))
                } catch {
                    complete(context, response: realtimeFailure(
                        action: action,
                        requestID: requestID,
                        error: error
                    ))
                }
            }

        case "realtime.hangup":
            guard exactKeys(
                message,
                required: [
                    "contract", "action", "requestId", "callId",
                    "clientSecret",
                ]
            ),
            let callID = message["callId"] as? String,
            let clientSecret = message["clientSecret"] as? String
            else {
                complete(context, response: schemaFailure(
                    action: action,
                    requestID: requestID
                ))
                return
            }
            Task {
                do {
                    try await ReaderRealtimeOpenAIClient.hangup(
                        callID: callID,
                        clientSecret: clientSecret
                    )
                    complete(context, response: baseResponse(
                        action: action,
                        requestID: requestID
                    ))
                } catch {
                    complete(context, response: realtimeFailure(
                        action: action,
                        requestID: requestID,
                        error: error
                    ))
                }
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

        case "notes.status":
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
                let state = try ReaderNativeFeatureStore()
                    .loadLocalNotesState() ?? .unavailable
                let pending = try ReaderLocalNoteOutboxStore().pending()
                let notes = mergedLocalNotes(state: state, pending: pending)
                var response = baseResponse(action: action, requestID: requestID)
                response["storage"] = localNotesStorageDictionary(
                    state,
                    pendingCount: pending.count,
                    totalCount: notes.count
                )
                complete(context, response: response)
            } catch {
                complete(context, response: bridgeFailure(
                    action: action,
                    requestID: requestID,
                    error: error
                ))
            }

        case "notes.list":
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
                let state = try ReaderNativeFeatureStore()
                    .loadLocalNotesState() ?? .unavailable
                let pending = try ReaderLocalNoteOutboxStore().pending()
                let notes = mergedLocalNotes(state: state, pending: pending)
                var response = baseResponse(action: action, requestID: requestID)
                response["storage"] = localNotesStorageDictionary(
                    state,
                    pendingCount: pending.count,
                    totalCount: notes.count
                )
                response["notes"] = notes.map(localNoteSummaryDictionary)
                complete(context, response: response)
            } catch {
                complete(context, response: bridgeFailure(
                    action: action,
                    requestID: requestID,
                    error: error
                ))
            }

        case "notes.read":
            guard exactKeys(
                message,
                required: ["contract", "action", "requestId", "noteId"]
            ), let noteID = message["noteId"] as? String,
               ReaderNativeBridgeContract.isSafeRequestID(noteID)
            else {
                complete(context, response: schemaFailure(
                    action: action,
                    requestID: requestID
                ))
                return
            }
            do {
                let state = try ReaderNativeFeatureStore()
                    .loadLocalNotesState() ?? .unavailable
                let pending = try ReaderLocalNoteOutboxStore().pending()
                let notes = mergedLocalNotes(state: state, pending: pending)
                guard let note = notes.first(where: { $0.id == noteID }) else {
                    complete(context, response: failure(
                        action: action,
                        requestID: requestID,
                        code: "BW_NATIVE_NOTE_NOT_FOUND",
                        message: "本机笔记索引中没有这条记录"
                    ))
                    return
                }
                var response = baseResponse(action: action, requestID: requestID)
                response["note"] = localNoteDictionary(note)
                complete(context, response: response)
            } catch {
                complete(context, response: bridgeFailure(
                    action: action,
                    requestID: requestID,
                    error: error
                ))
            }

        case "notes.create":
            guard exactKeys(
                message,
                required: ["contract", "action", "requestId", "name", "text"],
                optional: ["file", "page"]
            ),
            let name = message["name"] as? String,
            let text = message["text"] as? String
            else {
                complete(context, response: schemaFailure(
                    action: action,
                    requestID: requestID
                ))
                return
            }
            let sourceFile: String
            if let value = message["file"] {
                guard let value = value as? String else {
                    complete(context, response: schemaFailure(
                        action: action,
                        requestID: requestID
                    ))
                    return
                }
                sourceFile = value
            } else {
                sourceFile = ""
            }
            let sourcePage: Int
            if let value = message["page"] {
                guard let number = value as? NSNumber,
                      number.doubleValue.isFinite,
                      number.doubleValue.rounded() == number.doubleValue,
                      (0...10_000_000).contains(number.doubleValue)
                else {
                    complete(context, response: schemaFailure(
                        action: action,
                        requestID: requestID
                    ))
                    return
                }
                sourcePage = number.intValue
            } else {
                sourcePage = 0
            }
            do {
                let state = try ReaderNativeFeatureStore()
                    .loadLocalNotesState() ?? .unavailable
                guard state.enabled else {
                    var response = baseResponse(
                        action: action,
                        requestID: requestID
                    )
                    response["handled"] = false
                    response["disposition"] = "pi"
                    complete(context, response: response)
                    return
                }
                guard let request = ReaderLocalNoteCreateRequest(
                    id: requestID,
                    name: name,
                    text: text,
                    sourceFile: sourceFile,
                    sourcePage: sourcePage,
                    vaultGeneration: state.vaultGeneration
                ) else {
                    complete(context, response: schemaFailure(
                        action: action,
                        requestID: requestID
                    ))
                    return
                }
                guard state.configured else {
                    complete(context, response: failure(
                        action: action,
                        requestID: requestID,
                        code: "BW_NATIVE_NOTES_FOLDER_REQUIRED",
                        message: "请先在 BWReader App 中选择 Obsidian Vault"
                    ))
                    return
                }
                if let committed = state.notes.first(where: {
                    localNote($0, hasSamePayloadAs: request)
                }) {
                    var response = baseResponse(
                        action: action,
                        requestID: requestID
                    )
                    response["handled"] = true
                    response["disposition"] = "committed"
                    response["notePath"] = committed.fileName
                    response["obsidianURL"] = obsidianURL(
                        folderName: state.folderName,
                        fileName: committed.fileName
                    )
                    response["note"] = localNoteDictionary(committed)
                    complete(context, response: response)
                    return
                }
                let queued = try ReaderLocalNoteOutboxStore().enqueue(request)
                var response = baseResponse(
                    action: action,
                    requestID: requestID
                )
                response["handled"] = true
                response["disposition"] = "queued"
                response["plannedFileName"] = queued.desiredFileName
                response["obsidianURL"] = ""
                response["note"] = localNoteDictionary(queued.projection)
                complete(context, response: response)
            } catch {
                complete(context, response: bridgeFailure(
                    action: action,
                    requestID: requestID,
                    error: error
                ))
            }

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

    private func localNotesStorageDictionary(
        _ state: ReaderLocalNotesSharedState,
        pendingCount: Int,
        totalCount: Int
    ) -> [String: Any] {
        [
            "enabled": state.enabled,
            "configured": state.configured,
            "folderName": state.folderName,
            "updatedAt": state.updatedAtMilliseconds,
            "count": totalCount,
            "pendingCount": pendingCount,
        ]
    }

    private func localNoteSummaryDictionary(
        _ note: ReaderLocalNoteProjection
    ) -> [String: Any] {
        [
            "id": note.id,
            "title": note.title,
            "fileName": note.fileName,
            "preview": note.preview,
            "contentTruncated": note.contentTruncated,
            "sourceFile": note.sourceFile,
            "sourcePage": note.sourcePage,
            "createdAt": note.createdAtMilliseconds,
            "pendingExport": note.pendingExport == true,
        ]
    }

    private func localNoteDictionary(
        _ note: ReaderLocalNoteProjection
    ) -> [String: Any] {
        var value = localNoteSummaryDictionary(note)
        value["content"] = note.content
        return value
    }

    private func mergedLocalNotes(
        state: ReaderLocalNotesSharedState,
        pending: [ReaderLocalNoteCreateRequest]
    ) -> [ReaderLocalNoteProjection] {
        var result: [ReaderLocalNoteProjection] = []
        for note in pending.reversed().map(\.projection) + state.notes {
            if result.contains(where: {
                $0.id == note.id ||
                    ($0.title == note.title &&
                     $0.contentHash == note.contentHash &&
                     $0.sourceFile == note.sourceFile &&
                     $0.sourcePage == note.sourcePage)
            }) {
                continue
            }
            result.append(note)
            if result.count == 50 { break }
        }
        return result
    }

    private func localNote(
        _ note: ReaderLocalNoteProjection,
        hasSamePayloadAs request: ReaderLocalNoteCreateRequest
    ) -> Bool {
        note.title == request.name &&
            note.contentHash == request.contentHash &&
            note.sourceFile == request.sourceFile &&
            note.sourcePage == request.sourcePage
    }

    private func obsidianURL(folderName: String, fileName: String) -> String {
        var components = URLComponents()
        components.scheme = "obsidian"
        components.host = "open"
        components.queryItems = [
            URLQueryItem(name: "vault", value: folderName),
            URLQueryItem(
                name: "file",
                value: URL(fileURLWithPath: fileName)
                    .deletingPathExtension().lastPathComponent
            ),
        ]
        return components.url?.absoluteString ?? ""
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

    private func realtimeFailure(
        action: String,
        requestID: String,
        error: Error
    ) -> [String: Any] {
        failure(
            action: action,
            requestID: requestID,
            code: error is ReaderRealtimeCredentialError
                ? "BW_NATIVE_REALTIME_UNAVAILABLE"
                : "BW_NATIVE_REALTIME_FAILED",
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

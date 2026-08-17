import Combine
import CoreFoundation
import SwiftUI
import UIKit
import WebKit

private let nativeComputerVoiceMessageName = "bwNativeComputerVoice"
private let nativeComputerContextMessageName = "bwNativeComputerContext"
private let nativeAgentVoiceMessageName = "bwNativeAgentVoice"
private let nativePencilInkMessageName = "bwNativePencilInk"
private let nativeLocalNotesMessageName = "bwNativeLocalNotes"
private let nativeAnkiMobileMessageName = "bwNativeAnkiMobile"

struct ReaderLastLocalBookReference: Codable, Equatable, Sendable {
    let libraryID: String
    let bookID: String
}

struct ReaderLastLocalBookStore {
    static let shared = ReaderLastLocalBookStore()

    private let defaults: UserDefaults
    private let key = "reader.localLibrary.lastFinishedBook.v1"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    var hasStoredValue: Bool {
        defaults.object(forKey: key) != nil
    }

    func load() -> ReaderLastLocalBookReference? {
        guard let data = defaults.data(forKey: key),
              let value = try? JSONDecoder().decode(
                ReaderLastLocalBookReference.self,
                from: data
              ),
              UUID(uuidString: value.libraryID) != nil,
              value.bookID.hasPrefix("localbook-"),
              value.bookID.count == 74,
              value.bookID.dropFirst("localbook-".count).allSatisfy({
                $0.isHexDigit && !$0.isUppercase
              }) else {
            return nil
        }
        return value
    }

    func save(libraryID: String, bookID: String) {
        let value = ReaderLastLocalBookReference(
            libraryID: libraryID,
            bookID: bookID
        )
        guard let data = try? JSONEncoder().encode(value) else { return }
        defaults.set(data, forKey: key)
    }

    func clear() {
        defaults.removeObject(forKey: key)
    }
}

private struct ReaderAnkiMobilePendingRecord {
    let gid: String
    let index: Int
    let nonce: String
    let documentIdentity: String
    let expiresAt: Date
    let callbackReceived: Bool
}

private struct ReaderAnkiMobilePendingStore {
    static let shared = ReaderAnkiMobilePendingStore()

    private let defaults: UserDefaults
    private let key = "reader.ankiMobile.pending.v1"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    func load(now: Date = Date()) -> [ReaderAnkiMobilePendingRecord] {
        guard let data = defaults.data(forKey: key) else { return [] }
        guard
            let object = try? JSONSerialization.jsonObject(with: data),
            let envelope = object as? [String: Any],
            Set(envelope.keys) == Set(["version", "records"]),
            let version = envelope["version"] as? NSNumber,
            CFGetTypeID(version) != CFBooleanGetTypeID(),
            version.doubleValue == 1,
            let rows = envelope["records"] as? [[String: Any]],
            rows.count <= 256
        else {
            defaults.removeObject(forKey: key)
            return []
        }

        var records = [ReaderAnkiMobilePendingRecord]()
        var nonces = Set<String>()
        var cards = Set<String>()
        for row in rows {
            guard
                Set(row.keys) == Set([
                    "gid", "index", "nonce", "documentIdentity",
                    "expiresAt", "callbackReceived",
                ]),
                let gid = row["gid"] as? String,
                Self.isValidGID(gid),
                let index = row["index"] as? NSNumber,
                CFGetTypeID(index) != CFBooleanGetTypeID(),
                index.doubleValue == Double(index.intValue),
                (0...255).contains(index.intValue),
                let nonce = row["nonce"] as? String,
                Self.isValidNonce(nonce),
                let documentIdentity = row["documentIdentity"] as? String,
                Self.isValidDocumentIdentity(documentIdentity),
                let expiry = row["expiresAt"] as? NSNumber,
                CFGetTypeID(expiry) != CFBooleanGetTypeID(),
                expiry.doubleValue == Double(expiry.int64Value),
                expiry.int64Value >= 0,
                let callbackReceivedValue = row["callbackReceived"] as? NSNumber,
                CFGetTypeID(callbackReceivedValue) == CFBooleanGetTypeID(),
                nonces.insert(nonce).inserted,
                cards.insert("\(gid):\(index.intValue)").inserted
            else {
                defaults.removeObject(forKey: key)
                return []
            }
            let expiresAt = Date(
                timeIntervalSince1970: Double(expiry.int64Value) / 1_000
            )
            if expiresAt <= now { continue }
            records.append(ReaderAnkiMobilePendingRecord(
                gid: gid,
                index: index.intValue,
                nonce: nonce,
                documentIdentity: documentIdentity,
                expiresAt: expiresAt,
                callbackReceived: callbackReceivedValue.boolValue
            ))
        }
        if records.count != rows.count {
            save(records)
        }
        return records
    }

    func save(_ records: [ReaderAnkiMobilePendingRecord]) {
        guard !records.isEmpty else {
            defaults.removeObject(forKey: key)
            return
        }
        let rows: [[String: Any]] = records.map { record in
            [
                "gid": record.gid,
                "index": record.index,
                "nonce": record.nonce,
                "documentIdentity": record.documentIdentity,
                "expiresAt": Int64(
                    (record.expiresAt.timeIntervalSince1970 * 1_000).rounded()
                ),
                "callbackReceived": record.callbackReceived,
            ]
        }
        let envelope: [String: Any] = ["version": 1, "records": rows]
        guard JSONSerialization.isValidJSONObject(envelope),
              let data = try? JSONSerialization.data(withJSONObject: envelope)
        else { return }
        defaults.set(data, forKey: key)
    }

    private static func isValidGID(_ value: String) -> Bool {
        let suffix = value.dropFirst("card_".count)
        let allowed = CharacterSet(charactersIn: "0123456789abcdef")
        return value == value.lowercased()
            && value.hasPrefix("card_")
            && (4...64).contains(suffix.count)
            && suffix.unicodeScalars.allSatisfy { allowed.contains($0) }
    }

    private static func isValidNonce(_ value: String) -> Bool {
        let allowed = CharacterSet(charactersIn: "0123456789abcdef")
        return value.count == 32
            && value == value.lowercased()
            && value.unicodeScalars.allSatisfy { allowed.contains($0) }
    }

    private static func isValidDocumentIdentity(_ value: String) -> Bool {
        let prefix = "local-book:localbook-"
        guard value.hasPrefix(prefix) else { return false }
        let suffix = value.dropFirst(prefix.count)
        let allowed = CharacterSet(charactersIn: "0123456789abcdef")
        return suffix.count == 64
            && suffix.unicodeScalars.allSatisfy { allowed.contains($0) }
    }
}

private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    weak var delegate: WKScriptMessageHandler?

    init(delegate: WKScriptMessageHandler) {
        self.delegate = delegate
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        delegate?.userContentController(
            userContentController,
            didReceive: message
        )
    }
}

private final class WeakScriptMessageHandlerWithReply:
    NSObject,
    WKScriptMessageHandlerWithReply
{
    weak var delegate: WKScriptMessageHandlerWithReply?

    init(delegate: WKScriptMessageHandlerWithReply) {
        self.delegate = delegate
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        guard let delegate else {
            replyHandler(nil, "本机笔记处理器不可用")
            return
        }
        delegate.userContentController(
            userContentController,
            didReceive: message,
            replyHandler: replyHandler
        )
    }
}

@MainActor
final class ReaderWebViewModel: NSObject, ObservableObject {
    private struct PendingLocalBookNavigation {
        let navigation: WKNavigation
        let bookID: String
        let libraryID: String
        let restorationToken: UUID?
    }

    private struct NativePDFRecoverySettlement {
        let book: ReaderLocalBookRecord
        let access: ReaderLocalBookAccess
        let contentSHA256: String
        let recovery: ReaderNativePDFMutationRecoveryReceipt
    }

    private struct PendingAnkiMobileExport {
        let gid: String
        let index: Int
        let nonce: String
        let documentIdentity: String
        let expiresAt: Date
        var callbackReceived: Bool
        var delivering: Bool
    }

    private enum NativeAgentVoiceCommand {
        case start(NativeAgentVoiceContext)
        case stop
        case speak(String, String?)
        case finishSpeaking
        case cancelSpeaking
    }

    private enum NativePencilAction: String {
        case toggleEraser = "toggle-eraser"
        case toggleSelection = "toggle-selection"
        case showPalette = "show-palette"
    }

    private enum NativePencilGesture: String {
        case doubleTap = "double-tap"
        case squeeze
    }

    enum NativeReaderSettingError: LocalizedError {
        case pageUnavailable
        case invalidTouchDoubleTapAction

        var errorDescription: String? {
            switch self {
            case .pageUnavailable:
                return "Reader 页面尚未准备好"
            case .invalidTouchDoubleTapAction:
                return "触屏双击动作无效"
            }
        }
    }

    let webView: WKWebView
    private let localRuntimeServer: ReaderLocalRuntimeServer?
    private let localRuntimeInitializationError: String?

    @Published private(set) var isLoading = false
    @Published private(set) var loadError: String?
    @Published private(set) var libraryPresentationRequestID: UUID?
    private var nativeComputerVoiceMessageProxy: WeakScriptMessageHandler?
    private var nativeComputerContextMessageProxy: WeakScriptMessageHandler?
    private var nativeAgentVoiceMessageProxy: WeakScriptMessageHandler?
    private var nativePencilInkMessageProxy: WeakScriptMessageHandler?
    private var nativeLocalNotesMessageProxy: WeakScriptMessageHandlerWithReply?
    private var nativeAnkiMobileMessageProxy:
        WeakScriptMessageHandlerWithReply?
    private var nativePiGateway: ReaderNativePiGateway?
    private weak var remoteLibraryCoordinator: ReaderRemoteLibraryCoordinator?
    private var nativePiRemoteLibraryCancellable: AnyCancellable?
    private var nativePiSyncBridge: ReaderNativePiSyncBridge?
    private var nativeRealtimeBridge: ReaderNativeRealtimeBridge?
    private var nativeBookOCRBridge: NativeBookOCRBridge?
    private var nativePDFMutationBridge: ReaderNativePDFMutationBridge?
    var nativeAppPrefsBridge: ReaderNativeAppPrefsBridge?
    private let nativePDFMutationActor = ReaderNativePDFMutationActor()
    private var nativeBookOCRUpdateCancellable: AnyCancellable?
    private var bookUserStateWebAdapter: ReaderBookUserStateWebAdapter?
    private var bookUserStateCoordinator: ReaderBookUserStatePackageCoordinator?
    private let pendingBookUserStateStore =
        ReaderBookUserStatePendingImportStore.shared
    private var bookUserStateNotificationCancellables = Set<AnyCancellable>()
    private var bookUserStateImportTask: Task<Void, Never>?
    private var localPDFContentIdentityTask: Task<Void, Never>?
    private var bookUserStateContextGeneration: UInt64 = 0
    private var currentLocalBook: ReaderLocalBookRecord?
    private var currentLocalBookAccess: ReaderLocalBookAccess?
    private weak var currentLocalLibrary: ReaderLocalLibraryManager?
    private var currentLocalBookContentSHA256: String?
    private var pendingLocalBookNavigation: PendingLocalBookNavigation?
    private var remoteBookNavigationTask: Task<Void, Never>?
    private var localBookRestoreContinuations = [
        UUID: CheckedContinuation<Bool, Never>
    ]()
    private var waitsForInitialBookDecision = true
    private var deferredBookUserStateMessage: (text: String, isError: Bool)?
    private weak var nativeVoiceBridge: NativeVoiceBridge?
    private let nativeAgentVoice = NativeAgentVoiceSession()
    private var nativeAgentVoiceCommandTail: Task<Void, Never>?
    private var nativeAgentVoiceWasReady = false
    private var externalNativeAgentVoice = false
    private var externalNativeAgentControlTask: Task<Void, Never>?
    private var nativePencilInteraction: UIPencilInteraction?
    private var lastNativePencilTapTimestamp: TimeInterval = -1
    private let nativePencilSettings = NativePencilSettings.shared
    let nativePencilInk = NativePencilInkController()
    private var readerForeground = true
    private var readerWasBackgrounded = false
    private var webContentProcessNeedsReload = false
    private let ankiMobilePendingStore = ReaderAnkiMobilePendingStore.shared
    private var pendingAnkiMobileExports = [String: PendingAnkiMobileExport]()

    func bindNativeVisualCaptureCanvas(_ canvas: UIView) {
        localRuntimeServer?.visualCaptureBroker.bind(
            webView: webView,
            pencilCanvas: canvas
        )
    }

    func unbindNativeVisualCaptureCanvas(_ canvas: UIView) {
        localRuntimeServer?.visualCaptureBroker.unbind(
            pencilCanvas: canvas
        )
    }

    override init() {
        do {
            localRuntimeServer = try ReaderLocalRuntimeServer()
            localRuntimeInitializationError = nil
        } catch {
            localRuntimeServer = nil
            localRuntimeInitializationError = error.localizedDescription
        }
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = []

        let preferences = WKWebpagePreferences()
        preferences.allowsContentJavaScript = true
        configuration.defaultWebpagePreferences = preferences

        webView = WKWebView(
            frame: .zero,
            configuration: configuration
        )
        super.init()
        restorePendingAnkiMobileExports()

        let contentController = webView.configuration.userContentController
        let nativeComputerVoiceMessageProxy =
            WeakScriptMessageHandler(delegate: self)
        self.nativeComputerVoiceMessageProxy =
            nativeComputerVoiceMessageProxy
        contentController.add(
            nativeComputerVoiceMessageProxy,
            name: nativeComputerVoiceMessageName
        )
        let nativeComputerContextMessageProxy =
            WeakScriptMessageHandler(delegate: self)
        self.nativeComputerContextMessageProxy =
            nativeComputerContextMessageProxy
        contentController.add(
            nativeComputerContextMessageProxy,
            name: nativeComputerContextMessageName
        )
        let nativeAgentVoiceMessageProxy =
            WeakScriptMessageHandler(delegate: self)
        self.nativeAgentVoiceMessageProxy = nativeAgentVoiceMessageProxy
        contentController.add(
            nativeAgentVoiceMessageProxy,
            name: nativeAgentVoiceMessageName
        )
        let nativePencilInkMessageProxy =
            WeakScriptMessageHandler(delegate: self)
        self.nativePencilInkMessageProxy = nativePencilInkMessageProxy
        contentController.add(
            nativePencilInkMessageProxy,
            name: nativePencilInkMessageName
        )
        let nativeLocalNotesMessageProxy =
            WeakScriptMessageHandlerWithReply(delegate: self)
        self.nativeLocalNotesMessageProxy = nativeLocalNotesMessageProxy
        contentController.addScriptMessageHandler(
            nativeLocalNotesMessageProxy,
            contentWorld: .page,
            name: nativeLocalNotesMessageName
        )
        let nativeAnkiMobileMessageProxy =
            WeakScriptMessageHandlerWithReply(delegate: self)
        self.nativeAnkiMobileMessageProxy = nativeAnkiMobileMessageProxy
        contentController.addScriptMessageHandler(
            nativeAnkiMobileMessageProxy,
            contentWorld: .page,
            name: nativeAnkiMobileMessageName
        )
        if let localRuntimeServer {
            let nativePiGateway = ReaderNativePiGateway(
                webView: webView,
                trustedBaseURL: localRuntimeServer.baseURL,
                piProxyBroker: localRuntimeServer.piProxyBroker
            )
            self.nativePiGateway = nativePiGateway
            contentController.addScriptMessageHandler(
                nativePiGateway,
                contentWorld: .page,
                name: ReaderNativePiGateway.messageName
            )
            let nativePiSyncBridge = ReaderNativePiSyncBridge(
                webView: webView,
                trustedBaseURL: localRuntimeServer.baseURL
            )
            self.nativePiSyncBridge = nativePiSyncBridge
            contentController.addScriptMessageHandler(
                nativePiSyncBridge,
                contentWorld: .page,
                name: ReaderNativePiSyncBridge.messageName
            )
            let nativeRealtimeBridge = ReaderNativeRealtimeBridge(
                webView: webView,
                trustedBaseURL: localRuntimeServer.baseURL
            )
            self.nativeRealtimeBridge = nativeRealtimeBridge
            contentController.addScriptMessageHandler(
                nativeRealtimeBridge,
                contentWorld: .page,
                name: ReaderNativeRealtimeBridge.messageName
            )
            let nativeBookOCRBridge = NativeBookOCRBridge(
                webView: webView,
                trustedBaseURL: localRuntimeServer.baseURL,
                localBookID: "localbook-welcome"
            )
            self.nativeBookOCRBridge = nativeBookOCRBridge
            contentController.addScriptMessageHandler(
                nativeBookOCRBridge,
                contentWorld: .page,
                name: NativeBookOCRBridge.messageName
            )
            let nativePDFMutationBridge = ReaderNativePDFMutationBridge(
                webView: webView,
                trustedBaseURL: localRuntimeServer.baseURL
            ) { [weak self] command in
                guard let self else {
                    throw ReaderNativePDFMutationError.unavailable(
                        "Reader 页面已经关闭"
                    )
                }
                return try await self.handleNativePDFMutation(command)
            }
            self.nativePDFMutationBridge = nativePDFMutationBridge
            contentController.addScriptMessageHandler(
                nativePDFMutationBridge,
                contentWorld: .page,
                name: ReaderNativePDFMutationBridge.messageName
            )
            // 网页设置面板读写原生偏好（白名单）——用户要求把原生 sheet 里那 12 个
            // Section 并进我们自己的设置 tab，这是它需要的唯一新通道。
            let nativeAppPrefsBridge = ReaderNativeAppPrefsBridge()
            self.nativeAppPrefsBridge = nativeAppPrefsBridge
            contentController.addScriptMessageHandler(
                nativeAppPrefsBridge,
                contentWorld: .page,
                name: ReaderNativeAppPrefsBridge.messageName
            )
            nativeBookOCRUpdateCancellable = NativeBookOCRManager.shared
                .$lastUpdate
                .compactMap { $0 }
                .sink { [weak nativeBookOCRBridge, weak webView] update in
                    guard let nativeBookOCRBridge, let webView else { return }
                    Task { @MainActor in
                        await nativeBookOCRBridge.sendUpdate(
                            update,
                            to: webView
                        )
                    }
                }
            do {
                bookUserStateWebAdapter = try ReaderBookUserStateWebAdapter(
                    webView: webView,
                    trustedBaseURL: localRuntimeServer.baseURL,
                    localBookId: "localbook-welcome"
                )
                bookUserStateCoordinator = ReaderBookUserStatePackageCoordinator(
                    baselineStore: try ReaderBookUserStateBaselineStore()
                )
            } catch {
                // A downloaded package remains in native staging. Opening the
                // book will show the initialization error and can retry after
                // a later App launch; nothing is silently discarded here.
                bookUserStateWebAdapter = nil
                bookUserStateCoordinator = nil
            }
        }
        configureBookUserStateNotifications()
        contentController.addUserScript(WKUserScript(
            source: """
            (() => {
              window.__BW_NATIVE_COMPUTER_VOICE__ = true;
              window.__BW_NATIVE_READER_FOREGROUND__ = true;
              window.__BW_NATIVE_COMPUTER_VOICE_APP_VERSION__ =
                "\(nativeAppBuildVersion)";

              const selector = "#asst-computer, #vc-top-computer";
              let latest = {
                active: false,
                busy: false,
                sessionId: null,
                title: "电脑语音未连接"
              };
              const applyButton = (button) => {
                button.classList.toggle("on", latest.active === true);
                button.classList.toggle(
                  "connecting",
                  latest.busy === true && latest.active !== true
                );
                button.classList.remove("speaking");
                button.title = latest.title;
                button.setAttribute("aria-label", latest.title);
                button.setAttribute(
                  "aria-pressed",
                  latest.active === true ? "true" : "false"
                );
                button.setAttribute(
                  "aria-busy",
                  latest.busy === true ? "true" : "false"
                );
                button.disabled = latest.busy === true;
              };
              const applyAll = () => {
                document.querySelectorAll(selector).forEach(applyButton);
              };
              window.__bwNativeComputerVoiceApplyState = (value) => {
                const state = value && typeof value === "object" ? value : {};
                latest = {
                  active: state.active === true,
                  busy: state.busy === true,
                  sessionId: typeof state.sessionId === "string"
                    ? state.sessionId
                    : null,
                  title: String(state.title || "电脑语音未连接")
                };
                window.__BW_NATIVE_COMPUTER_VOICE_STATE__ = {
                  active: latest.active,
                  busy: latest.busy,
                  sessionId: latest.sessionId
                };
                window.dispatchEvent(new CustomEvent(
                  "bw-native-computer-voice-state",
                  { detail: window.__BW_NATIVE_COMPUTER_VOICE_STATE__ }
                ));
                applyAll();
              };

              new MutationObserver((records) => {
                const addedButton = records.some((record) =>
                  Array.from(record.addedNodes || []).some((node) =>
                    node?.nodeType === 1 && (
                      node.matches?.(selector) ||
                      node.querySelector?.(selector)
                    )
                  )
                );
                if (addedButton) applyAll();
              }).observe(document, { childList: true, subtree: true });
            })();
            """,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
        contentController.addUserScript(WKUserScript(
            source: #"""
            (() => {
              if (window.__bwNativeRealtime) return;
              if (location.origin !== "http://127.0.0.1:43129") return;
              const handler = window.webkit?.messageHandlers?.bwNativeRealtime;
              if (!handler || typeof handler.postMessage !== "function") return;
              const request = (payload) => handler.postMessage(payload);
              Object.defineProperty(window, "__bwNativeRealtime", {
                configurable: false,
                enumerable: false,
                writable: false,
                value: Object.freeze({ request })
              });
              Object.defineProperty(window, "__BW_NATIVE_OPENAI_REALTIME__", {
                configurable: false,
                enumerable: false,
                writable: false,
                value: true
              });
            })();
            """#,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
        contentController.addUserScript(WKUserScript(
            source: #"""
            (() => {
              if (window.__bwNativeAnkiMobile) return;
              if (location.origin !== "http://127.0.0.1:43129") return;
              const handler = window.webkit?.messageHandlers?.bwNativeAnkiMobile;
              if (!handler || typeof handler.postMessage !== "function") return;
              const request = (payload) => handler.postMessage(payload);
              Object.defineProperty(window, "__bwNativeAnkiMobile", {
                configurable: false,
                enumerable: false,
                writable: false,
                value: Object.freeze({ request })
              });
              window.dispatchEvent(new CustomEvent(
                "bw-native-anki-mobile-capability",
                { detail: { available: true } }
              ));
            })();
            """#,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
        contentController.addUserScript(WKUserScript(
            source: #"""
            (() => {
              if (window.__BW_NATIVE_LOCAL_NOTES_FETCH__) return;
              const localReader = location.origin ===
                "http://127.0.0.1:43129";
              if (!localReader) return;
              const handler = window.webkit?.messageHandlers?.bwNativeLocalNotes;
              if (!handler || typeof handler.postMessage !== "function") return;
              const originalFetch = window.fetch.bind(window);
              const targetPath = "/pdf/api/to-note";
              const requestURL = (input) => {
                try {
                  return new URL(
                    typeof input === "string" || input instanceof URL
                      ? String(input)
                      : String(input?.url || ""),
                    location.href
                  );
                } catch (_) {
                  return null;
                }
              };
              window.fetch = (input, init) => {
                const url = requestURL(input);
                const method = String(
                  init?.method || (input instanceof Request ? input.method : "GET")
                ).toUpperCase();
                if (!url || url.origin !== location.origin ||
                    url.pathname !== targetPath || method !== "POST") {
                  return originalFetch(input, init);
                }
                return (async () => {
                  let bodyText = typeof init?.body === "string" ? init.body : "";
                  if (!bodyText && input instanceof Request) {
                    bodyText = await input.clone().text();
                  }
                  let payload;
                  try {
                    payload = JSON.parse(bodyText || "{}");
                  } catch (_) {
                    return originalFetch(input, init);
                  }
                  let result;
                  try {
                    result = await handler.postMessage({
                      action: "create",
                      payload
                    });
                  } catch (error) {
                    throw new Error(
                      "本机笔记桥不可用：" + String(error?.message || error)
                    );
                  }
                  if (!result || result.handled !== true) {
                    return originalFetch(input, init);
                  }
                  const response = result.response &&
                    typeof result.response === "object"
                    ? result.response
                    : { ok: false, error: "本机笔记响应无效" };
                  return new Response(JSON.stringify(response), {
                    status: Number.isInteger(result.status) ? result.status : 200,
                    headers: { "Content-Type": "application/json; charset=utf-8" }
                  });
                })();
              };
              window.__BW_NATIVE_LOCAL_NOTES_FETCH__ = true;
            })();
            """#,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
        contentController.addUserScript(WKUserScript(
            source: """
            (() => {
              const localReader = location.origin ===
                "http://127.0.0.1:43129";
              if (!localReader) return;
              window.__BW_NATIVE_AGENT_VOICE__ = true;
              window.__bwNativeAgentVoiceDispatch = (value) => {
                const detail = value && typeof value === "object" ? value : {};
                window.dispatchEvent(new CustomEvent(
                  "bw-native-agent-voice-event",
                  { detail }
                ));
              };
              window.dispatchEvent(new CustomEvent(
                "bw-native-agent-voice-capability",
                { detail: { available: true } }
              ));
            })();
            """,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
        contentController.addUserScript(WKUserScript(
            source: """
            (() => {
              if (window.__BW_NATIVE_PENCIL__) return;
              window.__BW_NATIVE_PENCIL__ = true;
              // The App owns Apple Pencil sampling through PencilKit. The
              // web/PWA ink engine remains intact and is used whenever this
              // App-only flag is absent.
              window.__BW_NATIVE_PENCILKIT_INK__ = true;

              // App-native PencilKit is the only pen control surface here.
              // The old controls and pointer engine remain untouched in PWA
              // and non-Apple clients, where this flag/style do not exist.
              const style = document.createElement("style");
              style.id = "bw-native-pencilkit-style";
              style.textContent =
                "#ink-fab,#ink-toolbar,#ep-ink-btn,#ep-ink-toolbar{" +
                "display:none!important}" +
                // App 里这两个网页按钮是多余的：全屏在 App 里没有"腾出浏览器 chrome"
                // 可言（顶栏本来就是我们自己的），而「⬇ 本机」那条离线缓存链在 App 内
                // 因缺 pwa-cache-identity.js 本就半死——真正的本机书走 localbook: +
                // ReaderLocalRuntimeServer，跟这个按钮无关。
                // 只在 App 内藏起来而不是删掉：PWA 和浏览器仍然需要它们。
                "#fs-toggle,#lb-btn{display:none!important}" +
                // ReaderRootView owns two 44pt buttons in the upper-right.
                // Keep the web fullscreen recovery control outside their hit box.
                "#fs-restore{right:calc(env(safe-area-inset-right,0px) + 112px)!important}" +
                // 同一对原生按钮也压在右侧抽屉的顶部 —— 抽屉是 top:0/right:0 全高的，
                // 它头部那一排按钮正好落在按钮的命中区下面，点不到（用户实测）。
                // #fs-restore 早就做了水平避让，抽屉这边一直漏着；这里补垂直避让：
                // 44pt 按钮 + 上边距，取 52px。
                "#ep-side{padding-top:calc(env(safe-area-inset-top,0px) + 52px)!important}";
              (document.head || document.documentElement).appendChild(style);

              const dispatchOverride = (detail) => {
                try {
                  const event = new CustomEvent("bw-native-pencil-action", {
                    detail,
                    cancelable: true
                  });
                  return window.dispatchEvent(event) === false;
                } catch (error) {
                  return false;
                }
              };

              const toggleEraser = () => {
                // A note being edited owns the pencil before the page layer.
                const noteButton = document.querySelector(
                  '.rc-note.rc-note-editing .rc-note-tool[data-t="eraser"]'
                );
                if (noteButton) {
                  noteButton.click();
                  return true;
                }

                const toolbars = [
                  ["#ink-toolbar", "data-tool"],
                  ["#ep-ink-toolbar", "data-itool"],
                  [".bw-ink-tools", "data-tool"]
                ];
                for (const [selector, attribute] of toolbars) {
                  const toolbar = document.querySelector(selector);
                  if (!toolbar) continue;
                  const eraser = toolbar.querySelector(
                    `[${attribute}="eraser"]`
                  );
                  const pen = toolbar.querySelector(`[${attribute}="pen"]`);
                  if (!eraser) continue;
                  const target = eraser.classList.contains("on") && pen
                    ? pen
                    : eraser;
                  target.click();
                  return true;
                }
                return false;
              };

              const toggleSelection = () => {
                const toolbars = [
                  ["#ink-toolbar", "data-tool"],
                  ["#ep-ink-toolbar", "data-itool"],
                  [".bw-ink-tools", "data-tool"]
                ];
                for (const [selector, attribute] of toolbars) {
                  const toolbar = document.querySelector(selector);
                  if (!toolbar) continue;
                  const selection = toolbar.querySelector(
                    `[${attribute}="selection"], [${attribute}="region"]`
                  );
                  const pen = toolbar.querySelector(`[${attribute}="pen"]`);
                  if (!selection) continue;
                  const target = selection.classList.contains("on") && pen
                    ? pen
                    : selection;
                  target.click();
                  return true;
                }
                return false;
              };

              const showPalette = () => {
                if (document.querySelector(
                  ".rc-note.rc-note-editing, #ink-toolbar.show, " +
                  "#ep-ink-toolbar.show, .bw-ink-tools.show"
                )) return true;

                if (typeof window.__bwWebInk?.set === "function") {
                  window.__bwWebInk.set(true);
                  return true;
                }
                try {
                  if (window.RC?.actions?.has?.("ink.toggle")) {
                    const result = window.RC.actions.run("ink.toggle", {});
                    result?.catch?.(() => {});
                    return true;
                  }
                } catch (error) {}
                if (typeof window.inkToggle === "function") {
                  window.inkToggle();
                  return true;
                }
                return false;
              };

              window.__bwNativePencilPerform = (input) => {
                const detail = input && typeof input === "object" ? input : {};
                let handled = dispatchOverride(detail);
                if (!handled && detail.action === "toggle-eraser") {
                  handled = toggleEraser();
                } else if (!handled && detail.action === "toggle-selection") {
                  handled = toggleSelection();
                } else if (!handled && detail.action === "show-palette") {
                  handled = showPalette();
                }
                window.__BW_NATIVE_PENCIL_LAST_ACTION__ = {
                  ...detail,
                  handled,
                  at: Date.now()
                };
                if (!handled) {
                  try { window.RC?.toast?.("当前页面尚未准备好绘图工具"); }
                  catch (error) {}
                }
                return handled;
              };
            })();
            """,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))

        let nativePencilInteraction = UIPencilInteraction()
        nativePencilInteraction.delegate = self
        nativePencilInteraction.isEnabled = true
        webView.addInteraction(nativePencilInteraction)
        self.nativePencilInteraction = nativePencilInteraction

        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsLinkPreview = false
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        nativeAgentVoice.delegate = self
    }

    /// Binds the App-owned Pi catalog to the local reading shell. The gateway
    /// receives only digest-verified mappings from the currently open local
    /// library. `scope: current` routes still see one mapping; the bounded set
    /// exists solely for manifest routes whose policy explicitly says catalog.
    func bind(remoteLibrary: ReaderRemoteLibraryCoordinator) {
        guard remoteLibraryCoordinator !== remoteLibrary else { return }
        remoteLibraryCoordinator = remoteLibrary
        nativePiRemoteLibraryCancellable = Publishers.CombineLatest3(
            remoteLibrary.$books,
            remoteLibrary.$remoteToLocalID,
            remoteLibrary.$localDigests
        ).sink { [weak self] _, _, _ in
            Task { @MainActor [weak self] in
                self?.refreshNativePiRemoteBookBinding()
            }
        }
        refreshNativePiRemoteBookBinding()
    }

    private func refreshNativePiRemoteBookBinding() {
        guard let currentLocalBook,
              let currentLocalLibrary,
              let remoteLibraryCoordinator else {
            nativePiGateway?.updateTrustedRemoteBookBindings(
                current: nil,
                catalog: []
            )
            return
        }
        let currentBinding = remoteLibraryCoordinator
            .verifiedNativeRemoteBookBinding(
                for: currentLocalBook,
                localContentSHA256: currentLocalBookContentSHA256
            )
        let catalogBindings = currentLocalLibrary.books.compactMap { book in
            remoteLibraryCoordinator.verifiedNativeRemoteBookBinding(
                for: book,
                localContentSHA256: book.contentSha256
            )
        }
        nativePiGateway?.updateTrustedRemoteBookBindings(
            current: currentBinding,
            catalog: catalogBindings
        )
    }

    /// Legacy assistant, review and EPUB-conversion actions navigate through
    /// `/pdf/view?file=...&page=...` (or `/pdf/epub/view`). Those paths belong to
    /// the Pi/PWA renderer, so allowing them on loopback would replace the
    /// App-owned book with a 404 page. Preserve the old interface by resolving
    /// the exact catalog identity, reusing a digest-verified local copy when
    /// possible, otherwise downloading it, then opening the native shell at the
    /// requested location.
    private func takeOverRemoteBookNavigation(
        _ url: URL,
        sourceURL: URL?
    ) -> Bool {
        guard currentLocalBook != nil,
              isTrustedReaderURL(sourceURL),
              url.scheme?.lowercased() == "http",
              url.host?.lowercased() == ReaderLocalRuntimeServer.host,
              url.port == Int(ReaderLocalRuntimeServer.port),
              ["/pdf/view", "/pdf/epub/view"].contains(url.path),
              let components = URLComponents(
                url: url,
                resolvingAgainstBaseURL: false
              ) else {
            return false
        }
        let queryItems = components.queryItems ?? []
        guard !queryItems.isEmpty,
              Set(queryItems.map(\.name)).isSubset(
                of: Set(["file", "page"])
              ),
              queryItems.filter({ $0.name == "file" }).count == 1,
              queryItems.filter({ $0.name == "page" }).count <= 1 else {
            return false
        }
        let fileValues = queryItems
            .filter { $0.name == "file" }
            .compactMap(\.value)
        guard fileValues.count == 1,
              Self.isSafeRemoteLibraryRelativePath(fileValues[0]) else {
            return false
        }
        let initialPage: Int
        if let rawPage = queryItems.first(where: { $0.name == "page" })?.value {
            guard let page = Int(rawPage),
                  (1...10_000_000).contains(page) else { return false }
            initialPage = page
        } else {
            initialPage = 1
        }
        guard url.fragment == nil || url.fragment?.isEmpty == true else {
            return false
        }

        let remoteRelativePath = fileValues[0]
        guard let sourceBookID = currentLocalBook?.id,
              let localLibrary = currentLocalLibrary,
              let remoteLibraryCoordinator else {
            showBookUserStateMessage(
                "无法打开目标书籍：本机书库或 Pi 书库尚未连接",
                isError: true
            )
            return true
        }

        remoteBookNavigationTask?.cancel()
        let targetDisplayName = remoteRelativePath
            .split(separator: "/")
            .last
            .map(String.init) ?? "目标书籍"
        showBookUserStateMessage(
            "正在从书库打开《\(targetDisplayName)》…",
            isError: false
        )
        remoteBookNavigationTask = Task { @MainActor [weak self] in
            guard let self else { return }
            let cookies = await self.remoteLibraryCookies()
            guard !Task.isCancelled,
                  self.currentLocalBook?.id == sourceBookID else { return }
            var remoteBook = remoteLibraryCoordinator.books.first {
                $0.rel == remoteRelativePath
            }
            if remoteBook == nil {
                await remoteLibraryCoordinator.refresh(
                    cookies: cookies,
                    localLibrary: localLibrary
                )
                remoteBook = remoteLibraryCoordinator.books.first {
                    $0.rel == remoteRelativePath
                }
            }
            guard !Task.isCancelled,
                  self.currentLocalBook?.id == sourceBookID else { return }
            self.refreshNativePiRemoteBookBinding()
            guard let remoteBook,
                  ["pdf", "epub"].contains(remoteBook.kind.lowercased()) else {
                self.showBookUserStateMessage(
                    remoteLibraryCoordinator.errorMessage
                        ?? "Pi 书库中没有找到目标 PDF/EPUB",
                    isError: true
                )
                return
            }
            let localBook: ReaderLocalBookRecord
            if let localID = remoteLibraryCoordinator.localBookID(
                for: remoteBook
            ),
               let candidate = localLibrary.books.first(where: {
                   $0.id == localID
               }),
               remoteLibraryCoordinator.verifiedNativeRemoteBookBinding(
                   for: candidate,
                   localContentSHA256: candidate.contentSha256
               )?.remoteRelativePath == remoteRelativePath {
                localBook = candidate
            } else {
                guard let downloaded = await remoteLibraryCoordinator.download(
                    remoteBook,
                    localLibrary: localLibrary,
                    cookies: cookies
                ) else {
                    guard !Task.isCancelled else { return }
                    self.showBookUserStateMessage(
                        remoteLibraryCoordinator.errorMessage
                            ?? "目标书籍下载失败，请稍后再试",
                        isError: true
                    )
                    return
                }
                localBook = downloaded
                Task { @MainActor in
                    await remoteLibraryCoordinator.fetchAndStageUserState(
                        for: remoteBook,
                        localBook: downloaded,
                        cookies: cookies
                    )
                }
            }
            guard !Task.isCancelled,
                  self.currentLocalBook?.id == sourceBookID else { return }
            if !(await self.openLocalBook(
                localBook,
                library: localLibrary,
                restorationToken: nil,
                initialPage: initialPage
            )) {
                self.showBookUserStateMessage(
                    "目标书籍已在本机，但原生阅读器未能打开它",
                    isError: true
                )
            }
        }
        return true
    }

    /// The shared PDF/EPUB chrome still expresses “back to shelf” as `/pdf/`.
    /// In the native product the shelf is SwiftUI, not a loopback web route.
    /// Consume that legacy navigation here so it cannot escape to Safari or a
    /// loopback 404 while preserving the button's original user-visible action.
    private func takeOverLibraryNavigation(
        _ url: URL,
        sourceURL: URL?
    ) -> Bool {
        guard currentLocalBook != nil,
              isTrustedReaderURL(sourceURL),
              url.scheme?.lowercased() == "http",
              url.host?.lowercased() == ReaderLocalRuntimeServer.host,
              url.port == Int(ReaderLocalRuntimeServer.port),
              url.path == "/pdf/",
              url.query == nil,
              url.fragment == nil else {
            return false
        }
        libraryPresentationRequestID = UUID()
        return true
    }

    private static func isSafeRemoteLibraryRelativePath(_ value: String) -> Bool {
        guard !value.isEmpty, value.utf8.count <= 2_048,
              !value.hasPrefix("/"), !value.contains("\\"),
              !value.contains("?"), !value.contains("#"),
              !value.unicodeScalars.contains(where: {
                $0.value < 0x20 || $0.value == 0x7f
              }) else {
            return false
        }
        let segments = value.split(
            separator: "/",
            omittingEmptySubsequences: false
        )
        return !segments.isEmpty && segments.allSatisfy {
            !$0.isEmpty && $0 != "." && $0 != ".."
        }
    }

    func loadIfNeeded() {
        guard !waitsForInitialBookDecision, webView.url == nil else {
            return
        }
        reload()
    }

    func finishInitialBookDecision() {
        waitsForInitialBookDecision = false
        loadIfNeeded()
    }

    private func configureBookUserStateNotifications() {
        NotificationCenter.default.publisher(
            for: .readerBookUserStatePendingImportStaged
        )
        .sink { [weak self] notification in
            Task { @MainActor in
                guard let self,
                      self.notificationMatchesCurrentLocalBook(notification)
                else { return }
                self.schedulePendingBookUserStateImport()
            }
        }
        .store(in: &bookUserStateNotificationCancellables)

        NotificationCenter.default.publisher(
            for: .readerBookUserStatePendingImportFailed
        )
        .sink { [weak self] notification in
            Task { @MainActor in
                guard let self,
                      self.notificationMatchesCurrentLocalBook(notification),
                      let message = notification.userInfo?[
                        ReaderBookUserStatePendingImportStore
                            .notificationMessageKey
                      ] as? String else { return }
                self.showBookUserStateMessage(message, isError: true)
            }
        }
        .store(in: &bookUserStateNotificationCancellables)

        NotificationCenter.default.publisher(
            for: .readerBookUserStatePendingImportNotice
        )
        .sink { [weak self] notification in
            Task { @MainActor in
                guard let self,
                      self.notificationMatchesCurrentLocalBook(notification),
                      let message = notification.userInfo?[
                        ReaderBookUserStatePendingImportStore
                            .notificationMessageKey
                      ] as? String else { return }
                self.showBookUserStateMessage(message, isError: false)
            }
        }
        .store(in: &bookUserStateNotificationCancellables)
    }

    private func notificationMatchesCurrentLocalBook(
        _ notification: Notification
    ) -> Bool {
        guard let localBookId = notification.userInfo?[
            ReaderBookUserStatePendingImportStore.notificationLocalBookIdKey
        ] as? String else { return false }
        return localBookId == currentLocalBook?.id
    }

    private func resetBookUserStateContext(baseURL: URL) {
        bookUserStateImportTask?.cancel()
        bookUserStateImportTask = nil
        localPDFContentIdentityTask?.cancel()
        localPDFContentIdentityTask = nil
        bookUserStateContextGeneration &+= 1
        currentLocalBook = nil
        currentLocalBookAccess = nil
        currentLocalLibrary = nil
        currentLocalBookContentSHA256 = nil
        deferredBookUserStateMessage = nil
        try? bookUserStateWebAdapter?.updateTrustedContext(
            baseURL: baseURL,
            localBookId: "localbook-welcome"
        )
    }

    private func schedulePendingBookUserStateImport() {
        guard bookUserStateImportTask == nil,
              currentLocalBook != nil,
              currentLocalLibrary != nil else { return }
        let generation = bookUserStateContextGeneration
        bookUserStateImportTask = Task { @MainActor [weak self] in
            guard let self else { return }
            defer {
                if self.bookUserStateContextGeneration == generation {
                    self.bookUserStateImportTask = nil
                }
            }
            do {
                try await self.importPendingBookUserState(
                    generation: generation
                )
            } catch is CancellationError {
                // A navigation/context change is expected. The package remains
                // staged and the next matching book load will retry it.
            } catch {
                self.showBookUserStateMessage(
                    "Pi 用户数据未导入：\(error.localizedDescription)；重新打开本书可重试",
                    isError: true
                )
            }
        }
    }

    private func importPendingBookUserState(
        generation: UInt64
    ) async throws {
        guard let localBook = currentLocalBook else {
            throw CancellationError()
        }
        var pending = try await pendingBookUserStateStore.load(
            localBookId: localBook.id
        )
        let fetchIntent = try await pendingBookUserStateStore.loadFetchIntent(
            localBookId: localBook.id
        )
        guard pending != nil || fetchIntent != nil else { return }
        try await waitForBookUserStateAPI(
            localBookId: localBook.id,
            generation: generation
        )
        let digest = try await currentLocalContentDigest(
            localBookId: localBook.id,
            generation: generation
        )

        if pending == nil, let fetch = fetchIntent {
            guard fetch.contentSha256 == digest else {
                throw ReaderBookUserStatePendingImportError
                    .contentVersionMismatch
            }
            do {
                let payload = try await ReaderRemoteLibraryClient.shared
                    .userStatePackage(
                        bookId: fetch.remoteBookId,
                        contentSha256: fetch.contentSha256,
                        cookies: await remoteLibraryCookies()
                    )
                guard let payload else {
                    try await pendingBookUserStateStore
                        .removeFetchIntent(fetch)
                    showBookUserStateMessage(
                        "Pi 上这本书暂无用户附属数据；本机内容未改变",
                        isError: false
                    )
                    return
                }
                try await pendingBookUserStateStore.stage(
                    payload: payload,
                    localBookId: fetch.localBookId,
                    remoteBookId: fetch.remoteBookId,
                    contentSha256: fetch.contentSha256
                )
                pending = try await pendingBookUserStateStore.load(
                    localBookId: localBook.id
                )
            } catch {
                try? await pendingBookUserStateStore.markFetchFailure(
                    localBookId: localBook.id,
                    message: error.localizedDescription
                )
                throw error
            }
        }

        guard let pending else { return }
        guard pending.contentSha256 == digest else {
            throw ReaderBookUserStatePendingImportError.contentVersionMismatch
        }
        guard generation == bookUserStateContextGeneration,
              currentLocalBook?.id == pending.localBookId,
              let coordinator = bookUserStateCoordinator,
              let adapter = bookUserStateWebAdapter else {
            throw ReaderBookUserStateWebAdapterError.unavailable
        }
        try await verifyCurrentBookUserStateAccountScope(
            pending,
            generation: generation
        )
        let prepared = try await coordinator.prepareImport(
            packageData: pending.packageData,
            accountScopeDigest: pending.accountScopeDigest,
            localBookId: pending.localBookId,
            expectedRemoteBookId: pending.remoteBookId,
            expectedContentSha256: pending.contentSha256,
            localIsNewOrEmpty: true,
            applier: adapter
        )

        // Re-check the local file after the asynchronous snapshot. A path may
        // keep the same opaque id after external replacement; such a change
        // must never receive the old book's notes or ink.
        let beforeCommitDigest = try await currentLocalContentDigest(
            localBookId: pending.localBookId,
            generation: generation
        )
        guard beforeCommitDigest == pending.contentSha256 else {
            throw ReaderBookUserStatePendingImportError.contentVersionMismatch
        }
        try await verifyCurrentBookUserStateAccountScope(
            pending,
            generation: generation
        )
        let plan = try await coordinator.commitImport(
            prepared,
            applier: adapter
        )
        try await pendingBookUserStateStore.remove(pending)
        showBookUserStatePlan(plan)
    }

    /// Re-authenticates the native pending hand-off against the currently
    /// signed-in Pi session. The package's account digest is never accepted as
    /// proof of the current session merely because it was valid at download.
    /// This is intentionally called both before local snapshot planning and
    /// immediately before the first mutating renderer transaction.
    private func verifyCurrentBookUserStateAccountScope(
        _ pending: ReaderBookUserStatePendingImport,
        generation: UInt64
    ) async throws {
        guard generation == bookUserStateContextGeneration,
              currentLocalBook?.id == pending.localBookId else {
            throw CancellationError()
        }
        let cookies = await remoteLibraryCookies()
        guard !cookies.isEmpty else {
            throw ReaderBookUserStatePendingImportError
                .authenticationUnavailable
        }
        let current: ReaderBookUserStateRemotePayload
        do {
            guard let payload = try await ReaderRemoteLibraryClient.shared
                .userStatePackage(
                    bookId: pending.remoteBookId,
                    contentSha256: pending.contentSha256,
                    cookies: cookies
                ) else {
                throw ReaderBookUserStatePendingImportError
                    .accountScopeUnavailable
            }
            current = payload
        } catch ReaderRemoteLibraryError.server(let status, _)
            where status == 401 || status == 403 {
            throw ReaderBookUserStatePendingImportError
                .authenticationUnavailable
        }
        guard generation == bookUserStateContextGeneration,
              currentLocalBook?.id == pending.localBookId else {
            throw CancellationError()
        }
        guard current.accountScopeDigest == pending.accountScopeDigest else {
            throw ReaderBookUserStatePendingImportError.accountScopeChanged
        }
    }

    private func waitForBookUserStateAPI(
        localBookId: String,
        generation: UInt64
    ) async throws {
        for _ in 0..<40 {
            try Task.checkCancellation()
            guard generation == bookUserStateContextGeneration,
                  currentLocalBook?.id == localBookId else {
                throw CancellationError()
            }
            if isLocalRuntimeURL(webView.url),
               let ready = try? await webView.callAsyncJavaScript(
                """
                return window.top === window && Boolean(
                  window.BWReaderRuntime?.nativeLocalRuntime?.bookUserState &&
                  typeof window.BWReaderRuntime.nativeLocalRuntime
                    .bookUserState.snapshotHeaders === "function" &&
                  typeof window.BWReaderRuntime.nativeLocalRuntime
                    .bookUserState.applyAtomically === "function"
                );
                """,
                arguments: [:],
                in: nil,
                contentWorld: .page
               ) as? Bool,
               ready {
                return
            }
            try await Task.sleep(nanoseconds: 250_000_000)
        }
        throw ReaderBookUserStateWebAdapterError.unavailable
    }

    private func currentLocalContentDigest(
        localBookId: String,
        generation: UInt64
    ) async throws -> String {
        guard generation == bookUserStateContextGeneration,
              let library = currentLocalLibrary,
              let original = currentLocalBook,
              original.id == localBookId else {
            throw CancellationError()
        }
        let current = library.books.first(where: {
            $0.id == localBookId
                && $0.relativePath == original.relativePath
        }) ?? original
        let digest = try await library.ensureContentSHA256(for: current)
        guard generation == bookUserStateContextGeneration,
              currentLocalBook?.id == localBookId else {
            throw CancellationError()
        }
        currentLocalBook = library.books.first(where: {
            $0.id == localBookId
                && $0.relativePath == original.relativePath
        }) ?? current
        currentLocalBookContentSHA256 = digest
        if let baseURL = localRuntimeServer?.baseURL {
            nativeBookOCRBridge?.updateTrustedContext(
                baseURL: baseURL,
                localBookID: localBookId,
                expectedContentSHA256: digest,
                localBookAccess: currentLocalBookAccess
            )
        }
        return digest
    }

    /// Local PDFKit can paint the first page without reading the whole file.
    /// Keep that fast path: only establish a missing full-content identity once
    /// the shell is visible, then invalidate the passive page-text reads so the
    /// already rendered pages build their interactive char layers.
    private func scheduleLocalPDFContentIdentity() {
        guard localPDFContentIdentityTask == nil,
              currentLocalBook?.format == .pdf,
              currentLocalBookContentSHA256 == nil,
              let localBookID = currentLocalBook?.id else { return }
        let generation = bookUserStateContextGeneration
        localPDFContentIdentityTask = Task { @MainActor [weak self] in
            guard let self else { return }
            defer {
                if self.bookUserStateContextGeneration == generation {
                    self.localPDFContentIdentityTask = nil
                }
            }
            do {
                let digest = try await self.currentLocalContentDigest(
                    localBookId: localBookID,
                    generation: generation
                )
                try Task.checkCancellation()
                guard self.bookUserStateContextGeneration == generation,
                      self.currentLocalBook?.id == localBookID,
                      self.isLocalRuntimeURL(self.webView.url),
                      let bridge = self.nativeBookOCRBridge else { return }
                self.refreshNativePiRemoteBookBinding()
                let status = await NativeBookOCRManager.shared.readyStatus(
                    for: localBookID,
                    expectedContentSHA256: digest
                )
                await bridge.sendUpdate(
                    NativeBookOCRUpdate(
                        contract: NativeBookOCRUpdate.contract,
                        bookID: localBookID,
                        page: nil,
                        status: status
                    ),
                    to: self.webView
                )
            } catch is CancellationError {
                return
            } catch {
                guard self.bookUserStateContextGeneration == generation,
                      self.currentLocalBook?.id == localBookID else { return }
                self.showBookUserStateMessage(
                    "PDF 文字层初始化失败：\(error.localizedDescription)",
                    isError: true
                )
            }
        }
    }

    /// Completes the native half of the legacy PDF insert-page contract. The
    /// JavaScript runtime owns its IndexedDB page-anchor transaction; Swift
    /// owns the actual PDF bytes and does not finalize its backup until the
    /// replacement has been rescanned, reopened and rebound to this WebView.
    private func settleNativePDFMutation(
        book: ReaderLocalBookRecord,
        access: ReaderLocalBookAccess,
        library: ReaderLocalLibraryManager,
        expectedTicket: String? = nil,
        expectedOldContentSHA256: String? = nil,
        expectedStagedContentSHA256: String? = nil,
        requiresReceipt: Bool = false,
        outgoingRollback: Bool = false
    ) async throws -> NativePDFRecoverySettlement? {
        guard book.format == .pdf else { return nil }
        let identity = try await nativePDFMutationActor.recoveryIdentity(
            book: access
        )
        guard identity != nil
                || expectedTicket != nil
                || expectedOldContentSHA256 != nil
                || expectedStagedContentSHA256 != nil
                || requiresReceipt else {
            return nil
        }
        if let identity, let expectedTicket,
           expectedTicket != identity.ticket {
            throw ReaderNativePDFMutationError.commitFailed(
                "网页与原生 PDF 恢复票据不一致"
            )
        }

        let ocrLease: NativeBookOCRPDFMutationLease?
        if let identity {
            ocrLease = try await NativeBookOCRManager.shared
                .beginPDFMutationLease(
                    bookID: book.id,
                    expectedOldDigest: identity.ocrLease.oldContentSHA256,
                    token: identity.ocrLease.token
                )
        } else {
            ocrLease = nil
        }

        let recovery: ReaderNativePDFMutationRecoveryReceipt
        if outgoingRollback {
            guard let identity else {
                throw ReaderNativePDFMutationError.commitFailed(
                    "离开本书前找不到待回滚的原生 PDF 事务"
                )
            }
            recovery = try await nativePDFMutationActor
                .rollbackForOutgoingNavigation(
                    book: access,
                    ticket: identity.ticket
                )
        } else {
            recovery = try await nativePDFMutationActor.recover(
                book: access,
                ticket: expectedTicket ?? identity?.ticket,
                oldContentSHA256: expectedOldContentSHA256,
                stagedContentSHA256: expectedStagedContentSHA256
            )
        }
        let refreshed: ReaderLocalBookRecord
        let refreshedAccess: ReaderLocalBookAccess
        if recovery.outcome == .none, identity == nil {
            refreshed = book
            refreshedAccess = access
        } else {
            refreshed = try await refreshLocalBookAfterPDFMutation(
                book,
                library: library,
                expectedByteCount: recovery.byteCount,
                expectedContentSHA256: recovery.contentSHA256
            )
            refreshedAccess = try library.makeOpenAccess(for: refreshed)
        }

        if let ocrLease {
            guard let recoveryTicket = recovery.ticket,
                  recoveryTicket == identity?.ticket else {
                throw ReaderNativePDFMutationError.commitFailed(
                    "原生 PDF 恢复没有返回持久票据"
                )
            }
            try await NativeBookOCRManager.shared.rebuildPDFMutationStatus(
                lease: ocrLease,
                resolvedContentSHA256: recovery.contentSHA256,
                totalPages: recovery.pageCount,
                message: "PDF 改页恢复：\(recovery.outcome.rawValue)"
            )
            try await nativePDFMutationActor.acknowledgeRecovery(
                book: refreshedAccess,
                ticket: recoveryTicket
            )
            try await NativeBookOCRManager.shared.finishPDFMutationLease(
                ocrLease
            )
        }

        return NativePDFRecoverySettlement(
            book: refreshed,
            access: refreshedAccess,
            contentSHA256: recovery.contentSHA256,
            recovery: recovery
        )
    }

    private func applyNativePDFRecoverySettlement(
        _ settlement: NativePDFRecoverySettlement,
        localRuntimeServer: ReaderLocalRuntimeServer,
        reopenRuntime: Bool
    ) async throws {
        if reopenRuntime, settlement.recovery.outcome != .none {
            _ = try await localRuntimeServer.open(settlement.access)
        }
        currentLocalBook = settlement.book
        currentLocalBookAccess = settlement.access
        currentLocalBookContentSHA256 = settlement.contentSHA256
        refreshNativePiRemoteBookBinding()
        try? bookUserStateWebAdapter?.updateTrustedContext(
            baseURL: localRuntimeServer.baseURL,
            localBookId: settlement.book.id
        )
        nativeBookOCRBridge?.updateTrustedContext(
            baseURL: localRuntimeServer.baseURL,
            localBookID: settlement.book.id,
            expectedContentSHA256: settlement.contentSHA256,
            localBookAccess: settlement.access
        )
    }

    private func handleNativePDFMutation(
        _ command: ReaderNativePDFMutationCommand
    ) async throws -> [String: Any] {
        switch command {
        case .prepare(let request):
            guard let book = currentLocalBook,
                  let access = currentLocalBookAccess,
                  let library = currentLocalLibrary,
                  let localRuntimeServer,
                  book.id == request.localBookID,
                  book.format == .pdf,
                  isLocalRuntimeURL(webView.url) else {
                throw ReaderNativePDFMutationError.unavailable(
                    "当前本机 PDF 上下文已经变化"
                )
            }
            let digest = try await library.ensureContentSHA256(for: book)
            guard currentLocalBook?.id == request.localBookID,
                  currentLocalBookAccess === access else {
                throw ReaderNativePDFMutationError.unavailable(
                    "计算摘要时当前本机 PDF 上下文已经变化"
                )
            }
            currentLocalBookContentSHA256 = digest
            let ocrLease = try await NativeBookOCRManager.shared
                .beginPDFMutationLease(
                    bookID: request.localBookID,
                    expectedOldDigest: digest
                )
            let receipt: ReaderNativePDFMutationPreparedReceipt
            do {
                guard currentLocalBook?.id == request.localBookID,
                      currentLocalBookAccess === access else {
                    try await NativeBookOCRManager.shared
                        .abortUnstagedPDFMutationLease(ocrLease)
                    throw ReaderNativePDFMutationError.unavailable(
                        "建立 OCR 租约时当前本机 PDF 上下文已经变化"
                    )
                }
                receipt = try await nativePDFMutationActor.prepare(
                    book: access,
                    request: request,
                    ocrLease: ocrLease
                )
            } catch {
                let primary = error.localizedDescription
                do {
                    if try await nativePDFMutationActor
                        .hasUnfinishedMutation(book: access) {
                        if let settlement = try await settleNativePDFMutation(
                            book: book,
                            access: access,
                            library: library,
                            outgoingRollback: true
                        ) {
                            try await applyNativePDFRecoverySettlement(
                                settlement,
                                localRuntimeServer: localRuntimeServer,
                                reopenRuntime: true
                            )
                        }
                    } else {
                        try await NativeBookOCRManager.shared
                            .abortUnstagedPDFMutationLease(ocrLease)
                    }
                } catch let recoveryError {
                    throw ReaderNativePDFMutationError.commitFailed(
                        "\(primary)；prepare 失败后的恢复也失败："
                            + recoveryError.localizedDescription
                    )
                }
                throw error
            }
            return [
                "contract": ReaderNativePDFMutationBridge.responseContract,
                "action": "prepared",
                "requestId": receipt.requestID,
                "ok": true,
                "localBookId": receipt.localBookID,
                "ticket": receipt.ticket,
                "operation": receipt.operation.rawValue,
                "pivotPage": receipt.pivotPage,
                "oldPageCount": receipt.oldPageCount,
                "newPageCount": receipt.newPageCount,
                "oldContentSHA256": receipt.oldContentSHA256,
                "stagedContentSHA256": receipt.stagedContentSHA256,
                "warnings": receipt.warnings,
            ]

        case .commit(let requestID, let localBookID, let ticket):
            guard let originalBook = currentLocalBook,
                  let originalAccess = currentLocalBookAccess,
                  let library = currentLocalLibrary,
                  let localRuntimeServer,
                  originalBook.id == localBookID,
                  originalBook.format == .pdf,
                  isLocalRuntimeURL(webView.url) else {
                throw ReaderNativePDFMutationError.unavailable(
                    "提交时当前本机 PDF 上下文已经变化"
                )
            }
            let replacement = try await nativePDFMutationActor
                .replacePrepared(ticket: ticket, localBookID: localBookID)
            do {
                let refreshed = try await refreshLocalBookAfterPDFMutation(
                    originalBook,
                    library: library,
                    expectedByteCount: replacement.byteCount,
                    expectedContentSHA256: replacement.contentSHA256
                )
                let access = try library.makeOpenAccess(for: refreshed)
                _ = try await localRuntimeServer.open(access)
                currentLocalBook = refreshed
                currentLocalBookAccess = access
                currentLocalBookContentSHA256 = replacement.contentSHA256
                refreshNativePiRemoteBookBinding()
                try? bookUserStateWebAdapter?.updateTrustedContext(
                    baseURL: localRuntimeServer.baseURL,
                    localBookId: refreshed.id
                )
                return [
                    "contract": ReaderNativePDFMutationBridge.responseContract,
                    "action": "committed",
                    "requestId": requestID,
                    "ok": true,
                    "localBookId": localBookID,
                    "ticket": ticket,
                    "operation": replacement.operation.rawValue,
                    "pivotPage": replacement.pivotPage,
                    "oldPageCount": replacement.oldPageCount,
                    "newPageCount": replacement.newPageCount,
                    "contentSHA256": replacement.contentSHA256,
                    "mtime": Int(replacement.modifiedAt.timeIntervalSince1970),
                    "byteCount": replacement.byteCount,
                ]
            } catch {
                let primary = error.localizedDescription
                var rollbackFailure: String?
                do {
                    try await nativePDFMutationActor.cancelOrRollback(
                        ticket: ticket,
                        localBookID: localBookID
                    )
                    guard let settlement = try await settleNativePDFMutation(
                        book: originalBook,
                        access: originalAccess,
                        library: library,
                        expectedTicket: ticket,
                        outgoingRollback: true
                    ) else {
                        throw ReaderNativePDFMutationError.commitFailed(
                            "回滚后没有得到原生 PDF 恢复结果"
                        )
                    }
                    try await applyNativePDFRecoverySettlement(
                        settlement,
                        localRuntimeServer: localRuntimeServer,
                        reopenRuntime: true
                    )
                } catch {
                    rollbackFailure = error.localizedDescription
                }
                throw ReaderNativePDFMutationError.commitFailed(
                    rollbackFailure.map {
                        "\(primary)；自动回滚失败：\($0)"
                    } ?? "\(primary)；原 PDF 已自动恢复"
                )
            }

        case .finalize(let requestID, let localBookID, let ticket):
            try await nativePDFMutationActor.finalize(
                ticket: ticket,
                localBookID: localBookID
            )
            return [
                "contract": ReaderNativePDFMutationBridge.responseContract,
                "action": "finalized",
                "requestId": requestID,
                "ok": true,
                "localBookId": localBookID,
                "ticket": ticket,
            ]

        case .cancel(let requestID, let localBookID, let ticket):
            guard let originalBook = currentLocalBook,
                  let originalAccess = currentLocalBookAccess,
                  let library = currentLocalLibrary,
                  let localRuntimeServer,
                  originalBook.id == localBookID,
                  originalBook.format == .pdf else {
                throw ReaderNativePDFMutationError.unavailable(
                    "取消时当前本机 PDF 上下文已经变化"
                )
            }
            try await nativePDFMutationActor.cancelOrRollback(
                ticket: ticket,
                localBookID: localBookID
            )
            guard let settlement = try await settleNativePDFMutation(
                book: originalBook,
                access: originalAccess,
                library: library,
                expectedTicket: ticket,
                outgoingRollback: true
            ) else {
                throw ReaderNativePDFMutationError.commitFailed(
                    "取消后没有得到原生 PDF 恢复结果"
                )
            }
            try await applyNativePDFRecoverySettlement(
                settlement,
                localRuntimeServer: localRuntimeServer,
                reopenRuntime: true
            )
            return [
                "contract": ReaderNativePDFMutationBridge.responseContract,
                "action": "cancelled",
                "requestId": requestID,
                "ok": true,
                "localBookId": localBookID,
                "ticket": ticket,
            ]

        case .recover(
            let requestID,
            let localBookID,
            let ticket,
            let oldContentSHA256,
            let stagedContentSHA256
        ):
            guard let originalBook = currentLocalBook,
                  let access = currentLocalBookAccess,
                  let library = currentLocalLibrary,
                  let localRuntimeServer,
                  originalBook.id == localBookID,
                  originalBook.format == .pdf,
                  isLocalRuntimeURL(webView.url) else {
                throw ReaderNativePDFMutationError.unavailable(
                    "恢复时当前本机 PDF 上下文已经变化"
                )
            }
            let hasExpectedIdentity = ticket != nil
                || oldContentSHA256 != nil
                || stagedContentSHA256 != nil
            let hasUnfinishedMutation = try await nativePDFMutationActor
                .hasUnfinishedMutation(book: access)
            if !hasExpectedIdentity, !hasUnfinishedMutation {
                // A clean open is a probe, not a recovery operation. Hashing
                // the entire PDF here made every cold book switch read a
                // multi-hundred-megabyte file before book-meta/page images
                // could start. A real native journal, or a web journal carrying
                // expected identities, still takes the fully verified path.
                return [
                    "contract": ReaderNativePDFMutationBridge.responseContract,
                    "action": "recovered",
                    "requestId": requestID,
                    "ok": true,
                    "localBookId": localBookID,
                    "ticket": NSNull(),
                    "outcome": ReaderNativePDFMutationRecoveryReceipt
                        .Outcome.none.rawValue,
                    "contentSHA256": currentLocalBookContentSHA256
                        .map { $0 as Any } ?? NSNull(),
                    "mtime": Int((originalBook.modifiedAt
                        ?? Date(timeIntervalSince1970: 0)).timeIntervalSince1970),
                    "byteCount": originalBook.byteCount,
                ]
            }
            guard let settlement = try await settleNativePDFMutation(
                book: originalBook,
                access: access,
                library: library,
                expectedTicket: ticket,
                expectedOldContentSHA256: oldContentSHA256,
                expectedStagedContentSHA256: stagedContentSHA256,
                requiresReceipt: true
            ) else {
                throw ReaderNativePDFMutationError.commitFailed(
                    "原生 PDF 恢复没有返回结果"
                )
            }
            try await applyNativePDFRecoverySettlement(
                settlement,
                localRuntimeServer: localRuntimeServer,
                reopenRuntime: true
            )
            let recovery = settlement.recovery
            return [
                "contract": ReaderNativePDFMutationBridge.responseContract,
                "action": "recovered",
                "requestId": requestID,
                "ok": true,
                "localBookId": localBookID,
                "ticket": recovery.ticket ?? NSNull(),
                "outcome": recovery.outcome.rawValue,
                "contentSHA256": recovery.contentSHA256,
                "mtime": Int(recovery.modifiedAt.timeIntervalSince1970),
                "byteCount": recovery.byteCount,
            ]
        }
    }

    private func refreshLocalBookAfterPDFMutation(
        _ original: ReaderLocalBookRecord,
        library: ReaderLocalLibraryManager,
        expectedByteCount: Int64,
        expectedContentSHA256: String
    ) async throws -> ReaderLocalBookRecord {
        for _ in 0..<120 where library.isScanning {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        guard !library.isScanning else {
            throw ReaderNativePDFMutationError.commitFailed(
                "书库扫描长时间未结束"
            )
        }
        await library.rescan()
        guard let refreshed = library.books.first(where: {
            $0.id == original.id && $0.relativePath == original.relativePath
        }),
        refreshed.byteCount == expectedByteCount else {
            throw ReaderNativePDFMutationError.commitFailed(
                library.errorMessage ?? "书库没有确认替换后的 PDF"
            )
        }
        let digest = try await library.ensureContentSHA256(for: refreshed)
        guard digest == expectedContentSHA256,
              let withDigest = library.books.first(where: {
                $0.id == original.id
                    && $0.relativePath == original.relativePath
              }) else {
            throw ReaderNativePDFMutationError.commitFailed(
                "书库重扫后的 PDF 摘要不匹配"
            )
        }
        return withDigest
    }

    private func showBookUserStatePlan(_ plan: ReaderBookUserStateImportPlan) {
        let imported = plan.decisions.filter { $0.action == .import }.count
        let conflicts = plan.decisions.filter {
            $0.classification == .conflict
        }.count
        let localNewer = plan.decisions.filter {
            $0.classification == .localNewer
        }.count
        var parts: [String] = []
        parts.append(imported > 0 ? "已合并 \(imported) 类 Pi 用户数据" : "Pi 用户数据已核对")
        if localNewer > 0 {
            parts.append("保留本机较新数据 \(localNewer) 类")
        }
        if conflicts > 0 {
            parts.append("保留冲突数据 \(conflicts) 类，未覆盖")
        }
        showBookUserStateMessage(
            parts.joined(separator: "；"),
            isError: conflicts > 0
        )
    }

    private func showBookUserStateMessage(
        _ message: String,
        isError: Bool
    ) {
        guard isLocalRuntimeURL(webView.url), !isLoading else {
            deferredBookUserStateMessage = (message, isError)
            return
        }
        let payload: [String: Any] = [
            "message": String(message.prefix(1_000)),
            "isError": isError,
        ]
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload),
              let literal = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript(
            """
            (() => {
              const value = \(literal);
              let banner = document.getElementById("bw-native-user-state-status");
              if (!banner) {
                banner = document.createElement("div");
                banner.id = "bw-native-user-state-status";
                banner.setAttribute("style", [
                  "position:fixed", "left:12px", "right:12px", "top:12px",
                  "z-index:2147483647", "padding:10px 12px",
                  "border-radius:10px", "color:#fff",
                  "font:13px/1.5 -apple-system,system-ui,sans-serif",
                  "white-space:pre-wrap", "word-break:break-word",
                  "box-shadow:0 2px 12px rgba(0,0,0,.35)"
                ].join(";"));
                banner.addEventListener("click", () => banner.remove());
                document.body.appendChild(banner);
              }
              banner.style.background = value.isError
                ? "rgba(176,0,32,.95)" : "rgba(38,50,56,.94)";
              banner.textContent = value.message;
              clearTimeout(window.__bwNativeUserStateStatusTimer);
              window.__bwNativeUserStateStatusTimer = setTimeout(
                () => banner.remove(), 16000
              );
            })();
            """,
            completionHandler: nil
        )
    }

    func bind(nativeVoiceBridge: NativeVoiceBridge) {
        self.nativeVoiceBridge = nativeVoiceBridge
        updateNativeVoiceButton(state: nativeVoiceBridge.state)
    }

    func startExternalNativeAgentVoice(
        webContext: ReaderNativeWebContext
    ) async {
        guard webContext.isValid else { return }
        if let bridge = nativeVoiceBridge, bridge.state.phase != .idle {
            await bridge.stop()
        }
        await nativeAgentVoice.stop()
        externalNativeAgentControlTask?.cancel()
        externalNativeAgentVoice = true
        nativeAgentVoiceWasReady = false
        try? ReaderNativeBridgeStore().writeLatestWebContext(webContext)
        await nativeAgentVoice.start(context: NativeAgentVoiceContext())
        startExternalNativeAgentControlPump()
    }

    private func startExternalNativeAgentControlPump() {
        externalNativeAgentControlTask?.cancel()
        externalNativeAgentControlTask = Task { @MainActor [weak self] in
            guard let self else { return }
            let store = ReaderNativeBridgeStore()
            while !Task.isCancelled && self.externalNativeAgentVoice {
                do {
                    for control in try store.consumeAgentControls() {
                        switch control.command {
                        case "stop":
                            await self.nativeAgentVoice.stop()
                            self.externalNativeAgentVoice = false
                        case "speak":
                            if let text = control.text {
                                try await self.nativeAgentVoice.speak(
                                    text,
                                    mood: control.mood
                                )
                            }
                        case "speak_done":
                            try await self.nativeAgentVoice.finishSpeaking()
                        case "cancel":
                            await self.nativeAgentVoice.cancelSpeaking()
                        default:
                            break
                        }
                    }
                } catch {
                    self.publishExternalNativeAgentEvent(
                        "error",
                        payload: ReaderNativeAgentEventPayload(
                            error: error.localizedDescription
                        )
                    )
                }
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
            self.externalNativeAgentControlTask = nil
        }
    }

    func reload() {
        waitsForInitialBookDecision = false
        cancelPendingLocalBookNavigation()
        loadError = nil
        guard let localRuntimeServer else {
            loadError = localRuntimeInitializationError
                ?? ReaderLocalRuntimeError.bundleUnavailable.localizedDescription
            return
        }
        Task { @MainActor [weak self, localRuntimeServer] in
            guard let self else { return }
            do {
                try await localRuntimeServer.start()
                if let outgoingBook = self.currentLocalBook,
                   let outgoingAccess = self.currentLocalBookAccess,
                   let outgoingLibrary = self.currentLocalLibrary,
                   outgoingBook.format == .pdf,
                   let settlement = try await self.settleNativePDFMutation(
                        book: outgoingBook,
                        access: outgoingAccess,
                        library: outgoingLibrary,
                        outgoingRollback: true
                   ) {
                    try await self.applyNativePDFRecoverySettlement(
                        settlement,
                        localRuntimeServer: localRuntimeServer,
                        reopenRuntime: true
                    )
                }
                self.resetBookUserStateContext(
                    baseURL: localRuntimeServer.baseURL
                )
                self.nativeBookOCRBridge?.updateTrustedContext(
                    baseURL: localRuntimeServer.baseURL,
                    localBookID: "localbook-welcome",
                    expectedContentSHA256: nil,
                    localBookAccess: nil
                )
                self.webView.load(URLRequest(
                    url: localRuntimeServer.defaultShellURL(),
                    cachePolicy: .useProtocolCachePolicy,
                    timeoutInterval: 30
                ))
            } catch {
                self.loadError = error.localizedDescription
            }
        }
    }

    /// Opens an indexed local book without uploading it. The server retains
    /// the security-scoped access until another book replaces this session.
    @discardableResult
    func openLocalBook(
        _ book: ReaderLocalBookRecord,
        library: ReaderLocalLibraryManager
    ) async -> Bool {
        await openLocalBook(
            book,
            library: library,
            restorationToken: nil,
            initialPage: nil
        )
    }

    func restoreLocalBook(
        _ book: ReaderLocalBookRecord,
        library: ReaderLocalLibraryManager
    ) async -> Bool {
        let token = UUID()
        return await withCheckedContinuation {
            (continuation: CheckedContinuation<Bool, Never>) in
            localBookRestoreContinuations[token] = continuation
            Task { @MainActor [weak self] in
                guard let self else {
                    continuation.resume(returning: false)
                    return
                }
                let started = await self.openLocalBook(
                    book,
                    library: library,
                    restorationToken: token,
                    initialPage: nil
                )
                guard started else {
                    self.finishLocalBookRestore(token: token, succeeded: false)
                    return
                }
                Task { @MainActor [weak self] in
                    do {
                        try await Task.sleep(nanoseconds: 15_000_000_000)
                    } catch {
                        return
                    }
                    self?.finishLocalBookRestore(
                        token: token,
                        succeeded: false
                    )
                }
            }
        }
    }

    @discardableResult
    private func openLocalBook(
        _ book: ReaderLocalBookRecord,
        library: ReaderLocalLibraryManager,
        restorationToken: UUID?,
        initialPage: Int?
    ) async -> Bool {
        waitsForInitialBookDecision = false
        guard let localRuntimeServer else {
            library.reportError(
                ReaderLocalRuntimeError.serverUnavailable(
                    localRuntimeInitializationError ?? "本机 Reader 未初始化"
                )
            )
            return false
        }
        var didClearOutgoingRemoteBinding = false
        do {
            let changesBook = currentLocalBook?.id != book.id
                || currentLocalLibrary?.stableLibraryID
                    != library.stableLibraryID
            if changesBook,
               let outgoingBook = currentLocalBook,
               let outgoingAccess = currentLocalBookAccess,
               let outgoingLibrary = currentLocalLibrary,
               outgoingBook.format == .pdf,
               let settlement = try await settleNativePDFMutation(
                    book: outgoingBook,
                    access: outgoingAccess,
                    library: outgoingLibrary,
                    outgoingRollback: true
               ) {
                // A switch is not a commit signal. The exact outgoing book is
                // durably rolled back/cleaned and its OCR lease is released
                // before any target navigation or identity change begins.
                try await applyNativePDFRecoverySettlement(
                    settlement,
                    localRuntimeServer: localRuntimeServer,
                    reopenRuntime: true
                )
            }

            cancelPendingLocalBookNavigation()
            // The outgoing page loses its Pi identity only after its native PDF
            // transaction has settled. A failed rollback leaves this binding and
            // page in place because the catch below aborts the switch.
            nativePiGateway?.updateTrustedRemoteBookBinding(nil)
            didClearOutgoingRemoteBinding = true

            var openingBook = library.books.first(where: {
                $0.id == book.id && $0.relativePath == book.relativePath
            }) ?? book
            var access = try library.makeOpenAccess(for: openingBook)
            var openingContentSHA256 = openingBook.contentSha256
            if openingContentSHA256 == nil,
               let verified = remoteLibraryCoordinator?
                .verifiedNativeRemoteBookBinding(
                    for: openingBook,
                    localContentSHA256: nil
                ) {
                // A Pi download/reconciliation already compared the exact
                // bytes. Reuse that verified identity immediately instead of
                // hashing a large PDF again before its first page can appear.
                openingContentSHA256 = verified.localContentSHA256
            }
            if openingBook.format == .pdf,
               let settlement = try await settleNativePDFMutation(
                    book: openingBook,
                    access: access,
                    library: library
               ) {
                // Incoming crash recovery runs before localRuntimeServer.open,
                // so neither the shell nor injected JavaScript can observe a
                // half-replaced PDF/OCR sidecar pair.
                openingBook = settlement.book
                access = settlement.access
                openingContentSHA256 = settlement.contentSHA256
            }
            try await localRuntimeServer.start()
            let url = try await localRuntimeServer.open(
                access,
                initialPage: initialPage
            )
            bookUserStateImportTask?.cancel()
            bookUserStateImportTask = nil
            localPDFContentIdentityTask?.cancel()
            localPDFContentIdentityTask = nil
            bookUserStateContextGeneration &+= 1
            currentLocalBook = openingBook
            currentLocalBookAccess = access
            currentLocalLibrary = library
            currentLocalBookContentSHA256 = openingContentSHA256
            refreshNativePiRemoteBookBinding()
            try? bookUserStateWebAdapter?.updateTrustedContext(
                baseURL: localRuntimeServer.baseURL,
                localBookId: openingBook.id
            )
            nativeBookOCRBridge?.updateTrustedContext(
                baseURL: localRuntimeServer.baseURL,
                localBookID: openingBook.id,
                expectedContentSHA256: openingContentSHA256,
                localBookAccess: access
            )
            loadError = nil
            guard let navigation = webView.load(URLRequest(
                url: url,
                cachePolicy: .useProtocolCachePolicy,
                timeoutInterval: 30
            )) else {
                throw ReaderLocalRuntimeError.serverUnavailable(
                    "本机书籍导航未能启动"
                )
            }
            pendingLocalBookNavigation = PendingLocalBookNavigation(
                navigation: navigation,
                bookID: openingBook.id,
                libraryID: library.stableLibraryID,
                restorationToken: restorationToken
            )
            return true
        } catch {
            if didClearOutgoingRemoteBinding {
                nativePiGateway?.updateTrustedRemoteBookBinding(nil)
            }
            library.reportError(error)
            showBookUserStateMessage(
                "无法打开目标书籍：\(error.localizedDescription)",
                isError: true
            )
            if let restorationToken {
                finishLocalBookRestore(
                    token: restorationToken,
                    succeeded: false
                )
            }
            return false
        }
    }

    private func cancelPendingLocalBookNavigation() {
        guard let pending = pendingLocalBookNavigation else { return }
        pendingLocalBookNavigation = nil
        if let token = pending.restorationToken {
            finishLocalBookRestore(token: token, succeeded: false)
        }
    }

    private func finishLocalBookRestore(token: UUID, succeeded: Bool) {
        guard let continuation = localBookRestoreContinuations.removeValue(
            forKey: token
        ) else { return }
        continuation.resume(returning: succeeded)
    }

    func setReaderScenePhase(_ phase: ScenePhase) {
        switch phase {
        case .background:
            readerWasBackgrounded = true
            setReaderForeground(false, restartLocalRuntime: false)
        case .inactive:
            setReaderForeground(false, restartLocalRuntime: false)
        case .active:
            let shouldRestart = readerWasBackgrounded
            readerWasBackgrounded = false
            setReaderForeground(true, restartLocalRuntime: shouldRestart)
        @unknown default:
            setReaderForeground(false, restartLocalRuntime: false)
        }
    }

    func setReaderForeground(
        _ foreground: Bool,
        restartLocalRuntime: Bool = false
    ) {
        let wasForeground = readerForeground
        readerForeground = foreground
        if foreground, !wasForeground, isLocalRuntimeURL(webView.url) {
            if restartLocalRuntime, let localRuntimeServer {
                Task { @MainActor [weak self, localRuntimeServer] in
                    guard let self else { return }
                    do {
                        // Reload only for an actual server rebuild or a dead
                        // WebKit content process. A brief inactive transition
                        // leaves both intact and must preserve the rendered
                        // page, scroll position and warmed page images.
                        let restarted = try await localRuntimeServer
                            .restartAfterForeground()
                        self.reloadLocalRuntimeAfterRecoveryIfNeeded(
                            serverRebuilt: restarted
                        )
                    } catch {
                        self.loadError = error.localizedDescription
                    }
                }
            } else {
                reloadLocalRuntimeAfterRecoveryIfNeeded(serverRebuilt: false)
            }
        }
        guard webView.url != nil else {
            return
        }
        let value = foreground ? "true" : "false"
        webView.evaluateJavaScript(
            """
            (() => {
              window.__BW_NATIVE_READER_FOREGROUND__ = \(value);
              window.dispatchEvent(new CustomEvent(
                "bw-native-reader-foreground",
                { detail: { active: \(value) } }
              ));
              if (\(value)) window.__bwNativeInkHost?.refresh?.();
            })();
            """
        )
        if foreground {
            deliverPendingAnkiMobileCallbacks()
        }
    }

    /// Proves the dedicated Reader snapshot socket while the native scene is
    /// active. WebKit can keep a dead WebSocket in OPEN after the Windows
    /// service restarts, so waiting for `onclose` alone can leave the App on a
    /// stale snapshot indefinitely. The existing foreground handler performs
    /// a bounded read-only probe and rebuilds the link when that probe fails.
    func probeReaderSnapshotLink() {
        guard readerForeground, webView.url != nil else { return }
        webView.evaluateJavaScript(
            """
            window.dispatchEvent(new CustomEvent(
              "bw-native-reader-foreground",
              { detail: { active: true, probe: true } }
            ));
            """
        )
    }

    private func reloadLocalRuntimeAfterRecoveryIfNeeded(
        serverRebuilt: Bool
    ) {
        guard serverRebuilt || webContentProcessNeedsReload else { return }
        // Keep the recovery request set until didFinish. A native-ink save
        // guard can cancel this navigation, and a provisional load can fail;
        // clearing here would make either failure permanently suppress retry.
        webContentProcessNeedsReload = true
        webView.reload()
    }

    func updateNativeVoiceButton(state: NativeVoiceBridgeState) {
        guard webView.url != nil else {
            return
        }
        let detail = state.detail?.trimmingCharacters(in: .whitespacesAndNewlines)
        let title: String
        if let detail, !detail.isEmpty {
            title = "\(state.title)：\(detail)"
        } else {
            title = state.title
        }
        let value: [String: Any] = [
            "active": state.isActive,
            "busy": state.isBusy,
            "sessionId": state.sessionId ?? NSNull(),
            "title": title,
        ]
        guard
            JSONSerialization.isValidJSONObject(value),
            let data = try? JSONSerialization.data(withJSONObject: value),
            let literal = String(data: data, encoding: .utf8)
        else {
            return
        }
        webView.evaluateJavaScript(
            "window.__bwNativeComputerVoiceApplyState?.(\(literal))"
        )
    }

    func nativeTouchDoubleTapAction() async throws -> String {
        guard webView.url != nil else {
            throw NativeReaderSettingError.pageUnavailable
        }
        let value = try await webView.callAsyncJavaScript(
            """
            return String(
              localStorage.getItem("rc-ink-double-tap-action") || "eraser"
            );
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        let action = value as? String ?? "eraser"
        return ["eraser", "selection", "none"].contains(action)
            ? action
            : "eraser"
    }

    func setNativeTouchDoubleTapAction(_ action: String) async throws {
        guard ["eraser", "selection", "none"].contains(action) else {
            throw NativeReaderSettingError.invalidTouchDoubleTapAction
        }
        guard webView.url != nil else {
            throw NativeReaderSettingError.pageUnavailable
        }
        _ = try await webView.callAsyncJavaScript(
            """
            localStorage.setItem("rc-ink-double-tap-action", action);
            window.dispatchEvent(new CustomEvent(
              "bw-ink-double-tap-setting",
              { detail: { action } }
            ));
            return action;
            """,
            arguments: ["action": action],
            in: nil,
            contentWorld: .page
        )
    }

    private func toggleNativeComputerVoice(
        appKind: DirectVoiceTargetApp
    ) {
        guard let bridge = nativeVoiceBridge else {
            return
        }
        Task { @MainActor [weak bridge] in
            guard let bridge else {
                return
            }
            self.externalNativeAgentVoice = false
            self.externalNativeAgentControlTask?.cancel()
            self.externalNativeAgentControlTask = nil
            if self.nativeAgentVoice.state != .idle {
                await self.nativeAgentVoice.stop()
            }
            _ = try? ReaderNativeBridgeStore().consumeAgentControls()
            switch bridge.state.phase {
            case .idle, .failed:
                await bridge.start(appKind: appKind)
            case .active, .suspended:
                await bridge.stop()
            case .preparing, .connecting, .starting, .stopping:
                return
            }
        }
    }

    private func handleNativeAgentVoice(
        _ body: [String: Any]
    ) {
        guard let action = body["action"] as? String else {
            return
        }
        switch action {
        case "start":
            guard body.count == 3,
                  let file = body["file"] as? String,
                  file.count <= 2_048,
                  let pageNumber = body["page"] as? NSNumber else {
                return
            }
            let page = pageNumber.intValue
            enqueueNativeAgentVoiceCommand(.start(
                NativeAgentVoiceContext(
                    fileRelativePath: file.isEmpty ? nil : file,
                    page: page > 0 ? page : nil
                )
            ))
        case "stop":
            guard body.count == 1 else { return }
            enqueueNativeAgentVoiceCommand(.stop)
        case "speak":
            guard body.count == 3,
                  let text = body["text"] as? String,
                  !text.trimmingCharacters(
                    in: .whitespacesAndNewlines
                  ).isEmpty,
                  let mood = body["mood"] as? String else {
                return
            }
            enqueueNativeAgentVoiceCommand(.speak(
                text,
                mood.isEmpty ? nil : mood
            ))
        case "speak_done":
            guard body.count == 1 else { return }
            enqueueNativeAgentVoiceCommand(.finishSpeaking)
        case "cancel":
            guard body.count == 1 else { return }
            enqueueNativeAgentVoiceCommand(.cancelSpeaking)
        default:
            return
        }
    }

    /// WK messages arrive independently. Chaining them here preserves the
    /// relay protocol order (`speak*` before `speak_done`, and `cancel` after
    /// the text it supersedes) even when an actor send suspends.
    private func enqueueNativeAgentVoiceCommand(
        _ command: NativeAgentVoiceCommand
    ) {
        let previous = nativeAgentVoiceCommandTail
        let task = Task { @MainActor [weak self] in
            if let previous {
                await previous.value
            }
            guard let self else { return }
            await self.executeNativeAgentVoiceCommand(command)
        }
        nativeAgentVoiceCommandTail = task
    }

    private func executeNativeAgentVoiceCommand(
        _ command: NativeAgentVoiceCommand
    ) async {
        do {
            switch command {
            case .start(let context):
                if let bridge = nativeVoiceBridge,
                   bridge.state.phase != .idle {
                    await bridge.stop()
                }
                await nativeAgentVoice.start(context: context)
            case .stop:
                await nativeAgentVoice.stop()
            case .speak(let text, let mood):
                try await nativeAgentVoice.speak(text, mood: mood)
            case .finishSpeaking:
                try await nativeAgentVoice.finishSpeaking()
            case .cancelSpeaking:
                await nativeAgentVoice.cancelSpeaking()
            }
        } catch {
            sendNativeAgentVoiceEvent(
                "error",
                payload: ["error": error.localizedDescription]
            )
        }
    }

    private func sendNativeAgentVoiceEvent(
        _ event: String,
        payload: [String: Any] = [:]
    ) {
        guard isTrustedReaderURL(webView.url) else { return }
        let value: [String: Any] = [
            "event": event,
            "payload": payload,
        ]
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value),
              let literal = String(data: data, encoding: .utf8) else {
            return
        }
        webView.evaluateJavaScript(
            "window.__bwNativeAgentVoiceDispatch?.(\(literal))",
            completionHandler: nil
        )
    }

    private func publishExternalNativeAgentEvent(
        _ event: String,
        payload: ReaderNativeAgentEventPayload = .init()
    ) {
        let store = ReaderNativeBridgeStore()
        try? store.appendAgentEvent(event: event, payload: payload)
    }

    private func isValidAnkiMobileGID(_ value: String) -> Bool {
        guard value == value.lowercased(), value.hasPrefix("card_") else {
            return false
        }
        let suffix = value.dropFirst("card_".count)
        let allowed = CharacterSet(charactersIn: "0123456789abcdef")
        return (4...64).contains(suffix.count)
            && suffix.unicodeScalars.allSatisfy { allowed.contains($0) }
    }

    private func isValidAnkiMobileNonce(_ value: String) -> Bool {
        let allowed = CharacterSet(charactersIn: "0123456789abcdef")
        return value.count == 32
            && value == value.lowercased()
            && value.unicodeScalars.allSatisfy { allowed.contains($0) }
    }

    private func ankiMobileQuery(_ url: URL) -> [String: String]? {
        guard let components = URLComponents(
            url: url,
            resolvingAgainstBaseURL: false
        ) else { return nil }
        var result = [String: String]()
        for item in components.queryItems ?? [] {
            guard
                result[item.name] == nil,
                !item.name.contains("\0"),
                let value = item.value,
                !value.contains("\0")
            else { return nil }
            result[item.name] = value
        }
        return result
    }

    private func isValidAnkiMobileCallbackURL(
        _ url: URL,
        gid: String,
        index: Int,
        nonce: String
    ) -> Bool {
        guard
            url.scheme?.lowercased() == "bwreader",
            url.host?.lowercased() == "anki-export-success",
            url.user == nil,
            url.password == nil,
            url.port == nil,
            url.path.isEmpty,
            url.fragment == nil,
            let query = ankiMobileQuery(url),
            Set(query.keys) == Set(["gid", "index", "nonce"])
        else { return false }
        return query["gid"] == gid
            && query["index"] == String(index)
            && query["nonce"] == nonce
    }

    private func isValidAnkiMobileAddNoteURL(
        _ url: URL,
        gid: String,
        index: Int,
        nonce: String
    ) -> Bool {
        guard
            url.scheme?.lowercased() == "anki",
            url.host?.lowercased() == "x-callback-url",
            url.user == nil,
            url.password == nil,
            url.port == nil,
            url.path == "/addnote",
            url.fragment == nil,
            let query = ankiMobileQuery(url),
            let type = query["type"],
            query["deck"] == "BW Reader",
            let tags = query["tags"],
            let callbackValue = query["x-success"],
            let callbackURL = URL(string: callbackValue),
            isValidAnkiMobileCallbackURL(
                callbackURL,
                gid: gid,
                index: index,
                nonce: nonce
            )
        else { return false }

        let requiredFields: Set<String>
        if type == "Basic" {
            requiredFields = Set([
                "type", "deck", "fldFront", "fldBack", "tags", "x-success",
            ])
            guard
                query["fldFront"]?.isEmpty == false,
                query["fldBack"]?.isEmpty == false
            else { return false }
        } else if type == "Cloze" {
            requiredFields = Set([
                "type", "deck", "fldText", "tags", "x-success",
            ])
            guard
                let text = query["fldText"],
                !text.isEmpty,
                text.range(
                    of: #"\{\{c[1-9][0-9]*::[\s\S]+?\}\}"#,
                    options: .regularExpression
                ) != nil
            else { return false }
        } else {
            return false
        }
        guard Set(query.keys) == requiredFields else { return false }
        let tagSet = Set(
            tags.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        )
        return tagSet.contains("bwreader")
            && tagSet.contains("bwgid_\(gid)")
            && tagSet.contains("bwindex_\(index)")
            && query.values.allSatisfy { !$0.contains("\0") }
    }

    private func ankiMobileDocumentIdentity() -> String? {
        // Never persist the loopback capability path: it contains a bearer
        // token. A stable local book id is sufficient to bind delivery to the
        // same Reader document across WebKit and App restarts.
        guard let currentLocalBook else { return nil }
        return "local-book:\(currentLocalBook.id)"
    }

    private func restorePendingAnkiMobileExports() {
        for record in ankiMobilePendingStore.load() {
            guard pendingAnkiMobileExports[record.nonce] == nil,
                  !pendingAnkiMobileExports.values.contains(where: {
                    $0.gid == record.gid && $0.index == record.index
                  }) else { continue }
            pendingAnkiMobileExports[record.nonce] = PendingAnkiMobileExport(
                gid: record.gid,
                index: record.index,
                nonce: record.nonce,
                documentIdentity: record.documentIdentity,
                expiresAt: record.expiresAt,
                callbackReceived: record.callbackReceived,
                delivering: false
            )
        }
    }

    private func persistPendingAnkiMobileExports() {
        let records = pendingAnkiMobileExports.values.map { pending in
            ReaderAnkiMobilePendingRecord(
                gid: pending.gid,
                index: pending.index,
                nonce: pending.nonce,
                documentIdentity: pending.documentIdentity,
                expiresAt: pending.expiresAt,
                callbackReceived: pending.callbackReceived
            )
        }
        ankiMobilePendingStore.save(records)
    }

    private func prunePendingAnkiMobileExports() {
        let now = Date()
        let previousCount = pendingAnkiMobileExports.count
        pendingAnkiMobileExports = pendingAnkiMobileExports.filter {
            $0.value.expiresAt > now
        }
        if pendingAnkiMobileExports.count != previousCount {
            persistPendingAnkiMobileExports()
        }
    }

    private func handleNativeAnkiMobileRequest(
        _ body: [String: Any],
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        prunePendingAnkiMobileExports()
        guard let action = body["action"] as? String else {
            replyHandler(nil, "AnkiMobile 投影缺少 action")
            return
        }
        if action == "sync" {
            guard
                Set(body.keys) == Set(["action", "url"]),
                let rawURL = body["url"] as? String,
                rawURL == "anki://x-callback-url/sync",
                let url = URL(string: rawURL)
            else {
                replyHandler(nil, "AnkiMobile 同步请求无效")
                return
            }
            UIApplication.shared.open(url, options: [:]) { opened in
                Task { @MainActor in
                    replyHandler([
                        "ok": opened,
                        "opened": opened,
                        "status": opened ? "requested" : "failed",
                        "error": opened ? "" : "AnkiMobile 未安装或无法打开",
                    ], nil)
                }
            }
            return
        }

        guard
            action == "open",
            Set(body.keys) == Set([
                "action", "gid", "index", "nonce", "expiresAt", "url",
            ]),
            let gid = body["gid"] as? String,
            isValidAnkiMobileGID(gid),
            let indexNumber = body["index"] as? NSNumber,
            CFGetTypeID(indexNumber) != CFBooleanGetTypeID(),
            indexNumber.doubleValue == Double(indexNumber.intValue),
            (0...255).contains(indexNumber.intValue),
            let nonce = body["nonce"] as? String,
            isValidAnkiMobileNonce(nonce),
            let expiresAtNumber = body["expiresAt"] as? NSNumber,
            CFGetTypeID(expiresAtNumber) != CFBooleanGetTypeID(),
            expiresAtNumber.doubleValue
                == Double(expiresAtNumber.int64Value),
            expiresAtNumber.int64Value >= 0,
            let rawURL = body["url"] as? String,
            !rawURL.contains("\0"),
            rawURL.utf8.count <= 32 * 1024,
            let url = URL(string: rawURL),
            isValidAnkiMobileAddNoteURL(
                url,
                gid: gid,
                index: indexNumber.intValue,
                nonce: nonce
            ),
            let documentIdentity = ankiMobileDocumentIdentity()
        else {
            replyHandler(nil, "AnkiMobile 加卡请求无效")
            return
        }
        let index = indexNumber.intValue
        let expiresAt = Date(
            timeIntervalSince1970:
                Double(expiresAtNumber.int64Value) / 1_000
        )
        let now = Date()
        guard expiresAt > now,
              expiresAt <= now.addingTimeInterval(10 * 60 + 30) else {
            replyHandler(nil, "AnkiMobile 加卡过期时间无效")
            return
        }
        guard pendingAnkiMobileExports[nonce] == nil,
              !pendingAnkiMobileExports.values.contains(where: {
                $0.gid == gid && $0.index == index
              }) else {
            replyHandler(nil, "这张卡已有等待中的 AnkiMobile 投影")
            return
        }

        pendingAnkiMobileExports[nonce] = PendingAnkiMobileExport(
            gid: gid,
            index: index,
            nonce: nonce,
            documentIdentity: documentIdentity,
            expiresAt: expiresAt,
            callbackReceived: false,
            delivering: false
        )
        persistPendingAnkiMobileExports()
        UIApplication.shared.open(url, options: [:]) { [weak self] opened in
            Task { @MainActor in
                if !opened {
                    self?.pendingAnkiMobileExports.removeValue(forKey: nonce)
                    self?.persistPendingAnkiMobileExports()
                }
                replyHandler([
                    "ok": opened,
                    "opened": opened,
                    "status": opened ? "pending" : "failed",
                    "error": opened ? "" : "AnkiMobile 未安装或无法打开",
                ], nil)
            }
        }
    }

    @discardableResult
    func handleAnkiMobileCallback(_ url: URL) -> Bool {
        guard
            url.scheme?.lowercased() == "bwreader",
            url.host?.lowercased() == "anki-export-success"
        else { return false }
        restorePendingAnkiMobileExports()
        prunePendingAnkiMobileExports()
        guard
            let query = ankiMobileQuery(url),
            Set(query.keys) == Set(["gid", "index", "nonce"]),
            let gid = query["gid"],
            isValidAnkiMobileGID(gid),
            let indexValue = query["index"],
            let index = Int(indexValue),
            String(index) == indexValue,
            (0...255).contains(index),
            let nonce = query["nonce"],
            isValidAnkiMobileNonce(nonce),
            var pending = pendingAnkiMobileExports[nonce],
            pending.gid == gid,
            pending.index == index,
            isValidAnkiMobileCallbackURL(
                url,
                gid: gid,
                index: index,
                nonce: nonce
            )
        else {
            // It is our callback namespace, but not a callback issued by the
            // current App process. Never forward it as a general command URL.
            return true
        }
        pending.callbackReceived = true
        pending.delivering = false
        pendingAnkiMobileExports[nonce] = pending
        persistPendingAnkiMobileExports()
        deliverPendingAnkiMobileCallbacks()
        return true
    }

    private func deliverPendingAnkiMobileCallbacks() {
        prunePendingAnkiMobileExports()
        guard let documentIdentity = ankiMobileDocumentIdentity(),
              isTrustedReaderURL(webView.url) else { return }
        let ready = pendingAnkiMobileExports.filter {
            $0.value.callbackReceived
                && !$0.value.delivering
                && $0.value.documentIdentity == documentIdentity
        }
        for (nonce, var pending) in ready {
            let detail: [String: Any] = [
                "status": "succeeded",
                "gid": pending.gid,
                "index": pending.index,
                "nonce": pending.nonce,
            ]
            pending.delivering = true
            pendingAnkiMobileExports[nonce] = pending
            Task { @MainActor [weak self] in
                guard let self else { return }
                var durable = false
                do {
                    let value = try await self.webView.callAsyncJavaScript(
                        """
                        const api = window.BWReaderRuntime?.ankiMobileExport;
                        if (!api || api.CONTRACT !== "anki-mobile-export/1" ||
                            typeof api.handleNativeCallback !== "function") {
                          return { ok: false, durable: false };
                        }
                        return await api.handleNativeCallback(detail);
                        """,
                        arguments: ["detail": detail],
                        in: nil,
                        contentWorld: .page
                    )
                    if let ack = value as? [String: Any],
                       Set(ack.keys) == Set(["ok", "durable"]),
                       ack["ok"] as? Bool == true,
                       ack["durable"] as? Bool == true {
                        durable = true
                    }
                } catch {
                    durable = false
                }
                guard var current = self.pendingAnkiMobileExports[nonce],
                      current.nonce == nonce else { return }
                if durable {
                    self.pendingAnkiMobileExports.removeValue(forKey: nonce)
                } else {
                    current.delivering = false
                    self.pendingAnkiMobileExports[nonce] = current
                }
                self.persistPendingAnkiMobileExports()
            }
        }
    }

    private func isTrustedReaderURL(_ url: URL?) -> Bool {
        guard let url else { return false }
        let scheme = url.scheme?.lowercased()
        let host = url.host?.lowercased()
        let port = url.port
        let local = scheme == "http"
            && host == ReaderLocalRuntimeServer.host
            && port == Int(ReaderLocalRuntimeServer.port)
            && localRuntimeServer.map {
                url.path.hasPrefix($0.baseURL.path)
            } == true
        return local
    }

    private func isAllowedEmbeddedVideoURL(_ url: URL?) -> Bool {
        guard let url,
              url.scheme?.lowercased() == "https",
              url.user == nil,
              url.password == nil,
              url.port == nil || url.port == 443,
              url.fragment == nil,
              let host = url.host?.lowercased()
        else { return false }
        let path = url.path
        if host == "www.youtube-nocookie.com" {
            let parts = path.split(separator: "/", omittingEmptySubsequences: true)
            guard parts.count == 2, parts[0] == "embed" else { return false }
            let videoID = String(parts[1])
            return videoID.utf8.count == 11
                && videoID.utf8.allSatisfy { byte in
                    (48...57).contains(byte)
                        || (65...90).contains(byte)
                        || (97...122).contains(byte)
                        || byte == 95 || byte == 45
                }
        }
        if host == "player.bilibili.com" {
            return path == "/player.html"
        }
        if host == "www.bilibili.com" {
            return path == "/blackboard/webplayer/mbplayer.html"
        }
        return false
    }

    private func isLocalRuntimeURL(_ url: URL?) -> Bool {
        guard let url else { return false }
        return url.scheme?.lowercased() == "http"
            && url.host?.lowercased() == ReaderLocalRuntimeServer.host
            && url.port == Int(ReaderLocalRuntimeServer.port)
            && localRuntimeServer.map {
                url.path.hasPrefix($0.baseURL.path)
            } == true
    }

    private func isFinishedLocalBookURL(
        _ url: URL?,
        bookID: String
    ) -> Bool {
        guard let url, isLocalRuntimeURL(url),
              let localRuntimeServer else { return false }
        let pdfPath = localRuntimeServer.baseURL
            .appendingPathComponent("shells/pdf.html").path
        let epubPath = localRuntimeServer.baseURL
            .appendingPathComponent("shells/epub.html").path
        guard url.path == pdfPath || url.path == epubPath,
              let components = URLComponents(
                url: url,
                resolvingAgainstBaseURL: false
              ) else { return false }
        let bookValues = (components.queryItems ?? [])
            .filter { $0.name == "book" }
            .compactMap(\.value)
        return bookValues == [bookID]
    }

    func isTrustedLocalRuntimeFeatureURL(_ url: URL?) -> Bool {
        isLocalRuntimeURL(url)
    }

    private func updateNativeAgentVoiceState() {
        let phase: String
        let active: Bool
        let busy: Bool
        let speaking: Bool
        let detail: String
        switch nativeAgentVoice.state {
        case .idle:
            (phase, active, busy, speaking, detail) =
                ("idle", false, false, false, "")
        case .requestingMicrophone:
            (phase, active, busy, speaking, detail) =
                ("requesting-microphone", false, true, false,
                 "正在申请麦克风")
        case .connecting:
            (phase, active, busy, speaking, detail) =
                ("connecting", false, true, false, "正在连接语音中继")
        case .listening:
            (phase, active, busy, speaking, detail) =
                ("listening", true, false, false, "连续听中")
        case .speaking:
            (phase, active, busy, speaking, detail) =
                ("speaking", true, false, true, "正在朗读回答")
        case .suspended:
            (phase, active, busy, speaking, detail) =
                ("suspended", true, true, false, "系统音频暂时中断")
        case .stopping:
            (phase, active, busy, speaking, detail) =
                ("stopping", false, true, false, "正在结束通话")
        case .failed(let message):
            (phase, active, busy, speaking, detail) =
                ("failed", false, false, false, message)
        }
        let payload: [String: Any] = [
            "phase": phase,
            "active": active,
            "busy": busy,
            "speaking": speaking,
            "detail": detail,
        ]
        if externalNativeAgentVoice {
            let status = ReaderNativeAgentStatus(
                phase: phase,
                active: active,
                busy: busy,
                speaking: speaking,
                detail: detail.isEmpty ? nil : detail
            )
            let store = ReaderNativeBridgeStore()
            try? store.writeAgentStatus(status)
            publishExternalNativeAgentEvent(
                "state",
                payload: ReaderNativeAgentEventPayload(
                    phase: phase,
                    active: active,
                    busy: busy,
                    speaking: speaking,
                    detail: detail
                )
            )
            if phase == "idle" {
                externalNativeAgentVoice = false
                externalNativeAgentControlTask?.cancel()
            }
        } else {
            sendNativeAgentVoiceEvent("state", payload: payload)
        }
    }

    private func resolvedNativePencilAction(
        mapping: NativePencilGestureMapping,
        preferredAction: UIPencilPreferredAction,
        fallback: NativePencilAction
    ) -> NativePencilAction? {
        switch mapping {
        case .disabled:
            return nil
        case .toggleEraser:
            return .toggleEraser
        case .toggleSelection:
            return .toggleSelection
        case .showPalette:
            return .showPalette
        case .followSystem:
            break
        }
        if preferredAction == .ignore {
            return nil
        }
        if preferredAction == .switchEraser
            || preferredAction == .switchPrevious
        {
            return .toggleEraser
        }
        if preferredAction == .showColorPalette
            || preferredAction == .showInkAttributes
        {
            return .showPalette
        }
        if #available(iOS 17.5, *) {
            if preferredAction == .showContextualPalette {
                return .showPalette
            }
            if preferredAction == .runSystemShortcut {
                // A system shortcut is owned by iPadOS, not by Reader.
                return nil
            }
        }
        return fallback
    }

    private func performNativePencilAction(
        _ action: NativePencilAction,
        gesture: NativePencilGesture,
        preferredAction: UIPencilPreferredAction
    ) {
        guard webView.url != nil else { return }
        switch action {
        case .toggleEraser:
            nativePencilInk.toggleEraser()
        case .toggleSelection:
            nativePencilInk.toggleSelection()
        case .showPalette:
            nativePencilInk.showPalette()
        }
        // Keep note/editor-specific gesture routing in the web host. Page ink
        // itself is still sampled only by PencilKit inside the App.
        let payload: [String: Any] = [
            "action": action.rawValue,
            "gesture": gesture.rawValue,
            "preferredAction": preferredAction.rawValue,
        ]
        guard
            JSONSerialization.isValidJSONObject(payload),
            let data = try? JSONSerialization.data(withJSONObject: payload),
            let literal = String(data: data, encoding: .utf8)
        else {
            return
        }
        webView.evaluateJavaScript(
            "window.__bwNativePencilPerform?.(\(literal))",
            completionHandler: nil
        )
    }

    private func receiveNativePencilDoubleTap(timestamp: TimeInterval) {
        // Newer iPadOS versions may call both the modern and deprecated
        // delegate entry points for the same physical tap. Keep it one-shot.
        if lastNativePencilTapTimestamp >= 0,
           abs(timestamp - lastNativePencilTapTimestamp) < 0.15
        {
            return
        }
        lastNativePencilTapTimestamp = timestamp
        let preferredAction = UIPencilInteraction.preferredTapAction
        guard let action = resolvedNativePencilAction(
            mapping: nativePencilSettings.doubleTap,
            preferredAction: preferredAction,
            fallback: .toggleEraser
        ) else {
            return
        }
        performNativePencilAction(
            action,
            gesture: .doubleTap,
            preferredAction: preferredAction
        )
    }

    private func handleNativeComputerContext(
        _ body: [String: Any]
    ) {
        guard
            let requestID = body["requestId"] as? String,
            directVoiceSafeID(requestID)
        else {
            return
        }
        guard
            body.count == 3,
            let action = body["action"] as? String,
            action == "context" || action == "active-reading",
            let rawFields = body["fields"] as? [String: Any],
            JSONSerialization.isValidJSONObject(rawFields),
            let data = try? JSONSerialization.data(withJSONObject: rawFields),
            let value = try? JSONDecoder().decode(
                DirectJSONValue.self,
                from: data
            ),
            let fields = value.objectValue
        else {
            replyNativeComputerContext(
                requestID: requestID,
                failure: DirectVoiceFailure(
                    code: "BW_NATIVE_COMPUTER_CONTEXT_SCHEMA",
                    message: "Reader 原生上下文请求格式无效",
                    retryable: false
                )
            )
            return
        }
        guard let bridge = nativeVoiceBridge else {
            replyNativeComputerContext(
                requestID: requestID,
                failure: DirectVoiceFailure(
                    code: "BW_NATIVE_COMPUTER_CONTEXT_INACTIVE",
                    message: "原生电脑语音会话未连接",
                    retryable: true
                )
            )
            return
        }
        Task { @MainActor [weak self, weak bridge] in
            guard let self, let bridge else { return }
            do {
                let result = try await bridge.requestReaderContext(
                    action: action,
                    fields: fields
                )
                self.replyNativeComputerContext(
                    requestID: requestID,
                    value: result
                )
            } catch {
                let failure = error as? DirectVoiceFailure
                    ?? DirectVoiceFailure(
                        code: "BW_NATIVE_COMPUTER_CONTEXT_FAILED",
                        message: error.localizedDescription,
                        retryable: true
                    )
                self.replyNativeComputerContext(
                    requestID: requestID,
                    failure: failure
                )
            }
        }
    }

    private func replyNativeComputerContext(
        requestID: String,
        value: DirectJSONValue? = nil,
        failure: DirectVoiceFailure? = nil
    ) {
        let payload: DirectJSONValue
        if let value, failure == nil {
            payload = .object([
                "requestId": .string(requestID),
                "ok": .bool(true),
                "value": value,
            ])
        } else {
            let resolved = failure ?? DirectVoiceFailure(
                code: "BW_NATIVE_COMPUTER_CONTEXT_FAILED",
                message: "Reader 原生上下文请求失败",
                retryable: true
            )
            payload = .object([
                "requestId": .string(requestID),
                "ok": .bool(false),
                "error": .object([
                    "code": .string(resolved.code),
                    "message": .string(resolved.message),
                    "retryable": .bool(resolved.retryable),
                ]),
            ])
        }
        guard
            let data = try? JSONEncoder().encode(payload),
            let literal = String(data: data, encoding: .utf8)
        else {
            return
        }
        webView.evaluateJavaScript(
            "window.__bwNativeComputerContextApplyResult?.(\(literal))"
        )
    }

}

extension ReaderWebViewModel {
    /// 把 toggle 被拒的原因回传给网页。
    ///
    /// 之前这条路径上的八个前置条件共用一个 `else { return }`,任何一条不满足都静默返回:
    /// 网页端只知道 postMessage 没抛异常,按钮不变色时无法区分是 App 没更新、URL 不匹配,
    /// 还是消息压根没到。这里只做上报,不改变任何控制流。
    fileprivate func reportNativeVoiceToggleRejected(_ info: [String: Any]) {
        var payload = info
        payload["appVersion"] = nativeAppBuildVersion
        guard
            let data = try? JSONSerialization.data(withJSONObject: payload),
            let json = String(data: data, encoding: .utf8)
        else {
            return
        }
        let script = """
        (() => {
          const info = \(json);
          window.__BW_NATIVE_COMPUTER_VOICE_LAST_REJECT__ = info;
          try {
            console.warn("[BWReader] native voice toggle rejected", info);
          } catch (error) {}
          try {
            window.dispatchEvent(new CustomEvent(
              "bw-native-computer-voice-reject",
              { detail: info }
            ));
          } catch (error) {}
          // iPad 上看不到 console,诊断必须直接显示在屏幕上,否则等于没加。
          // 只在被拒时出现,正常路径完全不触发。
          try {
            const failed = Object.keys(info).filter((key) => info[key] === false);
            const banner = document.createElement("div");
            banner.textContent = "语音按钮被 App 拒绝｜v" + info.appVersion +
              "｜未满足: " + (failed.length ? failed.join(", ") : "字段校验") +
              "｜count=" + info.bodyFieldCount +
              " action=" + info.action + " appKind=" + info.appKind;
            banner.setAttribute("style", [
              "position:fixed", "left:8px", "right:8px", "top:8px",
              "z-index:2147483647", "padding:10px 12px", "border-radius:10px",
              "background:rgba(176,0,32,.95)", "color:#fff",
              "font:13px/1.5 -apple-system,system-ui,sans-serif",
              "white-space:pre-wrap", "word-break:break-all",
              "box-shadow:0 2px 12px rgba(0,0,0,.35)"
            ].join(";"));
            banner.addEventListener("click", () => banner.remove());
            document.body.appendChild(banner);
            setTimeout(() => banner.remove(), 12000);
          } catch (error) {}
        })();
        """
        DispatchQueue.main.async { [weak self] in
            self?.webView.evaluateJavaScript(script, completionHandler: nil)
        }
    }
}

extension ReaderWebViewModel: NativeAgentVoiceSessionDelegate {
    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didChangeState state: NativeAgentVoiceState
    ) {
        updateNativeAgentVoiceState()
        if state == .listening, !nativeAgentVoiceWasReady {
            nativeAgentVoiceWasReady = true
            if externalNativeAgentVoice {
                publishExternalNativeAgentEvent("agent_ready")
            } else {
                sendNativeAgentVoiceEvent("agent_ready")
            }
        } else if state == .idle {
            nativeAgentVoiceWasReady = false
        } else if case .failed = state {
            nativeAgentVoiceWasReady = false
        }
    }

    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didUpdateTranscript text: String
    ) {
        if externalNativeAgentVoice {
            publishExternalNativeAgentEvent(
                "asr",
                payload: ReaderNativeAgentEventPayload(text: text)
            )
        } else {
            sendNativeAgentVoiceEvent("asr", payload: ["text": text])
        }
    }

    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didFinalizeUtterance text: String
    ) {
        if externalNativeAgentVoice {
            publishExternalNativeAgentEvent(
                "utterance",
                payload: ReaderNativeAgentEventPayload(text: text)
            )
        } else {
            sendNativeAgentVoiceEvent("utterance", payload: ["text": text])
        }
    }

    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didReceiveSpokenSegment text: String
    ) {
        if externalNativeAgentVoice {
            publishExternalNativeAgentEvent(
                "tts_seg",
                payload: ReaderNativeAgentEventPayload(text: text)
            )
        } else {
            sendNativeAgentVoiceEvent("tts_seg", payload: ["text": text])
        }
    }

    func nativeAgentVoiceSessionDidFinishSpeaking(
        _ session: NativeAgentVoiceSession
    ) {
        if externalNativeAgentVoice {
            publishExternalNativeAgentEvent("tts_end")
        } else {
            sendNativeAgentVoiceEvent("tts_end")
        }
    }

    func nativeAgentVoiceSession(
        _ session: NativeAgentVoiceSession,
        didFail error: Error
    ) {
        if externalNativeAgentVoice {
            publishExternalNativeAgentEvent(
                "error",
                payload: ReaderNativeAgentEventPayload(
                    error: error.localizedDescription
                )
            )
            externalNativeAgentVoice = false
            externalNativeAgentControlTask?.cancel()
        } else {
            sendNativeAgentVoiceEvent(
                "error",
                payload: ["error": error.localizedDescription]
            )
        }
    }
}

extension ReaderWebViewModel: WKScriptMessageHandler {
    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        if message.name == nativeComputerVoiceMessageName {
            // 只读采样,不影响下面 guard 的判定;仅用于 guard 失败时说明是哪一条。
            let sampledBody = message.body as? [String: Any]
            let rejection: [String: Any] = [
                "isMainFrame": message.frameInfo.isMainFrame,
                "sameWebView": message.webView === webView,
                "trustedReader": isTrustedReaderURL(webView.url),
                "bodyIsDictionary": sampledBody != nil,
                "bodyFieldCount": sampledBody?.count ?? -1,
                "action": (sampledBody?["action"] as? String) ?? "<missing>",
                "appKind": (sampledBody?["appKind"] as? String) ?? "<missing>",
                "currentURL": isLocalRuntimeURL(webView.url)
                    ? "native-local://<capability-redacted>"
                    : (webView.url?.absoluteString ?? "<nil>"),
                "expectedURL": localRuntimeServer == nil
                    ? "<local-runtime-unavailable>"
                    : "native-local://<capability-redacted>",
            ]
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                isTrustedReaderURL(webView.url),
                isTrustedReaderURL(message.frameInfo.request.url),
                let body = message.body as? [String: Any],
                body["action"] as? String == "toggle"
            else {
                reportNativeVoiceToggleRejected(rejection)
                return
            }
            let appKind: DirectVoiceTargetApp
            if body.count == 1, body["appKind"] == nil {
                // A cached Reader bundle may still send the original
                // one-field message. It can safely mean Codex only; selecting
                // Classic continues to require the explicit second field.
                appKind = .codexDesktop
            } else if
                body.count == 2,
                let rawAppKind = body["appKind"] as? String,
                let parsed = DirectVoiceTargetApp(rawValue: rawAppKind)
            {
                appKind = parsed
            } else {
                reportNativeVoiceToggleRejected(rejection)
                return
            }
            toggleNativeComputerVoice(appKind: appKind)
        } else if message.name == nativeComputerContextMessageName {
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                isTrustedReaderURL(webView.url),
                isTrustedReaderURL(message.frameInfo.request.url),
                let body = message.body as? [String: Any]
            else {
                return
            }
            handleNativeComputerContext(body)
        } else if message.name == nativeAgentVoiceMessageName {
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                isTrustedReaderURL(webView.url),
                isTrustedReaderURL(message.frameInfo.request.url),
                let body = message.body as? [String: Any]
            else {
                return
            }
            handleNativeAgentVoice(body)
        } else if message.name == nativePencilInkMessageName {
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                isTrustedReaderURL(webView.url),
                isTrustedReaderURL(message.frameInfo.request.url),
                let body = message.body as? [String: Any]
            else {
                return
            }
            if body["type"] as? String == "tool",
               body.count == 2,
               let rawTool = body["tool"] as? String
            {
                switch rawTool {
                case "pen": nativePencilInk.select(.pen)
                case "eraser": nativePencilInk.select(.eraser)
                case "selection": nativePencilInk.select(.selection)
                default: return
                }
                return
            }
            nativePencilInk.updateLayout(from: body)
        }
    }
}

extension ReaderWebViewModel: WKScriptMessageHandlerWithReply {
    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        if message.name == nativeAnkiMobileMessageName {
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                isTrustedReaderURL(webView.url),
                isTrustedReaderURL(message.frameInfo.request.url),
                let body = message.body as? [String: Any]
            else {
                replyHandler(nil, "AnkiMobile 投影来源无效")
                return
            }
            handleNativeAnkiMobileRequest(body, replyHandler: replyHandler)
            return
        }
        guard message.name == nativeLocalNotesMessageName else {
            replyHandler(nil, "不支持的本机笔记消息")
            return
        }
        guard
            message.frameInfo.isMainFrame,
            message.webView === webView,
            isTrustedReaderURL(webView.url),
            isTrustedReaderURL(message.frameInfo.request.url)
        else {
            replyHandler(nil, "本机笔记来源无效")
            return
        }

        let manager = ReaderLocalNotesManager.shared
        guard manager.isEnabled else {
            replyHandler([
                "handled": true,
                "status": 409,
                "response": [
                    "ok": false,
                    "code": "BW_NATIVE_NOTES_DISABLED",
                    "error": "本机笔记未启用，未向 Pi 或回环地址假提交",
                ],
            ], nil)
            return
        }
        guard
            let body = message.body as? [String: Any],
            Set(body.keys) == ["action", "payload"],
            body["action"] as? String == "create",
            let payload = body["payload"] as? [String: Any],
            Set(["text", "name"]).isSubset(of: Set(payload.keys)),
            Set(payload.keys).isSubset(
                of: Set(["text", "name", "file", "page"])
            ),
            let text = payload["text"] as? String,
            let name = payload["name"] as? String,
            !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            text.utf8.count <= 262_144,
            name.utf8.count <= 512
        else {
            replyHandler([
                "handled": true,
                "status": 400,
                "response": [
                    "ok": false,
                    "error": "本机笔记字段无效",
                ],
            ], nil)
            return
        }
        let sourceFile: String
        if let value = payload["file"] {
            guard let value = value as? String, value.utf8.count <= 8_192 else {
                replyHandler([
                    "handled": true,
                    "status": 400,
                    "response": [
                        "ok": false,
                        "error": "本机笔记来源无效",
                    ],
                ], nil)
                return
            }
            sourceFile = value
        } else {
            sourceFile = ""
        }
        let sourcePage: Int
        if let value = payload["page"] {
            guard let number = value as? NSNumber,
                  number.doubleValue.isFinite,
                  number.doubleValue.rounded() == number.doubleValue,
                  (0...10_000_000).contains(number.doubleValue)
            else {
                replyHandler([
                    "handled": true,
                    "status": 400,
                    "response": [
                        "ok": false,
                        "error": "本机笔记页码无效",
                    ],
                ], nil)
                return
            }
            sourcePage = number.intValue
        } else {
            sourcePage = 0
        }

        Task { @MainActor in
            do {
                let receipt = try await manager.createNote(
                    name: name,
                    text: text,
                    sourceFile: sourceFile,
                    sourcePage: sourcePage
                )
                replyHandler([
                    "handled": true,
                    "status": 200,
                    "response": [
                        "ok": true,
                        "note_path": receipt.notePath,
                        "obsidian_url": receipt.obsidianURL,
                    ],
                ], nil)
            } catch {
                replyHandler([
                    "handled": true,
                    "status": 500,
                    "response": [
                        "ok": false,
                        "error": error.localizedDescription,
                    ],
                ], nil)
            }
        }
    }
}

extension ReaderWebViewModel: WKNavigationDelegate {
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webContentProcessNeedsReload = true
        isLoading = false
        guard readerForeground, isLocalRuntimeURL(webView.url) else { return }
        reloadLocalRuntimeAfterRecoveryIfNeeded(serverRebuilt: false)
    }

    func webView(
        _ webView: WKWebView,
        didStartProvisionalNavigation navigation: WKNavigation!
    ) {
        isLoading = true
        loadError = nil
        nativePencilInk.invalidateDocument()
        bookUserStateImportTask?.cancel()
        bookUserStateImportTask = nil
        localPDFContentIdentityTask?.cancel()
        localPDFContentIdentityTask = nil
        bookUserStateContextGeneration &+= 1
    }

    func webView(
        _ webView: WKWebView,
        didFinish navigation: WKNavigation!
    ) {
        webContentProcessNeedsReload = false
        isLoading = false
        loadError = nil
        if let navigation,
           let pending = pendingLocalBookNavigation,
           pending.navigation === navigation {
            pendingLocalBookNavigation = nil
            let succeeded = currentLocalBook?.id == pending.bookID
                && currentLocalLibrary?.stableLibraryID == pending.libraryID
                && isFinishedLocalBookURL(webView.url, bookID: pending.bookID)
            if succeeded {
                // This is deliberately the only persistence point. Starting a
                // request is not proof that the indexed local book rendered.
                ReaderLastLocalBookStore.shared.save(
                    libraryID: pending.libraryID,
                    bookID: pending.bookID
                )
            }
            if let token = pending.restorationToken {
                finishLocalBookRestore(token: token, succeeded: succeeded)
            }
        }
        if let nativeVoiceBridge {
            updateNativeVoiceButton(state: nativeVoiceBridge.state)
        }
        setReaderForeground(readerForeground)
        updateNativeAgentVoiceState()
        if let deferred = deferredBookUserStateMessage {
            deferredBookUserStateMessage = nil
            showBookUserStateMessage(
                deferred.text,
                isError: deferred.isError
            )
        }
        if currentLocalBook != nil, isLocalRuntimeURL(webView.url) {
            scheduleLocalPDFContentIdentity()
            schedulePendingBookUserStateImport()
        }
        deliverPendingAnkiMobileCallbacks()
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation!,
        withError error: Error
    ) {
        recordLoadFailure(error, navigation: navigation)
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        recordLoadFailure(error, navigation: navigation)
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard
            let url = navigationAction.request.url,
            let scheme = url.scheme?.lowercased()
        else {
            decisionHandler(.allow)
            return
        }
        // WKFrameInfo.request is not a stable committed-document identity for
        // same-main-frame location.href navigations. Trust the WebView's
        // committed URL for a main-frame initiator, while subframes must still
        // prove their own origin and may never borrow main-frame authority.
        let sourceURL = navigationAction.sourceFrame.isMainFrame
            ? webView.url
            : navigationAction.sourceFrame.request.url

        if nativePencilInk.hasPendingOperations,
           navigationAction.targetFrame?.isMainFrame != false
        {
            nativePencilInk.reportNavigationBlocked()
            decisionHandler(.cancel)
            return
        }

        if ["about", "blob", "data"].contains(scheme) {
            decisionHandler(.allow)
            return
        }

        if scheme == "http" || scheme == "https" {
            if navigationAction.targetFrame?.isMainFrame != false,
               takeOverLibraryNavigation(url, sourceURL: sourceURL) {
                decisionHandler(.cancel)
                return
            }
            if navigationAction.targetFrame?.isMainFrame != false,
               takeOverRemoteBookNavigation(url, sourceURL: sourceURL) {
                decisionHandler(.cancel)
                return
            }
            if isTrustedReaderURL(url) {
                decisionHandler(.allow)
                return
            }
            // Only the three fixed player documents may remain as subframes.
            // Their own links cannot turn the book renderer into a browser,
            // and an unrelated external frame cannot borrow the main page's
            // authority to navigate into the allowlist.
            if navigationAction.targetFrame?.isMainFrame == false,
               isAllowedEmbeddedVideoURL(url),
               (isTrustedReaderURL(sourceURL)
                    || isAllowedEmbeddedVideoURL(sourceURL)) {
                decisionHandler(.allow)
                return
            }
            // The App-owned WebView is a book renderer, not a general PWA or
            // browser. External links leave the renderer and cannot become a
            // new storage/document authority inside this WKWebView.
            if navigationAction.targetFrame?.isMainFrame != false {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
            } else {
                decisionHandler(.cancel)
            }
            return
        }

        UIApplication.shared.open(url)
        decisionHandler(.cancel)
    }

    private func recordLoadFailure(
        _ error: Error,
        navigation: WKNavigation?
    ) {
        if let navigation,
           let pending = pendingLocalBookNavigation,
           pending.navigation === navigation {
            pendingLocalBookNavigation = nil
            if let token = pending.restorationToken {
                finishLocalBookRestore(token: token, succeeded: false)
            }
        }
        let nsError = error as NSError
        guard nsError.code != NSURLErrorCancelled else {
            return
        }
        isLoading = false
        loadError = error.localizedDescription
    }
}

extension ReaderWebViewModel: WKUIDelegate {
    private func readerDialogPresenter(for webView: WKWebView) -> UIViewController? {
        func topViewController(from controller: UIViewController?) -> UIViewController? {
            guard let controller else { return nil }
            if let presented = controller.presentedViewController {
                return topViewController(from: presented)
            }
            if let navigation = controller as? UINavigationController {
                return topViewController(from: navigation.visibleViewController)
            }
            if let tab = controller as? UITabBarController {
                return topViewController(from: tab.selectedViewController)
            }
            if let split = controller as? UISplitViewController,
               let last = split.viewControllers.last {
                return topViewController(from: last)
            }
            return controller
        }

        guard let root = webView.window?.rootViewController else { return nil }
        return topViewController(from: root)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        guard let presenter = readerDialogPresenter(for: webView) else {
            completionHandler()
            return
        }
        let alert = UIAlertController(
            title: "Reader",
            message: message,
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "好", style: .default) { _ in
            completionHandler()
        })
        presenter.present(alert, animated: true)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        guard let presenter = readerDialogPresenter(for: webView) else {
            completionHandler(false)
            return
        }
        let alert = UIAlertController(
            title: "Reader",
            message: message,
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in
            completionHandler(false)
        })
        alert.addAction(UIAlertAction(title: "确定", style: .destructive) { _ in
            completionHandler(true)
        })
        presenter.present(alert, animated: true)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        guard let presenter = readerDialogPresenter(for: webView) else {
            completionHandler(nil)
            return
        }
        let alert = UIAlertController(
            title: "Reader",
            message: prompt,
            preferredStyle: .alert
        )
        alert.addTextField { field in
            field.text = defaultText
        }
        alert.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in
            completionHandler(nil)
        })
        alert.addAction(UIAlertAction(title: "确定", style: .default) { _ in
            completionHandler(alert.textFields?.first?.text ?? "")
        })
        presenter.present(alert, animated: true)
    }

    @available(iOS 15.0, *)
    func webView(
        _ webView: WKWebView,
        requestMediaCapturePermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        type: WKMediaCaptureType,
        decisionHandler: @escaping (WKPermissionDecision) -> Void
    ) {
        let trustedOrigin = origin.protocol.lowercased() == "http"
            && origin.host.lowercased() == ReaderLocalRuntimeServer.host
            && origin.port == Int(ReaderLocalRuntimeServer.port)
        let trustedFrame = frame.isMainFrame
            && frame.webView === webView
            && isTrustedReaderURL(webView.url)
        decisionHandler(
            trustedOrigin && trustedFrame && type == .microphone
                ? .grant
                : .deny
        )
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url {
            webView.load(URLRequest(url: url))
        }
        return nil
    }
}

extension ReaderWebViewModel: UIPencilInteractionDelegate {
    func pencilInteractionDidTap(_ interaction: UIPencilInteraction) {
        receiveNativePencilDoubleTap(
            timestamp: ProcessInfo.processInfo.systemUptime
        )
    }

    @available(iOS 17.5, *)
    func pencilInteraction(
        _ interaction: UIPencilInteraction,
        didReceiveTap tap: UIPencilInteraction.Tap
    ) {
        receiveNativePencilDoubleTap(timestamp: tap.timestamp)
    }

    @available(iOS 17.5, *)
    func pencilInteraction(
        _ interaction: UIPencilInteraction,
        didReceiveSqueeze squeeze: UIPencilInteraction.Squeeze
    ) {
        guard squeeze.phase == .ended else {
            return
        }
        let preferredAction = UIPencilInteraction.preferredSqueezeAction
        guard let action = resolvedNativePencilAction(
            mapping: nativePencilSettings.squeeze,
            preferredAction: preferredAction,
            fallback: .showPalette
        ) else {
            return
        }
        performNativePencilAction(
            action,
            gesture: .squeeze,
            preferredAction: preferredAction
        )
    }
}

struct ReaderWebView: UIViewRepresentable {
    @ObservedObject var model: ReaderWebViewModel

    func makeUIView(context: Context) -> WKWebView {
        model.loadIfNeeded()
        return model.webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}
}

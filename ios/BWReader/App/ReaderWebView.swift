import Combine
import SwiftUI
import UIKit
import WebKit

private let nativeComputerVoiceMessageName = "bwNativeComputerVoice"
private let nativeComputerContextMessageName = "bwNativeComputerContext"
private let nativeAgentVoiceMessageName = "bwNativeAgentVoice"
private let nativePencilInkMessageName = "bwNativePencilInk"
private let nativeLocalNotesMessageName = "bwNativeLocalNotes"

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

    enum NativeVoiceHandoffError: LocalizedError {
        case webVoiceAlreadyActive
        case contextRelayUnavailable

        var errorDescription: String? {
            switch self {
            case .webVoiceAlreadyActive:
                return "网页电脑语音仍在通话，请先结束当前电脑语音"
            case .contextRelayUnavailable:
                return "Reader 原生上下文接力尚未准备好"
            }
        }
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
    private var nativeComputerVoiceMessageProxy: WeakScriptMessageHandler?
    private var nativeComputerContextMessageProxy: WeakScriptMessageHandler?
    private var nativeAgentVoiceMessageProxy: WeakScriptMessageHandler?
    private var nativePencilInkMessageProxy: WeakScriptMessageHandler?
    private var nativeLocalNotesMessageProxy: WeakScriptMessageHandlerWithReply?
    private var nativePiGateway: ReaderNativePiGateway?
    private var nativePiSyncBridge: ReaderNativePiSyncBridge?
    private var nativeBookOCRBridge: NativeBookOCRBridge?
    private var nativeBookOCRUpdateCancellable: AnyCancellable?
    private var bookUserStateWebAdapter: ReaderBookUserStateWebAdapter?
    private var bookUserStateCoordinator: ReaderBookUserStatePackageCoordinator?
    private let pendingBookUserStateStore =
        ReaderBookUserStatePendingImportStore.shared
    private var bookUserStateNotificationCancellables = Set<AnyCancellable>()
    private var bookUserStateImportTask: Task<Void, Never>?
    private var bookUserStateContextGeneration: UInt64 = 0
    private var currentLocalBook: ReaderLocalBookRecord?
    private weak var currentLocalLibrary: ReaderLocalLibraryManager?
    private var currentLocalBookContentSHA256: String?
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
        if let localRuntimeServer {
            let nativePiGateway = ReaderNativePiGateway(
                webView: webView,
                trustedBaseURL: localRuntimeServer.baseURL
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
                "display:none!important}";
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

    func loadIfNeeded() {
        guard webView.url == nil else {
            return
        }
        reload()
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
        bookUserStateContextGeneration &+= 1
        currentLocalBook = nil
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
                expectedContentSHA256: digest
            )
        }
        return digest
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
                self.resetBookUserStateContext(
                    baseURL: localRuntimeServer.baseURL
                )
                self.nativeBookOCRBridge?.updateTrustedContext(
                    baseURL: localRuntimeServer.baseURL,
                    localBookID: "localbook-welcome",
                    expectedContentSHA256: nil
                )
                self.webView.load(URLRequest(
                    url: localRuntimeServer.defaultShellURL(),
                    cachePolicy: .reloadIgnoringLocalCacheData,
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
        guard let localRuntimeServer else {
            library.reportError(
                ReaderLocalRuntimeError.serverUnavailable(
                    localRuntimeInitializationError ?? "本机 Reader 未初始化"
                )
            )
            return false
        }
        do {
            let access = try library.makeOpenAccess(for: book)
            try await localRuntimeServer.start()
            let url = try await localRuntimeServer.open(access)
            bookUserStateImportTask?.cancel()
            bookUserStateImportTask = nil
            bookUserStateContextGeneration &+= 1
            currentLocalBook = book
            currentLocalLibrary = library
            currentLocalBookContentSHA256 = book.contentSha256
            try? bookUserStateWebAdapter?.updateTrustedContext(
                baseURL: localRuntimeServer.baseURL,
                localBookId: book.id
            )
            nativeBookOCRBridge?.updateTrustedContext(
                baseURL: localRuntimeServer.baseURL,
                localBookID: book.id,
                expectedContentSHA256: book.contentSha256
            )
            loadError = nil
            webView.load(URLRequest(
                url: url,
                cachePolicy: .reloadIgnoringLocalCacheData,
                timeoutInterval: 30
            ))
            return true
        } catch {
            library.reportError(error)
            return false
        }
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
        if foreground, !wasForeground, restartLocalRuntime,
           let localRuntimeServer,
           isLocalRuntimeURL(webView.url) {
            Task { @MainActor [weak self, localRuntimeServer] in
                guard let self else { return }
                do {
                    try await localRuntimeServer.restartAfterForeground()
                    self.webView.reloadFromOrigin()
                } catch {
                    self.loadError = error.localizedDescription
                }
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
    }

    func prepareForNativeVoice() async throws {
        guard webView.url != nil else {
            throw NativeVoiceHandoffError.contextRelayUnavailable
        }
        let value = try await webView.callAsyncJavaScript(
            """
            const voice = window.RC && window.RC.computerVoice;
            if (!voice || typeof voice.isActive !== "function") {
              return "not-ready";
            }
            if (voice.isActive()) return "web-active";
            if (typeof voice.prepareNativeContextHandoff !== "function") {
              return "not-ready";
            }
            return await voice.prepareNativeContextHandoff();
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        let result = value as? String ?? "not-ready"
        if result == "web-active" {
            throw NativeVoiceHandoffError.webVoiceAlreadyActive
        }
        if result != "native-ready" {
            throw NativeVoiceHandoffError.contextRelayUnavailable
        }
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

    private func isLocalRuntimeURL(_ url: URL?) -> Bool {
        guard let url else { return false }
        return url.scheme?.lowercased() == "http"
            && url.host?.lowercased() == ReaderLocalRuntimeServer.host
            && url.port == Int(ReaderLocalRuntimeServer.port)
            && localRuntimeServer.map {
                url.path.hasPrefix($0.baseURL.path)
            } == true
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
            replyHandler(["handled": false], nil)
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
    func webView(
        _ webView: WKWebView,
        didStartProvisionalNavigation navigation: WKNavigation!
    ) {
        isLoading = true
        loadError = nil
        nativePencilInk.invalidateDocument()
        bookUserStateImportTask?.cancel()
        bookUserStateImportTask = nil
        bookUserStateContextGeneration &+= 1
    }

    func webView(
        _ webView: WKWebView,
        didFinish navigation: WKNavigation!
    ) {
        isLoading = false
        loadError = nil
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
            schedulePendingBookUserStateImport()
        }
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation!,
        withError error: Error
    ) {
        recordLoadFailure(error)
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        recordLoadFailure(error)
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
            if isTrustedReaderURL(url) {
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

    private func recordLoadFailure(_ error: Error) {
        let nsError = error as NSError
        guard nsError.code != NSURLErrorCancelled else {
            return
        }
        isLoading = false
        loadError = error.localizedDescription
    }
}

extension ReaderWebViewModel: WKUIDelegate {
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

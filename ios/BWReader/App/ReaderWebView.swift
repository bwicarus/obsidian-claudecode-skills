import SwiftUI
import UIKit
import WebKit

private let readerStartURL = URL(string: "https://bwicarus.taile44d0c.ts.net/pdf/")!
private let nativeComputerVoiceMessageName = "bwNativeComputerVoice"
private let nativeComputerContextMessageName = "bwNativeComputerContext"
private let nativeAgentVoiceMessageName = "bwNativeAgentVoice"

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

    let webView: WKWebView

    @Published private(set) var isLoading = false
    @Published private(set) var loadError: String?
    private var nativeComputerVoiceMessageProxy: WeakScriptMessageHandler?
    private var nativeComputerContextMessageProxy: WeakScriptMessageHandler?
    private var nativeAgentVoiceMessageProxy: WeakScriptMessageHandler?
    private weak var nativeVoiceBridge: NativeVoiceBridge?
    private let nativeAgentVoice = NativeAgentVoiceSession()
    private var nativeAgentVoiceCommandTail: Task<Void, Never>?
    private var nativeAgentVoiceWasReady = false
    private var externalNativeAgentVoice = false
    private var externalNativeAgentControlTask: Task<Void, Never>?
    private var nativePencilInteraction: UIPencilInteraction?
    private var lastNativePencilTapTimestamp: TimeInterval = -1

    override init() {
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
        contentController.addUserScript(WKUserScript(
            source: """
            (() => {
              window.__BW_NATIVE_COMPUTER_VOICE__ = true;
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
            source: """
            (() => {
              if (location.protocol !== "https:" ||
                  location.hostname.toLowerCase() !==
                    "bwicarus.taile44d0c.ts.net") return;
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
        let request = URLRequest(
            url: readerStartURL,
            cachePolicy: .useProtocolCachePolicy,
            timeoutInterval: 30
        )
        webView.load(request)
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
        return url.scheme?.lowercased()
                == readerStartURL.scheme?.lowercased()
            && url.host?.lowercased()
                == readerStartURL.host?.lowercased()
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
        preferredAction: UIPencilPreferredAction,
        fallback: NativePencilAction
    ) -> NativePencilAction? {
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
        guard webView.url != nil else {
            return
        }
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
                "schemeMatches": webView.url?.scheme?.lowercased()
                    == readerStartURL.scheme?.lowercased(),
                "hostMatches": webView.url?.host?.lowercased()
                    == readerStartURL.host?.lowercased(),
                "bodyIsDictionary": sampledBody != nil,
                "bodyFieldCount": sampledBody?.count ?? -1,
                "action": (sampledBody?["action"] as? String) ?? "<missing>",
                "appKind": (sampledBody?["appKind"] as? String) ?? "<missing>",
                "currentURL": webView.url?.absoluteString ?? "<nil>",
                "expectedURL": readerStartURL.absoluteString,
            ]
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                webView.url?.scheme?.lowercased()
                    == readerStartURL.scheme?.lowercased(),
                webView.url?.host?.lowercased()
                    == readerStartURL.host?.lowercased(),
                let body = message.body as? [String: Any],
                body.count == 2,
                body["action"] as? String == "toggle",
                let rawAppKind = body["appKind"] as? String,
                let appKind = DirectVoiceTargetApp(rawValue: rawAppKind)
            else {
                reportNativeVoiceToggleRejected(rejection)
                return
            }
            toggleNativeComputerVoice(appKind: appKind)
        } else if message.name == nativeComputerContextMessageName {
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                webView.url?.scheme?.lowercased()
                    == readerStartURL.scheme?.lowercased(),
                webView.url?.host?.lowercased()
                    == readerStartURL.host?.lowercased(),
                let body = message.body as? [String: Any]
            else {
                return
            }
            handleNativeComputerContext(body)
        } else if message.name == nativeAgentVoiceMessageName {
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                webView.url?.scheme?.lowercased()
                    == readerStartURL.scheme?.lowercased(),
                webView.url?.host?.lowercased()
                    == readerStartURL.host?.lowercased(),
                let body = message.body as? [String: Any]
            else {
                return
            }
            handleNativeAgentVoice(body)
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
        updateNativeAgentVoiceState()
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

        let webSchemes = ["http", "https", "about", "blob", "data"]
        guard !webSchemes.contains(scheme) else {
            if (scheme == "http" || scheme == "https"),
               !isTrustedReaderURL(url),
               nativeAgentVoice.state != .idle {
                enqueueNativeAgentVoiceCommand(.stop)
            }
            decisionHandler(.allow)
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

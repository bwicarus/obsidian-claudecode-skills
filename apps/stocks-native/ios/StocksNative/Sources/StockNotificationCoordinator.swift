import AVFoundation
import CallKit
import Combine
import CryptoKit
import Foundation
import PushKit
import UIKit
import UserNotifications

extension Notification.Name {
    static let stocksOpenNotification = Notification.Name("stocks.openNotification")
}

/// One device binding and one system call. Receiving a push never starts paid voice.
@MainActor
final class StockNotificationCoordinator: NSObject, ObservableObject {
    static let shared = StockNotificationCoordinator()
    @Published private(set) var isCallActive = false
    @Published private(set) var notificationsEnabled = false
    @Published private(set) var status = "尚未启用通知"

    private struct Binding {
        let client: APIClient
        let deviceID: String
        let scope: String
    }
    private struct IncomingCall {
        let id: UUID
        let notificationID: String
        let binding: Binding
        var code: String?
        var answered = false
        var audioStarted = false
    }
    private weak var model: AppModel?
    private var binding: Binding?
    private var subscriptions = Set<AnyCancellable>()
    private var provider: CXProvider?
    private var registry: PKPushRegistry?
    private var pushToken: String?
    private var voipToken: String?
    private var currentCall: IncomingCall?
    private var callWatch: Task<Void, Never>?
    private var ringTimeout: Task<Void, Never>?
    private var answerReceipt: Task<Bool, Never>?
    private var presenceTask: Task<Void, Never>?
    private var registrationTask: Task<Void, Never>?
    private var foreground = false
    private var seenCalls: [String] = UserDefaults.standard.stringArray(forKey: "stocks.seenCalls") ?? []

    func configure(model: AppModel) {
        if self.model !== model {
            subscriptions.removeAll()
            self.model = model
            model.$baseURL.combineLatest(model.$isPaired, model.$isAIEnabled)
                .sink { [weak self] _ in
                    // Published emits before storage changes; read the new credentials next turn.
                    Task { @MainActor in self?.refreshBinding() }
                }.store(in: &subscriptions)
            model.voice.onSystemCallEnded = { [weak self] id in
                Task { @MainActor in
                    guard self?.currentCall?.id.uuidString.lowercased() == id else { return }
                    await self?.endCurrentCall()
                }
            }
        }
        setupSystemCalls()
        refreshBinding()
    }

    private func refreshBinding() {
        guard let model else { return }
        let client = model.client
        let rawScope = model.isPaired && client.token != nil
            ? client.baseURL.absoluteString + "|" + model.deviceID + "|" + (client.token ?? "") + "|ai:\(model.isAIEnabled)" : nil
        let scope = rawScope.map { SHA256.hash(data: Data($0.utf8)).map { String(format: "%02x", $0) }.joined() }
        guard scope != binding?.scope else { return }
        let old = binding
        binding = scope.map { Binding(client: client, deviceID: model.deviceID, scope: $0) }
        registrationTask?.cancel()
        presenceTask?.cancel()
        UNUserNotificationCenter.current().removeAllDeliveredNotifications()
        UNUserNotificationCenter.current().removeAllPendingNotificationRequests()
        if let old {
            // Preserve the old credentials for unbinding; never send old receipts as the new user.
            registrationTask = Task { [weak self] in
                _ = try? await Self.request(old, path: "api/notifications/device", payload: [
                    "deviceId": old.deviceID, "pushToken": NSNull(), "voipToken": NSNull(),
                    "environment": "production", "notificationsEnabled": false])
                guard !Task.isCancelled else { return }
                await self?.registerCurrentDevice()
            }
        } else {
            registrationTask = Task { [weak self] in await self?.registerCurrentDevice() }
        }
        if currentCall != nil { Task { await self.endCurrentCall(outcome: "ended") } }
        schedulePresence()
    }

    private func setupSystemCalls() {
        guard provider == nil else { return }
        let configuration = CXProviderConfiguration()
        configuration.supportsVideo = false
        configuration.maximumCallsPerCallGroup = 1
        configuration.maximumCallGroups = 1
        configuration.supportedHandleTypes = [.generic]
        let provider = CXProvider(configuration: configuration)
        provider.setDelegate(self, queue: .main)
        self.provider = provider
        let registry = PKPushRegistry(queue: .main)
        registry.delegate = self
        registry.desiredPushTypes = [.voIP]
        self.registry = registry
        UIApplication.shared.registerForRemoteNotifications()
    }

    func requestAuthorization() async {
        do {
            notificationsEnabled = try await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .badge, .sound])
            status = notificationsEnabled ? "通知已启用" : "通知未获授权，可在系统设置中开启"
            UIApplication.shared.registerForRemoteNotifications()
            await registerCurrentDevice()
        } catch { status = "通知授权失败：\(error.localizedDescription)" }
    }

    func setPushToken(_ data: Data) {
        pushToken = data.map { String(format: "%02x", $0) }.joined()
        Task { await registerCurrentDevice() }
    }

    func pushRegistrationFailed(_ error: Error) {
        status = "系统通知注册失败：\(error.localizedDescription)"
    }

    private func registerCurrentDevice() async {
        guard let binding else { return }
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        guard self.binding?.scope == binding.scope, !Task.isCancelled else { return }
        notificationsEnabled = settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
        var payload: [String: Any] = ["deviceId": binding.deviceID, "environment": "production",
                                     "notificationsEnabled": notificationsEnabled]
        if let pushToken { payload["pushToken"] = pushToken }
        // Review users can retain visual notifications but cannot receive AI calls.
        payload["voipToken"] = model?.isAIEnabled == true ? (voipToken as Any? ?? NSNull()) : NSNull()
        do {
            _ = try await Self.request(binding, path: "api/notifications/device", payload: payload)
            guard self.binding?.scope == binding.scope else { return }
            status = notificationsEnabled ? "通知已连接" : "可在监控页启用系统通知"
        } catch {
            guard self.binding?.scope == binding.scope else { return }
            status = "通知连接待恢复：\(error.localizedDescription)"
        }
    }

    func sceneChanged(active: Bool) {
        foreground = active
        refreshBinding()
        schedulePresence()
        if active { Task { await registerCurrentDevice() } }
    }

    private func schedulePresence() {
        presenceTask?.cancel()
        guard let binding else { return }
        presenceTask = Task { [weak self] in
            repeat {
                guard let self, self.binding?.scope == binding.scope, !Task.isCancelled else { return }
                _ = try? await Self.request(binding, path: "api/notifications/presence", payload: ["foreground": self.foreground])
                guard self.foreground else { return }
                do { try await Task.sleep(nanoseconds: 20_000_000_000) } catch { return }
            } while !Task.isCancelled
        }
    }

    private static func request(_ binding: Binding, path: String, payload: [String: Any]? = nil) async throws -> [String: Any] {
        var request = URLRequest(url: binding.client.baseURL.appendingPathComponent(path))
        request.timeoutInterval = 6
        request.setValue("Bearer \(binding.client.token ?? "")", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        // Device identity also accompanies GETs and receipts, independently of the owner token.
        request.setValue(binding.deviceID, forHTTPHeaderField: "X-Device-ID")
        if var payload {
            payload["deviceId"] = binding.deviceID
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: payload)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            throw AppError.message("通知请求未获服务端确认")
        }
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    private func receipt(_ call: IncomingCall, outcome: String) async -> Bool {
        do {
            _ = try await Self.request(call.binding, path: "api/notifications/receipt", payload: [
                "notificationId": call.notificationID, "channel": "call", "outcome": outcome,
                "callId": call.id.uuidString.lowercased()])
            return true
        } catch { status = "来电状态回报待恢复"; return false }
    }

    private func validatedNotification(_ info: [AnyHashable: Any]) async -> (Binding, [String: Any])? {
        guard let binding, let id = info["notificationId"] as? String, !id.isEmpty,
              let component = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              !id.contains("/"), !id.contains("..") else { return nil }
        guard let item = try? await Self.request(binding, path: "api/notifications/\(component)"),
              self.binding?.scope == binding.scope else { return nil }
        return (binding, item)
    }

    func presentNotification(_ info: [AnyHashable: Any], open: Bool) async -> Bool {
        guard let (binding, item) = await validatedNotification(info),
              let id = item["id"] as? String else { return false }
        _ = try? await Self.request(binding, path: "api/notifications/receipt", payload: [
            "notificationId": id, "channel": "visual", "outcome": "displayed"])
        guard self.binding?.scope == binding.scope else { return false }
        if open {
            var context: [String: Any] = ["notificationId": id]
            if let code = item["code"] as? String { context["code"] = code }
            NotificationCenter.default.post(name: .stocksOpenNotification, object: nil, userInfo: context)
        }
        return true
    }

    private func visualForCall(_ call: IncomingCall) async {
        guard let (_, item) = await validatedNotification(["notificationId": call.notificationID]),
              currentCall?.id == call.id, binding?.scope == call.binding.scope else { return }
        let content = UNMutableNotificationContent()
        content.title = item["title"] as? String ?? "股票提醒"
        content.body = item["body"] as? String ?? "监控信号已触发，点按查看。"
        content.userInfo = ["notificationId": call.notificationID, "code": item["code"] as? String ?? ""]
        // The system call already rings; the persistent visual fallback stays silent.
        try? await UNUserNotificationCenter.current().add(UNNotificationRequest(
            identifier: "stocks-\(call.notificationID)", content: content, trigger: nil))
        if binding?.scope != call.binding.scope {
            UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: ["stocks-\(call.notificationID)"])
        }
    }

    private func validateCall(_ call: IncomingCall) async -> [String: Any]? {
        guard binding?.scope == call.binding.scope else { return nil }
        guard let result = try? await Self.request(call.binding, path: "api/notifications/call/\(call.id.uuidString.lowercased())"),
              result["valid"] as? Bool == true,
              result["notificationId"] as? String == call.notificationID,
              binding?.scope == call.binding.scope else { return nil }
        if let expires = result["expiresAt"] as? Double, expires <= Date().timeIntervalSince1970 { return nil }
        return result
    }

    private func incoming(_ payload: [AnyHashable: Any], completion: @escaping () -> Void) {
        let parsedID = (payload["callId"] as? String).flatMap(UUID.init(uuidString:))
        let id = parsedID ?? UUID()
        guard let provider else { completion(); return }
        let update = CXCallUpdate()
        // Until ownership is verified, do not expose a previous account's stock information.
        update.remoteHandle = CXHandle(type: .generic, value: "股票提醒")
        update.hasVideo = false
        update.supportsHolding = false
        update.supportsGrouping = false
        update.supportsUngrouping = false
        update.supportsDTMF = false
        let duplicate = seenCalls.contains(id.uuidString.lowercased())
        let allowed = !duplicate && currentCall == nil && model?.voice.isStarted != true && model?.isAIEnabled == true
        let ntf = payload["notificationId"] as? String ?? ""
        let expires = payload["expiresAt"] as? Double ?? 0
        let validPayload = parsedID != nil && !ntf.isEmpty && expires.isFinite && expires > Date().timeIntervalSince1970
        var accepted: IncomingCall?
        if allowed, validPayload, let binding {
            let call = IncomingCall(id: id, notificationID: ntf, binding: binding)
            currentCall = call
            isCallActive = true
            accepted = call
            seenCalls.append(id.uuidString.lowercased())
            seenCalls = Array(seenCalls.suffix(100))
            UserDefaults.standard.set(seenCalls, forKey: "stocks.seenCalls")
        }
        let acceptedCall = accepted
        provider.reportNewIncomingCall(with: id, update: update) { [weak self] error in
            completion()
            Task { @MainActor in
                guard let self else { return }
                guard let call = acceptedCall else {
                    if self.currentCall?.id != id { provider.reportCall(with: id, endedAt: nil, reason: .failed) }
                    return
                }
                guard self.currentCall?.id == id else { return }
                guard error == nil else { await self.endCurrentCall(outcome: "failed"); return }
                await self.visualForCall(call)
                guard let result = await self.validateCall(call), self.currentCall?.id == id else {
                    if self.currentCall?.id == id { await self.endCurrentCall(outcome: "failed") }
                    return
                }
                self.currentCall?.code = result["code"] as? String
                let verifiedUpdate = CXCallUpdate()
                verifiedUpdate.remoteHandle = CXHandle(type: .generic, value: result["title"] as? String ?? "股票提醒")
                provider.reportCall(with: id, updated: verifiedUpdate)
            }
        }
        if accepted != nil {
            ringTimeout?.cancel()
            ringTimeout = Task { [weak self] in
                let seconds = max(1, min(45, expires - Date().timeIntervalSince1970))
                do { try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000)) } catch { return }
                guard self?.currentCall?.id == id, self?.currentCall?.answered == false else { return }
                await self?.endCurrentCall(outcome: "missed")
            }
        }
    }

    func endCurrentCall(outcome: String = "ended") async {
        guard let call = currentCall else { return }
        currentCall = nil
        isCallActive = false
        ringTimeout?.cancel(); ringTimeout = nil
        callWatch?.cancel(); callWatch = nil
        answerReceipt?.cancel(); answerReceipt = nil
        provider?.reportCall(with: call.id, endedAt: nil, reason: outcome == "missed" ? .unanswered : .remoteEnded)
        if model?.voice.systemCallID == call.id.uuidString.lowercased() { await model?.voice.stop() }
        _ = await receipt(call, outcome: outcome)
        status = outcome == "missed" ? "未接来电，提醒仍在通知列表" : "来电已结束"
    }

    private func startCallWatch(_ id: UUID) {
        callWatch?.cancel()
        callWatch = Task { [weak self] in
            let hardDeadline = Date().addingTimeInterval(600)
            var failedChecks = 0
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
                guard let self, let call = self.currentCall, call.id == id else { return }
                if Date() >= hardDeadline { await self.endCurrentCall(); return }
                if await self.validateCall(call) == nil { failedChecks += 1 } else { failedChecks = 0 }
                if failedChecks >= 2 { await self.endCurrentCall(); return }
            }
        }
    }
}

extension StockNotificationCoordinator: PKPushRegistryDelegate {
    nonisolated func pushRegistry(_ registry: PKPushRegistry, didUpdate credentials: PKPushCredentials, for type: PKPushType) {
        let token = credentials.token.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in self.voipToken = token; await self.registerCurrentDevice() }
    }
    nonisolated func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) {
        Task { @MainActor in self.voipToken = nil; await self.registerCurrentDevice() }
    }
    nonisolated func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload,
                                  for type: PKPushType, completion: @escaping () -> Void) {
        Task { @MainActor in self.incoming(payload.dictionaryPayload, completion: completion) }
    }
}

extension StockNotificationCoordinator: CXProviderDelegate {
    nonisolated func providerDidReset(_ provider: CXProvider) {
        Task { @MainActor in await self.endCurrentCall(outcome: "failed") }
    }
    nonisolated func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        Task { @MainActor in
            guard let call = self.currentCall, call.id == action.callUUID,
                  let result = await self.validateCall(call), self.currentCall?.id == call.id,
                  self.model?.voice.isStarted != true else {
                action.fail()
                if self.currentCall?.id == action.callUUID { await self.endCurrentCall(outcome: "failed") }
                return
            }
            do {
                try AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetooth])
            } catch { action.fail(); await self.endCurrentCall(outcome: "failed"); return }
            self.currentCall?.answered = true
            self.currentCall?.code = result["code"] as? String
            self.ringTimeout?.cancel(); self.ringTimeout = nil
            self.answerReceipt = Task { await self.receipt(call, outcome: "answered") }
            action.fulfill()
            self.startCallWatch(call.id)
            self.ringTimeout = Task { [weak self] in
                do { try await Task.sleep(nanoseconds: 20_000_000_000) } catch { return }
                guard self?.currentCall?.id == call.id, self?.currentCall?.audioStarted == false else { return }
                await self?.endCurrentCall(outcome: "failed")
            }
        }
    }
    nonisolated func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        Task { @MainActor in
            guard let call = self.currentCall, call.id == action.callUUID else { action.fulfill(); return }
            // Fulfill before any network await; local teardown is owned by the same task.
            action.fulfill()
            await self.endCurrentCall(outcome: call.answered ? "ended" : "declined")
        }
    }
    nonisolated func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        Task { @MainActor in
            guard let call = self.currentCall, call.answered, !call.audioStarted else { return }
            let reported = await self.answerReceipt?.value ?? false
            guard reported, self.currentCall?.id == call.id, self.binding?.scope == call.binding.scope,
                  let model = self.model, model.isAIEnabled, !model.voice.isStarted else {
                if self.currentCall?.id == call.id { await self.endCurrentCall(outcome: "failed") }
                return
            }
            self.currentCall?.audioStarted = true
            self.ringTimeout?.cancel(); self.ringTimeout = nil
            await model.voice.start(client: call.binding.client, deviceID: call.binding.deviceID,
                                    stockCode: call.code, systemCallID: call.id.uuidString.lowercased())
        }
    }
    nonisolated func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        Task { @MainActor in
            guard self.currentCall?.audioStarted == true else { return }
            await self.endCurrentCall()
        }
    }
    nonisolated func provider(_ provider: CXProvider, timedOutPerforming action: CXAction) {
        Task { @MainActor in
            guard let callAction = action as? CXCallAction, self.currentCall?.id == callAction.callUUID else { return }
            await self.endCurrentCall(outcome: "failed")
        }
    }
}

@MainActor
enum StocksAppRuntime {
    static let model = AppModel()
}

final class StocksAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        // PushKit background launches may never construct a SwiftUI view.
        MainActor.assumeIsolated {
            StockNotificationCoordinator.shared.configure(model: StocksAppRuntime.model)
        }
        return true
    }
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { @MainActor in StockNotificationCoordinator.shared.setPushToken(deviceToken) }
    }
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in StockNotificationCoordinator.shared.pushRegistrationFailed(error) }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        Task { @MainActor in
            let allowed = await StockNotificationCoordinator.shared.presentNotification(notification.request.content.userInfo, open: false)
            completionHandler(allowed ? [.banner, .list] : [])
        }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        Task { @MainActor in
            _ = await StockNotificationCoordinator.shared.presentNotification(response.notification.request.content.userInfo, open: true)
            completionHandler()
        }
    }
}

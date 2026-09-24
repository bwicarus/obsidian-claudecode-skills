import Combine
import CoreFoundation
import SwiftUI
import UIKit
import WebKit

private let nativeComputerVoiceMessageName = "bwNativeComputerVoice"
private let nativeComputerContextMessageName = "bwNativeComputerContext"
private let nativeAgentVoiceMessageName = "bwNativeAgentVoice"
private let nativePencilInkMessageName = "bwNativePencilInk"
private let nativeReadingProjectionMessageName = "bwNativeReadingProjection"
private let nativeReaderGeometryMessageName = "bwNativeReaderGeometry"
private let nativeLocalNotesMessageName = "bwNativeLocalNotes"
private let nativeAnkiMobileMessageName = "bwNativeAnkiMobile"
private let nativeAnkiPCMessageName = "bwNativeAnkiPC"
private let nativeConversationMessageName = "bwNativeConversation"
private let nativeDataStoreMessageName = "bwNativeDataStore"

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
    let nativeConversation = ReaderNativeConversationModel()
    private let localRuntimeServer: ReaderLocalRuntimeServer?
    private let localRuntimeInitializationError: String?

    @Published private(set) var isLoading = false
    @Published private(set) var loadError: String?
    /// 渲染进程被回收后的提示。⚠ 它不是"错误提示"，是**唯一的目击证词**：
    /// 页面已经重载、功能也恢复了，但如果这里不出声，这件事就等于没发生过 ——
    /// 用户只会说"点一下就崩"，而没有任何地方记得崩之前在做什么。
    @Published private(set) var webContentRecoveryNotice: String?
    /// 随手一句话的提示（顶层胶囊，几秒后自己消失）。
    /// ⚠ 存储属性必须写在**类体**里 —— extension 不能有存储属性，
    ///   这个错今天已经犯过两次（build 845 的泛型 static、849 的 extension）。
    @Published private(set) var transientNotice: String?
    private var transientNoticeTicket = 0
    /// 拖卡时画的落点预览（窗口坐标）。
    ///
    /// ⚠ 它**不能**是本对象的 @Published。这个模型被工作区、视口、页卡层一起观察，
    /// 发布一次就让**每一张卡**重算一遍 body —— 而拖动期间它每秒要发十来次。
    /// 单独一个小对象，只有画预览的那一层观察它。
    let cardDropPreviews = ReaderNativeDropPreviewModel()
    let cardDrag = ReaderNativeCardDragState()
    /// 文档卡片层（跟 PDF 同一帧滚的那层）此刻挂着没有。没挂的时候钉在页上的卡
    /// 必须仍由屏幕层画 —— 否则两边都不画，卡直接消失。
    @Published var documentCardLayerMounted = false
    private var dropPreviewStamp = Date.distantPast
    private var dropPreviewPoint = CGPoint(x: -10_000, y: -10_000)
    private var dropPreviewBusy = false
    private var webContentTerminationCount = 0
    @Published private(set) var libraryPresentationRequestID: UUID?
    /// 顶栏「App 设置」请求打开原生工具 sheet。与书库那条同一套做法：
    /// 网页按钮不做 URL 导航，直接经通道请求原生弹 sheet。
    @Published private(set) var nativeToolsPresentationRequestID: UUID?
    /// 设置面板「本机」tab 里的三个原生入口。它们各自弹**单一用途**的原生 UI，
    /// 而不是那张 12 个 Section 的大表 —— 用户 2026-08-18 要的就是这个分界。
    @Published private(set) var vaultPickerPresentationRequestID: UUID?
    @Published private(set) var realtimeKeyPresentationRequestID: UUID?
    @Published private(set) var piLoginPresentationRequestID: UUID?
    private var nativeComputerVoiceMessageProxy: WeakScriptMessageHandler?
    private var nativeConversationMessageProxy: WeakScriptMessageHandler?
    private var nativeComputerContextMessageProxy: WeakScriptMessageHandler?
    private var nativeAgentVoiceMessageProxy: WeakScriptMessageHandler?
    private var nativePencilInkMessageProxy: WeakScriptMessageHandler?
    private var nativeReadingProjectionMessageProxy: WeakScriptMessageHandler?
    private var nativeReaderGeometryMessageProxy: WeakScriptMessageHandlerWithReply?
    private var nativeDataStoreMessageProxy: WeakScriptMessageHandlerWithReply?
    private var nativeProjectionRefreshTask: Task<Void, Never>?
    private var nativeInkSurfaceTask: Task<Void, Never>?
    private var nativeLocalNotesMessageProxy: WeakScriptMessageHandlerWithReply?
    private var nativeAnkiMobileMessageProxy:
        WeakScriptMessageHandlerWithReply?
    private var nativeServerGateway: ReaderNativeServerGateway?
    private var nativeAssistantStream: ReaderNativeAssistantStreamBridge?
    private var nativeContextSelections: ReaderNativeContextSelectionBridge?
    private var nativeTurns: ReaderNativeTurnBridge?
    private let nativeAssistantSettings = ReaderNativeAssistantSettings()
    private var nativePhraseService: ReaderNativePhraseService?
    private var nativeFavoritesService: ReaderNativeFavoritesService?
    private var nativeFavoritesContext: UInt64?
    private var nativeReviewQueue: ReaderNativeReviewQueue?
    private var nativeReviewQueueContext: UInt64?
    private var nativeReviewQueueGatewayContext: UInt64?
    private var nativeReviewImprovements: ReaderNativeReviewImprovements?
    private var nativeReviewImprovementsContext: UInt64?
    private var nativeReviewImprovementsGatewayContext: UInt64?
    private weak var remoteLibraryCoordinator: ReaderRemoteLibraryCoordinator?
    private var nativeServerRemoteLibraryCancellable: AnyCancellable?
    private var nativeServerSyncBridge: ReaderNativeServerSyncBridge?
    private var nativeRealtimeBridge: ReaderNativeRealtimeBridge?
    private var nativeBookOCRBridge: NativeBookOCRBridge?
    private var nativePDFMutationBridge: ReaderNativePDFMutationBridge?
    private var nativePDFNavigationBridge: ReaderNativePDFNavigationBridge?
    private let nativePDFIndex = ReaderNativePDFIndex()
    private weak var activeNativePDFDocument: ReaderNativePDFDocument?
    /// 主阅读区挂上去的那份原生文档。**强引用在这里**：
    /// `prepareNativePDFDocument()` 的说明写着「调用方持有这个视口」，而在
    /// 2026-09-21 之前根本没有调用方 —— 组件建好了却从没挂上界面。
    /// SwiftUI 要能观察到它才画得出来，所以所有权落在 model 上。
    @Published private(set) var nativePDFDocument: ReaderNativePDFDocument?
    /// 挂载失败的原因。**要能看见** —— 否则原生阅读区白着而日志里什么都没有。
    @Published private(set) var nativePDFMountFailure: String?
    /// 原生查词/翻译面板。非 nil 即弹出（在 ReaderNativeWorkspace 里呈现）。
    @Published var nativeLookup: ReaderNativeLookupModel?
    /// 贴词小框（原版 #word-pop）：点词查词 / 词组的结果贴着那个词弹出，不走底部面板。
    @Published var nativeWordPop: ReaderNativeLookupModel?
    @Published var nativeWordPopAnchor: CGRect?
    @Published var nativeFigure: ReaderNativeFigureModel?
    @Published var nativeGrammar: ReaderNativeGrammarModel?
    @Published var nativeHighlightEditor: ReaderNativeHighlightEditorModel?
    /// EPUB 选区操作条上的色板。与网页工具栏同一份来源（RC.settings.hlColors），
    /// 所以用户改过色板之后两边一致。取不到就空着 —— 不猜一组默认色，
    /// 那会让他划出一个自己没设过的颜色。
    /// ⚠ 存储属性只能待在类主体里：extension 里放 @Published 会直接编译失败
    ///   （extensions must not contain stored properties）。2026-09-22 为此红过一轮。
    @Published private(set) var epubHighlightColors: [String] = []
    /// 本机数据库。⚠ 懒开：没开启新存储的用户不该因为装了这个版本就多出一个
    /// SQLite 文件 —— 第一次真有请求进来才建。
    private lazy var nativeDataStoreHost = ReaderNativeDataStoreHost()
    private var nativeReadingStoreBookID: String?
    private var nativeReadingStoreDeviceID: String?
    private var nativeRequestedPDFPage: Int?
    private var nativeReplicationService: ReaderNativeReplicationService?
    private var nativeInkDocumentToken = UUID().uuidString
    private var nativeInkSyncTasks: [String: Task<Void, Never>] = [:]
    private var nativePDFMutationCommandDepth = 0
    @Published private(set) var nativeInkHistoryBusy = false
    private var nativeHTMLNoteActions: [String:ReaderNativeHTMLNotes.Action] = [:]
    private var nativeHTMLNoteKey = ""
    private var nativeHTMLNotesEnabled = false
    private var nativeHTMLPinned = Set<String>()

    private func readingDomains(localBookID: String) async throws -> [ReaderBookUserStateDomainPayload] {
        if nativeReadingStoreBookID == localBookID {
            let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
            return try ReaderNativeBookProjection(store: store).exportReadingDomains(bookID: localBookID)
        }
        guard let adapter = bookUserStateWebAdapter else { throw ReaderBookUserStateWebAdapterError.unavailable }
        return try await adapter.exportPackage(localBookId: localBookID)
    }
    private var nativePDFMountTask: Task<Void, Never>?
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
    private var currentLocalBookAccess: ReaderLocalBookAccess? {
        didSet {
            let pdf = currentLocalBookAccess?.record.format == .pdf
            if nativePDFExpected != pdf { nativePDFExpected = pdf }
        }
    }
    /// 当前是本机 PDF 书：正文**只由 PDFKit 画**，网页层永远不露面（它只当数据层）。
    ///
    /// ⚠ 以前原生正文挂在一个默认关的开关后面，挂上之前 / 被卸下重挂的那一段，
    ///   屏幕上露出来的是网页渲的页和网页那套卡片、锁定框 —— 2026-09-23 用户截图里
    ///   "刚做好是细线框、滚动有残影，翻页回来又变了样"就是两套渲染来回换。
    ///   用户："不能就把网页的渲染直接彻底删掉么 app 里不需要啊"。
    @Published private(set) var nativePDFExpected = false
    /// 原生正文没能打开的原因。非 nil = 显示错误与「重试」，**不退回网页渲页**。
    @Published private(set) var nativePDFOpenFailure: String?
    private var nativePDFSelectionSequence = 0
    private weak var currentLocalLibrary: ReaderLocalLibraryManager?
    private var currentLocalBookContentSHA256: String?
    private var pendingLocalBookNavigation: PendingLocalBookNavigation?
    private var remoteBookNavigationTask: Task<Void, Never>?
    // 最近一次 openLocalBook 抛错的人话原因。只为启动恢复失败的横幅服务:
    // 那个时点没有别的诊断出口(runtime 未起、复制通道未连)。
    private(set) var lastLocalBookOpenFailure: String?
    private var localBookRestoreContinuations = [
        UUID: CheckedContinuation<Bool, Never>
    ]()
    private var waitsForInitialBookDecision = true
    private var deferredBookUserStateMessage: (text: String, isError: Bool)?
    private weak var nativeVoiceBridge: NativeVoiceBridge?
    /// 键盘通知的观察者句柄（重建 webView 时要先撤掉旧的）。
    private var keyboardInsetObservers: [NSObjectProtocol] = []
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
    /// 上一次发布出去的各域摘要串。内容没变就不重发 —— 导出要在页面里跑 JS
    /// 并算八个域的摘要，白发一次不便宜。换书时不必清：指纹里带着域摘要，
    /// 换了书自然对不上。
    private var lastPublishedUserStateFingerprint: String?
    /// .inactive 宽限期的定时任务；回到 .active 时取消（见 setReaderScenePhase）
    private var readerInactiveGraceTask: Task<Void, Never>?
    private var webContentProcessNeedsReload = false
    private let ankiMobilePendingStore = ReaderAnkiMobilePendingStore.shared
    private var pendingAnkiMobileExports = [String: PendingAnkiMobileExport]()
    private var nativeAnkiMobileExpiryTask: Task<Void, Never>?
    private var nativeAnkiPCTasks: [String: Task<[String: Any], Never>] = [:]
    private var nativeAnkiPCRecovered = false
    private var nativeAnkiPCRetryTask: Task<Void, Never>?

    func setNativeConversationMode(_ enabled: Bool) async {
        guard isTrustedReaderURL(webView.url), !isLoading else { return }
        _ = try? await webView.callAsyncJavaScript(
            "if (window.__bwNativeConversation) { window.__bwNativeConversation.setNativeMode(enabled); }",
            arguments: ["enabled": enabled], in: nil, contentWorld: .page
        )
    }

    /// 页卡在屏幕上的位置。
    ///
    /// ⚠ 原生正文接管时**必须用原生几何**：网页那侧的 rect 是从 DOM 推出来的，
    /// 而接管之后网页既不渲页、滚动也不跟着动 —— 那套坐标已经不对应屏幕上的
    /// 任何东西。`noteGeometry` 走 PDFKit 解锚，是唯一还成立的来源。
    /// 返回 nil = 没有原生几何，调用方退回网页那条路（不猜）。
    func nativePageCardGeometry(id: String, size: CGSize?, in container: CGRect) -> CGRect? {
        guard let document = nativePDFDocument,
              let note = document.notes.first(where: { $0["id"] as? String == id }),
              let geometry = document.noteGeometry(note) else { return nil }   // size 是归一化的，不能当视图点
        _ = size
        return document.view.convert(geometry.rect, to: nil)
            .offsetBy(dx: -container.minX, dy: -container.minY)
    }

    /// 卡片在文档层里的位置（PDFView 文档视图的坐标）。它不随滚动变化，
    /// 所以文档层滚动时 SwiftUI 不用重算任何东西 —— 跟随全靠宿主的同帧变换。
    func nativePageCardDocumentRect(id: String, size: CGSize?) -> CGRect? {
        guard let document = nativePDFDocument, let content = document.view.documentView,
              let note = document.notes.first(where: { $0["id"] as? String == id }),
              let geometry = document.noteGeometry(note) else { return nil }
        // ⚠ 不传 presentationSize：placement.size 是**除以网页视口**的归一化值，
        //   noteGeometry 却把它当视图点用 —— 调过大小的卡会被算成 0.3×0.2 点。
        //   原生改尺寸本来就写回便签自己的 w/h，noteGeometry 按 w/h × 页宽比例换算即可。
        _ = size
        let rect = content.convert(geometry.rect, from: document.view)
        return rect.offsetBy(dx: -content.bounds.minX, dy: -content.bounds.minY)
    }

    /// 词锚卡此刻是否展开。便签来源的卡开合只由原生管（nativeOpenBoundNotes）。
    func isBoundCardOpen(_ item: ReaderNativePagePlacement) -> Bool {
        item.fromNote ? nativeOpenBoundNotes.contains(item.noteID) : item.open
    }

    /// 点正文里的锁定框 → 展开 / 收起那张卡。
    ///
    /// 原生正文下开合是原生自己的状态（nativeOpenBoundNotes），卡由文档层按便签数据画；
    /// 只有网页在渲页时，才走网页挂的那张卡的 `toggleBound` 控件。
    /// 找不到就**出声** —— "点了没反应"是这块地方已经栽过一次的坑。
    func openNativeBoundCard(noteID: String) {
        guard !noteID.isEmpty else { return }
        traceCardPipeline("tap", noteID: noteID)
        // 点完 1.5 秒再看一次：文档层有没有真把卡画出来（drawn=）。
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            self?.traceCardPipeline("after-tap", noteID: noteID)
        }
        let placement = nativeConversation.placements.first(where: { $0.noteID == noteID })
        // 网页在渲页时挂的卡：走它自己的开合。
        if let placement, !placement.fromNote, let action = placement.controls["toggleBound"] {
            nativeOpenBoundNotes.remove(noteID)
            Task { [weak self] in
                guard let self else { return }
                if await nativeConversation.perform("liveAction", parameters: ["actionId": action]) == false {
                    showTransientNotice(nativeConversation.error ?? "卡片没能打开，请重试。")
                }
            }
            return
        }
        // 原生正文下：开合只是原生自己的状态，卡由文档层按便签数据画。
        // 内容还没到（快照只带当前页前后几页）就说出来，不静默。
        guard placement != nil || nativeOpenBoundNotes.contains(noteID) else {
            // 快照可能只是旧了：便签在网页那侧已经载入，只是还没有事件触发下一次快照
            // （2026-09-23 实录：网页交得出 14 张、含这一张，原生这边却是 0 张）。
            // 先要一次新快照再判，别直接说"没同步"。
            probeNoteCards(noteID: noteID)
            Task { @MainActor [weak self] in
                guard let self else { return }
                _ = await self.nativeConversation.perform("snapshot")
                for _ in 0..<10 where !self.nativeConversation.placements.contains(where: { $0.noteID == noteID }) {
                    try? await Task.sleep(nanoseconds: 100_000_000)
                }
                if self.nativeConversation.placements.contains(where: { $0.noteID == noteID }) {
                    self.nativeOpenBoundNotes = [noteID]
                } else {
                    self.showTransientNotice("这张卡的内容还没同步到本机，请稍后再点。")
                }
            }
            return
        }
        if nativeOpenBoundNotes.contains(noteID) { nativeOpenBoundNotes.remove(noteID) }
        else { nativeOpenBoundNotes = [noteID] }   // 一次只展开一张（原版 toggleBoundCard 同规则）
    }

    /// 卡片链路诊断：把"点锁定框 → 开合 → 画卡"每一环的实际状态写进回传服务器的客户端日志。
    ///
    /// ⚠ 为什么要它：这条链已经连续修错好几轮（2026-09-23 用户："这个问题已经连续错了
    ///   太多次了"）。每一轮都是看截图推测哪一环坏了，而这台 iPad 我摸不到。
    ///   与其再猜一轮，不如让它自己把现场说出来 —— 网页挂没挂这张卡、开合状态、原生能不能
    ///   取到便签和几何、两种摆法各算出什么、文档层挂没挂。
    func traceCardPipeline(_ stage: String, noteID: String) {
        var parts: [String] = ["[card-trace] " + stage, "id=" + String(noteID.prefix(12))]
        guard let document = nativePDFDocument else {
            parts.append("nativePDF=nil")
            postClientLog(parts.joined(separator: " "))
            return
        }
        let placement = nativeConversation.placements.first { $0.noteID == noteID }
        if let placement {
            parts.append("placement=\(placement.fromNote ? "note" : "web") open=\(isBoundCardOpen(placement)) bound=\(placement.bound) form=\(placement.form)")
            parts.append("ctrls=" + placement.controls.keys.sorted().joined(separator: ","))
        } else {
            parts.append("placement=none")
        }
        parts.append("nativeOpen=\(nativeOpenBoundNotes.contains(noteID))")
        parts.append("docLayer=\(documentCardLayerMounted) drawn=\(documentLayerCardCount)")
        if let note = document.notes.first(where: { $0["id"] as? String == noteID }) {
            let anchor = note["anchor"] as? [String: Any] ?? [:]
            parts.append("anchor=\(anchor["kind"] ?? "-")/p\(anchor["page"] ?? "-")")
            let payload = note["card"] as? [String: Any] ?? note["html"] as? [String: Any] ?? [:]
            let bind = payload["bind"] as? [String: Any] ?? [:]
            parts.append("bind=p\(bind["page"] ?? "-") \(String(describing: bind["text"] ?? "-").prefix(12))")
            if let geometry = document.noteGeometry(note) {
                parts.append("geo=\(Int(geometry.rect.minX)),\(Int(geometry.rect.minY)) \(Int(geometry.rect.width))x\(Int(geometry.rect.height)) bindRects=\(geometry.bindingRects.count)")
            } else {
                parts.append("geo=nil")
            }
            parts.append("wordRect=" + (nativeWordCardDocumentRect(id: noteID, size: nil).map { "\(Int($0.minX)),\(Int($0.minY))" } ?? "nil"))
            parts.append("pageRect=" + (nativePageCardDocumentRect(id: noteID, size: nil).map { "\(Int($0.minX)),\(Int($0.minY))" } ?? "nil"))
        } else {
            parts.append("note=missing notes=\(document.notes.count)")
        }
        parts.append("placements=\(nativeConversation.placements.count)")
        postClientLog(parts.joined(separator: " "))
    }

    /// 点卡打不开时，问网页那侧：便签模块一共交得出几张卡、这张在不在里面、它以为当前是第几页。
    private func probeNoteCards(noteID: String) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            let value = try? await webView.callAsyncJavaScript(
                """
                try {
                  const s = window.RC?.stickynote;
                  const all = typeof s?.nativeNoteCards === 'function' ? s.nativeNoteCards(null) : null;
                  const nav = window.RC?.readerNavigation?.state?.() || {};
                  return JSON.stringify({ api: typeof s?.nativeNoteCards, all: all ? all.length : -1,
                    has: !!(all && all.some(c => c.id === id)), pos: nav.position ?? null,
                    viewport: !!window.RC?.readerNavigation?.nativeViewport });
                } catch (e) { return 'err:' + String(e && e.message || e).slice(0, 120); }
                """, arguments: ["id": noteID], in: nil, contentWorld: .page)
            postClientLog("[card-probe] id=" + String(noteID.prefix(12)) + " " + String(describing: value ?? "nil"))
        }
    }

    /// 写一行到网页那条已经在回传服务器的客户端日志（__bwClientLog）。
    func postClientLog(_ line: String) {
        ReaderNativeFaultReporter.shared.note("trace", String(line.prefix(180)))
        guard let data = try? JSONSerialization.data(withJSONObject: [line]),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript(
            "(function(m){try{if(window.__bwClientLog)window.__bwClientLog('log',m);else if(window.dlog)window.dlog(m);}catch(e){}})(" + json + "[0])",
            completionHandler: nil)
    }

    /// 文档层此刻实际画出了几张卡（它自己用 preference 报上来）。只给诊断用。
    var documentLayerCardCount = 0

    /// 原生正文被卸下的原因。卸下那一刻网页多半也在重载，日志发不出去 —— 先攒着，
    /// 下一次原生正文重新挂上时一起报。
    private var nativePDFLifecycleNotes: [String] = []

    /// 原生正文下展开着的词锚卡（开合只在原生，一次一张）。
    @Published var nativeOpenBoundNotes: Set<String> = []

    /// 拖卡松手的落点：页码 + 页内归一化坐标 + 原生认出的词（认不出就没有 bind）。
    func nativeDropTarget(windowPoint: CGPoint) -> [String: Any]? {
        guard let document = nativePDFDocument else { return nil }
        let local = document.view.convert(windowPoint, from: nil)
        guard let placed = document.pagePoint(at: local) else { return nil }
        var value: [String: Any] = ["page": placed.page, "x": Double(placed.point.x), "y": Double(placed.point.y)]
        if let word = document.wordBind(at: local) { value["bind"] = word.payload }
        return value
    }

    /// 展开的词锚卡贴着它的词摆 —— 照原版 `_placeWordCard`：词右侧留 10；右边放不下
    /// 就放到词左侧；竖直方向与词居中。
    /// ⚠ 原版最后夹进**屏幕**，这里夹进**页面**：夹屏幕的话位置随滚动变，文档层就又得
    ///   每帧重排（= 残影）；夹页面则滚动时一动不动。
    func nativeWordCardDocumentRect(id: String, size: CGSize?) -> CGRect? {
        guard let document = nativePDFDocument, let content = document.view.documentView,
              let note = document.notes.first(where: { $0["id"] as? String == id }),
              // 不传 size：理由同上。按展开尺寸算 —— 打开的词锚卡总是完全展开。
              let geometry = document.noteGeometry(note, expanded: true),
              let word = geometry.bindingRects.last else { return nil }
        // 词所在的页（可能跟卡片锚点不是同一页）。
        let payload = note["card"] as? [String: Any] ?? note["html"] as? [String: Any]
        let bindPage = ((payload?["bind"] as? [String: Any])?["page"] as? NSNumber)?.intValue ?? geometry.page
        guard let page = document.viewRect(normalized: CGRect(x: 0, y: 0, width: 1, height: 1), page: bindPage)
        else { return nil }
        func doc(_ rect: CGRect) -> CGRect {
            content.convert(rect, from: document.view).offsetBy(dx: -content.bounds.minX, dy: -content.bounds.minY)
        }
        // size 给了就按卡片此刻在文档层里的真实大小摆（屏幕 1:1 画，见 nativeCardLayout）。
        let raw = doc(geometry.rect)
        let base = size.map { CGRect(origin: raw.origin, size: $0) } ?? raw
        let w = doc(word), frame = doc(page)
        let gap: CGFloat = 10, margin: CGFloat = 8
        var left = w.maxX + gap
        if left + base.width > frame.maxX - margin { left = w.minX - base.width - gap }
        var top = w.midY - base.height / 2
        left = min(max(frame.minX + margin, left), max(frame.minX + margin, frame.maxX - base.width - margin))
        top = min(max(frame.minY + margin, top), max(frame.minY + margin, frame.maxY - base.height - margin))
        return CGRect(x: left, y: top, width: base.width, height: base.height)
    }

    func nativePageCardRect(_ rect: CGRect, in container: CGRect) -> CGRect {
        let local = CGRect(x: rect.minX * webView.bounds.width, y: rect.minY * webView.bounds.height,
                           width: rect.width * webView.bounds.width, height: rect.height * webView.bounds.height)
        return webView.convert(local, to: nil).offsetBy(dx: -container.minX, dy: -container.minY)
    }

    func registerNativeCardInk(id: String, windowRect: CGRect?, occlusion: CGRect? = nil, aspectRatio: CGFloat, geometry: String) {
        guard let windowRect, windowRect.width > 0, windowRect.height > 0,
              webView.bounds.width > 0, webView.bounds.height > 0 else {
            nativePencilInk.setCardSurface(nil, id: id)
            return
        }
        let local = webView.convert(windowRect, from: nil)
        let rect = CGRect(x: local.minX / webView.bounds.width, y: local.minY / webView.bounds.height,
                          width: local.width / webView.bounds.width, height: local.height / webView.bounds.height)
        let outer = webView.convert(occlusion ?? windowRect, from: nil)
        let cover = CGRect(x: outer.minX / webView.bounds.width, y: outer.minY / webView.bounds.height,
                           width: outer.width / webView.bounds.width, height: outer.height / webView.bounds.height)
        nativePencilInk.setCardSurface(NativeInkSurface(id: "card:" + id, rect: rect, exclusions: [],
            aspectRatio: aspectRatio, geometry: geometry, occlusionRect: cover), id: id)
    }

    func applyNativeCardInk(actionID: String, value: [String: Any]) async throws {
        let receipt = await requestNativeConversationCommand(["action": "liveAction", "scope": nativeConversation.scope,
            "actionId": actionID, "value": value])
        guard receipt["ok"] as? Bool == true else {
            throw NSError(domain: "ReaderCardInk", code: 1, userInfo: [NSLocalizedDescriptionKey: receipt["error"] as? String ?? "卡片笔迹尚未保存"])
        }
    }

    /// 内容摘要变了（真实改页写回 PDF）→ 重挂原生正文。
    ///
    /// ⚠ 不重挂的后果不是"报错"，而是**静默停更**：原生那边显示的仍是改页前的
    /// 文档，而投影/划线/定位全都因为 `matches` 失败而默默什么都不做。
    /// 静默失败比崩溃更难发现 —— 用户只会觉得"插进去的页没出现"。
    private func remountNativePDFIfContentChanged(_ digest: String) {
        guard let document = nativePDFDocument, let bookID = currentLocalBook?.id,
              !document.matches(bookID: bookID, contentSHA256: digest) else { return }
        invalidateNativePDFDocument(reason: "content-digest-changed")
        // 另起一轮：当前这次调用可能正发生在 prepare 里面（它也会取摘要），
        // 就地重挂会递归。
        Task { @MainActor [weak self] in self?.mountNativePDFDocument() }
    }

    /// Visible-page vocabulary is projected from native character data and the
    /// original local vocabulary store. Cached/server enrichment follows without
    /// delaying local rendering or creating hidden web page layers.
    private var nativeOverlayPages: Set<Int> = []
    private var nativeOverlayOrder: [Int] = []
    private var nativeOverlayLoadTasks: [Int: Task<Void, Never>] = [:]
    private var nativeOverlayTickets: [Int: UUID] = [:]
    private weak var nativeOverlayDocument: ReaderNativePDFDocument?
    private let nativeVocabularyOverlayStore = ReaderNativePageOverlayStore()
    private var nativeOverlayGeneration: UInt64 = 0
    private var nativeOverlayVocabularyGeneration: UInt64?
    private var nativeVocabularyEnrichmentTasks: [Int: Task<Void, Never>] = [:]
    private var nativePageTranslationTasks: [Int: Task<Void, Never>] = [:]
    private var nativePageTranslationCache: [String: [[String: Any]]] = [:]
    private var nativePageTranslationCacheBytes = 0
    private let nativeFigures = ReaderNativeFigures()
    private var nativeFigureTasks: [Int: Task<Void, Never>] = [:]
    private var nativeFigurePending: Set<Int> = []
    private var nativeReadingFiguresSetting: (generation: UInt64, enabled: Bool?, warning: String?)?

    private func cancelNativePageWork(_ page: Int) {
        nativeOverlayTickets.removeValue(forKey: page)
        nativeOverlayLoadTasks.removeValue(forKey: page)?.cancel()
        nativeVocabularyEnrichmentTasks.removeValue(forKey: page)?.cancel()
        nativePageTranslationTasks.removeValue(forKey: page)?.cancel()
        nativeFigureTasks.removeValue(forKey: page)?.cancel()
        nativeOverlayPages.remove(page)
    }

    private func cancelAllNativePageWork() {
        for page in Array(nativeOverlayTickets.keys) { cancelNativePageWork(page) }
    }

    private func refreshNativePageOverlays(force: Bool = false) {
        guard let document = nativePDFDocument else { return }
        if let bookID = currentLocalBook?.id, bookID != nativeFigures.bookID {
            nativeFigures.open(bookID); nativeFigurePending.removeAll()
        }
        let vocabularyGeneration = (try? nativeDataStoreHost.bridge(for: "bw-reader-native-v1-global").store)?.generation(collection: ReaderNativeVocabularyState.collection)
        if nativeOverlayDocument !== document || force || vocabularyGeneration != nativeOverlayVocabularyGeneration {
            cancelAllNativePageWork()
            nativeOverlayDocument = document
            nativeOverlayVocabularyGeneration = vocabularyGeneration
            nativeOverlayPages = []
            nativeOverlayGeneration &+= 1
            nativeVocabularyEnrichmentTasks.values.forEach { $0.cancel() }
            nativeVocabularyEnrichmentTasks = [:]
            nativePageTranslationTasks.values.forEach { $0.cancel() }
            nativePageTranslationTasks = [:]
            nativeFigureTasks.values.forEach { $0.cancel() }
            nativeFigureTasks = [:]
        }
        let visible = Set(document.position.visiblePages.prefix(8))
        let running = Set(nativeOverlayLoadTasks.keys).union(nativeVocabularyEnrichmentTasks.keys)
            .union(nativePageTranslationTasks.keys).union(nativeFigureTasks.keys)
        for page in running.subtracting(visible) { cancelNativePageWork(page) }
        nativeOverlayOrder.removeAll { visible.contains($0) }
        nativeOverlayOrder.append(contentsOf: visible.sorted())
        while nativeOverlayOrder.count > 24 {
            cancelNativePageWork(nativeOverlayOrder.removeFirst())
        }
        document.retainPageDecorations(Set(nativeOverlayOrder))
        for page in document.position.visiblePages.prefix(8) where !nativeOverlayPages.contains(page) || (nativeFigurePending.contains(page) && nativeFigureTasks[page] == nil) {
            guard nativeOverlayLoadTasks[page] == nil else { continue }
            cancelNativePageWork(page)
            nativeOverlayPages.insert(page)
            let generation = nativeOverlayGeneration
            let ticket = UUID(); nativeOverlayTickets[page] = ticket
            nativeOverlayLoadTasks[page] = Task { @MainActor [weak self, weak document] in
                guard let self, let document else { return }
                defer { if self.nativeOverlayTickets[page] == ticket { self.nativeOverlayLoadTasks.removeValue(forKey: page) } }
                do {
                    let input = try await self.loadNativeVocabularyOverlay(page: page, document: document, generation: generation, ticket: ticket)
                    try Task.checkCancellation()
                    self.startNativePageTranslation(source: input.source, enabled: input.flags["translation"] as? Bool == true,
                        page: page, document: document, generation: generation, ticket: ticket)
                } catch {
                    if self.nativePDFDocument === document, self.nativeOverlayGeneration == generation, self.nativeOverlayTickets[page] == ticket, !Task.isCancelled {
                        self.nativeOverlayPages.remove(page)
                        self.postClientLog("native-page-overlay: " + error.localizedDescription)
                    }
                }
                guard self.nativePDFDocument === document, self.nativeOverlayGeneration == generation, self.nativeOverlayTickets[page] == ticket, !Task.isCancelled else { return }
                // A figures-only page may have no text/OCR, so this path must
                // remain independent of the character-layer result above.
                let enabled = try? await self.webView.callAsyncJavaScript(
                    "return window.__figBookOn===true;", arguments: [:], in: nil, contentWorld: .page)
                guard self.nativePDFDocument === document, self.nativeOverlayGeneration == generation, self.nativeOverlayTickets[page] == ticket, !Task.isCancelled else { return }
                self.startNativeFigures(enabled: enabled as? Bool == true, page: page, document: document, generation: generation, ticket: ticket)
            }
        }
    }

    private func displayNativeFigures(page: Int, document: ReaderNativePDFDocument) {
        let items = (nativeFigures.figures(page: page) ?? []).compactMap { row -> ReaderNativePDFDocument.Figure? in
            guard let id = row["id"] as? String, let box = row["box"] as? [Double], box.count == 4 else { return nil }
            let badge = (row["badge"] as? [Double]).map { CGPoint(x: $0[0], y: $0[1]) }
            return .init(id: id, page: page, box: CGRect(x: box[0], y: box[1], width: box[2] - box[0], height: box[3] - box[1]),
                badge: badge, caption: row["caption"] as? String ?? "", desc: row["desc"] as? String ?? "",
                group: row["group"] as? Bool == true, attached: row["attached"] as? Bool == true)
        }
        document.setFigures(items, page: page)
    }

    private func startNativeFigures(enabled: Bool, page: Int, document: ReaderNativePDFDocument, generation: UInt64, ticket: UUID) {
        nativeFigureTasks[page]?.cancel(); nativeFigureTasks.removeValue(forKey: page)
        guard enabled else { document.setFigures([], page: page); return }
        if nativeFigures.figures(page: page) != nil {
            displayNativeFigures(page: page, document: document)
            if !nativeFigurePending.contains(page) { return }
        }
        guard let gateway = nativeServerGateway, let bookID = currentLocalBook?.id else { return }
        var url = URLComponents(); url.path = "/pdf/api/page-figures"
        url.queryItems = [URLQueryItem(name: "file", value: "localbook:" + bookID), URLQueryItem(name: "page", value: String(page))]
        guard let path = url.string else { return }
        nativeFigureTasks[page] = Task { @MainActor [weak self, weak document] in
            guard let self, let document else { return }
            defer { if self.nativeOverlayTickets[page] == ticket { self.nativeFigureTasks.removeValue(forKey: page) } }
            do {
                for attempt in 0..<9 {
                    try Task.checkCancellation()
                    guard self.nativePDFDocument === document, self.nativeOverlayGeneration == generation,
                          self.currentLocalBook?.id == bookID, self.nativeOverlayTickets[page] == ticket, document.position.visiblePages.contains(page) else { return }
                    let response = try await gateway.fetchData(path: path, surface: .pdf)
                    try Task.checkCancellation()
                    guard self.nativePDFDocument === document, self.nativeOverlayGeneration == generation,
                          self.currentLocalBook?.id == bookID, self.nativeOverlayTickets[page] == ticket else { return }
                    guard (200..<300).contains(response.status),
                          let raw = try JSONSerialization.jsonObject(with: response.data) as? [String: Any] else {
                        throw ReaderNativeFigures.Failure(message: "插图获取失败")
                    }
                    let pending = try self.nativeFigures.accept(raw, page: page)
                    if pending { self.nativeFigurePending.insert(page) } else { self.nativeFigurePending.remove(page) }
                    self.displayNativeFigures(page: page, document: document)
                    if !pending || attempt == 8 { return }
                    try await Task.sleep(nanoseconds: 4_500_000_000)
                }
            } catch {
                if !(error is CancellationError), !Task.isCancelled, self.nativeOverlayGeneration == generation, self.nativeOverlayTickets[page] == ticket {
                    self.nativeOverlayPages.remove(page)
                    self.postClientLog("native-figures: " + error.localizedDescription)
                }
            }
        }
    }

    private func nativeFigureProjection() throws -> [String: Any] {
        guard let bookID = currentLocalBook?.id, nativeFigures.bookID == bookID,
              nativeReadingStoreBookID == bookID else { throw ReaderNativeBookStore.MutationError.unavailable }
        let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
        let ink = try ReaderNativeBookProjection(store: store).state("ink", bookID: bookID).payload as? [String: [[String: Any]]] ?? [:]
        return nativeFigures.projection(ink: ink)
    }

    private func publishNativeFigureProjection() {
        guard let payload = try? nativeFigureProjection() else { return }
        webView.callAsyncJavaScript(ReaderNativeFigureBridge.source, arguments: ["payload": payload], in: nil, in: .page,
            completionHandler: { _ in })
    }

    private func performNativeFigureCommand(_ command: [String: Any]) async -> [String: Any]? {
        guard command["action"] as? String == "nativeFigureAttach", currentLocalBook?.format == .pdf else { return nil }
        guard let document = nativePDFDocument, let value = command["value"] as? [String: Any],
              let page = value["page"] as? Int, let id = value["id"] as? String, let desired = value["attached"] as? Bool,
              (command["scope"] == nil || command["scope"] as? String == nativeConversation.scope),
              !isLoading, isTrustedReaderURL(webView.url), currentLocalBook?.id == nativeFigures.bookID else {
            return ["ok": false, "error": "阅读页已切换"]
        }
        do {
            let attached = try nativeFigures.setAttached(desired, id: id, page: page)
            // Install the exact committed selection before reporting success.
            // No hidden thumbnails or a second attachment writer are created.
            let projected = try await webView.callAsyncJavaScript(ReaderNativeFigureBridge.source,
                arguments: ["payload": try nativeFigureProjection()], in: nil, contentWorld: .page)
            guard nativePDFDocument === document, projected as? Bool == true else {
                throw ReaderNativeFigures.Failure(message: "图已选中，但助手上下文未就绪，请刷新后重试")
            }
            displayNativeFigures(page: page, document: document)
            return ["ok": true, "value": ["attached": attached]]
        } catch { return ["ok": false, "error": error.localizedDescription] }
    }

    private func applyNativeVocabularyOverlay(_ payload: [String: Any], page: Int,
                                               document: ReaderNativePDFDocument) {
        guard let size = document.characterPageSize(page) else { return }
        let rows = payload["vocabMarks"] as? [[String: Any]] ?? []
        let marks: [ReaderNativePDFDocument.VocabMark] = rows.compactMap { row in
            guard let slug = row["label_slug"] as? String,
                  let rects = row["rects"] as? [[Double]] else { return nil }
            // 点坐标 → 归一化，viewRect 才能换算。与高亮同一口径。
            let boxes = rects.compactMap { r -> CGRect? in
                guard r.count == 4, r.allSatisfy(\.isFinite), r[2] > r[0], r[3] > r[1], size.width > 0, size.height > 0 else { return nil }
                return CGRect(x: r[0] / size.width, y: r[1] / size.height,
                              width: (r[2] - r[0]) / size.width,
                              height: (r[3] - r[1]) / size.height)
            }
            guard !boxes.isEmpty else { return nil }
            return .init(slug: slug, rects: boxes)
        }
        document.setVocabMarks(marks, page: page)
        // masteredFuri 为 null 表示振假名整体关着 —— 那时一个都不画，
        // 跟"这一页没有已掌握的词"不是一回事。
        let mastered = payload["masteredFuri"] as? [String]
        document.setFuriganaMastered(mastered, enabled: mastered != nil, page: page)
        let sentences = (payload["vocabSentences"] as? [[String: Any]] ?? [])
            .enumerated().compactMap { index, row -> ReaderNativePDFDocument.VocabSentence? in
                guard let text = row["text"] as? String, !text.isEmpty,
                      let rects = row["rects"] as? [[Double]] else { return nil }
                let boxes = rects.compactMap { r -> CGRect? in
                    guard r.count == 4, r.allSatisfy(\.isFinite), r[2] > r[0], r[3] > r[1], size.width > 0, size.height > 0 else { return nil }
                    return CGRect(x: r[0] / size.width, y: r[1] / size.height,
                                  width: (r[2] - r[0]) / size.width,
                                  height: (r[3] - r[1]) / size.height)
                }
                guard !boxes.isEmpty else { return nil }
                // id 要带页码：不同页的第 0 句不能撞成同一个。
                return .init(id: "\(page):\(index)", index: index, page: page,
                             text: text, rects: boxes)
            }
        document.setVocabSentences(sentences, page: page)
        // 搜索跳转后要亮的那个词。网页那侧取走即清，所以只会亮一次。
        if let query = payload["searchQuery"] as? String, !query.isEmpty {
            document.highlightSearchHits(query: query, page: page)
        }
    }

    private func loadNativeVocabularyOverlay(page: Int, document: ReaderNativePDFDocument, generation: UInt64, ticket: UUID) async throws -> (source: [String: Any], flags: [String: Any]) {
        guard let book = currentLocalBook, nativeReadingStoreBookID == book.id,
              let deviceID = nativeReadingStoreDeviceID else { throw NativeBookOCRError.pageUnavailable }
        let source = try await document.sourceCharacters(page: page)
        try Task.checkCancellation()
        guard nativePDFDocument === document, nativeOverlayGeneration == generation,
              nativeOverlayTickets[page] == ticket,
              let chars = source["chars"] as? [[String: Any]], let revision = source["revision"] as? String,
              document.characterPageSize(page) != nil else { throw NativeBookOCRError.pageUnavailable }
        let global = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-global").store
        let local = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
        guard try global.meta("legacyImport") == "done", try local.meta("legacyImport") == "done" else {
            throw ReaderNativeVocabularyState.Failure(message: "本地状态尚未就绪")
        }
        // Persistent flags come directly from their canonical native records.
        // Only transient translation/search/optimistic vocabulary state still
        // comes from the compatibility command owner.
        let catalog = try ReaderNativePreferences.Catalog.packaged.get()
        let preferences = ReaderNativePreferences(store: global, deviceID: deviceID)
        let device = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-device").store
        guard try device.meta("legacyImport") == "done" else { throw ReaderBookUserStateWebAdapterError.unavailable }
        let devicePreferences = ReaderNativePreferences(store: device, deviceID: deviceID)
        let vocabularyRaw = try preferences.raw(catalog.entry("pdf-vocab-underline"))
        let vocabulary = vocabularyRaw == nil || vocabularyRaw == "1"
        let ruby = try devicePreferences.raw(catalog.entry("pdf-ruby")) == "1"
        let settingsGeneration = global.generation(collection: "user-settings")
        let deviceSettingsGeneration = device.generation(collection: "device-preferences")
        let raw = try await webView.callAsyncJavaScript("""
            return {translation:window.__bwReaderPageTranslateOn?.()===true,
                    overrides:Object.fromEntries(window.__vocabOverride||[]),
                    mastered:Array.from(window.__masteredLocal||[]),
                    searchQuery:typeof _takePendingSearchQuery==='function'?_takePendingSearchQuery(page):''};
            """, arguments: ["page": page], in: nil, contentWorld: .page)
        guard nativePDFDocument === document, nativeOverlayGeneration == generation, nativeOverlayTickets[page] == ticket, !Task.isCancelled,
              var flags = raw as? [String: Any] else { throw ReaderBookUserStateWebAdapterError.contextChanged }
        guard global.generation(collection: "user-settings") == settingsGeneration,
              device.generation(collection: "device-preferences") == deviceSettingsGeneration else {
            refreshNativePageOverlays(force: true)
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        flags["vocabulary"] = vocabulary
        flags["ruby"] = ruby
        let vocabularyGeneration = global.generation(collection: ReaderNativeVocabularyState.collection)
        let index = try nativeVocabularyOverlayStore.vocabulary(global)
        let calculation = Task.detached(priority: .userInitiated) { ReaderNativeVocabularyOverlay.localMarks(chars, state: index) }
        let marks = await withTaskCancellationHandler(operation: { await calculation.value }, onCancel: { calculation.cancel() })
        try Task.checkCancellation()
        guard nativePDFDocument === document, nativeOverlayGeneration == generation, nativeOverlayTickets[page] == ticket else {
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        guard global.generation(collection: ReaderNativeVocabularyState.collection) == vocabularyGeneration else {
            refreshNativePageOverlays(force: true)
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        let cached = try? ReaderNativePageOverlayStore.cached(local, bookID: book.id, page: page, revision: revision)
        func payload(_ enrichment: [String: Any]?, search: Bool) -> [String: Any] {
            let combined = ReaderNativeVocabularyOverlay.merge(marks, enrichment?["vocab_marks"] as? [[String: Any]] ?? [])
            let filtered = ReaderNativeVocabularyOverlay.visible(combined, state: index,
                overrides: flags["overrides"] as? [String: Bool] ?? [:], legacyMastered: Set(flags["mastered"] as? [String] ?? []))
            return ["vocabMarks": flags["vocabulary"] as? Bool != false ? filtered : [],
                "masteredFuri": flags["ruby"] as? Bool == true ? Array((enrichment?["mastered_furi"] as? [String] ?? []).prefix(4000)) as Any : NSNull(),
                "vocabSentences": Array((enrichment?["vocab_sentences"] as? [[String: Any]] ?? []).prefix(64)),
                "searchQuery": search ? flags["searchQuery"] as? String ?? "" : ""]
        }
        applyNativeVocabularyOverlay(payload(cached, search: true), page: page, document: document)
        guard let gateway = nativeServerGateway else { return (source, flags) }
        var url = URLComponents(); url.path = "/pdf/api/page-overlay"
        url.queryItems = [URLQueryItem(name: "file", value: "localbook:" + book.id), URLQueryItem(name: "page", value: String(page))]
        guard let path = url.string else { return (source, flags) }
        nativeVocabularyEnrichmentTasks[page]?.cancel()
        nativeVocabularyEnrichmentTasks[page] = Task { @MainActor [weak self, weak document] in
            guard let self, let document else { return }
            defer { if self.nativeOverlayTickets[page] == ticket { self.nativeVocabularyEnrichmentTasks.removeValue(forKey: page) } }
            do {
                let response = try await gateway.fetchData(path: path, surface: .pdf)
                try Task.checkCancellation()
                guard self.nativePDFDocument === document, self.nativeOverlayGeneration == generation, self.nativeOverlayTickets[page] == ticket,
                      (200..<300).contains(response.status),
                      let raw = try JSONSerialization.jsonObject(with: response.data) as? [String: Any],
                      let entry = ReaderNativePageOverlayStore.normalized(raw, page: page, revision: revision,
                          savedAt: Date().timeIntervalSince1970 * 1000) else { return }
                // Text/OCR replacement can keep the same document object. Check
                // the content revision again before displaying or caching boxes.
                let current = try await document.sourceCharacters(page: page)
                guard self.nativePDFDocument === document, self.nativeOverlayGeneration == generation, self.nativeOverlayTickets[page] == ticket,
                      current["revision"] as? String == revision, !Task.isCancelled else { return }
                if global.generation(collection: ReaderNativeVocabularyState.collection) == vocabularyGeneration {
                    self.applyNativeVocabularyOverlay(payload(entry, search: false), page: page, document: document)
                } else { self.refreshNativePageOverlays(force: true) }
                try ReaderNativePageOverlayStore.save(entry, store: local, bookID: book.id, deviceID: deviceID)
            } catch {
                // Network-only enrichment is optional: leave the local and
                // persisted cached projection visible, without a web retry.
                if !(error is CancellationError), !Task.isCancelled {
                    self.postClientLog("native-overlay-enrichment: " + error.localizedDescription)
                }
            }
        }
        return (source, flags)
    }

    private func startNativePageTranslation(source: [String: Any], enabled: Bool, page: Int,
                                            document: ReaderNativePDFDocument, generation: UInt64, ticket: UUID) {
        guard nativePDFDocument === document, nativeOverlayGeneration == generation else { return }
        nativePageTranslationTasks[page]?.cancel()
        nativePageTranslationTasks.removeValue(forKey: page)
        guard enabled else { document.setTranslationSlices([], page: page); return }
        guard let chars = source["chars"] as? [[String: Any]], let revision = source["revision"] as? String,
              let bookID = currentLocalBook?.id, let gateway = nativeServerGateway else { return }
        let cacheKey = bookID + ":" + String(page) + ":" + revision
        func display(_ rows: [[String: Any]]) {
            guard let size = document.characterPageSize(page), size.width > 0, size.height > 0 else { return }
            let slices = rows.compactMap { row -> ReaderNativePDFDocument.TranslationSlice? in
                guard let text = row["text"] as? String, !text.isEmpty,
                      let x = row["x"] as? Double, let y = row["y"] as? Double,
                      let width = row["w"] as? Double, let sizePt = row["fontSize"] as? Double,
                      [x, y, width, sizePt].allSatisfy(\.isFinite) else { return nil }
                return .init(origin: CGPoint(x: x / size.width, y: y / size.height), width: width / size.width,
                             fontScale: sizePt / size.height, text: text)
            }
            document.setTranslationSlices(slices, page: page)
        }
        if let cached = nativePageTranslationCache[cacheKey] { display(cached); return }
        let sentences = ReaderNativePageTranslation.sentences(chars)
        guard !sentences.isEmpty else { display([]); return }
        nativePageTranslationTasks[page] = Task { @MainActor [weak self, weak document] in
            guard let self, let document else { return }
            defer { if self.nativeOverlayTickets[page] == ticket { self.nativePageTranslationTasks.removeValue(forKey: page) } }
            do {
                let response = try await gateway.fetchData(path: "/pdf/api/epub-translate-section", method: "POST",
                    body: JSONSerialization.data(withJSONObject: ["texts": sentences.map { $0["text"] as? String ?? "" }]), surface: .pdf)
                try Task.checkCancellation()
                let translated = try ReaderNativePageTranslation.translated(sentences, status: response.status, data: response.data)
                let slices = Array(ReaderNativePageTranslation.slices(translated).prefix(400))
                let current = try await document.sourceCharacters(page: page)
                guard !Task.isCancelled, self.nativePDFDocument === document, self.nativeOverlayGeneration == generation,
                      self.nativeOverlayTickets[page] == ticket,
                      current["revision"] as? String == revision else { return }
                display(slices)
                let bytes = try JSONSerialization.data(withJSONObject: slices).count
                if bytes <= 1024 * 1024 {
                    if self.nativePageTranslationCache.count >= 24 || self.nativePageTranslationCacheBytes + bytes > 2 * 1024 * 1024 {
                        self.nativePageTranslationCache.removeAll(); self.nativePageTranslationCacheBytes = 0
                    }
                    self.nativePageTranslationCache[cacheKey] = slices; self.nativePageTranslationCacheBytes += bytes
                }
            } catch {
                if !(error is CancellationError), !Task.isCancelled, self.nativePDFDocument === document, self.nativeOverlayGeneration == generation, self.nativeOverlayTickets[page] == ticket {
                    self.nativeOverlayPages.remove(page)
                    self.postClientLog("native-page-translation: " + error.localizedDescription)
                }
            }
        }
    }

    /// 把可见页的屏幕矩形推给墨迹层。
    ///
    /// ⚠ 原生接管后网页不再渲页：`__inkCanvas` 不存在、`getBoundingClientRect`
    /// 量不到任何东西 —— 墨迹表面会一个都没有，**Pencil 在原生正文上直接画不了**。
    /// 页面位置此时只有 PDFKit 知道，所以由这边算好塞过去。
    /// id 仍用 `page:N`，落库那一路（resolveSurface → byPage）完全不用改。
    /// 合并成一次：滚动时布局回调每帧都来，逐帧过一次 WebKit 没有意义。
    /// 与导航桥同一口径（180ms）。
    /// 布局一变（滚动/缩放）就排一次；**滚动停下来**才真正做 —— 每来一次就把上一次的取消重排。
    ///
    /// ⚠ 以前是节流（滚动中每 180ms 做一次）：可见页的笔迹面、可见正文、生词下划线/振假名
    ///   都在滚动途中一遍遍过 WebKit，下划线数据一到每页装饰整页重画（连振假名一起）——
    ///   2026-09-23 用户报"卡顿还是没有解决"。停下来再做，滚动途中这些一件都不做。
    private func scheduleNativeInkSurfacePublish() {
        nativeInkSurfaceTask?.cancel()
        nativeInkSurfaceTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .milliseconds(250))
            guard let self, !Task.isCancelled else { return }
            self.nativeInkSurfaceTask = nil
            self.publishNativeInkSurfaces()
            self.publishNativeHTMLNotes()
            self.publishNativeVisibleText()
            // 翻页后可见页变了，生词下划线也要跟着取 —— 已经取过的页不重取。
            self.refreshNativePageOverlays()
        }
    }

    /// 把「用户此刻看得见的正文」推给助手。
    ///
    /// ⚠ 助手的 `_visibleText()` 是从 `.page-wrap` 的 `__charBoxes` 拼的 —— 接管后
    /// 一页都没有，于是它拿到**空字符串**：后端系统提示里「紧扣可见段落」那条就此
    /// 失效，回答变泛而没有任何人看得出原因。这是一处纯粹的静默降级。
    /// 截断（1000 字 + 省略号）仍留在网页那一处，这里只供原文。
    func publishNativeVisibleText() {
        guard let document = nativePDFDocument else { return }
        var parts: [String] = []
        for page in document.position.visiblePages.prefix(4) {
            if let text = document.pageText(page), !text.isEmpty { parts.append(text) }
        }
        let joined = String(parts.joined(separator: "\n").prefix(4000))
        Task { @MainActor [weak self] in
            guard let self else { return }
            _ = try? await self.webView.callAsyncJavaScript(
                "window.__bwNativeVisibleText = text; return true;",
                arguments: ["text": joined], in: nil, contentWorld: .page)
        }
    }

    func publishNativeInkSurfaces() {
        guard let document = nativePDFDocument,
              webView.bounds.width > 0, webView.bounds.height > 0 else { return }
        var surfaces: [[String: Any]] = []
        for page in document.position.visiblePages.prefix(8) {
            guard let pageRect = document.viewRect(
                normalized: CGRect(x: 0, y: 0, width: 1, height: 1), page: page) else { continue }
            let local = webView.convert(document.view.convert(pageRect, to: nil), from: nil)
            guard local.width > 0, local.height > 0 else { continue }
            surfaces.append([
                "id": "page:\(page)",
                "rect": ["x": local.minX / webView.bounds.width,
                         "y": local.minY / webView.bounds.height,
                         "width": local.width / webView.bounds.width,
                         "height": local.height / webView.bounds.height],
            ])
        }
        if nativeReadingStoreBookID == currentLocalBook?.id, nativeReadingStoreBookID != nil {
            nativePencilInk.updateLayout(from: ["type":"layout", "documentToken":nativeInkDocumentToken, "surfaces":surfaces])
            return
        }
        guard let data = try? JSONSerialization.data(withJSONObject: surfaces),
              let json = String(data: data, encoding: .utf8) else { return }
        Task { @MainActor [weak self] in
            _ = try? await self?.webView.callAsyncJavaScript(
                "window.__bwNativeInkSurfaces = JSON.parse(value);"
                + "window.__bwNativeInkSurfacesChanged?.();",
                arguments: ["value": json], in: nil, contentWorld: .page)
        }
    }

    /// Page strokes never need a webpage element. EPUB keeps its own adapter
    /// until its document model has migrated; a failed native PDF write must
    /// not silently fall back to a second writer.
    func performNativePDFInk(_ action: String, payload: [String: Any]) async throws -> Bool {
        guard let document = nativePDFDocument else { return false }
        guard let book = currentLocalBook, let access = currentLocalBookAccess,
              nativeReadingStoreBookID == book.id, let deviceID = nativeReadingStoreDeviceID,
              let digest = currentLocalBookContentSHA256,
              document.matches(bookID:book.id,contentSHA256:digest),
              payload["documentToken"] as? String == nativeInkDocumentToken,
              let opID = payload["opId"] as? String, nativePDFMutationCommandDepth == 0 else {
            throw ReaderNativeBookStore.MutationError.unavailable
        }
        let generation = bookUserStateContextGeneration
        let pending = try await nativePDFMutationActor.hasUnfinishedMutation(book:access)
        guard !pending, nativePDFMutationCommandDepth == 0,
              generation == bookUserStateContextGeneration, currentLocalBookAccess === access,
              document.matches(bookID:book.id,contentSHA256:digest) else {
            throw ReaderNativeBookStore.MutationError.unavailable
        }
        // No await between the final identity/barrier check and commit.
        let store = try nativeDataStoreHost.bridge(for:"bw-reader-native-v1-document").store
        let business = ReaderNativeBookStore(store:store,bookID:book.id,deviceID:deviceID,
            displayName:book.title,contentSHA256:digest)
        var input = payload; input["action"] = action
        let receipt = try business.perform(["bookID":book.id,"mutationId":opID,"operation":"ink-operation","value":input])
        // The durable receipt, not a DOM redraw, releases the Pencil queue.
        let result = receipt["result"] as? [String:Any] ?? [:]
        let changes = result["changes"] as? [[String:Any]] ?? []
        if !changes.isEmpty {
            let domains = try ReaderNativeBookProjection(store:store).exportReadingDomains(bookID:book.id)
            try document.applyOverlays(domains,bookID:book.id,contentSHA256:digest)
            if nativeFigures.hasAttachments { publishNativeFigureProjection() }
            markCloudSyncDirty()
        }
        resumeNativeInkSync(business)
        // Transitional consumers only observe this receipt; they do not save
        // it again. Native side remains the owner of strokes and the outbox.
        let event: [String:Any] = ["source":"native-pencil","opId":opID,"changes":changes,
            "surfaceIds":changes.compactMap { ($0["page"] as? NSNumber).map { "page:" + $0.stringValue } }]
        webView.callAsyncJavaScript("window.dispatchEvent(new CustomEvent('rc:inkchange',{detail:value})); return true;",
            arguments:["value":event],in:nil,in:.page,completionHandler:nil)
        return true
    }

    func performNativeInkHistory(_ action: String) {
        guard ["undo","redo","clear"].contains(action), !nativeInkHistoryBusy,
              !nativePencilInk.hasPendingOperations, let document = nativePDFDocument else { return }
        nativeInkHistoryBusy = true
        let payload: [String:Any] = ["opId":"ink-" + UUID().uuidString,
            "documentToken":nativeInkDocumentToken,"page":document.position.page]
        Task { @MainActor [weak self] in
            guard let self else { return }
            defer { self.nativeInkHistoryBusy = false }
            do { _ = try await self.performNativePDFInk(action,payload:payload) }
            catch { self.nativePencilInk.report(error) }
        }
    }

    private func resumeNativeInkSync(_ business: ReaderNativeBookStore) {
        guard nativeInkSyncTasks[business.bookID] == nil else { return }
        nativeInkSyncTasks[business.bookID] = Task { @MainActor [weak self] in
            defer { self?.nativeInkSyncTasks.removeValue(forKey:business.bookID) }
            var backoff: UInt64 = 2
            while !Task.isCancelled {
                do {
                    guard let due = try business.nextInkSyncTime() else { break }
                    let delay = max(0,Double(due) / 1000 - Date().timeIntervalSince1970)
                    if delay > 0 { try await Task.sleep(nanoseconds:UInt64(min(delay,60) * 1_000_000_000)); continue }
                    _ = try business.perform(["bookID":business.bookID,"mutationId":"ink-sync-" + UUID().uuidString,
                        "operation":"ink-sync","value":[:]])
                    self?.nativeReplicationService?.wake()
                    backoff = 2
                } catch is CancellationError { break }
                catch {
                    self?.postClientLog("原生笔迹同步仍保留待处理记录：" + error.localizedDescription)
                    do { try await Task.sleep(nanoseconds:backoff * 1_000_000_000) } catch { break }
                    backoff = min(backoff * 2,60)
                }
            }
        }
    }

    private func publishNativeHTMLNotes(force: Bool = false) {
        guard let document = nativePDFDocument, let bookID = nativeReadingStoreBookID,
              bookID == currentLocalBook?.id else { return }
        let key = bookID + ":" + String(bookUserStateContextGeneration) + ":" + String(document.notesContentRevision)
            + ":" + String(document.position.page) + ":" + nativeHTMLPinned.sorted().joined(separator:"|")
        guard force || key != nativeHTMLNoteKey else { return }
        do {
            let snapshot = try ReaderNativeHTMLNotes.project(document.notes,bookID:bookID,page:document.position.page,pinned:nativeHTMLPinned)
            nativeHTMLNoteActions = snapshot.actions; nativeHTMLNoteKey = key
            nativeConversation.setNativeHTMLNotes(snapshot.placements)
            if !nativeHTMLNotesEnabled {
                nativeHTMLNotesEnabled = true
                webView.evaluateJavaScript("window.__BW_NATIVE_HTML_NOTES__ = true; window.__bwNativeConversation?.snapshot?.();", completionHandler:nil)
            }
        } catch { nativeConversation.report("注解卡读取失败：" + error.localizedDescription) }
    }

    private func performNativeHTMLNoteCommand(_ command: [String:Any]) async -> [String:Any]? {
        guard let token = command["actionId"] as? String, token.hasPrefix("native-note-") else { return nil }
        do {
            guard !isLoading, let action = nativeHTMLNoteActions[token],
                  command["scope"] as? String == nativeConversation.scope,
                  let book = currentLocalBook, let access = currentLocalBookAccess,
                  nativeReadingStoreBookID == book.id, let deviceID = nativeReadingStoreDeviceID,
                  let digest = currentLocalBookContentSHA256 else { throw ReaderNativeBookStore.MutationError.unavailable }
            let store = try nativeDataStoreHost.bridge(for:"bw-reader-native-v1-document").store
            let notes = try ReaderNativeBookProjection(store:store).state("document-notes-legacy",bookID:book.id).payload as? [[String:Any]] ?? []
            guard let note = notes.first(where: { $0["id"] as? String == action.noteID }),
                  let html = note["html"] as? [String:Any] ?? note["video"] as? [String:Any] else {
                throw ReaderNativeNoteRules.NoteError.missing
            }
            let outer = command["action"] as? String
            if outer == "inspectArtifact" { return ["ok":true,"detail":["kind":"general","title":html["label"] ?? "卡片","content":html]] }
            if outer == "mediaResource", let resource = action.resource { return ["ok":true,"resource":resource] }
            guard outer == "liveAction" else { throw ReaderNativeNoteRules.NoteError.invalid("卡片命令") }
            if action.key == "favorite" {
                let service = try prepareNativeFavoritesService()
                let result = try await service.perform("save", ["card": [
                    "label": html["label"] ?? "工具卡片", "raw": html["content"] ?? "",
                    "text": html["contextText"] ?? html["content"] ?? "", "isHtml": html["isHtml"] as? Bool ?? false,
                    "kind": "tool", "cid": html["cid"] ?? action.noteID,
                    "meta": ["file": book.title, "page": ReaderNativeCardRules.string((note["anchor"] as? [String: Any])?["page"]), "q": ""]]])
                return ["ok": true, "value": result]
            }
            if ["select","pin"].contains(action.key) {
                // These shared registries are still being migrated. Their
                // adapter accepts authoritative data and never reads a card
                // element or forces a hidden page to mount.
                let result = try await webView.callAsyncJavaScript("""
                    const html = note.html, cid = String(html.cid || note.id);
                    const registry = window.BWReaderRuntime?.contextSelections;
                    if (action === 'select') {
                      if (typeof text !== 'string' || text.length > 16000) throw new Error('选区内容无效');
                      if (!text.trim()) { if (window.__bwNativeSelection?.owner === token) window.__bwNativeSelection.active = false; return {ok:true}; }
                      window.__bwNativeSelection = {text:text.trim(),active:true,owner:token,scope};
                      window.__setFocusSel(text.trim(),'text');
                      if (window.__focusSel?.text !== text.trim()) throw new Error('选区暂未进入对话');
                      return {ok:true};
                    }
                    if (!registry?.toggle) throw new Error('对话上下文尚未就绪');
                    const plain = String(html.contextText || html.content || '').replace(/<[^>]+>/g,' ').replace(/\\s+/g,' ').trim();
                    registry.toggle({id:'card:'+cid,kind:'card',label:String(html.label || '工具卡片').slice(0,200),text:plain.slice(0,16000),
                      source:{cid,tool:String(html.kind || '')},meta:{contract:'tool-card-context/1',host:'page-placement',card:html}});
                    return {ok:true,selected:!!registry.isSelected('card:'+cid)};
                    """, arguments:["note":note,"action":action.key,"text":command["text"] as? String ?? "", "token":token,"scope":nativeConversation.scope],
                    in:nil,contentWorld:.page)
                guard currentLocalBookAccess === access else { throw ReaderNativeBookStore.MutationError.unavailable }
                let receipt = result as? [String:Any] ?? ["ok":false]
                if action.key == "pin" {
                    let cid = html["cid"] as? String ?? action.noteID
                    if receipt["selected"] as? Bool == true { nativeHTMLPinned.insert(cid) } else { nativeHTMLPinned.remove(cid) }
                    publishNativeHTMLNotes(force:true)
                }
                return receipt
            }
            let value = command["value"] as? [String:Any] ?? [:]
            var input: [String:Any] = ["id":action.noteID,"action":"update"]
            switch action.key {
            case "move":
                var changes: [String:Any] = ["anchor":value.filter { ["page","x","y"].contains($0.key) }]
                if ReaderNativeNoteActions.binding(note) != nil { changes["bind"] = value["bind"] ?? NSNull() }
                input["changes"] = changes
            case "anchor": input["changes"] = ["bind":value["bind"] ?? NSNull()]
            case "form": input["changes"] = ["form":command["value"] ?? ""]
            case "collapse": input["changes"] = ["form":"dot"]
            case "expand": input["changes"] = ["form":"full"]
            case "resize": input["changes"] = value
            case "remove", "trash": input["action"] = "remove"
            case "ink": input = value; input["action"] = "ink"; input["id"] = action.noteID
            case "video": input["action"] = "video"; input["changes"] = value
            case "inspect": return ["ok":true]
            default: throw ReaderNativeNoteRules.NoteError.invalid("卡片动作")
            }
            let generation = bookUserStateContextGeneration
            guard nativePDFMutationCommandDepth == 0 else { throw ReaderNativeBookStore.MutationError.unavailable }
            let pending = try await nativePDFMutationActor.hasUnfinishedMutation(book:access)
            guard !pending, nativePDFMutationCommandDepth == 0, generation == bookUserStateContextGeneration,
                  currentLocalBookAccess === access, currentLocalBookContentSHA256 == digest else { throw ReaderNativeBookStore.MutationError.unavailable }
            let opID = input["opId"] as? String ?? "note-" + UUID().uuidString
            input["opId"] = opID
            let receipt = try ReaderNativeBookStore(store:store,bookID:book.id,deviceID:deviceID,displayName:book.title,contentSHA256:digest)
                .perform(["bookID":book.id,"mutationId":opID,"operation":"note-operation","value":input])
            nativeReplicationService?.wake()
            markCloudSyncDirty()
            await refreshNativePDFProjection()
            if currentLocalBookAccess === access {
                // Wake old transport/context consumers until those owners
                // have migrated. No duplicate write and no rendered element.
                webView.callAsyncJavaScript("window.dispatchEvent(new CustomEvent('bw:native-book-committed',{detail:value})); return true;",
                    arguments:["value":["bookID":book.id,"bindingChanges":receipt["bindingChanges"] ?? []]],in:nil,in:.page,completionHandler:nil)
            }
            var result = receipt["result"] as? [String:Any] ?? [:]
            result["ok"] = true; result["persisted"] = true
            return ["ok":true,"value":result]
        } catch { return ["ok":false,"error":error.localizedDescription] }
    }

    /// 原生正文接管时拖动页卡：把落点换成 **PDF 页内归一化坐标**再写锚点。
    ///
    /// ⚠ 不能沿用 `placeNativeConversationCard`：那条路把落点换成**网页视口**的
    /// 归一化坐标，交给网页的锚点解析器。原生接管后网页视口里根本没有那一页，
    /// 那个坐标不指向任何东西 —— 卡会飞到别处。
    /// 拖卡时告诉用户"松手会锁在哪"。
    ///
    /// ⚠ 判据必须与 `nativeDropTarget` **同源**，否则预览与落点会各说各话：
    ///   有原生文档就走 PDFKit + Swift 字符几何；
    ///   没有（EPUB / 网页渲染的 PDF）才问网页那份 —— 那种情形下正文确实
    ///   由网页渲染，视口坐标是对的。
    func previewCardDrop(windowPoint: CGPoint) {
        // 字符命中与精确匹配已搬到 Swift；预览仍限流，避免每个触点都扫描
        // 多栏/表格的字符。PDFKit 卡片移动本身不受这个提示更新频率限制。
        let dx = windowPoint.x - dropPreviewPoint.x, dy = windowPoint.y - dropPreviewPoint.y
        guard dx * dx + dy * dy > 36, Date().timeIntervalSince(dropPreviewStamp) > 0.09 else { return }
        dropPreviewPoint = windowPoint
        dropPreviewStamp = Date()
        if let document = nativePDFDocument {
            let local = document.view.convert(windowPoint, from: nil)
            cardDropPreviews.preview = document.dropPreview(local).map {
                ReaderNativeDropPreview(rects: $0.rects.map { document.view.convert($0, to: nil) },
                                        line: $0.line.map { document.view.convert($0, to: nil) })
            }
            return
        }
        previewCardDropViaWeb(windowPoint)
    }

    func clearCardDropPreview() {
        cardDropPreviews.preview = nil
        dropPreviewPoint = CGPoint(x: -10_000, y: -10_000)
        dropPreviewStamp = .distantPast
    }

    private func previewCardDropViaWeb(_ windowPoint: CGPoint) {
        // 过网页那一跳是异步的：同一时刻只允许一个在飞，否则会堆成一串排队的请求。
        // （时间/距离的限流在上面 previewCardDrop 里统一做了，这里不再限一次。）
        guard !dropPreviewBusy else { return }
        let size = webView.bounds.size
        guard size.width > 0, size.height > 0 else { return }
        let local = webView.convert(windowPoint, from: nil)
        let x = local.x / size.width, y = local.y / size.height
        guard (0...1).contains(x), (0...1).contains(y) else { cardDropPreviews.preview = nil; return }
        dropPreviewBusy = true
        Task { [weak self] in
            guard let self else { return }
            let receipt = await requestNativeConversationCommand(
                ["action": "anchorPreview", "x": Double(x), "y": Double(y)])
            dropPreviewBusy = false
            guard receipt["ok"] as? Bool == true else { return }
            guard let value = receipt["value"] as? [String: Any] else { cardDropPreviews.preview = nil; return }
            func window(_ rect: CGRect) -> CGRect {
                webView.convert(CGRect(x: rect.minX * size.width, y: rect.minY * size.height,
                                       width: rect.width * size.width, height: rect.height * size.height), to: nil)
            }
            let rects = (value["rects"] as? [[String: NSNumber]] ?? []).compactMap { box -> CGRect? in
                guard let x = box["x"]?.doubleValue, let y = box["y"]?.doubleValue,
                      let w = box["width"]?.doubleValue, let h = box["height"]?.doubleValue,
                      [x, y, w, h].allSatisfy({ $0.isFinite }), w > 0, h > 0 else { return nil }
                return window(CGRect(x: x, y: y, width: w, height: h))
            }
            if !rects.isEmpty { cardDropPreviews.preview = ReaderNativeDropPreview(rects: rects, line: nil); return }
            guard let lineY = (value["y"] as? NSNumber)?.doubleValue, lineY.isFinite else {
                cardDropPreviews.preview = nil; return
            }
            cardDropPreviews.preview = ReaderNativeDropPreview(
                rects: [], line: window(CGRect(x: 0, y: lineY, width: 1, height: 0.0015)))
        }
    }

    /// 文档层里一张卡怎么摆：左上角（文档坐标）、卡片自身尺寸（卡片点 = 便签的 w/h）、
    /// 以及卡片点 → 文档坐标的比例。
    ///
    /// 卡片按**屏幕 1:1** 画（比例 = 1/缩放）：字、按钮、间距都是正常的原生尺寸，
    /// 不随页面缩小；只有位置钉在页上。
    /// ⚠ 2026-09-23 用户：「字体大小有很大问题」「整个卡片所有元素都小过头了」——
    ///   此前卡片跟着文档层一起缩放，页面缩到 0.34 时卡里 12pt 的字只剩 4pt。
    ///   原版展开的词锚卡也是这样：portal 到 body，逃出页面缩放（rc-stickynote wordPortalIn）。
    func nativeCardLayout(_ item: ReaderNativePagePlacement, zoom: CGFloat) -> (origin: CGPoint, size: CGSize, scale: CGFloat)? {
        guard let document = nativePDFDocument,
              let note = document.notes.first(where: { $0["id"] as? String == item.noteID }) else { return nil }
        let scale = 1 / max(zoom, 0.01)
        let w = (note["w"] as? NSNumber)?.doubleValue ?? 300
        let h = (note["h"] as? NSNumber)?.doubleValue ?? 180
        let size = CGSize(width: min(720, max(180, w.isFinite ? w : 300)),
                          height: min(720, max(100, h.isFinite ? h : 180)))
        let docSize = CGSize(width: size.width * scale, height: size.height * scale)
        if item.bound, isBoundCardOpen(item),
           let rect = nativeWordCardDocumentRect(id: item.noteID, size: docSize) {
            return (rect.origin, size, scale)
        }
        guard let rect = nativePageCardDocumentRect(id: item.noteID, size: nil) else { return nil }
        return (rect.origin, size, scale)
    }

    func placeNativeConversationCard(actionID: String, scope: String, windowPoint: CGPoint, nativeTarget: [String: Any]? = nil) async {
        // ⚠ 以前这里条件不满足就一声不吭地 return —— 侧栏卡拖过去、什么都没发生
        //   （2026-09-23 用户："侧边栏中的卡片无法和以前一样拖动到页面上"）。每一步都说出来。
        postClientLog("[card-drop] place x=\(Int(windowPoint.x)) y=\(Int(windowPoint.y)) scopeOK=\(scope == nativeConversation.scope) native=\(nativePDFDocument != nil)")
        guard scope == nativeConversation.scope else {
            showTransientNotice("对话已切换，请从当前侧栏重新拖一次。")
            return
        }
        guard webView.window != nil, webView.bounds.width > 0, webView.bounds.height > 0 else {
            showTransientNotice("阅读页还没准备好，请稍后再放。")
            return
        }
        // Convert the native drop into the same WKWebView viewport used by the
        // existing anchor resolver; safe-area/Pencil overlays add no offset.
        let point = webView.convert(windowPoint, from: nil)
        var parameters: [String: Any] = [
            "actionId": actionID,
            "x": point.x / webView.bounds.width,
            "y": point.y / webView.bounds.height
        ]
        // ⚠ 原生接管正文后上面那组视口坐标**解不出锚点** —— 网页视口里没有那一页。
        //   跟 nativeDropTarget 一样先用 PDFKit 定页，把页内坐标一并交过去；
        //   网页那侧拿到就跳过自己的解析。拿不到（EPUB / 网页渲染）就照旧。
        if let nativeTarget {
            parameters["value"] = nativeTarget
            parameters["x"] = 0; parameters["y"] = 0
        } else if let document = nativePDFDocument,
           let placed = document.canonicalPoint(document.view.convert(windowPoint, from: nil), from: document.view) {
            parameters["value"] = ["page": placed.page, "x": placed.point.x, "y": placed.point.y]
        } else if nativePDFDocument != nil {
            postClientLog("[card-drop] no page under drop point")
            showTransientNotice("请放到书页正文上。")
            return
        }
        let ok = await nativeConversation.perform("liveAction", parameters: parameters)
        postClientLog("[card-drop] result ok=\(ok) error=" + (nativeConversation.error ?? "-"))
        if ok { scheduleNativePDFProjectionRefresh() }
    }

    func resizeNativeConversationCard(actionID: String, scope: String, size: CGSize) async -> Bool {
        guard scope == nativeConversation.scope, webView.bounds.width > 0, webView.bounds.height > 0 else { return false }
        return await nativeConversation.perform("liveAction", parameters: ["actionId": actionID,
            "value": ["width": min(1, size.width / webView.bounds.width), "height": min(1, size.height / webView.bounds.height)]])
    }

    private func performNativeConversationCommand(_ command: [String: Any]) async -> String? {
        if command["action"] as? String == "send", let ids = command["attachmentIds"] as? [String], !ids.isEmpty {
            guard command["scope"] as? String == nativeConversation.scope else { return "对话已变化，请重新发送。" }
            let text = command["text"] as? String ?? "请查看附件。"
            if nativeConversation.conversationMode == "normal" {
                guard let bridge = nativeVoiceBridge,
                      await bridge.sendTypedToBackend(text, attachmentIDs: ids, submissionID: command["submissionId"] as? String) else {
                    // No fallback: an unknown acknowledgement may already have started work.
                    return "尚未确认后台收到附件。附件仍保留，可再次确认发送；请勿另建重复消息。"
                }
                return nil
            }
            // Review has a separate conversation. Preserve that destination and attach
            // server file references rather than redirecting it to the voice thread.
            var linked = command
            linked.removeValue(forKey: "attachmentIds"); linked.removeValue(forKey: "submissionId")
            linked.removeValue(forKey: "attachmentReferences")
            linked["text"] = text + "\n\n" + (command["attachmentReferences"] as? String ?? "")
            let receipt = await requestNativeConversationCommand(linked)
            return receipt["ok"] as? Bool == true ? nil : (receipt["error"] as? String ?? "附件消息未获确认。")
        }
        // 不在通话时，普通对话里打的字交给语音核心的后台线程（与通话中同一个归宿）。
        // 语音核心不在（ReaderPC 没开）才退回下面原来的文字助手。
        // 通话中的那条仍走网页 __vcSendText → 原生 sendTyped，不在这里拦。
        if command["action"] as? String == "send",
           nativeConversation.conversationMode == "normal",
           !nativeConversation.voice.active, !nativeConversation.voice.busy,
           let bridge = nativeVoiceBridge, !bridge.state.isActive, !bridge.state.isBusy,
           let text = (command["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
           !text.isEmpty,
           await bridge.sendTypedToBackend(text) {
            return nil
        }
        let receipt = await requestNativeConversationCommand(command)
        let ok = receipt["ok"] as? Bool == true
        // 顶栏「阅读工具」里的那些按钮点的是网页工具栏（译页/注音/生词下划线/图描述…），
        // 它们改的正是原生正文要画的东西。不在这儿重取一次，表现就是「点了译页没反应」
        // —— 网页那侧确实开了，只是原生没去拿新数据。
        if ok, command["action"] as? String == "liveAction" {
            refreshNativePageOverlays(force: true)
            // 卡片的开合 / 移动 / 形态 / 新建都会改便签 —— 立刻重取一次投影，
            // 否则原生的锁定框和卡位要等下一次别的写入才更新（翻页回来才变的那种）。
            scheduleNativePDFProjectionRefresh()
        }
        return ok ? nil : (receipt["error"] as? String ?? "操作未完成，请重试")
    }

    func updateNativePDFSelection(_ values: [ReaderNativePDFDocument.CharacterSelection],
                                  bookID: String, contentSHA256: String, scope: String) async -> Bool {
        guard currentLocalBook?.id == bookID, currentLocalBookContentSHA256?.lowercased() == contentSHA256.lowercased(),
              scope == nativeConversation.scope, values.allSatisfy({ $0.bookID == bookID && $0.contentSHA256.lowercased() == contentSHA256.lowercased() }) else { return false }
        nativePDFSelectionSequence += 1
        let pages: [[String: Any]] = values.map { value in
            ["page": value.page, "text": value.text, "sentence": value.sentence, "indexes": value.indexes,
             "geometryDigest": value.geometryDigest, "contentSHA256": value.contentSHA256]
        }
        let receipt = await requestNativeConversationCommand(["action": "nativePageSelection", "scope": scope,
            "value": ["sequence": nativePDFSelectionSequence, "pages": pages]])
        if receipt["ok"] as? Bool != true, !values.isEmpty {
            postClientLog("[native-sel] report failed: " + String(describing: receipt["error"] ?? "unknown"))
        }
        return receipt["ok"] as? Bool == true
    }

    // MARK: - 卡片收藏夹（原版 #vc-dock-btn / #vc-dock-panel）
    //
    // 网页层在原生外壳里被藏着，原版右下角那个收藏夹按钮与面板也就看不见了
    // （2026-09-23 用户："卡片收藏进收藏夹后也没有显示收藏夹的图标按钮"）。
    // Swift owns collection requests and the session cache. The web adapter
    // temporarily observes committed snapshots for shared context selection.

    func loadNativeFavorites() async -> [ReaderNativeFavorite] {
        let receipt = await requestNativeConversationCommand(["action": "favoritesList", "scope": nativeConversation.scope])
        guard receipt["ok"] as? Bool == true else {
            showTransientNotice(receipt["error"] as? String ?? "收藏夹没能打开。")
            return (nativeFavoritesService?.records ?? []).map { ReaderNativeFavoritesService.presentation($0) }.compactMap(ReaderNativeFavorite.init)
        }
        return (receipt["value"] as? [[String: Any]] ?? []).compactMap(ReaderNativeFavorite.init)
    }

    /// 收藏夹面板开着没有（原版 #vc-dock-panel：从屏幕底边升起的一条）。
    @Published var nativeFavoritesOpen = false

    /// 拖出收藏夹放到书页（收藏是复制，收藏夹里那张不动）。落点由 PDFKit 定页。
    func placeNativeFavorite(_ favorite: ReaderNativeFavorite, windowPoint: CGPoint) async -> Bool {
        guard nativePDFDocument != nil else {
            showTransientNotice("只有 PDF 原生正文里能把收藏卡放到书页上。")
            return false
        }
        guard let target = nativeDropTarget(windowPoint: windowPoint),
              let page = target["page"] as? Int, let x = target["x"] as? Double, let y = target["y"] as? Double else {
            showTransientNotice("请放到书页正文上。")
            return false
        }
        let receipt = await requestNativeConversationCommand([
            "action": "favoritesPlace", "scope": nativeConversation.scope,
            "value": ["id": favorite.id, "page": page, "x": x, "y": y],
        ])
        guard receipt["ok"] as? Bool == true else {
            showTransientNotice(receipt["error"] as? String ?? "没能放到书页上。")
            return false
        }
        scheduleNativePDFProjectionRefresh()
        return true
    }

    func deleteNativeFavorites(_ ids: [String]) async -> Bool {
        let receipt = await requestNativeConversationCommand([
            "action": "favoritesDelete", "scope": nativeConversation.scope, "value": ["ids": ids],
        ])
        if receipt["ok"] as? Bool != true {
            showTransientNotice(receipt["error"] as? String ?? "没能删除。")
            return false
        }
        return true
    }

    func loadNativeFavoritesTrash() async -> [ReaderNativeFavorite] {
        let receipt = await requestNativeConversationCommand(["action": "favoritesTrash", "scope": nativeConversation.scope])
        if receipt["ok"] as? Bool != true {
            showTransientNotice(receipt["error"] as? String ?? "回收站暂时无法读取。")
            return (nativeFavoritesService?.trashRecords ?? []).map { ReaderNativeFavoritesService.presentation($0) }.compactMap(ReaderNativeFavorite.init)
        }
        return (receipt["value"] as? [[String: Any]] ?? []).compactMap(ReaderNativeFavorite.init)
    }

    func restoreNativeFavorite(_ id: String) async -> Bool {
        let receipt = await requestNativeConversationCommand([
            "action": "favoritesRestore", "scope": nativeConversation.scope, "value": ["id": id],
        ])
        if receipt["ok"] as? Bool != true { showTransientNotice(receipt["error"] as? String ?? "恢复未获确认。") }
        return receipt["ok"] as? Bool == true
    }

    /// 长按 = 带入/移出对话。返回之后是否在对话里；失败返回 nil。
    func toggleNativeFavoritePin(_ id: String) async -> Bool? {
        let receipt = await requestNativeConversationCommand([
            "action": "favoritesPin", "scope": nativeConversation.scope, "value": ["id": id],
        ])
        guard receipt["ok"] as? Bool == true else {
            showTransientNotice(receipt["error"] as? String ?? "没能带入对话。")
            return nil
        }
        return (receipt["value"] as? [String: Any])?["pinned"] as? Bool
    }

    /// 最近一次原生选区（选区窗口「对话」时要重新送一遍）。
    private var lastNativePDFSelection: [ReaderNativePDFDocument.CharacterSelection] = []

    /// 选区窗口的「对话」（原版 onChat）：打开侧栏，把这段选区钉进对话。
    /// ⚠ 侧栏关着时选区按原版规定不钉进对话（__setFocusSel 第一句），所以先开侧栏、
    ///   等网页那侧知道侧栏开了，再把同一段选区重送一遍。
    func chatWithNativeSelection() async {
        guard let book = currentLocalBook, let digest = currentLocalBookContentSHA256,
              !lastNativePDFSelection.isEmpty else { return }
        if !nativeConversation.sidebarOpen {
            _ = await nativeConversation.perform("toggleAssistant")
            try? await Task.sleep(nanoseconds: 350_000_000)
        }
        if await updateNativePDFSelection(lastNativePDFSelection, bookID: book.id, contentSHA256: digest,
                                          scope: nativeConversation.scope) == false {
            showTransientNotice("选中的内容没能带进对话，请在侧栏打开后重新选一次。")
        }
    }

    func setNativeDocumentCaptureViewport(_ view: UIView?) {
        localRuntimeServer?.visualCaptureBroker.setNativeDocumentViewport(view)
    }

    // MARK: - iCloud 同步用的两个出入口

    private var cloudSync: ReaderCloudUserStateSync?
    private var cloudSyncBridge: ReaderCloudUserStateBridge?

    /// 开关由阅读设置里的 `@AppStorage("reader.iCloudSync")` 驱动，**默认关**。
    ///
    /// ⚠ 关掉只是停掉引擎，**不删云端也不删基线** —— 用户多半是"先别同步"而不是
    /// "把这些都扔了"。真要清（换账号）由 `accountChange` 那条路负责。
    func setCloudSyncEnabled(_ enabled: Bool) {
        guard enabled else { cloudSync = nil; cloudSyncBridge = nil; return }
        guard cloudSync == nil else { return }
        let bridge = ReaderCloudUserStateBridge(reader: self)
        let sync = ReaderCloudUserStateSync(source: bridge, store: ReaderCloudSyncStore())
        cloudSyncBridge = bridge
        cloudSync = sync
        Task { await sync.start() }
    }

    /// 本地写入后告诉同步器「这本书脏了」。
    /// ⚠ 只登记，不在这里导出：墨迹是一笔一次写入，导出整包会把主线程压住。
    private func markCloudSyncDirty() {
        guard let cloudSync, let digest = cloudSyncContentDigest else { return }
        Task { await cloudSync.markDirty(contentSHA256: digest) }
    }

    /// 当前这本书的内容摘要。⚠ 同步桥只看得到这一个 —— 别把
    /// `currentLocalBook` / adapter 这些也放出去：导出/写回必须留在这一侧，
    /// 它们要穿过该书的本地 runtime，在别处调就是对着错的书说话。
    var cloudSyncContentDigest: String? { currentLocalBookContentSHA256?.lowercased() }

    /// 导出当前这本书的各域快照给同步引擎。
    ///
    /// ⚠ 只对**当前打开的那本书**有效：导出要穿过该书的本地 runtime。摘要对不上
    /// 就返回空 —— 返回别的书的内容比返回空糟得多（会被当成"这本书是空的"推上云端）。
    func exportUserStateForCloudSync(contentSHA256: String)
        async throws -> [ReaderCloudUserStateSync.DomainSnapshot] {
        guard let adapter = bookUserStateWebAdapter, let book = currentLocalBook,
              currentLocalBookContentSHA256?.lowercased() == contentSHA256.lowercased() else { return [] }
        let generation = bookUserStateContextGeneration
        let domains = try await adapter.exportPackage(localBookId: book.id)
        guard generation == bookUserStateContextGeneration,
              currentLocalBookContentSHA256?.lowercased() == contentSHA256.lowercased() else { return [] }
        return domains.map {
            .init(name: $0.name.rawValue, payloadJson: $0.payloadJson,
                  digest: $0.digest, revision: Int($0.revision), empty: $0.empty)
        }
    }

    /// 把合并结果整域写回本地。
    ///
    /// ⚠ `expectedLocalHeaders` 必须取**此刻**的本地头：从"读快照"到"写回"之间
    /// 用户可能又划了一道。取旧的会把那一道盖掉，而 runtime 那道乐观并发闸
    /// （`BW_USER_STATE_LOCAL_CHANGED`）正是为此存在 —— 让它拒，下一轮重新合。
    func applyUserStateFromCloudSync(_ domains: [ReaderCloudUserStateSync.DomainSnapshot],
                                     contentSHA256: String) async throws {
        guard let adapter = bookUserStateWebAdapter, let book = currentLocalBook,
              currentLocalBookContentSHA256?.lowercased() == contentSHA256.lowercased(),
              !domains.isEmpty else { return }
        let generation = bookUserStateContextGeneration
        let headers = try await adapter.snapshotHeaders(localBookId: book.id)
        guard generation == bookUserStateContextGeneration else { return }

        var payloads: [ReaderBookUserStateDomainPayload] = []
        var expected: [String: ReaderBookUserStateDomainHeader] = [:]
        guard let merger = ReaderUserStateMerge() else { return }
        for domain in domains {
            guard let name = ReaderBookUserStateDomainName(rawValue: domain.name),
                  let header = headers[name] else { continue }
            // empty 用合并模块里那份逐字副本算 —— runtime 会拿它自己那份复核，
            // 对不上整笔事务被拒，而表面上只是"同步没生效"。
            guard let value = try? JSONSerialization.jsonObject(
                    with: Data(domain.payloadJson.utf8), options: [.fragmentsAllowed]),
                  let empty = try? merger.domainEmpty(domain: domain.name, value: value) else { continue }
            payloads.append(ReaderCloudUserStateEncoding.payload(
                name: name, json: domain.payloadJson,
                revision: Int64(max(1, domain.revision)), empty: empty))
            expected[name.rawValue] = header
        }
        guard !payloads.isEmpty else { return }

        let transaction = ReaderBookUserStateImportTransaction(
            contract: ReaderBookUserStateImportTransaction.currentContract,
            transactionId: "us_" + UUID().uuidString
                .replacingOccurrences(of: "-", with: "").lowercased(),
            localBookId: book.id,
            remoteBookId: ReaderCloudUserStateEncoding.remoteBookId(contentSHA256: contentSHA256),
            contentSha256: contentSHA256.lowercased(),
            packageRevision: 1,
            expectedLocalHeaders: expected,
            domains: payloads)
        _ = try await adapter.applyAtomically(transaction)
    }

    /// Prepare against the original identity and atomic user-state export. The
    /// caller retains this viewport; rendering ownership transfers after layout.
    func prepareNativePDFDocument() async throws -> ReaderNativePDFDocument {
        guard !isLoading, let access = currentLocalBookAccess, access.record.format == .pdf,
              isFinishedLocalBookURL(webView.url, bookID: access.record.id),
              let bridge = nativePDFNavigationBridge, bookUserStateWebAdapter != nil else {
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        let generation = bookUserStateContextGeneration
        let digest = try await currentLocalContentDigest(localBookId: access.record.id, generation: generation)
        let position = try await bridge.initialPosition()
        let domains = try await readingDomains(localBookID: access.record.id)
        guard generation == bookUserStateContextGeneration, currentLocalBookAccess === access,
              let page = (position["page"] as? NSNumber)?.intValue,
              let notes = domains.first(where: { $0.name == .notes }) else {
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        let document = ReaderNativePDFDocument()
        try document.open(access, contentSHA256: digest, page: page,
                          fraction: CGFloat((position["fraction"] as? NSNumber)?.doubleValue ?? 0))
        document.setLayout(mode: position["mode"] as? String ?? "continuous",
                           firstPageAlone: (position["spreadOffset"] as? NSNumber)?.intValue == 1)
        if position["cropEnabled"] as? Bool == true {
            guard let value = position["crop"] as? [String: Any], let crop = ReaderNativePDFCrop(value) else {
                throw ReaderBookUserStateWebAdapterError.invalidResponse
            }
            try document.setCrop(crop)
        }
        try document.applyOverlays(domains, bookID: access.record.id, contentSHA256: digest)
        try document.applyNotes(notes, bookID: access.record.id, contentSHA256: digest)
        return document
    }

    func activateNativePDFDocument(_ document: ReaderNativePDFDocument) async throws {
        guard !isLoading, let bookID = currentLocalBook?.id, let digest = currentLocalBookContentSHA256,
              let bridge = nativePDFNavigationBridge, document.matches(bookID: bookID, contentSHA256: digest) else {
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        let generation = bookUserStateContextGeneration
        // ⚠ 接管是否有效只看**书的身份**（加载代际 / 书 / 内容摘要），不看侧栏会话的 scope。
        //   scope 会合理地变化（切复习模式、账号上下文更新……）；以前把它算进来，再加上
        //   scope 曾随翻页变化，接管在第一次翻页后就失效了（2026-09-23 实录）。
        try await bridge.attach(document, bookID: bookID, contentSHA256: digest) { [weak self] in
            guard let self else { return false }
            return !self.isLoading && self.bookUserStateContextGeneration == generation
                && self.currentLocalBook?.id == bookID && self.currentLocalBookContentSHA256 == digest
        }
        activeNativePDFDocument = document
        document.view.cardDropReceiver = { [weak self, weak document] point in
            guard let self, let document, self.nativePDFDocument === document,
                  let location = document.canonicalPoint(point, from: document.view) else { return nil }
            let target: [String: Any] = ["page": location.page, "x": location.point.x, "y": location.point.y]
            let pointInWindow = document.view.convert(point, to: nil)
            return { [weak self, weak document] payload in
                Task { @MainActor in
                    guard let self, let document, self.nativePDFDocument === document else { return }
                    await self.placeNativeConversationCard(actionID: payload.actionID, scope: payload.scope,
                        windowPoint: pointInWindow, nativeTarget: target)
                }
            }
        }
        document.view.onCardDropError = { [weak self] message in self?.showTransientNotice(message) }
        document.onDismissTransient = { [weak self] in
            Task { @MainActor [weak self] in
                // 滚动 / 点空白：小框收起；还在查的词回来后不再自动弹（原版 _wordPopCancelSeq）。
                self?.nativeLookupCancelSeq += 1
                if self?.nativeWordPop != nil { self?.nativeWordPop = nil }
            }
        }
        document.onOpenPendingLookup = { [weak self] id in
            Task { @MainActor [weak self] in self?.openPendingLookup(id) }
        }
        nativePlacementsCancellable = nativeConversation.$placements.sink { [weak self] items in
            Task { @MainActor [weak self] in self?.reconcileNativeNotePlacements(items) }
        }
        document.onDiagnostic = { [weak self] line in
            Task { @MainActor [weak self] in self?.postClientLog(line) }
        }
        document.onHighlight = { [weak self] request in
            Task { @MainActor [weak self] in
                await self?.highlightFromNativeSelection(
                    request, bookID: bookID, contentSHA256: digest)
            }
        }
        document.onLookup = { [weak self] page, text, sentence, mode in
            Task { @MainActor [weak self] in
                self?.openNativeLookup(page: page, text: text, sentence: sentence, mode: mode)
            }
        }
        // 布局一变就重推墨迹表面：滚动/缩放后页面的屏幕位置变了，不推的话
        // Pencil 会画在上一帧的位置上。挂载那次的 onGeometry 已在回调里自清。
        document.onRecognize = { [weak self] page, rect in
            Task { @MainActor [weak self] in self?.recognizeNativeSelection(page: page, rect: rect) }
        }
        document.onEditHighlight = { [weak self] highlight in
            Task { @MainActor [weak self] in self?.openNativeHighlightEditor(highlight) }
        }
        document.onGrammar = { [weak self] _, sentence, focus in
            Task { @MainActor [weak self] in
                self?.openNativeGrammar(sentence: sentence, focus: focus)
            }
        }
        document.onGeometry = { [weak self] in
            Task { @MainActor [weak self] in self?.scheduleNativeInkSurfacePublish() }
        }
        publishNativeInkSurfaces()
        publishNativeHTMLNotes()
        refreshNativePageOverlays()
        document.onSelectionSearch = { query in
            // 原版 onSearchSel：用 Bing 搜选中内容。
            var parts = URLComponents(string: "https://www.bing.com/search")
            parts?.queryItems = [URLQueryItem(name: "q", value: String(query.prefix(400)))]
            if let target = parts?.url { UIApplication.shared.open(target) }
        }
        document.onSelectionChat = { [weak self] _, _, _ in
            Task { @MainActor [weak self] in await self?.chatWithNativeSelection() }
        }
        document.onSelection = { [weak self] values in
            Task { @MainActor [weak self] in
                // 用**当下**的会话 scope：接管那一刻的 scope 一旦过期，每次选中都会被悄悄丢掉。
                guard let self else { return }
                self.lastNativePDFSelection = values
                _ = await self.updateNativePDFSelection(values, bookID: bookID, contentSHA256: digest,
                                                        scope: self.nativeConversation.scope)
            }
        }
        setNativeDocumentCaptureViewport(document.view)
    }

    private func invalidateNativePDFDocument(reason: String = "unspecified") {
        if nativePDFDocument != nil || activeNativePDFDocument != nil {
            nativePDFLifecycleNotes.append(reason + "@" + String(Int(Date().timeIntervalSince1970) % 100000))
            if nativePDFLifecycleNotes.count > 12 { nativePDFLifecycleNotes.removeFirst() }
            ReaderNativeFaultReporter.shared.note("native-pdf", "invalidate:" + reason)
        }
        nativePDFMountTask?.cancel()
        nativePDFMountTask = nil
        nativeInkSurfaceTask?.cancel()
        nativeInkSurfaceTask = nil
        nativeProjectionRefreshTask?.cancel()
        nativeProjectionRefreshTask = nil
        cancelAllNativePageWork()
        nativeOverlayOrder.removeAll()
        nativeOverlayGeneration &+= 1
        nativeVocabularyEnrichmentTasks.values.forEach { $0.cancel() }
        nativeVocabularyEnrichmentTasks = [:]
        nativePageTranslationTasks.values.forEach { $0.cancel() }
        nativePageTranslationTasks = [:]
        nativeFigureTasks.values.forEach { $0.cancel() }
        nativeFigureTasks = [:]
        nativeOverlayPages = []
        nativeOverlayDocument = nil
        nativePDFIndex.reset()
        nativePDFNavigationBridge?.invalidate()
        activeNativePDFDocument?.onSelection = nil
        activeNativePDFDocument?.onGeometry = nil
        activeNativePDFDocument?.close()
        activeNativePDFDocument = nil
        if let mounted = nativePDFDocument {
            mounted.onGeometry = nil
            mounted.close()
            nativePDFDocument = nil
        }
        setNativeDocumentCaptureViewport(nil)
    }

    /// 把原生 PDF 主阅读区挂到界面上。
    ///
    /// ⚠ 顺序是被 `attach` 的前置条件定死的：它要求 `document.view.bounds` 已经
    /// 有尺寸（见 ReaderNativePDFNavigationBridge.attach）。所以必须
    /// **先发布让 SwiftUI 挂上去、等它布局完，才能 activate** —— 反过来做一定
    /// 拿到 0×0 然后抛 unavailable。`onGeometry` 就是"已布局"的信号
    /// （document 自己在 layoutChanged 里触发，此前没人接）。
    ///
    /// 本机 PDF 书一律原生（不再有开关）。打不开就**出声**（nativePDFOpenFailure），
    /// 不退回网页渲页 —— App 里那套已经不要了。
    func mountNativePDFDocument() {
        guard nativePDFExpected, nativePDFDocument == nil, nativePDFMountTask == nil, !isLoading else { return }
        let generation = bookUserStateContextGeneration
        nativePDFMountTask = Task { @MainActor [weak self] in
            guard let self else { return }
            defer { if self.bookUserStateContextGeneration == generation { self.nativePDFMountTask = nil } }
            let document: ReaderNativePDFDocument
            do { document = try await self.prepareNativePDFDocument() } catch {
                // 换书 / 导航中途被取消不算失败；其余一律摆出来，给「重试」。
                guard !Task.isCancelled, self.bookUserStateContextGeneration == generation,
                      self.nativePDFExpected else { return }
                let reason = String(describing: error).prefix(160)
                self.nativePDFOpenFailure = "正文没能打开：" + reason
                self.postClientLog("[native-pdf] prepare failed: " + reason)
                return
            }
            guard !Task.isCancelled, self.bookUserStateContextGeneration == generation else {
                document.close()
                return
            }
            // ⚠ 回调本身不是 MainActor 隔离的（与 onSelection 同一形态），所以一律
            //   先跳进 MainActor 再碰视图和模型。
            // ⚠ 只接管一次。布局回调一次布局里会连发好几下，每下都排一个 Task —— 它们在
            //   `document.onGeometry = nil` 生效之前就已经排进去了，于是几个 activate 并发：
            //   先到的拿到视口，后到的撞上「视口已被占用」报 unavailable。2026-09-23 实录
            //   同一毫秒 3～4 条 activate failed；后到的那个失败还把先到的成功拆掉了。
            let claim = ReaderNativeActivationClaim()
            document.onGeometry = { [weak self, weak document] in
                Task { @MainActor [weak self, weak document] in
                    guard let self, let document, !claim.claimed,
                          document.view.bounds.width > 0, document.view.bounds.height > 0,
                          self.bookUserStateContextGeneration == generation else { return }
                    claim.claimed = true
                    document.onGeometry = nil
                    do { try await self.activateNativePDFDocument(document) } catch {
                        // 已经被换掉的文档失败了，与当前这本无关，别去拆当前的。
                        guard self.nativePDFDocument === document else { return }
                        // 出声：静默失败的表现是「原生阅读区白着，没人知道为什么」。
                        // 卸下这份半挂的文档，让错误面板（带重试）顶上来，而不是一块白。
                        let reason = String(describing: error).prefix(160)
                        self.nativePDFMountFailure = String(reason)
                        self.invalidateNativePDFDocument(reason: "activate-failed")
                        self.nativePDFOpenFailure = "正文没能打开：" + reason
                        self.postClientLog("[native-pdf] activate failed: " + reason)
                    }
                }
            }
            self.nativePDFMountFailure = nil
            self.nativePDFOpenFailure = nil
            self.nativePDFDocument = document
            self.publishNativeInkSurfaces()
            self.publishNativeHTMLNotes()
            ReaderNativeStartupProfile.shared.mark("原生阅读区挂载")
            let history = self.nativePDFLifecycleNotes.joined(separator: ",")
            self.nativePDFLifecycleNotes = []
            let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?"
            self.postClientLog("[native-pdf] mounted build=" + build + (history.isEmpty ? "" : " after " + history))
        }
    }

    /// 错误面板上的「重试」。
    func retryNativePDFOpen() {
        nativePDFOpenFailure = nil
        mountNativePDFDocument()
    }

    /// 生词句子行首的「译」：整句交给原生翻译面板（与选区菜单里的「翻译」同一个，
    /// 不另做一套句子翻译 UI）。
    func openNativeSentenceTranslation(_ sentence: ReaderNativePDFDocument.VocabSentence) {
        openNativeLookup(page: sentence.page, text: sentence.text,
                         sentence: sentence.text, mode: "translate")
    }

    /// 原生选区菜单里点了查词/翻译：开一个原生面板，取数仍在阅读器那侧。
    private func openNativeLookup(page: Int, text: String, sentence: String, mode: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.count <= 2000, ["dict", "translate", "explain", "phrase"].contains(mode) else { return }
        // ⚠ 句境要用**所在整句**，不是选中串自己。以前这里传的是 trimmed.prefix(320)，
        // 等于告诉词典"这个词的上下文就是这个词" —— 一词多义时给出的那条释义，
        // 跟用户正在读的这句话未必是同一个意思。整句取不到才退回选中串。
        let whole = sentence.trimmingCharacters(in: .whitespacesAndNewlines)
        let generation = bookUserStateContextGeneration
        let bookID = currentLocalBook?.id
        let panel = ReaderNativeLookupModel(
            text: trimmed, mode: mode, page: max(0, page),
            context: String((whole.isEmpty ? trimmed : whole).prefix(320))
        ) { [weak self] command in
            guard let self, generation == self.bookUserStateContextGeneration, bookID == self.currentLocalBook?.id else {
                return ["ok": false, "error": "阅读页已切换"]
            }
            return await self.requestNativeConversationCommand(command)
        }
        // 标了掌握就重取一次叠加数据：否则这一页的下划线要翻页才消失。
        panel.onMarked = { [weak self] in self?.refreshNativePageOverlays(force: true) }
        panel.onGrammar = { [weak self] sentence, focus in
            guard let self else { return }
            self.nativeLookup = nil
            self.nativeWordPop = nil
            // 等词典面板收起再开语法面板（两个 sheet 不能同时出）。
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 450_000_000)
                self?.openNativeGrammar(sentence: sentence, focus: focus)
            }
        }
        // 点词 / 词组：贴着那个词弹小框（原版 #word-pop）；翻译、解释这类长结果仍走面板。
        if ["dict", "phrase"].contains(mode), let document = nativePDFDocument,
           let anchor = document.lastLookupAnchor {
            presentWordLookup(panel, anchor: anchor, document: document)
        } else {
            nativeWordPop = nil
            nativeLookup = panel
        }
    }

    // MARK: - 查词的等待表现（原版 15-phrase-wordpop「单击查词的等待表现」）
    //
    // 2026-09-23 用户："没有命中时应该用之前我们的那个闪烁逻辑，不然挡在这里什么都干不了"
    // （此前是一个转圈的框挡在正文上）。原版规则：
    //   · 400ms 内回来（已缓存 / 快词）→ 直接弹小框；
    //   · 慢了 → 不弹框，那个词呼吸高亮；可以接着读、接着点别的词（多个并存）；
    //   · 结果到了：期间没滚动、也没点别的词 → 自动弹小框；否则转常亮，点它才出结果。
    private var nativeLookupCancelSeq = 0
    private var pendingLookupPanels: [UUID: ReaderNativeLookupModel] = [:]

    private func presentWordLookup(_ panel: ReaderNativeLookupModel, anchor: CGRect,
                                   document: ReaderNativePDFDocument) {
        nativeLookupCancelSeq += 1
        let seq = nativeLookupCancelSeq
        let page = document.lastLookupPage, rects = document.lastLookupRects
        nativeWordPop = nil
        Task { @MainActor [weak self, weak document] in
            let loading = Task { await panel.load() }
            for _ in 0..<8 where panel.loading { try? await Task.sleep(nanoseconds: 50_000_000) }
            guard let self, let document, self.nativePDFDocument === document else { return }
            if !panel.loading {
                if seq == self.nativeLookupCancelSeq {
                    self.nativeWordPopAnchor = anchor
                    self.nativeWordPop = panel
                }
                return
            }
            let id = UUID()
            self.pendingLookupPanels[id] = panel
            document.addPendingLookup(id: id, page: page, rects: rects)
            await loading.value
            guard self.nativePDFDocument === document else { return }
            if seq == self.nativeLookupCancelSeq, panel.error == nil,
               let now = document.pendingLookupWindowRect(id) {
                self.openPendingLookup(id, anchor: now)
            } else {
                document.markPendingLookupReady(id)
            }
        }
    }

    private func openPendingLookup(_ id: UUID, anchor: CGRect? = nil) {
        guard let document = nativePDFDocument, let panel = pendingLookupPanels.removeValue(forKey: id) else { return }
        let place = anchor ?? document.pendingLookupWindowRect(id)
        document.removePendingLookup(id)
        guard let place else { return }
        nativeWordPopAnchor = place
        nativeWordPop = panel
        Task { await panel.load() }   // 还没查完的话接着等（load 只查一次）
    }

    /// 顶栏 🗒 新建便签。
    ///
    /// ⚠ 不能转给网页的 createAtCenter：它靠 document.elementFromPoint 找落点，
    /// 接管后一页都不在 DOM 里，七个候选点全落空 —— 便签没建，连"放不了"的
    /// toast 也看不见（toast 也在被藏的那层里）。页面位置此时只有 PDFKit 知道。
    func createNativeStickyNote() {
        guard let document = nativePDFDocument, let book = currentLocalBook, let access = currentLocalBookAccess,
              let digest = currentLocalBookContentSHA256, let deviceID = nativeReadingStoreDeviceID,
              nativeReadingStoreBookID == book.id, document.matches(bookID: book.id, contentSHA256: digest) else { return }
        // 落在视野中央那一页的正中。中央恰好在页缝时退到下一个可见页 ——
        // 与网页那侧「中央落空就试附近候选」是同一个意思。
        let pages = document.position.visiblePages
        guard let page = pages.first(where: { document.characterPageSize($0) != nil }) ?? pages.first else { return }
        Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                let generation = self.bookUserStateContextGeneration
                guard self.nativePDFMutationCommandDepth == 0 else { throw ReaderNativeBookStore.MutationError.unavailable }
                let pending = try await self.nativePDFMutationActor.hasUnfinishedMutation(book: access)
                guard !pending, self.nativePDFMutationCommandDepth == 0,
                      generation == self.bookUserStateContextGeneration, self.currentLocalBookAccess === access,
                      self.nativePDFDocument === document, document.matches(bookID: book.id, contentSHA256: digest) else {
                    throw ReaderNativeBookStore.MutationError.unavailable
                }
                let store = try self.nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
                let id = "c_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
                let body: [String: Any] = ["file": "localbook:" + book.id, "id": id,
                    "anchor": ["kind": "pdf", "page": page, "x": 0.5, "y": 0.5],
                    "color": "#ffffff", "w": 260, "h": 180]
                _ = try ReaderNativeBookStore(store: store, bookID: book.id, deviceID: deviceID, displayName: book.title, contentSHA256: digest)
                    .perform(["bookID": book.id, "mutationId": "create-note-" + id, "operation": "note-create",
                              "value": ["method": "POST", "body": body]])
                self.nativeReplicationService?.wake()
                self.markCloudSyncDirty()
                await self.refreshNativePDFProjection()
                if self.currentLocalBookAccess === access {
                    self.webView.callAsyncJavaScript("window.dispatchEvent(new CustomEvent('bw:native-book-committed',{detail:value})); return true;",
                        arguments: ["value": ["bookID": book.id]], in: nil, in: .page, completionHandler: nil)
                }
            } catch { self.nativeConversation.report("便签没有建成：" + error.localizedDescription) }
        }
    }

    /// 选区菜单里点了「OCR」：文字层坏掉时对这块重新识别。
    ///
    /// ⚠ 这不是"把服务端那套 OCR 接过来"：App 里 `/pdf/api/ocr-selection` 由本地
    /// runtime 接管，跑的是 **App 自己的** OCR（NativeBookOCRBridge），写回的也是
    /// App 自己的字符层 —— 识别完 `NativeBookOCRManager.lastUpdate` 会让这一页的
    /// 字符层失效并重读，所以这里不需要自己去刷新。
    private func recognizeNativeSelection(page: Int, rect: CGRect) {
        guard page > 0, rect.width >= 0.5, rect.height >= 0.5 else { return }
        let bbox: [Double] = [rect.minX, rect.minY, rect.maxX, rect.maxY]
        Task { @MainActor [weak self] in
            guard let self else { return }
            let receipt = await self.requestNativeConversationCommand([
                "action": "nativeOcrSelection", "scope": self.nativeConversation.scope,
                "value": ["page": page, "bbox": bbox],
            ])
            guard receipt["ok"] as? Bool == true,
                  let text = (receipt["value"] as? [String: Any])?["text"] as? String, !text.isEmpty else {
                self.nativeConversation.report(receipt["error"] as? String ?? "没有识别出文字。")
                return
            }
            // 结果要看得见：识别完悄无声息的话，用户不知道该不该再选一次。
            self.nativeConversation.report("已重新识别：" + String(text.prefix(120)))
        }
    }

    /// 点了已有划线 → 原生编辑面板（改色 / 备注 / 删除）。
    private func openNativeHighlightEditor(_ highlight: ReaderNativePDFDocument.Highlight) {
        let bookID = currentLocalBook?.id, digest = currentLocalBookContentSHA256
        let panel = ReaderNativeHighlightEditorModel(highlight: highlight) { [weak self] command in
            guard let self, let bookID, let digest, let value = command["value"] as? [String:Any] else {
                return ["ok":false,"error":"阅读页已关闭"]
            }
            return await self.performNativeHighlight(operation:"highlight-edit",input:value,bookID:bookID,digest:digest)
        }
        // 改完重取一次投影：颜色变了、虚框出现、整条消失，都要这一步才看得见。
        // PATCH/DELETE 经本地 runtime 时本来也会 ping 回来（withNativePDFWriter 的
        // 成功分支），这里再排一次是因为**面板是原生发起的**，不该指望那条回路。
        panel.onChanged = { [weak self] in
            self?.scheduleNativePDFProjectionRefresh()
        }
        nativeHighlightEditor = panel
    }

    /// 选区菜单里点了「语法」。分析对象是整句，焦点是选中那一段。
    private func openNativeGrammar(sentence: String, focus: String) {
        let whole = sentence.trimmingCharacters(in: .whitespacesAndNewlines)
        let picked = focus.trimmingCharacters(in: .whitespacesAndNewlines)
        // 整句取不到时退用选中串：宁可分析得窄一点，也不要什么都不发生。
        let target = whole.isEmpty ? picked : whole
        guard !target.isEmpty, target.count <= 4000 else { return }
        nativeGrammar = ReaderNativeGrammarModel(
            sentence: target, focus: picked.isEmpty ? target : picked
        ) { [weak self] command in
            await self?.requestNativeConversationCommand(command)
                ?? ["ok": false, "error": "阅读页已关闭"]
        }
    }

    /// 点图徽标 → 原生描述面板。描述文本随图一起取过来了，这里不再回网页问一次。
    func openNativeFigurePanel(_ figure: ReaderNativePDFDocument.Figure) {
        let document = nativePDFDocument
        let panel = ReaderNativeFigureModel(figure: figure) { [weak self, weak document] command in
            guard let self, let document, self.nativePDFDocument === document else { return ["ok": false, "error": "阅读页已关闭"] }
            return await self.requestNativeConversationCommand(command)
        }
        // 带入状态变了，正文上那个持久绿框和徽标颜色要跟着变。
        panel.onAttachChanged = { [weak self] attached in
            self?.nativePDFDocument?.setFigureAttached(attached, id: figure.id, page: figure.page)
        }
        nativeFigure = panel
    }

    /// The native selection already has exact PDF rectangles. Persist them
    /// with undo and replication in the same SQLite transaction as AI edits.
    private func highlightFromNativeSelection(
        _ request: ReaderNativePDFDocument.HighlightRequest,
        bookID: String, contentSHA256: String
    ) async {
        let trimmed = request.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.count <= 4000, request.page > 0,
              !request.rects.isEmpty, request.rects.count <= 512,
              request.pageWidth > 0, request.pageHeight > 0,
              ["yellow", "green", "blue", "pink"].contains(request.color),
              nativePDFDocument?.matches(bookID: bookID, contentSHA256: contentSHA256) == true else { return }
        let id = "c_" + UUID().uuidString.replacingOccurrences(of:"-",with:"").lowercased()
        let body: [String:Any] = ["file":"localbook:" + bookID,"id":id,"page":request.page,"text":trimmed,
            "color":ReaderNativeHighlightRules.palette[request.color]!,"sentence":String(request.sentence.prefix(600)),
            "rects":request.rects,"page_w":request.pageWidth,"page_h":request.pageHeight]
        let receipt = await performNativeHighlight(operation:"highlight-api",input:["method":"POST","body":body,"assistant":true],bookID:bookID,digest:contentSHA256)
        if receipt["ok"] as? Bool != true {
            nativePDFMountFailure = "划线失败：" + ((receipt["error"] as? String) ?? "未知原因")
        }
    }

    private func performNativeHighlight(operation:String,input:[String:Any],bookID:String,digest:String) async -> [String:Any] {
        do {
            guard let document = nativePDFDocument, let access = currentLocalBookAccess, access.record.id == bookID,
                  nativeReadingStoreBookID == bookID, let deviceID = nativeReadingStoreDeviceID,
                  document.matches(bookID:bookID,contentSHA256:digest), nativePDFMutationCommandDepth == 0 else {
                throw ReaderNativeBookStore.MutationError.unavailable
            }
            let generation = bookUserStateContextGeneration
            let pending = try await nativePDFMutationActor.hasUnfinishedMutation(book:access)
            guard !pending, nativePDFMutationCommandDepth == 0, generation == bookUserStateContextGeneration,
                  currentLocalBookAccess === access, currentLocalBookContentSHA256 == digest else { throw ReaderNativeBookStore.MutationError.unavailable }
            let store = try nativeDataStoreHost.bridge(for:"bw-reader-native-v1-document").store
            let receipt = try ReaderNativeBookStore(store:store,bookID:bookID,deviceID:deviceID,displayName:access.record.title,contentSHA256:digest)
                .perform(["bookID":bookID,"mutationId":"highlight-" + UUID().uuidString,"operation":operation,"value":input])
            let result = receipt["result"] as? [String:Any] ?? [:]
            let domains = try ReaderNativeBookProjection(store:store).exportReadingDomains(bookID:bookID)
            try document.applyOverlays(domains,bookID:bookID,contentSHA256:digest)
            nativeReplicationService?.wake()
            markCloudSyncDirty()
            // Transitional legacy consumers observe a committed record only.
            // They no longer normalize, save, or render a hidden PDF page.
            webView.callAsyncJavaScript("window.dispatchEvent(new CustomEvent('bw:native-highlight-committed',{detail:value})); return true;",
                arguments:["value":["bookID":bookID,"input":input,"result":result]],in:nil,in:.page,completionHandler:nil)
            return ["ok":true,"value":result]
        } catch { return ["ok":false,"error":error.localizedDescription] }
    }

    /// 本地 runtime 落了一笔用户状态 → 把高亮/墨迹/便签重新投影到原生正文。
    ///
    /// ⚠ 在此之前这三样是**开书那一刻的只读快照**：划完线、AI 改完、同步回来，
    /// 原生正文上什么都不会变，要关掉再开才看得见。
    ///
    /// 合并成一次：一次划线会连着落好几笔（高亮本体 + 关联记录），逐笔重投
    /// 等于把整包 user-state 导出好几遍。180ms 与导航桥的节流同口径。
    /// 网页那侧已经交出、原生这边还不知道的便签卡（刚放下的卡）。
    ///
    /// ⚠ 2026-09-23 实录：侧栏卡拖到书页，放置回执 ok=true、卡却不出现。便签先进网页内存、
    ///   异步落库；原生那次重读投影发生在落库之前，拿不到这张卡的位置就不画，之后也没有
    ///   任何事件再触发重读。这里对"交出来了但原生不认识"的卡退避重读，读到为止（至多 5 次）。
    private var nativePlacementsCancellable: AnyCancellable?
    private var nativeNotesReconciling = false

    private func reconcileNativeNotePlacements(_ items: [ReaderNativePagePlacement]) {
        guard !nativeNotesReconciling, let document = nativePDFDocument else { return }
        let known = Set(document.notes.compactMap { $0["id"] as? String })
        let missing = Set(items.filter { $0.fromNote && !known.contains($0.noteID) }.map(\.noteID))
        guard !missing.isEmpty else { return }
        nativeNotesReconciling = true
        Task { @MainActor [weak self, weak document] in
            defer { self?.nativeNotesReconciling = false }
            for attempt in 1...5 {
                try? await Task.sleep(nanoseconds: UInt64(attempt) * 400_000_000)
                guard let self, let document, self.nativePDFDocument === document else { return }
                await self.refreshNativePDFProjection()
                let now = Set(document.notes.compactMap { $0["id"] as? String })
                if missing.isSubset(of: now) { return }
            }
        }
    }

    private func scheduleNativePDFProjectionRefresh() {
        guard nativePDFDocument != nil else { return }
        guard nativeProjectionRefreshTask == nil else { return }
        nativeProjectionRefreshTask = Task { @MainActor [weak self] in
            defer { self?.nativeProjectionRefreshTask = nil }
            try? await Task.sleep(for: .milliseconds(180))
            guard let self, !Task.isCancelled else { return }
            await self.refreshNativePDFProjection()
        }
    }

    /// 重新导出 user-state 并投影。身份校验与 prepare 同口径：书、内容摘要、
    /// 上下文代际任一对不上就放弃 —— 把甲书的高亮画到乙书上比不更新糟得多。
    private func refreshNativePDFProjection() async {
        guard let document = nativePDFDocument, !isLoading,
              let access = currentLocalBookAccess, access.record.format == .pdf,
              let digest = currentLocalBookContentSHA256,
              bookUserStateWebAdapter != nil,
              document.matches(bookID: access.record.id, contentSHA256: digest) else { return }
        let generation = bookUserStateContextGeneration
        do {
            let domains = try await readingDomains(localBookID: access.record.id)
            guard generation == bookUserStateContextGeneration,
                  self.nativePDFDocument === document,
                  document.matches(bookID: access.record.id, contentSHA256: digest),
                  let notes = domains.first(where: { $0.name == .notes }) else { return }
            try document.applyOverlays(domains, bookID: access.record.id, contentSHA256: digest)
            try document.applyNotes(notes, bookID: access.record.id, contentSHA256: digest)
            if nativeFigures.hasAttachments { publishNativeFigureProjection() }
            publishNativeHTMLNotes()
        } catch {
            // 出声但不打断阅读：投影失败不该让正文消失。
            nativePDFMountFailure = "投影更新失败：" + String(describing: error).prefix(160)
        }
    }

    func captureNativeReadingHierarchyImage() throws -> UIImage {
        guard let localRuntimeServer else { throw NativeReaderCaptureError.pageUnavailable }
        return try localRuntimeServer.visualCaptureBroker.captureImage(region: nil)
    }

    private func performNativeCardCommand(_ command: [String: Any]) async -> [String: Any]? {
        guard command["action"] as? String == "liveAction", let token = command["actionId"] as? String,
              let target = nativeConversation.nativeCardAction(token),
              ["add", "del", "edit-front", "edit-back", "edit-cloze", "export-mobile", "export-desktop", "reveal"].contains(target.key),
              (target.input["entityRev"] as? NSNumber)?.int64Value ?? 0 > 0 else { return nil }
        // Queue-owned reviews have their own reveal/advance contract and are
        // migrated with the review controller, not as independent saved cards.
        if target.key == "reveal", target.input["controlledReview"] as? Bool == true { return nil }
        do {
            guard !isLoading, isTrustedReaderURL(webView.url), command["scope"] as? String == nativeConversation.scope,
                  let deviceID = nativeReadingStoreDeviceID,
                  let state = ReaderNativeCardPresentation.interaction(target.input) else {
                throw ReaderNativeCardRules.fail("UNAVAILABLE", "卡片上下文已切换")
            }
            let editing = target.key.hasPrefix("edit-")
            if editing {
                guard state["editable"] as? Bool == true else { throw ReaderNativeCardRules.fail("TRANSITION", "当前卡片不可编辑") }
            } else {
                guard let control = (state["controls"] as? [[String: Any]])?.first(where: { $0["key"] as? String == target.key }),
                      control["disabled"] as? Bool == false else { throw ReaderNativeCardRules.fail("TRANSITION", "当前卡片操作不可用") }
            }
            if target.key == "export-mobile", let gid = target.input["gid"] as? String, let index = target.input["cardIndex"] as? Int {
                return await exportNativeAnkiMobile(gid: gid, index: index)
            }
            if target.key == "export-desktop", let gid = target.input["gid"] as? String, let index = target.input["cardIndex"] as? Int {
                return await exportNativeAnkiPC(gid: gid, index: index)
            }
            let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-global").store
            guard try store.meta("legacyImport") == "done" else { throw ReaderNativeCardRules.fail("UNAVAILABLE", "卡库尚未就绪") }
            var input = target.input.filter { ["gid", "cardIndex", "entityRev", "stateRev"].contains($0.key) }
            input["action"] = editing ? "edit" : target.key
            if editing { input["field"] = String(target.key.dropFirst(5)); input["text"] = command["text"] ?? NSNull() }
            let receipt = try ReaderNativeCardRepository(store: store, deviceID: deviceID).perform([
                "operation": "interact", "arguments": [input], "mutationId": "native-card-ui:" + UUID().uuidString])
            if let record = receipt["result"] as? [String: Any] {
                nativeConversation.acceptCardRecord(record)
                if input["action"] as? String == "add", target.input["autoExportDesktop"] as? Bool == true,
                   let gid = input["gid"] as? String, let index = input["cardIndex"] as? Int {
                    Task { @MainActor [weak self] in _ = await self?.exportNativeAnkiPC(gid: gid, index: index) }
                }
            }
            markCloudSyncDirty(); scheduleNativePDFProjectionRefresh()
            // Compatibility observers consume a receipt, never a command to
            // perform the local mutation again. Their failure cannot undo a
            // committed save or make the UI report it as an unknown mutation.
            do {
                _ = try await webView.callAsyncJavaScript(
                    "window.RC?.flashcard?.acceptNativeRecord(record,index,action); window.__bwNativeConversation?.snapshot?.();",
                    arguments: ["record": receipt["result"]!, "index": input["cardIndex"]!, "action": input["action"]!],
                    in: nil, contentWorld: .page)
            } catch { postClientLog("卡片已在原生卡库提交，兼容投影待刷新：" + error.localizedDescription) }
            return ["ok": true, "committed": true]
        } catch let error as ReaderNativeCardRules.Failure {
            return ["ok": false, "code": error.code, "error": error.detail]
        } catch { return ["ok": false, "error": error.localizedDescription] }
    }

    private var nativeLookupCache: [String: [String: Any]] = [:]
    private var nativeLookupCacheBytes = 0
    private var nativeLookupTasks: [String: (id: UUID, task: Task<[String: Any], Error>)] = [:]
    private var nativeWordLookupEntries: [String: [String: Any]] = [:]
    private var nativeWordLookupOrder: [String] = []
    private var nativeWordLookupCacheLoaded = false

    private func nativeWordEntry(_ input: [String: Any], book: ReaderLocalBookRecord,
                                 languages: [String], gateway: ReaderNativeServerGateway) async throws -> [String: Any] {
        let word = (input["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !word.isEmpty, word.utf16.count <= 2000, !word.contains("\0") else {
            throw ReaderNativeLookupRequest.Failure(message: "BW_READER_LOOKUP_TEXT")
        }
        let context = String((input["context"] as? String ?? "").prefix(320))
        let japanese = ReaderNativeLookupRequest.isJapanese(word, languages: languages)
        let generation = bookUserStateContextGeneration, gatewayContext = gateway.contextRevision
        let surface: ReaderNativeInterfaceSurface = book.format == .pdf ? .pdf : .epub
        let deviceStore = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-device").store
        let globalStore = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-global").store
        let cacheMeta = "native-word-lookup-cache-v1"
        if !nativeWordLookupCacheLoaded {
            if let raw = try deviceStore.meta(cacheMeta), raw.utf8.count <= 4 * 1024 * 1024,
               let cached = try? JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [String: Any] {
                nativeWordLookupOrder = Array((cached["order"] as? [String] ?? []).suffix(600))
                let entries = cached["entries"] as? [String: [String: Any]] ?? [:]
                for key in nativeWordLookupOrder { nativeWordLookupEntries[key] = entries[key] }
            }
            nativeWordLookupCacheLoaded = true
        }
        var query = URLComponents(); query.path = "/pdf/api/dict-quick"
        let page = max(0, (input["page"] as? NSNumber)?.intValue ?? 0)
        query.queryItems = [.init(name: "word", value: word), .init(name: "file", value: "localbook:" + book.id),
            .init(name: "page", value: String(page)), .init(name: "context", value: context),
            .init(name: "langs", value: languages.joined(separator: ","))]
        query.percentEncodedQuery = query.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
        guard let path = query.string else { throw ReaderNativeLookupRequest.Failure(message: "BW_READER_LOOKUP_TEXT") }
        let cacheKey = path, taskKey = "word:" + String(generation) + ":" + path
        let raw: [String: Any]
        if let hit = nativeWordLookupEntries[cacheKey], ReaderNativeWordLookup.cacheable(hit, japanese: japanese) { raw = hit }
        else {
            let job: Task<[String: Any], Error>, jobID: UUID
            if let running = nativeLookupTasks[taskKey] { job = running.task; jobID = running.id }
            else {
                guard nativeLookupTasks.count < 24 else { throw ReaderNativeLookupRequest.Failure(message: "查询正在处理中，请稍候") }
                jobID = UUID()
                job = Task { @MainActor in
                    try await ReaderNativeWordLookup.lookup(japanese: japanese, local: {
                        let data = try await ReaderNativeOfflineDictionary.shared.lookup(word)
                        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
                    }, remote: {
                        let response = try await gateway.fetchData(path: path, surface: surface)
                        guard (200..<300).contains(response.status) else { throw ReaderNativeLookupRequest.Failure(message: "词典服务器暂不可用") }
                        return try JSONSerialization.jsonObject(with: response.data) as? [String: Any] ?? [:]
                    }, fallback: { base in
                        guard word.utf16.count <= 256 else { return nil }
                        let socket = DirectVoiceSocket(configuration: .readerContext) { _ in }
                        do {
                            _ = try await socket.openReaderContext()
                            let reply = try await socket.requestReaderDictionary(term: word, context: context,
                                reading: String((base["reading"] as? String ?? "").prefix(128)))
                            await socket.disconnect()
                            let object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(reply)) as? [String: Any] ?? [:]
                            var value = base
                            value["ok"] = true; value["jp"] = true; value["word"] = word
                            if (value["lemma"] as? String ?? "").isEmpty { value["lemma"] = word }
                            value["zh"] = object["text"]; value["translation"] = object["text"]
                            value["meaning_source"] = "pc-codex-cli"; value["cli_cached"] = object["cached"]
                            if (value["source"] as? String ?? "").isEmpty { value["source"] = "pc-codex-cli" }
                            return ReaderNativeWordLookup.meaning(value).isEmpty ? nil : value
                        } catch { await socket.disconnect(); throw error }
                    })
                }
                nativeLookupTasks[taskKey] = (jobID, job)
            }
            defer { if nativeLookupTasks[taskKey]?.id == jobID { nativeLookupTasks.removeValue(forKey: taskKey) } }
            raw = try await job.value
            try Task.checkCancellation()
            guard generation == bookUserStateContextGeneration, gatewayContext == gateway.contextRevision,
                  currentLocalBook?.id == book.id else { throw CancellationError() }
            if ReaderNativeWordLookup.cacheable(raw, japanese: japanese) {
                var cached = raw
                for field in ["mastered", "cached", "cli_cached"] { cached.removeValue(forKey: field) }
                nativeWordLookupEntries[cacheKey] = cached
                nativeWordLookupOrder.removeAll { $0 == cacheKey }; nativeWordLookupOrder.append(cacheKey)
                while nativeWordLookupOrder.count > 600 { nativeWordLookupEntries.removeValue(forKey: nativeWordLookupOrder.removeFirst()) }
                var data = try JSONSerialization.data(withJSONObject: ["order": nativeWordLookupOrder, "entries": nativeWordLookupEntries])
                while data.count > 4 * 1024 * 1024, !nativeWordLookupOrder.isEmpty {
                    nativeWordLookupEntries.removeValue(forKey: nativeWordLookupOrder.removeFirst())
                    data = try JSONSerialization.data(withJSONObject: ["order": nativeWordLookupOrder, "entries": nativeWordLookupEntries])
                }
                // A disposable lookup cache cannot turn a successful read into
                // a failed query; canonical vocabulary state is separate.
                try? deviceStore.putMeta(cacheMeta, json: String(decoding: data, as: UTF8.self))
            }
        }
        guard generation == bookUserStateContextGeneration, gatewayContext == gateway.contextRevision else { throw CancellationError() }
        var mastered = raw["mastered"] as? Bool == true
        if let deviceID = nativeReadingStoreDeviceID {
            let spec: [String: Any] = ["kind": "word", "language": japanese ? "ja" : "en",
                "lemma": (raw["lemma"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? word,
                "word": word, "forms": raw["forms"] as? [String] ?? []]
            if let id = (try? ReaderNativeVocabularyState.normalized(spec, property: "mastered"))?["id"] as? String,
               try globalStore.record(collection: ReaderNativeVocabularyState.collection, id: id) != nil {
                mastered = try ReaderNativeVocabularyState(store: globalStore, deviceID: deviceID).enabled(spec, property: "mastered")
            }
        }
        return try ReaderNativeWordLookup.entry(raw, word: word, japanese: japanese, mastered: mastered)
    }

    private func performNativeLookupCommand(_ command: [String: Any]) async -> [String: Any]? {
        guard command["action"] as? String == "nativeSelectionLookup", let input = command["value"] as? [String: Any],
              let mode = input["mode"] as? String, ["translate", "example-zh", "dict", "dict-full", "phrase"].contains(mode) else { return nil }
        do {
            guard !isLoading, let book = currentLocalBook, let gateway = nativeServerGateway,
                  isTrustedReaderURL(webView.url) else { throw ReaderNativeLookupRequest.Failure(message: "阅读页尚未就绪") }
            let generation = bookUserStateContextGeneration
            let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
            let languages = try ReaderNativeBookProjection(store: store).state("book-languages", bookID: book.id).payload as? [String] ?? []
            if mode == "dict" {
                return ["ok": true, "value": try await nativeWordEntry(input, book: book, languages: languages, gateway: gateway)]
            }
            if mode == "phrase" {
                guard let text = input["text"] as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                      text.utf16.count <= 2000, let service = nativePhraseService else {
                    throw ReaderNativeLookupRequest.Failure(message: "词组上下文尚未就绪")
                }
                let japanese = ReaderNativeLookupRequest.isJapanese(text, languages: languages)
                var value: [String: Any]
                if japanese {
                    value = try await nativeWordEntry(input, book: book, languages: languages, gateway: gateway)
                } else {
                    var translation = input; translation["mode"] = "translate"
                    let receipt = await performNativeLookupCommand(["action": "nativeSelectionLookup", "value": translation])
                    guard receipt?["ok"] as? Bool == true, let content = receipt?["value"] as? [String: Any] else {
                        throw ReaderNativeLookupRequest.Failure(message: receipt?["error"] as? String ?? "词组查询失败")
                    }
                    value = content
                }
                for (key, state) in try await service.lookupState(text, japanese: japanese) { value[key] = state }
                guard generation == bookUserStateContextGeneration, currentLocalBook?.id == book.id else {
                    throw ReaderNativeLookupRequest.Failure(message: "阅读页已切换")
                }
                return ["ok": true, "value": value]
            }
            let plan = try ReaderNativeLookupRequest(input, file: "localbook:" + book.id, languages: languages)
            let key = String(generation) + ":" + plan.mode + ":" + plan.path + ":" + plan.body.base64EncodedString()
            if let cached = nativeLookupCache[key] { return ["ok": true, "value": cached] }
            let job: Task<[String: Any], Error>, jobID: UUID
            if let running = nativeLookupTasks[key] { job = running.task; jobID = running.id }
            else {
                guard nativeLookupTasks.count < 24 else { throw ReaderNativeLookupRequest.Failure(message: "查询正在处理中，请稍候") }
                jobID = UUID()
                job = Task { @MainActor in
                    try await plan.response(directTranslation: {
                        try await ReaderTranslateDirectService.shared.cachedOrTranslate(plan.text, target: "zh-CN")
                    }, remote: {
                        let response = try await gateway.fetchData(path: plan.path, method: plan.method, body: plan.body,
                                                                  surface: book.format == .pdf ? .pdf : .epub)
                        return (response.status, response.data)
                    })
                }
                nativeLookupTasks[key] = (jobID, job)
            }
            defer { if nativeLookupTasks[key]?.id == jobID { nativeLookupTasks.removeValue(forKey: key) } }
            let value = try await job.value
            guard generation == bookUserStateContextGeneration, currentLocalBook?.id == book.id else {
                throw ReaderNativeLookupRequest.Failure(message: "阅读页已切换")
            }
            // Cache only complete read results. Empty example translations can
            // be retried; failures never start a duplicate web request.
            let cacheBytes = (try? JSONSerialization.data(withJSONObject: value).count) ?? Int.max
            if cacheBytes <= 64 * 1024, nativeLookupCache[key] == nil,
               mode != "example-zh" || !(value["zh"] as? String ?? "").isEmpty {
                if nativeLookupCache.count >= 128 || nativeLookupCacheBytes + cacheBytes > 1024 * 1024 {
                    nativeLookupCache.removeAll(keepingCapacity: true); nativeLookupCacheBytes = 0
                }
                nativeLookupCache[key] = value
                nativeLookupCacheBytes += cacheBytes
            }
            return ["ok": true, "value": value]
        } catch { return ["ok": false, "error": error.localizedDescription] }
    }

    private func performNativeVocabularyCommand(_ command: [String: Any]) async -> [String: Any]? {
        guard command["action"] as? String == "nativeVocabMark" else { return nil }
        do {
            guard !isLoading, isTrustedReaderURL(webView.url), let book = currentLocalBook,
                  let gateway = nativeServerGateway, let deviceID = nativeReadingStoreDeviceID,
                  let input = command["value"] as? [String: Any], let original = input["word"] as? String else {
                throw ReaderNativeVocabularyState.Failure(message: "词汇上下文尚未就绪")
            }
            let word = original.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !word.isEmpty, word.utf16.count <= 200, !word.contains("\0") else {
                throw ReaderNativeVocabularyState.Failure(message: "词无效")
            }
            let generation = bookUserStateContextGeneration
            let documentStore = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
            let languages = try ReaderNativeBookProjection(store: documentStore).state("book-languages", bookID: book.id).payload as? [String] ?? []
            let japanese = input["jp"] as? Bool ?? ReaderNativeLookupRequest.isJapanese(word, languages: languages)
            let mastered = input["mastered"] as? Bool != false
            let kind = input["kind"] as? String ?? "word"
            guard ["word", "phrase"].contains(kind) else { throw ReaderNativeVocabularyState.Failure(message: "词汇类型无效") }
            let phrase = kind == "phrase"
            let vocabularyInput: [String: Any] = ["kind": kind, "language": japanese ? "ja" : "en", "lemma": word, "text": word, "word": word]
            _ = try ReaderNativeVocabularyState.normalized(vocabularyInput, property: "mastered", enabled: mastered)
            let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-global").store
            guard try store.meta("legacyImport") == "done" else { throw ReaderNativeVocabularyState.Failure(message: "词汇库尚未就绪") }
            // Keep the compatibility lexicon's confirmation semantics. Unknown
            // transport results are reported; no second request is attempted.
            let path = phrase ? "/pdf/api/phrase-mark" : (japanese ? "/pdf/api/jp-vocab-mark" : "/pdf/api/vocab-mark")
            let markBody = phrase ? ["text": word, "mark": mastered ? "mastered" : ""]
                : ["word": word, "mark": mastered ? "known" : "unknown"]
            let response = try await gateway.fetchData(path: path,
                method: "POST", body: JSONSerialization.data(withJSONObject: markBody),
                surface: book.format == .pdf ? .pdf : .epub)
            guard (200..<300).contains(response.status),
                  let reply = try JSONSerialization.jsonObject(with: response.data) as? [String: Any],
                  (try? ReaderNativeCardRules.bool(reply["ok"], "ok")) == true else {
                throw ReaderNativeVocabularyState.Failure(message: "词汇标记未获确认")
            }
            guard generation == bookUserStateContextGeneration, currentLocalBook?.id == book.id else {
                throw ReaderNativeVocabularyState.Failure(message: "阅读页已切换，请刷新词汇状态")
            }
            let record = try ReaderNativeVocabularyState(store: store, deviceID: deviceID).set(vocabularyInput,
                property: "mastered", enabled: mastered, mutation: "native-vocab-ui:" + UUID().uuidString)
            nativeLookupCache.removeAll(); nativeLookupCacheBytes = 0
            markCloudSyncDirty()
            // Notify remaining presentation observers and their legacy display
            // mirrors. They do not re-run the canonical mastery transaction,
            // and the observation does not delay the native receipt.
            if phrase {
                do {
                    try prepareNativePhraseService(deviceID: deviceID)
                    _ = try await nativePhraseService?.read()
                }
                catch { showTransientNotice("掌握状态已保存，词组分词刷新待重试：" + error.localizedDescription) }
                nativePDFDocument?.invalidateTokenization()
            }
            webView.callAsyncJavaScript("window.BWReaderRuntime?.vocabularyState?.importRecord(record,{source:'native'}); if (!phrase) window.applyVocabLocalOverride?.(word,mastered,{word,surface:word,forms:[],jp});",
                arguments: ["record": record, "word": word, "mastered": mastered, "jp": japanese, "phrase": phrase], in: nil, in: .page,
                completionHandler: { _ in })
            return ["ok": true, "value": ["ok": true, "mastered": mastered, "jp": japanese]]
        } catch { return ["ok": false, "error": error.localizedDescription] }
    }

    private func prepareNativeReviewImprovements() throws -> ReaderNativeReviewImprovements {
        guard let book = currentLocalBook, let gateway = nativeServerGateway else {
            throw ReaderNativeReviewImprovements.Failure(message: "草稿服务尚未连接")
        }
        let generation = bookUserStateContextGeneration, context = gateway.contextRevision
        if let service = nativeReviewImprovements, nativeReviewImprovementsContext == generation,
           nativeReviewImprovementsGatewayContext == context { return service }
        nativeReviewImprovements?.invalidate()
        let device = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-device").store
        guard try device.meta("legacyImport") == "done" else {
            throw ReaderNativeReviewImprovements.Failure(message: "本机数据尚未完成导入")
        }
        let surface: ReaderNativeInterfaceSurface = book.format == .epub ? .epub : .pdf
        let service = ReaderNativeReviewImprovements(fetch: { [weak self] path, body in
            guard let self, self.bookUserStateContextGeneration == generation,
                  self.nativeServerGateway?.contextRevision == context else { throw CancellationError() }
            let result = try await gateway.fetchData(path: path, method: "POST", body: body, surface: surface)
            // Persist a received commit receipt even after navigating away.
            // The owner separately fences whether it can still publish it.
            return .init(status: result.status, data: result.data)
        }, read: { try device.meta($0) }, write: { try device.putMeta($0, json: $1) })
        nativeReviewImprovements = service; nativeReviewImprovementsContext = generation
        nativeReviewImprovementsGatewayContext = context
        return service
    }

    private func prepareNativeReviewQueue() throws -> ReaderNativeReviewQueue {
        guard let book = currentLocalBook, let deviceID = nativeReadingStoreDeviceID,
              nativeReadingStoreBookID == book.id else {
            throw ReaderNativeReviewQueue.Failure(message: "复习卡库尚未就绪")
        }
        let generation = bookUserStateContextGeneration
        if let service = nativeReviewQueue, nativeReviewQueueContext == generation,
           nativeReviewQueueGatewayContext == nativeServerGateway?.contextRevision { return service }
        nativeReviewQueue?.invalidate()
        let global = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-global").store
        let device = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-device").store
        guard try global.meta("legacyImport") == "done", try device.meta("legacyImport") == "done" else {
            throw ReaderNativeReviewQueue.Failure(message: "复习卡库尚未完成导入")
        }
        let surface: ReaderNativeInterfaceSurface = book.format == .epub ? .epub : .pdf
        let gateway = nativeServerGateway, gatewayContext = nativeServerGateway?.contextRevision
        let check: () throws -> Void = { [weak self] in
            guard let self, self.bookUserStateContextGeneration == generation, self.currentLocalBook?.id == book.id,
                  self.nativeServerGateway?.contextRevision == gatewayContext else { throw CancellationError() }
        }
        let service = ReaderNativeReviewQueue(local: {
            try check()
            return try ReaderNativeCardRepository(store: global, deviceID: deviceID).reviewQueue()
        }, read: {
            try check(); return try device.meta(ReaderNativeReviewQueue.cacheKey)
        }, write: { text in
            try check(); try device.putMeta(ReaderNativeReviewQueue.cacheKey, json: text)
        }, fetch: { path, method, body in
            try check()
            guard let gateway else { throw ReaderNativeReviewQueue.Failure(message: "复习服务连接不可用") }
            let result = try await gateway.fetchData(path: path, method: method, body: body, surface: surface)
            // Queue loads have their own lease check. A score already sent
            // keeps its actual receipt even after the visible book changes.
            return .init(status: result.status, data: result.data)
        })
        nativeReviewQueue = service; nativeReviewQueueContext = generation; nativeReviewQueueGatewayContext = gatewayContext
        return service
    }

    private func prepareNativeFavoritesService() throws -> ReaderNativeFavoritesService {
        guard let gateway = nativeServerGateway, let book = currentLocalBook else {
            throw ReaderNativeFavoritesService.Failure(message: "收藏夹连接尚未就绪")
        }
        let context = gateway.contextRevision, generation = bookUserStateContextGeneration
        if let service = nativeFavoritesService, nativeFavoritesContext == context { return service }
        nativeFavoritesService?.invalidate()
        nativeFavoritesContext = context
        nativeConversation.setNativeFavoritesCount(nil)
        let surface: ReaderNativeInterfaceSurface = book.format == .epub ? .epub : .pdf
        let service = ReaderNativeFavoritesService(fetch: { [weak self] path, method, body in
            guard let self, self.bookUserStateContextGeneration == generation,
                  gateway.contextRevision == context else { throw CancellationError() }
            let result = try await gateway.fetchData(path: path, method: method, body: body ?? Data(), surface: surface)
            guard self.bookUserStateContextGeneration == generation, gateway.contextRevision == context else { throw CancellationError() }
            return .init(status: result.status, data: result.data)
        }, changed: { [weak self] rows in
            guard let self, self.bookUserStateContextGeneration == generation,
                  self.nativeFavoritesContext == context else { return }
            self.nativeConversation.setNativeFavoritesCount(rows.count)
            self.webView.callAsyncJavaScript("window.__bwReaderAcceptNativeFavorites?.(snapshot); return true;",
                arguments: ["snapshot": ["cards": rows, "context": String(context),
                                         "revision": self.nativeFavoritesService?.projectionRevision ?? 0]],
                in: nil, in: .page, completionHandler: nil)
        })
        nativeFavoritesService = service
        return service
    }

    private func performNativeFavoritesCommand(_ command: [String: Any]) async -> [String: Any]? {
        guard let action = command["action"] as? String,
              ["favoritesList", "favoritesTrash", "favoritesDelete", "favoritesRestore", "favoritesPlace"].contains(action) else { return nil }
        do {
            guard !isLoading, isTrustedReaderURL(webView.url), command["scope"] as? String == nativeConversation.scope else {
                throw ReaderNativeFavoritesService.Failure(message: "收藏夹所属页面已切换")
            }
            let service = try prepareNativeFavoritesService()
            let scope = nativeConversation.scope
            var value = command["value"] as? [String: Any] ?? [:]
            if action == "favoritesPlace" {
                guard let book = currentLocalBook, let access = currentLocalBookAccess,
                      let document = nativePDFDocument, let digest = currentLocalBookContentSHA256,
                      nativeReadingStoreBookID == book.id, let deviceID = nativeReadingStoreDeviceID,
                      let id = value["id"] as? String, let page = value["page"] as? Int,
                      let x = value["x"] as? Double, let y = value["y"] as? Double else {
                    throw ReaderNativeFavoritesService.Failure(message: "书页卡片尚未就绪")
                }
                let generation = bookUserStateContextGeneration
                let data = try await service.perform("read")
                guard let rows = data["cards"] as? [[String: Any]], let row = rows.first(where: { $0["id"] as? String == id }) else {
                    throw ReaderNativeFavoritesService.Failure(message: "这张卡已不在收藏夹里")
                }
                guard nativePDFMutationCommandDepth == 0 else { throw ReaderNativeBookStore.MutationError.unavailable }
                let pending = try await nativePDFMutationActor.hasUnfinishedMutation(book: access)
                guard !pending, nativePDFMutationCommandDepth == 0, generation == bookUserStateContextGeneration,
                      currentLocalBookAccess === access, nativePDFDocument === document, scope == nativeConversation.scope,
                      document.matches(bookID: book.id, contentSHA256: digest) else { throw ReaderNativeBookStore.MutationError.unavailable }
                let body = try ReaderNativeFavoritePlacement.body(row, file: "localbook:" + book.id, page: page, x: x, y: y,
                    pageWidth: Double(document.characterPageSize(page)?.width ?? 0))
                let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
                let receipt = try ReaderNativeBookStore(store: store, bookID: book.id, deviceID: deviceID,
                    displayName: book.title, contentSHA256: digest).perform([
                        "bookID": book.id, "mutationId": "favorite-place-" + (body["id"] as! String),
                        "operation": "note-create", "value": ["method": "POST", "body": body]])
                nativeReplicationService?.wake(); markCloudSyncDirty()
                await refreshNativePDFProjection()
                if currentLocalBookAccess === access {
                    webView.callAsyncJavaScript("window.dispatchEvent(new CustomEvent('bw:native-book-committed',{detail:value})); return true;",
                        arguments: ["value": ["bookID": book.id]], in: nil, in: .page, completionHandler: nil)
                }
                return ["ok": true, "value": receipt]
            }
            let operation: String
            switch action {
            case "favoritesList": operation = "read"; value["refresh"] = true
            case "favoritesTrash": operation = "trash"
            case "favoritesDelete": operation = "delete"
            default: operation = "restore"
            }
            let result = try await service.perform(operation, value)
            guard scope == nativeConversation.scope else { throw CancellationError() }
            if operation == "read" || operation == "trash" {
                let rows = result["cards"] as? [[String: Any]] ?? []
                // Pin state still belongs to the shared selection registry.
                // Read only its small identity set, never card HTML or nodes.
                let ids = rows.compactMap { $0["id"] as? String }
                let pinned = (try? await webView.callAsyncJavaScript(
                    "return ids.filter(id => window.RC?.voiceCard?.favorite?.pinned?.(id));",
                    arguments: ["ids": ids], in: nil, contentWorld: .page)) as? [String] ?? []
                guard scope == nativeConversation.scope else { throw CancellationError() }
                return ["ok": true, "value": rows.map { ReaderNativeFavoritesService.presentation($0, pinned: pinned.contains($0["id"] as? String ?? "")) }]
            }
            return ["ok": true, "value": result]
        } catch { return ["ok": false, "error": error.localizedDescription] }
    }

    private func prepareNativePhraseService(deviceID: String) throws {
        if nativePhraseService?.deviceID == deviceID { return }
        let device = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-device").store
        let global = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-global").store
        guard try device.meta("legacyImport") == "done", try global.meta("legacyImport") == "done" else {
            throw ReaderNativeVocabularyState.Failure(message: "词组库尚未完成导入")
        }
        nativePhraseService = ReaderNativePhraseService(store: device, global: global, deviceID: deviceID,
            seedSource: { [weak self] in
                if let self, let gateway = self.nativeServerGateway {
                    do {
                        let response = try await gateway.fetchData(path: "/pdf/api/phrases",
                            surface: self.currentLocalBook?.format == .epub ? .epub : .pdf)
                        if (200..<300).contains(response.status),
                           let value = try JSONSerialization.jsonObject(with: response.data) as? [String: Any],
                           value["ok"] as? Bool == true, let phrases = value["phrases"] as? [String] { return phrases }
                    } catch { /* Read-only fallback to the existing mirror. */ }
                }
                let mirror = try await ReaderLocalRuntimeServer.requestBridgeMirror(["path": "/reader-phrases", "method": "GET"])
                guard mirror["ok"] as? Bool == true, let body = mirror["body"] as? [String: Any],
                      let phrases = body["phrases"] as? [String] else {
                    throw ReaderNativeVocabularyState.Failure(message: "历史词组暂时无法取回，未写入空清单")
                }
                return phrases
            }, changed: { [weak self] phrases, records in
                guard let self else { return }
                self.nativeLookupCache.removeAll(); self.nativeLookupCacheBytes = 0
                self.nativePDFDocument?.invalidateTokenization()
                self.refreshNativePageOverlays(force: true)
                self.markCloudSyncDirty()
                // Presentation observers consume committed state only. They
                // must not write the list or call phrases-set a second time.
                self.webView.callAsyncJavaScript("for (const record of records) await window.BWReaderRuntime?.vocabularyState?.importRecord(record,{source:'native'}); window.__bwReaderAcceptNativePhrases?.(phrases); window.dispatchEvent(new Event('bw:native-phrases-changed'));",
                    arguments: ["phrases": phrases, "records": records], in: nil, in: .page, completionHandler: { _ in })
            }, report: { [weak self] message in self?.postClientLog("原生词组：" + message) })
        nativePhraseService?.wake()
    }

    private func nativeReadingSettingsState(bookID: String, generation: UInt64) throws -> [String: Any] {
        guard bookUserStateContextGeneration == generation, currentLocalBook?.id == bookID,
              let document = nativePDFDocument, let deviceID = nativeReadingStoreDeviceID else {
            throw ReaderBookUserStateWebAdapterError.contextChanged
        }
        let catalog = try ReaderNativePreferences.Catalog.packaged.get()
        func raw(_ key: String) throws -> String? {
            let entry = try catalog.entry(key)
            let store = try nativeDataStoreHost.bridge(for: entry.storeName).store
            guard try store.meta("legacyImport") == "done" else { throw ReaderBookUserStateWebAdapterError.unavailable }
            return try ReaderNativePreferences(store: store, deviceID: deviceID).raw(entry)
        }
        func enabled(_ key: String, fallback: Bool) throws -> Bool {
            guard let value = try raw(key) else { return fallback }
            return value == "1"
        }
        let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
        let projection = ReaderNativeBookProjection(store: store)
        let languages = try projection.state("book-languages", bookID: bookID).payload ?? []
        guard let languages = languages as? [String], languages.allSatisfy({ ["en", "ja", "zh", "ko", "fr", "de"].contains($0) }) else {
            throw ReaderNativeBookStore.MutationError.invalid("书籍语言")
        }
        let cropPayload = try projection.state("book-crop", bookID: bookID).payload
        guard cropPayload == nil || cropPayload is [String: Any] else { throw ReaderNativeBookStore.MutationError.invalid("书籍裁边") }
        let crop = cropPayload as? [String: Any] ?? [:]
        guard Set(crop.keys).isSubset(of: Set(["l", "r", "t", "b"])) else { throw ReaderNativeBookStore.MutationError.invalid("裁边字段") }
        var cropValue: [String: Double] = [:]
        for key in ["l", "r", "t", "b"] {
            if let value = crop[key] {
                guard let numberValue = value as? NSNumber, CFGetTypeID(numberValue) != CFBooleanGetTypeID() else { throw ReaderNativeBookStore.MutationError.invalid("裁边比例") }
                let number = try ReaderNativeCardRules.number(numberValue, "裁边")
                guard number <= 45 else { throw ReaderNativeBookStore.MutationError.invalid("裁边比例") }
                cropValue[key] = number
            } else { cropValue[key] = 0 }
        }
        var colors = ["#fff59d", "#a7f3d0", "#a3d4ff", "#fda4af"]
        if let value = try raw("pdf-hl-colors"), let bytes = value.data(using: .utf8),
           let saved = try? JSONSerialization.jsonObject(with: bytes) as? [String], !saved.isEmpty { colors = saved }
        let grammar = try raw("pdf-grammar-view") ?? "components"
        let figures = nativeReadingFiguresSetting?.generation == generation ? nativeReadingFiguresSetting : nil
        return ["host": "pdf", "book": "localbook:" + bookID,
                "vocabulary": try enabled("pdf-vocab-underline", fallback: true),
                "clickTranslate": try enabled("pdf-click-translate-unmastered", fallback: true),
                "autoOrient": try enabled("pdf-auto-orient", fallback: false), "debug": try enabled("pdf-debug", fallback: false),
                "languages": languages, "crop": cropValue, "cropEnabled": document.position.crop != nil,
                "grammar": ["deps", "skeleton", "components", "tree"].contains(grammar) ? grammar : "components",
                "colors": colors, "figures": figures?.enabled ?? false, "figuresAvailable": figures?.enabled != nil,
                "warnings": figures?.warning.map { [$0] } ?? []]
    }

    private func performNativeReadingSettings(_ command: [String: Any]) async -> [String: Any]? {
        guard let action = command["action"] as? String, ["readingSettingsRead", "readingSettingsWrite"].contains(action),
              currentLocalBook?.format == .pdf else { return nil }
        guard !isLoading, isTrustedReaderURL(webView.url), let book = currentLocalBook,
              nativeReadingStoreBookID == book.id, let deviceID = nativeReadingStoreDeviceID,
              let document = nativePDFDocument, command["scope"] as? String == nativeConversation.scope else {
            return ["ok": false, "error": "阅读设置尚未就绪"]
        }
        let generation = bookUserStateContextGeneration, scope = nativeConversation.scope
        func current() throws {
            guard bookUserStateContextGeneration == generation, currentLocalBook?.id == book.id,
                  nativePDFDocument === document, nativeConversation.scope == scope, !Task.isCancelled else {
                throw ReaderBookUserStateWebAdapterError.contextChanged
            }
        }
        do {
            // Drain earlier compatibility intents once at an explicit settings
            // action. Page rendering never needs this barrier.
            _ = try await webView.callAsyncJavaScript("if (!window.__BW_READER_PREFERENCES__?.flush) throw new Error('设置尚未就绪'); await window.__BW_READER_PREFERENCES__.flush(); return true;",
                arguments: [:], in: nil, contentWorld: .page)
            try current()
            var committed: [String: Any]? = nil
            if action == "readingSettingsWrite" {
                guard let key = command["key"] as? String, let value = command["value"] else { throw ReaderNativePreferences.Failure(message: "缺少设置值") }
                let flags = ["vocabulary": "pdf-vocab-underline", "clickTranslate": "pdf-click-translate-unmastered", "autoOrient": "pdf-auto-orient", "debug": "pdf-debug"]
                let settingKey: String?, raw: String?
                if let name = flags[key] {
                    settingKey = name; raw = try ReaderNativeCardRules.bool(value, key) ? "1" : "0"
                } else if key == "grammar" {
                    guard let mode = value as? String, ["deps", "skeleton", "components", "tree"].contains(mode) else { throw ReaderNativePreferences.Failure(message: "语法显示选项无效") }
                    settingKey = "pdf-grammar-view"; raw = mode
                } else if key == "colors" {
                    guard let colors = value as? [String], !colors.isEmpty, colors.count <= 32,
                          colors.allSatisfy({ $0.range(of: "^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$", options: .regularExpression) != nil }) else {
                        throw ReaderNativePreferences.Failure(message: "请使用有效的颜色值")
                    }
                    var seen = Set<String>()
                    settingKey = "pdf-hl-colors"; raw = String(decoding: try ReaderNativeCardRules.bytes(colors.filter { seen.insert($0).inserted }), as: UTF8.self)
                } else { settingKey = nil; raw = nil }
                if let settingKey, let raw {
                    let entry = try ReaderNativePreferences.Catalog.packaged.get().entry(settingKey)
                    let store = try nativeDataStoreHost.bridge(for: entry.storeName).store
                    let owner = ReaderNativePreferences(store: store, deviceID: deviceID)
                    committed = try owner.commit(entry, raw: raw, mutation: "native-settings-" + UUID().uuidString)
                    markCloudSyncDirty()
                } else if key == "languages" || key == "crop" {
                    if key == "languages" {
                        guard let languages = value as? [String], languages.count <= 2, languages.allSatisfy({ ["en", "ja"].contains($0) }) else {
                            throw ReaderNativeBookStore.MutationError.invalid("语言选项")
                        }
                    }
                    let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
                    let kind = key == "languages" ? "book-languages" : "book-crop"
                    let previous = try ReaderNativeBookProjection(store: store).state(kind, bookID: book.id)
                    _ = try ReaderNativeBookStore(store: store, bookID: book.id, deviceID: deviceID).perform([
                        "bookID": book.id, "operation": kind, "value": value, "expectedRevision": previous.revision,
                        "mutationId": "native-settings-" + UUID().uuidString])
                    markCloudSyncDirty()
                    if key == "languages" {
                        nativeLookupCache.removeAll(); nativeLookupCacheBytes = 0; document.invalidateTokenization()
                    } else {
                        guard let data = value as? [String: Any], let crop = ReaderNativePDFCrop(data), let navigation = nativePDFNavigationBridge else {
                            throw ReaderNativeBookStore.MutationError.invalid("裁边视口")
                        }
                        let enabled = document.position.crop != nil || data.values.contains { ($0 as? NSNumber)?.doubleValue ?? 0 > 0 }
                        try await navigation.applyCrop(enabled ? crop : nil, expectedBookID: book.id)
                        try current()
                    }
                } else if key == "cropEnabled" {
                    let enabled = try ReaderNativeCardRules.bool(value, key)
                    let state = try nativeReadingSettingsState(bookID: book.id, generation: generation)
                    guard let data = state["crop"] as? [String: Any], let crop = ReaderNativePDFCrop(data), let navigation = nativePDFNavigationBridge,
                          !enabled || data.values.contains(where: { ($0 as? NSNumber)?.doubleValue ?? 0 > 0 }) else {
                        throw ReaderNativePreferences.Failure(message: "请先设置去边比例")
                    }
                    try await navigation.applyCrop(enabled ? crop : nil, expectedBookID: book.id)
                    try current()
                } else if key == "figures" {
                    let enabled = try ReaderNativeCardRules.bool(value, key)
                    guard let gateway = nativeServerGateway else { throw ReaderNativePreferences.Failure(message: "插图服务尚未连接") }
                    let context = gateway.contextRevision
                    let response = try await gateway.fetchData(path: "/pdf/api/book-figures", method: "POST",
                        body: ReaderNativeCardRules.bytes(["file": "localbook:" + book.id, "enabled": enabled]), surface: .pdf)
                    try current()
                    guard gateway.contextRevision == context, (200..<300).contains(response.status),
                          let reply = try JSONSerialization.jsonObject(with: response.data) as? [String: Any], reply["ok"] as? Bool == true,
                          let flag = reply["enabled"] as? NSNumber, CFGetTypeID(flag) == CFBooleanGetTypeID() else {
                        throw ReaderNativePreferences.Failure(message: "插图设置未确认保存")
                    }
                    nativeReadingFiguresSetting = (generation, flag.boolValue, nil)
                } else { throw ReaderNativePreferences.Failure(message: "未知阅读设置") }
                try current()
                let snapshot = try nativeReadingSettingsState(bookID: book.id, generation: generation)
                let result = try await webView.callAsyncJavaScript("""
                    if (window.__bwNativeConversation?.currentScope?.() !== scope) return false;
                    if (receipt) window.__bwNativePreferencesObserve(receipt);
                    return window.__bwReaderAcceptNativeReadingSettings?.(snapshot, key) === true;
                    """, arguments: ["snapshot": snapshot, "key": key, "receipt": committed as Any? ?? NSNull(), "scope": scope], in: nil, contentWorld: .page)
                try current()
                guard result as? Bool == true else { throw ReaderNativePreferences.Failure(message: "设置已保存，界面状态同步未完成，请重新打开设置核对") }
                refreshNativePageOverlays(force: true)
                return ["ok": true, "value": snapshot]
            }
            do {
                guard let gateway = nativeServerGateway else { throw ReaderNativePreferences.Failure(message: "服务器尚未连接") }
                let context = gateway.contextRevision
                var path = URLComponents(); path.path = "/pdf/api/book-figures"; path.queryItems = [.init(name: "file", value: "localbook:" + book.id)]
                let response = try await gateway.fetchData(path: path.string!, surface: .pdf)
                try current()
                guard gateway.contextRevision == context, (200..<300).contains(response.status),
                      let reply = try JSONSerialization.jsonObject(with: response.data) as? [String: Any], reply["ok"] as? Bool == true,
                      let enabled = reply["enabled"] as? NSNumber, CFGetTypeID(enabled) == CFBooleanGetTypeID() else { throw ReaderNativePreferences.Failure(message: "返回格式无效") }
                nativeReadingFiguresSetting = (generation, enabled.boolValue, nil)
            } catch {
                try current()
                nativeReadingFiguresSetting = (generation, nil, "插图分析设置暂不可读取：" + error.localizedDescription)
            }
            try current()
            let snapshot = try nativeReadingSettingsState(bookID: book.id, generation: generation)
            let mirrored = try await webView.callAsyncJavaScript("if (window.__bwNativeConversation?.currentScope?.() !== scope) return false; return window.__bwReaderAcceptNativeReadingSettings?.(snapshot, '') === true;",
                arguments: ["scope": scope, "snapshot": snapshot], in: nil, contentWorld: .page)
            try current()
            guard mirrored as? Bool == true else { throw ReaderBookUserStateWebAdapterError.contextChanged }
            return ["ok": true, "value": snapshot]
        } catch { return ["ok": false, "error": error.localizedDescription] }
    }

    private func performNativeReviewSource(_ command: [String: Any]) async -> [String: Any]? {
        guard command["action"] as? String == "reviewAction", let value = command["value"] as? [String: Any],
              value["key"] as? String == "source" else { return nil }
        guard !isLoading, isTrustedReaderURL(webView.url), command["scope"] as? String == nativeConversation.scope,
              let queue = nativeReviewQueue else { return ["ok": false, "error": "复习队列尚未就绪"] }
        do {
            let card = try queue.currentCard(context: value["contextKey"] as? String ?? "", cardID: value["cardId"] as? String ?? "")
            let target = ReaderNativeReviewFaces.source(card)
            if let file = target.file, let page = target.page {
                let localID = file.hasPrefix("localbook:") ? String(file.dropFirst(10)) : file
                if let library = currentLocalLibrary, let book = library.books.first(where: { $0.id == localID }) {
                    if book.id == currentLocalBook?.id, book.format == .pdf, let navigation = nativePDFNavigationBridge {
                        _ = try await navigation.navigate("jump", value: page)
                        return ["ok": true]
                    }
                    let started = await openLocalBook(book, library: library, restorationToken: nil, initialPage: page)
                    return ["ok": started, "error": started ? "" : "无法打开卡片出处"]
                }
                if !file.hasPrefix("localbook:"), Self.isSafeRemoteLibraryRelativePath(file), let base = localRuntimeServer?.baseURL,
                   var url = URLComponents(url: base, resolvingAgainstBaseURL: false) {
                    url.path = "/pdf/view"; url.queryItems = [.init(name: "file", value: file), .init(name: "page", value: String(page))]
                    if let endpoint = url.url, takeOverRemoteBookNavigation(endpoint, sourceURL: webView.url) { return ["ok": true] }
                }
            }
            let currentFile = currentLocalBook.map { "localbook:" + $0.id } ?? ""
            let sameDocument = target.documentID.isEmpty || target.documentID == currentFile || target.documentID == "reader-book:" + currentFile
            if sameDocument, currentLocalBook?.format == .pdf, let page = target.page, let navigation = nativePDFNavigationBridge {
                _ = try await navigation.navigate("jump", value: page); return ["ok": true]
            }
            // EPUB's CFI/DOM anchors remain owned by its retained WebKit renderer.
            if sameDocument, currentLocalBook?.format == .epub, !target.locations.isEmpty { return nil }
            if let url = target.url {
                let opened = await UIApplication.shared.open(url)
                return ["ok": opened, "error": opened ? "" : "无法打开卡片出处链接"]
            }
            return ["ok": false, "error": "这张卡尚未记录可直接打开的原笔记链接"]
        } catch { return ["ok": false, "error": error.localizedDescription] }
    }

    private func requestNativeConversationCommand(_ command: [String: Any]) async -> [String: Any] {
        if let result = await performNativeReviewSource(command) { return result }
        if command["action"] as? String == "settingsRead", command["section"] as? String == "computer" {
            guard !isLoading, isTrustedReaderURL(webView.url), command["scope"] as? String == nativeConversation.scope,
                  let voice = nativeVoiceBridge else { return ["ok":false,"error":"电脑通话状态尚未就绪"] }
            let scope = nativeConversation.scope, generation = bookUserStateContextGeneration
            let value = await voice.settingsStatus()
            guard !Task.isCancelled, nativeConversation.scope == scope, bookUserStateContextGeneration == generation else {
                return ["ok":false,"error":"页面已切换"]
            }
            return ["ok":true,"value":value]
        }
        if ["settingsRead","settingsWrite"].contains(command["action"] as? String ?? ""),
           ["models","voice","profiles"].contains(command["section"] as? String ?? "") {
            guard !isLoading, isTrustedReaderURL(webView.url), let gateway = nativeServerGateway,
                  command["scope"] as? String == nativeConversation.scope, let deviceID = nativeReadingStoreDeviceID else {
                return ["ok":false,"error":"设置尚未就绪"]
            }
            let scope = nativeConversation.scope, generation = bookUserStateContextGeneration
            let epoch = gateway.contextRevision, surface: ReaderNativeInterfaceSurface = currentLocalBook?.format == .epub ? .epub : .pdf
            func current() throws {
                guard !isLoading, nativeConversation.scope == scope, bookUserStateContextGeneration == generation,
                      gateway.contextRevision == epoch, !Task.isCancelled else { throw CancellationError() }
            }
            do {
                let catalog = try ReaderNativePreferences.Catalog.packaged.get()
                _ = try await webView.callAsyncJavaScript("if (!window.__BW_READER_PREFERENCES__?.flush) throw new Error('设置尚未就绪'); await window.__BW_READER_PREFERENCES__.flush(); return true;",
                    arguments:[:],in:nil,contentWorld:.page)
                try current()
                let result = try await nativeAssistantSettings.perform(command,scope:scope,gateway:gateway,surface:surface,raw:{ key in
                    try current()
                    let entry = try catalog.entry(key), store = try self.nativeDataStoreHost.bridge(for:entry.storeName).store
                    guard try store.meta("legacyImport") == "done" else { throw ReaderBookUserStateWebAdapterError.unavailable }
                    return try ReaderNativePreferences(store:store,deviceID:deviceID).raw(entry)
                },write:{ key,raw in
                    try current()
                    let entry = try catalog.entry(key), store = try self.nativeDataStoreHost.bridge(for:entry.storeName).store
                    guard try store.meta("legacyImport") == "done" else { throw ReaderBookUserStateWebAdapterError.unavailable }
                    let result = try ReaderNativePreferences(store:store,deviceID:deviceID).commit(entry,raw:raw,mutation:"assistant-setting-" + UUID().uuidString)
                    self.markCloudSyncDirty(); self.nativeReplicationService?.wake()
                    let observed = try await self.webView.callAsyncJavaScript("if (window.__bwNativeConversation?.currentScope?.() !== scope) return false; await window.__bwNativePreferencesObserve(receipt); return true;",
                        arguments:["scope":scope,"receipt":result],in:nil,contentWorld:.page)
                    try current()
                    guard observed as? Bool == true else { throw ReaderNativePreferences.Failure(message:"设置已保存，显示同步未完成，请重新打开核对") }
                },refreshVoice:{
                    try current()
                    _ = try await self.webView.callAsyncJavaScript("if (window.__bwNativeConversation?.currentScope?.() !== scope) throw new Error('语音上下文已切换'); await window.RC?.voicecall?.pushCfg?.(); return true;",
                        arguments:["scope":scope],in:nil,contentWorld:.page)
                    try current()
                },isCurrent:{ (try? current()) != nil })
                try current(); return ["ok":true,"value":result]
            } catch { return ["ok":false,"error":error.localizedDescription] }
        }
        if currentLocalBook?.format == .pdf,
           ["navigationRead","navigationAction","searchRead","searchJump","tocRead","tocJump"].contains(command["action"] as? String ?? "") {
            guard !isLoading, isTrustedReaderURL(webView.url), let book = currentLocalBook,
                  let digest = currentLocalBookContentSHA256, let document = nativePDFDocument,
                  let navigation = nativePDFNavigationBridge, command["scope"] as? String == nativeConversation.scope else {
                return ["ok":false,"error":"阅读索引尚未就绪"]
            }
            let generation = bookUserStateContextGeneration, scope = nativeConversation.scope
            do {
                return try await nativePDFIndex.perform(command,bookID:book.id,digest:digest,scope:scope,
                    document:document,navigation:navigation,isCurrent:{ [weak self, weak document] in
                        guard let self, let document else { return false }
                        return !self.isLoading && self.bookUserStateContextGeneration == generation
                            && self.currentLocalBook?.id == book.id && self.nativePDFDocument === document
                            && self.nativeConversation.scope == scope
                    })
            } catch { return ["ok":false,"error":error.localizedDescription] }
        }
        if let result = await performNativeReadingSettings(command) { return result }
        if let result = await performNativeFavoritesCommand(command) { return result }
        if let result = await performNativeFigureCommand(command) { return result }
        if command["action"] as? String == "nativePhraseFav" {
            guard !isLoading, isTrustedReaderURL(webView.url),
                  let service = nativePhraseService, let value = command["value"] as? [String: Any],
                  let text = value["text"] as? String, let enabled = value["enabled"] as? Bool else {
                return ["ok": false, "error": "词组上下文尚未就绪"]
            }
            do { return ["ok": true, "value": try await service.set(text, enabled: enabled)] }
            catch { return ["ok": false, "error": error.localizedDescription] }
        }
        if let result = await performNativeVocabularyCommand(command) { return result }
        if let result = await performNativeLookupCommand(command) { return result }
        if let result = await performNativeCardCommand(command) { return result }
        if let result = await performNativeHTMLNoteCommand(command) { return result }
        let allowed: Set<String> = ["send", "stop", "openModels", "openSettings", "openReview",
            // "showLegacy" 已删除：旧网页界面不再是一个可以被请求的目的地。
            // "openArtifact" / "action" 一并删除：它们唯一的实现是把旧网页界面
            // 端出来（reveal→setLegacy），而原生界面从来没有地方会去点它们。
            "hideLegacy", "refresh", "snapshot", "openTOC", "openSearch",
            "favoritesList", "favoritesPlace", "favoritesDelete", "favoritesTrash", "favoritesRestore", "favoritesPin",
            "toggleVoice", "toggleComputerVoice", "newConversation", "openHistory", "toggleAssistant", "liveAction", "clearSelection", "inspectArtifact", "mediaResource", "settingsRead", "settingsWrite", "reviewAction", "searchRead", "searchJump",
            "tocRead", "tocJump", "navigationRead", "navigationAction", "clearConversation", "readingSettingsRead", "readingSettingsWrite", "nativePageSelection",
            // 原生选区菜单的划线：转交阅读器自己的划线路径（见 highlightFromNativeSelection）
            "nativeSelectionHighlight", "nativeSelectionLookup",
            "nativeVocabMark", "nativeFigureAttach",
            "nativeGrammar", "nativeHighlightEdit", "nativePhraseFav", "nativeCreateNote",
            "nativeOcrSelection", "nativeEpubHighlight", "nativeEpubHighlightColors",
            "anchorPreview"]
        guard let action = command["action"] as? String, allowed.contains(action),
              JSONSerialization.isValidJSONObject(command),
              isTrustedReaderURL(webView.url), !isLoading else {
            return ["ok": false, "error": "阅读页尚未准备好，请稍后重试"]
        }
        do {
            let result = try await webView.callAsyncJavaScript(
                "if (!window.__bwNativeConversation) return {ok:false,error:'阅读页尚未准备好'}; return await window.__bwNativeConversation.perform(command);",
                arguments: ["command": command], in: nil, contentWorld: .page
            )
            guard let receipt = result as? [String: Any], receipt["ok"] as? Bool == true else {
                return ["ok": false, "error": (result as? [String: Any])?["error"] as? String ?? "操作未完成，请重试"]
            }
            return receipt
        } catch {
            return ["ok": false, "error": "操作未完成：\(error.localizedDescription)"]
        }
    }

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
        let conversationProxy = WeakScriptMessageHandler(delegate: self)
        nativeConversationMessageProxy = conversationProxy
        contentController.add(conversationProxy, name: nativeConversationMessageName)
        // ⚠ 网页那套外壳（顶栏等）在 App 这个表面上**默认就不存在**，而不是
        //   "先存在、再由脚本盖住"（2026-09-22 用户：「为何要压制，既然重置了就把
        //   原版删掉啊」）。
        //
        //   原来的做法把这条样式写在 `ReaderNativeConversationScript` 里，而那个
        //   脚本是 **atDocumentEnd** 注入的，还要再等 Swift 把 setNativeMode 送到
        //   页面才会生效。可原生顶栏是 SwiftUI 画的，**不等任何人**。中间这段窗口
        //   里两套一起在；更糟的是只要那条消息没送到（页面重载、渲染进程被回收后
        //   恢复、脚本还没装好），这个"两套同时存在"的状态就是**永久**的 ——
        //   用户看到的"所有元素好像都有两种实现"正是它。
        //
        //   改成 atDocumentStart 落一条样式 + 一个类：默认没有网页外壳；关掉原生
        //   界面时才由 `bw-native-legacy-chrome` 把它放回来。**失败的方向反过来了**
        //   —— 脚本没跑成，结果是"网页外壳不出现"（原生顶栏照常工作），
        //   而不是"两套一起出现"。
        let legacyChrome = !(UserDefaults.standard.object(forKey: "reader.nativeInterfaceEnabled") as? Bool ?? true)
        contentController.addUserScript(WKUserScript(
            source: """
            (() => {
              const root = document.documentElement;
              root.classList.add('bw-native-shell');
              window.__BW_NATIVE_CONVERSATION_DATA__ = \(legacyChrome ? "false" : "true");
              if (\(legacyChrome ? "true" : "false")) root.classList.add('bw-native-legacy-chrome');
              const style = document.createElement('style');
              style.id = 'bw-native-shell-style';
              style.textContent =
                '.bw-native-shell:not(.bw-native-legacy-chrome) #header,' +
                '.bw-native-shell:not(.bw-native-legacy-chrome) #ep-top,' +
                '.bw-native-shell:not(.bw-native-legacy-chrome) #fs-restore,' +
                // ⚠ 网页顶栏的收起把手（rc-ui.js 的 mountCollapsibleTopbar）挂在顶栏
                //   **外面**（el.parentNode），所以只藏顶栏它还在 —— 屏幕中间那个
                //   孤零零的小药丸就是它（2026-09-22 用户：“旧的上边栏把手没有清除”）。
                '.bw-native-shell:not(.bw-native-legacy-chrome) .rc-topbar-pill,' +
                // ⚠ rc-voicecall 把两个语音按钮插在 #fs-toggle 旁边当顶栏入口，
                //   它们不在被藏的容器里 —— EPUB 上就成了正文上方一条孤零零的条
                //   （2026-09-22 用户：“epub 上方不知为何有个这东西”）。
                //   原生侧栏里有同款的电脑语音/通话，这两个是多余的。
                '.bw-native-shell:not(.bw-native-legacy-chrome) #vc-top-computer,' +
                '.bw-native-shell:not(.bw-native-legacy-chrome) #vc-top-call{display:none!important}';
              root.appendChild(style);
            })();
            """,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
        contentController.addUserScript(WKUserScript(
            source: ReaderNativeConversationScript.source,
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: true
        ))
        nativeConversation.commandHandler = { [weak self] command in
            guard let self else { return "阅读页已关闭" }
            return await self.performNativeConversationCommand(command)
        }
        nativeConversation.inspectionHandler = { [weak self] command in
            guard let self else { return ["ok": false, "error": "阅读页已关闭"] }
            return await self.requestNativeConversationCommand(command)
        }
        nativeConversation.videoRequestHandler = { [weak self] scope, path, method, body in
            guard let self, scope == self.nativeConversation.scope, self.isTrustedReaderURL(self.webView.url),
                  ReaderNativeVideoPlayer.Coordinator.allowed(path: path, method: method) else { throw CancellationError() }
            if path == "/pdf/api/video-player-prefs" {
                guard let deviceID = self.nativeReadingStoreDeviceID else { throw URLError(.resourceUnavailable) }
                let store = try self.nativeDataStoreHost.bridge(for: "bw-reader-native-v1-device").store
                guard try store.meta("legacyImport") == "done" else { throw URLError(.resourceUnavailable) }
                let result = try ReaderNativeVideoPreferences(store: store, deviceID: deviceID).request(method: method, body: body)
                return ["status": 200, "body": String(decoding: try JSONSerialization.data(withJSONObject: result), as: UTF8.self)]
            }
            guard let gateway = self.nativeServerGateway else { throw URLError(.resourceUnavailable) }
            let surface: ReaderNativeInterfaceSurface = self.currentLocalBook?.format == .epub ? .epub : .pdf
            let response = try await gateway.fetchData(path: path, method: method, surface: surface)
            guard scope == self.nativeConversation.scope, response.data.count <= 8 * 1_024 * 1_024 else { throw CancellationError() }
            return ["status": response.status, "body": String(decoding: response.data, as: UTF8.self)]
        }
        nativeConversation.imageHandler = { [weak self] scope, id in
            guard let self, let base = self.localRuntimeServer?.baseURL,
                  let referer = self.webView.url, self.isTrustedReaderURL(referer),
                  scope == self.nativeConversation.scope else { throw URLError(.resourceUnavailable) }
            let result: [String: Any]
            if id.hasPrefix("native-inline:") {
                guard let route = self.nativeConversation.nativeInlineResource(id) else { throw URLError(.resourceUnavailable) }
                result = ["ok": true, "resource": route]
            } else {
                result = await self.requestNativeConversationCommand([
                    "action": "mediaResource", "scope": scope, "actionId": id
                ])
            }
            guard result["ok"] as? Bool == true, let route = result["resource"] as? String else {
                throw URLError(.resourceUnavailable)
            }
            if route.hasPrefix("data:image/"), let comma = route.firstIndex(of: ","),
               route[..<comma].hasSuffix(";base64"), route.utf8.count <= 23 * 1_024 * 1_024,
               let bytes = Data(base64Encoded: String(route[route.index(after: comma)...])) { return bytes }
            guard route.hasPrefix("/pdf/api/"), !route.hasPrefix("//"),
                  let url = URL(string: route, relativeTo: base)?.absoluteURL,
                  url.host == base.host, url.port == base.port,
                  ["/pdf/api/card-asset", "/pdf/api/img-proxy", "/pdf/api/page-image"].contains(url.path)
                    || (url.host == base.host && url.port == base.port && url.path.hasPrefix("/pdf/api/asset/")) else {
                throw URLError(.unsupportedURL)
            }
            var request = URLRequest(url: url, cachePolicy: .useProtocolCachePolicy, timeoutInterval: 25)
            request.setValue(referer.absoluteString, forHTTPHeaderField: "Referer")
            let (bytes, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                  (http.mimeType ?? "").hasPrefix("image/"), bytes.count <= 16 * 1_024 * 1_024 else {
                throw URLError(.cannotDecodeContentData)
            }
            guard scope == self.nativeConversation.scope, referer == self.webView.url else { throw CancellationError() }
            return bytes
        }
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
        // 原生正文的实时投影：本地 runtime 每落一笔用户状态就 ping 一下，
        // 原生 PDFKit 视图据此把高亮/墨迹/便签重新投影上去。
        let nativeReadingProjectionMessageProxy =
            WeakScriptMessageHandler(delegate: self)
        self.nativeReadingProjectionMessageProxy = nativeReadingProjectionMessageProxy
        contentController.add(
            nativeReadingProjectionMessageProxy,
            name: nativeReadingProjectionMessageName
        )
        // 原生字符层的定位通道：网页那几个 AI 划线入口在原生接管时问它要坐标，
        // 这样就不必再把目标页在网页里渲出来（那正是双份渲染的最后一处来源）。
        let nativeReaderGeometryMessageProxy =
            WeakScriptMessageHandlerWithReply(delegate: self)
        self.nativeReaderGeometryMessageProxy = nativeReaderGeometryMessageProxy
        contentController.addScriptMessageHandler(
            nativeReaderGeometryMessageProxy,
            contentWorld: .page,
            name: nativeReaderGeometryMessageName
        )
        let nativeDataStoreMessageProxy =
            WeakScriptMessageHandlerWithReply(delegate: self)
        self.nativeDataStoreMessageProxy = nativeDataStoreMessageProxy
        contentController.addScriptMessageHandler(
            nativeDataStoreMessageProxy,
            contentWorld: .page,
            name: nativeDataStoreMessageName
        )
        // 把「用不用本机数据库」这个选择递给页面。
        //
        // ⚠ 库是**启动时**选定的（`createStores()` 只跑一次），所以这个值
        //   必须在 documentStart 就到位，而且翻了要重开阅读器才算数。
        // ⚠ 这里只是「许可」，不是「生效」：网页那边还要确认通道真在、
        //   两个模块都装上了。存储是唯一一类"选错了就把数据弄没"的东西，
        //   宁可不切换，也不要切到一半。
        contentController.addUserScript(WKUserScript(
            source: """
            (() => {
              window.__BW_NATIVE_DATA_STORE__ = true;
              window.__BW_NATIVE_DATA_STORE_REQUIRED__ = true;
            })();
            """,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
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
        contentController.addScriptMessageHandler(
            nativeAnkiMobileMessageProxy, contentWorld: .page, name: nativeAnkiPCMessageName
        )
        if let localRuntimeServer {
            let navigationBridge = ReaderNativePDFNavigationBridge(webView: webView, trustedBaseURL: localRuntimeServer.baseURL)
            navigationBridge.restorePosition = { [weak self] total in
                guard let self, let bookID = self.nativeReadingStoreBookID, bookID == self.currentLocalBook?.id else { return nil }
                let store = try self.nativeDataStoreHost.bridge(for:"bw-reader-native-v1-document").store
                guard var value = try ReaderNativeReadingPosition.restore(store:store,bookID:bookID,total:total) else { return nil }
                if let page = self.nativeRequestedPDFPage {
                    value["page"] = min(total,max(1,page)); value["fraction"] = 0
                }
                return value
            }
            navigationBridge.savePosition = { [weak self] bookID, value in
                guard let self, bookID == self.nativeReadingStoreBookID, bookID == self.currentLocalBook?.id,
                      let deviceID = self.nativeReadingStoreDeviceID else { throw ReaderNativeBookStore.MutationError.unavailable }
                let store = try self.nativeDataStoreHost.bridge(for:"bw-reader-native-v1-document").store
                let writer = ReaderNativeBookStore(store:store,bookID:bookID,deviceID:deviceID,
                    displayName:self.currentLocalBook?.title,contentSHA256:self.currentLocalBookContentSHA256)
                _ = try writer.perform(["bookID":bookID,"mutationId":"position-" + UUID().uuidString,
                    "operation":"pdf-position","value":value])
                self.nativeReplicationService?.wake()
                self.markCloudSyncDirty()
            }
            navigationBridge.reportFailure = { [weak self] message in self?.showTransientNotice("阅读位置未保存：" + message) }
            nativePDFNavigationBridge = navigationBridge
            contentController.addScriptMessageHandler(navigationBridge, contentWorld: .page,
                                                     name: ReaderNativePDFNavigationBridge.messageName)
            let nativeServerGateway = ReaderNativeServerGateway(
                webView: webView,
                trustedBaseURL: localRuntimeServer.baseURL,
                serverProxyBroker: localRuntimeServer.serverProxyBroker
            )
            self.nativeServerGateway = nativeServerGateway
            contentController.addScriptMessageHandler(
                nativeServerGateway,
                contentWorld: .page,
                name: ReaderNativeServerGateway.messageName
            )
            let nativeAssistantStream = ReaderNativeAssistantStreamBridge(webView: webView,
                trustedBaseURL: localRuntimeServer.baseURL, gateway: nativeServerGateway)
            self.nativeAssistantStream = nativeAssistantStream
            contentController.addScriptMessageHandler(nativeAssistantStream, contentWorld: .page,
                                                       name: ReaderNativeAssistantStreamBridge.messageName)
            contentController.addUserScript(WKUserScript(source: ReaderNativeAssistantStreamBridge.script,
                injectionTime: .atDocumentStart, forMainFrameOnly: true))
            let nativeContextSelections = ReaderNativeContextSelectionBridge(webView: webView,
                trustedBaseURL: localRuntimeServer.baseURL)
            self.nativeContextSelections = nativeContextSelections
            contentController.addScriptMessageHandler(nativeContextSelections, contentWorld: .page,
                name: ReaderNativeContextSelectionBridge.messageName)
            contentController.addUserScript(WKUserScript(source: ReaderNativeContextSelectionBridge.script,
                injectionTime: .atDocumentStart, forMainFrameOnly: true))
            let nativeTurns = ReaderNativeTurnBridge(webView:webView,trustedBaseURL:localRuntimeServer.baseURL,gateway:nativeServerGateway)
            nativeTurns.onFailure = { [weak self] message in self?.nativeConversation.report(message) }
            nativeAssistantStream.beforeHistoryClear = { [weak nativeTurns] mode in
                guard let nativeTurns else { throw CancellationError() }
                return try await nativeTurns.beginClear(mode)
            }
            nativeAssistantStream.afterHistoryClear = { [weak nativeTurns] mode, token, cleared in nativeTurns?.endClear(mode,token:token,cleared:cleared) }
            self.nativeTurns = nativeTurns
            contentController.addScriptMessageHandler(nativeTurns,contentWorld:.page,name:ReaderNativeTurnBridge.messageName)
            contentController.addUserScript(WKUserScript(source:ReaderNativeTurnBridge.script,injectionTime:.atDocumentStart,forMainFrameOnly:true))
            let nativeServerSyncBridge = ReaderNativeServerSyncBridge(
                webView: webView,
                trustedBaseURL: localRuntimeServer.baseURL
            )
            self.nativeServerSyncBridge = nativeServerSyncBridge
            contentController.addScriptMessageHandler(
                nativeServerSyncBridge,
                contentWorld: .page,
                name: ReaderNativeServerSyncBridge.messageName
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
            // 顶栏的「书籍」「App 设置」按钮经这里请求原生 sheet。
            // ⚠ 不能再让网页按 URL 导航到 /pdf/ —— takeOverLibraryNavigation 只在
            //   **正在读本机书**且地址是环回 /pdf/ 时才拦截；读 Pi 上的书时它不拦，
            //   于是硬导航跑去打开一个不该存在的网页（用户实测）。产品已本地化，
            //   书架是 SwiftUI，不该有任何一条路径经过网络地址。
            nativeAppPrefsBridge.onOpenLibrary = { [weak self] in
                self?.libraryPresentationRequestID = UUID()
            }
            nativeAppPrefsBridge.onOpenNativeTools = { [weak self] in
                self?.nativeToolsPresentationRequestID = UUID()
            }
            nativeAppPrefsBridge.onOpenVaultPicker = { [weak self] in
                self?.vaultPickerPresentationRequestID = UUID()
            }
            nativeAppPrefsBridge.onOpenRealtimeKey = { [weak self] in
                self?.realtimeKeyPresentationRequestID = UUID()
            }
            nativeAppPrefsBridge.onOpenPiLogin = { [weak self] in
                self?.piLoginPresentationRequestID = UUID()
            }
            // 来源闸：与本机笔记桥同一套双检（isMainFrame + 两侧 URL 都可信）。
            // 不设它的话，允许内嵌的第三方播放器子框也能调下面这批动作。
            nativeAppPrefsBridge.isTrustedFrame = { [weak self] message in
                guard let self else { return false }
                return message.frameInfo.isMainFrame
                    && message.webView === self.webView
                    && self.isTrustedReaderURL(self.webView.url)
                    && self.isTrustedReaderURL(message.frameInfo.request.url)
            }
            nativeAppPrefsBridge.surfacesProvider = {
                ReaderNativeSurfaceState.snapshot()
            }
            nativeAppPrefsBridge.performAction = { action, value in
                ReaderNativeSurfaceState.perform(action, value: value)
            }
            self.nativeAppPrefsBridge = nativeAppPrefsBridge
            contentController.addScriptMessageHandler(
                nativeAppPrefsBridge,
                contentWorld: .page,
                name: ReaderNativeAppPrefsBridge.messageName
            )
            nativeBookOCRUpdateCancellable = NativeBookOCRManager.shared
                .$lastUpdate
                .compactMap { $0 }
                .sink { [weak self, weak nativeBookOCRBridge, weak webView] update in
                    guard let nativeBookOCRBridge, let webView else { return }
                    Task { @MainActor in
                        if let self, update.bookID == self.currentLocalBook?.id {
                            self.cancelAllNativePageWork()
                            self.nativeOverlayGeneration &+= 1
                            self.nativeVocabularyEnrichmentTasks.values.forEach { $0.cancel() }
                            self.nativeVocabularyEnrichmentTasks = [:]
                            self.nativePageTranslationTasks.values.forEach { $0.cancel() }
                            self.nativePageTranslationTasks = [:]
                            self.nativeFigureTasks.values.forEach { $0.cancel() }
                            self.nativeFigureTasks = [:]
                            self.nativeOverlayPages = []
                            self.scheduleNativeInkSurfacePublish()
                        }
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
                //
                // ⚠ 但**原因必须留下来**。这里原先只是置 nil:一旦初始化失败,
                // 整个 App 生命周期里适配器都是 nil,于是**每本书每次都失败**,
                // 而现场只剩一句「尚未准备好」—— 跟"脚本没跑"完全分不开。
                // 这正是 silent-failure-lessons.md 那条:
                // **每个提前退出都要出声**。
                bookUserStateWebAdapter = nil
                bookUserStateCoordinator = nil
                Self.bookUserStateSetupFailure =
                    (error as? LocalizedError)?.errorDescription
                    ?? String(describing: error)
            }
        }
        configureBookUserStateNotifications()
        // 最早的一段脚本：把页面自己的报错记下来。
        //
        // ⚠ 它必须**在别的脚本之前**跑，否则 runtime 抛的错就已经过去了。
        // 这是 silent-failure-lessons.md 那条「无控制台设备上沉默等于不可诊断」
        // 的落点：iPad 上没有控制台，页面抛了什么在设备上是**看不到**的，
        // 所以要在它抛的当下就存下来，等有人来问再报。
        contentController.addUserScript(WKUserScript(
            source: #"""
            (() => {
              if (window.__BW_BOOT_ERRORS__) return;
              const box = [];
              window.__BW_BOOT_ERRORS__ = box;
              const push = (text) => {
                if (box.length >= 8) return;   // 只留最早的几条：真正的病因在最前面
                box.push(String(text).slice(0, 300));
              };
              window.addEventListener("error", (event) => {
                if (event && event.message) {
                  const where = event.filename
                    ? String(event.filename).split("/").pop() : "?";
                  push(where + ":" + (event.lineno || 0) + " " + event.message);
                } else if (event && event.target && event.target.src) {
                  // 资源加载失败（404 / 被 CSP 挡）——它没有 message，
                  // 只能从 target 上认出来。漏掉这一支就会把"脚本没下来"
                  // 误判成"脚本没报错"。
                  push("资源没加载：" +
                    String(event.target.src).split("/").pop());
                }
              }, true);
              window.addEventListener("unhandledrejection", (event) => {
                push("未捕获的 Promise：" +
                  ((event && event.reason && event.reason.message)
                    || String(event && event.reason)));
              });
            })();
            """#,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
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
                value: Object.freeze({ request, ownsExports: true })
              });
              window.dispatchEvent(new CustomEvent(
                "bw-native-anki-mobile-capability",
                { detail: { available: true } }
              ));
              const pc = window.webkit?.messageHandlers?.bwNativeAnkiPC;
              if (pc && typeof pc.postMessage === 'function' && !window.__bwNativeAnkiPC) {
                Object.defineProperty(window, '__bwNativeAnkiPC', {
                  value: Object.freeze({ ownsExports: true, request: payload => pc.postMessage(payload) })
                });
              }
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
                // ⚠ 这里**不再**给 #ep-side 加顶部避让。
                //   上一轮我给它加了 52px，方向错了：App 里侧栏就该置顶（右上角那两枚
                //   原生悬浮钮已经撤掉，不再有东西压着它）；真正需要留距离的是**扩展**
                //   注入到任意网页的那一侧（那里顶部可能有网页自己的 fixed 头部）。
                //   扩展那边的距离由 rc-sidedrawer 自己按环境判断，不在这里管。
                "";
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
        // ── 键盘弹出时别把整页顶上去（用户 2026-09-11 两次实测都还在）──
        //
        // 症状：点侧栏输入框 → **连左边那本书一起**整体上抬，底下露出黑带。
        // 网页那侧修不动它：那不是页面布局，是 WKWebView 自己的 scrollView
        // 被塞了底部 contentInset 并跟着滚了一段。
        // `contentInsetAdjustmentBehavior = .never` 只关掉**安全区**避让，
        // 不关键盘避让 —— 这两件事是分开的，我一度以为设了它就够了。
        //
        // 这个壳的页面是 height:100% 的固定版式，外层 scrollView 本来就不该
        // 滚动；所以键盘出现时把 inset 和偏移都按回零。输入框会不会被键盘
        // 挡住由网页那侧管（按遮挡量垫面板底部），两边各管一件事。
        keyboardInsetObservers.forEach {
            NotificationCenter.default.removeObserver($0)
        }
        keyboardInsetObservers = [
            UIResponder.keyboardWillShowNotification,
            UIResponder.keyboardDidShowNotification,
            UIResponder.keyboardWillChangeFrameNotification,
            UIResponder.keyboardDidChangeFrameNotification,
            UIResponder.keyboardDidHideNotification,
        ].map { name in
            NotificationCenter.default.addObserver(
                forName: name,
                object: nil,
                queue: .main
            ) { [weak webView] _ in
                guard let scroll = webView?.scrollView else { return }
                // 立刻按一次，再在下一拍按一次：键盘是动画出现的，
                // WebKit 会在动画过程中再塞一次 inset。
                Self.flattenKeyboardInset(scroll)
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
                    Self.flattenKeyboardInset(scroll)
                }
            }
        }
        nativeAgentVoice.delegate = self
    }

    /// 把外层 scrollView 的键盘 inset 与偏移按回零。
    ///
    /// ⚠ 只在**真的不为零**时写，别每次通知都无条件赋值 —— 无条件写会跟
    /// WebKit 自己的动画打架，表现是抬一下又落回去的抖动。
    private static func flattenKeyboardInset(_ scroll: UIScrollView) {
        if scroll.contentInset.bottom != 0 {
            scroll.contentInset.bottom = 0
        }
        if scroll.verticalScrollIndicatorInsets.bottom != 0 {
            scroll.verticalScrollIndicatorInsets.bottom = 0
        }
        if scroll.contentOffset.y != 0 {
            scroll.contentOffset.y = 0
        }
    }

    /// Binds the App-owned Pi catalog to the local reading shell. The gateway
    /// receives only digest-verified mappings from the currently open local
    /// library. `scope: current` routes still see one mapping; the bounded set
    /// exists solely for manifest routes whose policy explicitly says catalog.
    func bind(remoteLibrary: ReaderRemoteLibraryCoordinator) {
        guard remoteLibraryCoordinator !== remoteLibrary else { return }
        remoteLibraryCoordinator = remoteLibrary
        nativeServerRemoteLibraryCancellable = Publishers.CombineLatest3(
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
            nativeServerGateway?.updateTrustedRemoteBookBindings(
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
        nativeServerGateway?.updateTrustedRemoteBookBindings(
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
                "无法打开目标书籍：本机书库或服务器书库尚未连接",
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
                        ?? "服务器书库中没有找到目标 PDF/EPUB",
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
        nativePDFNavigationBridge?.flushPendingPosition()
        nativeHTMLNoteActions = [:]; nativeHTMLNoteKey = ""; nativeHTMLNotesEnabled = false; nativeHTMLPinned = []
        nativeConversation.setNativeHTMLNotes(nil)
        nativeInkDocumentToken = UUID().uuidString
        nativeReadingStoreBookID = nil
        nativeReadingStoreDeviceID = nil
        nativeRequestedPDFPage = nil
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
                    "服务器用户数据未导入：\(error.localizedDescription)；重新打开本书可重试",
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
        // ⚠ 这里原来是「没有待导入、也没有重试意图 → 直接返回」。于是**本机已有的书
        //   永远不会去服务器看一眼** —— 只有"刚从远程书库下载"那条路会 staging。
        //   用户 2026-09-19：手机上打开同一本书，钉的卡片都没有。换台设备打开同一本书
        //   正是同步最该起作用的时刻，而它恰恰被这道 guard 挡在门外。
        try await waitForBookUserStateAPI(
            localBookId: localBook.id,
            generation: generation
        )
        let digest = try await currentLocalContentDigest(
            localBookId: localBook.id,
            generation: generation
        )

        if pending == nil, fetchIntent == nil {
            // 机会性拉取：服务器上可能有别的设备推过的一份。
            // ⚠ 这一步**故意不出声**：绝大多数书在服务器上本来就没有状态包，
            //   每次开书都提示一句"没有"纯属噪音。真拉到了才说话（见下面的 notice）。
            //   ——「静默」在这里是有边界的：失败不改变任何本机数据，也不影响开书。
            if let payload = try? await ReaderServerLibrary.userStatePayload(
                contentSha256: digest
            ), let package = try? JSONDecoder().decode(
                ReaderBookUserStatePackage.self, from: payload.packageData
            ) {
                try? await pendingBookUserStateStore.stage(
                    payload: payload,
                    localBookId: localBook.id,
                    remoteBookId: package.bookId,
                    contentSha256: digest
                )
                pending = try await pendingBookUserStateStore.load(
                    localBookId: localBook.id
                )
                if pending != nil {
                    showBookUserStateMessage(
                        "服务器上有这本书的最新数据，正在合并", isError: false)
                }
            }
        }
        guard pending != nil || fetchIntent != nil else { return }

        if pending == nil, let fetch = fetchIntent {
            guard fetch.contentSha256 == digest else {
                throw ReaderBookUserStatePendingImportError
                    .contentVersionMismatch
            }
            do {
                // 2026-09-19 换源 → Windows 桥（三处取包必须同源，见 staging 与提交前复核）。
                let payload = try await ReaderServerLibrary.userStatePayload(
                    contentSha256: fetch.contentSha256
                )
                guard let payload else {
                    try await pendingBookUserStateStore
                        .removeFetchIntent(fetch)
                    showBookUserStateMessage(
                        "服务器上这本书暂无用户附属数据；本机内容未改变",
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
        // ⚠ 这四个条件**必须分开报**。
        //
        // 它们此前挤在一个 guard 里共用一句「尚未准备好」——
        // 2026-08-28 就栽在这:我给等待循环加了详细诊断,结果横幅一字未变,
        // 因为真正抛错的是**这里**,而这里抛的是光秃秃的 .unavailable。
        // 「加了诊断却什么都没变」本身花掉了一整个 TestFlight 来回。
        //
        // 而且这四条指向完全不同的下一步：
        //   前两条 = 期间换了书/换了上下文（正常，重开即可）
        //   后两条 = **App 启动时就初始化失败了**，它不会自愈，
        //            所以每本书每次都失败 —— 正是本次的现象。
        guard generation == bookUserStateContextGeneration else {
            throw ReaderBookUserStateWebAdapterError
                .unavailableDetailed("期间切换过阅读上下文")
        }
        guard currentLocalBook?.id == pending.localBookId else {
            throw ReaderBookUserStateWebAdapterError.unavailableDetailed(
                "当前书跟暂存包对不上（当前 "
                + (currentLocalBook?.id ?? "无")
                + "，包里 " + pending.localBookId + "）")
        }
        guard let coordinator = bookUserStateCoordinator else {
            throw ReaderBookUserStateWebAdapterError.unavailableDetailed(
                "打包协调器没建起来"
                + (Self.bookUserStateSetupFailure.map { "：" + $0 }
                   ?? "（启动时没记下原因）"))
        }
        guard let adapter = bookUserStateWebAdapter else {
            throw ReaderBookUserStateWebAdapterError.unavailableDetailed(
                "写入适配器没建起来"
                + (Self.bookUserStateSetupFailure.map { "：" + $0 }
                   ?? "（启动时没记下原因）"))
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
        // 2026-09-19 换源：与 staging 同源（Windows 桥）。
        // ⚠ 这一步的意义是"提交前再确认一次这份数据属于谁"，所以它**必须和取包那次同源** ——
        //   一处拉桥、一处拉 Pi，作用域摘要永远对不上，导入会稳定失败。桥是本机服务，
        //   没有 cookie 概念，所以原来那道"没登录就不让导"的闸在这条路上不适用。
        let current: ReaderBookUserStateRemotePayload
        do {
            guard let payload = try await ReaderServerLibrary.userStatePayload(
                contentSha256: pending.contentSha256
            ) else {
                throw ReaderBookUserStatePendingImportError
                    .accountScopeUnavailable
            }
            current = payload
        } catch ReaderServerLibrary.Failure.capabilityMissing {
            // 桥在，但还没有这个端点（旧版）。这跟"没鉴权"是两件事，别混。
            throw ReaderBookUserStatePendingImportError
                .accountScopeUnavailable
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
        // ⚠ 探针返回的是**原始事实**,不是一个 Bool。
        //
        // 原来这里把一切压成 `ready: Bool`,于是超时后四种完全不同的病因
        // ——脚本没跑 / 跑了但半路抛了 / 页面不是本机壳 / 在子框里——
        // 只剩同一句「尚未准备好」。分不开就只能猜,而这次是"每次都失败",
        // 猜错一轮就是一个 TestFlight 来回。
        //
        // 决定性的那一条是 `runtimeRoot`:`window.BWReaderRuntime` 在
        // native-local-runtime.js **第 63 行**就建好了,而 `nativeLocalRuntime`
        // 在**末尾第 14138 行**才挂上。所以:
        //   runtimeRoot 有、nativeLocalRuntime 没有 → **脚本跑了但中途抛了**
        //   两个都没有                             → **脚本根本没跑**
        var lastObserved = "没拿到现场"
        for _ in 0..<40 {
            try Task.checkCancellation()
            guard generation == bookUserStateContextGeneration,
                  currentLocalBook?.id == localBookId else {
                throw CancellationError()
            }
            let trustedURL = isLocalRuntimeURL(webView.url)
            if trustedURL,
               let raw = try? await webView.callAsyncJavaScript(
                """
                const rt = window.BWReaderRuntime;
                const nlr = rt && rt.nativeLocalRuntime;
                const us = nlr && nlr.bookUserState;
                return {
                  isTop: window.top === window,
                  hasRuntimeRoot: Boolean(rt),
                  hasNativeRuntime: Boolean(nlr),
                  hasUserState: Boolean(us),
                  snapshotFn: us ? typeof us.snapshotHeaders : "none",
                  applyFn: us ? typeof us.applyAtomically : "none",
                  scriptTag: Boolean(document.querySelector(
                    'script[src*="native-local-runtime"]')),
                  bootErrors: (window.__BW_BOOT_ERRORS__ || [])
                    .slice(0, 2).join(" | ").slice(0, 240),
                  path: String(location.pathname).slice(-40)
                };
                """,
                arguments: [:],
                in: nil,
                contentWorld: .page
               ) as? [String: Any] {
                let ready = (raw["isTop"] as? Bool ?? false)
                    && (raw["snapshotFn"] as? String) == "function"
                    && (raw["applyFn"] as? String) == "function"
                if ready { return }
                lastObserved = Self.describeUserStateProbe(raw)
            } else if !trustedURL {
                lastObserved = "当前页不是本机阅读页（"
                    + (webView.url?.path.suffix(40).description ?? "无 URL") + "）"
            } else {
                lastObserved = "页面里的探针没能执行"
            }
            try await Task.sleep(nanoseconds: 250_000_000)
        }
        // 初始化那一步就失败的话，页面再正常也没用 —— 优先报它。
        if let setup = Self.bookUserStateSetupFailure {
            throw ReaderBookUserStateWebAdapterError
                .unavailableDetailed("App 侧初始化就失败了：" + setup)
        }
        throw ReaderBookUserStateWebAdapterError
            .unavailableDetailed(lastObserved)
    }

    /// 把探针的原始事实翻成**指向病因**的一句话。
    ///
    /// ⚠ 不做"归纳"——每一支都对应一个不同的下一步动作，混起来就白探了。
    private static func describeUserStateProbe(_ raw: [String: Any]) -> String {
        let hasRoot = raw["hasRuntimeRoot"] as? Bool ?? false
        let hasNative = raw["hasNativeRuntime"] as? Bool ?? false
        let scriptTag = raw["scriptTag"] as? Bool ?? false
        let bootErrors = (raw["bootErrors"] as? String) ?? ""
        let path = (raw["path"] as? String) ?? "?"
        if !(raw["isTop"] as? Bool ?? false) {
            return "阅读页在子框里，主框没有这个接口"
        }
        if !scriptTag {
            return "壳页面(\(path))里根本没有 runtime 脚本标签"
        }
        if !hasRoot {
            return "脚本标签在但一行都没执行"
                + (bootErrors.isEmpty ? "（可能被 CSP 挡了或 404）"
                                      : "：\(bootErrors)")
        }
        if !hasNative {
            return "脚本跑到一半抛了"
                + (bootErrors.isEmpty ? "（没抓到报错）" : "：\(bootErrors)")
        }
        let snapshotFn = (raw["snapshotFn"] as? String) ?? "?"
        let applyFn = (raw["applyFn"] as? String) ?? "?"
        return "接口在但方法不全（snapshot=\(snapshotFn)，apply=\(applyFn)）"
    }

    /// App 启动时适配器初始化失败的原因。整个进程共用一份 ——
    /// 它一旦失败就**不会**自己恢复，所以必须能一直报出来。
    private static var bookUserStateSetupFailure: String?

    /// 这次跑的是哪个 build。用来分辨「诊断没生效」和「新版没装上」。
    static let bundleBuildNumber =
        Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion")
            as? String ?? "?"

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
        // ⚠ 真实改页（插入页写回 PDF 文件）会换掉内容摘要。而原生正文的每一条路
        //   —— 投影、划线、定位、墨迹表面 —— 都 `guard document.matches(digest)`，
        //   摘要一变它们会**静默全停**：屏幕上还是旧的那一份，却再也不更新。
        //   所以摘要变了就重挂一次。
        remountNativePDFIfContentChanged(digest)
        // 这本书不在前台时，别的设备的改动被引擎存进了待处理区 —— 现在它打开了，
        // 合掉它们。⚠ 不做这一步的话那些改动会一直躺着，表现是"另一台设备上做的
        // 批注过很久才出现，或者根本不出现"。
        if let cloudSync {
            Task { await cloudSync.drainPending(contentSHA256: digest) }
        }
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
        // Set before the first suspension; native ink cannot enter while PDF
        // page indexes and sidecars are being changed by another command.
        nativePDFMutationCommandDepth += 1
        defer { nativePDFMutationCommandDepth -= 1 }
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
        parts.append(imported > 0 ? "已合并 \(imported) 类服务器用户数据" : "服务器用户数据已核对")
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
        // ⚠ 横幅带上 build 号。
        //
        // 2026-08-28 的教训:我加了诊断、发了新版,横幅**一字未变** ——
        // 那一刻有两种解释:「诊断没打到点上」和「新版根本没装上」,
        // 而它们指向完全相反的下一步。当时分不开,白花了一个来回。
        // 一个 build 号就能永久消灭这个歧义,代价是几个字符。
        let payload: [String: Any] = [
            "message": "[b\(Self.bundleBuildNumber)] "
                + String(message.prefix(1_000)),
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
                        // 90s：这不是"页面该多快"的预算，是"永远没信号"的兜底。
                        // 15s 时被实锤误报过（2026-08-25）：升级重启后首开要做
                        // 改页事务恢复 + OCR 状态重建 + 93MB PDF 首渲，超过 15s
                        // 就弹"上次阅读的书无法打开"并清掉记忆——书其实正在打开。
                        // 真正打不开有自己的失败信号（recordLoadFailure / catch），
                        // 不靠这个计时器。
                        try await Task.sleep(nanoseconds: 90_000_000_000)
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
        nativePDFNavigationBridge?.flushPendingPosition()
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
            nativeServerGateway?.updateTrustedRemoteBookBinding(nil)
            didClearOutgoingRemoteBinding = true

            var openingBook = library.books.first(where: {
                $0.id == book.id && $0.relativePath == book.relativePath
            }) ?? book
            var access = try library.makeOpenAccess(for: openingBook)
            // 打开前对账磁盘：真实改页/事务恢复会在会话间改写 PDF 文件，
            // 索引缓存滞后一拍就会让 open 与后续所有 validateCurrentFile
            // 连锁拒绝（BW_LOCAL_BOOK_CHANGED，2026-08-25 手动打开也中招）。
            // 记录过期时重扫一次并重取记录，而不是把陈旧的 access 一路
            // 带进 settle / runtime boot。
            if (try? access.validateCurrentFile(
                maximumEPUBBytes: Int64.max
            )) == nil {
                await library.rescan()
                for _ in 0..<100 where library.isScanning {
                    try await Task.sleep(nanoseconds: 100_000_000)
                }
                openingBook = library.books.first(where: {
                    $0.id == book.id
                        && $0.relativePath == book.relativePath
                }) ?? openingBook
                access = try library.makeOpenAccess(for: openingBook)
            }
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
            nativeRequestedPDFPage = initialPage
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
            lastLocalBookOpenFailure = nil
            return true
        } catch {
            if didClearOutgoingRemoteBinding {
                nativeServerGateway?.updateTrustedRemoteBookBinding(nil)
            }
            // 启动自动恢复失败时这是唯一的真因记录 —— 恢复发生在书库 sheet
            // 弹出之前，横幅只能靠它转述（2026-08-25 实锤：没有它，用户只能
            // 看到一句猜测性的"可能已移动或权限失效"，连续排查三轮都在猜）。
            lastLocalBookOpenFailure = error.localizedDescription
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

    /// `.inactive` 的宽限期。
    ///
    /// iOS 在**用户根本没离开 App** 的时候也会给 `.inactive`：下拉通知中心、
    /// 上滑控制中心、进 App 切换器、系统权限弹窗、多任务尺寸变化 —— 这些
    /// 大多在一两秒内就回到 `.active`。
    ///
    /// 而这里原来 `.inactive` 与 `.background` 走同一条路、**零缓冲**地
    /// 拆掉上下文 WSS，于是"稍微碰一下就什么都做不了"（用户 2026-08-23 原话）。
    ///
    /// ⚠ 服务端不需要跟着改：Direct 对 context-only 阶段**本来就没有空闲期限**
    /// （DirectConnectionPhaseDeadline.ContextOnly => null），断开完全是客户端
    /// 自己的策略。所以这一刀只在客户端，代价可控。
    ///
    /// 真进后台（`.background`）仍然立刻断 —— iOS 会挂起进程，留着也没用，
    /// 而且那才是"用户确实离开了"的可靠信号。
    private static let readerInactiveGrace: TimeInterval = 12

    /// 把当前这本书的状态包发布到 Windows 桥，供别的设备拉取（2026-09-19）。
    ///
    /// ⚠ 在此之前**这条链路只有入口没有出口**：App 能拉能导入，却没有任何一处把本机
    ///   状态发布出去 —— 所以「多端同步」实际是单向的，换台设备打开同一本书什么都没有。
    /// ⚠ 只在内容真的变过时才发：导出要在页面里跑 JS 并算各域摘要，白发一次不便宜；
    ///   摘要串没变就直接跳过。
    /// ⚠ 失败**不打断任何东西**：它是旁路。但也不静默 —— 记一条给排查用。
    func publishBookUserStateSnapshot() {
        guard let localBook = currentLocalBook,
              let contentSha = currentLocalBookContentSHA256,
              let adapter = bookUserStateWebAdapter else { return }
        let generation = bookUserStateContextGeneration
        Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                let domains = try await adapter.exportPackage(
                    localBookId: localBook.id
                )
                guard generation == self.bookUserStateContextGeneration else { return }
                let fingerprint = domains
                    .sorted { $0.name.rawValue < $1.name.rawValue }
                    .map { $0.name.rawValue + ":" + $0.digest }
                    .joined(separator: "|")
                guard fingerprint != self.lastPublishedUserStateFingerprint else { return }
                let package = ReaderBookUserStatePackage(
                    contract: ReaderBookUserStatePackage.currentContract,
                    bookId: localBook.id,
                    contentSha256: contentSha,
                    revision: Int64(Date().timeIntervalSince1970 * 1000),
                    updatedAt: ISO8601DateFormatter().string(from: Date()),
                    domains: domains
                )
                try await ReaderServerLibrary.publishUserState(package)
                self.lastPublishedUserStateFingerprint = fingerprint
            } catch {
                self.showBookUserStateMessage(
                    "本机数据没能同步到服务器：\(error.localizedDescription)",
                    isError: true
                )
            }
        }
    }

    func setReaderScenePhase(_ phase: ScenePhase) {
        switch phase {
        case .background:
            readerWasBackgrounded = true
            cancelReaderInactiveGrace()
            // 出口（2026-09-19）：离开前把这本书的状态发布出去，别的设备才拉得到。
            // ⚠ 放在 setReaderForeground 之前 —— 那一步会停掉本机 runtime，
            //   而导出要在页面里执行 JS，runtime 停了就取不到了。
            publishBookUserStateSnapshot()
            setReaderForeground(false, restartLocalRuntime: false)
        case .inactive:
            // 先不动连接，给一个宽限期；期间回到 .active 就当什么都没发生。
            scheduleReaderInactiveGrace()
        case .active:
            cancelReaderInactiveGrace()
            let shouldRestart = readerWasBackgrounded
            readerWasBackgrounded = false
            setReaderForeground(true, restartLocalRuntime: shouldRestart)
        @unknown default:
            cancelReaderInactiveGrace()
            setReaderForeground(false, restartLocalRuntime: false)
        }
    }

    private func scheduleReaderInactiveGrace() {
        cancelReaderInactiveGrace()
        readerInactiveGraceTask = Task { [weak self] in
            try? await Task.sleep(
                nanoseconds: UInt64(Self.readerInactiveGrace * 1_000_000_000))
            guard !Task.isCancelled else { return }
            // 类已是 @MainActor，Task 继承 actor 上下文 —— 不需要再 MainActor.run
            guard let self else { return }
            self.readerInactiveGraceTask = nil
            self.setReaderForeground(false, restartLocalRuntime: false)
        }
    }

    private func cancelReaderInactiveGrace() {
        readerInactiveGraceTask?.cancel()
        readerInactiveGraceTask = nil
    }

    private var deviceLocationPushWired = false
    private func wireDeviceLocationPushIfNeeded() {
        guard !deviceLocationPushWired else { return }
        deviceLocationPushWired = true
        ReaderLocationProvider.shared.onUpdate = { [weak self] snapshot in
            self?.pushDeviceLocationToPage(snapshot)
        }
        if let latest = ReaderLocationProvider.shared.latest {
            pushDeviceLocationToPage(latest)
        }
        // 首次接线顺带取一次定位:开关已开而本会话还没定位过的场景
        // (刚打开 App 直接读书)不该等到下一次后台往返。
        ReaderLocationProvider.shared.refresh()
        // 攒在设备上的后台定位借这次机会补送 —— 它们多半是电脑
        // 睡着时留下的，而那是最常见的状况。
        ReaderLocationProvider.shared.flushPendingFixes()
    }

    private func pushDeviceLocationToPage(_ snapshot: [String: Any]) {
        guard isLocalRuntimeURL(webView.url),
              let data = try? JSONSerialization.data(withJSONObject: snapshot),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript(
            "window.__BW_DEVICE_LOCATION__ = \(json); undefined;",
            completionHandler: nil
        )
    }

    func setReaderForeground(
        _ foreground: Bool,
        restartLocalRuntime: Bool = false
    ) {
        let wasForeground = readerForeground
        if !foreground { nativePDFNavigationBridge?.flushPendingPosition() }
        readerForeground = foreground
        nativeReplicationService?.setActive(foreground)
        if foreground { scheduleNativeAnkiPCRetry(); nativePhraseService?.wake() }
        if foreground {
            // 接线必须无条件(幂等):readerForeground 初始值就是 true,
            // 依赖"后台→前台转换"意味着首启动直读的会话永远接不上线,
            // 位置推不进页面、dwell 永远不带 loc(2026-08-25 实锤:设置
            // 显示定位正常而快照始终未知)。
            wireDeviceLocationPushIfNeeded()
        }
        if foreground, !wasForeground {
            // 地点维度:进前台取一次定位(开关关着时 refresh 是空操作)。
            ReaderLocationProvider.shared.refresh()
        // 攒在设备上的后台定位借这次机会补送 —— 它们多半是电脑
        // 睡着时留下的，而那是最常见的状况。
        ReaderLocationProvider.shared.flushPendingFixes()
        }
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
            scheduleNativeAnkiMobileExpiry()
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

    /// 把网页输入框里打的字交给原生桥，送进正在进行的那通语音。
    private func sendNativeComputerVoiceTyped(_ text: String) {
        guard let bridge = nativeVoiceBridge else { return }
        Task { @MainActor [weak bridge] in
            guard let bridge else { return }
            _ = await bridge.sendTyped(text)
        }
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

    private func nativeAnkiPCOwner() throws -> ReaderNativeAnkiPC {
        let repository = try nativeAnkiMobileOwner().repository
        let owner = ReaderNativeAnkiPC(repository: repository)
        if !nativeAnkiPCRecovered {
            for record in try owner.recoverInterrupted() { acceptNativeAnkiReceipt(record) }
            nativeAnkiPCRecovered = true
        }
        return owner
    }
    private func exportNativeAnkiPC(gid: String, index: Int) async -> [String: Any] {
        let key = gid + ":" + String(index)
        if let active = nativeAnkiPCTasks[key] { return await active.value }
        let task = Task { @MainActor [weak self] () -> [String: Any] in
            guard let self else { return ["ok": false, "error": "阅读器已关闭"] }
            do {
                let owner = try self.nativeAnkiPCOwner()
                let attempt = try owner.prepare(gid: gid, index: index, render: ReaderNativeAnkiProjection.html)
                self.acceptNativeAnkiReceipt(attempt.record)
                let socket = DirectVoiceSocket(configuration: .readerContext) { _ in }
                var sent = false
                do {
                    let fields = try JSONDecoder().decode(DirectJSONValue.self, from: attempt.request).objectValue!
                    _ = try await socket.openReaderContext()
                    sent = true
                    let result = try await socket.requestReaderAnki(fields: fields)
                    await socket.disconnect()
                    self.acceptNativeAnkiReceipt(try owner.settle(attempt, result: JSONEncoder().encode(result)))
                    return ["ok": true, "status": "succeeded"]
                } catch {
                    await socket.disconnect()
                    let code = (error as? DirectVoiceFailure)?.code ?? (error as? ReaderNativeAnkiPC.Failure)?.code ?? ""
                    do { self.acceptNativeAnkiReceipt(try owner.settle(attempt, result: nil, errorCode: code, message: error.localizedDescription, sent: sent)) }
                    catch { self.postClientLog("Anki 回执尚未保存，保留待核对状态：" + error.localizedDescription) }
                    return ["ok": false, "error": error.localizedDescription, "code": code]
                }
            } catch { return ["ok": false, "error": error.localizedDescription] }
        }
        nativeAnkiPCTasks[key] = task
        let result = await task.value
        nativeAnkiPCTasks.removeValue(forKey: key)
        return result
    }
    private func scheduleNativeAnkiPCRetry() {
        guard readerForeground, nativeAnkiPCRetryTask == nil, nativeReadingStoreDeviceID != nil else { return }
        nativeAnkiPCRetryTask = Task { @MainActor [weak self] in
            guard let self else { return }
            defer { self.nativeAnkiPCRetryTask = nil }
            do {
                try await Task.sleep(for: .milliseconds(1200))
                let owner = try self.nativeAnkiPCOwner()
                for group in try owner.repository.snapshot() where self.readerForeground && !Task.isCancelled {
                    guard group["deleted"] as? Bool != true, let gid = group["gid"] as? String else { continue }
                    for (key, value) in group["states"] as? [String: Any] ?? [:] {
                        guard self.readerForeground, let index = Int(key), let state = value as? [String: Any], state["removed"] as? Bool != true,
                              state["phase"] as? String == "confirmed", ReaderNativeAnkiPC.receipt(group, index: index)["status"] as? String == "failed" else { continue }
                        _ = await self.exportNativeAnkiPC(gid: gid, index: index)
                    }
                }
            } catch { /* Next foreground or connection recovery wakes the retained failures. */ }
        }
    }
    private func nativeAnkiMobileOwner() throws -> ReaderNativeAnkiMobile {
        guard let deviceID = nativeReadingStoreDeviceID else { throw ReaderNativeAnkiMobile.Failure(message: "本地卡库尚未就绪") }
        let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-global").store
        guard try store.meta("legacyImport") == "done" else { throw ReaderNativeAnkiMobile.Failure(message: "本地卡库仍在迁移") }
        return ReaderNativeAnkiMobile(repository: ReaderNativeCardRepository(store: store, deviceID: deviceID))
    }
    private func acceptNativeAnkiReceipt(_ record: [String: Any]) {
        nativeConversation.acceptCardRecord(record)
        markCloudSyncDirty()
        // Compatibility observers read the committed receipt only; no launch
        // or persistence operation is replayed by this notification.
        webView.callAsyncJavaScript("window.RC?.flashcard?.acceptNativeRecord(record,index,'receipt'); window.__bwNativeConversation?.snapshot?.();",
            arguments: ["record": record, "index": -1], in: nil, in: .page, completionHandler: nil)
    }
    private func scheduleNativeAnkiMobileExpiry() {
        nativeAnkiMobileExpiryTask?.cancel(); nativeAnkiMobileExpiryTask = nil
        do {
            let owner = try nativeAnkiMobileOwner(), entries = try owner.pending()
            let now = Int64(Date().timeIntervalSince1970 * 1000)
            var deadline: Int64?
            for entry in entries {
                if entry.expiresAt <= now {
                    do { if let record = try owner.expire(entry) { acceptNativeAnkiReceipt(record) } }
                    catch { deadline = min(deadline ?? Int64.max, now + 30_000); postClientLog("AnkiMobile 过期回执待重试：" + error.localizedDescription) }
                } else { deadline = min(deadline ?? Int64.max, entry.expiresAt) }
            }
            guard let deadline else { return }
            nativeAnkiMobileExpiryTask = Task { @MainActor [weak self] in
                do { try await Task.sleep(for: .milliseconds(max(1, deadline - now))) } catch { return }
                self?.scheduleNativeAnkiMobileExpiry()
            }
        } catch { /* Database readiness schedules this again after startup. */ }
    }
    private func exportNativeAnkiMobile(gid: String, index: Int) async -> [String: Any] {
        do {
            let owner = try nativeAnkiMobileOwner(), prepared = try owner.prepare(gid: gid, index: index)
            acceptNativeAnkiReceipt(prepared.record); scheduleNativeAnkiMobileExpiry()
            let response: [String: Any]
            do {
                response = try await withCheckedThrowingContinuation { continuation in
                    handleNativeAnkiMobileRequest(prepared.request) { value, error in
                        if let error { continuation.resume(throwing: ReaderNativeAnkiMobile.Failure(message: error)) }
                        else { continuation.resume(returning: value as? [String: Any] ?? [:]) }
                    }
                }
            } catch {
                acceptNativeAnkiReceipt(try owner.didNotOpen(prepared.pending, message: error.localizedDescription))
                scheduleNativeAnkiMobileExpiry(); throw error
            }
            guard response["ok"] as? Bool == true, response["opened"] as? Bool == true else {
                let message = response["error"] as? String ?? "AnkiMobile 未安装或无法打开"
                acceptNativeAnkiReceipt(try owner.didNotOpen(prepared.pending, message: message))
                scheduleNativeAnkiMobileExpiry(); throw ReaderNativeAnkiMobile.Failure(message: message)
            }
            return ["ok": true, "status": "pending", "gid": gid, "index": index, "callbackExpected": true]
        } catch { return ["ok": false, "error": error.localizedDescription] }
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
        if action == "exportCard" {
            guard Set(body.keys) == Set(["action", "gid", "index"]), let gid = body["gid"] as? String,
                  let index = body["index"] as? Int, (0...255).contains(index), isValidAnkiMobileGID(gid) else {
                replyHandler(nil, "AnkiMobile 导出参数无效"); return
            }
            Task { @MainActor [weak self] in
                guard let self else { replyHandler(nil, "阅读器已关闭"); return }
                replyHandler(await self.exportNativeAnkiMobile(gid: gid, index: index), nil)
            }
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
        do {
            let owner = try nativeAnkiMobileOwner()
            guard let group = try owner.repository.load(gid), let pending = ReaderNativeAnkiMobile.pending(group, index: index),
                  pending.nonce == nonce, pending.expiresAt == expiresAtNumber.int64Value,
                  let cards = group["cards"] as? [[String: Any]], cards.indices.contains(index),
                  try ReaderNativeAnkiMobile.addURL(gid: gid, index: index, card: cards[index], nonce: nonce) == rawURL else {
                throw ReaderNativeAnkiMobile.Failure(message: "外部打开请求与本地待发卡片不一致")
            }
        } catch { replyHandler(nil, error.localizedDescription); return }
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
            pending.delivering = true
            pendingAnkiMobileExports[nonce] = pending
            Task { @MainActor [weak self] in
                guard let self else { return }
                var durable = false
                do {
                    let owner = try self.nativeAnkiMobileOwner()
                    let record = try owner.confirm(gid: pending.gid, index: pending.index, nonce: nonce)
                    self.acceptNativeAnkiReceipt(record)
                    self.scheduleNativeAnkiMobileExpiry()
                    durable = true
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
        if message.name == nativeConversationMessageName {
            guard message.frameInfo.isMainFrame,
                  message.webView === webView,
                  isTrustedReaderURL(webView.url),
                  isTrustedReaderURL(message.frameInfo.request.url),
                  let body = message.body as? [String: Any],
                  body["version"] as? Int == 1 else { return }
            if body["type"] as? String == "figure-consumed" {
                guard body["file"] as? String == "localbook:" + nativeFigures.bookID,
                      let epoch = body["epoch"] as? String, let tokens = body["tokens"] as? [String],
                      tokens.count <= 128, tokens.allSatisfy({ $0.count <= 64 }) else { return }
                if nativeFigures.consume(epoch: epoch, tokens: tokens) {
                    if let document = nativePDFDocument {
                        for page in document.figures.keys { displayNativeFigures(page: page, document: document) }
                    }
                    publishNativeFigureProjection()
                }
                return
            }
            nativeConversation.receive(body)
            if nativeHTMLNotesEnabled, let values = body["nativePinnedCards"] as? [String], Set(values) != nativeHTMLPinned {
                nativeHTMLPinned = Set(values)
                publishNativeHTMLNotes()
            }
        } else if message.name == nativeComputerVoiceMessageName {
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
            // 打字直达通话（用户 2026-09-11）：与 toggle 同一条消息通道，
            // 但**字段集各自精确**——沿用这里原有的"数清字段个数"风格，
            // 多一个少一个都不受理。
            //
            // ⚠ App 上这条必须走原生：网页那套 DirectSocket 在 App 里没有
            // session（状态是原生推进去的），JS 直接发会静静失败，表现是
            // "输入框绿了、回答却来自文字助手"。
            if
                message.frameInfo.isMainFrame,
                message.webView === webView,
                isTrustedReaderURL(webView.url),
                isTrustedReaderURL(message.frameInfo.request.url),
                let typedBody = message.body as? [String: Any],
                typedBody["action"] as? String == "type",
                typedBody.count == 2,
                let typedText = typedBody["text"] as? String,
                !typedText.isEmpty,
                typedText.count <= 4000
            {
                sendNativeComputerVoiceTyped(typedText)
                return
            }
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
            if nativePDFDocument == nil || nativeReadingStoreBookID == nil {
                nativePencilInk.updateLayout(from: body)
            }
        } else if message.name == nativeReadingProjectionMessageName {
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                isTrustedReaderURL(webView.url),
                isTrustedReaderURL(message.frameInfo.request.url),
                let body = message.body as? [String: Any],
                body["type"] as? String == "user-state-written"
            else {
                return
            }
            scheduleNativePDFProjectionRefresh()
            // 同一条信号也是「这本书的用户状态变了」—— 它钩在 withNativePDFWriter
            // 的成功分支上，是 App 所有 PDF 用户状态写入的唯一咽喉。
            markCloudSyncDirty()
        }
    }
}

extension ReaderWebViewModel: WKScriptMessageHandlerWithReply {
    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        if message.name == nativeAnkiPCMessageName {
            guard message.frameInfo.isMainFrame, message.webView === webView, isTrustedReaderURL(webView.url),
                  isTrustedReaderURL(message.frameInfo.request.url), let body = message.body as? [String: Any] else {
                replyHandler(nil, "Anki 导出来源无效"); return
            }
            if body["action"] as? String == "retry", body.count == 1 { scheduleNativeAnkiPCRetry(); replyHandler(["ok": true], nil); return }
            guard body["action"] as? String == "exportCard", Set(body.keys) == Set(["action", "gid", "index"]),
                  let gid = body["gid"] as? String, isValidAnkiMobileGID(gid), let index = body["index"] as? Int, (0...19).contains(index) else {
                replyHandler(nil, "Anki 导出参数无效"); return
            }
            Task { @MainActor [weak self] in
                guard let self else { replyHandler(nil, "阅读器已关闭"); return }
                replyHandler(await self.exportNativeAnkiPC(gid: gid, index: index), nil)
            }
            return
        }
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
        if message.name == nativeDataStoreMessageName {
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                isTrustedReaderURL(webView.url),
                isTrustedReaderURL(message.frameInfo.request.url),
                let body = message.body as? [String: Any]
            else {
                // ⚠ 来源不可信时**不能**回一个"空结果" —— 那会被当成"库里没有
                //   这条记录"，于是调用方以为数据不存在。必须是错误。
                replyHandler(nil, "数据库请求来源无效")
                return
            }
            if body["action"] as? String == "readingStoreReady" {
                guard let bookID = body["bookID"] as? String, bookID == currentLocalBook?.id,
                      let deviceID = body["deviceID"] as? String, !deviceID.isEmpty,
                      deviceID.utf16.count <= 240 else {
                    replyHandler(nil, "数据库所属书籍已切换")
                    return
                }
                do {
                    let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
                    guard try store.meta("legacyImport") == "done" else {
                        throw ReaderBookUserStateWebAdapterError.unavailable
                    }
                    nativeReadingStoreBookID = bookID
                    nativeReadingStoreDeviceID = deviceID
                    try prepareNativePhraseService(deviceID: deviceID)
                    scheduleNativeAnkiPCRetry()
                    scheduleNativeAnkiMobileExpiry()
                    deliverPendingAnkiMobileCallbacks()
                    if nativeReplicationService == nil {
                        nativeReplicationService = ReaderNativeReplicationService(store:store,report:{ [weak self] message in
                            self?.postClientLog("原生复制队列：" + message)
                        },settled:{ [weak self] in
                            self?.webView.evaluateJavaScript("window.dispatchEvent(new Event('bw:native-outbox-drained'));",completionHandler:nil)
                        })
                    }
                    nativeReplicationService?.setActive(readerForeground)
                    publishNativeInkSurfaces()
                    publishNativeHTMLNotes()
                    // Resume persisted pending pages, including books closed
                    // before the quiet period elapsed.
                    let pending = try store.records(collection:"native-ink-pending",idPrefix:"")
                    for row in pending where !row.deleted && row.id.hasSuffix(":ink-pending") {
                        let pendingBookID = String(row.id.dropLast(":ink-pending".count))
                        resumeNativeInkSync(ReaderNativeBookStore(store:store,bookID:pendingBookID,deviceID:deviceID,
                            displayName:pendingBookID == bookID ? currentLocalBook?.title : nil,
                            contentSHA256:pendingBookID == bookID ? currentLocalBookContentSHA256 : nil))
                    }
                    replyHandler(["ok": true, "nativeBookWrites": true, "nativeReplicationTransport":true, "nativePhrases": true], nil)
                } catch { replyHandler(nil, error.localizedDescription) }
                return
            }
            if body["action"] as? String == "replicationWake" {
                guard nativeReadingStoreBookID == currentLocalBook?.id, nativeReplicationService != nil else {
                    replyHandler(nil,"原生复制队列尚未准备好"); return
                }
                nativeReplicationService?.wake()
                replyHandler(["ok":true],nil)
                return
            }
            if body["action"] as? String == "phrases" {
                guard let service = nativePhraseService, service.deviceID == nativeReadingStoreDeviceID,
                      nativeReadingStoreBookID == currentLocalBook?.id,
                      let operation = body["operation"] as? String, ["read", "set"].contains(operation) else {
                    replyHandler(nil, "词组库尚未就绪"); return
                }
                Task { @MainActor in
                    do {
                        if operation == "read" { replyHandler(try await service.read(), nil) }
                        else {
                            guard let text = body["text"] as? String, let enabled = body["enabled"] as? Bool else {
                                throw ReaderNativeVocabularyState.Failure(message: "词组参数无效")
                            }
                            replyHandler(try await service.set(text, enabled: enabled), nil)
                        }
                    } catch { replyHandler(nil, error.localizedDescription) }
                }
                return
            }
            if body["action"] as? String == "favorites" {
                guard let operation = body["operation"] as? String,
                      ["read", "trash", "save", "delete", "restore"].contains(operation) else {
                    replyHandler(nil, "收藏操作无效"); return
                }
                Task { @MainActor in
                    do {
                        let service = try prepareNativeFavoritesService()
                        let context = nativeFavoritesContext
                        var value = try await service.perform(operation, body["value"] as? [String: Any] ?? [:])
                        guard context == nativeFavoritesContext else { throw CancellationError() }
                        value["revision"] = service.projectionRevision
                        value["context"] = String(context ?? 0)
                        replyHandler(value, nil)
                    } catch { replyHandler(nil, error.localizedDescription) }
                }
                return
            }
            if body["action"] as? String == "reviewImprovement" {
                Task { @MainActor [weak self] in
                    do {
                        guard let self, let request = body["request"] as? [String: Any],
                              let operation = request["operation"] as? String else {
                            throw ReaderNativeReviewImprovements.Failure(message: "草稿请求无效")
                        }
                        if operation == "cancel" {
                            self.nativeReviewImprovements?.invalidate(request["lease"] as? String ?? "")
                            replyHandler(["ok": true], nil); return
                        }
                        let service = try self.prepareNativeReviewImprovements()
                        let value: [String: Any]
                        switch operation {
                        case "prepare": value = try await service.prepare(request)
                        case "commit": value = try await service.commit(request)
                        default: throw ReaderNativeReviewImprovements.Failure(message: "未知草稿操作")
                        }
                        replyHandler(["ok": true, "value": value], nil)
                    } catch { replyHandler(["ok": false, "error": error.localizedDescription], nil) }
                }
                return
            }
            if body["action"] as? String == "reviewQueue" {
                Task { @MainActor [weak self] in
                    do {
                        guard let self, let request = body["request"] as? [String: Any],
                              let operation = request["operation"] as? String else {
                            throw ReaderNativeReviewQueue.Failure(message: "复习请求无效")
                        }
                        let service = try self.prepareNativeReviewQueue()
                        let value: Any
                        switch operation {
                        case "load": value = try await service.load(request)
                        case "peek": value = try service.peek() as Any? ?? NSNull()
                        case "stageRating": value = try service.stageRating(request)
                        case "selectCard": value = try service.selectCard(request)
                        case "answer": value = try await service.answer(request)
                        case "undoRating": value = try service.undoRating(request)
                        case "interact": value = try service.interact(request)
                        case "reconcile":
                            guard let id = request["entityId"] as? String, let deviceID = self.nativeReadingStoreDeviceID else {
                                throw ReaderNativeReviewQueue.Failure(message: "卡片更新缺少身份")
                            }
                            let store = try self.nativeDataStoreHost.bridge(for: "bw-reader-native-v1-global").store
                            let record = try ReaderNativeCardRepository(store: store, deviceID: deviceID).load(id, includeDeleted: true)
                            value = try service.reconcile(id: id, record: record, request: request["lease"] as? String ?? "")
                        case "restoreRating": value = try service.restoreRating(request)
                        case "completeRating": service.completeRating(request); value = true
                        case "takeRating":
                            value = try service.takeRating(lease: request["lease"] as? String ?? "", stageID: request["stageId"] as? String ?? "")
                        case "discardRating":
                            service.discardRating(lease: request["lease"] as? String ?? "", stageID: request["stageId"] as? String ?? "")
                            value = true
                        case "save":
                            guard let snapshot = request["snapshot"] as? [String: Any], let lease = request["lease"] as? String else {
                                throw ReaderNativeReviewQueue.Failure(message: "复习保存缺少轮次")
                            }
                            value = try service.save(snapshot, request: lease)
                        case "cancel":
                            service.cancel(request["lease"] as? String ?? ""); value = true
                        default: throw ReaderNativeReviewQueue.Failure(message: "未知复习操作")
                        }
                        replyHandler(["ok": true, "value": value, "nativeReviewState": service.presentation()], nil)
                    } catch { replyHandler(["ok": false, "error": error.localizedDescription], nil) }
                }
                return
            }
            if body["action"] as? String == "preference" {
                do {
                    guard let request = body["request"] as? [String: Any],
                          let key = request["legacyKey"] as? String,
                          let deviceID = request["deviceID"] as? String,
                          let mutation = request["mutationId"] as? String else {
                        throw ReaderNativePreferences.Failure(message: "设置请求缺少身份")
                    }
                    let entry = try ReaderNativePreferences.Catalog.packaged.get().entry(key)
                    guard body["store"] as? String == entry.storeName,
                          request["collection"] as? String == entry.collection else {
                        throw ReaderNativePreferences.Failure(message: "设置存储范围不匹配")
                    }
                    let store = try nativeDataStoreHost.bridge(for: entry.storeName).store
                    guard try store.meta("legacyImport") == "done" else { throw ReaderBookUserStateWebAdapterError.unavailable }
                    let remove = try ReaderNativeCardRules.bool(request["remove"], "remove")
                    let raw = remove ? nil : try ReaderNativePreferences.rawValue(request["rawValue"])
                    let expected = ReaderNativeCardRules.has(request["ifRev"])
                        ? try ReaderNativeCardRules.integer(request["ifRev"], "ifRev") : nil
                    let receipt = try ReaderNativePreferences(store: store, deviceID: deviceID)
                        .commit(entry, raw: raw, mutation: mutation, expectedRevision: expected)
                    if !(receipt["changes"] as? [Any] ?? []).isEmpty {
                        markCloudSyncDirty()
                        if ["pdf-vocab-underline", "pdf-ruby"].contains(key) { refreshNativePageOverlays(force: true) }
                    }
                    replyHandler(receipt, nil)
                } catch ReaderNativeDataStore.StoreError.revisionConflict {
                    replyHandler(["ok": false, "code": "BW_DATA_CONFLICT"], nil)
                } catch { replyHandler(["ok": false, "code": "BW_NATIVE_PREFERENCE_FAILED", "error": error.localizedDescription], nil) }
                return
            }
            if body["action"] as? String == "cardRepository" {
                guard body["store"] as? String == "bw-reader-native-v1-global",
                      let request = body["request"] as? [String: Any],
                      let deviceID = request["deviceID"] as? String, !deviceID.isEmpty,
                      deviceID.utf16.count <= 240 else {
                    replyHandler(["ok": false, "code": "BW_CARD_REPOSITORY_UNAVAILABLE"], nil); return
                }
                do {
                    let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-global").store
                    guard try store.meta("legacyImport") == "done" else {
                        throw ReaderNativeCardRules.fail("UNAVAILABLE", "原生卡片数据库尚未完成迁移")
                    }
                    let receipt = try ReaderNativeCardRepository(store: store, deviceID: deviceID).perform(request)
                    if let record = receipt["result"] as? [String: Any] { nativeConversation.acceptCardRecord(record) }
                    if !(receipt["changes"] as? [Any] ?? []).isEmpty {
                        scheduleNativePDFProjectionRefresh()
                        markCloudSyncDirty()
                    }
                    replyHandler(receipt, nil)
                } catch let error as ReaderNativeCardRules.Failure {
                    replyHandler(["ok": false, "code": error.code, "error": error.detail], nil)
                } catch {
                    replyHandler(["ok": false, "code": "BW_CARD_REPOSITORY_UNAVAILABLE", "error": error.localizedDescription], nil)
                }
                return
            }
            if body["action"] as? String == "bookPageCard" {
                guard let bookID = nativeReadingStoreBookID, bookID == currentLocalBook?.id,
                      currentLocalBook?.format == .pdf, let deviceID = nativeReadingStoreDeviceID,
                      body["bookID"] as? String == bookID, let request = body["request"] as? [String:Any],
                      let operation = request["operation"] as? String,
                      ["action","direct","transition","recover"].contains(operation) else {
                    replyHandler(nil,"页面卡片书籍上下文已改变"); return
                }
                do {
                    let document = try nativeDataStoreHost.bridge(for:"bw-reader-native-v1-document").store
                    let global = try nativeDataStoreHost.bridge(for:"bw-reader-native-v1-global").store
                    let documentCursor = try document.cursor(), globalCursor = try global.cursor()
                    defer {
                        if (try? document.cursor()) != documentCursor || (try? global.cursor()) != globalCursor {
                            scheduleNativePDFProjectionRefresh(); markCloudSyncDirty()
                            nativeReplicationService?.wake()
                            // A direct edit or interrupted saga can commit an
                            // entity without returning a placement receipt.
                            // Refresh native faces from those committed rows,
                            // including when the following book write failed.
                            let changes = (try? global.journal(after:globalCursor,limit:4096)) ?? []
                            var changedIDs = Set<String>()
                            for change in changes {
                                guard let value = try? JSONSerialization.jsonObject(with:Data(change.json.utf8)) as? [String:Any],
                                      value["collection"] as? String == ReaderNativeCardRepository.entities,
                                      let record = value["record"] as? [String:Any], let id = record["id"] as? String else { continue }
                                changedIDs.insert(id)
                            }
                            let repository = ReaderNativeCardRepository(store:global,deviceID:deviceID)
                            for id in changedIDs {
                                if let result = try? repository.perform(["operation":"load","arguments":[id]]),
                                   let record = result["result"] as? [String:Any] { nativeConversation.acceptCardRecord(record) }
                            }
                        }
                    }
                    guard try global.meta("legacyImport") == "done" else { throw ReaderNativePageCardActions.F("卡片数据库尚未准备好") }
                    let receipt = try ReaderNativePageCardActions(
                        book:.init(store:document,bookID:bookID,deviceID:deviceID),
                        repository:.init(store:global,deviceID:deviceID),sanitizeHTML:ReaderNativePageCardHTML.sanitize).perform(request)
                    if let result = receipt["result"] as? [String:Any], let journal = result["receipt"] as? [String:Any],
                       let entity = journal["entity"] as? [String:Any], let id = entity["id"] as? String,
                       let record = try ReaderNativeCardRepository(store:global,deviceID:deviceID).perform(["operation":"load","arguments":[id]])["result"] as? [String:Any] {
                        nativeConversation.acceptCardRecord(record)
                    }
                    replyHandler(receipt,nil)
                } catch let error as ReaderNativePageCardActions.F {
                    replyHandler(["ok":false,"code":"BW_NATIVE_PDF_ASSISTANT_ACTION","status":error.conflict ? 409 : 400,"error":error.localizedDescription],nil)
                } catch { replyHandler(nil,error.localizedDescription) }
                return
            }
            if body["action"] as? String == "bookAssistantSnapshot" {
                guard let bookID = nativeReadingStoreBookID, bookID == currentLocalBook?.id,
                      nativeReadingStoreDeviceID != nil, body["bookID"] as? String == bookID,
                      let surface = body["surface"] as? String,
                      surface == (currentLocalBook?.format == .epub ? "epub" : "pdf") else {
                    replyHandler(nil,"助手书籍状态未就绪或已经切换"); return
                }
                do {
                    let store = try nativeDataStoreHost.bridge(for:"bw-reader-native-v1-document").store
                    replyHandler(["ok":true,"snapshot":try ReaderNativeBookProjection(store:store).assistantSnapshot(bookID:bookID,surface:surface)],nil)
                } catch { replyHandler(nil,error.localizedDescription) }
                return
            }
            if body["action"] as? String == "bookMutation" {
                guard let request = body["request"] as? [String: Any],
                      let bookID = nativeReadingStoreBookID, bookID == currentLocalBook?.id,
                      let deviceID = nativeReadingStoreDeviceID,
                      request["bookID"] as? String == bookID else {
                    replyHandler(nil, "原生书籍写入上下文尚未准备好或已切换")
                    return
                }
                do {
                    let store = try nativeDataStoreHost.bridge(for: "bw-reader-native-v1-document").store
                    let receipt = try ReaderNativeBookStore(store: store, bookID: bookID, deviceID: deviceID,
                        displayName: currentLocalBook?.title, contentSHA256: currentLocalBookContentSHA256).perform(request)
                    nativeReplicationService?.wake()
                    if request["operation"] as? String != "replication-enqueue" {
                        scheduleNativePDFProjectionRefresh()
                        markCloudSyncDirty()
                    }
                    replyHandler(receipt, nil)
                } catch let error as ReaderNativeAssistantEdits.Failure {
                    replyHandler(["ok":false,"code":"BW_NATIVE_PDF_ASSISTANT_ACTION","status":error.conflict ? 409 : 400,"error":error.localizedDescription],nil)
                } catch ReaderNativeDataStore.StoreError.revisionConflict {
                    replyHandler(["ok": false, "code": "BW_DATA_CONFLICT"], nil)
                } catch ReaderNativeNoteRules.NoteError.missing {
                    replyHandler(["ok": false, "code": "BW_LOCAL_NOTES", "status": 404, "error": "未找到便签"], nil)
                } catch let error as ReaderNativeNoteRules.NoteError {
                    replyHandler(["ok": false, "code": "BW_LOCAL_NOTES", "status": 400, "error": error.localizedDescription], nil)
                } catch ReaderNativeHighlightRules.HighlightError.missing {
                    replyHandler(["ok":false,"code":"BW_LOCAL_HIGHLIGHTS","status":404,"error":"未找到划线"],nil)
                } catch ReaderNativeHighlightRules.HighlightError.conflict {
                    replyHandler(["ok":false,"code":"BW_NATIVE_PDF_ASSISTANT_CONFLICT","status":409,"error":"划线操作编号已用于不同内容"],nil)
                } catch let error as ReaderNativeHighlightRules.HighlightError {
                    replyHandler(["ok":false,"code":"BW_LOCAL_HIGHLIGHTS","status":400,"error":error.localizedDescription],nil)
                } catch { replyHandler(nil, String(describing: error)) }
                return
            }
            // Native mutations require one authoritative database. A migration
            // failure is visible/retryable, never a switch to a second writer.
            if body["action"] as? String == "disableStore" {
                replyHandler(["ok":false,"error":"原生数据库迁移未完成，请重试；原数据已保留"], nil)
                return
            }
            do {
                let settingScope = body["store"] as? String == "bw-reader-native-v1-global" ? "user-settings"
                    : body["store"] as? String == "bw-reader-native-v1-device" ? "device-preferences" : nil
                let settingsStore = body["action"] as? String == "commit" && settingScope != nil
                    ? try nativeDataStoreHost.bridge(for: body["store"] as! String).store : nil
                let settingsBefore = settingsStore.map { $0.generation(collection: settingScope!) }
                let receipt = try nativeDataStoreHost.handle(body)
                if let settingsStore, let settingScope, settingsBefore != settingsStore.generation(collection: settingScope) {
                    refreshNativePageOverlays(force: true)
                }
                replyHandler(receipt, nil)
            } catch {
                // 冲突是**正常分支**（乐观并发），要让 JS 那侧认得出来去重试，
                // 而不是当成一次失败往上抛。
                if case ReaderNativeDataStore.StoreError.revisionConflict = error {
                    replyHandler(["ok": false, "code": "BW_DATA_CONFLICT"], nil)
                } else {
                    replyHandler(nil, String(describing: error))
                }
            }
            return
        }
        if message.name == nativeReaderGeometryMessageName {
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                isTrustedReaderURL(webView.url),
                isTrustedReaderURL(message.frameInfo.request.url),
                let body = message.body as? [String: Any],
                Set(body.keys).isSubset(of: ["action", "page", "text"]),
                let action = body["action"] as? String, ["binding", "characters"].contains(action),
                let number = body["page"] as? NSNumber,
                CFGetTypeID(number) != CFBooleanGetTypeID(),
                number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
                number.doubleValue > 0, number.doubleValue < 10_000_000,
                action == "characters" || ((body["text"] as? String).map { !$0.isEmpty && $0.count <= 16000 } ?? false)
            else {
                replyHandler(nil, "原生定位请求无效")
                return
            }
            guard let document = nativePDFDocument else {
                // Unavailable is distinct from a text miss; neither authorizes
                // recreating a hidden webpage renderer.
                replyHandler(["ok": false, "code": "BW_NATIVE_GEOMETRY_UNAVAILABLE"], nil)
                return
            }
            Task { @MainActor [weak self, weak document] in
                do {
                    guard let self, let document, self.nativePDFDocument === document else {
                        throw ReaderBookUserStateWebAdapterError.contextChanged
                    }
                    let value: [String: Any]?
                    if action == "characters" {
                        value = try await document.sourceCharacters(page: number.intValue)
                    } else {
                        value = try await document.prepareBinding(page: number.intValue, text: body["text"] as? String ?? "")
                    }
                    guard self.nativePDFDocument === document else {
                        throw ReaderBookUserStateWebAdapterError.contextChanged
                    }
                    guard var payload = value else {
                        replyHandler(["ok": false, "code": "BW_NATIVE_GEOMETRY_MISS"], nil)
                        return
                    }
                    payload["ok"] = true
                    replyHandler(payload, nil)
                } catch { replyHandler(nil, error.localizedDescription) }
            }
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
                    "error": "本机笔记未启用，未向服务器或回环地址假提交",
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
        // ⚠ 这里原来**一声不响**地重载（2026-09-22 修）：渲染进程被系统杀掉，
        //   页面白屏转圈再自己回来，用户只能叫它"崩溃"，而崩之前在做什么、
        //   崩了几次，没有任何地方说得出来。于是每次都只能靠猜。
        //   页面里的线索随进程一起没了，能留下证据的只有 App 进程这一侧。
        noteWebContentTermination()
        nativeAssistantStream?.invalidate()
        nativeContextSelections?.invalidate()
        nativeTurns?.invalidate()
        nativeReviewQueue?.invalidate(); nativeReviewQueue = nil; nativeReviewQueueContext = nil
        nativeReviewImprovements?.invalidate(); nativeReviewImprovements = nil; nativeReviewImprovementsContext = nil
        invalidateNativePDFDocument(reason: "webcontent-terminated")
        nativeConversation.resetForNavigation()
        webContentProcessNeedsReload = true
        isLoading = false
        guard readerForeground, isLocalRuntimeURL(webView.url) else { return }
        reloadLocalRuntimeAfterRecoveryIfNeeded(serverRebuilt: false)
    }

    func dismissWebContentRecoveryNotice() { webContentRecoveryNotice = nil }

    /// 随手一句话的提示（顶层胶囊，几秒后自己消失）。
    ///
    /// ⚠ 存在的理由：原生那堆“点一下去做件事”的按钮失败时，错误只写进
    /// `ReaderNativeConversationModel.error`，而那东西只在**侧栏里**显示 ——
    /// 侧栏多半没开。于是用户看到的就是“点了没反应”，而我们连它报没报错都不知道。
    func showTransientNotice(_ text: String) {
        guard !text.isEmpty else { return }
        transientNotice = text
        transientNoticeTicket += 1
        let ticket = transientNoticeTicket
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            guard let self, self.transientNoticeTicket == ticket else { return }
            self.transientNotice = nil
        }
    }

    func dismissTransientNotice() { transientNotice = nil }

    /// App 这一侧的内存占用。⚠ 渲染进程是**另一个**进程，这个数字不等于它 ——
    /// 但两边一起涨是常态，所以它仍然是"是不是内存压力"的第一手线索。
    /// 取不到就回 0，绝不因为一个诊断数字让上报失败。
    // ⚠ 内存读取的唯一实现在 ReaderNativeStartupProfile —— 两处各写一份，
    //   日后就会报出两个对不上的数。
    private static func memoryFootprintMB() -> Int { ReaderNativeStartupProfile.footprintMB() }

    /// 把这次回收记成一句人看得懂的话。**在 `resetForNavigation()` 之前调**
    /// —— 它会把上一条命令连同会话状态一起清掉，那正是我们要的线索。
    private func noteWebContentTermination() {
        webContentTerminationCount += 1
        // 自动送现场：App 进程还活着，所以这一刻能把面包屑直接发出去。
        // ⚠ 这是唯一能抓到"渲染进程被回收"的时机 —— 它不会触发下次启动的补报
        //   （App 没死），不报就永远没有记录。
        ReaderNativeFaultReporter.shared.report(
            code: "BW_WEBCONTENT_TERMINATED",
            message: "阅读页渲染进程被系统回收（第 \(webContentTerminationCount) 次）",
            detail: "last=" + nativeConversation.lastCommandAction
                + " scope=" + nativeConversation.scope
                + " footprintMB=" + String(Self.memoryFootprintMB())
                // 快照开销：怀疑"每 60ms 序列化整段对话"把渲染进程顶掉时，
                // 这一句就是判据。没有它又只能靠猜。
                + " " + nativeConversation.snapshotCostSummary)
        var line = "阅读页渲染进程被系统回收，已自动重载"
        if !nativeConversation.lastCommandAction.isEmpty {
            let formatter = DateFormatter()
            formatter.dateFormat = "HH:mm:ss"
            let when = nativeConversation.lastCommandAt.map { "，" + formatter.string(from: $0) } ?? ""
            line += "（上一步：" + nativeConversation.lastCommandAction + when + "）"
        }
        if webContentTerminationCount > 1 { line += " · 本次阅读第 \(webContentTerminationCount) 次" }
        webContentRecoveryNotice = line
        let ticket = webContentTerminationCount
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 12_000_000_000)
            guard let self, self.webContentTerminationCount == ticket else { return }
            self.webContentRecoveryNotice = nil
        }
    }

    func webView(
        _ webView: WKWebView,
        didStartProvisionalNavigation navigation: WKNavigation!
    ) {
        nativeAssistantStream?.invalidate()
        nativeContextSelections?.invalidate()
        nativeTurns?.invalidate()
        nativeReviewQueue?.invalidate(); nativeReviewQueue = nil; nativeReviewQueueContext = nil
        nativeReviewImprovements?.invalidate(); nativeReviewImprovements = nil; nativeReviewImprovementsContext = nil
        invalidateNativePDFDocument(reason: "navigation-start")
        nativeLookupTasks.values.forEach { $0.task.cancel() }
        nativeLookupTasks.removeAll()
        nativeLookupCache.removeAll()
        nativeLookupCacheBytes = 0
        isLoading = true
        loadError = nil
        nativeConversation.resetForNavigation()
        nativePencilInk.invalidateDocument()
        nativeOpenBoundNotes = []   // 开合是这本书的原生状态，换页面就清
        nativePDFOpenFailure = nil
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
        // 基线：到这一刻为止烧掉的，主要就是阅读器前端那 5.77 MB JS 的解析与执行。
        // 「把判断搬进 Swift 值不值」这个问题，答案的一半在这个数字里。
        ReaderNativeStartupProfile.shared.mark("网页层就绪 (didFinish)")
        Task { @MainActor [weak self] in
            let enabled = UserDefaults.standard.object(forKey: "reader.nativeInterfaceEnabled") as? Bool ?? true
            await self?.setNativeConversationMode(enabled)
        }
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
        // 书渲完才挂原生主阅读区：prepare 要读原件身份、初始位置和 user-state 包，
        // 这三样在 didFinish 之前都还没就位。
        mountNativePDFDocument()
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
    /// 当前这本是 EPUB 吗 —— 选区操作条只在 EPUB 上出（PDF 有自己的选区菜单）。
    var isEPUBBook: Bool { currentLocalBook?.format == .epub }

    func refreshEPUBHighlightColors() {
        guard isEPUBBook else { epubHighlightColors = []; return }
        Task { @MainActor [weak self] in
            guard let self else { return }
            let receipt = await self.requestNativeConversationCommand([
                "action": "nativeEpubHighlightColors", "scope": self.nativeConversation.scope,
            ])
            guard receipt["ok"] as? Bool == true,
                  let colors = (receipt["value"] as? [String: Any])?["colors"] as? [String] else { return }
            self.epubHighlightColors = colors.filter { $0.hasPrefix("#") && $0.count == 7 }
        }
    }

    /// EPUB 选区操作条点了某一项。
    func performEPUBSelectionAction(_ mode: String) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            if mode == "grammar" { await self.openEPUBGrammar() }
            else if mode.hasPrefix("highlight:") {
                await self.highlightEPUBSelection(color: String(mode.dropFirst("highlight:".count)))
            }
            else { await self.openEPUBLookup(mode: mode) }
        }
    }

    @MainActor
    private func highlightEPUBSelection(color: String) async {
        let receipt = await requestNativeConversationCommand([
            "action": "nativeEpubHighlight", "scope": nativeConversation.scope,
            "value": ["color": color],
        ])
        guard receipt["ok"] as? Bool == true else {
            nativeConversation.report(receipt["error"] as? String ?? "划线没有保存成功。")
            return
        }
    }

    /// 问网页要当前选区，然后开原生面板。
    ///
    /// ⚠ 选区要**此刻**去问，不能缓存：菜单从弹出到点下去之间，用户可能已经改了
    /// 选择（拖了把手、或点到别处又重选）。拿旧的就会解释一段他没选的文字。
    @MainActor
    private func openEPUBLookup(mode: String) async {
        let generation = bookUserStateContextGeneration
        let value = try? await webView.callAsyncJavaScript(
            "return window.__bwReaderEpubSelection?.({consume: true}) ?? null;",
            arguments: [:], in: nil, contentWorld: .page)
        guard generation == bookUserStateContextGeneration, isEPUBBook else { return }
        guard let payload = value as? [String: Any],
              let text = payload["text"] as? String, !text.isEmpty else {
            nativeConversation.report("没有选中内容。")
            return
        }
        openNativeLookup(page: 0, text: text,
                         sentence: payload["context"] as? String ?? "", mode: mode)
    }

    /// EPUB 的「语法」。分析对象是**所在句**，焦点是选中那一段 —— 与 PDF 同一口径。
    /// ⚠ EPUB 给的 context 是所在**块**（比句子宽）。`RC.grammar.analyzeData` 内部
    /// 不会再切句，所以这里先用 `RC.grammar.extractSentence` 把那一句抠出来 ——
    /// 直接把整段送进去，AI 会去分析一段而不是一句。
    @MainActor
    private func openEPUBGrammar() async {
        let value = try? await webView.callAsyncJavaScript(
            """
            const sel = window.__bwReaderEpubSelection?.();
            if (!sel || !sel.text) return null;
            const g = window.RC && window.RC.grammar;
            const sentence = (g && g.extractSentence)
              ? (g.extractSentence(sel.context || sel.text, sel.text) || sel.text) : sel.text;
            return { text: sel.text, sentence: sentence };
            """,
            arguments: [:], in: nil, contentWorld: .page)
        guard let payload = value as? [String: Any],
              let text = payload["text"] as? String, !text.isEmpty else {
            nativeConversation.report("没有选中内容。")
            return
        }
        openNativeGrammar(sentence: payload["sentence"] as? String ?? text, focus: text)
    }

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

/// 原生正文「接管阅读视口」只做一次的标记（见 mountNativePDFDocument）。
/// MainActor 隔离：只在主线程的 Task 里读写，并发安全由隔离保证。
@MainActor
final class ReaderNativeActivationClaim {
    var claimed = false
}

import CoreFoundation
import CryptoKit
import Foundation
import WebKit

@MainActor
final class NativeBookOCRBridge: NSObject, WKScriptMessageHandlerWithReply {
    static let messageName = "bwNativePageText"
    static let requestContract = "reader-native-page-text-request/1"
    static let responseContract = "reader-native-page-text-response/1"

    private weak var webView: WKWebView?
    private let manager: NativeBookOCRManager
    private var trustedBaseURL: URL
    private var localBookID: String
    private var expectedContentSHA256: String?
    private var rejectsContentIdentity: Bool
    private var localBookAccess: ReaderLocalBookAccess?

    init(
        webView: WKWebView,
        manager: NativeBookOCRManager = .shared,
        trustedBaseURL: URL,
        localBookID: String,
        expectedContentSHA256: String? = nil,
        localBookAccess: ReaderLocalBookAccess? = nil
    ) {
        let normalizedContentSHA256 = Self.normalizedSHA256(
            expectedContentSHA256
        )
        self.webView = webView
        self.manager = manager
        self.trustedBaseURL = trustedBaseURL
        self.localBookID = localBookID
        self.expectedContentSHA256 = normalizedContentSHA256
        self.localBookAccess = localBookAccess
        self.rejectsContentIdentity = expectedContentSHA256 != nil
            && normalizedContentSHA256 == nil
        super.init()
        if let expectedContentSHA256 = self.expectedContentSHA256 {
            try? manager.activate(
                bookID: localBookID,
                expectedContentSHA256: expectedContentSHA256
            )
        }
    }

    /// A retained bridge may follow the same trusted local WKWebView to a new
    /// opaque book. The host updates both values together before navigation.
    func updateTrustedContext(
        baseURL: URL,
        localBookID: String,
        expectedContentSHA256: String? = nil,
        localBookAccess: ReaderLocalBookAccess? = nil
    ) {
        let previousBookID = self.localBookID
        trustedBaseURL = baseURL
        self.localBookID = localBookID
        self.expectedContentSHA256 = Self.normalizedSHA256(
            expectedContentSHA256
        )
        self.localBookAccess = localBookAccess
        rejectsContentIdentity = expectedContentSHA256 != nil
            && self.expectedContentSHA256 == nil
        if let expectedContentSHA256 = self.expectedContentSHA256 {
            try? manager.activate(
                bookID: localBookID,
                expectedContentSHA256: expectedContentSHA256
            )
        }
        if previousBookID != localBookID {
            manager.deactivate(bookID: previousBookID)
        }
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        guard message.name == Self.messageName,
              message.frameInfo.isMainFrame,
              let webView,
              message.webView === webView,
              let frameURL = message.frameInfo.request.url,
              isTrusted(frameURL),
              let documentURL = webView.url,
              isTrusted(documentURL) else {
            replyHandler(nil, "BW_NATIVE_PAGE_TEXT_UNTRUSTED")
            return
        }
        let request: Request
        do {
            request = try Request.parse(
                message.body,
                expectedLocalBookID: localBookID
            )
        } catch {
            replyHandler(nil, "BW_NATIVE_PAGE_TEXT_INVALID_REQUEST")
            return
        }

        let expectedContentSHA256 = rejectsContentIdentity ? nil
            : (self.expectedContentSHA256
                ?? manager.activatedContentSHA256(for: request.localBookID))
        let requestBookAccess = localBookAccess
        Task { [manager] in
            let status: NativeBookOCRBookStatus
            if let expectedContentSHA256 {
                status = await manager.readyStatus(
                    for: request.localBookID,
                    expectedContentSHA256: expectedContentSHA256
                )
            } else {
                // Restored jobs never activate themselves. A local book with
                // no current full-content identity must stay passive/idle.
                status = .idle(bookID: request.localBookID)
            }
            do {
                let payload: [String: Any]
                switch request.action {
                case .pageCharacters:
                    payload = try await Self.pageReply(
                        request: request,
                        expectedContentSHA256: expectedContentSHA256,
                        status: status,
                        book: requestBookAccess,
                        manager: manager
                    )
                case .status:
                    payload = Self.statusReply(
                        request: request,
                        status: status
                    )
                case .search:
                    payload = try await Self.searchReply(
                        request: request,
                        expectedContentSHA256: expectedContentSHA256,
                        status: status,
                        manager: manager
                    )
                case .recognizeSelection:
                    payload = try await Self.selectionReply(
                        request: request,
                        expectedContentSHA256: expectedContentSHA256,
                        status: status,
                        book: requestBookAccess,
                        manager: manager
                    )
                case .reOCRPage:
                    payload = try await Self.reOCRReply(
                        request: request,
                        expectedContentSHA256: expectedContentSHA256,
                        status: status,
                        book: requestBookAccess,
                        manager: manager
                    )
                case .clearReOCRPage:
                    payload = try await Self.clearReOCRReply(
                        request: request,
                        expectedContentSHA256: expectedContentSHA256,
                        status: status,
                        book: requestBookAccess,
                        manager: manager
                    )
                }
                guard JSONSerialization.isValidJSONObject(payload) else {
                    replyHandler(nil, "BW_NATIVE_PAGE_TEXT_ENCODING_FAILED")
                    return
                }
                replyHandler(payload, nil)
            } catch {
                let payload = Self.failureReply(
                    request: request,
                    status: status,
                    code: "BW_NATIVE_PAGE_TEXT_READ_FAILED",
                    message: Self.safeMessage(error.localizedDescription)
                )
                replyHandler(payload, nil)
            }
        }
    }

    /// Emits the exact passive update event expected by the local Reader. A
    /// nil page is an explicit whole-layer switch and invalidates every loaded
    /// page without starting or resuming OCR.
    func sendUpdate(
        _ update: NativeBookOCRUpdate,
        to webView: WKWebView
    ) async {
        guard update.bookID == localBookID,
              update.page.map({ $0 >= 1 }) ?? true,
              let registeredWebView = self.webView,
              webView === registeredWebView,
              let documentURL = webView.url,
              isTrusted(documentURL) else {
            return
        }
        guard !rejectsContentIdentity,
              let expectedContentSHA256 = self.expectedContentSHA256
                ?? manager.activatedContentSHA256(for: update.bookID),
              update.status.contentSHA256.isEmpty
                || update.status.contentSHA256.caseInsensitiveCompare(
                    expectedContentSHA256
                ) == .orderedSame else {
            return
        }
        let pageValue: NativeBookOCRPageCharacters?
        if let page = update.page {
            pageValue = try? await manager.pageCharacters(
                bookID: update.bookID,
                expectedContentSHA256: expectedContentSHA256,
                page: page
            )
        } else {
            pageValue = nil
        }
        let payload: [String: Any] = [
            "contract": NativeBookOCRUpdate.contract,
            "localBookId": update.bookID,
            "page": Self.jsonNullable(update.page),
            "state": pageValue?.status.rawValue ?? Self.passiveState(
                update.status
            ).rawValue,
            "source": Self.jsonNullable(
                pageValue?.source?.rawValue ?? update.status.source?.rawValue
            ),
            "revision": pageValue.map { Self.pageRevision($0) }
                ?? Self.statusRevision(update.status),
        ]
        guard JSONSerialization.isValidJSONObject(payload) else { return }
        _ = try? await webView.callAsyncJavaScript(
            """
            window.dispatchEvent(new CustomEvent(
              'bw:native-page-text-updated',
              {detail: payload}
            ));
            return true;
            """,
            arguments: ["payload": payload],
            in: nil,
            contentWorld: .page
        )
    }

    private enum Action: String {
        case pageCharacters = "page-chars"
        case status
        case search
        case recognizeSelection = "ocr-selection"
        case reOCRPage = "reocr-page"
        case clearReOCRPage = "clear-reocr-page"
    }

    private struct Request {
        let action: Action
        let requestID: String
        let localBookID: String
        let page: Int?
        let query: String?
        let limit: Int?
        let bbox: [Double]?

        static func parse(
            _ body: Any,
            expectedLocalBookID: String
        ) throws -> Request {
            guard let value = body as? [String: Any],
                  value["contract"] as? String
                    == NativeBookOCRBridge.requestContract,
                  let actionRaw = value["action"] as? String,
                  let action = Action(rawValue: actionRaw),
                  let requestID = value["requestId"] as? String,
                  requestID.range(
                    of: #"^[A-Za-z0-9_-]{1,128}$"#,
                    options: .regularExpression
                  ) != nil,
                  let localBookID = value["localBookId"] as? String,
                  localBookID == expectedLocalBookID,
                  !localBookID.isEmpty,
                  localBookID.count <= 200 else {
                throw BridgeError.invalidRequest
            }
            let common = Set(["contract", "action", "requestId", "localBookId"])
            switch action {
            case .pageCharacters:
                guard Set(value.keys) == common.union(["page"]),
                      let page = strictInteger(value["page"]),
                      (1...100_000).contains(page) else {
                    throw BridgeError.invalidRequest
                }
                return Request(
                    action: action,
                    requestID: requestID,
                    localBookID: localBookID,
                    page: page,
                    query: nil,
                    limit: nil,
                    bbox: nil
                )
            case .status:
                guard Set(value.keys) == common else {
                    throw BridgeError.invalidRequest
                }
                return Request(
                    action: action,
                    requestID: requestID,
                    localBookID: localBookID,
                    page: nil,
                    query: nil,
                    limit: nil,
                    bbox: nil
                )
            case .search:
                guard Set(value.keys) == common.union(["query", "limit"]),
                      let query = value["query"] as? String else {
                    throw BridgeError.invalidRequest
                }
                let trimmed = query.trimmingCharacters(
                    in: .whitespacesAndNewlines
                )
                guard (1...256).contains(trimmed.count),
                      let limit = strictInteger(value["limit"]),
                      (1...200).contains(limit) else {
                    throw BridgeError.invalidRequest
                }
                return Request(
                    action: action,
                    requestID: requestID,
                    localBookID: localBookID,
                    page: nil,
                    query: trimmed,
                    limit: limit,
                    bbox: nil
                )
            case .recognizeSelection:
                guard Set(value.keys) == common.union(["page", "bbox"]),
                      let page = strictInteger(value["page"]),
                      (1...100_000).contains(page),
                      let rawBBox = value["bbox"] as? [Any],
                      rawBBox.count == 4 else {
                    throw BridgeError.invalidRequest
                }
                let bbox = try rawBBox.map { raw -> Double in
                    guard let number = raw as? NSNumber,
                          CFGetTypeID(number) != CFBooleanGetTypeID(),
                          number.doubleValue.isFinite,
                          number.doubleValue >= 0,
                          number.doubleValue <= 100_000 else {
                        throw BridgeError.invalidRequest
                    }
                    return number.doubleValue
                }
                guard bbox[2] - bbox[0] >= 0.5,
                      bbox[3] - bbox[1] >= 0.5 else {
                    throw BridgeError.invalidRequest
                }
                return Request(
                    action: action,
                    requestID: requestID,
                    localBookID: localBookID,
                    page: page,
                    query: nil,
                    limit: nil,
                    bbox: bbox
                )
            case .reOCRPage, .clearReOCRPage:
                guard Set(value.keys) == common.union(["page"]),
                      let page = strictInteger(value["page"]),
                      (1...100_000).contains(page) else {
                    throw BridgeError.invalidRequest
                }
                return Request(
                    action: action,
                    requestID: requestID,
                    localBookID: localBookID,
                    page: page,
                    query: nil,
                    limit: nil,
                    bbox: nil
                )
            }
        }

        private static func strictInteger(_ value: Any?) -> Int? {
            guard let number = value as? NSNumber,
                  CFGetTypeID(number) != CFBooleanGetTypeID() else {
                return nil
            }
            let double = number.doubleValue
            guard double.isFinite,
                  double.rounded(.towardZero) == double,
                  double >= Double(Int.min),
                  double <= Double(Int.max) else {
                return nil
            }
            return number.intValue
        }
    }

    private enum BridgeError: Error {
        case invalidRequest
    }

    private static func pageReply(
        request: Request,
        expectedContentSHA256: String?,
        status: NativeBookOCRBookStatus,
        book: ReaderLocalBookAccess?,
        manager: NativeBookOCRManager
    ) async throws -> [String: Any] {
        let pageNumber = request.page!
        let value: NativeBookOCRPageCharacters?
        if let expectedContentSHA256,
           let book,
           book.record.id == request.localBookID,
           book.record.format == .pdf {
            value = try await manager.readerPageCharacters(
                book: book,
                expectedContentSHA256: expectedContentSHA256,
                page: pageNumber
            )
        } else if let expectedContentSHA256 {
            value = try await manager.pageCharacters(
                bookID: request.localBookID,
                expectedContentSHA256: expectedContentSHA256,
                page: pageNumber
            )
        } else {
            value = nil
        }
        guard let value else {
            let state: NativeBookOCRPageState = status.state == .failed
                ? .failed : (status.state == .idle ? .idle : .pending)
            return [
                "contract": responseContract,
                "action": request.action.rawValue,
                "requestId": request.requestID,
                "ok": false,
                "state": state.rawValue,
                "source": jsonNullable(status.source?.rawValue),
                "revision": statusRevision(status),
                "error": state == .failed
                    ? (errorObject(
                        code: "BW_NATIVE_PAGE_TEXT_FAILED",
                        message: status.message ?? "本机文字预处理失败",
                        retryable: true
                      ) as Any)
                    : (NSNull() as Any),
                "page": pageNumber,
                "pageWidth": 0,
                "pageHeight": 0,
                "chars": [],
                "furigana": [],
                "wordSegmentation": NativeBookOCRWordSegmentationState.unavailable.rawValue,
                "characterGeometry": NativeBookOCRCharacterGeometryState.unavailable.rawValue,
                "formulaCoverage": NativeBookOCRFormulaCoverage.unknown.rawValue,
                "formulaRegions": [],
                "textAuthority": NativeBookOCRTextAuthority.supplemental.rawValue,
            ]
        }
        return [
            "contract": responseContract,
            "action": request.action.rawValue,
            "requestId": request.requestID,
            "ok": value.status == .ready || value.status == .readyEmpty,
            "state": value.status.rawValue,
            "source": jsonNullable(value.source?.rawValue),
            "revision": pageRevision(value),
            "error": jsonNullable(value.error.map {
                errorObject(
                    code: "BW_NATIVE_PAGE_TEXT_FAILED",
                    message: safeMessage($0),
                    retryable: true
                )
            }),
            "page": value.page,
            "pageWidth": value.pageWidth,
            "pageHeight": value.pageHeight,
            "chars": value.chars.map(characterObject),
            "furigana": value.furigana.map(furiganaObject),
            "wordSegmentation": value.wordSegmentation.rawValue,
            "characterGeometry": value.characterGeometry.rawValue,
            "formulaCoverage": value.formulaCoverage.rawValue,
            "formulaRegions": value.formulaRegions.map(formulaObject),
            "textAuthority": (
                value.textAuthority ?? .supplemental
            ).rawValue,
        ]
    }

    private static func statusReply(
        request: Request,
        status: NativeBookOCRBookStatus
    ) -> [String: Any] {
        [
            "contract": responseContract,
            "action": request.action.rawValue,
            "requestId": request.requestID,
            "ok": true,
            "state": passiveState(status).rawValue,
            "source": jsonNullable(status.source?.rawValue),
            "revision": statusRevision(status),
            "error": NSNull(),
            "progress": [
                "total": status.totalPages,
                "ready": status.textProgress.completed,
                "pending": status.textProgress.pending,
                "failed": status.textProgress.failed,
                "activePage": jsonNullable(status.currentPage),
                "currentPage": jsonNullable(status.currentPage),
                "textProgress": stageObject(status.textProgress),
                "wordProgress": stageObject(status.wordProgress),
                "formulaProgress": stageObject(status.formulaProgress),
                "formulaPendingRegions": status.formulaPendingRegions,
                "formulaFailedRegions": status.formulaFailedRegions,
            ],
        ]
    }

    private static func searchReply(
        request: Request,
        expectedContentSHA256: String?,
        status: NativeBookOCRBookStatus,
        manager: NativeBookOCRManager
    ) async throws -> [String: Any] {
        let value: NativeBookOCRSearchResult
        if let expectedContentSHA256 {
            value = try await manager.search(
                bookID: request.localBookID,
                expectedContentSHA256: expectedContentSHA256,
                query: request.query!,
                limit: request.limit!
            )
        } else {
            value = NativeBookOCRSearchResult(
                matches: [], total: 0, pages: [], incomplete: true
            )
        }
        let grouped = Dictionary(grouping: value.matches, by: \NativeBookOCRSearchHit.page)
        let matches: [[String: Any]] = grouped.keys.sorted().map { page in
            let hits = grouped[page] ?? []
            return [
                "page": page,
                "count": hits.count,
                "snippet": safeMessage(hits.first?.text ?? ""),
            ]
        }
        return [
            "contract": responseContract,
            "action": request.action.rawValue,
            "requestId": request.requestID,
            "ok": true,
            "state": passiveState(status).rawValue,
            "source": jsonNullable(status.source?.rawValue),
            "revision": statusRevision(status),
            "error": NSNull(),
            "matches": matches,
            "total": value.total,
            "pages": status.textProgress.completed,
            "incomplete": value.incomplete,
        ]
    }

    private static func selectionReply(
        request: Request,
        expectedContentSHA256: String?,
        status: NativeBookOCRBookStatus,
        book: ReaderLocalBookAccess?,
        manager: NativeBookOCRManager
    ) async throws -> [String: Any] {
        let (book, digest) = try mutationContext(
            request: request,
            expectedContentSHA256: expectedContentSHA256,
            book: book
        )
        let result = try await manager.recognizeSelection(
            book: book,
            contentSHA256: digest,
            page: request.page!,
            bbox: request.bbox!
        )
        return mutationReply(
            request: request,
            page: result.page,
            fields: [
                "text": result.text,
                "cv": pageRevision(result.page),
                "persisted": true,
                "page": result.page.page,
                "textAuthority": NativeBookOCRTextAuthority.localOverride.rawValue,
            ]
        )
    }

    private static func reOCRReply(
        request: Request,
        expectedContentSHA256: String?,
        status: NativeBookOCRBookStatus,
        book: ReaderLocalBookAccess?,
        manager: NativeBookOCRManager
    ) async throws -> [String: Any] {
        let (book, digest) = try mutationContext(
            request: request,
            expectedContentSHA256: expectedContentSHA256,
            book: book
        )
        let page = try await manager.reOCRPage(
            book: book,
            contentSHA256: digest,
            page: request.page!
        )
        return mutationReply(
            request: request,
            page: page,
            fields: [
                "chars": page.chars.filter { $0.sp == 0 }.count,
                "cv": pageRevision(page),
                "page": page.page,
                "textAuthority": NativeBookOCRTextAuthority.localOverride.rawValue,
            ]
        )
    }

    private static func clearReOCRReply(
        request: Request,
        expectedContentSHA256: String?,
        status: NativeBookOCRBookStatus,
        book: ReaderLocalBookAccess?,
        manager: NativeBookOCRManager
    ) async throws -> [String: Any] {
        let (book, digest) = try mutationContext(
            request: request,
            expectedContentSHA256: expectedContentSHA256,
            book: book
        )
        let result = try await manager.clearManualReOCR(
            book: book,
            contentSHA256: digest,
            page: request.page!
        )
        if let page = result.page {
            return mutationReply(
                request: request,
                page: page,
                fields: [
                    "cleared": result.cleared,
                    "cv": pageRevision(page),
                    "page": page.page,
                    "textAuthority": (
                        page.textAuthority ?? .supplemental
                    ).rawValue,
                ]
            )
        }
        return [
            "contract": responseContract,
            "action": request.action.rawValue,
            "requestId": request.requestID,
            "ok": true,
            "state": passiveState(status).rawValue,
            "source": jsonNullable(status.source?.rawValue),
            "revision": statusRevision(status),
            "error": NSNull(),
            "cleared": result.cleared,
            "cv": statusRevision(status),
            "page": request.page!,
            "textAuthority": NativeBookOCRTextAuthority.supplemental.rawValue,
        ]
    }

    private static func mutationContext(
        request: Request,
        expectedContentSHA256: String?,
        book: ReaderLocalBookAccess?
    ) throws -> (ReaderLocalBookAccess, String) {
        guard let expectedContentSHA256,
              let book,
              book.record.id == request.localBookID,
              book.record.format == .pdf else {
            throw NativeBookOCRError.unreadableBook
        }
        return (book, expectedContentSHA256)
    }

    private static func mutationReply(
        request: Request,
        page: NativeBookOCRPageCharacters,
        fields: [String: Any]
    ) -> [String: Any] {
        var payload: [String: Any] = [
            "contract": responseContract,
            "action": request.action.rawValue,
            "requestId": request.requestID,
            "ok": true,
            "state": page.status.rawValue,
            "source": jsonNullable(page.source?.rawValue),
            "revision": pageRevision(page),
            "error": NSNull(),
        ]
        payload.merge(fields) { _, incoming in incoming }
        return payload
    }

    private static func failureReply(
        request: Request,
        status: NativeBookOCRBookStatus,
        code: String,
        message: String
    ) -> [String: Any] {
        var payload: [String: Any] = [
            "contract": responseContract,
            "action": request.action.rawValue,
            "requestId": request.requestID,
            "ok": false,
            "state": NativeBookOCRPageState.failed.rawValue,
            "source": jsonNullable(status.source?.rawValue),
            "revision": statusRevision(status),
            "error": errorObject(
                code: code,
                message: message,
                retryable: true
            ),
        ]
        switch request.action {
        case .pageCharacters:
            payload.merge([
                "page": request.page!,
                "pageWidth": 0,
                "pageHeight": 0,
                "chars": [],
                "furigana": [],
                "wordSegmentation": NativeBookOCRWordSegmentationState.unavailable.rawValue,
                "characterGeometry": NativeBookOCRCharacterGeometryState.unavailable.rawValue,
                "formulaCoverage": NativeBookOCRFormulaCoverage.unknown.rawValue,
                "formulaRegions": [],
                "textAuthority": NativeBookOCRTextAuthority.supplemental.rawValue,
            ]) { current, _ in current }
        case .status:
            payload["progress"] = [
                "total": status.totalPages,
                "ready": status.textProgress.completed,
                "pending": status.textProgress.pending,
                "failed": status.textProgress.failed,
                "activePage": jsonNullable(status.currentPage),
                "currentPage": jsonNullable(status.currentPage),
                "textProgress": stageObject(status.textProgress),
                "wordProgress": stageObject(status.wordProgress),
                "formulaProgress": stageObject(status.formulaProgress),
                "formulaPendingRegions": status.formulaPendingRegions,
                "formulaFailedRegions": status.formulaFailedRegions,
            ]
        case .search:
            payload["matches"] = []
            payload["total"] = 0
            payload["pages"] = []
            payload["incomplete"] = true
        case .recognizeSelection:
            payload["page"] = request.page!
            payload["text"] = ""
            payload["cv"] = statusRevision(status)
            payload["persisted"] = false
            payload["textAuthority"] = NativeBookOCRTextAuthority.supplemental.rawValue
        case .reOCRPage:
            payload["page"] = request.page!
            payload["chars"] = 0
            payload["cv"] = statusRevision(status)
            payload["textAuthority"] = NativeBookOCRTextAuthority.supplemental.rawValue
        case .clearReOCRPage:
            payload["page"] = request.page!
            payload["cleared"] = false
            payload["cv"] = statusRevision(status)
            payload["textAuthority"] = NativeBookOCRTextAuthority.supplemental.rawValue
        }
        return payload
    }

    private static func passiveState(
        _ status: NativeBookOCRBookStatus
    ) -> NativeBookOCRPageState {
        switch status.state {
        case .idle, .cancelled:
            return .idle
        case .running, .paused:
            return .pending
        case .failed:
            return .failed
        case .completed:
            return status.textProgress.completed == 0 ? .readyEmpty : .ready
        }
    }

    private static func statusRevision(_ status: NativeBookOCRBookStatus) -> String {
        if status.state == .idle, status.contentSHA256.isEmpty { return "0" }
        return String(Int64(status.updatedAt.timeIntervalSince1970 * 1_000))
    }

    private static func pageRevision(
        _ value: NativeBookOCRPageCharacters
    ) -> String {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .millisecondsSince1970
        encoder.outputFormatting = [.sortedKeys]
        let digest: String
        if let data = try? encoder.encode(value) {
            digest = SHA256.hash(data: data).map {
                String(format: "%02x", $0)
            }.joined()
        } else {
            digest = value.geometryDigest
        }
        // Keep the content digest for cache identity, while exposing the
        // bounded engine revision so the shared selector can distinguish
        // ordinary reading order from manga bubble/column layout.
        let engine = String(value.engineRevision.prefix(72))
        return "\(engine):\(String(digest.prefix(72)))"
    }

    private static func stageObject(
        _ value: NativeBookOCRStageProgress
    ) -> [String: Any] {
        [
            "total": value.total,
            "completed": value.completed,
            "pending": value.pending,
            "failed": value.failed,
            "unavailable": value.unavailable,
        ]
    }

    private static func characterObject(
        _ value: NativeBookOCRCharacter
    ) -> [String: Any] {
        [
            "c": value.c,
            "x0": value.x0,
            "y0": value.y0,
            "x1": value.x1,
            "y1": value.y1,
            "sp": value.sp,
            "w": value.w,
            "b": value.b,
            "bk": value.bk,
        ]
    }

    private static func furiganaObject(
        _ value: NativeBookOCRFurigana
    ) -> [String: Any] {
        var result: [String: Any] = [:]
        if let value = value.wd { result["wd"] = value }
        if let value = value.rt { result["rt"] = value }
        if let value = value.x0 { result["x0"] = value }
        if let value = value.y0 { result["y0"] = value }
        if let value = value.x1 { result["x1"] = value }
        if let value = value.y1 { result["y1"] = value }
        return result
    }

    private static func formulaObject(
        _ value: NativeBookOCRFormulaRegion
    ) -> [String: Any] {
        [
            "id": value.id,
            "x0": value.x0,
            "y0": value.y0,
            "x1": value.x1,
            "y1": value.y1,
            "state": value.state.rawValue,
            "latex": jsonNullable(value.latex),
            "multiline": jsonNullable(value.multiline),
            "error": jsonNullable(value.error.map {
                errorObject(
                    code: "BW_NATIVE_FORMULA_FAILED",
                    message: safeMessage($0),
                    retryable: true
                )
            }),
        ]
    }

    private static func errorObject(
        code: String,
        message: String,
        retryable: Bool
    ) -> [String: Any] {
        ["code": code, "message": safeMessage(message), "retryable": retryable]
    }

    private static func safeMessage(_ value: String) -> String {
        var output = String.UnicodeScalarView()
        for scalar in value.unicodeScalars
            where !CharacterSet.controlCharacters.contains(scalar)
                || scalar.value == 10 {
            output.append(scalar)
            if output.count >= 500 { break }
        }
        return String(output)
    }

    private static func normalizedSHA256(_ value: String?) -> String? {
        guard let value,
              value.range(
                of: #"^[0-9a-fA-F]{64}$"#,
                options: .regularExpression
              ) != nil else {
            return nil
        }
        return value.lowercased()
    }

    private static func jsonNullable<T>(_ value: T?) -> Any {
        value.map { $0 as Any } ?? NSNull()
    }

    private func isTrusted(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == trustedBaseURL.scheme?.lowercased(),
              url.host?.lowercased() == trustedBaseURL.host?.lowercased(),
              Self.effectivePort(url) == Self.effectivePort(trustedBaseURL) else {
            return false
        }
        let basePath = trustedBaseURL.path
        if basePath.hasSuffix("/") {
            return url.path.hasPrefix(basePath)
        }
        return url.path == basePath
    }

    private static func effectivePort(_ url: URL) -> Int? {
        if let port = url.port { return port }
        switch url.scheme?.lowercased() {
        case "http": return 80
        case "https": return 443
        default: return nil
        }
    }
}

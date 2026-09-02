import Combine
import Foundation
import WebKit

enum ReaderPiSyncRunState: String, Equatable, Sendable {
    case complete
    case partial
    case blocked
    case unknown

    var title: String {
        switch self {
        case .complete: return "同步完成"
        case .partial: return "部分完成"
        case .blocked: return "存在冲突，未覆盖"
        case .unknown: return "结果尚未确认"
        }
    }
}

enum ReaderPiDataSyncState: String, Decodable, Equatable, Sendable {
    case complete
    case partial
    case blocked
    case unknown
    case error
}

struct ReaderPiBookSyncReport: Equatable, Sendable {
    var configured = true
    var localCount = 0
    var uploaded = 0
    var unchanged = 0
    var remoteOnly = 0
    var pending = 0
    var failed = 0
    var unknown = 0
    var remoteNewer: [String] = []
    var conflicts: [String] = []

    var blockedCount: Int { remoteNewer.count + conflicts.count }

    var summary: String {
        guard configured else { return "尚未选择本机书库文件夹" }
        var parts = [
            "已上传或关联 \(uploaded) 本",
            "无需上传 \(unchanged) 本",
        ]
        if remoteOnly > 0 { parts.append("仅 Pi \(remoteOnly) 本未下载") }
        if blockedCount > 0 { parts.append("冲突或 Pi 较新 \(blockedCount) 本未覆盖") }
        if pending > 0 { parts.append("待下次同步 \(pending) 本") }
        if failed > 0 { parts.append("本地读取失败 \(failed) 本") }
        if unknown > 0 { parts.append("结果未知 \(unknown) 本") }
        return parts.joined(separator: "；")
    }
}

struct ReaderPiDataSyncReport: Equatable, Sendable {
    let state: ReaderPiDataSyncState
    let owner: String
    let collections: [String]
    let applied: Int
    let pendingLocal: Bool
    let conflictCount: Int
    let errorCode: String
    let retryable: Bool
    /// 真实原因(2026-09-03):只显示错误码时,"可重试失败"盖住了桥/传输层的具体消息。
    var errorMessage: String = ""

    var summary: String {
        switch state {
        case .complete:
            return "设置、词汇与卡片仓库已同步"
        case .partial:
            return pendingLocal
                ? "设置、词汇或卡片仓库仍有待同步变更"
                : "设置、词汇与卡片仓库仅部分同步"
        case .blocked:
            if errorCode == "BW_NATIVE_SYNC_BOOTSTRAP_UNAVAILABLE" {
                return "设置、词汇与卡片仓库尚未接通 Pi；本机数据未受影响"
            }
            if errorCode == "BW_SYNC_REGISTRY_MISMATCH" {
                return "Pi 尚未完成卡片仓库同步升级；本机数据未受影响"
            }
            return conflictCount > 0
                ? "设置、词汇或卡片仓库存在 \(conflictCount) 个冲突，未覆盖"
                : "数据同步 owner 当前暂停"
        case .unknown:
            return "无法确认设置、词汇与卡片仓库的同步结果"
        case .error:
            let messageSuffix = errorMessage.isEmpty ? "" : "：" + errorMessage
            if errorCode.isEmpty {
                return errorMessage.isEmpty
                    ? "设置、词汇与卡片仓库同步失败"
                    : "设置、词汇与卡片仓库同步失败（\(errorMessage)）"
            }
            return "设置、词汇与卡片仓库同步失败（\(errorCode)\(messageSuffix)）"
        }
    }
}

struct ReaderPiSyncReport: Identifiable, Equatable, Sendable {
    static let syncedDataCollections = [
        "card-entities",
        "card-states",
        "user-settings",
        "vocabulary-state",
    ]

    static let unsupportedDomains = [
        "阅读进度",
        "高亮",
        "笔迹",
        "便签",
        "卡片位置",
        "对话记录（联网时从 Pi 在线恢复，不在离线同步包）",
    ]

    let id: String
    let finishedAt: Date
    let state: ReaderPiSyncRunState
    let books: ReaderPiBookSyncReport
    let data: ReaderPiDataSyncReport
    let unsupported: [String]
}

private struct ReaderPiDataSyncWireResult: Decodable {
    let contract: String
    let requestId: String
    let owner: String
    let state: ReaderPiDataSyncState
    let collections: [String]
    let applied: Int
    let pendingLocal: Bool
    let conflictCount: Int
    let errorCode: String
    let retryable: Bool
    let errorMessage: String?
}

private enum ReaderPiSyncError: LocalizedError {
    case pageUnavailable
    case dataContractUnavailable
    case invalidDataResult

    var errorDescription: String? {
        switch self {
        case .pageUnavailable:
            return "Reader 页面尚未准备好，无法同步设置、词汇与卡片仓库"
        case .dataContractUnavailable:
            return "当前 Reader 版本尚未提供安全的数据同步入口"
        case .invalidDataResult:
            return "Reader 返回了无法确认的数据同步结果"
        }
    }
}

@MainActor
protocol ReaderPiDataSyncRunning: AnyObject {
    func run(
        using reader: ReaderWebViewModel,
        requestID: String
    ) async throws -> ReaderPiDataSyncReport
}

@MainActor
private final class ReaderPiWebDataSyncRunner: ReaderPiDataSyncRunning {
    func run(
        using reader: ReaderWebViewModel,
        requestID: String
    ) async throws -> ReaderPiDataSyncReport {
        try await reader.runNativePiDataSync(requestID: requestID)
    }
}

@MainActor
final class ReaderPiSyncCoordinator: ObservableObject {
    enum Phase: Equatable {
        case idle
        case scanning
        case readingPiCatalog
        case uploading(current: Int, total: Int, title: String)
        case syncingData

        var isRunning: Bool { self != .idle }

        var title: String {
            switch self {
            case .idle: return ""
            case .scanning: return "正在扫描本机书库…"
            case .readingPiCatalog: return "正在核对 Pi 书库…"
            case .uploading(let current, let total, let title):
                return "正在上传 \(current)/\(total)：\(title)"
            case .syncingData: return "正在同步设置、词汇与卡片仓库…"
            }
        }
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var report: ReaderPiSyncReport?
    @Published private(set) var errorMessage: String?

    private let localLibrary: ReaderLocalLibraryManager
    private let remoteLibrary: ReaderRemoteLibraryCoordinator
    private let dataSyncRunner: any ReaderPiDataSyncRunning

    init(
        localLibrary: ReaderLocalLibraryManager = .shared,
        remoteLibrary: ReaderRemoteLibraryCoordinator? = nil,
        dataSyncRunner: (any ReaderPiDataSyncRunning)? = nil
    ) {
        self.localLibrary = localLibrary
        self.remoteLibrary = remoteLibrary ?? ReaderRemoteLibraryCoordinator()
        self.dataSyncRunner = dataSyncRunner ?? ReaderPiWebDataSyncRunner()
    }

    var isRunning: Bool { phase.isRunning }

    func syncToPi(using reader: ReaderWebViewModel) async {
        guard !phase.isRunning else { return }
        let requestID = UUID().uuidString
        errorMessage = nil
        var bookReport = ReaderPiBookSyncReport()

        if localLibrary.isConfigured {
            phase = .scanning
            if localLibrary.isScanning {
                bookReport.pending = localLibrary.books.count
                bookReport.failed = 1
            } else {
                await localLibrary.rescan()
                if let message = localLibrary.errorMessage {
                    bookReport.pending = localLibrary.books.count
                    bookReport.failed = 1
                    errorMessage = message
                } else {
                    bookReport = await syncBooks(using: reader)
                }
            }
        } else {
            bookReport.configured = false
        }

        phase = .syncingData
        let dataReport: ReaderPiDataSyncReport
        do {
            dataReport = try await dataSyncRunner.run(
                using: reader,
                requestID: requestID
            )
        } catch {
            dataReport = ReaderPiDataSyncReport(
                state: .unknown,
                owner: "unknown",
                collections: ReaderPiSyncReport.syncedDataCollections,
                applied: 0,
                pendingLocal: true,
                conflictCount: 0,
                errorCode: "BW_PI_SYNC_RESULT_UNKNOWN",
                retryable: false
            )
            if errorMessage == nil {
                errorMessage = error.localizedDescription
            }
        }

        let overall = overallState(books: bookReport, data: dataReport)
        report = ReaderPiSyncReport(
            id: requestID,
            finishedAt: Date(),
            state: overall,
            books: bookReport,
            data: dataReport,
            unsupported: ReaderPiSyncReport.unsupportedDomains
        )
        phase = .idle
    }

    private func syncBooks(
        using reader: ReaderWebViewModel
    ) async -> ReaderPiBookSyncReport {
        var summary = ReaderPiBookSyncReport()
        let localBooks = localLibrary.books
        summary.localCount = localBooks.count
        phase = .readingPiCatalog
        remoteLibrary.dismissMessages()
        let cookies = await reader.remoteLibraryCookies()
        await remoteLibrary.refresh(
            cookies: cookies,
            localLibrary: localLibrary
        )
        if let message = remoteLibrary.errorMessage {
            summary.pending = localBooks.count
            summary.unknown = localBooks.isEmpty ? 0 : 1
            errorMessage = message
            return summary
        }

        summary.remoteOnly = remoteLibrary.books.filter {
            remoteLibrary.syncState(for: $0) == .piOnly
        }.count
        var uploadOutcomeUnknown = false
        for (offset, localBook) in localBooks.enumerated() {
            let state = remoteLibrary.syncState(for: localBook)
            switch state {
            case .synced:
                summary.unchanged += 1
            case .piNewer:
                summary.remoteNewer.append(localBook.title)
            case .conflict, .piOnly:
                summary.conflicts.append(localBook.title)
            case .localOnly, .localNewer:
                guard !uploadOutcomeUnknown else {
                    summary.pending += 1
                    continue
                }
                phase = .uploading(
                    current: offset + 1,
                    total: localBooks.count,
                    title: localBook.title
                )
                let digest: String
                do {
                    digest = try await localLibrary.ensureContentSHA256(
                        for: localBook
                    )
                } catch {
                    summary.failed += 1
                    continue
                }
                guard let uploaded = await remoteLibrary.upload(
                    localBook,
                    localLibrary: localLibrary,
                    cookies: cookies
                ) else {
                    // The request may have reached Pi. Stop further uploads and
                    // force the next tap to recatalogue by SHA before retrying.
                    summary.unknown += 1
                    uploadOutcomeUnknown = true
                    errorMessage = remoteLibrary.errorMessage
                        ?? "无法确认 Pi 是否已收到该书；下次同步会先重新核对"
                    continue
                }
                guard uploaded.contentSha256.caseInsensitiveCompare(digest)
                        == .orderedSame else {
                    summary.unknown += 1
                    uploadOutcomeUnknown = true
                    errorMessage = "Pi 返回的书籍摘要与本机不一致，已停止后续上传"
                    continue
                }
                summary.uploaded += 1
            }
        }
        return summary
    }

    private func overallState(
        books: ReaderPiBookSyncReport,
        data: ReaderPiDataSyncReport
    ) -> ReaderPiSyncRunState {
        if books.unknown > 0 || data.state == .unknown {
            return .unknown
        }
        if books.blockedCount > 0 || data.state == .blocked {
            return .blocked
        }
        if books.failed > 0 || books.pending > 0 ||
            data.state == .partial || data.state == .error ||
            !ReaderPiSyncReport.unsupportedDomains.isEmpty {
            return .partial
        }
        return .complete
    }
}

@MainActor
private extension ReaderWebViewModel {
    func runNativePiDataSync(
        requestID: String
    ) async throws -> ReaderPiDataSyncReport {
        guard webView.url != nil else {
            throw ReaderPiSyncError.pageUnavailable
        }
        let value = try await webView.callAsyncJavaScript(
            """
            const runtime = globalThis.BWReaderRuntime || {};
            const request = {
              contract: "reader-pi-sync-request/1",
              requestId
            };
            var syncNow = null;
            if (runtime.nativeLocalRuntime &&
                typeof runtime.nativeLocalRuntime.syncNow === "function") {
              syncNow = runtime.nativeLocalRuntime.syncNow.bind(
                runtime.nativeLocalRuntime
              );
            } else if (runtime.pwaRuntime &&
                       typeof runtime.pwaRuntime.syncControl === "function") {
              const control = runtime.pwaRuntime.syncControl();
              if (control && typeof control.syncNow === "function") {
                syncNow = control.syncNow.bind(control);
              }
            }
            if (!syncNow) return "";
            try {
              return JSON.stringify(await syncNow(request));
            } catch (error) {
              const rawCode = String(error && error.code || "");
              const errorCode = /^[A-Z][A-Z0-9_]{0,79}$/.test(rawCode)
                ? rawCode : "BW_NATIVE_SYNC";
              return JSON.stringify({
                contract: "reader-pi-data-sync-result/1",
                requestId,
                owner: "native-app",
                state: errorCode === "BW_SYNC_REGISTRY_MISMATCH"
                  ? "blocked" : "error",
                collections,
                applied: 0,
                pendingLocal: true,
                conflictCount: 0,
                errorCode,
                // 真实原因(2026-09-03):syncNow 抛出时此前只带码,桥的"哪条 change 哪个字段"全丢
                errorMessage: String(error && error.message || "").replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, 200),
                retryable: error && error.retryable === true
              });
            }
            """,
            arguments: [
                "requestId": requestID,
                "collections": ReaderPiSyncReport.syncedDataCollections,
            ],
            in: nil,
            contentWorld: .page
        )
        guard let json = value as? String, !json.isEmpty else {
            throw ReaderPiSyncError.dataContractUnavailable
        }
        let decoded = try JSONDecoder().decode(
            ReaderPiDataSyncWireResult.self,
            from: Data(json.utf8)
        )
        let safeCollections = decoded.collections.filter {
            !$0.isEmpty && $0.count <= 80 &&
                $0.range(of: "^[A-Za-z0-9._-]+$", options: .regularExpression)
                    != nil
        }
        let safeOwners = ["native-app", "pwa", "extension-background"]
        guard decoded.contract == "reader-pi-data-sync-result/1",
              decoded.requestId == requestID,
              safeOwners.contains(decoded.owner),
              safeCollections.count == decoded.collections.count,
              safeCollections == ReaderPiSyncReport.syncedDataCollections,
              decoded.applied >= 0,
              decoded.conflictCount >= 0,
              decoded.errorCode.isEmpty ||
                decoded.errorCode.range(
                    of: "^[A-Z][A-Z0-9_]{0,79}$",
                    options: .regularExpression
                ) != nil else {
            throw ReaderPiSyncError.invalidDataResult
        }
        // 对话仍由 Pi 按既有 normal/review/EPUB scope 权威保存，不加入
        // DataRegistry。它与本次离线 collection sync 的 complete/partial/blocked/error
        // 终态互相独立：只要服务端返回了可解析报告，就让当前页面尝试一次在线历史刷新。
        // 直接调用组件的有界入口，不广播可由宿主网页伪造并反复触发的 window CustomEvent。
        _ = try? await webView.callAsyncJavaScript(
            """
            if (window.RC && RC.assistant &&
                typeof RC.assistant.onNativePiSyncFinished === "function") {
              return RC.assistant.onNativePiSyncFinished();
            }
            return false;
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        return ReaderPiDataSyncReport(
            state: decoded.state,
            owner: decoded.owner,
            collections: safeCollections,
            applied: decoded.applied,
            pendingLocal: decoded.pendingLocal,
            conflictCount: decoded.conflictCount,
            errorCode: decoded.errorCode,
            retryable: decoded.retryable,
            errorMessage: String((decoded.errorMessage ?? "").prefix(200))
        )
    }
}

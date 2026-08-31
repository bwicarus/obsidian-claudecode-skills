import CryptoKit
import FlyingFox
import FlyingSocks
import Foundation
import PDFKit
import Security
import UIKit

enum ReaderLocalRuntimeError: LocalizedError {
    case bundleUnavailable
    case serverUnavailable(String)
    case invalidBook
    case epubTooLarge
    case bookChanged

    var errorDescription: String? {
        switch self {
        case .bundleUnavailable:
            return "BW_LOCAL_BUNDLE_UNAVAILABLE：本机 Reader 资源不完整"
        case .serverUnavailable(let detail):
            return "BW_LOCAL_SERVER_UNAVAILABLE：\(detail)"
        case .invalidBook:
            return "BW_LOCAL_BOOK_UNAVAILABLE：本机书籍不可用"
        case .epubTooLarge:
            return "BW_LOCAL_EPUB_TOO_LARGE：EPUB 超过 512 MiB，无法在本机安全解包"
        case .bookChanged:
            return "BW_LOCAL_BOOK_CHANGED：本机书籍已变化，请刷新书库后重试"
        }
    }
}

// Access is immutable after construction and its lifetime is the authority:
// retaining it keeps the security-scoped root open until the reading session
// replaces the current book.
extension ReaderLocalBookAccess: @unchecked Sendable {
    func validateCurrentFile(maximumEPUBBytes: Int64) throws {
        let values = try url.resourceValues(forKeys: [
            .isRegularFileKey, .isReadableKey, .isSymbolicLinkKey,
            .fileSizeKey, .contentModificationDateKey,
        ])
        guard values.isRegularFile == true,
              values.isReadable != false,
              values.isSymbolicLink != true,
              Int64(values.fileSize ?? -1) == record.byteCount,
              values.contentModificationDate == record.modifiedAt else {
            throw ReaderLocalRuntimeError.bookChanged
        }
        if record.format == .epub, record.byteCount > maximumEPUBBytes {
            throw ReaderLocalRuntimeError.epubTooLarge
        }
    }
}

private actor ReaderLocalRuntimeState {
    private var current: ReaderLocalBookAccess?

    func replace(with access: ReaderLocalBookAccess) {
        current = access
    }

    func access(for opaqueBookID: String) -> ReaderLocalBookAccess? {
        guard current?.record.id == opaqueBookID else { return nil }
        return current
    }
}

private enum ReaderNativePDFPageRenderError: LocalizedError {
    case documentUnavailable
    case pageUnavailable
    case imageUnavailable

    var errorDescription: String? {
        switch self {
        case .documentUnavailable:
            return "PDFKit 无法打开这本书"
        case .pageUnavailable:
            return "PDF 页码不可用"
        case .imageUnavailable:
            return "PDFKit 无法渲染这一页"
        }
    }
}

/// Serializes PDFKit access and keeps only the active document plus a bounded
/// near-page JPEG cache. WebKit remains the DocumentHost/overlay owner; this
/// actor replaces only PDF.js page-pixel work for native local PDFs.
private actor ReaderNativePDFPageRenderer {
    private var documentIdentity: String?
    private var document: PDFDocument?
    private let imageCache = NSCache<NSString, NSData>()

    init() {
        imageCache.countLimit = 24
        imageCache.totalCostLimit = 96 * 1_024 * 1_024
    }

    func jpegData(
        for access: ReaderLocalBookAccess,
        pageNumber: Int,
        pixelWidth: Int
    ) throws -> Data {
        let record = access.record
        let identity = [
            record.id,
            record.contentFingerprint,
            String(record.byteCount),
            String(record.modifiedAt?.timeIntervalSince1970 ?? 0),
        ].joined(separator: ":")
        if documentIdentity != identity {
            guard let opened = PDFDocument(url: access.url),
                  opened.pageCount > 0 else {
                throw ReaderNativePDFPageRenderError.documentUnavailable
            }
            documentIdentity = identity
            document = opened
            imageCache.removeAllObjects()
        }
        guard let document,
              pageNumber >= 1,
              pageNumber <= document.pageCount,
              let page = document.page(at: pageNumber - 1) else {
            throw ReaderNativePDFPageRenderError.pageUnavailable
        }

        let cacheKey = "\(identity):p\(pageNumber):w\(pixelWidth)" as NSString
        if let cached = imageCache.object(forKey: cacheKey) {
            return cached as Data
        }

        let bounds = page.bounds(for: .cropBox)
        guard bounds.width > 0, bounds.height > 0 else {
            throw ReaderNativePDFPageRenderError.imageUnavailable
        }
        let requestedWidth = CGFloat(pixelWidth)
        let requestedHeight = requestedWidth * bounds.height / bounds.width
        let maximumDimension: CGFloat = 4_096
        let maximumPixels: CGFloat = 12 * 1_024 * 1_024
        let dimensionScale = min(
            1,
            maximumDimension / max(requestedWidth, requestedHeight)
        )
        let pixelScale = min(
            dimensionScale,
            (maximumPixels / (requestedWidth * requestedHeight)).squareRoot()
        )
        let targetSize = CGSize(
            width: max(1, floor(requestedWidth * pixelScale)),
            height: max(1, floor(requestedHeight * pixelScale))
        )
        let thumbnail = page.thumbnail(of: targetSize, for: .cropBox)
        guard thumbnail.size.width > 0, thumbnail.size.height > 0 else {
            throw ReaderNativePDFPageRenderError.imageUnavailable
        }
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        let canvasBounds = CGRect(origin: .zero, size: targetSize)
        let image = UIGraphicsImageRenderer(
            size: targetSize,
            format: format
        ).image { context in
            context.cgContext.setFillColor(UIColor.white.cgColor)
            context.cgContext.fill(canvasBounds)
            thumbnail.draw(in: canvasBounds)
        }
        guard let data = image.jpegData(compressionQuality: 0.9) else {
            throw ReaderNativePDFPageRenderError.imageUnavailable
        }
        imageCache.setObject(data as NSData, forKey: cacheKey, cost: data.count)
        return data
    }
}

private struct ReaderLocalBundleVerificationInput: Sendable {
    let bundleRoot: URL
    let files: [String: String]
    let validatesDigests: Bool
}

private struct ReaderNativeVisualDeliveryRequest: Sendable {
    let callID: String
    let clientSecret: String
    let tool: String
}

private enum ReaderLocalBundleIntegrity {
    static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func verify(_ input: ReaderLocalBundleVerificationInput) throws {
        let rootPath = input.bundleRoot.path.hasSuffix("/")
            ? input.bundleRoot.path : input.bundleRoot.path + "/"
        for (relative, expectedDigest) in input.files {
            guard ReaderLocalHTTPHandler.isCanonicalRelativePath(relative),
                  expectedDigest.count == 64,
                  expectedDigest.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) else {
                throw ReaderLocalRuntimeError.bundleUnavailable
            }
            let fileURL = relative.split(separator: "/").reduce(input.bundleRoot) {
                $0.appendingPathComponent(String($1), isDirectory: false)
            }.resolvingSymlinksInPath().standardizedFileURL
            guard fileURL.path.hasPrefix(rootPath),
                  FileManager.default.fileExists(atPath: fileURL.path) else {
                throw ReaderLocalRuntimeError.bundleUnavailable
            }
            guard input.validatesDigests else { continue }
            guard let bytes = try? Data(
                contentsOf: fileURL,
                options: .mappedIfSafe
            ) else {
                throw ReaderLocalRuntimeError.bundleUnavailable
            }
            let actualDigest = SHA256.hash(data: bytes).map {
                String(format: "%02x", $0)
            }.joined()
            guard actualDigest == expectedDigest else {
                throw ReaderLocalRuntimeError.bundleUnavailable
            }
        }
    }
}

private struct ReaderLocalHTTPHandler: HTTPHandler {
    private static let openAIRealtimeOrigin = "https://api.openai.com"

    let capabilityToken: String
    let cspNonce: String
    let bundleRoot: URL
    let allowedStaticFiles: Set<String>
    let staticRevision: String
    let state: ReaderLocalRuntimeState
    let piProxyBroker: ReaderNativePiProxyBroker
    let imageProxyBroker: ReaderNativeImageProxyBroker
    let pageRenderer: ReaderNativePDFPageRenderer
    let visualCaptureBroker: ReaderNativeVisualCaptureBroker

    func handleRequest(_ request: HTTPRequest) async throws -> HTTPResponse {
        guard request.method == .GET
                || request.method == .HEAD
                || request.method == .POST else {
            return response(
                status: .methodNotAllowed,
                text: "method not allowed",
                headers: [HTTPHeader("Allow"): "GET, HEAD, POST"]
            )
        }
        guard request.headers[.host]?.lowercased()
                == "\(ReaderLocalRuntimeServer.host):\(ReaderLocalRuntimeServer.port)" else {
            return response(status: .forbidden, text: "invalid host")
        }

        let encodedPath = request.target.path(percentEncoded: true)
        let lowered = encodedPath.lowercased()
        guard !lowered.contains("%2e"), !lowered.contains("%2f"),
              !lowered.contains("%5c"),
              let decodedPath = encodedPath.removingPercentEncoding,
              !decodedPath.contains("\\"), !decodedPath.contains("\0") else {
            return response(status: .badRequest, text: "invalid path")
        }
        let nativeVisualPath = "/r/\(capabilityToken)/native-api/visual-capture"
        if request.method == .POST, decodedPath != nativeVisualPath {
            return response(
                status: .methodNotAllowed,
                text: "POST is restricted to native visual delivery",
                headers: [HTTPHeader("Allow"): "GET, HEAD"]
            )
        }

        if decodedPath == "/pdf/api/toc" {
            guard trustedResourceSurface(
                referer: request.headers[HTTPHeader("Referer")]
            ) == .pdf else {
                return response(status: .forbidden, text: "invalid referer")
            }
            return await serveNativeTOC(request)
        }

        if decodedPath == "/pdf/api/page-image",
           trustedResourceSurface(
                referer: request.headers[HTTPHeader("Referer")]
           ) == .pdf {
            guard let fileIdentity = request.query["file"],
                  let opaqueBookID = Self.opaqueBookID(
                    fromLocalFileIdentity: fileIdentity
                  ) else {
                return response(status: .badRequest, text: "invalid local book")
            }
            return await serveNativePageImage(
                request,
                opaqueBookID: opaqueBookID
            )
        }

        if decodedPath == "/pdf/api/card-asset" {
            guard request.method == .GET else {
                return response(
                    status: .methodNotAllowed,
                    text: "method not allowed",
                    headers: [HTTPHeader("Allow"): "GET"]
                )
            }
            guard trustedResourceSurface(
                referer: request.headers[HTTPHeader("Referer")]
            ) != nil else {
                return response(status: .forbidden, text: "invalid referer")
            }
            return await serveNativeCardAsset(request)
        }

        if decodedPath == "/pdf/api/img-proxy" {
            guard request.method == .GET else {
                return response(
                    status: .methodNotAllowed,
                    text: "method not allowed",
                    headers: [HTTPHeader("Allow"): "GET"]
                )
            }
            guard trustedResourceSurface(
                referer: request.headers[HTTPHeader("Referer")]
            ) != nil else {
                return response(status: .forbidden, text: "invalid referer")
            }
            return await serveNativeImageProxy(request)
        }

        if Self.isDirectPiResourcePath(decodedPath) {
            guard request.method == .GET else {
                return response(
                    status: .methodNotAllowed,
                    text: "method not allowed",
                    headers: [HTTPHeader("Allow"): "GET"]
                )
            }
            guard let surface = trustedResourceSurface(
                referer: request.headers[HTTPHeader("Referer")]
            ) else {
                return response(status: .forbidden, text: "invalid referer")
            }
            do {
                var result = try await piProxyBroker.responseForResource(
                    ReaderNativePiResourceProxyRequest(
                        requestTarget: request.target.rawValue,
                        surface: surface,
                        accept: request.headers[HTTPHeader("Accept")] ?? "*/*",
                        range: request.headers[.range]
                    )
                )
                Self.secure(&result)
                return result
            } catch {
                return response(
                    status: .badGateway,
                    text: error.localizedDescription,
                    headers: [
                        HTTPHeader("X-BW-Reader-Error"): "pi-resource-unavailable"
                    ]
                )
            }
        }

        if decodedPath.hasPrefix("/static/") {
            return try await serveStatic(request, decodedPath: decodedPath)
        }

        let prefix = "/r/\(capabilityToken)/"
        guard decodedPath.hasPrefix(prefix) else {
            return response(status: .notFound, text: "not found")
        }
        let relative = String(decodedPath.dropFirst(prefix.count))
        guard Self.isCanonicalRelativePath(relative) else {
            return response(status: .badRequest, text: "invalid path")
        }

        switch relative {
        case "empty.pdf":
            return dataResponse(
                request,
                data: Self.emptyPDF(),
                contentType: "application/pdf",
                cacheControl: "no-store"
            )
        case "shells/pdf.html", "shells/epub.html":
            return await serveShell(request, relative: relative)
        case "native-api/book-meta":
            return await serveBookMeta(request)
        case "native-api/visual-capture":
            return await serveNativeVisualCapture(request)
        default:
            if relative.hasPrefix("native-api/offline-dictionary/") {
                return serveOfflineDictionary(
                    request,
                    relative: String(
                        relative.dropFirst(
                            "native-api/offline-dictionary/".count
                        )
                    )
                )
            }
            break
        }

        let segments = relative.split(separator: "/", omittingEmptySubsequences: false)
        if segments.count == 2,
           segments[0] == Substring(ReaderNativePiProxyBroker.routeComponent) {
            guard request.method == .GET else {
                return response(
                    status: .methodNotAllowed,
                    text: "method not allowed",
                    headers: [HTTPHeader("Allow"): "GET"]
                )
            }
            do {
                var result = try await piProxyBroker.response(
                    for: String(segments[1])
                )
                Self.secure(&result)
                return result
            } catch {
                return response(
                    status: .badGateway,
                    text: error.localizedDescription,
                    headers: [
                        HTTPHeader("X-BW-Reader-Error"): "pi-stream-unavailable"
                    ]
                )
            }
        }
        if segments.count == 3,
           segments[0] == "books", segments[2] == "content" {
            return try await serveBook(
                request,
                opaqueBookID: String(segments[1])
            )
        }
        // Token-prefixed static URLs are accepted for relative shell assets,
        // but still use the exact signed manifest whitelist.
        if relative.hasPrefix("static/") {
            return try await serveStatic(request, decodedPath: "/\(relative)")
        }
        return response(status: .notFound, text: "not found")
    }

    private func trustedResourceSurface(
        referer: String?
    ) -> ReaderNativeInterfaceSurface? {
        guard let url = trustedResourceURL(referer: referer) else {
            return nil
        }
        let prefix = "/r/\(capabilityToken)/shells/"
        if url.path == "\(prefix)pdf.html" { return .pdf }
        if url.path == "\(prefix)epub.html" { return .epub }
        return nil
    }

    private func trustedResourceURL(referer: String?) -> URL? {
        guard let referer, referer.utf8.count <= 4_096,
              let url = URL(string: referer),
              url.scheme?.lowercased() == "http",
              url.host?.lowercased() == ReaderLocalRuntimeServer.host,
              url.port == Int(ReaderLocalRuntimeServer.port),
              url.user == nil, url.password == nil,
              url.fragment == nil else {
            return nil
        }
        let prefix = "/r/\(capabilityToken)/shells/"
        guard url.path == "\(prefix)pdf.html"
                || url.path == "\(prefix)epub.html" else {
            return nil
        }
        return url
    }

    private static func isDirectPiResourcePath(_ path: String) -> Bool {
        if [
            "/pdf/api/page-image",
            "/pdf/api/figure-crop",
            "/pdf/api/reader-events",
            "/pdf/api/vocab-audio",
        ].contains(path) {
            return true
        }
        return path.hasPrefix("/pdf/api/asset/")
            && path.count > "/pdf/api/asset/".count
            || path.hasPrefix("/api/assistant/voice-clip/")
            && path.count > "/api/assistant/voice-clip/".count
    }

    /// 卡片图片：本地资产优先，未命中拉桥留底一份再回（渲染即同步）——
    /// 「数据都 App 本地化」的图片半边（用户 2026-09-01）。桥离线只影响
    /// 还没同步过的图；已同步的永远本地秒回。
    /// 缓存头用 7 天而不是 1 年：本地 store 自身有 512MB 上限管容量，
    /// HTTP 缓存层再存一份是双份 —— 1 年 immutable 是 54GB 事故的老路。
    private func serveNativeCardAsset(
        _ request: HTTPRequest
    ) async -> HTTPResponse {
        guard let assetID = request.query["id"],
              ReaderCardAssetLocalStore.isValidAssetID(assetID) else {
            return response(
                status: .badRequest,
                text: "invalid card asset id",
                headers: [
                    HTTPHeader("X-BW-Reader-Error"): "invalid-card-asset-id"
                ]
            )
        }
        if let hit = await ReaderCardAssetLocalStore.shared.load(assetID) {
            return dataResponse(
                request,
                data: hit.data,
                contentType: hit.contentType,
                cacheControl: "private, max-age=604800, immutable",
                additionalHeaders: [
                    HTTPHeader("X-BW-Card-Asset"): "local-hit"
                ]
            )
        }
        let bridgeURL =
            "https://bwicarus-2.taile44d0c.ts.net/reader-card-asset/"
            + assetID
        do {
            let payload = try await imageProxyBroker.fetch(rawURL: bridgeURL)
            await ReaderCardAssetLocalStore.shared.save(
                assetID, data: payload.data, contentType: payload.contentType)
            await MainActor.run {
                ReaderImageProxyHealth.noteSuccess(host: "card-asset:bridge")
            }
            return dataResponse(
                request,
                data: payload.data,
                contentType: payload.contentType,
                cacheControl: "private, max-age=604800, immutable",
                additionalHeaders: [
                    HTTPHeader("X-BW-Card-Asset"): "bridge-fill"
                ]
            )
        } catch let error as ReaderNativeImageProxyError {
            await MainActor.run {
                ReaderImageProxyHealth.noteFailure(
                    code: error.diagnosticCode, host: "card-asset:bridge")
            }
            let phrase = HTTPURLResponse.localizedString(
                forStatusCode: error.httpStatus
            ).capitalized
            return response(
                status: HTTPStatusCode(error.httpStatus, phrase: phrase),
                text: error.localizedDescription,
                headers: [
                    HTTPHeader("X-BW-Reader-Error"): error.diagnosticCode
                ]
            )
        } catch {
            return response(
                status: .badGateway,
                text: "card asset unavailable",
                headers: [
                    HTTPHeader("X-BW-Reader-Error"): "card-asset-unavailable"
                ]
            )
        }
    }

    private func serveNativeImageProxy(
        _ request: HTTPRequest
    ) async -> HTTPResponse {
        guard let rawURL = request.query["url"] else {
            return response(
                status: .forbidden,
                text: "BW_NATIVE_IMAGE_URL：缺少图片地址",
                headers: [
                    HTTPHeader("X-BW-Reader-Error"): "invalid-url"
                ]
            )
        }
        // 健康行只记 host 不记完整 URL —— 排查要的是"哪个源、什么错"。
        let host = URLComponents(string: rawURL)?.host ?? "?"
        do {
            let payload = try await imageProxyBroker.fetch(rawURL: rawURL)
            await MainActor.run {
                ReaderImageProxyHealth.noteSuccess(host: host)
            }
            return dataResponse(
                request,
                data: payload.data,
                contentType: payload.contentType,
                cacheControl: "private, max-age=604800, immutable",
                additionalHeaders: [
                    HTTPHeader("X-BW-Native-Image-Proxy"): "local/1"
                ]
            )
        } catch let error as ReaderNativeImageProxyError {
            await MainActor.run {
                ReaderImageProxyHealth.noteFailure(
                    code: error.diagnosticCode, host: host)
            }
            let phrase = HTTPURLResponse.localizedString(
                forStatusCode: error.httpStatus
            ).capitalized
            return response(
                status: HTTPStatusCode(error.httpStatus, phrase: phrase),
                text: error.localizedDescription,
                headers: [
                    HTTPHeader("X-BW-Reader-Error"): error.diagnosticCode
                ]
            )
        } catch {
            await MainActor.run {
                ReaderImageProxyHealth.noteFailure(
                    code: "transport", host: host)
            }
            return response(
                status: .badGateway,
                text: "BW_NATIVE_IMAGE_TRANSPORT：图片请求失败",
                headers: [
                    HTTPHeader("X-BW-Reader-Error"): "transport"
                ]
            )
        }
    }

    private func serveBookMeta(_ request: HTTPRequest) async -> HTTPResponse {
        guard request.method == .GET || request.method == .HEAD else {
            return jsonResponse(
                request,
                status: .methodNotAllowed,
                object: ["ok": false, "error": "method not allowed"],
                additionalHeaders: [HTTPHeader("Allow"): "GET, HEAD"]
            )
        }
        guard let opaqueBookID = request.query["book"],
              Self.isOpaqueBookID(opaqueBookID),
              let access = await state.access(for: opaqueBookID),
              access.record.format == .pdf else {
            return jsonResponse(
                request,
                status: .notFound,
                object: ["ok": false, "error": "book unavailable"]
            )
        }
        do {
            try access.validateCurrentFile(
                maximumEPUBBytes: ReaderLocalRuntimeServer.maximumEPUBBytes
            )
        } catch {
            return jsonResponse(
                request,
                status: .conflict,
                object: ["ok": false, "error": error.localizedDescription]
            )
        }
        guard let document = PDFDocument(url: access.url),
              document.pageCount > 0,
              let firstPage = document.page(at: 0) else {
            return jsonResponse(
                request,
                status: .badRequest,
                object: ["ok": false, "error": "PDF metadata unavailable"]
            )
        }
        // PyMuPDF's legacy `page.rect` describes the visible, rotated page.
        // PDFKit's crop box is the matching native rendering geometry and is
        // also the coordinate space used by the local OCR pipeline.
        let bounds = firstPage.bounds(for: .cropBox)
        let modifiedAt = access.record.modifiedAt
            ?? Date(timeIntervalSince1970: 0)
        return jsonResponse(
            request,
            status: .ok,
            object: [
                "ok": true,
                "page_count": document.pageCount,
                "page_w": Double(bounds.width),
                "page_h": Double(bounds.height),
                "mtime": Int(modifiedAt.timeIntervalSince1970),
            ]
        )
    }

    private func serveOfflineDictionary(
        _ request: HTTPRequest,
        relative: String
    ) -> HTTPResponse {
        guard request.method == .GET || request.method == .HEAD else {
            return response(
                status: .methodNotAllowed,
                text: "method not allowed",
                headers: [HTTPHeader("Allow"): "GET, HEAD"]
            )
        }
        guard trustedResourceSurface(
            referer: request.headers[HTTPHeader("Referer")]
        ) != nil else {
            return response(status: .forbidden, text: "invalid referer")
        }
        do {
            let data = try ReaderOfflineDictionaryStore.readRuntimeResource(
                relative: relative
            )
            return dataResponse(
                request,
                data: data,
                contentType: "application/json; charset=utf-8",
                cacheControl: "no-store",
                additionalHeaders: [
                    HTTPHeader("X-BW-Dictionary-Storage"): "app-private"
                ]
            )
        } catch ReaderOfflineDictionaryError.notInstalled {
            return response(
                status: .notFound,
                text: "BW_OFFLINE_DICTIONARY_NOT_INSTALLED",
                headers: [
                    HTTPHeader("X-BW-Reader-Error"):
                        "BW_OFFLINE_DICTIONARY_NOT_INSTALLED"
                ]
            )
        } catch {
            return response(
                status: .conflict,
                text: error.localizedDescription,
                headers: [
                    HTTPHeader("X-BW-Reader-Error"):
                        "BW_OFFLINE_DICTIONARY_INVALID"
                ]
            )
        }
    }

    private func serveNativeVisualCapture(
        _ request: HTTPRequest
    ) async -> HTTPResponse {
        let delivery: ReaderNativeVisualDeliveryRequest?
        if request.query["deliver"] == "realtime" {
            guard request.method == .POST else {
                return response(
                    status: .methodNotAllowed,
                    text: "native visual delivery requires POST",
                    headers: [HTTPHeader("Allow"): "POST"]
                )
            }
            guard let parsed = await nativeVisualDeliveryRequest(request) else {
                return jsonResponse(
                    request,
                    status: .badRequest,
                    object: [
                        "ok": false,
                        "error": "原生合成图直投请求无效",
                        "stage": "delivery-request",
                    ],
                    additionalHeaders: [
                        HTTPHeader("X-BW-Reader-Error"):
                            "BW_NATIVE_VISUAL_DELIVERY_REQUEST_INVALID"
                    ]
                )
            }
            delivery = parsed
        } else if request.query["deliver"] == nil {
            guard request.method == .GET || request.method == .HEAD else {
                return response(
                    status: .methodNotAllowed,
                    text: "native visual capture requires GET",
                    headers: [HTTPHeader("Allow"): "GET, HEAD"]
                )
            }
            delivery = nil
        } else {
            return jsonResponse(
                request,
                status: .badRequest,
                object: [
                    "ok": false,
                    "error": "原生合成图投递模式无效",
                    "stage": "delivery-request",
                ],
                additionalHeaders: [
                    HTTPHeader("X-BW-Reader-Error"):
                        "BW_NATIVE_VISUAL_DELIVERY_REQUEST_INVALID"
                ]
            )
        }
        let queryNames = request.query.map(\.name)
        guard Set(queryNames).count == queryNames.count,
              queryNames.allSatisfy({
                  ["scope", "deliver", "page", "x", "y", "w", "h"]
                      .contains($0)
              }) else {
            return response(
                status: .badRequest,
                text: "native visual capture query is invalid",
                headers: [
                    HTTPHeader("X-BW-Reader-Error"):
                        "BW_NATIVE_VISUAL_QUERY_INVALID"
                ]
            )
        }
        let referer = request.headers[HTTPHeader("Referer")]
        guard let trustedDocumentURL = trustedResourceURL(referer: referer),
              let trustedSurface = trustedResourceSurface(referer: referer) else {
            return response(
                status: .forbidden,
                text: "native visual capture requires a trusted reader shell",
                headers: [
                    HTTPHeader("X-BW-Reader-Error"):
                        "BW_NATIVE_VISUAL_UNTRUSTED"
                ]
            )
        }
        let scope = request.query["scope"] ?? "viewport"
        let region: ReaderNativeVisualCaptureRegion?
        if scope == "viewport" {
            guard request.query["x"] == nil,
                  request.query["y"] == nil,
                  request.query["w"] == nil,
                  request.query["h"] == nil else {
                return nativeVisualErrorResponse(
                    .invalidRegion,
                    status: .badRequest
                )
            }
            region = nil
        } else if scope == "region",
                  let x = Double(request.query["x"] ?? ""),
                  let y = Double(request.query["y"] ?? ""),
                  let width = Double(request.query["w"] ?? ""),
                  let height = Double(request.query["h"] ?? "") {
            region = ReaderNativeVisualCaptureRegion(
                x: x,
                y: y,
                width: width,
                height: height
            )
        } else if scope == "page" {
            guard trustedSurface == .pdf else {
                return nativeVisualErrorResponse(
                    .pageScopeUnsupported,
                    status: .badRequest
                )
            }
            guard let pageNumber = Int(request.query["page"] ?? ""),
                  pageNumber >= 1 else {
                return nativeVisualErrorResponse(
                    .invalidPage,
                    status: .badRequest
                )
            }
            let cropValues = ["x", "y", "w", "h"].map {
                request.query[$0]
            }
            let pageRegion: ReaderNativeVisualCaptureRegion?
            if cropValues.allSatisfy({ $0 == nil }) {
                pageRegion = nil
            } else if let x = Double(request.query["x"] ?? ""),
                      let y = Double(request.query["y"] ?? ""),
                      let width = Double(request.query["w"] ?? ""),
                      let height = Double(request.query["h"] ?? "") {
                pageRegion = ReaderNativeVisualCaptureRegion(
                    x: x,
                    y: y,
                    width: width,
                    height: height
                )
            } else {
                return nativeVisualErrorResponse(
                    .invalidRegion,
                    status: .badRequest
                )
            }
            return await serveNativePDFPageVisualCapture(
                request,
                pageNumber: pageNumber,
                region: pageRegion,
                trustedDocumentURL: trustedDocumentURL,
                delivery: delivery
            )
        } else {
            return nativeVisualErrorResponse(
                .invalidRegion,
                status: .badRequest
            )
        }

        do {
            let capture = try await visualCaptureBroker.capture(region: region)
            return await nativeVisualCaptureResponse(
                request,
                capture: capture,
                captureContract: "native-hierarchy/1",
                ink: nil,
                delivery: delivery
            )
        } catch let error as ReaderNativeVisualCaptureError {
            return nativeVisualErrorResponse(
                error,
                status: nativeVisualStatus(for: error)
            )
        } catch {
            return response(
                status: .internalServerError,
                text: "原生合成图：未分类失败",
                headers: [
                    HTTPHeader("X-BW-Reader-Error"):
                        "BW_NATIVE_VISUAL_UNKNOWN"
                ]
            )
        }
    }

    private func serveNativePDFPageVisualCapture(
        _ request: HTTPRequest,
        pageNumber: Int,
        region: ReaderNativeVisualCaptureRegion?,
        trustedDocumentURL: URL,
        delivery: ReaderNativeVisualDeliveryRequest?
    ) async -> HTTPResponse {
        let bookValues = URLComponents(
            url: trustedDocumentURL,
            resolvingAgainstBaseURL: false
        )?.queryItems?
            .filter { $0.name == "book" }
            .compactMap(\.value) ?? []
        guard bookValues.count == 1,
              let opaqueBookID = bookValues.first,
              Self.isOpaqueBookID(opaqueBookID),
              let access = await state.access(for: opaqueBookID),
              access.record.format == .pdf else {
            return nativeVisualErrorResponse(
                .bookUnavailable,
                status: .conflict
            )
        }

        let baseJPEGData: Data
        do {
            try access.validateCurrentFile(
                maximumEPUBBytes: ReaderLocalRuntimeServer.maximumEPUBBytes
            )
            baseJPEGData = try await pageRenderer.jpegData(
                for: access,
                pageNumber: pageNumber,
                pixelWidth: region == nil ? 2_000 : 3_000
            )
        } catch {
            return nativeVisualErrorResponse(
                .pdfPageRenderFailed,
                status: .conflict
            )
        }

        do {
            let capture = try await visualCaptureBroker.capturePDFPage(
                baseJPEGData: baseJPEGData,
                pageNumber: pageNumber,
                region: region,
                expectedBookID: opaqueBookID,
                expectedDocumentURL: trustedDocumentURL
            )
            let strokeCount = capture.inkStrokeCount ?? 0
            return await nativeVisualCaptureResponse(
                request,
                capture: capture,
                captureContract: "native-pdf-page/1",
                ink: strokeCount > 0 ? "present" : "none",
                delivery: delivery,
                extraHeaders: [
                    HTTPHeader("X-BW-Visual-Page"): String(pageNumber),
                    HTTPHeader("X-BW-Visual-Ink-Strokes"): String(strokeCount),
                ]
            )
        } catch let error as ReaderNativeVisualCaptureError {
            return nativeVisualErrorResponse(
                error,
                status: nativeVisualStatus(for: error)
            )
        } catch {
            return nativeVisualErrorResponse(
                .pageCompositeFailed,
                status: .internalServerError
            )
        }
    }

    private func nativeVisualDeliveryRequest(
        _ request: HTTPRequest
    ) async -> ReaderNativeVisualDeliveryRequest? {
        let contentType = String(
            (request.headers[.contentType] ?? "").split(separator: ";").first ?? ""
        ).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard contentType == "application/json",
              (Int(request.headers[.contentLength] ?? "") ?? 0) <= 8_192,
              let data = try? await request.bodyData,
              (2...8_192).contains(data.count),
              let object = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              Set(object.keys) == ["call_id", "client_secret", "tool"],
              let callID = object["call_id"] as? String,
              let clientSecret = object["client_secret"] as? String,
              let tool = object["tool"] as? String,
              ["see_ink", "see_page", "see_figure"].contains(tool) else {
            return nil
        }
        return ReaderNativeVisualDeliveryRequest(
            callID: callID,
            clientSecret: clientSecret,
            tool: tool
        )
    }

    private func nativeVisualCaptureResponse(
        _ request: HTTPRequest,
        capture: ReaderNativeVisualCaptureResult,
        captureContract: String,
        ink: String?,
        delivery: ReaderNativeVisualDeliveryRequest?,
        extraHeaders: HTTPHeaders = [:]
    ) async -> HTTPResponse {
        var headers = extraHeaders
        headers[HTTPHeader("X-BW-Visual-Capture")] = captureContract
        headers[HTTPHeader("X-BW-Visual-Width")] = String(capture.pixelWidth)
        headers[HTTPHeader("X-BW-Visual-Height")] = String(capture.pixelHeight)
        if let ink {
            headers[HTTPHeader("X-BW-Visual-Ink")] = ink
        }
        guard let delivery else {
            return dataResponse(
                request,
                data: capture.jpegData,
                contentType: "image/jpeg",
                cacheControl: "no-store",
                additionalHeaders: headers
            )
        }
        do {
            let itemID = try await ReaderRealtimeOpenAIClient.injectImage(
                callID: delivery.callID,
                clientSecret: delivery.clientSecret,
                mediaType: "image/jpeg",
                imageData: capture.jpegData
            )
            return jsonResponse(
                request,
                status: .ok,
                object: [
                    "ok": true,
                    "delivered": true,
                    "item_id": itemID,
                    "bytes": capture.jpegData.count,
                    "ink": ink ?? "unknown",
                    "capture": captureContract,
                ],
                additionalHeaders: headers
            )
        } catch {
            let message = String(error.localizedDescription.prefix(240))
            headers[HTTPHeader("X-BW-Reader-Error")] =
                "BW_NATIVE_VISUAL_DELIVERY_FAILED"
            return jsonResponse(
                request,
                status: .badGateway,
                object: [
                    "ok": false,
                    "error": message,
                    "stage": "realtime-delivery",
                ],
                additionalHeaders: headers
            )
        }
    }

    private func nativeVisualStatus(
        for error: ReaderNativeVisualCaptureError
    ) -> HTTPStatusCode {
        switch error {
        case .invalidRegion, .invalidPage, .pageScopeUnsupported:
            return .badRequest
        case .bookUnavailable, .pageUnavailable,
             .pencilOverlayUnavailable, .hierarchyUnavailable,
             .emptyViewport, .pdfPageRenderFailed:
            return .conflict
        case .inkStateUnavailable:
            return .badGateway
        case .hierarchyRenderFailed, .inkPayloadInvalid,
             .inkPayloadTooLarge, .pageCompositeFailed,
             .jpegEncodingFailed, .imageTooSmall, .imageTooLarge:
            return .internalServerError
        }
    }

    private func nativeVisualErrorResponse(
        _ error: ReaderNativeVisualCaptureError,
        status: HTTPStatusCode
    ) -> HTTPResponse {
        response(
            status: status,
            text: error.localizedDescription,
            headers: [HTTPHeader("X-BW-Reader-Error"): error.code]
        )
    }

    private func serveNativePageImage(
        _ request: HTTPRequest,
        opaqueBookID: String
    ) async -> HTTPResponse {
        guard request.method == .GET || request.method == .HEAD else {
            return response(
                status: .methodNotAllowed,
                text: "method not allowed",
                headers: [HTTPHeader("Allow"): "GET, HEAD"]
            )
        }
        guard Self.isOpaqueBookID(opaqueBookID),
              let access = await state.access(for: opaqueBookID),
              access.record.format == .pdf else {
            return response(status: .notFound, text: "book unavailable")
        }
        guard let pageNumber = Int(request.query["page"] ?? "1"),
              let requestedWidth = Int(request.query["w"] ?? "1400") else {
            return response(status: .badRequest, text: "invalid page image query")
        }
        let pixelWidth = min(3_000, max(400, requestedWidth))
        do {
            try access.validateCurrentFile(
                maximumEPUBBytes: ReaderLocalRuntimeServer.maximumEPUBBytes
            )
            let data = try await pageRenderer.jpegData(
                for: access,
                pageNumber: pageNumber,
                pixelWidth: pixelWidth
            )
            return dataResponse(
                request,
                data: data,
                contentType: "image/jpeg",
                // ⚠ **不要用一年的 immutable。**
                //
                // 这个头是从服务端设计继承来的:那边页图要从 Pi 走网络,
                // 一年缓存省下的是一次真实往返。**而在 App 里渲染是本地
                // PDFKit,省的只是一次函数调用** —— 原来那个取舍在这个表面上
                // 根本不成立。
                //
                // 代价是实打实的:整本预热会渲染**每一页**,而每页还按**多个
                // 宽度**各缓存一份。2026-08-28 用户实测 App 数据涨到 **54 GB**,
                // 直接把 IndexedDB 的配额撑爆 —— 表现是「本机 Reader 无法安全
                // 启动」,而那时看不出跟页图有任何关系。
                //
                // 5 分钟足够翻页/来回滚动复用,又不会攒成几十 GB。
                cacheControl: "private, max-age=300",
                additionalHeaders: [
                    HTTPHeader("X-BW-PDF-Renderer"): "pdfkit"
                ]
            )
        } catch {
            return response(
                status: .badRequest,
                text: error.localizedDescription
            )
        }
    }

    private func serveNativeTOC(_ request: HTTPRequest) async -> HTTPResponse {
        guard let fileIdentity = request.query["file"],
              let opaqueBookID = Self.opaqueBookID(
                fromLocalFileIdentity: fileIdentity
              ),
              let access = await state.access(for: opaqueBookID),
              access.record.format == .pdf else {
            return jsonResponse(
                request,
                status: .notFound,
                object: ["ok": false, "error": "book unavailable"]
            )
        }
        do {
            try access.validateCurrentFile(
                maximumEPUBBytes: ReaderLocalRuntimeServer.maximumEPUBBytes
            )
        } catch {
            return jsonResponse(
                request,
                status: .conflict,
                object: ["ok": false, "error": error.localizedDescription]
            )
        }
        guard let document = PDFDocument(url: access.url) else {
            return jsonResponse(
                request,
                status: .badRequest,
                object: ["ok": false, "error": "PDF outline unavailable"]
            )
        }
        let entries = Self.nativeTOCEntries(in: document)
        var payload: [String: Any] = [
            "ok": true,
            "exists": !entries.isEmpty,
            "source": entries.isEmpty ? "none" : "native",
            "count": entries.count,
            "range": [String: Any](),
        ]
        if request.query["entries"] != nil {
            payload["entries"] = entries
        }
        return jsonResponse(request, status: .ok, object: payload)
    }

    private static func nativeTOCEntries(
        in document: PDFDocument
    ) -> [[String: Any]] {
        guard let root = document.outlineRoot else { return [] }
        var entries: [[String: Any]] = []

        func appendChildren(of parent: PDFOutline, level: Int) {
            for index in 0..<parent.numberOfChildren {
                guard let outline = parent.child(at: index) else { continue }
                let title = (outline.label ?? "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                let destination = outline.destination
                if !title.isEmpty, let page = destination?.page {
                    let pageIndex = document.index(for: page)
                    if (0..<document.pageCount).contains(pageIndex) {
                        entries.append([
                            "title": title,
                            "page": pageIndex + 1,
                            "level": max(1, level),
                        ])
                    }
                }
                appendChildren(of: outline, level: level + 1)
            }
        }

        appendChildren(of: root, level: 1)
        return entries
    }

    private func serveStatic(
        _ request: HTTPRequest,
        decodedPath: String
    ) async throws -> HTTPResponse {
        let requestedRelative = String(decodedPath.dropFirst())
        let revisionPrefix = "static/\(staticRevision)/"
        let isRevisioned = requestedRelative.hasPrefix(revisionPrefix)
        let relative = isRevisioned
            ? "static/" + String(
                requestedRelative.dropFirst(revisionPrefix.count)
              )
            : requestedRelative
        guard allowedStaticFiles.contains(relative),
              let url = safeBundleURL(relative: relative) else {
            return response(status: .notFound, text: "resource unavailable")
        }
        var result = try await FileHTTPHandler(
            path: url,
            contentType: Self.mimeType(for: url.pathExtension),
            cacheControl: [.noCache]
        ).handleRequest(request)
        result.headers[.cacheControl] = isRevisioned
            ? "public, max-age=31536000, immutable"
            : "no-cache"
        Self.secure(&result)
        return result
    }

    private func serveShell(
        _ request: HTTPRequest,
        relative: String
    ) async -> HTTPResponse {
        guard let shellURL = safeBundleURL(relative: relative),
              var shell = try? String(contentsOf: shellURL, encoding: .utf8) else {
            return response(status: .notFound, text: "shell unavailable")
        }
        let opaqueBookID = request.query["book"]
        let access: ReaderLocalBookAccess?
        if let opaqueBookID {
            access = await state.access(for: opaqueBookID)
            guard access != nil else {
                return response(status: .notFound, text: "book unavailable")
            }
        } else {
            access = nil
        }

        let record = access?.record
        let opaqueID = record?.id ?? "localbook-welcome"
        let fileRel = "localbook:\(opaqueID)"
        let title = record?.title ?? "本机书库"
        let contentURL = record == nil
            ? "/r/\(capabilityToken)/empty.pdf"
            : "/r/\(capabilityToken)/books/\(opaqueID)/content"
        let sha = record?.contentSha256 ?? record?.contentFingerprint ?? ""
        let initialPage: Int
        let initialPositionTimestamp: Int
        if let rawPage = request.query["page"] {
            guard let parsedPage = Int(rawPage),
                  (1...10_000_000).contains(parsedPage),
                  record != nil else {
                return response(
                    status: .badRequest,
                    text: "invalid initial book location"
                )
            }
            initialPage = parsedPage
            initialPositionTimestamp = Int(Date().timeIntervalSince1970)
        } else {
            initialPage = 1
            initialPositionTimestamp = 0
        }
        let initialEPUBPosition = max(0, initialPage - 1)

        shell = shell
            .replacingOccurrences(
                of: "__BW_LOCAL_PDF_URL_JSON__",
                with: Self.jsonLiteral(contentURL)
            )
            .replacingOccurrences(
                of: "__BW_LOCAL_FILE_REL_JSON__",
                with: Self.jsonLiteral(fileRel)
            )
            .replacingOccurrences(
                of: "__BW_LOCAL_FILE_NAME_JSON__",
                with: Self.jsonLiteral(title)
            )
            .replacingOccurrences(
                of: "__BW_LOCAL_FILE_NAME_HTML__",
                with: Self.htmlEscaped(title)
            )
            .replacingOccurrences(
                of: "__BW_LOCAL_PDF_SIZE__",
                with: String(record?.byteCount ?? Int64(Self.emptyPDF().count))
            )
            .replacingOccurrences(
                of: "__BW_LOCAL_EPUB_SHA_JSON__",
                with: Self.jsonLiteral(sha)
            )
            .replacingOccurrences(
                of: "__BW_LOCAL_INITIAL_PAGE__",
                with: String(initialPage)
            )
            .replacingOccurrences(
                of: "__BW_LOCAL_INITIAL_PAGE_TS__",
                with: String(initialPositionTimestamp)
            )
            .replacingOccurrences(
                of: "__BW_LOCAL_INITIAL_EPUB_POS__",
                with: String(initialEPUBPosition)
            )
            .replacingOccurrences(
                of: "__BW_LOCAL_INITIAL_EPUB_POS_TS__",
                with: String(initialPositionTimestamp)
            )
            .replacingOccurrences(
                of: "__BW_LOCAL_CSP_NONCE__",
                with: cspNonce
            )
            .replacingOccurrences(
                of: "/static/",
                with: "/static/\(staticRevision)/"
            )
        guard Self.allScriptTagsCarryNonce(shell, nonce: cspNonce) else {
            return response(
                status: .internalServerError,
                text: "BW_LOCAL_BUNDLE_UNAVAILABLE: shell nonce contract missing"
            )
        }
        let tokenBase = "/r/\(capabilityToken)"
        let bootstrap = """
        window.__BW_NATIVE_LOCAL_BOOK_ID__=\(Self.jsonLiteral(opaqueID));
        window.__BW_NATIVE_LOCAL_BASE_PATH__=\(Self.jsonLiteral(tokenBase));
        """
        shell = shell.replacingOccurrences(
            of: "window.__BW_NATIVE_LOCAL_READER__=true;",
            with: "window.__BW_NATIVE_LOCAL_READER__=true;\(bootstrap)"
        )
        return dataResponse(
            request,
            data: Data(shell.utf8),
            contentType: "text/html; charset=utf-8",
            cacheControl: "no-store",
            additionalHeaders: [
                HTTPHeader("Content-Security-Policy"): "default-src 'self'; script-src 'self' 'nonce-\(cspNonce)'; script-src-attr 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob: data:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self' \(Self.openAIRealtimeOrigin) wss://bwicarus-2.taile44d0c.ts.net; object-src 'none'; base-uri 'none'; frame-src https://www.youtube-nocookie.com/embed/ https://player.bilibili.com/player.html https://www.bilibili.com/blackboard/webplayer/mbplayer.html; form-action 'none'",
                // Direct <img>/<audio> requests need the unguessable shell
                // capability in their same-origin Referer. Cross-origin
                // requests still receive no Referer under this policy.
                HTTPHeader("Referrer-Policy"): "same-origin",
            ]
        )
    }

    private func serveBook(
        _ request: HTTPRequest,
        opaqueBookID: String
    ) async throws -> HTTPResponse {
        guard Self.isOpaqueBookID(opaqueBookID),
              let access = await state.access(for: opaqueBookID) else {
            return response(status: .notFound, text: "book unavailable")
        }
        do {
            try access.validateCurrentFile(
                maximumEPUBBytes: ReaderLocalRuntimeServer.maximumEPUBBytes
            )
        } catch {
            return response(status: .badRequest, text: error.localizedDescription)
        }
        let contentType = access.record.format == .pdf
            ? "application/pdf" : "application/epub+zip"
        var result = try await FileHTTPHandler(
            path: access.url,
            contentType: contentType,
            cacheControl: [.noStore]
        ).handleRequest(request)
        Self.secure(&result)
        return result
    }

    private func dataResponse(
        _ request: HTTPRequest,
        data: Data,
        contentType: String,
        cacheControl: String,
        additionalHeaders: HTTPHeaders = [:]
    ) -> HTTPResponse {
        var headers = additionalHeaders
        headers[.contentType] = contentType
        headers[.contentLength] = String(data.count)
        headers[.cacheControl] = cacheControl
        headers[HTTPHeader("X-Content-Type-Options")] = "nosniff"
        if headers[HTTPHeader("Referrer-Policy")] == nil {
            headers[HTTPHeader("Referrer-Policy")] = "no-referrer"
        }
        return HTTPResponse(
            statusCode: .ok,
            headers: headers,
            body: request.method == .HEAD ? Data() : data
        )
    }

    private func jsonResponse(
        _ request: HTTPRequest,
        status: HTTPStatusCode,
        object: [String: Any],
        additionalHeaders: HTTPHeaders = [:]
    ) -> HTTPResponse {
        guard JSONSerialization.isValidJSONObject(object),
              let data = try? JSONSerialization.data(withJSONObject: object) else {
            return response(
                status: .internalServerError,
                text: "json encoding unavailable"
            )
        }
        var headers = additionalHeaders
        headers[.contentType] = "application/json; charset=utf-8"
        headers[.contentLength] = String(data.count)
        headers[.cacheControl] = "no-store"
        headers[HTTPHeader("X-Content-Type-Options")] = "nosniff"
        headers[HTTPHeader("Referrer-Policy")] = "no-referrer"
        return HTTPResponse(
            statusCode: status,
            headers: headers,
            body: request.method == .HEAD ? Data() : data
        )
    }

    private func response(
        status: HTTPStatusCode,
        text: String,
        headers additionalHeaders: HTTPHeaders = [:]
    ) -> HTTPResponse {
        var headers = additionalHeaders
        let data = Data(text.utf8)
        headers[.contentType] = "text/plain; charset=utf-8"
        headers[.contentLength] = String(data.count)
        headers[.cacheControl] = "no-store"
        headers[HTTPHeader("X-Content-Type-Options")] = "nosniff"
        headers[HTTPHeader("Referrer-Policy")] = "no-referrer"
        return HTTPResponse(statusCode: status, headers: headers, body: data)
    }

    private static func secure(_ response: inout HTTPResponse) {
        response.headers[HTTPHeader("X-Content-Type-Options")] = "nosniff"
        response.headers[HTTPHeader("Referrer-Policy")] = "no-referrer"
    }

    private func safeBundleURL(relative: String) -> URL? {
        guard Self.isCanonicalRelativePath(relative) else { return nil }
        let candidate = relative.split(separator: "/").reduce(bundleRoot) {
            $0.appendingPathComponent(String($1), isDirectory: false)
        }.resolvingSymlinksInPath().standardizedFileURL
        let rootPath = bundleRoot.path.hasSuffix("/")
            ? bundleRoot.path : bundleRoot.path + "/"
        guard candidate.path.hasPrefix(rootPath),
              FileManager.default.fileExists(atPath: candidate.path) else {
            return nil
        }
        return candidate
    }

    static func isCanonicalRelativePath(_ value: String) -> Bool {
        guard !value.isEmpty, !value.hasPrefix("/"),
              !value.contains("\\"), !value.contains("\0") else {
            return false
        }
        let pieces = value.split(separator: "/", omittingEmptySubsequences: false)
        return !pieces.isEmpty && pieces.allSatisfy {
            !$0.isEmpty && $0 != "." && $0 != ".."
        }
    }

    static func isOpaqueBookID(_ value: String) -> Bool {
        guard value.hasPrefix("localbook-"), value.count == 74 else { return false }
        return value.dropFirst("localbook-".count).allSatisfy {
            $0.isHexDigit && !$0.isUppercase
        }
    }

    private static func opaqueBookID(
        fromLocalFileIdentity value: String
    ) -> String? {
        guard value.hasPrefix("localbook:") else { return nil }
        let opaqueBookID = String(value.dropFirst("localbook:".count))
        return isOpaqueBookID(opaqueBookID) ? opaqueBookID : nil
    }

    static func jsonLiteral(_ value: String) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: [value]),
              let text = String(data: data, encoding: .utf8) else {
            return "\"\""
        }
        return String(text.dropFirst().dropLast())
    }

    static func htmlEscaped(_ value: String) -> String {
        value.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
            .replacingOccurrences(of: "'", with: "&#39;")
    }

    static func allScriptTagsCarryNonce(
        _ html: String,
        nonce: String
    ) -> Bool {
        guard let expression = try? NSRegularExpression(
            pattern: #"<script\b[^>]*>"#,
            options: [.caseInsensitive]
        ) else { return false }
        let range = NSRange(html.startIndex..<html.endIndex, in: html)
        let matches = expression.matches(in: html, range: range)
        guard !matches.isEmpty else { return false }
        return matches.allSatisfy { match in
            guard let tagRange = Range(match.range, in: html) else {
                return false
            }
            return html[tagRange].contains("nonce=\"\(nonce)\"")
        }
    }

    static func mimeType(for ext: String) -> String {
        switch ext.lowercased() {
        case "html": return "text/html; charset=utf-8"
        case "js", "mjs": return "text/javascript; charset=utf-8"
        case "css": return "text/css; charset=utf-8"
        case "json": return "application/json; charset=utf-8"
        case "svg": return "image/svg+xml"
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "gif": return "image/gif"
        case "woff": return "font/woff"
        case "woff2": return "font/woff2"
        case "ttf": return "font/ttf"
        case "wasm": return "application/wasm"
        case "pdf": return "application/pdf"
        default: return "application/octet-stream"
        }
    }

    static func emptyPDF() -> Data {
        var chunks = ["%PDF-1.4\n"]
        let objects = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> /Contents 4 0 R >>",
            "<< /Length 0 >>\nstream\n\nendstream",
        ]
        var offsets = [0]
        var length = chunks[0].utf8.count
        for (index, body) in objects.enumerated() {
            offsets.append(length)
            let object = "\(index + 1) 0 obj\n\(body)\nendobj\n"
            chunks.append(object)
            length += object.utf8.count
        }
        let xrefOffset = length
        chunks.append("xref\n0 \(objects.count + 1)\n")
        chunks.append("0000000000 65535 f \n")
        for offset in offsets.dropFirst() {
            chunks.append(String(format: "%010d 00000 n \n", offset))
        }
        chunks.append("trailer\n<< /Size \(objects.count + 1) /Root 1 0 R >>\nstartxref\n\(xrefOffset)\n%%EOF\n")
        return Data(chunks.joined().utf8)
    }
}

/// Stable, loopback-only origin for the bundled Reader renderer.
///
/// The port is deliberately fixed because IndexedDB is origin-scoped. The
/// capability token protects shell/book routes; `/static/**` is the only bare
/// route and can serve only bytes that passed the signed bundle manifest.
@MainActor
final class ReaderLocalRuntimeServer {
    static let port: UInt16 = 43_129
    static let host = "127.0.0.1"
    static let origin = "http://127.0.0.1:43129"
    static let maximumEPUBBytes: Int64 = 512 * 1_024 * 1_024

    private let capabilityToken: String
    private let cspNonce: String
    private let state: ReaderLocalRuntimeState
    let piProxyBroker: ReaderNativePiProxyBroker
    let visualCaptureBroker: ReaderNativeVisualCaptureBroker
    private let server: HTTPServer
    private let bundleVerificationTask: Task<Void, Error>
    private let bundleVerificationCacheToken: String
    private var bundleVerificationRecorded = false
    private var runTask: Task<Void, Never>?
    private var lifecycleTask: Task<Void, Error>?
    private var lifecycleToken: UUID?
    private var lastRunError: Error?

    init(bundle: Bundle = .main) throws {
        guard let resourceRoot = bundle.resourceURL else {
            throw ReaderLocalRuntimeError.bundleUnavailable
        }
        let root = resourceRoot.appendingPathComponent("ReaderBundle", isDirectory: true)
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: root.path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            throw ReaderLocalRuntimeError.bundleUnavailable
        }
        let bundleRoot = root.resolvingSymlinksInPath().standardizedFileURL
        let manifestURL = bundleRoot.appendingPathComponent("bundle-manifest.json")
        guard let manifestData = try? Data(contentsOf: manifestURL),
              let manifest = try? JSONSerialization.jsonObject(with: manifestData)
                as? [String: Any],
              manifest["contract"] as? String == "bw-local-reader-bundle/1",
              let files = manifest["files"] as? [String: String] else {
            throw ReaderLocalRuntimeError.bundleUnavailable
        }
        let requiredShells: Set<String> = [
            "shells/pdf.html", "shells/epub.html",
        ]
        guard requiredShells.isSubset(of: Set(files.keys)) else {
            throw ReaderLocalRuntimeError.bundleUnavailable
        }

        var staticFiles = Set<String>()
        for (relative, expectedDigest) in files {
            guard ReaderLocalHTTPHandler.isCanonicalRelativePath(relative),
                  expectedDigest.count == 64,
                  expectedDigest.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) else {
                throw ReaderLocalRuntimeError.bundleUnavailable
            }
            if relative.hasPrefix("static/") {
                staticFiles.insert(relative)
            }
        }

        let manifestDigest = ReaderLocalBundleIntegrity.sha256Hex(manifestData)
        let bundleIdentity = [
            bundle.bundleIdentifier ?? "unknown",
            bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString")
                as? String ?? "0",
            bundle.object(forInfoDictionaryKey: "CFBundleVersion")
                as? String ?? "0",
        ].joined(separator: ":")
        let verificationCacheToken = "\(bundleIdentity):\(manifestDigest)"
        let verificationCacheKey = "reader.localRuntime.bundleIntegrity.v1"
        let cacheHit = UserDefaults.standard.string(forKey: verificationCacheKey)
            == verificationCacheToken
        bundleVerificationCacheToken = verificationCacheToken
        bundleVerificationTask = Task.detached(priority: .utility) {
            try ReaderLocalBundleIntegrity.verify(
                ReaderLocalBundleVerificationInput(
                    bundleRoot: bundleRoot,
                    files: files,
                    // The installed App bundle is code-signed and immutable. A
                    // matching bundle identity + manifest digest can therefore
                    // reuse the prior full digest pass; existence/path checks
                    // still run off the main actor on every launch.
                    validatesDigests: !cacheHit
                )
            )
        }

        capabilityToken = try Self.makeRandomHex(byteCount: 32)
        cspNonce = try Self.makeRandomHex(byteCount: 32)
        state = ReaderLocalRuntimeState()
        piProxyBroker = ReaderNativePiProxyBroker()
        let imageProxyBroker = ReaderNativeImageProxyBroker()
        visualCaptureBroker = ReaderNativeVisualCaptureBroker()
        let address = try sockaddr_in.inet(ip4: Self.host, port: Self.port)
        let handler = ReaderLocalHTTPHandler(
            capabilityToken: capabilityToken,
            cspNonce: cspNonce,
            bundleRoot: bundleRoot,
            allowedStaticFiles: staticFiles,
            staticRevision: String(manifestDigest.prefix(24)),
            state: state,
            piProxyBroker: piProxyBroker,
            imageProxyBroker: imageProxyBroker,
            pageRenderer: ReaderNativePDFPageRenderer(),
            visualCaptureBroker: visualCaptureBroker
        )
        server = HTTPServer(
            address: address,
            timeout: 30,
            logger: .disabled,
            handler: handler
        )
    }

    deinit {
        piProxyBroker.cancelAll()
        bundleVerificationTask.cancel()
        lifecycleTask?.cancel()
        runTask?.cancel()
    }

    var baseURL: URL {
        URL(string: "\(Self.origin)/r/\(capabilityToken)/")!
    }

    @discardableResult
    func start() async throws -> Bool {
        if await server.isListening { return false }
        if try await joinLifecycleTaskIfPresent() { return false }

        let token = UUID()
        let transition = Task<Void, Error> { @MainActor [weak self] in
            guard let self else { throw CancellationError() }
            try await self.performStart()
        }
        lifecycleToken = token
        lifecycleTask = transition
        do {
            try await transition.value
            clearLifecycleTask(ifMatching: token)
            return true
        } catch {
            clearLifecycleTask(ifMatching: token)
            throw error
        }
    }

    /// Restarts only if the server actually stopped while backgrounded.
    ///
    /// Returns whether a restart happened, because the caller reloads the web
    /// view afterwards and a reload throws away the rendered page, the scroll
    /// position and every warmed page image. Doing that on each return to the
    /// foreground made a glance at the notification centre cost a full reload.
    ///
    /// iOS does not always tear the listener down -- a brief switch usually
    /// leaves it intact -- so the cheap check comes first and the expensive
    /// restart only follows when it is genuinely gone.
    @discardableResult
    func restartAfterForeground() async throws -> Bool {
        if await server.isListening, runTask != nil { return false }
        if try await joinLifecycleTaskIfPresent() { return false }

        let token = UUID()
        let transition = Task<Void, Error> { @MainActor [weak self] in
            guard let self else { throw CancellationError() }
            self.runTask?.cancel()
            await self.server.stop(timeout: 0)
            self.runTask = nil
            try await self.performStart()
        }
        lifecycleToken = token
        lifecycleTask = transition
        do {
            try await transition.value
            clearLifecycleTask(ifMatching: token)
            return true
        } catch {
            clearLifecycleTask(ifMatching: token)
            throw error
        }
    }

    private func performStart() async throws {
        try await ensureBundleVerified()
        if await server.isListening { return }
        lastRunError = nil
        runTask?.cancel()
        let server = server
        runTask = Task { @MainActor [weak self] in
            do {
                try await server.run()
            } catch is CancellationError {
                // Expected during an explicit restart or app teardown.
            } catch {
                self?.lastRunError = error
            }
        }
        do {
            try await server.waitUntilListening(timeout: 3)
        } catch {
            runTask?.cancel()
            await server.stop(timeout: 0)
            let detail = lastRunError?.localizedDescription
                ?? error.localizedDescription
            throw ReaderLocalRuntimeError.serverUnavailable(detail)
        }
    }

    private func ensureBundleVerified() async throws {
        do {
            try await bundleVerificationTask.value
            if !bundleVerificationRecorded {
                UserDefaults.standard.set(
                    bundleVerificationCacheToken,
                    forKey: "reader.localRuntime.bundleIntegrity.v1"
                )
                bundleVerificationRecorded = true
            }
        } catch {
            UserDefaults.standard.removeObject(
                forKey: "reader.localRuntime.bundleIntegrity.v1"
            )
            throw ReaderLocalRuntimeError.bundleUnavailable
        }
    }

    private func joinLifecycleTaskIfPresent() async throws -> Bool {
        guard let transition = lifecycleTask,
              let token = lifecycleToken else { return false }
        do {
            try await transition.value
            clearLifecycleTask(ifMatching: token)
            return true
        } catch {
            clearLifecycleTask(ifMatching: token)
            throw error
        }
    }

    private func clearLifecycleTask(ifMatching token: UUID) {
        guard lifecycleToken == token else { return }
        lifecycleTask = nil
        lifecycleToken = nil
    }

    func defaultShellURL() -> URL {
        makeShellURL(format: .pdf, bookID: nil, initialPage: nil)
    }

    /// Replaces the single active document and strongly retains its security
    /// scope. Only the opaque localBookID appears in the renderer URL.
    func open(
        _ access: ReaderLocalBookAccess,
        initialPage: Int? = nil
    ) async throws -> URL {
        try access.validateCurrentFile(maximumEPUBBytes: Self.maximumEPUBBytes)
        if let initialPage,
           !(1...10_000_000).contains(initialPage) {
            throw ReaderLocalRuntimeError.serverUnavailable(
                "本机书籍初始位置无效"
            )
        }
        await state.replace(with: access)
        return makeShellURL(
            format: access.record.format,
            bookID: access.record.id,
            initialPage: initialPage
        )
    }

    private func makeShellURL(
        format: ReaderLocalBookFormat,
        bookID: String?,
        initialPage: Int?
    ) -> URL {
        let shell = format == .epub ? "epub.html" : "pdf.html"
        var components = URLComponents(
            url: baseURL.appendingPathComponent("shells/\(shell)"),
            resolvingAgainstBaseURL: false
        )!
        var queryItems = [URLQueryItem]()
        if let bookID {
            queryItems.append(URLQueryItem(name: "book", value: bookID))
        }
        if let initialPage {
            queryItems.append(URLQueryItem(
                name: "page",
                value: String(initialPage)
            ))
        }
        components.queryItems = queryItems.isEmpty ? nil : queryItems
        return components.url!
    }

    private static func makeRandomHex(byteCount: Int) throws -> String {
        var bytes = [UInt8](repeating: 0, count: byteCount)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        guard status == errSecSuccess else {
            throw ReaderLocalRuntimeError.serverUnavailable("无法生成本机会话凭据")
        }
        return bytes.map { String(format: "%02x", $0) }.joined()
    }
}

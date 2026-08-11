import Foundation
import UIKit
import WebKit

enum NativeReaderCaptureError: LocalizedError {
    case pageUnavailable
    case invalidPagePayload
    case emptyViewport
    case snapshotUnavailable

    var errorDescription: String? {
        switch self {
        case .pageUnavailable:
            return "阅读器页面尚未加载完成"
        case .invalidPagePayload:
            return "无法读取当前页面信息"
        case .emptyViewport:
            return "当前阅读视口为空"
        case .snapshotUnavailable:
            return "无法截取当前阅读视口"
        }
    }
}

struct ReaderNativeVisualCaptureRegion: Sendable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct ReaderNativeVisualCaptureResult: Sendable {
    let jpegData: Data
    let pixelWidth: Int
    let pixelHeight: Int
    let inkStrokeCount: Int?

    init(
        jpegData: Data,
        pixelWidth: Int,
        pixelHeight: Int,
        inkStrokeCount: Int? = nil
    ) {
        self.jpegData = jpegData
        self.pixelWidth = pixelWidth
        self.pixelHeight = pixelHeight
        self.inkStrokeCount = inkStrokeCount
    }
}

private struct ReaderNativeVisualInkReply: Decodable {
    let ok: Bool
    let error: String?
    let byteCount: Int?
    let strokes: [ReaderNativeVisualInkStroke]?
}

private struct ReaderNativeVisualInkStroke: Decodable {
    let type: String?
    let color: String?
    let width: Double?
    let points: [[Double]]?
    let legacyPoints: [[Double]]?
    let id: String?
    let createdAtEpochMs: Double?
    let ordinal: Int?

    enum CodingKeys: String, CodingKey {
        case type = "t"
        case color = "c"
        case width = "w"
        case points = "p"
        case legacyPoints = "pts"
        case id
        case createdAtEpochMs
        case ordinal
    }

    var canonicalPoints: [[Double]] {
        points ?? legacyPoints ?? []
    }
}

enum ReaderNativeVisualCaptureError: LocalizedError, Sendable {
    case invalidRegion
    case invalidPage
    case pageScopeUnsupported
    case bookUnavailable
    case pageUnavailable
    case pencilOverlayUnavailable
    case hierarchyUnavailable
    case emptyViewport
    case hierarchyRenderFailed
    case pdfPageRenderFailed
    case inkStateUnavailable(String)
    case inkPayloadInvalid
    case inkPayloadTooLarge(Int)
    case pageCompositeFailed
    case jpegEncodingFailed
    case imageTooSmall(Int)
    case imageTooLarge(Int)

    var code: String {
        switch self {
        case .invalidRegion:
            return "BW_NATIVE_VISUAL_INVALID_REGION"
        case .invalidPage:
            return "BW_NATIVE_VISUAL_INVALID_PAGE"
        case .pageScopeUnsupported:
            return "BW_NATIVE_VISUAL_PAGE_SCOPE_UNSUPPORTED"
        case .bookUnavailable:
            return "BW_NATIVE_VISUAL_BOOK_UNAVAILABLE"
        case .pageUnavailable:
            return "BW_NATIVE_VISUAL_PAGE_UNAVAILABLE"
        case .pencilOverlayUnavailable:
            return "BW_NATIVE_VISUAL_PENCIL_OVERLAY_UNAVAILABLE"
        case .hierarchyUnavailable:
            return "BW_NATIVE_VISUAL_HIERARCHY_UNAVAILABLE"
        case .emptyViewport:
            return "BW_NATIVE_VISUAL_EMPTY_VIEWPORT"
        case .hierarchyRenderFailed:
            return "BW_NATIVE_VISUAL_RENDER_FAILED"
        case .pdfPageRenderFailed:
            return "BW_NATIVE_VISUAL_PDF_RENDER_FAILED"
        case .inkStateUnavailable:
            return "BW_NATIVE_VISUAL_INK_UNAVAILABLE"
        case .inkPayloadInvalid:
            return "BW_NATIVE_VISUAL_INK_INVALID"
        case .inkPayloadTooLarge:
            return "BW_NATIVE_VISUAL_INK_TOO_LARGE"
        case .pageCompositeFailed:
            return "BW_NATIVE_VISUAL_PAGE_COMPOSITE_FAILED"
        case .jpegEncodingFailed:
            return "BW_NATIVE_VISUAL_JPEG_FAILED"
        case .imageTooSmall:
            return "BW_NATIVE_VISUAL_IMAGE_TOO_SMALL"
        case .imageTooLarge:
            return "BW_NATIVE_VISUAL_IMAGE_TOO_LARGE"
        }
    }

    var errorDescription: String? {
        switch self {
        case .invalidRegion:
            return "原生合成图区域无效"
        case .invalidPage:
            return "原生合成图页码无效"
        case .pageScopeUnsupported:
            return "原生离屏合成目前只支持 PDF"
        case .bookUnavailable:
            return "原生离屏合成：当前本机 PDF 不可用"
        case .pageUnavailable:
            return "原生合成图：阅读器页面当前不在屏幕上"
        case .pencilOverlayUnavailable:
            return "原生合成图：PencilKit 笔迹层尚未挂载"
        case .hierarchyUnavailable:
            return "原生合成图：网页与笔迹层没有公共视图层级"
        case .emptyViewport:
            return "原生合成图：当前阅读视口为空"
        case .hierarchyRenderFailed:
            return "原生合成图：Apple 视图层级渲染失败"
        case .pdfPageRenderFailed:
            return "原生离屏合成：PDFKit 无法渲染目标页"
        case .inkStateUnavailable(let reason):
            return "原生离屏合成：本机笔迹状态不可用（\(reason)）"
        case .inkPayloadInvalid:
            return "原生离屏合成：本机笔迹数据无效"
        case .inkPayloadTooLarge(let byteCount):
            return "原生离屏合成：本机笔迹数据过大（\(byteCount) 字节）"
        case .pageCompositeFailed:
            return "原生离屏合成：PDF 与笔迹叠加失败"
        case .jpegEncodingFailed:
            return "原生合成图：JPEG 编码失败"
        case .imageTooSmall(let byteCount):
            return "原生合成图：画面疑似空白（仅 \(byteCount) 字节）"
        case .imageTooLarge(let byteCount):
            return "原生合成图：压缩后仍过大（\(byteCount) 字节）"
        }
    }
}

/// Captures exactly what the App renders: the WKWebView plus every native
/// overlay above it, including PencilKit. The broker is main-actor isolated
/// because `drawHierarchy` and view-tree traversal are UIKit operations.
///
/// The HTTP route returns binary JPEG rather than base64. The page converts it
/// only once at the final Realtime boundary, avoiding a second 4/3 expansion in
/// the loopback server.
@MainActor
final class ReaderNativeVisualCaptureBroker {
    private static let maximumLongEdge: CGFloat = 1_600
    private static let maximumJPEGBytes = 675_000
    private static let minimumJPEGBytes = 3_000
    private static let compressionQualities: [CGFloat] = [0.85, 0.70, 0.50]
    private static let fallbackLongEdges: [CGFloat] = [1_280, 1_024, 800]
    private static let maximumInkPayloadBytes = 8 * 1_024 * 1_024
    private static let maximumInkStrokeCount = 4_096
    private static let maximumInkPointCount = 131_072

    private weak var webView: WKWebView?
    private weak var pencilCanvas: UIView?

    func bind(webView: WKWebView, pencilCanvas: UIView) {
        self.webView = webView
        self.pencilCanvas = pencilCanvas
    }

    func unbind(pencilCanvas: UIView) {
        guard self.pencilCanvas === pencilCanvas else { return }
        self.pencilCanvas = nil
    }

    func capture(
        region: ReaderNativeVisualCaptureRegion?
    ) throws -> ReaderNativeVisualCaptureResult {
        guard let webView, webView.window != nil else {
            throw ReaderNativeVisualCaptureError.pageUnavailable
        }
        guard let pencilCanvas, pencilCanvas.window != nil else {
            throw ReaderNativeVisualCaptureError.pencilOverlayUnavailable
        }
        guard let host = Self.lowestCommonAncestor(webView, pencilCanvas) else {
            throw ReaderNativeVisualCaptureError.hierarchyUnavailable
        }

        host.layoutIfNeeded()
        let viewport = webView.convert(webView.bounds, to: host)
            .intersection(host.bounds)
        guard viewport.width >= 8, viewport.height >= 8 else {
            throw ReaderNativeVisualCaptureError.emptyViewport
        }
        let captureBounds = try Self.captureBounds(
            viewport: viewport,
            region: region
        ).integral
        guard captureBounds.width >= 8, captureBounds.height >= 8 else {
            throw ReaderNativeVisualCaptureError.emptyViewport
        }

        let displayScale = webView.window?.screen.scale ?? UIScreen.main.scale
        let boundedScale = min(
            displayScale,
            Self.maximumLongEdge /
                max(captureBounds.width, captureBounds.height)
        )
        let format = UIGraphicsImageRendererFormat()
        format.scale = boundedScale
        format.opaque = true
        var hierarchyRendered = false
        let image = UIGraphicsImageRenderer(
            size: captureBounds.size,
            format: format
        ).image { context in
            context.cgContext.setFillColor(UIColor.black.cgColor)
            context.cgContext.fill(
                CGRect(origin: .zero, size: captureBounds.size)
            )
            context.cgContext.translateBy(
                x: -captureBounds.minX,
                y: -captureBounds.minY
            )
            hierarchyRendered = host.drawHierarchy(
                in: host.bounds,
                afterScreenUpdates: true
            )
        }
        guard hierarchyRendered else {
            throw ReaderNativeVisualCaptureError.hierarchyRenderFailed
        }
        return try Self.encode(image)
    }

    /// Reconstructs an arbitrary PDF page without scrolling the live reader.
    /// PDFKit provides the page pixels; the trusted reader page provides its
    /// authoritative persisted ink JSON because the PencilKit canvas is only
    /// an input buffer and intentionally drops strokes after web persistence.
    func capturePDFPage(
        baseJPEGData: Data,
        pageNumber: Int,
        region: ReaderNativeVisualCaptureRegion?,
        expectedBookID: String,
        expectedDocumentURL: URL
    ) async throws -> ReaderNativeVisualCaptureResult {
        guard pageNumber >= 1 else {
            throw ReaderNativeVisualCaptureError.invalidPage
        }
        let strokes = try await loadPDFPageInk(
            pageNumber: pageNumber,
            expectedBookID: expectedBookID,
            expectedDocumentURL: expectedDocumentURL
        )
        guard let baseImage = UIImage(data: baseJPEGData),
              let baseCGImage = baseImage.cgImage else {
            throw ReaderNativeVisualCaptureError.pdfPageRenderFailed
        }
        let pageBounds = CGRect(
            x: 0,
            y: 0,
            width: CGFloat(baseCGImage.width),
            height: CGFloat(baseCGImage.height)
        )
        let cropBounds = try Self.pageCaptureBounds(
            pageBounds: pageBounds,
            region: region
        ).integral.intersection(pageBounds)
        guard cropBounds.width >= 8, cropBounds.height >= 8 else {
            throw ReaderNativeVisualCaptureError.invalidRegion
        }

        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        let image = UIGraphicsImageRenderer(
            size: cropBounds.size,
            format: format
        ).image { rendererContext in
            let context = rendererContext.cgContext
            context.setFillColor(UIColor.white.cgColor)
            context.fill(CGRect(origin: .zero, size: cropBounds.size))
            context.translateBy(x: -cropBounds.minX, y: -cropBounds.minY)
            baseImage.draw(in: pageBounds)
            Self.drawInk(strokes, in: context, pageBounds: pageBounds)
        }
        guard image.cgImage != nil else {
            throw ReaderNativeVisualCaptureError.pageCompositeFailed
        }
        let encoded = try Self.encode(image)
        return ReaderNativeVisualCaptureResult(
            jpegData: encoded.jpegData,
            pixelWidth: encoded.pixelWidth,
            pixelHeight: encoded.pixelHeight,
            inkStrokeCount: strokes.count
        )
    }

    private func loadPDFPageInk(
        pageNumber: Int,
        expectedBookID: String,
        expectedDocumentURL: URL
    ) async throws -> [ReaderNativeVisualInkStroke] {
        guard let webView,
              Self.sameReaderDocument(
                webView.url,
                expectedDocumentURL,
                bookID: expectedBookID
              ) else {
            throw ReaderNativeVisualCaptureError.inkStateUnavailable(
                "document-mismatch"
            )
        }

        let raw: Any?
        do {
            raw = try await webView.callAsyncJavaScript(
                """
                const fail = (error, byteCount) => JSON.stringify({
                  ok: false,
                  error,
                  byteCount: Number.isSafeInteger(byteCount) ? byteCount : null
                });
                if (window.top !== window) return fail("sub-frame");
                if (window.__BW_NATIVE_LOCAL_READER__ !== true) {
                  return fail("local-runtime-disabled");
                }
                if (String(window.__BW_NATIVE_LOCAL_BOOK_ID__ || "") !== expectedBookID) {
                  return fail("book-mismatch");
                }
                const file = String(window.__PDF_CFG?.file_rel || "");
                if (file !== "localbook:" + expectedBookID) {
                  return fail("file-mismatch");
                }
                try {
                  const response = await fetch(
                    "/pdf/api/ink?file=" + encodeURIComponent(file),
                    { cache: "no-store" }
                  );
                  if (!response || !response.ok) {
                    return fail("ink-http-" + String(response?.status || 0));
                  }
                  const value = await response.json();
                  if (!value || value.ok !== true || !value.pages ||
                      typeof value.pages !== "object" || Array.isArray(value.pages)) {
                    return fail("ink-response-invalid");
                  }
                  const key = String(pageNumber);
                  const persisted = Object.prototype.hasOwnProperty.call(value.pages, key)
                    ? value.pages[key] : [];
                  const live = window._ink && window._ink.byPage;
                  const dirty = window._ink && window._ink.dirty &&
                    window._ink.dirty[pageNumber];
                  const strokes = dirty && live && Array.isArray(live[pageNumber])
                    ? live[pageNumber] : persisted;
                  if (!Array.isArray(strokes)) return fail("ink-page-invalid");
                  if (strokes.length > 4096) return fail("ink-too-large");
                  let points = 0;
                  for (const stroke of strokes) {
                    if (!stroke || typeof stroke !== "object" || Array.isArray(stroke)) {
                      return fail("ink-page-invalid");
                    }
                    const list = Array.isArray(stroke.p) ? stroke.p : stroke.pts;
                    if (!Array.isArray(list) || list.length > 4096) {
                      return fail("ink-page-invalid");
                    }
                    points += list.length;
                    if (points > 131072) return fail("ink-too-large");
                  }
                  const payload = JSON.stringify({ ok: true, strokes });
                  const byteCount = typeof TextEncoder === "function"
                    ? new TextEncoder().encode(payload).byteLength : payload.length * 2;
                  if (byteCount > 8388608) return fail("ink-too-large", byteCount);
                  return payload;
                } catch (_) {
                  return fail("ink-fetch-failed");
                }
                """,
                arguments: [
                    "pageNumber": pageNumber,
                    "expectedBookID": expectedBookID,
                ],
                in: nil,
                contentWorld: .page
            )
        } catch {
            throw ReaderNativeVisualCaptureError.inkStateUnavailable(
                "bridge-call-failed"
            )
        }
        guard Self.sameReaderDocument(
            webView.url,
            expectedDocumentURL,
            bookID: expectedBookID
        ) else {
            throw ReaderNativeVisualCaptureError.inkStateUnavailable(
                "document-changed"
            )
        }
        guard let json = raw as? String,
              let data = json.data(using: .utf8) else {
            throw ReaderNativeVisualCaptureError.inkPayloadInvalid
        }
        guard data.count <= Self.maximumInkPayloadBytes else {
            throw ReaderNativeVisualCaptureError.inkPayloadTooLarge(data.count)
        }
        let reply: ReaderNativeVisualInkReply
        do {
            reply = try JSONDecoder().decode(
                ReaderNativeVisualInkReply.self,
                from: data
            )
        } catch {
            throw ReaderNativeVisualCaptureError.inkPayloadInvalid
        }
        guard reply.ok else {
            if reply.error == "ink-too-large" {
                throw ReaderNativeVisualCaptureError.inkPayloadTooLarge(
                    reply.byteCount ?? Self.maximumInkPayloadBytes + 1
                )
            }
            throw ReaderNativeVisualCaptureError.inkStateUnavailable(
                Self.safeInkFailure(reply.error)
            )
        }
        let strokes = reply.strokes ?? []
        guard strokes.count <= Self.maximumInkStrokeCount else {
            throw ReaderNativeVisualCaptureError.inkPayloadTooLarge(data.count)
        }
        var pointCount = 0
        for stroke in strokes {
            let points = stroke.canonicalPoints
            guard !points.isEmpty, points.count <= 4_096 else {
                throw ReaderNativeVisualCaptureError.inkPayloadInvalid
            }
            pointCount += points.count
            guard pointCount <= Self.maximumInkPointCount,
                  points.allSatisfy({ point in
                    point.count >= 2
                        && point[0].isFinite
                        && point[1].isFinite
                  }) else {
                throw ReaderNativeVisualCaptureError.inkPayloadInvalid
            }
        }
        return strokes
    }

    private static func sameReaderDocument(
        _ current: URL?,
        _ expected: URL,
        bookID: String
    ) -> Bool {
        guard let current,
              current.scheme?.lowercased() == expected.scheme?.lowercased(),
              current.host?.lowercased() == expected.host?.lowercased(),
              current.port == expected.port,
              current.path == expected.path else {
            return false
        }
        func bookValues(_ url: URL) -> [String] {
            URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?
                .filter { $0.name == "book" }
                .compactMap(\.value) ?? []
        }
        return bookValues(current) == [bookID]
            && bookValues(expected) == [bookID]
    }

    private static func safeInkFailure(_ value: String?) -> String {
        let known: Set<String> = [
            "sub-frame", "local-runtime-disabled", "book-mismatch",
            "file-mismatch", "ink-http-0", "ink-http-400", "ink-http-404",
            "ink-http-409", "ink-http-500", "ink-response-invalid",
            "ink-page-invalid", "ink-fetch-failed",
        ]
        guard let value, known.contains(value) else { return "unknown" }
        return value
    }

    private static func pageCaptureBounds(
        pageBounds: CGRect,
        region: ReaderNativeVisualCaptureRegion?
    ) throws -> CGRect {
        guard let region else { return pageBounds }
        let unit = try validatedUnitRegion(region)
        return CGRect(
            x: pageBounds.minX + unit.minX * pageBounds.width,
            y: pageBounds.minY + unit.minY * pageBounds.height,
            width: unit.width * pageBounds.width,
            height: unit.height * pageBounds.height
        )
    }

    private static func drawInk(
        _ strokes: [ReaderNativeVisualInkStroke],
        in context: CGContext,
        pageBounds: CGRect
    ) {
        let regionOrdinals = regionOrdinalMap(strokes)
        for (index, stroke) in strokes.enumerated() {
            let cap = stroke.type == "region" ? 512 : 4_096
            let points = stroke.canonicalPoints.prefix(cap).map { raw in
                CGPoint(
                    x: pageBounds.minX
                        + CGFloat(min(1, max(0, raw[0]))) * pageBounds.width,
                    y: pageBounds.minY
                        + CGFloat(min(1, max(0, raw[1]))) * pageBounds.height
                )
            }
            guard let first = points.first else { continue }
            let type = stroke.type ?? "pen"
            let widthValue = stroke.width.flatMap { $0.isFinite ? $0 : nil }
            let lineWidth = CGFloat(max(0.6, min(20, widthValue ?? 2.5)))
            let color = UIColor(bwHex: normalizedInkColor(stroke.color))

            context.saveGState()
            context.setStrokeColor(color.cgColor)
            context.setLineWidth(lineWidth)
            context.setLineCap(.round)
            context.setLineJoin(.round)
            switch type {
            case "region" where points.count >= 3:
                let path = CGMutablePath()
                path.move(to: first)
                for point in points.dropFirst() { path.addLine(to: point) }
                path.closeSubpath()
                context.addPath(path)
                context.setFillColor(color.withAlphaComponent(0.18).cgColor)
                context.drawPath(using: .eoFill)
                context.addPath(path)
                context.setStrokeColor(color.withAlphaComponent(0.92).cgColor)
                context.strokePath()
                drawRegionLabel(
                    stroke,
                    points: points,
                    ordinal: regionOrdinals[index] ?? 0,
                    pageBounds: pageBounds
                )
            case "pen":
                context.beginPath()
                context.move(to: first)
                if points.count == 1 {
                    context.addLine(to: CGPoint(x: first.x + 0.1, y: first.y))
                } else if let last = points.last {
                    if points.count > 2 {
                        for offset in 1..<(points.count - 1) {
                            let control = points[offset]
                            let next = points[offset + 1]
                            context.addQuadCurve(
                                to: CGPoint(
                                    x: (control.x + next.x) / 2,
                                    y: (control.y + next.y) / 2
                                ),
                                control: control
                            )
                        }
                    }
                    context.addLine(to: last)
                }
                context.strokePath()
            case "line" where points.count >= 2:
                context.beginPath()
                context.move(to: first)
                context.addLine(to: points[1])
                context.strokePath()
            case "arrow" where points.count >= 2:
                let end = points[1]
                context.beginPath()
                context.move(to: first)
                context.addLine(to: end)
                context.strokePath()
                let angle = atan2(end.y - first.y, end.x - first.x)
                let head = max(9, lineWidth * 3.5)
                context.beginPath()
                context.move(to: end)
                context.addLine(to: CGPoint(
                    x: end.x - head * cos(angle - 0.42),
                    y: end.y - head * sin(angle - 0.42)
                ))
                context.move(to: end)
                context.addLine(to: CGPoint(
                    x: end.x - head * cos(angle + 0.42),
                    y: end.y - head * sin(angle + 0.42)
                ))
                context.strokePath()
            case "rect" where points.count >= 2:
                context.stroke(CGRect(
                    x: min(first.x, points[1].x),
                    y: min(first.y, points[1].y),
                    width: abs(points[1].x - first.x),
                    height: abs(points[1].y - first.y)
                ))
            default:
                break
            }
            context.restoreGState()
        }
    }

    private static func normalizedInkColor(_ value: String?) -> String {
        guard let value, value.count == 7, value.first == "#",
              value.dropFirst().allSatisfy(\.isHexDigit) else {
            return "#e74c3c"
        }
        return value
    }

    private static func regionOrdinalMap(
        _ strokes: [ReaderNativeVisualInkStroke]
    ) -> [Int: Int] {
        let indices = strokes.indices.filter { strokes[$0].type == "region" }
            .sorted { left, right in
                let lhs = strokes[left]
                let rhs = strokes[right]
                let timeOrder = (lhs.createdAtEpochMs ?? 0)
                    - (rhs.createdAtEpochMs ?? 0)
                if timeOrder != 0 { return timeOrder < 0 }
                return (lhs.id ?? "") < (rhs.id ?? "")
            }
        var result = [Int: Int]()
        var used = Set<Int>()
        var missing = [Int]()
        var maximum = 0
        for index in indices {
            let ordinal = strokes[index].ordinal ?? 0
            if ordinal <= 0 || used.contains(ordinal) {
                missing.append(index)
                continue
            }
            result[index] = ordinal
            used.insert(ordinal)
            maximum = max(maximum, ordinal)
        }
        for index in missing {
            repeat { maximum += 1 } while used.contains(maximum)
            result[index] = maximum
            used.insert(maximum)
        }
        return result
    }

    private static func drawRegionLabel(
        _ stroke: ReaderNativeVisualInkStroke,
        points: [CGPoint],
        ordinal: Int,
        pageBounds: CGRect
    ) {
        guard let first = points.first else { return }
        let minimum = points.dropFirst().reduce(first) { current, point in
            CGPoint(x: min(current.x, point.x), y: min(current.y, point.y))
        }
        let timestamp = stroke.createdAtEpochMs ?? 0
        let timeLabel: String
        if timestamp.isFinite, timestamp > 0 {
            let components = Calendar.current.dateComponents(
                [.hour, .minute],
                from: Date(timeIntervalSince1970: timestamp / 1_000)
            )
            timeLabel = String(
                format: "%02d:%02d",
                components.hour ?? 0,
                components.minute ?? 0
            )
        } else {
            timeLabel = "--:--"
        }
        let label = "#\(ordinal > 0 ? String(ordinal) : "?") \(timeLabel)" as NSString
        let font = UIFont.systemFont(ofSize: 11, weight: .semibold)
        let attributes: [NSAttributedString.Key: Any] = [
            .font: font,
            .foregroundColor: UIColor.white,
        ]
        let padding: CGFloat = 3
        let textSize = label.size(withAttributes: attributes)
        let labelSize = CGSize(
            width: textSize.width + padding * 2,
            height: textSize.height + padding * 2
        )
        let origin = CGPoint(
            x: max(
                pageBounds.minX,
                min(pageBounds.maxX - labelSize.width, minimum.x)
            ),
            y: max(
                pageBounds.minY,
                min(
                    pageBounds.maxY - labelSize.height,
                    minimum.y - labelSize.height - 2
                )
            )
        )
        UIColor.black.withAlphaComponent(0.82).setFill()
        UIRectFill(CGRect(origin: origin, size: labelSize))
        label.draw(
            at: CGPoint(x: origin.x + padding, y: origin.y + padding),
            withAttributes: attributes
        )
    }

    private static func captureBounds(
        viewport: CGRect,
        region: ReaderNativeVisualCaptureRegion?
    ) throws -> CGRect {
        guard let region else { return viewport }
        let unit = try validatedUnitRegion(region)
        return CGRect(
            x: viewport.minX + unit.minX * viewport.width,
            y: viewport.minY + unit.minY * viewport.height,
            width: unit.width * viewport.width,
            height: unit.height * viewport.height
        ).intersection(viewport)
    }

    private static func validatedUnitRegion(
        _ region: ReaderNativeVisualCaptureRegion
    ) throws -> CGRect {
        let values = [region.x, region.y, region.width, region.height]
        guard values.allSatisfy(\.isFinite),
              region.width > 0, region.height > 0 else {
            throw ReaderNativeVisualCaptureError.invalidRegion
        }
        let unit = CGRect(
            x: CGFloat(region.x),
            y: CGFloat(region.y),
            width: CGFloat(region.width),
            height: CGFloat(region.height)
        ).intersection(CGRect(x: 0, y: 0, width: 1, height: 1))
        guard !unit.isNull, unit.width > 0, unit.height > 0 else {
            throw ReaderNativeVisualCaptureError.invalidRegion
        }
        return unit
    }

    private static func lowestCommonAncestor(
        _ first: UIView,
        _ second: UIView
    ) -> UIView? {
        var secondAncestors = Set<ObjectIdentifier>()
        var candidate: UIView? = second
        while let view = candidate {
            secondAncestors.insert(ObjectIdentifier(view))
            candidate = view.superview
        }
        candidate = first
        while let view = candidate {
            if secondAncestors.contains(ObjectIdentifier(view)) {
                return view
            }
            candidate = view.superview
        }
        return nil
    }

    private static func encode(
        _ image: UIImage
    ) throws -> ReaderNativeVisualCaptureResult {
        guard image.cgImage != nil else {
            throw ReaderNativeVisualCaptureError.jpegEncodingFailed
        }
        var candidate = image
        var lastByteCount = 0
        let longEdges = [maximumLongEdge] + fallbackLongEdges
        for (index, longEdge) in longEdges.enumerated() {
            if index > 0 {
                candidate = try resized(candidate, maximumLongEdge: longEdge)
            }
            for quality in compressionQualities {
                guard let data = candidate.jpegData(
                    compressionQuality: quality
                ) else {
                    throw ReaderNativeVisualCaptureError.jpegEncodingFailed
                }
                lastByteCount = data.count
                guard data.count >= minimumJPEGBytes else { continue }
                if data.count <= maximumJPEGBytes,
                   let cgImage = candidate.cgImage {
                    return ReaderNativeVisualCaptureResult(
                        jpegData: data,
                        pixelWidth: cgImage.width,
                        pixelHeight: cgImage.height
                    )
                }
            }
        }
        if lastByteCount < minimumJPEGBytes {
            throw ReaderNativeVisualCaptureError.imageTooSmall(lastByteCount)
        }
        throw ReaderNativeVisualCaptureError.imageTooLarge(lastByteCount)
    }

    private static func resized(
        _ image: UIImage,
        maximumLongEdge: CGFloat
    ) throws -> UIImage {
        guard let cgImage = image.cgImage else {
            throw ReaderNativeVisualCaptureError.jpegEncodingFailed
        }
        let source = CGSize(
            width: CGFloat(cgImage.width),
            height: CGFloat(cgImage.height)
        )
        let scale = min(1, maximumLongEdge / max(source.width, source.height))
        let target = CGSize(
            width: max(1, floor(source.width * scale)),
            height: max(1, floor(source.height * scale))
        )
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        return UIGraphicsImageRenderer(size: target, format: format).image {
            context in
            context.cgContext.setFillColor(UIColor.black.cgColor)
            context.cgContext.fill(CGRect(origin: .zero, size: target))
            image.draw(in: CGRect(origin: .zero, size: target))
        }
    }
}

private struct NativeReaderJavaScriptSnapshot: Decodable {
    let title: String
    let url: String
    let localBookID: String
    let file: String
    let page: String
    let pageCount: String
    let selection: String
    let visibleText: String
}

@MainActor
extension ReaderWebViewModel {
    func remoteLibraryCookies() async -> [HTTPCookie] {
        await withCheckedContinuation { continuation in
            webView.configuration.websiteDataStore.httpCookieStore
                .getAllCookies { cookies in
                    continuation.resume(returning: cookies)
                }
        }
    }

    /// Captures a bounded, local snapshot of the page currently visible in the
    /// App's WKWebView. This is for App-native tools and the App Group cache;
    /// it does not participate in the Windows voice/context bridge.
    func captureNativeReaderSnapshot() async throws -> ReaderSharedSnapshot {
        guard isTrustedLocalRuntimeFeatureURL(webView.url) else {
            throw NativeReaderCaptureError.pageUnavailable
        }

        let script = #"""
        (() => {
          const clean = (value, limit) => String(value == null ? "" : value)
            .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
            .slice(0, limit);
          let state = null;
          try {
            state = window.RC && RC.ctxSync && typeof RC.ctxSync._state === "function"
              ? RC.ctxSync._state()
              : null;
          } catch (_) {}
          const pending = state && state.pend && typeof state.pend === "object"
            ? state.pend : {};
          const canonical = state && state.canonical && typeof state.canonical === "object"
            ? state.canonical : null;
          const pendingFile = pending.kind === "web" ? pending.url : pending.file;
          const viewBound = canonical && canonical.viewFile === pendingFile &&
            String(canonical.viewPage == null ? "" : canonical.viewPage) ===
              String(pending.pos == null ? "" : pending.pos);

          let selected = "";
          try { selected = window.getSelection ? window.getSelection().toString() : ""; }
          catch (_) {}
          if (!selected && typeof pending.selection === "string") {
            selected = pending.selection;
          }

          // Read text nodes that actually intersect the visible viewport. The
          // bounded walk avoids turning a long book or an infinite web page
          // into a costly full-DOM export.
          const pieces = [];
          let count = 0;
          let length = 0;
          try {
            const root = document.body || document.documentElement;
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode()) && count < 1800 && length < 18000) {
              count += 1;
              const parent = node.parentElement;
              if (!parent || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(parent.tagName)) continue;
              const value = String(node.nodeValue || "").replace(/\s+/g, " ").trim();
              if (!value) continue;
              const style = getComputedStyle(parent);
              if (style.display === "none" || style.visibility === "hidden" ||
                  Number(style.opacity || "1") === 0) continue;
              const range = document.createRange();
              range.selectNodeContents(node);
              const rects = Array.from(range.getClientRects());
              const visible = rects.some((rect) =>
                rect.width > 0 && rect.height > 0 && rect.bottom >= 0 && rect.right >= 0 &&
                rect.top <= innerHeight && rect.left <= innerWidth
              );
              if (!visible) continue;
              pieces.push(value);
              length += value.length + 1;
            }
          } catch (_) {}
          let visibleText = pieces.join("\n");
          if (!visibleText) {
            try { visibleText = (document.body && document.body.innerText) || ""; }
            catch (_) {}
          }

          return JSON.stringify({
            title: clean(pending.title || document.title, 1024),
            url: window.__BW_NATIVE_LOCAL_READER__ === true
              ? "" : clean(location.href, 2048),
            localBookID: clean(window.__BW_NATIVE_LOCAL_BOOK_ID__, 160),
            file: clean(viewBound ? canonical.file : (pendingFile || ""), 2048),
            page: clean(viewBound ? canonical.page : pending.pos, 64),
            pageCount: clean(pending.total, 64),
            selection: clean(selected, 4096),
            visibleText: clean(visibleText, 16384)
          });
        })()
        """#

        let raw: Any = try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Any, Error>) in
            webView.evaluateJavaScript(script) { value, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if let value {
                    continuation.resume(returning: value)
                } else {
                    continuation.resume(
                        throwing: NativeReaderCaptureError.invalidPagePayload
                    )
                }
            }
        }
        guard
            let json = raw as? String,
            let data = json.data(using: .utf8),
            let value = try? JSONDecoder().decode(
                NativeReaderJavaScriptSnapshot.self,
                from: data
            )
        else {
            throw NativeReaderCaptureError.invalidPagePayload
        }
        let route = ReaderNativeActivityRoute.url(
            for: .openReader,
            localBookID: value.localBookID.isEmpty ? nil : value.localBookID
        ) ?? ReaderNativeActivityRoute.url(for: .openReader)
        guard let route else {
            throw NativeReaderCaptureError.invalidPagePayload
        }
        return ReaderSharedSnapshot(
            title: value.title,
            url: route.absoluteString,
            file: value.file,
            page: value.page,
            pageCount: value.pageCount,
            selection: value.selection,
            visibleText: value.visibleText
        )
    }

    /// Takes a pixel-accurate image of only the currently visible WKWebView.
    /// It intentionally does not capture the full scrollable document. App
    /// Realtime delivery keeps this JPEG in native code; the binary response
    /// remains available for non-delivery callers and web fallbacks.
    func captureNativeViewportImage() async throws -> UIImage {
        guard webView.url != nil else {
            throw NativeReaderCaptureError.pageUnavailable
        }
        guard !webView.bounds.isEmpty else {
            throw NativeReaderCaptureError.emptyViewport
        }
        let configuration = WKSnapshotConfiguration()
        configuration.rect = webView.bounds
        let image: UIImage = try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<UIImage, Error>) in
            webView.takeSnapshot(with: configuration) { image, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if let image {
                    continuation.resume(returning: image)
                } else {
                    continuation.resume(
                        throwing: NativeReaderCaptureError.snapshotUnavailable
                    )
                }
            }
        }
        return image
    }

    @discardableResult
    func refreshNativeSharedSnapshot() async throws -> ReaderSharedSnapshot {
        let snapshot = try await captureNativeReaderSnapshot()
        try ReaderNativeFeatureStore().writeSnapshot(snapshot)
        return snapshot
    }
}

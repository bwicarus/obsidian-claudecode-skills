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
}

enum ReaderNativeVisualCaptureError: LocalizedError, Sendable {
    case invalidRegion
    case pageUnavailable
    case pencilOverlayUnavailable
    case hierarchyUnavailable
    case emptyViewport
    case hierarchyRenderFailed
    case jpegEncodingFailed
    case imageTooLarge(Int)

    var code: String {
        switch self {
        case .invalidRegion:
            return "BW_NATIVE_VISUAL_INVALID_REGION"
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
        case .jpegEncodingFailed:
            return "BW_NATIVE_VISUAL_JPEG_FAILED"
        case .imageTooLarge:
            return "BW_NATIVE_VISUAL_IMAGE_TOO_LARGE"
        }
    }

    var errorDescription: String? {
        switch self {
        case .invalidRegion:
            return "原生合成图区域无效"
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
        case .jpegEncodingFailed:
            return "原生合成图：JPEG 编码失败"
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
        let boundedScale = max(
            0.5,
            min(
                displayScale,
                Self.maximumLongEdge /
                    max(captureBounds.width, captureBounds.height)
            )
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

    private static func captureBounds(
        viewport: CGRect,
        region: ReaderNativeVisualCaptureRegion?
    ) throws -> CGRect {
        guard let region else { return viewport }
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
        return CGRect(
            x: viewport.minX + unit.minX * viewport.width,
            y: viewport.minY + unit.minY * viewport.height,
            width: unit.width * viewport.width,
            height: unit.height * viewport.height
        ).intersection(viewport)
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
    /// It intentionally does not capture the full scrollable document.
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

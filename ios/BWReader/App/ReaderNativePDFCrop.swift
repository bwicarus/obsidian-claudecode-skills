import Foundation
import PDFKit

/// Existing book-crop percentages expressed in rotated display space. Values
/// are not inferred from PDF boxes, and never rewrite the source document.
struct ReaderNativePDFCrop: Equatable {
    let left: CGFloat
    let right: CGFloat
    let top: CGFloat
    let bottom: CGFloat
    var width: CGFloat { 1 - left - right }
    var height: CGFloat { 1 - top - bottom }
    var percentages: [String: Double] {
        ["l": Double(left * 100), "r": Double(right * 100), "t": Double(top * 100), "b": Double(bottom * 100)]
    }

    func bounds(for page: PDFPage) -> CGRect? {
        guard let reference = page.pageRef else { return nil }
        let original = page.bounds(for: .cropBox)
        let rotation = (page.rotation % 360 + 360) % 360
        let size = rotation == 90 || rotation == 270
            ? CGSize(width: original.height, height: original.width) : original.size
        guard size.width > 0, size.height > 0 else { return nil }
        let transform = reference.getDrawingTransform(.cropBox, rect: CGRect(origin: .zero, size: size),
                                                      rotate: 0, preserveAspectRatio: false)
        let displayed = CGRect(x: left * size.width, y: bottom * size.height,
                               width: width * size.width, height: height * size.height)
        let box = displayed.applying(transform.inverted()).standardized.intersection(original)
        guard !box.isNull, !box.isInfinite, box.width > 0, box.height > 0,
              [box.minX, box.minY, box.width, box.height].allSatisfy(\.isFinite) else { return nil }
        return box
    }

    init?(_ value: [String: Any]) {
        guard Set(value.keys) == Set(["l", "r", "t", "b"]) else { return nil }
        func fraction(_ key: String) -> CGFloat? {
            guard let number = value[key] as? NSNumber,
                  CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite,
                  (0...45).contains(number.doubleValue) else { return nil }
            return CGFloat(number.doubleValue / 100)
        }
        guard let left = fraction("l"), let right = fraction("r"),
              let top = fraction("t"), let bottom = fraction("b") else { return nil }
        self.left = left; self.right = right; self.top = top; self.bottom = bottom
    }
}

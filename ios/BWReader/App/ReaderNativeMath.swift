import UIKit
import SwiftMath

@MainActor
enum ReaderNativeMath {
    static let sourceKey = NSAttributedString.Key("ReaderMathSource")
    private static let cache: NSCache<NSString, UIImage> = {
        let cache = NSCache<NSString, UIImage>(); cache.countLimit = 128; cache.totalCostLimit = 8 * 1024 * 1024; return cache
    }()
    static func render(latex: String, original: String, display: Bool, font: UIFont, color: UIColor) -> NSAttributedString? {
        guard !latex.isEmpty, latex.utf8.count <= 16_000 else { return nil }
        let resolved = color.resolvedColor(with: UITraitCollection.current)
        let key = "\(font.pointSize):\(resolved):\(display):" + latex
        let image: UIImage
        if let cached = cache.object(forKey: key as NSString) { image = cached }
        else {
            // Check geometry before allocating a bitmap for malformed or huge
            // formulae. Preserve the original formula on unsupported LaTeX.
            let label = MTMathUILabel()
            label.fontSize = font.pointSize; label.labelMode = display ? .display : .text
            label.latex = latex
            let size = label.intrinsicContentSize
            guard size.width.isFinite, size.height.isFinite, size.width > 0, size.height > 0,
                  size.width <= 4096, size.height <= 2048,
                  size.width * size.height * pow(UIScreen.main.scale, 2) <= 2_000_000 else { return nil }
            let (error, rendered) = MTMathImage(latex: latex, fontSize: font.pointSize, textColor: resolved, labelMode: display ? .display : .text).asImage()
            guard error == nil, let rendered else { return nil }
            image = rendered
            cache.setObject(image, forKey: key as NSString, cost: Int(image.size.width * image.size.height * image.scale * image.scale * 4))
        }
        let attachment = NSTextAttachment()
        attachment.image = image
        attachment.bounds = CGRect(x: 0, y: font.descender, width: image.size.width, height: image.size.height)
        let value = NSMutableAttributedString(attachment: attachment)
        value.addAttribute(sourceKey, value: original, range: NSRange(location: 0, length: value.length))
        return value
    }
    static func selectedText(_ content: NSAttributedString, range: NSRange) -> String {
        var result = ""
        content.enumerateAttribute(sourceKey, in: range) { value, span, _ in
            result += value as? String ?? (content.string as NSString).substring(with: span)
        }
        return result
    }
}

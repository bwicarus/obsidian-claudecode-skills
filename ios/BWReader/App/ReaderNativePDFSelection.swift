import Foundation
import JavaScriptCore
import CoreGraphics

/// Original block, table-cell, reading-order and word-boundary rules, evaluated
/// locally as data. No WebView, HTML renderer, host bridge or remote script.
@MainActor
final class ReaderNativePDFSelection {
    struct Value {
        let indexes: [Int]
        let text: String
        let sentence: String
        let rects: [CGRect]
    }
    private let context: JSContext
    private let page: JSValue
    private let count: Int
    private let width: Double
    private let height: Double
    private static let source: String? = {
        guard let root = Bundle.main.url(forResource: "ReaderBundle", withExtension: nil) else { return nil }
        return try? String(contentsOf: root.appendingPathComponent("native/pdf-selection-core.js"), encoding: .utf8)
    }()

    init(_ characters: NativeBookOCRPageCharacters) throws {
        guard let context = JSContext(), let source = Self.source,
              characters.pageWidth.isFinite, characters.pageHeight.isFinite,
              characters.pageWidth > 0, characters.pageHeight > 0 else { throw NativeBookOCRError.pageUnavailable }
        context.evaluateScript(source)
        guard context.exception == nil else { throw NativeBookOCRError.pageUnavailable }
        let input = try JSONSerialization.jsonObject(with: JSONEncoder().encode(characters))
        guard let constructor = context.objectForKeyedSubscript("BWNativePDFSelection"),
              let page = constructor.call(withArguments: [input]), context.exception == nil,
              !page.isUndefined, !page.isNull else { throw NativeBookOCRError.pageUnavailable }
        self.context = context; self.page = page
        count = characters.chars.count; width = characters.pageWidth; height = characters.pageHeight
    }

    func hit(_ point: CGPoint, anchor: Int? = nil, exactOnly: Bool = true) -> Int? {
        context.exception = nil
        let raw = page.invokeMethod("hit", withArguments: [Double(point.x) * width, Double(point.y) * height,
                                                          anchor ?? -1, exactOnly])
        guard context.exception == nil, let index = raw?.toNumber()?.intValue, index >= 0, index < count else { return nil }
        return index
    }

    func range(from start: Int, to end: Int) throws -> Value? { try invoke("range", arguments: [start, end]) }
    func exact(_ indexes: [Int]) throws -> Value? { try invoke("exact", arguments: [indexes]) }
    func sentence(_ indexes: [Int]) throws -> Value? { try invoke("sentence", arguments: [indexes]) }

    private func invoke(_ method: String, arguments: [Any]) throws -> Value? {
        context.exception = nil
        let result = page.invokeMethod(method, withArguments: arguments)
        guard context.exception == nil, let result, !result.isUndefined else { throw NativeBookOCRError.pageUnavailable }
        if result.isNull { return nil }
        guard let value = result.toDictionary(), let indexes = value["indexes"] as? [Int],
              indexes.allSatisfy({ $0 >= 0 && $0 < count }), let text = value["text"] as? String,
              let sentence = value["sentence"] as? String, let rects = value["rects"] as? [[Double]],
              rects.allSatisfy({ $0.count == 4 && $0.allSatisfy(\.isFinite) }) else { throw NativeBookOCRError.pageUnavailable }
        return Value(indexes: indexes, text: text, sentence: sentence, rects: rects.map {
            CGRect(x: $0[0] / width, y: $0[1] / height, width: ($0[2]-$0[0]) / width, height: ($0[3]-$0[1]) / height)
        })
    }
}

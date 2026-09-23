import Foundation
import CoreGraphics

/// Native text geometry. No per-page JavaScript VM, webpage raster or DOM
/// participates in a native gesture.
@MainActor
final class ReaderNativePDFSelection {
    struct Value {
        let indexes: [Int]
        let text: String
        let sentence: String
        let rects: [CGRect]
        let quality: String?
        let matches: Int
    }
    private let geometry: ReaderNativePDFTextGeometry

    init(_ characters: NativeBookOCRPageCharacters) throws {
        guard let page = try JSONSerialization.jsonObject(with: JSONEncoder().encode(characters)) as? [String: Any] else {
            throw NativeBookOCRError.pageUnavailable
        }
        geometry = try ReaderNativePDFTextGeometry(page: page)
    }

    func hit(_ point: CGPoint, anchor: Int? = nil, exactOnly: Bool = true) -> Int? {
        geometry.hit(x: Double(point.x) * geometry.width, y: Double(point.y) * geometry.height,
                     anchor: anchor, exactOnly: exactOnly)
    }

    func range(from start: Int, to end: Int) throws -> Value? { value(try geometry.range(from: start, to: end)) }
    func exact(_ indexes: [Int]) throws -> Value? { value(try geometry.exact(indexes)) }
    func sentence(_ indexes: [Int]) throws -> Value? { value(try geometry.sentence(indexes)) }
    func binding(_ input: [String: Any]) throws -> Value? { value(try geometry.binding(input)) }

    private func value(_ result: ReaderNativePDFTextGeometry.Result?) -> Value? {
        guard let result else { return nil }
        return Value(indexes: result.indexes, text: result.text, sentence: result.sentence,
                     rects: result.rects.map {
                         CGRect(x: $0.minX / geometry.width, y: $0.minY / geometry.height,
                                width: $0.width / geometry.width, height: $0.height / geometry.height)
                     }, quality: result.quality, matches: result.matches)
    }
}

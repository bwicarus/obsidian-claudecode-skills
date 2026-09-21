import Foundation
import PDFKit

func check(_ condition: @autoclosure () -> Bool, _ reason: String) {
    guard condition() else { fatalError(reason) }
}

let bytes = NSMutableData()
let consumer = CGDataConsumer(data: bytes)!
var media = CGRect(x: 0, y: 0, width: 500, height: 700)
let writer = CGContext(consumer: consumer, mediaBox: &media, nil)!
writer.beginPDFPage(nil)
writer.setFillColor(CGColor(gray: 0.5, alpha: 1)); writer.fill(media)
writer.endPDFPage(); writer.closePDF()
let original = CGRect(x: 37, y: 59, width: 300, height: 500)
let values: [String: Any] = ["l": 10, "r": 20, "t": 30, "b": 5]
let crop = ReaderNativePDFCrop(values)!
let cases: [(Int, CGRect)] = [
    (0, CGRect(x: 67, y: 84, width: 210, height: 325)),
    (90, CGRect(x: 127, y: 109, width: 195, height: 350)),
    (180, CGRect(x: 97, y: 209, width: 210, height: 325)),
    (270, CGRect(x: 52, y: 159, width: 195, height: 350))
]
for (rotation, expected) in cases {
    let document = PDFDocument(data: bytes as Data)!
    let page = document.page(at: 0)!
    page.setBounds(original, for: .cropBox); page.rotation = rotation
    // Re-read real PDF bytes, including inherited PDF rotation, just as the App.
    let source = document.dataRepresentation()!
    let reopened = PDFDocument(data: source)!
    let rendered = reopened.page(at: 0)!
    let result = crop.bounds(for: rendered)!
    for (actual, target) in zip([result.minX, result.minY, result.width, result.height],
                                [expected.minX, expected.minY, expected.width, expected.height]) {
        check(abs(actual - target) < 0.0001, "Rotation \(rotation): \(result) != \(expected)")
    }
    rendered.setBounds(result, for: .artBox)
    check(rendered.bounds(for: .cropBox) == original, "Display cropping rewrote original coordinates")
    check(PDFDocument(data: source)!.page(at: 0)!.bounds(for: .cropBox) == original, "Source bytes changed")
}
let invalidValues: [[String: Any]] = [
    ["l": true, "r": 0, "t": 0, "b": 0], ["l": -1, "r": 0, "t": 0, "b": 0],
    ["l": 46, "r": 0, "t": 0, "b": 0], ["l": Double.nan, "r": 0, "t": 0, "b": 0],
    ["l": 0, "r": 0, "t": 0], ["l": 0, "r": 0, "t": 0, "b": 0, "extra": 1]
]
for invalid in invalidValues { check(ReaderNativePDFCrop(invalid) == nil, "Invalid crop accepted: \(invalid)") }
print("Native PDF crop: four rotations, nonzero crop origin, source preservation and invalid input passed")

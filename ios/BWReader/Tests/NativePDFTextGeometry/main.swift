import Foundation

let fixtureURL = URL(fileURLWithPath: CommandLine.arguments[1])
let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as! [String: Any]
let pages = try (fixture["pages"] as! [[String: Any]]).map { try ReaderNativePDFTextGeometry(page: $0) }
let cases = fixture["cases"] as! [[String: Any]]
var failures = 0
func fail(_ description: String) { failures += 1; if failures < 16 { print(description) } }
for (index, item) in cases.enumerated() {
    let pageNumber = item["page"] as! Int, page = pages[pageNumber]
    let method = item["method"] as! String, args = item["args"] as! [Any]
    let tag = "case \(index) page \(pageNumber) \(method) \(args)"
    if method == "hit" {
        let expected = item["expected"] as! Int
        let actual = page.hit(x: args[0] as! Double, y: args[1] as! Double, anchor: args[2] as? Int, exactOnly: args[3] as! Bool) ?? -1
        if actual != expected { fail("\(tag): hit \(actual), expected \(expected)") }; continue
    }
    let actual: ReaderNativePDFTextGeometry.Result?
    switch method {
    case "range": actual = try page.range(from: args[0] as! Int, to: args[1] as! Int)
    case "exact": actual = try page.exact(args[0] as! [Int])
    case "sentence": actual = try page.sentence(args[0] as! [Int])
    default: actual = try page.binding(args[0] as! [String: Any])
    }
    guard let expected = item["expected"] as? [String: Any] else {
        if actual != nil { fail("\(tag): expected no selection") }; continue
    }
    guard let actual else { fail("\(tag): missing selection"); continue }
    if actual.indexes != expected["indexes"] as! [Int] { fail("\(tag): indexes \(actual.indexes), expected \(expected["indexes"]!)") }
    if actual.text != expected["text"] as! String { fail("\(tag): text \(actual.text.debugDescription), expected \(expected["text"]!)") }
    if actual.sentence != expected["sentence"] as! String { fail("\(tag): sentence differs") }
    if actual.quality != expected["quality"] as? String || actual.matches != expected["matches"] as! Int { fail("\(tag): binding quality/count differs") }
    let rects = expected["rects"] as! [[Double]]
    if rects.count != actual.rects.count { fail("\(tag): rect count \(actual.rects.count), expected \(rects.count)") }
    else {
        for (a, b) in zip(actual.rects, rects) {
            if zip([a.minX,a.minY,a.maxX,a.maxY],b).contains(where: { abs($0.0-$0.1)>0.00001 }) { fail("\(tag): rectangle differs \(a) vs \(b)") }
        }
    }
}
// UTF-16 source indices must not drift when one glyph is a surrogate pair or
// ligature. These are intentional corrections to the web text-search mapper.
let unicode = try ReaderNativePDFTextGeometry(page: ["page_w":100,"page_h":100,"chars":[
    ["c":"📖","x0":0,"y0":0,"x1":10,"y1":10,"bk":0],
    ["c":"fi","x0":10,"y0":0,"x1":20,"y1":10,"bk":0],
    ["c":"本","x0":20,"y0":0,"x1":30,"y1":10,"bk":0]]])
if try unicode.binding(["text":"fi本"])?.indexes != [1,2] { fail("Unicode/ligature binding changed source indices") }
if failures > 0 { fatalError("\(failures) native geometry parity failures") }
print("Native geometry matches \(cases.count) reference cases; Unicode source identity passed")

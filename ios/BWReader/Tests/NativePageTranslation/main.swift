import Foundation
typealias P = ReaderNativePageTranslation
func equal(_ a: Any, _ b: Any) throws -> Bool {
    let options: JSONSerialization.WritingOptions = [.sortedKeys,.withoutEscapingSlashes]
    return try JSONSerialization.data(withJSONObject:a,options:options) == JSONSerialization.data(withJSONObject:b,options:options)
}
let fixture = try JSONSerialization.jsonObject(with:Data(contentsOf:URL(fileURLWithPath:CommandLine.arguments[1]))) as! [String:Any]
for (index,item) in (fixture["cases"] as! [[String:Any]]).enumerated() {
    let sentences = P.sentences(item["chars"] as! [P.Row])
    let sentenceParity = try equal(sentences,item["sentences"]!)
    precondition(sentenceParity,"sentence segmentation differs \(index): \(sentences), expected \(item["sentences"]!)")
    let slices = P.slices(item["translated"] as! [P.Row])
    let sliceParity = try equal(slices,item["slices"]!)
    precondition(sliceParity,"translation layout differs \(index): \(slices)")
}
do { _ = try P.translated([["text":"one"]],status:200,data:Data(#"{"translations":[]}"#.utf8));preconditionFailure("incomplete translation accepted") } catch {}
do { _ = try P.translated([["text":"one"]],status:503,data:Data(#"{"translations":["一"]}"#.utf8));preconditionFailure("HTTP error accepted") } catch {}
let result = try P.translated([["text":"one"]],status:200,data:Data(#"{"translations":["一"]}"#.utf8))
precondition(result.first?["zh"] as? String == "一")
print("Native page translation segmentation, layout and response validation passed")

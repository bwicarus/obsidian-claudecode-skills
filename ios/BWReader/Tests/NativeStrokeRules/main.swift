import Foundation
let raw = try Data(contentsOf:URL(fileURLWithPath:CommandLine.arguments[1]))
let fixture = try JSONSerialization.jsonObject(with:raw) as! [String:Any]
for (i,item) in (fixture["hit"] as! [[String:Any]]).enumerated() {
    let actual = ReaderNativeStrokeRules.hit(item["stroke"] as! [String:Any],point:item["point"] as! [Double],threshold:0.018)
    precondition(actual == item["expected"] as! Bool,"Eraser parity case \(i)")
}
for (i,item) in (fixture["ordinal"] as! [[String:Any]]).enumerated() {
    var actual = item["input"] as! [[String:Any]]
    ReaderNativeStrokeRules.ensureRegionOrdinals(&actual)
    let encoded = try JSONSerialization.data(withJSONObject:actual,options:.sortedKeys)
    let expected = try JSONSerialization.data(withJSONObject:item["expected"]!,options:.sortedKeys)
    precondition(encoded == expected,"Region-number parity case \(i)")
}
print("Native stroke geometry and persistent region numbering match the existing engine")

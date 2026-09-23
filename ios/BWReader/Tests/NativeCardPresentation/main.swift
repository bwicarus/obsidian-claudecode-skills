import Foundation

let fixtures = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [[String: Any]]
func json(_ value: Any) -> String {
    String(decoding: try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .withoutEscapingSlashes]), as: UTF8.self)
}
for (index, item) in fixtures.enumerated() {
    let input = item["input"] as! [String: Any]
    let actual = ReaderNativeCardPresentation.interaction(input)!
    precondition(json(actual) == json(item["expected"]!), "card presentation differs \(index):\n\(json(actual))\n\(json(item["expected"]!))")
    let controls = actual["controls"] as! [[String: Any]]
    let fields = actual["fields"] as! [[String: Any]]
    var ids: [String: String] = [:]
    for control in controls { let key = control["key"] as! String; ids[key] = "action:" + key }
    for field in fields { let key = "edit-" + (field["key"] as! String); ids[key] = "action:" + key }
    let data = ReaderNativeCardPresentation.project(["nativeCard": input, "nativeCardActions": ids])
    precondition(data["live"] as? Bool == true)
    precondition((data["controls"] as! [[String: Any]]).allSatisfy { ($0["id"] as! String).hasPrefix("action:") })
    precondition(json(data["faces"]!) == json((actual["presentation"] as! [String: Any])["faces"]!))
}
print("Native card presentation: \(fixtures.count) browser parity cases passed")

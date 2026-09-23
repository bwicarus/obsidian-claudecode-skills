import Foundation

let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [[String: Any]]
let merger = ReaderUserStateMerge()!
func json(_ value: Any) throws -> Data { try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed, .withoutEscapingSlashes]) }
for (index, item) in cases.enumerated() {
    let domain = item["domain"] as! String, expected = item["expected"] as! [String: Any]
    let actual = try merger.merge(domain: domain, base: item["base"], mine: item["mine"], theirs: item["theirs"])
    guard try json(actual.value) == json(expected["value"]!), actual.changed == expected["changed"] as! Bool,
          actual.unknown == expected["unknown"] as! Bool,
          try merger.domainEmpty(domain: domain, value: actual.value) == item["empty"] as! Bool else {
        fatalError("Native merge disagreed on case \(index) domain \(domain)")
    }
}
do { _ = try merger.merge(domain: "notes", base: [], mine: [Double.nan], theirs: []); fatalError("invalid merge input accepted") } catch { }
print("Native book merge matches \(cases.count) reference cases; invalid input rejected")

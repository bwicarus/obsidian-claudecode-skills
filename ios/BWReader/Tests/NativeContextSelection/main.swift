import Foundation
func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
func canonical(_ value: Any) throws -> Data { try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) }
var state = ReaderNativeContextSelection()
let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [[String: Any]]
for (index, item) in cases.enumerated() {
    let now = (item["now"] as! NSNumber).doubleValue
    if let command = item["command"] as? [String: Any] { try state.apply(command, now: now) }
    else { state.expire(now: now) }
    let actual = state.snapshot(maxText: 100000)
    check(try canonical(actual) == canonical(item["expected"]!), "Context graph/expiry mismatch at \(index): \(actual)")
}
let before = try canonical(state.projection)
do {
    try state.apply(["operation":"select", "id":"bad", "record":["id":"other"]], now:0)
    fatalError("malformed record accepted")
} catch is ReaderNativeContextSelection.Failure { }
check(try canonical(state.projection) == before, "rejected operation changed context")
check((state.snapshot(limit: 0)["items"] as! [[String:Any]]).isEmpty, "empty limit ignored")
let reset = ReaderNativeContextSelection()
check((reset.snapshot()["items"] as! [[String:Any]]).isEmpty, "new document retained selection")
print("Native context selection: graph/identity parity, containment, cycles, expiry, nonrenewal and cancellation passed")

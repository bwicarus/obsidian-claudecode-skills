import Foundation

/// One frozen PDF authority per assistant request. Only the existing native
/// transaction/saga owners write data; this owner sequences their receipts.
final class ReaderNativeAssistantDocumentSession {
    typealias O = [String:Any]
    typealias F = ReaderNativeAssistantEdits.Failure
    let id: String
    let bookID: String
    private(set) var authority: O
    private(set) var sequence = 0
    private var closed = false
    private var busy = false

    init(id: String, authority: O) throws {
        guard UUID(uuidString:id) != nil,
              authority["contract"] as? String == "reader-native-pdf-assistant-state/1",
              let file = authority["file"] as? String, file.hasPrefix("localbook:"), file.count > 10,
              authority["revisions"] is O, JSONSerialization.isValidJSONObject(authority) else {
            throw F("原生 PDF 助手上下文无效")
        }
        self.id = id; self.bookID = String(file.dropFirst(10)); self.authority = authority
    }

    func close() { closed = true }

    func commit(_ events: [O], sequence next: Int,
                bookMutation: (O) throws -> O, pageCard: (O) throws -> O) throws -> O {
        guard !closed, !busy, next == sequence + 1, events.count <= 10000 else {
            throw F("助手事件已过期或重复，未再次执行")
        }
        busy = true
        defer { busy = false }
        do {
            // Validate the whole framing before making the first write. A
            // later storage failure retains earlier durable operation IDs.
            let batches: [[Any]] = try events.map { event in
                guard event["name"] as? String == "actions", let data = event["data"] as? String,
                      let actions = try JSONSerialization.jsonObject(with:Data(data.utf8)) as? [Any], actions.count <= 1000 else {
                    throw F("助手书籍动作列表无效")
                }
                return actions
            }
            var output: [O] = [], changes: [Any] = [], pageCardsChanged = false
            for (index, actions) in batches.enumerated() {
                let descriptors = actions.map { ($0 as? O).flatMap(ReaderNativeAssistantEdits.descriptor) }
                let cardIndices = descriptors.indices.filter { descriptors[$0]?.0.hasPrefix("page-card-") == true }
                var safe = actions
                if let cardIndex = cardIndices.first {
                    guard cardIndices.count == 1, descriptors.compactMap({ $0 }).count == 1,
                          let descriptor = descriptors[cardIndex], var action = actions[cardIndex] as? O,
                          var args = action["args"] as? [Any] else { throw F("页面卡片操作不能与其他本机改动混合") }
                    let receipt = try pageCard(["operation":"action","data":descriptor.1,"expectedState":authority])
                    guard receipt["ok"] as? Bool == true, let result = receipt["result"] as? O,
                          let revision = result["revision"], let journal = result["receipt"] as? O,
                          journal["contract"] as? String == "reader-native-page-card-action/1" else { throw F("页面卡片改动未确认") }
                    var data = descriptor.1
                    data["file"] = authority["file"]
                    data["item"] = ["id":descriptor.1["expected_id"] as? String ?? ""]
                    args[0] = data; action["args"] = args; safe[cardIndex] = action
                    var revisions = authority["revisions"] as! O; revisions["notes"] = revision
                    authority["revisions"] = revisions; authority.removeValue(forKey:"page_cards")
                    changes.append(contentsOf:receipt["changes"] as? [Any] ?? [])
                    pageCardsChanged = true
                } else if descriptors.contains(where:{ $0 != nil }) {
                    let receipt = try bookMutation(["bookID":bookID,
                        "mutationId":"assistant-\(id)-\(next)-\(index)","operation":"assistant-actions",
                        "value":["actions":actions,"expectedState":authority]])
                    guard receipt["ok"] as? Bool == true, let result = receipt["result"] as? O,
                          let committed = result["actions"] as? [Any], committed.count == actions.count,
                          let revisions = result["revisions"] as? O else { throw F("书页改动未确认") }
                    safe = committed; authority["revisions"] = revisions
                    if let journal = result["receipt"] as? O, journal["contract"] as? String == "reader-native-page-card-action/1" {
                        authority.removeValue(forKey:"page_cards")
                    }
                }
                output.append(["name":"actions","data":String(decoding:try JSONSerialization.data(withJSONObject:safe),as:UTF8.self)])
            }
            sequence = next
            return ["ok":true,"sequence":next,"file":authority["file"]!,"events":output,
                    "changes":changes,"pageCardsChanged":pageCardsChanged]
        } catch {
            // Never replay an unknown/partially committed batch on this stream.
            closed = true
            throw error
        }
    }
}

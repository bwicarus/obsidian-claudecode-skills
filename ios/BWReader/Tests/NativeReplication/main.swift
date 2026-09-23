import Foundation

func check(_ value:Bool,_ reason:String) { if !value { fatalError(reason) } }
let id = "mut-v2-" + String(repeating:"a",count:32)
for size in [0,1,195 * 1024,600 * 1024,1800 * 1024] {
    let content = String(repeating:"日🖊/",count:size/8)
    let object: [String:Any] = ["contract":"replication-command/1","op":["mutationId":id,"body":["text":content]]]
    let data = try JSONSerialization.data(withJSONObject:object,options:[.sortedKeys,.withoutEscapingSlashes])
    let requests = try ReaderNativeReplicationTransport.requests(data,mutationID:id)
    check(!requests.isEmpty,"empty transport plan")
    var joined = ""
    for (index,request) in requests.enumerated() {
        var frame = request.fields
        frame["contract"] = .string(DirectVoiceProtocol.contract); frame["type"] = .string(request.action)
        frame["requestId"] = .string(String(repeating:"r",count:160)); frame["sessionId"] = .string(String(repeating:"s",count:160))
        check(try JSONEncoder().encode(DirectJSONValue.object(frame)).count <= DirectVoiceProtocol.maximumMessageBytes,"encoded frame exceeds server limit")
        if let chunk = request.fields["chunk"]?.objectValue {
            check(chunk["seq"] == .number(Double(index)),"chunk order changed")
            check(chunk["total"] == .number(Double(requests.count)),"chunk total changed")
            joined += chunk["part"]!.stringValue!
        }
    }
    if requests.count > 1 { check(Data(base64Encoded:joined) == data,"Unicode envelope changed during chunking") }
}
func reply(_ mutation:String,_ outcome:String) -> DirectJSONValue {
    .object(["contract":.string("replication-command/1"),"mutationId":.string(mutation),"outcome":.string(outcome)])
}
check(try ReaderNativeReplicationTransport.outcome(reply(id,"accepted"),mutationID:id,partial:false) == "accepted","accepted reply refused")
for (value,partial) in [(reply("wrong","accepted"),false),(reply(id,"partial"),false),(reply(id,"accepted"),true),(reply(id,"applied"),false)] {
    do { _ = try ReaderNativeReplicationTransport.outcome(value,mutationID:id,partial:partial); fatalError("invalid reply accepted") }
    catch is DirectVoiceFailure { }
}
check(DirectVoiceConfiguration.readerContext.endpoint.path == "/reader-context/v1","replication acquired voice endpoint")
check(DirectVoiceConfiguration.production.endpoint == DirectVoiceProtocol.endpoint,"voice endpoint changed")
print("Native replication: Unicode chunk roundtrip, every frame bounded, exact final/partial receipts and dedicated endpoint passed")

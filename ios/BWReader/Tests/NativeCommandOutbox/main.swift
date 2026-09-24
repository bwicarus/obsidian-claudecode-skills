import Foundation

func check(_ value: @autoclosure () -> Bool, _ message: String) {
    if !value() { fatalError(message) }
}
func fails(_ message: String, _ body: () throws -> Void) {
    do { try body(); fatalError(message) } catch {}
}
let namespace = "acct-v1-" + String(repeating: "a", count: 64)
let otherNamespace = "acct-v1-" + String(repeating: "b", count: 64)
func command(_ id: Int, queue: String = "review:a", stamp: Int? = nil) -> [String: Any] {
    ["contract": "command-outbox/2", "recordType": "mutation", "ownerNamespace": namespace,
     "mutationId": "mut-v2-" + String(format: "%032x", id), "queueKey": queue,
     "url": "/pdf/api/review-answer", "method": "POST", "body": ["aid": "answer-\(id)"], "ts": stamp ?? id]
}
let store = try ReaderNativeDataStore(path: ":memory:")
let outbox = try ReaderNativeCommandOutbox(store: store, namespace: namespace)
let first = command(1), second = command(2), third = command(3)
try outbox.importRecords([first, second])
let captured = try outbox.pending()
let latest = outbox.selected(captured)
check(latest.count == 1 && latest[0].mutationID == (second["mutationId"] as? String), "必须合并同键最新命令")
try outbox.enqueue(third)
try outbox.acknowledge(latest[0], snapshot: captured, status: 200)
check(try! outbox.pending().count == 1, "回执不能删除发送期间的新操作")
try outbox.importRecords([first, second])
check(try! outbox.pending().count == 1, "残留旧记录不能重新激活已确认的操作")
var conflict = first; conflict["body"] = ["aid": "changed"]
fails("同一编号不同内容不能被静默接收") { try outbox.importRecords([conflict]) }
let snap = try outbox.pending()
try outbox.acknowledge(snap[0], snapshot: snap, status: 429)
check(try! outbox.pending().count == 1, "限流必须保留重试")
try outbox.acknowledge(snap[0], snapshot: snap, status: 503)
check(try! outbox.pending().count == 1, "服务器错误必须保留重试")
try outbox.enqueue(command(4))
let rejectedSnapshot = try outbox.pending()
try outbox.acknowledge(outbox.selected(rejectedSnapshot)[0], snapshot: rejectedSnapshot, status: 409)
check(try! outbox.pending().isEmpty, "终态拒绝后不能倒退重放被覆盖的旧命令")
check(try! outbox.entries().count == 2, "拒绝记录必须保留，不静默丢弃")
try outbox.importRecords([third, command(4)])
check(try! outbox.pending().isEmpty, "旧副本不能把已拒绝命令改回待发送")
check(try! store.journalCount() == 0, "本机传输状态不能成为学习数据的同步变更")
var wrongOwner = command(5); wrongOwner["ownerNamespace"] = otherNamespace
fails("不能跨账户导入") { try outbox.importRecords([command(6), wrongOwner]) }
check(try! outbox.pending().isEmpty, "整批导入失败不能留下一半数据")
let other = try ReaderNativeCommandOutbox(store: store, namespace: otherNamespace)
check(try! other.entries().isEmpty, "账户记录不能混在一起")
check(!ReaderNativeCommandOutbox.route("https://evil.test/pdf/api/review-answer", method: "POST"), "不能调用外站")
check(!ReaderNativeCommandOutbox.route("/pdf/api/../review-answer", method: "POST"), "不能绕过固定路由")
check(!ReaderNativeCommandOutbox.route("/pdf/api/entity/abc%2Fother", method: "PATCH"), "不能改变动态 ID 的路径边界")

let port = ReaderNativeCommandOutboxPort(store: { store })
let lease: [String: Any] = ["contract": "account-context-lease/1", "namespace": namespace,
    "contextId": "account-context-1", "generation": 1]
func request(_ operation: String, _ fields: [String: Any] = [:]) -> [String: Any] {
    var value: [String: Any] = ["contract": "command-outbox/2", "lease": lease, "operation": operation]
    fields.forEach { value[$0.key] = $0.value }; return value
}
_ = try port.handle(request("import", ["records": [command(10), command(11, queue: "review:b")]]))
let batch = try port.handle(request("capture"))
check((batch["ops"] as? [[String: Any]])?.count == 2, "捕获批次须带原操作")
let token = batch["token"] as! String
fails("不完整回执不能消耗批次") { _ = try port.handle(request("ack", ["token": token, "statuses": [200]])) }
check(try! outbox.pending().count == 2, "不完整回执不能删除任意操作")
var stale = request("ack", ["token": token, "statuses": [200, 200]])
var newLease = lease; newLease["generation"] = 2; stale["lease"] = newLease
fails("账户租约不匹配不能确认旧批次") { _ = try port.handle(stale) }
_ = try port.handle(request("ack", ["token": token, "statuses": [200, 503]]))
check(try! outbox.pending().count == 1, "部分结果只确认成功项")
fails("确认过的批次不能再次消费") { _ = try port.handle(request("ack", ["token": token, "statuses": [200, 200]])) }
let pendingBatch = try port.handle(request("capture"))
port.invalidate()
fails("导航后不能使用旧回执") { _ = try port.handle(request("ack", ["token": pendingBatch["token"]!, "statuses": [200]])) }
check(try! outbox.pending().count == 1, "导航只释放内存批次，不能删除未发送记录")

let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
defer { try? FileManager.default.removeItem(at: root) }
let host = ReaderNativeDataStoreHost(root: root)
let durable = try host.bridge(for: "bw-reader-native-v1-transport").store
try ReaderNativeCommandOutbox(store: durable, namespace: namespace).enqueue(command(12))
try host.resetDeviceStore()
host.closeAll()
let reopened = try host.bridge(for: "bw-reader-native-v1-transport").store
check(try! ReaderNativeCommandOutbox(store: reopened, namespace: namespace).pending().count == 1,
    "退出与设备缓存回收不能清掉未送达命令")
host.closeAll()
print("native command outbox checks passed")

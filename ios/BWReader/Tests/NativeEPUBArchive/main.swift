import Foundation

let root = URL(fileURLWithPath: CommandLine.arguments[1])
let service = ReaderNativeEPUBArchive()
let url = root.appendingPathComponent("valid.epub")
let entries = try await service.list(url: url, identity: "valid")
precondition(entries.count == 8)
let publication = try await service.describe(url: url, identity: "valid")
precondition(publication["title"] as? String == "Reader & Test")
let spine = publication["spine"] as! [[String: Any]], toc = publication["toc"] as! [[String: Any]]
precondition(spine.map { $0["path"] as! String } == ["OPS/two.xhtml", "OPS/章一.xhtml"], "spine order changed")
precondition(toc.map { $0["idx"] as! Int } == [1, 0] && toc.map { $0["label"] as! String } == ["第一 章", "Second"], "TOC selection, percent href or dedup changed")
for name in ["ncx", "fallback"] {
    let value = try await service.describe(url: root.appendingPathComponent(name + ".epub"), identity: name)
    let rows = value["toc"] as! [[String: Any]]
    precondition(rows.first?["label"] as? String == (name == "ncx" ? "NCX label" : "two.xhtml"))
    precondition(rows.first?["idx"] as? Int == 0)
}
let original = try Data(contentsOf: root.appendingPathComponent("chapter.bin"))
let read = try await service.read(url: url, identity: "valid", path: "OPS/章一.xhtml", maximumBytes: 8 * 1024 * 1024)
precondition(read == original, "chapter bytes and anchors changed")
let composed = try await service.read(url: url, identity: "valid", path: "OPS/é.txt", maximumBytes: 8 * 1024 * 1024)
let decomposed = try await service.read(url: url, identity: "valid", path: "OPS/e\u{0301}.txt", maximumBytes: 8 * 1024 * 1024)
precondition(composed != decomposed, "distinct ZIP UTF-8 paths were conflated")
for path in ["OPS/large.bin", "missing", "../secret"] {
    do {
        _ = try await service.read(url: url, identity: "valid", path: path, maximumBytes: 8 * 1024 * 1024)
        preconditionFailure("invalid text read accepted: \(path)")
    } catch is ReaderNativeEPUBArchive.Failure {}
}
let large = try await service.read(url: url, identity: "valid", path: "OPS/large.bin", maximumBytes: 32 * 1024 * 1024)
precondition(large.count == 8 * 1024 * 1024 + 1)
for name in ["duplicate", "traversal", "bomb", "symlink"] {
    do {
        _ = try await service.list(url: root.appendingPathComponent(name + ".epub"), identity: name)
        preconditionFailure("invalid archive accepted: \(name)")
    } catch is ReaderNativeEPUBArchive.Failure {}
}
do {
    _ = try await service.read(url: root.appendingPathComponent("corrupt.epub"), identity: "corrupt", path: "chapter", maximumBytes: 8 * 1024 * 1024)
    preconditionFailure("corrupt chapter accepted")
} catch {}
let reopened = try await service.read(url: url, identity: "reopen", path: "OPS/章一.xhtml", maximumBytes: 8 * 1024 * 1024)
precondition(reopened == original, "failed book open poisoned the next archive")
print("Native EPUB archive checks passed")

import Foundation
import PDFKit

/// Search reads the existing OCR index; bookmarks reuse the displayed PDF.
/// Opaque result IDs belong to one document/scope/query and never survive a
/// newer read. Neither operation asks the hidden renderer to load a page.
@MainActor
final class ReaderNativePDFIndex {
    private struct Entry { let page: Int; let query: String }
    private var scope = ""
    private var bookID = ""
    private var digest = ""
    private var searchSequence = 0
    private var search: [String:Entry] = [:]
    private var toc: [String:Entry] = [:]

    func reset() {
        scope = ""; bookID = ""; digest = ""; searchSequence += 1
        search.removeAll(); toc.removeAll()
    }

    func perform(_ command: [String:Any], bookID: String, digest: String, scope: String,
                 document: ReaderNativePDFDocument, navigation: ReaderNativePDFNavigationBridge,
                 isCurrent: () -> Bool) async throws -> [String:Any] {
        if self.scope != scope || self.bookID != bookID || self.digest != digest {
            reset(); self.scope = scope; self.bookID = bookID; self.digest = digest
        }
        func current() throws {
            try Task.checkCancellation()
            guard isCurrent(), self.scope == scope, self.bookID == bookID, self.digest == digest,
                  document.matches(bookID:bookID,contentSHA256:digest) else { throw CancellationError() }
        }
        try current()
        switch command["action"] as? String {
        case "navigationRead": return ["ok":true,"value":try navigation.state()]
        case "navigationAction":
            let value = try await navigation.navigate(command["text"] as? String ?? "",value:command["value"])
            try current(); return ["ok":true,"value":value]
        case "tocRead":
            guard let pdf = document.view.document else { throw NativeBookOCRError.pageUnavailable }
            toc.removeAll()
            let rows = ReaderLocalRuntimeServer.nativeTOCEntries(in:pdf).compactMap { row -> [String:Any]? in
                guard let page = row["page"] as? Int else { return nil }
                let id = UUID().uuidString
                toc[id] = Entry(page:page,query:"")
                return ["id":id,"title":row["title"] ?? "","level":row["level"] ?? 1,"label":"P\(navigation.displayPage(page))"]
            }
            return ["ok":true,"value":rows]
        case "searchRead":
            searchSequence += 1; let ticket = searchSequence; search.removeAll()
            let query = (command["text"] as? String ?? "").trimmingCharacters(in:.whitespacesAndNewlines)
            guard query.count <= 256 else { throw ReaderNativeTurnStore.Failure(message:"搜索文字过长") }
            if query.isEmpty { return ["ok":true,"value":["results":[],"total":0,"pages":0,"incomplete":false]] }
            let result = try await NativeBookOCRManager.shared.search(bookID:bookID,expectedContentSHA256:digest,query:query,limit:200)
            try current()
            guard ticket == searchSequence else { throw CancellationError() }
            let grouped = Dictionary(grouping:result.matches,by:\.page)
            let rows = grouped.keys.sorted().map { page -> [String:Any] in
                let id = UUID().uuidString, hits = grouped[page] ?? []
                search[id] = Entry(page:page,query:query)
                return ["id":id,"label":"P\(navigation.displayPage(page))","excerpt":hits.first?.text ?? "","count":hits.count]
            }
            return ["ok":true,"value":["results":rows,"total":result.total,"pages":result.pages.count,"incomplete":result.incomplete]]
        case "tocJump", "searchJump":
            let searching = command["action"] as? String == "searchJump"
            guard let id = command["actionId"] as? String, let entry = searching ? search[id] : toc[id] else {
                throw ReaderNativeTurnStore.Failure(message:"结果已更新，请重新选择")
            }
            _ = try await navigation.navigate("jump",value:entry.page)
            try current()
            if searching {
                // Position succeeds even when OCR geometry is concurrently being
                // refreshed. Highlight only the still-selected result/page.
                _ = try? await document.prepareBinding(page:entry.page,text:entry.query)
                try current()
                if search[id]?.query == entry.query, document.position.page == entry.page {
                    document.highlightSearchHits(query:entry.query,page:entry.page)
                }
            }
            return ["ok":true]
        default: throw ReaderNativeTurnStore.Failure(message:"未知阅读索引操作")
        }
    }
}

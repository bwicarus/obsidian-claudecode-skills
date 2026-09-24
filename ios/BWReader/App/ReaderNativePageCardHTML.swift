import Foundation
import SwiftSoup

enum ReaderNativePageCardHTML {
    static func sanitize(_ html: String) throws -> (content:String,text:String) {
        let document = try SwiftSoup.parseBodyFragment(html)
        document.outputSettings().prettyPrint(pretty:false)
        for item in try document.select("script,style,iframe,object,embed,form,base,meta,link,svg,math").array() { try item.remove() }
        let allowed = try Whitelist.relaxed().addTags("span","div","ruby","rt","rp","del","s","input","button","label","section","article","header","footer","main","aside","figure","figcaption","details","summary","mark","time","hr","wbr")
            .addAttributes(":all","class","style","title","id","role","hidden","dir","lang","width","height")
            .addAttributes("input","type","value","checked","disabled")
            .addAttributes("button","type","disabled")
            .addProtocols("img","src","data","blob")
            .preserveRelativeLinks(true)
        // Preserve the stored tool-card data contract, while the sanitizer still
        // rejects event handlers, executable URLs and non-HTML namespaces.
        for item in try document.select("*").array() {
            for attribute in item.getAttributes()?.asList() ?? [] {
                let key = attribute.getKey().lowercased()
                if key.hasPrefix("data-") || key.hasPrefix("aria-") { _ = try allowed.addAttributes(item.tagName(),key) }
            }
        }
        // Preserve inline whitespace. Pretty-printing here would change stored
        // text and replay fingerprints merely because an edit was retried.
        let clean = try SwiftSoup.clean(document.body()?.html() ?? "", "", allowed, document.outputSettings()) ?? ""
        let safe = try SwiftSoup.parseBodyFragment(clean)
        safe.outputSettings().prettyPrint(pretty:false)
        var texts:[String] = [], nodes:[Node] = safe.body().map { [$0] } ?? []
        while let node = nodes.popLast() {
            if let text = node as? TextNode { texts.append(text.getWholeText()) }
            else { nodes.append(contentsOf:node.getChildNodes().reversed()) }
        }
        return (try safe.body()?.html() ?? "",texts.joined())
    }
}

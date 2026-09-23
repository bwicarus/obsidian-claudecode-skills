import Foundation
import CoreFoundation

/// Device viewport geometry and the replicated page remain distinct: scrolling
/// within a page updates only local viewport state, not the command queue.
enum ReaderNativeReadingPosition {
    static func validated(_ value:[String:Any]) throws -> [String:Any] {
        func number(_ key:String,_ range:ClosedRange<Double>,integer:Bool = false) throws -> Double {
            guard let n = value[key] as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(),
                  n.doubleValue.isFinite, range.contains(n.doubleValue), !integer || n.doubleValue.rounded() == n.doubleValue else {
                throw ReaderNativeBookStore.MutationError.invalid("阅读位置 " + key)
            }
            return n.doubleValue
        }
        let page = try number("page",1...10_000_000,integer:true), fraction = try number("fraction",0...1), scale = try number("scale",0.001...100)
        let offset = try number("spreadOffset",0...1,integer:true)
        guard let mode = value["mode"] as? String, ["single","continuous","spread"].contains(mode),
              let flag = value["cropEnabled"] as? NSNumber, CFGetTypeID(flag) == CFBooleanGetTypeID() else {
            throw ReaderNativeBookStore.MutationError.invalid("阅读排版模式")
        }
        var result:[String:Any] = ["page":Int(page),"fraction":fraction,"scale":scale,"mode":mode,"spreadOffset":Int(offset),"cropEnabled":flag.boolValue]
        if flag.boolValue {
            guard let crop = value["crop"] as? [String:Any], Set(crop.keys) == Set(["l","r","t","b"]),
                  crop.values.allSatisfy({ raw in
                    guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return false }
                    return number.doubleValue.isFinite && (0...45).contains(number.doubleValue)
                  }) else { throw ReaderNativeBookStore.MutationError.invalid("去边参数") }
            result["crop"] = crop
        }
        return result
    }

    static func restore(store:ReaderNativeDataStore,bookID:String,total:Int) throws -> [String:Any]? {
        guard total > 0 else { return nil }
        let read = ReaderNativeBookProjection(store:store)
        return try store.inTransaction {
            guard let raw = try read.state("pdf-viewport",bookID:bookID).payload as? [String:Any] else { return nil }
            var viewport = try validated(raw)
            let page = viewport["page"] as! Int
            let position = try read.state("reading-position",bookID:bookID).payload as? [String:Any]
            let saved = (position?["pos"] as? NSNumber)?.intValue ?? page
            let resolved = min(total,max(1,saved))
            if resolved != page { viewport["fraction"] = 0 }
            viewport["page"] = resolved; viewport["total"] = total; viewport["file"] = "localbook:" + bookID
            return viewport
        }
    }
}

import SwiftUI
import SwiftSoup

/// Original HTML stays unchanged. Tables are projected into native cells while
/// TextKit continues to own text selection and ruby inside each cell.
@MainActor
struct ReaderNativeRichDocument: View {
    let content: String
    var format = "markdown"
    var onSelection: ((String) -> Void)?

    var body: some View {
        if format == "html", content.range(of: "<table\\b", options: [.regularExpression, .caseInsensitive]) != nil,
           let blocks = ReaderNativeDocumentParser.blocks(content) {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                    switch block {
                    case .text(let html):
                        ReaderNativeRichText(content: html, format: "html", onSelection: onSelection)
                    case .table(let table):
                        ReaderNativeTable(table: table, onSelection: onSelection)
                    }
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        } else {
            ReaderNativeRichText(content: content, format: format, onSelection: onSelection)
        }
    }
}

private enum ReaderNativeDocumentBlock {
    case text(String)
    case table(ReaderNativeTableData)
}

private struct ReaderNativeTableData {
    struct Cell: Identifiable {
        let id: Int
        let row: Int
        let column: Int
        let rows: Int
        let columns: Int
        let header: Bool
        let html: String
    }
    let caption: String
    let columns: Int
    let rows: Int
    let cells: [Cell]
}

@MainActor
private enum ReaderNativeDocumentParser {
    private final class Cached: NSObject {
        let blocks: [ReaderNativeDocumentBlock]
        init(_ blocks: [ReaderNativeDocumentBlock]) { self.blocks = blocks }
    }
    private static let cache: NSCache<NSString, Cached> = {
        let cache = NSCache<NSString, Cached>()
        cache.countLimit = 64; cache.totalCostLimit = 4 * 1_024 * 1_024
        return cache
    }()
    static func blocks(_ html: String) -> [ReaderNativeDocumentBlock]? {
        if let cached = cache.object(forKey: html as NSString) { return cached.blocks }
        guard let body = try? SwiftSoup.parseBodyFragment(html).body() else { return nil }
        var result: [ReaderNativeDocumentBlock] = []
        var pending = ""
        func flush() {
            if !pending.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { result.append(.text(pending)) }
            pending = ""
        }
        func visit(_ node: Node) {
            if let element = node as? Element {
                if ["script", "style", "iframe"].contains(element.tagName()) { return }
                if element.tagName() == "table" {
                    flush()
                    if let value = table(element) { result.append(.table(value)) }
                    else { result.append(.text((try? element.outerHtml()) ?? "")) }
                    return
                }
                if (try? element.select("table").isEmpty()) == false {
                    flush()
                    for child in element.getChildNodes() { visit(child) }
                    flush()
                    return
                }
            }
            pending += (try? node.outerHtml()) ?? ""
        }
        for node in body.getChildNodes() { visit(node) }
        flush()
        cache.setObject(Cached(result), forKey: html as NSString, cost: html.utf8.count * 3)
        return result
    }

    private static func table(_ element: Element) -> ReaderNativeTableData? {
        var rows: [Element] = []
        var groupEnds: [Int: Int] = [:]
        func collect(_ parent: Element) {
            let start = rows.count
            for node in parent.getChildNodes() {
                guard let child = node as? Element else { continue }
                if child.tagName() == "tr" { rows.append(child) }
                else if ["thead", "tbody", "tfoot"].contains(child.tagName()) { collect(child) }
            }
            for index in start..<rows.count where groupEnds[index] == nil { groupEnds[index] = rows.count }
        }
        collect(element)
        guard !rows.isEmpty, rows.count <= 512 else { return nil }
        var occupied: Set<String> = []
        var cells: [ReaderNativeTableData.Cell] = []
        var columns = 0
        var totalRows = rows.count
        for (r, row) in rows.enumerated() {
            var column = 0
            for node in row.getChildNodes() {
                guard let cell = node as? Element, ["td", "th"].contains(cell.tagName()) else { continue }
                let columnSpan = max(1, Int((try? cell.attr("colspan")) ?? "") ?? 1)
                let rawRowSpan = Int((try? cell.attr("rowspan")) ?? "") ?? 1
                let remainingRows = (groupEnds[r] ?? rows.count) - r
                let rowSpan = rawRowSpan == 0 ? remainingRows : min(remainingRows, max(1, rawRowSpan))
                // Malformed layouts remain readable as original text, without
                // allocating an unbounded native grid from untrusted markup.
                guard columnSpan <= 32, rowSpan <= 512 else { return nil }
                while (column..<(column + columnSpan)).contains(where: { occupied.contains("\(r):\($0)") }) { column += 1 }
                guard column + columnSpan <= 32 else { return nil }
                for y in r..<(r + rowSpan) {
                    for x in column..<(column + columnSpan) { occupied.insert("\(y):\(x)") }
                }
                let inner = (try? cell.html()) ?? ""
                let isHeader = cell.tagName() == "th"
                cells.append(.init(id: cells.count, row: r, column: column, rows: rowSpan,
                                   columns: columnSpan, header: isHeader, html: isHeader ? "<strong>\(inner)</strong>" : inner))
                columns = max(columns, column + columnSpan)
                totalRows = max(totalRows, r + rowSpan)
                column += columnSpan
            }
        }
        guard columns > 0 else { return nil }
        let caption = (try? element.select("caption").first()?.text()) ?? ""
        return ReaderNativeTableData(caption: caption, columns: columns, rows: totalRows, cells: cells)
    }
}

private struct ReaderTableCellKey: LayoutValueKey {
    static let defaultValue = [0, 0, 1, 1]
}

private struct ReaderNativeTableLayout: Layout {
    let columns: Int
    let rows: Int

    private func heights(width: CGFloat, subviews: Subviews) -> [CGFloat] {
        let cellWidth = width / CGFloat(columns)
        var result = Array(repeating: CGFloat(32), count: rows)
        // Single-row cells set the baseline; spanning cells grow only the rows
        // they occupy, so narrow layouts never clip their full text.
        for view in subviews.sorted(by: { $0[ReaderTableCellKey.self][2] < $1[ReaderTableCellKey.self][2] }) {
            let slot = view[ReaderTableCellKey.self]
            let measured = view.sizeThatFits(.init(width: CGFloat(slot[3]) * cellWidth, height: nil)).height
            let current = result[slot[0]..<(slot[0] + slot[2])].reduce(0, +)
            if measured > current {
                let extra = (measured - current) / CGFloat(slot[2])
                for row in slot[0]..<(slot[0] + slot[2]) { result[row] += extra }
            }
        }
        return result
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? CGFloat(columns) * 140
        return CGSize(width: width, height: heights(width: width, subviews: subviews).reduce(0, +))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let heights = heights(width: bounds.width, subviews: subviews)
        let cellWidth = bounds.width / CGFloat(columns)
        var positions: [CGFloat] = [0]
        for height in heights { positions.append(positions.last! + height) }
        for view in subviews {
            let slot = view[ReaderTableCellKey.self]
            view.place(at: CGPoint(x: bounds.minX + CGFloat(slot[1]) * cellWidth, y: bounds.minY + positions[slot[0]]),
                proposal: .init(width: CGFloat(slot[3]) * cellWidth, height: positions[slot[0] + slot[2]] - positions[slot[0]]))
        }
    }
}

@MainActor
private struct ReaderNativeTable: View {
    let table: ReaderNativeTableData
    let onSelection: ((String) -> Void)?
    @State private var availableWidth: CGFloat = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            if !table.caption.isEmpty { Text(table.caption).font(.caption.weight(.medium)).textSelection(.enabled) }
            ScrollView(.horizontal) {
                ReaderNativeTableLayout(columns: table.columns, rows: table.rows) {
                    ForEach(table.cells) { cell in
                        ReaderNativeRichText(content: cell.html, format: "html", onSelection: onSelection)
                            .padding(8).frame(maxHeight: .infinity, alignment: .topLeading)
                            .background(cell.header ? ReaderNativeTheme.accent.opacity(0.09) : Color.clear)
                            .overlay(Rectangle().stroke(ReaderNativeTheme.muted.opacity(0.2), lineWidth: 0.5))
                            .layoutValue(key: ReaderTableCellKey.self, value: [cell.row, cell.column, cell.rows, cell.columns])
                    }
                }.frame(width: max(availableWidth, CGFloat(table.columns) * 120))
            }
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { availableWidth = $0 }
        }
    }
}

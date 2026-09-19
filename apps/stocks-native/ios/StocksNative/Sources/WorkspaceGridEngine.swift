import Foundation

struct WorkspaceGridRect: Codable, Equatable, Hashable {
    var column: Int
    var row: Int
    var width: Int
    var height: Int
    var maxColumn: Int { column + width }
    var maxRow: Int { row + height }
}

enum WorkspaceGridEdge: String, Codable { case top, bottom, left, right }
enum WorkspaceGridAxis: String, Codable { case horizontal, vertical }

struct WorkspaceGridSnapTarget: Equatable {
    let edge: WorkspaceGridEdge
    let coordinate: Int
    let peerIDs: [String]
    let primaryID: String?
}

struct WorkspaceGridSharedEdge: Equatable, Identifiable {
    let axis: WorkspaceGridAxis
    let coordinate: Int
    let rangeStart: Int
    let rangeEnd: Int
    let leadingIDs: [String]
    let trailingIDs: [String]
    var id: String { "\(axis.rawValue):\(coordinate)" }
}

/// Integer-grid geometry shared by the editor and the live native workspace.
/// As in the original canvas, a moving anchor stays put and collisions flow downward.
enum WorkspaceGridEngine {
    static let columnCount = 12
    static let rowHeight: CGFloat = 30
    static let gap: CGFloat = 6

    static func clamped(_ rect: WorkspaceGridRect) -> WorkspaceGridRect {
        let width = min(max(rect.width, 1), columnCount)
        return WorkspaceGridRect(column: min(max(rect.column, 0), columnCount - width),
                                 row: min(max(rect.row, 0), 1_000_000),
                                 width: width, height: min(max(rect.height, 1), 1_000_000))
    }

    static func overlaps(_ lhs: WorkspaceGridRect, _ rhs: WorkspaceGridRect) -> Bool {
        lhs.column < rhs.maxColumn && rhs.column < lhs.maxColumn
            && lhs.row < rhs.maxRow && rhs.row < lhs.maxRow
    }

    static func normalized(_ cards: [WorkspaceCard]) -> [WorkspaceCard] {
        var result = cards
        // Migrate the former half/full rows in their original order, including hidden cards.
        if !result.isEmpty && result.allSatisfy({ $0.grid == nil }) {
            var column = 0, row = 0, lineHeight = 0
            for index in result.indices {
                let width = result[index].span == .full ? columnCount : columnCount / 2
                let height = result[index].kind.defaultGridHeight
                if column + width > columnCount || (width == columnCount && column > 0) {
                    row += lineHeight; column = 0; lineHeight = 0
                }
                result[index].grid = WorkspaceGridRect(column: column, row: row, width: width, height: height)
                column += width
                lineHeight = max(lineHeight, height)
                if column == columnCount { row += lineHeight; column = 0; lineHeight = 0 }
            }
        } else {
            for index in result.indices {
                if let rect = result[index].grid { result[index].grid = clamped(rect) }
            }
            for index in result.indices where result[index].grid == nil {
                let width = result[index].span == .full ? columnCount : columnCount / 2
                result[index].grid = firstAvailable(width: width, height: result[index].kind.defaultGridHeight, in: result)
            }
        }
        return resolveCollisions(result, anchors: [])
    }

    static func firstAvailable(width: Int, height: Int, in cards: [WorkspaceCard]) -> WorkspaceGridRect {
        let size = clamped(WorkspaceGridRect(column: 0, row: 0, width: width, height: height))
        // Hidden cards reserve their original home when adding a new card.
        let occupied = cards.compactMap(\.grid).map(clamped)
        let candidateRows = Set([0] + occupied.map(\.maxRow)).sorted()
        for row in candidateRows {
            for column in 0...(columnCount - size.width) {
                let candidate = WorkspaceGridRect(column: column, row: row, width: size.width, height: size.height)
                if !occupied.contains(where: { overlaps(candidate, $0) }) { return candidate }
            }
        }
        return WorkspaceGridRect(column: 0, row: occupied.map(\.maxRow).max() ?? 0,
                                 width: size.width, height: size.height)
    }

    static func move(_ cards: [WorkspaceCard], id: String, to rect: WorkspaceGridRect) -> [WorkspaceCard] {
        var result = normalized(cards)
        guard let index = result.firstIndex(where: { $0.id == id }) else { return result }
        result[index].grid = clamped(rect)
        return resolveCollisions(result, anchors: [id])
    }

    static func resize(_ cards: [WorkspaceCard], id: String, to rect: WorkspaceGridRect) -> [WorkspaceCard] {
        move(cards, id: id, to: rect)
    }

    static func snapTarget(for rect: WorkspaceGridRect, in cards: [WorkspaceCard], excluding id: String,
                           columnTolerance: Double = 0.5, rowTolerance: Double = 0.75) -> WorkspaceGridSnapTarget? {
        snapTarget(for: CGRect(x: CGFloat(rect.column), y: CGFloat(rect.row), width: CGFloat(rect.width), height: CGFloat(rect.height)),
                   in: cards, excluding: id, columnTolerance: columnTolerance, rowTolerance: rowTolerance)
    }

    static func snapTarget(for rect: CGRect, in cards: [WorkspaceCard], excluding id: String,
                           columnTolerance: Double = 0.5, rowTolerance: Double = 0.75) -> WorkspaceGridSnapTarget? {
        guard rect.minX.isFinite, rect.minY.isFinite, rect.width.isFinite, rect.height.isFinite else { return nil }
        let peers = normalized(cards).filter { $0.id != id && $0.isVisible }
        var best: WorkspaceGridSnapTarget?
        var bestScore = Double.infinity
        func consider(_ edge: WorkspaceGridEdge, coordinate: Int, distance: Double,
                      tolerance: Double, primary: String?, group: [String]) {
            guard tolerance > 0, distance <= tolerance else { return }
            let score = distance / tolerance
            guard score < bestScore else { return }
            bestScore = score
            best = WorkspaceGridSnapTarget(edge: edge, coordinate: coordinate, peerIDs: group, primaryID: primary)
        }
        for peer in peers {
            guard let other = peer.grid else { continue }
            let horizontalOverlap = Double(rect.maxX) > Double(other.column) && Double(rect.minX) < Double(other.maxColumn)
            let verticalOverlap = Double(rect.maxY) > Double(other.row) && Double(rect.minY) < Double(other.maxRow)
            for edge in [WorkspaceGridEdge.top, .bottom, .left, .right] {
                let coordinate = boundary(other, edge)
                let distance: Double
                let tolerance: Double
                switch edge {
                case .top: guard horizontalOverlap else { continue }; distance = abs(Double(rect.maxY) - Double(coordinate)); tolerance = rowTolerance
                case .bottom: guard horizontalOverlap else { continue }; distance = abs(Double(rect.minY) - Double(coordinate)); tolerance = rowTolerance
                case .left: guard verticalOverlap else { continue }; distance = abs(Double(rect.maxX) - Double(coordinate)); tolerance = columnTolerance
                case .right: guard verticalOverlap else { continue }; distance = abs(Double(rect.minX) - Double(coordinate)); tolerance = columnTolerance
                }
                let group = peers.filter { $0.grid.map { boundary($0, edge) == coordinate } == true }.map(\.id)
                consider(edge, coordinate: coordinate, distance: distance, tolerance: tolerance, primary: peer.id, group: group)
            }
        }
        // The infinitely scrolling canvas has top/left/right borders, but no artificial bottom.
        consider(.left, coordinate: 0, distance: abs(Double(rect.minX)), tolerance: columnTolerance, primary: nil, group: [])
        consider(.right, coordinate: columnCount, distance: abs(Double(rect.maxX) - Double(columnCount)), tolerance: columnTolerance, primary: nil, group: [])
        consider(.top, coordinate: 0, distance: abs(Double(rect.minY)), tolerance: rowTolerance, primary: nil, group: [])
        return best
    }

    static func dock(_ cards: [WorkspaceCard], id: String, target: WorkspaceGridSnapTarget) -> [WorkspaceCard] {
        var result = normalized(cards)
        guard let movingIndex = result.firstIndex(where: { $0.id == id }), var moving = result[movingIndex].grid else { return result }
        guard let primaryID = target.primaryID else {
            switch target.edge {
            case .left: moving.column = 0
            case .right: moving.column = columnCount - moving.width
            case .top: moving.row = 0
            case .bottom: return result
            }
            return move(result, id: id, to: moving)
        }
        guard let primary = result.first(where: { $0.id == primaryID && $0.isVisible })?.grid else { return result }
        let group = result.filter { $0.id != id && $0.isVisible && target.peerIDs.contains($0.id) }
        let rects = group.compactMap(\.grid)
        guard !rects.isEmpty else { return result }
        let groupIDs = Set(group.map(\.id))
        let blockers = result.filter { $0.id != id && $0.isVisible && !groupIDs.contains($0.id) }.compactMap(\.grid)
        switch target.edge {
        case .top, .bottom:
            if target.edge == .top {
                guard primary.row > 0 else { return result }
                moving.height = min(moving.height, primary.row)
                moving.row = primary.row - moving.height
            } else { moving.row = primary.maxRow }
            var lower = rects.map(\.column).min() ?? primary.column
            var upper = rects.map(\.maxColumn).max() ?? primary.maxColumn
            for blocker in blockers where blocker.row < moving.maxRow && blocker.maxRow > moving.row {
                if blocker.maxColumn <= primary.column { lower = max(lower, blocker.maxColumn) }
                else if blocker.column >= primary.maxColumn { upper = min(upper, blocker.column) }
            }
            moving.column = lower; moving.width = max(1, upper - lower)
        case .left, .right:
            if target.edge == .left {
                guard primary.column > 0 else { return result }
                moving.width = min(moving.width, primary.column)
                moving.column = primary.column - moving.width
            } else {
                guard primary.maxColumn < columnCount else { return result }
                moving.column = primary.maxColumn
                moving.width = min(moving.width, columnCount - moving.column)
            }
            var lower = rects.map(\.row).min() ?? primary.row
            var upper = rects.map(\.maxRow).max() ?? primary.maxRow
            for blocker in blockers where blocker.column < moving.maxColumn && blocker.maxColumn > moving.column {
                if blocker.maxRow <= primary.row { lower = max(lower, blocker.maxRow) }
                else if blocker.row >= primary.maxRow { upper = min(upper, blocker.row) }
            }
            moving.row = lower; moving.height = max(1, upper - lower)
        }
        result[movingIndex].grid = clamped(moving)
        return resolveCollisions(result, anchors: groupIDs.union([id]))
    }

    static func sharedEdges(_ cards: [WorkspaceCard]) -> [WorkspaceGridSharedEdge] {
        let visible = normalized(cards).filter(\.isVisible)
        var result: [WorkspaceGridSharedEdge] = []
        for axis in [WorkspaceGridAxis.horizontal, .vertical] {
            let coordinates = Set(visible.flatMap { card -> [Int] in
                guard let rect = card.grid else { return [] }
                return axis == .horizontal ? [rect.row, rect.maxRow] : [rect.column, rect.maxColumn]
            }).sorted()
            for coordinate in coordinates {
                let before = visible.filter { card in card.grid.map { axis == .horizontal ? $0.maxRow == coordinate : $0.maxColumn == coordinate } == true }
                let after = visible.filter { card in card.grid.map { axis == .horizontal ? $0.row == coordinate : $0.column == coordinate } == true }
                let linked = before.contains { a in after.contains { b in
                    guard let lhs = a.grid, let rhs = b.grid else { return false }
                    return axis == .horizontal
                        ? lhs.column < rhs.maxColumn && rhs.column < lhs.maxColumn
                        : lhs.row < rhs.maxRow && rhs.row < lhs.maxRow
                } }
                guard linked else { continue }
                let all = (before + after).compactMap(\.grid)
                let start = all.map { axis == .horizontal ? $0.column : $0.row }.min() ?? 0
                let end = all.map { axis == .horizontal ? $0.maxColumn : $0.maxRow }.max() ?? 0
                result.append(WorkspaceGridSharedEdge(axis: axis, coordinate: coordinate, rangeStart: start, rangeEnd: end,
                                                      leadingIDs: before.map(\.id), trailingIDs: after.map(\.id)))
            }
        }
        return result
    }

    static func resizeSharedEdge(_ cards: [WorkspaceCard], edge: WorkspaceGridSharedEdge, to coordinate: Int) -> [WorkspaceCard] {
        var result = normalized(cards)
        let before = result.indices.filter { edge.leadingIDs.contains(result[$0].id) && result[$0].isVisible }
        let after = result.indices.filter { edge.trailingIDs.contains(result[$0].id) && result[$0].isVisible }
        guard !before.isEmpty, !after.isEmpty else { return result }
        let minBefore = before.compactMap { result[$0].grid }.map { edge.axis == .horizontal ? $0.height : $0.width }.min() ?? 1
        let minAfter = after.compactMap { result[$0].grid }.map { edge.axis == .horizontal ? $0.height : $0.width }.min() ?? 1
        let delta = min(max(coordinate - edge.coordinate, -(minBefore - 1)), minAfter - 1)
        for index in before {
            guard var rect = result[index].grid else { continue }
            if edge.axis == .horizontal { rect.height += delta } else { rect.width += delta }
            result[index].grid = rect
        }
        for index in after {
            guard var rect = result[index].grid else { continue }
            if edge.axis == .horizontal { rect.row += delta; rect.height -= delta }
            else { rect.column += delta; rect.width -= delta }
            result[index].grid = rect
        }
        return resolveCollisions(result, anchors: Set(edge.leadingIDs + edge.trailingIDs))
    }

    private static func boundary(_ rect: WorkspaceGridRect, _ edge: WorkspaceGridEdge) -> Int {
        switch edge {
        case .top: rect.row
        case .bottom: rect.maxRow
        case .left: rect.column
        case .right: rect.maxColumn
        }
    }

    private static func resolveCollisions(_ cards: [WorkspaceCard], anchors: Set<String>) -> [WorkspaceCard] {
        var result = cards
        let anchorIndices = result.indices.filter { anchors.contains(result[$0].id) && result[$0].isVisible }
        for _ in 0..<max(32, cards.count * cards.count + 1) {
            var changed = false
            for index in result.indices where result[index].isVisible && !anchors.contains(result[index].id) {
                for anchor in anchorIndices {
                    guard var rect = result[index].grid, let pinned = result[anchor].grid, overlaps(rect, pinned) else { continue }
                    rect.row = pinned.maxRow; result[index].grid = rect; changed = true
                }
            }
            let others = result.indices.filter { result[$0].isVisible && !anchors.contains(result[$0].id) }.sorted {
                let lhs = result[$0].grid!, rhs = result[$1].grid!
                if lhs.row != rhs.row { return lhs.row < rhs.row }
                if lhs.column != rhs.column { return lhs.column < rhs.column }
                return $0 < $1
            }
            for (position, upperIndex) in others.enumerated() {
                for lowerIndex in others.dropFirst(position + 1) {
                    guard let upper = result[upperIndex].grid, var lower = result[lowerIndex].grid, overlaps(upper, lower) else { continue }
                    lower.row = upper.maxRow; result[lowerIndex].grid = lower; changed = true
                }
            }
            if !changed { break }
        }
        return result
    }
}

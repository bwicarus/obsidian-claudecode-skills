import Foundation
#if canImport(CoreGraphics)
import CoreGraphics
#endif

struct WorkspaceGridRect: Codable, Equatable, Hashable {
    var column: Int
    var row: Int
    var width: Int
    var height: Int
    var maxColumn: Int { column + width }
    var maxRow: Int { row + height }
}

/// Precomputed from the canvas's pixel geometry, never from layout measurements.
struct WorkspaceGridConstraints {
    var minimumColumns: [String: Int] = [:]
    var minimumRows: [String: [Int: Int]] = [:]
    var columnPitch: CGFloat = 1
    var rowPitch: CGFloat = 1
    static let unrestricted = WorkspaceGridConstraints()

    func columns(for id: String) -> Int { min(12, max(1, minimumColumns[id] ?? 1)) }
    func rows(for id: String, width: Int) -> Int { max(1, minimumRows[id]?[width] ?? 1) }

    func accepts(_ rect: WorkspaceGridRect, id: String) -> Bool {
        rect.width >= columns(for: id) && rect.height >= rows(for: id, width: rect.width)
    }
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

struct WorkspaceGridJunction: Equatable, Identifiable {
    let vertical: WorkspaceGridSharedEdge
    let horizontal: WorkspaceGridSharedEdge
    var id: String { "junction:\(vertical.coordinate):\(horizontal.coordinate)" }
    var cardIDs: Set<String> {
        Set(vertical.leadingIDs + vertical.trailingIDs + horizontal.leadingIDs + horizontal.trailingIDs)
    }
}

/// Integer-grid geometry shared by the editor and the live native workspace.
/// Moving anchors resolve collisions first; the resulting cards then settle upward.
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

    private static func clamped(_ rect: WorkspaceGridRect, id: String,
                                constraints: WorkspaceGridConstraints) -> WorkspaceGridRect {
        var result = clamped(rect)
        result.width = max(result.width, constraints.columns(for: id))
        result.column = min(result.column, columnCount - result.width)
        result.height = max(result.height, constraints.rows(for: id, width: result.width))
        return result
    }

    static func overlaps(_ lhs: WorkspaceGridRect, _ rhs: WorkspaceGridRect) -> Bool {
        lhs.column < rhs.maxColumn && rhs.column < lhs.maxColumn
            && lhs.row < rhs.maxRow && rhs.row < lhs.maxRow
    }

    static func normalized(_ cards: [WorkspaceCard], constraints: WorkspaceGridConstraints = .unrestricted) -> [WorkspaceCard] {
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
        for index in result.indices where result[index].isVisible {
            if let rect = result[index].grid {
                result[index].grid = clamped(rect, id: result[index].id, constraints: constraints)
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

    static func move(_ cards: [WorkspaceCard], id: String, to rect: WorkspaceGridRect,
                     constraints: WorkspaceGridConstraints = .unrestricted) -> [WorkspaceCard] {
        var result = normalized(cards, constraints: constraints)
        guard let index = result.firstIndex(where: { $0.id == id }) else { return result }
        result[index].grid = clamped(rect, id: id, constraints: constraints)
        return compactVertically(resolveCollisions(result, anchors: [id]))
    }

    static func resize(_ cards: [WorkspaceCard], id: String, to rect: WorkspaceGridRect,
                       constraints: WorkspaceGridConstraints = .unrestricted) -> [WorkspaceCard] {
        move(cards, id: id, to: rect, constraints: constraints)
    }

    static func snapTarget(for rect: WorkspaceGridRect, in cards: [WorkspaceCard], excluding id: String,
                           columnTolerance: Double = 0.5, rowTolerance: Double = 0.75,
                           constraints: WorkspaceGridConstraints = .unrestricted) -> WorkspaceGridSnapTarget? {
        snapTarget(for: CGRect(x: CGFloat(rect.column), y: CGFloat(rect.row), width: CGFloat(rect.width), height: CGFloat(rect.height)),
                   in: cards, excluding: id, columnTolerance: columnTolerance, rowTolerance: rowTolerance, constraints: constraints)
    }

    static func snapTarget(for rect: CGRect, in cards: [WorkspaceCard], excluding id: String,
                           columnTolerance: Double = 0.5, rowTolerance: Double = 0.75,
                           constraints: WorkspaceGridConstraints = .unrestricted) -> WorkspaceGridSnapTarget? {
        guard rect.minX.isFinite, rect.minY.isFinite, rect.width.isFinite, rect.height.isFinite else { return nil }
        let peers = normalized(cards, constraints: constraints).filter { $0.id != id && $0.isVisible }
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

    static func dock(_ cards: [WorkspaceCard], id: String, target: WorkspaceGridSnapTarget,
                     constraints: WorkspaceGridConstraints = .unrestricted) -> [WorkspaceCard] {
        var result = normalized(cards, constraints: constraints)
        guard let movingIndex = result.firstIndex(where: { $0.id == id }), var moving = result[movingIndex].grid else { return result }
        guard let primaryID = target.primaryID else {
            switch target.edge {
            case .left: moving.column = 0
            case .right: moving.column = columnCount - moving.width
            case .top: moving.row = 0
            case .bottom: return result
            }
            return move(result, id: id, to: moving, constraints: constraints)
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
        // Reject an undersized docking gap instead of shrinking content to fit it.
        guard constraints.accepts(moving, id: id) else { return result }
        result[movingIndex].grid = clamped(moving)
        return compactVertically(resolveCollisions(result, anchors: groupIDs.union([id])))
    }

    /// Insert beside a card, making space for both readable contents. Unlike dock,
    /// this may divide the target's rectangle or push the occupied row downward.
    static func insert(_ cards: [WorkspaceCard], id: String, targetID: String, edge: WorkspaceGridEdge,
                       constraints: WorkspaceGridConstraints = .unrestricted) -> [WorkspaceCard] {
        guard id != targetID,
              cards.contains(where: { $0.id == id && $0.isVisible }),
              cards.contains(where: { $0.id == targetID && $0.isVisible }) else { return cards }
        let minimumMoving = constraints.columns(for: id)
        let minimumTarget = constraints.columns(for: targetID)
        if edge == .left || edge == .right {
            guard minimumMoving + minimumTarget <= columnCount else { return cards }
        }
        var result = normalized(cards, constraints: constraints)
        guard let movingIndex = result.firstIndex(where: { $0.id == id }),
              let targetIndex = result.firstIndex(where: { $0.id == targetID }),
              var moving = result[movingIndex].grid,
              var target = result[targetIndex].grid else { return cards }

        // A repeated preview or drop on the same shared edge must not divide again.
        let adjacent: Bool
        switch edge {
        case .top:
            adjacent = moving.column == target.column && moving.width == target.width && moving.maxRow == target.row
        case .bottom:
            adjacent = moving.column == target.column && moving.width == target.width && target.maxRow == moving.row
        case .left:
            adjacent = moving.row == target.row && moving.height == target.height && moving.maxColumn == target.column
        case .right:
            adjacent = moving.row == target.row && moving.height == target.height && target.maxColumn == moving.column
        }
        if adjacent && result == cards { return cards }

        switch edge {
        case .top, .bottom:
            let width = max(target.width, max(minimumMoving, minimumTarget))
            let column = min(target.column, columnCount - width)
            moving.column = column; moving.width = width
            target.column = column; target.width = width
            moving.height = max(moving.height, constraints.rows(for: id, width: width))
            target.height = max(target.height, constraints.rows(for: targetID, width: width))
            if edge == .top {
                moving.row = target.row
                target.row = moving.maxRow
            } else {
                moving.row = target.maxRow
            }
        case .left, .right:
            let minimumWidth = minimumMoving + minimumTarget
            let width = target.width >= minimumWidth ? target.width
                : min(columnCount, max(minimumWidth, target.width + moving.width))
            let movingWidth = min(max(width / 2, minimumMoving), width - minimumTarget)
            let targetWidth = width - movingWidth
            let column = edge == .left ? max(0, target.maxColumn - width)
                : min(target.column, columnCount - width)
            let height = max(max(moving.height, target.height),
                             max(constraints.rows(for: id, width: movingWidth),
                                 constraints.rows(for: targetID, width: targetWidth)))
            moving.width = movingWidth; target.width = targetWidth
            moving.height = height; target.height = height
            moving.row = target.row
            if edge == .left {
                moving.column = column; target.column = column + movingWidth
            } else {
                target.column = column; moving.column = column + targetWidth
            }
        }
        guard constraints.accepts(moving, id: id), constraints.accepts(target, id: targetID) else { return cards }
        result[movingIndex].grid = moving
        result[targetIndex].grid = target
        result = resolveCollisions(result, anchors: [id, targetID])

        // Both rectangles exactly tile this box. Settle them as one object so
        // asymmetric blockers above a left/right pair cannot break their seam.
        let group = WorkspaceGridRect(column: min(moving.column, target.column), row: min(moving.row, target.row),
                                      width: max(moving.maxColumn, target.maxColumn) - min(moving.column, target.column),
                                      height: max(moving.maxRow, target.maxRow) - min(moving.row, target.row))
        var grouped = result.filter { $0.id != targetID }
        guard let groupIndex = grouped.firstIndex(where: { $0.id == id }) else { return cards }
        grouped[groupIndex].grid = group
        let settled = compactVertically(grouped)
        guard let settledGroup = settled.first(where: { $0.id == id })?.grid else { return cards }
        let shift = settledGroup.row - group.row
        moving.row += shift; target.row += shift
        let positions = Dictionary(uniqueKeysWithValues: settled.compactMap { card in
            card.grid.map { (card.id, $0) }
        })
        for index in result.indices {
            if !cards[index].isVisible { result[index] = cards[index] }
            else if result[index].id == id { result[index].grid = moving }
            else if result[index].id == targetID { result[index].grid = target }
            else { result[index].grid = positions[result[index].id] }
        }
        return result
    }

    static func sharedEdges(_ cards: [WorkspaceCard], constraints: WorkspaceGridConstraints = .unrestricted) -> [WorkspaceGridSharedEdge] {
        let visible = normalized(cards, constraints: constraints).filter(\.isVisible)
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

    static func resizeSharedEdge(_ cards: [WorkspaceCard], edge: WorkspaceGridSharedEdge, to coordinate: Int,
                                 constraints: WorkspaceGridConstraints = .unrestricted) -> [WorkspaceCard] {
        var result = normalized(cards, constraints: constraints)
        let before = result.indices.filter { edge.leadingIDs.contains(result[$0].id) && result[$0].isVisible }
        let after = result.indices.filter { edge.trailingIDs.contains(result[$0].id) && result[$0].isVisible }
        guard !before.isEmpty, !after.isEmpty else { return result }
        let minBefore = before.compactMap { result[$0].grid }.map { edge.axis == .horizontal ? $0.height : $0.width }.min() ?? 1
        let minAfter = after.compactMap { result[$0].grid }.map { edge.axis == .horizontal ? $0.height : $0.width }.min() ?? 1
        let lower = edge.coordinate - (minBefore - 1), upper = edge.coordinate + minAfter - 1
        let requested = min(max(coordinate, lower), upper)
        let delta: Int
        if edge.axis == .horizontal {
            let minimum = before.compactMap { index -> Int? in
                guard let rect = result[index].grid else { return nil }
                return edge.coordinate + constraints.rows(for: result[index].id, width: rect.width) - rect.height
            }.max() ?? lower
            let maximum = after.compactMap { index -> Int? in
                guard let rect = result[index].grid else { return nil }
                return edge.coordinate + rect.height - constraints.rows(for: result[index].id, width: rect.width)
            }.min() ?? upper
            guard minimum <= maximum else { return result }
            delta = min(max(requested, minimum), maximum) - edge.coordinate
        } else {
            let legal = (lower...upper).filter { candidate in
                let shift = candidate - edge.coordinate
                return (before + after).allSatisfy { index in
                    guard var rect = result[index].grid else { return false }
                    rect.width += before.contains(index) ? shift : -shift
                    return constraints.accepts(rect, id: result[index].id)
                }
            }
            guard let selected = legal.min(by: {
                let lhs = abs($0 - requested), rhs = abs($1 - requested)
                return lhs == rhs ? abs($0 - edge.coordinate) < abs($1 - edge.coordinate) : lhs < rhs
            }) else { return result }
            delta = selected - edge.coordinate
        }
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
        return compactVertically(resolveCollisions(result, anchors: Set(edge.leadingIDs + edge.trailingIDs)))
    }

    static func sharedJunctions(_ cards: [WorkspaceCard], constraints: WorkspaceGridConstraints = .unrestricted) -> [WorkspaceGridJunction] {
        let positioned = normalized(cards, constraints: constraints)
        let edges = sharedEdges(positioned, constraints: constraints)
        let verticals = edges.filter { $0.axis == .vertical }
        let horizontals = edges.filter { $0.axis == .horizontal }
        var junctions: [WorkspaceGridJunction] = []
        for vertical in verticals {
            for horizontal in horizontals {
                guard let localVertical = connectedEdge(vertical, through: horizontal.coordinate, cards: positioned),
                      let localHorizontal = connectedEdge(horizontal, through: vertical.coordinate, cards: positioned) else { continue }
                let verticalIDs = Set(localVertical.leadingIDs + localVertical.trailingIDs)
                let horizontalIDs = Set(localHorizontal.leadingIDs + localHorizontal.trailingIDs)
                guard !verticalIDs.isDisjoint(with: horizontalIDs) else { continue }
                junctions.append(WorkspaceGridJunction(vertical: localVertical, horizontal: localHorizontal))
            }
        }
        return junctions
    }

    static func resizeSharedJunction(_ cards: [WorkspaceCard], junction: WorkspaceGridJunction,
                                     column: Int, row: Int,
                                     constraints: WorkspaceGridConstraints = .unrestricted) -> [WorkspaceCard] {
        var result = normalized(cards, constraints: constraints)
        guard let current = sharedJunctions(result, constraints: constraints).first(where: { $0.id == junction.id }) else { return result }
        let vertical = current.vertical, horizontal = current.horizontal
        let left = Set(vertical.leadingIDs), right = Set(vertical.trailingIDs)
        let above = Set(horizontal.leadingIDs), below = Set(horizontal.trailingIDs)
        func minimumSpan(_ ids: Set<String>, axis: WorkspaceGridAxis) -> Int {
            result.filter { ids.contains($0.id) && $0.isVisible }.compactMap(\.grid)
                .map { axis == .vertical ? $0.width : $0.height }.min() ?? 1
        }
        // Both limits come from the original layout. A diagonal drag must not
        // reflow one axis before calculating the other axis's participants.
        let minimumColumn = vertical.coordinate - (minimumSpan(left, axis: .vertical) - 1)
        let maximumColumn = vertical.coordinate + (minimumSpan(right, axis: .vertical) - 1)
        let minimumRow = horizontal.coordinate - (minimumSpan(above, axis: .horizontal) - 1)
        let maximumRow = horizontal.coordinate + (minimumSpan(below, axis: .horizontal) - 1)
        let targetColumn = min(max(column, minimumColumn), maximumColumn)
        let targetRow = min(max(row, minimumRow), maximumRow)
        var best: (column: Int, row: Int, distance: Double, movement: Int)?
        for candidateColumn in minimumColumn...maximumColumn {
            let shift = candidateColumn - vertical.coordinate
            var lower = minimumRow, upper = maximumRow
            var valid = true
            for card in result where card.isVisible && current.cardIDs.contains(card.id) {
                guard var rect = card.grid else { valid = false; break }
                if left.contains(card.id) { rect.width += shift }
                if right.contains(card.id) { rect.width -= shift }
                guard rect.width >= constraints.columns(for: card.id) else { valid = false; break }
                let height = constraints.rows(for: card.id, width: rect.width)
                if above.contains(card.id) { lower = max(lower, horizontal.coordinate + height - rect.height) }
                else if below.contains(card.id) { upper = min(upper, horizontal.coordinate + rect.height - height) }
                else if rect.height < height { valid = false; break }
            }
            guard valid, lower <= upper else { continue }
            let candidateRow = min(max(targetRow, lower), upper)
            let xDistance = Double(candidateColumn - targetColumn) * Double(constraints.columnPitch)
            let yDistance = Double(candidateRow - targetRow) * Double(constraints.rowPitch)
            let distance = xDistance * xDistance + yDistance * yDistance
            let movement = abs(shift) + abs(candidateRow - horizontal.coordinate)
            if best == nil || distance < best!.distance || (distance == best!.distance && movement < best!.movement) {
                best = (candidateColumn, candidateRow, distance, movement)
            }
        }
        guard let best else { return result }
        let dx = best.column - vertical.coordinate, dy = best.row - horizontal.coordinate
        for index in result.indices where result[index].isVisible {
            guard var rect = result[index].grid else { continue }
            let id = result[index].id
            if left.contains(id) { rect.width += dx }
            if right.contains(id) { rect.column += dx; rect.width -= dx }
            if above.contains(id) { rect.height += dy }
            if below.contains(id) { rect.row += dy; rect.height -= dy }
            result[index].grid = rect
        }
        return compactVertically(resolveCollisions(result, anchors: current.cardIDs))
    }

    private struct SharedSegment {
        let leadingID: String
        let trailingID: String
        let start: Int
        let end: Int
    }

    /// sharedEdges intentionally merges collinear groups. For an intersection,
    /// require real contact here, then follow only connected parts of that seam.
    private static func connectedEdge(_ edge: WorkspaceGridSharedEdge, through position: Int,
                                      cards: [WorkspaceCard]) -> WorkspaceGridSharedEdge? {
        let before = cards.filter { $0.isVisible && edge.leadingIDs.contains($0.id) }
        let after = cards.filter { $0.isVisible && edge.trailingIDs.contains($0.id) }
        var segments: [SharedSegment] = []
        for leading in before {
            for trailing in after {
                guard let lhs = leading.grid, let rhs = trailing.grid else { continue }
                let start = edge.axis == .vertical ? max(lhs.row, rhs.row) : max(lhs.column, rhs.column)
                let end = edge.axis == .vertical ? min(lhs.maxRow, rhs.maxRow) : min(lhs.maxColumn, rhs.maxColumn)
                if start < end {
                    segments.append(SharedSegment(leadingID: leading.id, trailingID: trailing.id, start: start, end: end))
                }
            }
        }
        var included = Set(segments.indices.filter { segments[$0].start <= position && position <= segments[$0].end })
        guard !included.isEmpty else { return nil }
        var changed = true
        while changed {
            changed = false
            let selected = included.map { segments[$0] }
            let ids = Set(selected.flatMap { [$0.leadingID, $0.trailingID] })
            for index in segments.indices where !included.contains(index) {
                let segment = segments[index]
                let touches = selected.contains { segment.start <= $0.end && $0.start <= segment.end }
                if touches || ids.contains(segment.leadingID) || ids.contains(segment.trailingID) {
                    included.insert(index)
                    changed = true
                }
            }
        }
        let selected = included.map { segments[$0] }
        let leadingIDs = Set(selected.map(\.leadingID)), trailingIDs = Set(selected.map(\.trailingID))
        let members = (before + after).filter { leadingIDs.contains($0.id) || trailingIDs.contains($0.id) }.compactMap(\.grid)
        let start = members.map { edge.axis == .vertical ? $0.row : $0.column }.min() ?? position
        let end = members.map { edge.axis == .vertical ? $0.maxRow : $0.maxColumn }.max() ?? position
        return WorkspaceGridSharedEdge(axis: edge.axis, coordinate: edge.coordinate, rangeStart: start, rangeEnd: end,
                                       leadingIDs: edge.leadingIDs.filter { leadingIDs.contains($0) },
                                       trailingIDs: edge.trailingIDs.filter { trailingIDs.contains($0) })
    }

    private static func boundary(_ rect: WorkspaceGridRect, _ edge: WorkspaceGridEdge) -> Int {
        switch edge {
        case .top: rect.row
        case .bottom: rect.maxRow
        case .left: rect.column
        case .right: rect.maxColumn
        }
    }

    private static func compactVertically(_ cards: [WorkspaceCard]) -> [WorkspaceCard] {
        var result = cards
        let visible = result.indices.filter { result[$0].isVisible && result[$0].grid != nil }.sorted {
            let lhs = result[$0].grid!, rhs = result[$1].grid!
            if lhs.row != rhs.row { return lhs.row < rhs.row }
            if lhs.column != rhs.column { return lhs.column < rhs.column }
            return $0 < $1
        }
        var columnBottoms = Array(repeating: 0, count: columnCount)
        for index in visible {
            guard var rect = result[index].grid else { continue }
            // Preserve horizontal placement and the vertical order in each column.
            // A spanning card stops at the tallest blocker instead of jumping past it.
            let columns = rect.column..<rect.maxColumn
            rect.row = columns.map { columnBottoms[$0] }.max() ?? 0
            result[index].grid = rect
            for column in columns { columnBottoms[column] = rect.maxRow }
        }
        return result
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

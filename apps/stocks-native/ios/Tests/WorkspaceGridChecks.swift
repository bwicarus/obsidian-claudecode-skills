import Foundation

/// Command-line contracts for the native layout value model; no UI or Store I/O.
/// swiftc StockWorkspaceLayout.swift WorkspaceGridEngine.swift WorkspaceGridChecks.swift -o grid-checks
@main
struct WorkspaceGridChecks {
    struct CheckFailure: Error, CustomStringConvertible {
        let description: String
    }

    static func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        let passed = try condition()
        if !passed { throw CheckFailure(description: message) }
    }

    static func card(_ kind: WorkspaceCardKind, _ column: Int, _ row: Int,
                     _ width: Int, _ height: Int, visible: Bool = true) -> WorkspaceCard {
        WorkspaceCard(kind: kind, isVisible: visible,
                      grid: WorkspaceGridRect(column: column, row: row, width: width, height: height))
    }

    static func rect(_ kind: WorkspaceCardKind, in cards: [WorkspaceCard]) throws -> WorkspaceGridRect {
        guard let value = cards.first(where: { $0.kind == kind })?.grid else {
            throw CheckFailure(description: "Missing positioned card: \(kind.rawValue)")
        }
        return value
    }

    static func requireValid(_ cards: [WorkspaceCard]) throws {
        let visible = cards.filter(\.isVisible)
        for card in visible {
            guard let box = card.grid else { throw CheckFailure(description: "Missing grid: \(card.id)") }
            try require(box.column >= 0 && box.row >= 0 && box.width >= 1 && box.height >= 1,
                        "Grid coordinates must remain nonnegative and sizes positive")
            try require(box.column + box.width <= 12, "A card crossed the twelve-column canvas")
        }
        for first in visible.indices {
            for second in visible.indices where second > first {
                let a = try rect(visible[first].kind, in: visible)
                let b = try rect(visible[second].kind, in: visible)
                let separated = a.column + a.width <= b.column || b.column + b.width <= a.column
                    || a.row + a.height <= b.row || b.row + b.height <= a.row
                try require(separated, "Visible cards overlap: \(visible[first].id), \(visible[second].id)")
            }
        }
    }

    static func legacyLayout() throws -> WorkspaceLayout {
        let data = Data(#"""
        {
          "schemaVersion": 1,
          "selectedPageID": "research-custom",
          "pages": [
            {"id": "market-custom", "title": "我的行情", "cards": [
              {"kind": "quote", "span": "half", "isVisible": true},
              {"kind": "orderBook", "span": "half", "isVisible": false},
              {"kind": "kline", "span": "full", "isVisible": true}
            ]},
            {"id": "research-custom", "title": "研究笔记", "cards": [
              {"kind": "fund", "span": "half", "isVisible": true},
              {"kind": "announcements", "span": "full", "isVisible": true}
            ]}
          ]
        }
        """#.utf8)
        return try JSONDecoder().decode(WorkspaceLayout.self, from: data).sanitized()
    }

    static func migrationPreservesUserChoices() throws {
        let layout = try legacyLayout()
        try require(layout.schemaVersion == 2, "The old layout did not migrate to schema 2")
        try require(layout.pages.map(\.id) == ["market-custom", "research-custom"], "Migration changed page identity or order")
        try require(layout.pages.map(\.title) == ["我的行情", "研究笔记"], "Migration lost custom page names")
        try require(layout.selectedPageID == "research-custom", "Migration changed the selected page")
        try require(layout.pages[0].cards.map(\.kind) == [.quote, .orderBook, .kline], "Migration lost or reordered cards")
        try require(layout.pages[0].cards[1].isVisible == false, "Migration unhid a hidden card")
        try require(try rect(.kline, in: layout.pages[0].cards).width == 12, "Migration lost a full-width card")
        for page in layout.pages { try requireValid(page.cards) }
    }

    static func repeatedSavePreservesCoordinates() throws {
        var layout = try legacyLayout()
        layout.pages[1].cards = [card(.fund, 0, 0, 5, 4), card(.announcements, 5, 0, 7, 8)]
        layout = layout.sanitized()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let first = try encoder.encode(layout)
        var restored = try JSONDecoder().decode(WorkspaceLayout.self, from: first).sanitized()
        for _ in 0..<3 {
            restored = try JSONDecoder().decode(WorkspaceLayout.self, from: encoder.encode(restored)).sanitized()
        }
        try require(restored == layout, "Repeated saves drifted positions, visibility, or page selection")
        try require(try encoder.encode(restored) == first, "Repeated saves changed the stored layout")
    }

    static func freeMoveKeepsAnchorAndPushesNeighbors() throws {
        let original = [card(.quote, 0, 0, 6, 4), card(.orderBook, 6, 0, 6, 4), card(.fund, 0, 4, 6, 3)]
        let destination = WorkspaceGridRect(column: 6, row: 0, width: 6, height: 4)
        let moved = WorkspaceGridEngine.move(original, id: "quote", to: destination)
        try require(try rect(.quote, in: moved) == destination, "The moved card did not remain at its chosen position")
        try require(try rect(.orderBook, in: moved).row >= 4, "The occupied landing area did not push its neighbor down")
        try require(try rect(.fund, in: moved).row == 0, "Moving a card away left its old column empty above the next card")
        try require(moved.count == original.count, "Free movement lost a card")
        try requireValid(moved)
    }

    static func shrinkingCardClosesVerticalGaps() throws {
        let original = [card(.quote, 0, 0, 6, 8), card(.orderBook, 6, 0, 6, 5),
                        card(.fund, 0, 8, 6, 3), card(.macd, 0, 11, 12, 4)]
        let resized = WorkspaceGridEngine.resize(original, id: "quote",
            to: WorkspaceGridRect(column: 0, row: 0, width: 6, height: 3))
        try require(try rect(.fund, in: resized).row == 3, "The lower card did not fill the space freed by shrinking")
        try require(try rect(.macd, in: resized).row == 6, "A spanning card did not settle against its tallest blocker")
        try require(try rect(.orderBook, in: resized) == original[1].grid, "Compaction changed a neighboring column's size")
        try requireValid(resized)
    }

    static func compactionPreservesBlockersAndHiddenHomes() throws {
        let original = [card(.quote, 0, 0, 6, 8), card(.orderBook, 6, 0, 6, 2),
                        card(.fund, 0, 12, 12, 2), card(.macd, 6, 16, 6, 2),
                        card(.kdj, 6, 3, 6, 9, visible: false)]
        let settled = WorkspaceGridEngine.move(original, id: "quote", to: try rect(.quote, in: original))
        try require(try rect(.fund, in: settled).row == 8, "The spanning card did not stop at the taller column")
        try require(try rect(.macd, in: settled).row == 10,
                    "Compaction jumped a card above its spanning blocker into a disconnected hole")
        try require(try rect(.kdj, in: settled) == original[4].grid, "Compaction moved a hidden card's saved home")
        try require(settled.map(\.id) == original.map(\.id), "Compaction reordered the stored card list")
        for card in settled where card.isVisible {
            let before = try rect(card.kind, in: original), after = try rect(card.kind, in: settled)
            try require(after.column == before.column && after.width == before.width && after.height == before.height,
                        "Compaction changed a card's horizontal position or size")
            try require(after.row <= before.row, "Compaction moved a card downward")
        }
        let repeated = WorkspaceGridEngine.move(settled, id: "quote", to: try rect(.quote, in: settled))
        try require(repeated == settled, "Repeated compaction drifted the layout")
        try requireValid(settled)
    }

    static func floatingDropSettlesBeforeSaving() throws {
        let original = [card(.quote, 0, 0, 6, 4), card(.orderBook, 6, 0, 6, 4), card(.fund, 0, 4, 6, 3)]
        let moved = WorkspaceGridEngine.move(original, id: "fund",
            to: WorkspaceGridRect(column: 6, row: 30, width: 6, height: 3))
        try require(try rect(.fund, in: moved) == WorkspaceGridRect(column: 6, row: 4, width: 6, height: 3),
                    "Dropping into empty lower space left a floating card")
        try require(WorkspaceGridEngine.normalized(moved) == moved, "Saving changed the preview's settled positions")
        try requireValid(moved)
    }

    static func facingEdgeSnapDocksToWholeGroup() throws {
        let original = [card(.quote, 0, 0, 3, 4), card(.orderBook, 3, 0, 3, 4), card(.fund, 0, 9, 3, 3)]
        let ghost = WorkspaceGridRect(column: 0, row: 4, width: 3, height: 3)
        guard let target = WorkspaceGridEngine.snapTarget(for: ghost, in: original, excluding: "fund") else {
            throw CheckFailure(description: "Facing edges that touch did not produce a snap target")
        }
        try require(target.edge == .bottom && target.coordinate == 4, "Snap chose the wrong facing edge")
        try require(Set(target.peerIDs) == Set(["quote", "orderBook"]), "Snap omitted a collinear peer")
        let docked = WorkspaceGridEngine.dock(original, id: "fund", target: target)
        let box = try rect(.fund, in: docked)
        try require(box.column == 0 && box.width == 6 && box.row == 4, "Docking did not fill the shared edge")
        try require(try rect(.quote, in: docked) == original[0].grid, "Docking moved its anchor")
        try requireValid(docked)
    }

    static func parallelEdgesDoNotPretendToDock() throws {
        let original = [card(.quote, 3, 3, 4, 4), card(.fund, 3, 9, 4, 2)]
        let overlappingGhost = WorkspaceGridRect(column: 3, row: 4, width: 4, height: 2)
        let target = WorkspaceGridEngine.snapTarget(for: overlappingGhost, in: original, excluding: "fund")
        try require(target == nil, "Matching left edges were mistaken for adjacent facing edges")
    }

    static func verticalSharedEdgeResizesAllPeers() throws {
        let original = [card(.quote, 0, 0, 6, 4), card(.macd, 0, 4, 6, 4), card(.orderBook, 6, 0, 6, 8)]
        guard let edge = WorkspaceGridEngine.sharedEdges(original).first(where: { $0.axis == .vertical && $0.coordinate == 6 }) else {
            throw CheckFailure(description: "Shared vertical edge was not exposed")
        }
        let resized = WorkspaceGridEngine.resizeSharedEdge(original, edge: edge, to: 8)
        try require(try rect(.quote, in: resized).width == 8, "Upper left peer did not resize with the divider")
        try require(try rect(.macd, in: resized).width == 8, "Lower left peer did not resize with the divider")
        let right = try rect(.orderBook, in: resized)
        try require(right.column == 8 && right.column + right.width == 12, "The shared divider changed the outer right edge")
        try require(resized.compactMap(\.grid).map { $0.row + $0.height }.max() == 8, "Vertical resize changed the outer bottom edge")
        try requireValid(resized)
    }

    static func horizontalSharedEdgePreservesOuterBounds() throws {
        let original = [card(.quote, 0, 0, 6, 4), card(.orderBook, 6, 0, 6, 4), card(.fund, 0, 4, 12, 4)]
        guard let edge = WorkspaceGridEngine.sharedEdges(original).first(where: { $0.axis == .horizontal && $0.coordinate == 4 }) else {
            throw CheckFailure(description: "Shared horizontal edge was not exposed")
        }
        let resized = WorkspaceGridEngine.resizeSharedEdge(original, edge: edge, to: 6)
        try require(try rect(.quote, in: resized).height == 6, "First upper peer did not resize")
        try require(try rect(.orderBook, in: resized).height == 6, "Second upper peer did not resize")
        let bottom = try rect(.fund, in: resized)
        try require(bottom.row == 6 && bottom.row + bottom.height == 8, "Shared resize changed the outer bottom edge")
        try require(bottom.column == 0 && bottom.width == 12, "Horizontal resize changed the outer sides")
        try requireValid(resized)
    }

    static func invalidSizesAndCanvasEscapesAreClamped() throws {
        let original = [card(.quote, 0, 0, 6, 4), card(.orderBook, 6, 0, 6, 4)]
        let shrunken = WorkspaceGridEngine.resize(original, id: "quote",
            to: WorkspaceGridRect(column: 0, row: 0, width: 0, height: -4))
        try requireValid(shrunken)
        let escaped = WorkspaceGridEngine.move(original, id: "quote",
            to: WorkspaceGridRect(column: -4, row: -9, width: 99, height: -3))
        try requireValid(escaped)
        guard let edge = WorkspaceGridEngine.sharedEdges(original).first(where: { $0.axis == .vertical }) else {
            throw CheckFailure(description: "Expected a divider for clamp validation")
        }
        for coordinate in [-99, 999] {
            let clamped = WorkspaceGridEngine.resizeSharedEdge(original, edge: edge, to: coordinate)
            try requireValid(clamped)
            let left = try rect(.quote, in: clamped), right = try rect(.orderBook, in: clamped)
            try require(left.column == 0 && right.column + right.width == 12, "Clamping moved the outer canvas edges")
        }
    }

    static func fourCardJunctionMovesBothAxes() throws {
        let original = [card(.quote, 0, 0, 6, 4), card(.orderBook, 6, 0, 6, 4),
                        card(.fund, 0, 4, 6, 4), card(.macd, 6, 4, 6, 4),
                        card(.kdj, 0, 20, 6, 3), card(.peers, 6, 20, 6, 3)]
        guard let junction = WorkspaceGridEngine.sharedJunctions(original).first(where: {
            $0.vertical.coordinate == 6 && $0.horizontal.coordinate == 4
        }) else { throw CheckFailure(description: "Four touching cards did not expose their junction") }
        try require(junction.cardIDs == Set(["quote", "orderBook", "fund", "macd"]),
                    "A disconnected collinear group joined the local junction")
        let resized = WorkspaceGridEngine.resizeSharedJunction(original, junction: junction, column: 8, row: 6)
        try require(try rect(.quote, in: resized) == WorkspaceGridRect(column: 0, row: 0, width: 8, height: 6),
                    "Diagonal movement did not grow both top-left dimensions")
        try require(try rect(.orderBook, in: resized) == WorkspaceGridRect(column: 8, row: 0, width: 4, height: 6),
                    "The top-right card did not follow both junction axes")
        try require(try rect(.fund, in: resized) == WorkspaceGridRect(column: 0, row: 6, width: 8, height: 2),
                    "The bottom-left card did not follow both junction axes")
        try require(try rect(.macd, in: resized) == WorkspaceGridRect(column: 8, row: 6, width: 4, height: 2),
                    "The bottom-right outer boundaries changed")
        try require(try rect(.kdj, in: resized) == WorkspaceGridRect(column: 0, row: 8, width: 6, height: 3),
                    "A remote left card did not settle without resizing with the junction")
        try require(try rect(.peers, in: resized) == WorkspaceGridRect(column: 6, row: 8, width: 6, height: 3),
                    "A remote right card did not settle without resizing with the junction")
        try requireValid(resized)
    }

    static func junctionLimitsBothAxesIndependently() throws {
        let original = [card(.quote, 0, 0, 6, 4), card(.orderBook, 6, 0, 6, 4),
                        card(.fund, 0, 4, 6, 4), card(.macd, 6, 4, 6, 4)]
        guard let junction = WorkspaceGridEngine.sharedJunctions(original).first else {
            throw CheckFailure(description: "Expected a junction for minimum-size checks")
        }
        for column in [Int.min, Int.max] {
            for row in [Int.min, Int.max] {
                let resized = WorkspaceGridEngine.resizeSharedJunction(original, junction: junction, column: column, row: row)
                let expectedColumn = column < 0 ? 1 : 11
                let expectedRow = row < 0 ? 1 : 7
                let topLeft = try rect(.quote, in: resized), bottomRight = try rect(.macd, in: resized)
                try require(topLeft.column == 0 && topLeft.row == 0
                            && topLeft.maxColumn == expectedColumn && topLeft.maxRow == expectedRow,
                            "One axis's limit changed the other axis or the outer top-left corner")
                try require(bottomRight.column == expectedColumn && bottomRight.row == expectedRow
                            && bottomRight.maxColumn == 12 && bottomRight.maxRow == 8,
                            "Clamping a junction moved the outer right/bottom boundaries")
                try requireValid(resized)
            }
        }
    }

    static func tJunctionResizesOnlyParticipatingDimensions() throws {
        let original = [card(.quote, 0, 0, 6, 8), card(.orderBook, 6, 0, 6, 4), card(.fund, 6, 4, 6, 4)]
        guard let junction = WorkspaceGridEngine.sharedJunctions(original).first else {
            throw CheckFailure(description: "A real T-shaped seam did not expose its junction")
        }
        let resized = WorkspaceGridEngine.resizeSharedJunction(original, junction: junction, column: 8, row: 5)
        try require(try rect(.quote, in: resized) == WorkspaceGridRect(column: 0, row: 0, width: 8, height: 8),
                    "Moving a T junction changed the unsplit card's height")
        try require(try rect(.orderBook, in: resized) == WorkspaceGridRect(column: 8, row: 0, width: 4, height: 5),
                    "Moving a T junction did not resize the upper neighbor")
        try require(try rect(.fund, in: resized) == WorkspaceGridRect(column: 8, row: 5, width: 4, height: 3),
                    "Moving a T junction did not preserve the lower neighbor's outer bounds")
        try requireValid(resized)
    }

    static func disconnectedSeamsDoNotCreateJunctions() throws {
        let original = [card(.quote, 4, 0, 2, 2), card(.orderBook, 6, 0, 2, 2),
                        card(.fund, 4, 8, 2, 2), card(.macd, 6, 8, 2, 2),
                        card(.kdj, 0, 2, 2, 3), card(.signals, 0, 5, 2, 3),
                        card(.peers, 10, 2, 2, 3), card(.valuation, 10, 5, 2, 3)]
        let edges = WorkspaceGridEngine.sharedEdges(original)
        try require(edges.contains { $0.axis == .vertical && $0.coordinate == 6 }
                    && edges.contains { $0.axis == .horizontal && $0.coordinate == 5 },
                    "Fixture must contain merged vertical and horizontal boundaries")
        try require(WorkspaceGridEngine.sharedJunctions(original).isEmpty,
                    "Separated seams produced a junction in their empty bounding-box intersection")
    }

    static func minimumContentSizesApplyToSavedCardsAndResize() throws {
        let constraints = WorkspaceGridConstraints(minimumColumns: ["quote": 4],
            minimumRows: ["quote": [4: 8, 6: 4]])
        let original = [card(.quote, 0, 0, 2, 1), card(.orderBook, 6, 0, 6, 4),
                        card(.fund, 0, 9, 2, 1, visible: false)]
        let loaded = WorkspaceGridEngine.normalized(original, constraints: constraints)
        try require(try rect(.quote, in: loaded) == WorkspaceGridRect(column: 0, row: 0, width: 4, height: 8),
                    "A saved undersized card was not expanded to its readable content size")
        try require(try rect(.fund, in: loaded) == original[2].grid, "Minimum content sizes changed a hidden card's home")
        let resized = WorkspaceGridEngine.resize(loaded, id: "quote",
            to: WorkspaceGridRect(column: 0, row: 0, width: 6, height: 1), constraints: constraints)
        try require(try rect(.quote, in: resized).height == 4, "Wider content did not use its new height minimum")
        try requireValid(resized)
    }

    static func sharedDividerRespectsWidthDependentHeight() throws {
        let quoteRows = Dictionary(uniqueKeysWithValues: (1...12).map { ($0, $0 < 6 ? 8 : 4) })
        let constraints = WorkspaceGridConstraints(minimumColumns: ["quote": 4, "orderBook": 3],
            minimumRows: ["quote": quoteRows])
        let original = [card(.quote, 0, 0, 6, 6), card(.orderBook, 6, 0, 6, 6)]
        guard let edge = WorkspaceGridEngine.sharedEdges(original, constraints: constraints).first(where: { $0.axis == .vertical }) else {
            throw CheckFailure(description: "Missing divider for minimum-content checks")
        }
        let narrowed = WorkspaceGridEngine.resizeSharedEdge(original, edge: edge, to: 4, constraints: constraints)
        try require(try rect(.quote, in: narrowed).width == 6,
                    "A divider made the card too narrow for its fixed shared-edge height")
        let widened = WorkspaceGridEngine.resizeSharedEdge(original, edge: edge, to: 11, constraints: constraints)
        try require(try rect(.orderBook, in: widened).width == 3, "A divider bypassed the other card's minimum width")
        try requireValid(narrowed)
        try requireValid(widened)
    }

    static func junctionCouplesWidthAndHeightMinimums() throws {
        let quoteRows = Dictionary(uniqueKeysWithValues: (1...12).map { ($0, $0 < 6 ? 8 : 4) })
        let otherRows = Dictionary(uniqueKeysWithValues: (1...12).map { ($0, 4) })
        let constraints = WorkspaceGridConstraints(
            minimumColumns: ["quote": 4, "orderBook": 4, "fund": 4, "macd": 4],
            minimumRows: ["quote": quoteRows, "orderBook": otherRows, "fund": otherRows, "macd": otherRows],
            columnPitch: 60, rowPitch: 36)
        let original = [card(.quote, 0, 0, 6, 6), card(.orderBook, 6, 0, 6, 6),
                        card(.fund, 0, 6, 6, 6), card(.macd, 6, 6, 6, 6)]
        guard let junction = WorkspaceGridEngine.sharedJunctions(original, constraints: constraints).first else {
            throw CheckFailure(description: "Missing junction for coupled content checks")
        }
        let resized = WorkspaceGridEngine.resizeSharedJunction(original, junction: junction, column: 4, row: 6,
                                                                constraints: constraints)
        try require(try rect(.quote, in: resized) == WorkspaceGridRect(column: 0, row: 0, width: 4, height: 8),
                    "The junction failed to allocate the extra height required by a narrower card")
        let fund = try rect(.fund, in: resized), macd = try rect(.macd, in: resized)
        try require(fund.maxRow == 12 && macd.maxColumn == 12,
                    "Coupled constraints moved the outer bounds")
        for card in resized {
            try require(constraints.accepts(try rect(card.kind, in: resized), id: card.id),
                        "A junction left content below its readable minimum")
        }
        try requireValid(resized)
    }

    static func dockingRejectsUnreadableGaps() throws {
        let original = [card(.quote, 3, 0, 9, 4), card(.fund, 0, 4, 6, 4)]
        let constraints = WorkspaceGridConstraints(minimumColumns: ["fund": 4])
        let target = WorkspaceGridSnapTarget(edge: .left, coordinate: 3, peerIDs: ["quote"], primaryID: "quote")
        let docked = WorkspaceGridEngine.dock(original, id: "fund", target: target, constraints: constraints)
        try require(docked == original, "Docking shrank a card into a gap below its minimum width")
        try requireValid(docked)
    }

    static func main() throws {
        let checks: [(String, () throws -> Void)] = [
            ("schema 1 migration", migrationPreservesUserChoices),
            ("repeat save stability", repeatedSavePreservesCoordinates),
            ("free move and collision", freeMoveKeepsAnchorAndPushesNeighbors),
            ("resize fills vertical gaps", shrinkingCardClosesVerticalGaps),
            ("compaction blockers and hidden homes", compactionPreservesBlockersAndHiddenHomes),
            ("floating drop settles before save", floatingDropSettlesBeforeSaving),
            ("facing edge group dock", facingEdgeSnapDocksToWholeGroup),
            ("parallel edge rejection", parallelEdgesDoNotPretendToDock),
            ("vertical shared edge", verticalSharedEdgeResizesAllPeers),
            ("horizontal shared edge", horizontalSharedEdgePreservesOuterBounds),
            ("minimum sizes and bounds", invalidSizesAndCanvasEscapesAreClamped),
            ("four-card diagonal junction", fourCardJunctionMovesBothAxes),
            ("junction independent limits", junctionLimitsBothAxesIndependently),
            ("T-shaped junction", tJunctionResizesOnlyParticipatingDimensions),
            ("disconnected seam rejection", disconnectedSeamsDoNotCreateJunctions),
            ("saved and resized content minimums", minimumContentSizesApplyToSavedCardsAndResize),
            ("divider content minimums", sharedDividerRespectsWidthDependentHeight),
            ("junction coupled content minimums", junctionCouplesWidthAndHeightMinimums),
            ("readable dock minimum", dockingRejectsUnreadableGaps)
        ]
        for (name, check) in checks {
            try check()
            print("PASS: \(name)")
        }
        print("WorkspaceGridChecks: \(checks.count) contracts passed")
    }
}

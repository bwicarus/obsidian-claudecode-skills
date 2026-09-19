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
        try require(moved.count == original.count, "Free movement lost a card")
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

    static func main() throws {
        let checks: [(String, () throws -> Void)] = [
            ("schema 1 migration", migrationPreservesUserChoices),
            ("repeat save stability", repeatedSavePreservesCoordinates),
            ("free move and collision", freeMoveKeepsAnchorAndPushesNeighbors),
            ("facing edge group dock", facingEdgeSnapDocksToWholeGroup),
            ("parallel edge rejection", parallelEdgesDoNotPretendToDock),
            ("vertical shared edge", verticalSharedEdgeResizesAllPeers),
            ("horizontal shared edge", horizontalSharedEdgePreservesOuterBounds),
            ("minimum sizes and bounds", invalidSizesAndCanvasEscapesAreClamped)
        ]
        for (name, check) in checks {
            try check()
            print("PASS: \(name)")
        }
        print("WorkspaceGridChecks: \(checks.count) contracts passed")
    }
}

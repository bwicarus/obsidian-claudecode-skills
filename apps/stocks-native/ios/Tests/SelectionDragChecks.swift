import Foundation

@main
struct SelectionDragChecks {
    struct Failure: Error, CustomStringConvertible { let description: String }
    static let allowed: Set<String> = ["price", "volume", "trend"]

    static func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
        if !condition() { throw Failure(description: message) }
    }

    static func seed() -> SelectionDefinition {
        SelectionDefinition(groups: [
            SelectionRuleGroup(id: "a", name: "第一组", and: ["price", "volume"]),
            SelectionRuleGroup(id: "b", name: "第二组", and: ["trend"])
        ], parameters: ["max_price": 60, "custom": 3], disabled: ["a|volume", "b|trend"])
    }

    static func payloadRoundTripsAndRejectsInvalidStrings() throws {
        for transfer in [SelectionCriterionTransfer(criterionID: "price"),
                         SelectionCriterionTransfer(criterionID: "price", sourceGroupID: "a", sourceExcluded: true)] {
            let decoded = SelectionCriterionTransfer.decode(transfer.stringValue)
            try require(decoded?.criterionID == transfer.criterionID
                        && decoded?.sourceGroupID == transfer.sourceGroupID
                        && decoded?.sourceExcluded == transfer.sourceExcluded, "A valid drag payload did not round trip")
        }
        let invalid = ["price", "stocks-criterion:v2:{}", "stocks-criterion:v1:not-json",
                       "stocks-criterion:v1:[]", "stocks-criterion:v1:{}",
                       "stocks-criterion:v1:{\"criterionID\":\"price\",\"sourceExcluded\":\"false\"}",
                       "stocks-criterion:v1:{\"criterionID\":\"price\",\"sourceExcluded\":true}",
                       "stocks-criterion:v1:{\"criterionID\":\"price|not:trend\",\"sourceExcluded\":false}",
                       "stocks-criterion:v1:" + String(repeating: "x", count: 2048)]
        for value in invalid { try require(SelectionCriterionTransfer.decode(value) == nil, "An invalid external payload was accepted") }
        try require(SelectionCriterionTransfer(criterionID: "").stringValue.isEmpty, "An empty ID became draggable")
        try require(SelectionCriterionTransfer(criterionID: String(repeating: "x", count: 129)).stringValue.isEmpty,
                    "An overlong identifier became draggable")
        try require(SelectionCriterionTransfer(criterionID: "price", sourceGroupID: "a\n").stringValue.isEmpty,
                    "A control character was accepted in a source identifier")
    }

    static func poolCopiesToMultipleGroups() throws {
        var definition = seed()
        let transfer = SelectionCriterionTransfer(criterionID: "volume")
        let payload = transfer.stringValue
        try require(definition.applyCriterionDrop(transfer, targetGroupID: "b", excluded: false, allowedIDs: allowed), "Pool copy failed")
        try require(definition.groups[0].and == ["price", "volume"] && definition.groups[1].and == ["trend", "volume"],
                    "Pool copy removed the criterion from another group")
        try require(transfer.stringValue == payload, "Pool data changed after a copy")
        try require(definition.disabled == seed().disabled && definition.parameters == seed().parameters,
                    "A pool copy changed unrelated switches or parameters")
    }

    static func sameGroupMoveIsExclusiveAndMovesDisabledKey() throws {
        var definition = seed()
        let transfer = SelectionCriterionTransfer(criterionID: "volume", sourceGroupID: "a")
        try require(definition.applyCriterionDrop(transfer, targetGroupID: "a", excluded: true, allowedIDs: allowed), "AND-to-NOT move failed")
        try require(definition.groups[0].and == ["price"] && definition.groups[0].not == ["volume"], "A criterion remained on both sides")
        try require(definition.disabled.contains("a|not:volume") && !definition.disabled.contains("a|volume"), "The disabled key did not follow the operator")
        let reverse = SelectionCriterionTransfer(criterionID: "volume", sourceGroupID: "a", sourceExcluded: true)
        try require(definition.applyCriterionDrop(reverse, targetGroupID: "a", excluded: false, allowedIDs: allowed), "NOT-to-AND move failed")
        try require(definition == seed(), "Moving back lost parameters, order, switches, or another group")
    }

    static func crossGroupMoveDeduplicatesAndPreservesSourceState() throws {
        var definition = seed()
        definition.groups[1].and += ["volume", "volume"]
        definition.groups[1].not = ["volume"]
        definition.disabled += ["a|not:volume", "b|volume", "b|not:volume"]
        try require(definition.applyCriterionDrop(SelectionCriterionTransfer(criterionID: "volume", sourceGroupID: "a"),
            targetGroupID: "b", excluded: true, allowedIDs: allowed), "Cross-group move failed")
        try require(definition.groups[0].and == ["price"] && definition.groups[0].not.isEmpty
                    && definition.groups[1].and == ["trend"] && definition.groups[1].not == ["volume"],
                    "Cross-group movement left duplicates or deleted an unrelated criterion")
        try require(definition.disabled == ["b|trend", "b|not:volume"], "Moving left stale disabled keys at the source or opposite target operator")
        try require(definition.parameters == seed().parameters && definition.groups[0].name == "第一组", "Moving changed unrelated configuration")

        definition = seed()
        definition.groups[0].enabled = false
        try require(definition.applyCriterionDrop(SelectionCriterionTransfer(criterionID: "price", sourceGroupID: "a"),
            targetGroupID: "b", excluded: false, allowedIDs: allowed), "Moving from a disabled group failed")
        try require(definition.disabled.contains("b|price") && !definition.groups[0].enabled && definition.groups[1].enabled,
                    "Dragging out of a disabled group unexpectedly enabled the criterion")
    }

    static func poolRedropPreservesSwitchAndSameLocationIsStable() throws {
        var definition = seed()
        try require(definition.applyCriterionDrop(SelectionCriterionTransfer(criterionID: "volume"),
            targetGroupID: "a", excluded: false, allowedIDs: allowed), "Pool redrop was rejected")
        try require(definition == seed(), "Pool redrop reordered or enabled an existing criterion")
        try require(definition.applyCriterionDrop(SelectionCriterionTransfer(criterionID: "price", sourceGroupID: "a"),
            targetGroupID: "a", excluded: false, allowedIDs: allowed), "Same-position drop should report success")
        try require(definition == seed(), "Same-position movement changed the definition")
        try require(definition.applyCriterionDrop(SelectionCriterionTransfer(criterionID: "volume"),
            targetGroupID: "a", excluded: true, allowedIDs: allowed), "Pool redrop across operators failed")
        try require(definition.disabled.contains("a|not:volume") && !definition.disabled.contains("a|volume"),
                    "Pool redrop lost an existing disabled state")
    }

    static func invalidDropsLeaveDefinitionUntouched() throws {
        let cases: [(SelectionCriterionTransfer, String)] = [
            (SelectionCriterionTransfer(criterionID: "unknown", sourceGroupID: "a"), "b"),
            (SelectionCriterionTransfer(criterionID: "price", sourceGroupID: "missing"), "b"),
            (SelectionCriterionTransfer(criterionID: "trend", sourceGroupID: "a"), "b"),
            (SelectionCriterionTransfer(criterionID: "price", sourceGroupID: "a", sourceExcluded: true), "b"),
            (SelectionCriterionTransfer(criterionID: "price", sourceGroupID: "a"), "missing"),
            (SelectionCriterionTransfer(criterionID: "price", sourceExcluded: true), "b")
        ]
        for (transfer, target) in cases {
            var definition = seed()
            try require(!definition.applyCriterionDrop(transfer, targetGroupID: target, excluded: false, allowedIDs: allowed),
                        "An invalid source, target, or criterion was accepted")
            try require(definition == seed(), "A failed drop partially changed the definition")
        }
        var duplicateGroups = seed()
        duplicateGroups.groups.append(duplicateGroups.groups[1])
        let original = duplicateGroups
        try require(!duplicateGroups.applyCriterionDrop(SelectionCriterionTransfer(criterionID: "price", sourceGroupID: "a"),
            targetGroupID: "b", excluded: false, allowedIDs: allowed) && duplicateGroups == original,
                    "An ambiguous target group was mutated")
    }

    static func main() throws {
        let checks: [(String, () throws -> Void)] = [
            ("typed bounded payload", payloadRoundTripsAndRejectsInvalidStrings),
            ("pool copy to multiple groups", poolCopiesToMultipleGroups),
            ("same-group AND/NOT transfer", sameGroupMoveIsExclusiveAndMovesDisabledKey),
            ("cross-group deduplication and disabled state", crossGroupMoveDeduplicatesAndPreservesSourceState),
            ("pool redrop and stable same-location drop", poolRedropPreservesSwitchAndSameLocationIsStable),
            ("invalid drops are atomic", invalidDropsLeaveDefinitionUntouched)
        ]
        for (name, check) in checks { try check(); print("PASS: \(name)") }
        print("SelectionDragChecks: \(checks.count) contracts passed")
    }
}

import CoreSpotlight
import Foundation
import UniformTypeIdentifiers

struct ReaderNativeActivityRoute: Equatable, Sendable {
    static let host = "reader-feature"

    let action: ReaderNativeFeatureAction
    let localBookID: String?

    init(
        action: ReaderNativeFeatureAction,
        localBookID: String? = nil
    ) {
        self.action = action
        self.localBookID = localBookID
    }

    static func url(
        for action: ReaderNativeFeatureAction,
        localBookID: String? = nil
    ) -> URL? {
        var components = URLComponents()
        components.scheme = ReaderNativeBridgeContract.launchScheme
        components.host = host
        var queryItems = [
            URLQueryItem(name: "action", value: action.rawValue),
        ]
        if let localBookID {
            guard isOpaqueLocalBookID(localBookID) else { return nil }
            queryItems.append(URLQueryItem(name: "book", value: localBookID))
        }
        components.queryItems = queryItems
        return components.url
    }

    static func parse(_ url: URL) -> ReaderNativeActivityRoute? {
        guard
            url.scheme?.lowercased()
                == ReaderNativeBridgeContract.launchScheme,
            url.host?.lowercased() == host,
            let components = URLComponents(
                url: url,
                resolvingAgainstBaseURL: false
            ),
            let rawAction = components.queryItems?.first(
                where: { $0.name == "action" }
            )?.value,
            let action = ReaderNativeFeatureAction(rawValue: rawAction)
        else {
            return nil
        }

        let rawBookID = components.queryItems?.first(
            where: { $0.name == "book" }
        )?.value
        if rawBookID != nil && !isOpaqueLocalBookID(rawBookID ?? "") {
            return nil
        }
        return ReaderNativeActivityRoute(
            action: action,
            localBookID: rawBookID
        )
    }

    static func parse(
        _ userActivity: NSUserActivity,
        store: ReaderNativeFeatureStore = ReaderNativeFeatureStore()
    ) -> ReaderNativeActivityRoute? {
        if
            let webpageURL = userActivity.webpageURL,
            let route = parse(webpageURL)
        {
            return route
        }

        guard
            userActivity.activityType == CSSearchableItemActionType,
            let identifier = userActivity.userInfo?[
                CSSearchableItemActivityIdentifier
            ] as? String,
            identifier == ReaderSpotlightIndex.currentItemIdentifier
        else {
            return nil
        }

        let storedURL = store.readSnapshot().flatMap { URL(string: $0.url) }
        if let storedURL, let route = parse(storedURL) {
            return route
        }
        return ReaderNativeActivityRoute(action: .openReader)
    }

    private static func isOpaqueLocalBookID(_ value: String) -> Bool {
        guard value.hasPrefix("localbook-"), value.count == 74 else { return false }
        return value.dropFirst("localbook-".count).unicodeScalars.allSatisfy {
            (48...57).contains($0.value) || (97...102).contains($0.value)
        }
    }
}

enum ReaderSpotlightIndex {
    static let currentItemIdentifier =
        "space.bwicarus.bwreader2.current-reading"
    private static let domainIdentifier =
        "space.bwicarus.bwreader2.reading"

    static func indexStoredSnapshot(
        store: ReaderNativeFeatureStore = ReaderNativeFeatureStore()
    ) async throws -> Bool {
        guard let snapshot = store.readSnapshot() else { return false }
        try await indexCurrentSnapshot(snapshot)
        return true
    }

    static func indexCurrentSnapshot(
        _ snapshot: ReaderSharedSnapshot
    ) async throws {
        let attributes = CSSearchableItemAttributeSet(contentType: .text)
        attributes.title = snapshot.title.isEmpty
            ? "BW 阅读器"
            : snapshot.title
        attributes.contentDescription = contentDescription(for: snapshot)
        attributes.textContent = snapshot.searchableText
        attributes.keywords = spotlightKeywords(for: snapshot)
        let snapshotURL = URL(string: snapshot.url)
        attributes.contentURL = snapshotURL.flatMap {
            ReaderNativeActivityRoute.parse($0)
        } == nil
            ? ReaderNativeActivityRoute.url(for: .openReader)
            : snapshotURL

        let item = CSSearchableItem(
            uniqueIdentifier: currentItemIdentifier,
            domainIdentifier: domainIdentifier,
            attributeSet: attributes
        )
        item.expirationDate = Calendar.current.date(
            byAdding: .day,
            value: 30,
            to: Date()
        )

        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            CSSearchableIndex.default().indexSearchableItems([item]) { error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: ())
                }
            }
        }
    }

    static func removeCurrentSnapshot() async throws {
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            CSSearchableIndex.default().deleteSearchableItems(
                withIdentifiers: [currentItemIdentifier]
            ) { error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: ())
                }
            }
        }
    }

    private static func contentDescription(
        for snapshot: ReaderSharedSnapshot
    ) -> String {
        let excerpt = snapshot.selection.isEmpty
            ? snapshot.visibleText
            : snapshot.selection
        let compactExcerpt = excerpt
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if compactExcerpt.isEmpty {
            return snapshot.pageSummary
        }
        return "\(snapshot.pageSummary) · \(compactExcerpt.prefix(240))"
    }

    private static func spotlightKeywords(
        for snapshot: ReaderSharedSnapshot
    ) -> [String] {
        var keywords = ["BW 阅读器", "阅读", "书籍"]
        if !snapshot.file.isEmpty {
            keywords.append(String(snapshot.file.prefix(160)))
        }
        if !snapshot.page.isEmpty {
            keywords.append("第 \(snapshot.page) 页")
        }
        return keywords
    }
}

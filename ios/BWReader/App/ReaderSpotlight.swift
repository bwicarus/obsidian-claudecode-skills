import CoreSpotlight
import Foundation
import UniformTypeIdentifiers

struct ReaderNativeActivityRoute: Equatable, Sendable {
    static let host = "reader-feature"

    let action: ReaderNativeFeatureAction
    let readerURL: URL?

    static func url(
        for action: ReaderNativeFeatureAction,
        readerURL: URL? = nil
    ) -> URL? {
        var components = URLComponents()
        components.scheme = ReaderNativeBridgeContract.launchScheme
        components.host = host
        var queryItems = [
            URLQueryItem(name: "action", value: action.rawValue),
        ]
        if let readerURL, isSupportedReaderURL(readerURL) {
            queryItems.append(
                URLQueryItem(name: "url", value: readerURL.absoluteString)
            )
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

        let candidate = components.queryItems?.first(
            where: { $0.name == "url" }
        )?.value.flatMap(URL.init(string:))
        let readerURL = candidate.flatMap {
            isSupportedReaderURL($0) ? $0 : nil
        }
        return ReaderNativeActivityRoute(action: action, readerURL: readerURL)
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

        let readerURL = store.readSnapshot().flatMap {
            URL(string: $0.url)
        }.flatMap {
            isSupportedReaderURL($0) ? $0 : nil
        }
        return ReaderNativeActivityRoute(
            action: .openReader,
            readerURL: readerURL
        )
    }

    private static func isSupportedReaderURL(_ url: URL) -> Bool {
        guard url.user == nil, url.password == nil else { return false }
        switch url.scheme?.lowercased() {
        case "http", "https":
            return true
        default:
            return false
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
        attributes.contentURL = ReaderNativeActivityRoute.url(
            for: .openReader,
            readerURL: URL(string: snapshot.url)
        )

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

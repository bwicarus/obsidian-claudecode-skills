// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "NativeMarkdownChecks", platforms: [.macOS(.v13)], dependencies: [
    .package(url: "https://github.com/swiftlang/swift-cmark.git", exact: "0.9.0"),
    .package(url: "https://github.com/scinfu/SwiftSoup.git", exact: "2.13.9")
], targets: [.executableTarget(name: "NativeMarkdownChecks", dependencies: [
    .product(name: "cmark-gfm", package: "swift-cmark"), .product(name: "cmark-gfm-extensions", package: "swift-cmark"),
    .product(name: "SwiftSoup", package: "SwiftSoup")
], path: ".", exclude: ["Package.swift"], sources: ["main.swift", "ReaderNativeMarkdown.swift", "ReaderNativeMathSyntax.swift", "ReaderNativeInlineMedia.swift", "ReaderNativeMediaRoute.swift", "ReaderNativeAnkiProjection.swift", "ReaderNativeReviewFaces.swift", "ReaderNativeWordCards.swift", "ReaderNativeDataStore.swift", "ReaderNativeLookupRequest.swift", "ReaderNativeFavoritePlacement.swift", "ReaderNativeFavoritesService.swift", "ReaderNativeCardRules.swift", "ReaderNativePageCardHTML.swift"])])

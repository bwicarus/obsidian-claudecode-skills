// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "NativeEPUBArchiveChecks", platforms: [.macOS(.v13)], dependencies: [
    .package(url: "https://github.com/weichsel/ZIPFoundation.git", exact: "0.9.20")
], targets: [.executableTarget(name: "NativeEPUBArchiveChecks", dependencies: [
    .product(name: "ZIPFoundation", package: "ZIPFoundation")
], path: ".", exclude: ["Package.swift"], sources: ["main.swift", "ReaderNativeEPUBArchive.swift"])])

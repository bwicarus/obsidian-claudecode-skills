import CryptoKit
import Foundation
import SwiftUI

// 离线日语词典的**下载与安装**，以及驱动 UI 的 Manager。
//
// 只读核心（存储路径、校验、读取）在 Shared/ReaderOfflineDictionaryCore.swift ——
// Safari 扩展 target 也要编译那一半（C 组 #19：扩展查词走 App 的本地词典而不是
// 打 Pi）。下载留在这里：它用 URLSession 拉几百 MB，扩展既不需要也不该有。

private enum ReaderOfflineDictionaryInstaller {
    private struct Asset: Sendable {
        let relative: String
        let bytes: Int64
        let sha256: String
    }

    static func install(
        progress: @escaping @Sendable (Int64, Int64) async -> Void
    ) async throws -> ReaderOfflineDictionaryInfo {
        try Task.checkCancellation()
        try ReaderOfflineDictionaryStore.prepareApplicationRoot()

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 180
        configuration.waitsForConnectivity = true
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }

        let manifestURL = try ReaderOfflineDictionaryStore.remoteURL(
            relative: "manifest.json"
        )
        let manifestData = try await fetch(
            url: manifestURL,
            session: session,
            label: "manifest.json"
        )
        let manifest = try ReaderOfflineDictionaryStore.decodeAndValidateManifest(
            manifestData
        )

        let licenseBytes = manifest.license.bytes ?? 0
        let chineseLicenseBytes = manifest.chineseLicense.bytes ?? 0
        let tanakaLicenseBytes = manifest.tanakaLicense.bytes ?? 0
        var assets = [
            Asset(
                relative: manifest.license.path,
                bytes: licenseBytes,
                sha256: manifest.license.sha256
            ),
            Asset(
                relative: manifest.chineseLicense.path,
                bytes: chineseLicenseBytes,
                sha256: manifest.chineseLicense.sha256
            ),
            Asset(
                relative: manifest.tanakaLicense.path,
                bytes: tanakaLicenseBytes,
                sha256: manifest.tanakaLicense.sha256
            ),
        ]
        assets.append(contentsOf: manifest.resources.sorted(by: { $0.key < $1.key }).map {
            Asset(
                relative: $0.value.path,
                bytes: $0.value.bytes ?? 0,
                sha256: $0.value.sha256
            )
        })
        assets.append(contentsOf: manifest.shards.sorted(by: { $0.key < $1.key }).map {
            Asset(
                relative: $0.value.path,
                bytes: $0.value.bytes,
                sha256: $0.value.sha256
            )
        })
        let totalBytes = Int64(manifestData.count)
            + assets.reduce(Int64(0)) { $0 + $1.bytes }

        let staging = try ReaderOfflineDictionaryStore.partialRoot()
        if let existingManifest = try? Data(
            contentsOf: staging.appendingPathComponent("manifest.json"),
            options: .mappedIfSafe
        ), ReaderOfflineDictionaryStore.sha256Hex(existingManifest)
            != ReaderOfflineDictionaryStore.manifestDigest {
            try? FileManager.default.removeItem(at: staging)
        }
        try FileManager.default.createDirectory(
            at: staging.appendingPathComponent("shards", isDirectory: true),
            withIntermediateDirectories: true
        )
        try manifestData.write(
            to: staging.appendingPathComponent("manifest.json"),
            options: .atomic
        )

        var completedBytes = Int64(manifestData.count)
        await progress(completedBytes, totalBytes)
        var nextIndex = 0
        try await withThrowingTaskGroup(of: Int64.self) { group in
            let concurrency = min(4, assets.count)
            for _ in 0..<concurrency {
                let asset = assets[nextIndex]
                nextIndex += 1
                group.addTask {
                    try await installAsset(
                        asset,
                        staging: staging,
                        session: session
                    )
                }
            }
            while let completed = try await group.next() {
                completedBytes += completed
                await progress(completedBytes, totalBytes)
                if nextIndex < assets.count {
                    let asset = assets[nextIndex]
                    nextIndex += 1
                    group.addTask {
                        try await installAsset(
                            asset,
                            staging: staging,
                            session: session
                        )
                    }
                }
            }
        }

        let installedAt = Date()
        try ReaderOfflineDictionaryStore.writeInstallMarker(
            at: staging,
            bytes: totalBytes,
            installedAt: installedAt
        )
        let destination = try ReaderOfflineDictionaryStore.releaseRoot()
        try FileManager.default.createDirectory(
            at: destination.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if FileManager.default.fileExists(atPath: destination.path) {
            try FileManager.default.removeItem(at: destination)
        }
        try FileManager.default.moveItem(at: staging, to: destination)

        let releases = destination.deletingLastPathComponent()
        if let children = try? FileManager.default.contentsOfDirectory(
            at: releases,
            includingPropertiesForKeys: nil
        ) {
            for child in children where child.lastPathComponent
                != ReaderOfflineDictionaryStore.datasetID {
                try? FileManager.default.removeItem(at: child)
            }
        }
        return ReaderOfflineDictionaryInfo(
            release: ReaderOfflineDictionaryStore.sourceRelease,
            byteCount: totalBytes,
            installedAt: installedAt
        )
    }

    private static func installAsset(
        _ asset: Asset,
        staging: URL,
        session: URLSession
    ) async throws -> Int64 {
        try Task.checkCancellation()
        let target = asset.relative.split(separator: "/").reduce(staging) {
            $0.appendingPathComponent(String($1), isDirectory: false)
        }
        if let existing = try? Data(contentsOf: target, options: .mappedIfSafe),
           existing.count == Int(asset.bytes),
           ReaderOfflineDictionaryStore.sha256Hex(existing) == asset.sha256 {
            return asset.bytes
        }
        let remote = try ReaderOfflineDictionaryStore.remoteURL(
            relative: asset.relative
        )
        let data = try await fetch(
            url: remote,
            session: session,
            label: asset.relative
        )
        guard data.count == Int(asset.bytes),
              ReaderOfflineDictionaryStore.sha256Hex(data) == asset.sha256 else {
            throw ReaderOfflineDictionaryError.invalidResource(asset.relative)
        }
        try data.write(to: target, options: .atomic)
        return asset.bytes
    }

    private static func fetch(
        url: URL,
        session: URLSession,
        label: String
    ) async throws -> Data {
        do {
            let (data, response) = try await session.data(from: url)
            guard let http = response as? HTTPURLResponse,
                  http.statusCode == 200 else {
                let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                throw ReaderOfflineDictionaryError.sourceUnavailable(
                    "\(label) HTTP \(status)"
                )
            }
            return data
        } catch is CancellationError {
            throw CancellationError()
        } catch let error as ReaderOfflineDictionaryError {
            throw error
        } catch {
            throw ReaderOfflineDictionaryError.sourceUnavailable(
                "\(label)：\(error.localizedDescription)"
            )
        }
    }
}

@MainActor
final class ReaderOfflineDictionaryManager: ObservableObject {
    enum State: Equatable {
        case notInstalled
        case downloading
        case installed(ReaderOfflineDictionaryInfo)
        case failed(String)
    }

    static let shared = ReaderOfflineDictionaryManager()

    @Published private(set) var state: State = .notInstalled
    @Published private(set) var completedBytes: Int64 = 0
    @Published private(set) var totalBytes: Int64 = 0
    private var downloadTask: Task<Void, Never>?

    private init() {
        refresh()
    }

    var isDownloading: Bool {
        state == .downloading
    }

    var isInstalled: Bool {
        if case .installed = state { return true }
        return false
    }

    var progress: Double {
        guard totalBytes > 0 else { return 0 }
        return min(1, Double(completedBytes) / Double(totalBytes))
    }

    var statusText: String {
        switch state {
        case .notInstalled:
            return "未下载"
        case .downloading:
            return "正在下载 \(Self.bytes(completedBytes)) / \(Self.bytes(totalBytes))"
        case .installed(let info):
            return "已下载 · \(Self.bytes(info.byteCount))"
        case .failed(let message):
            return "下载失败：\(message)"
        }
    }

    func refresh() {
        guard !isDownloading else { return }
        do {
            if let info = try ReaderOfflineDictionaryStore.installedInfo() {
                state = .installed(info)
                completedBytes = info.byteCount
                totalBytes = info.byteCount
            } else {
                state = .notInstalled
                completedBytes = 0
                totalBytes = 0
            }
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    func download() {
        guard downloadTask == nil else { return }
        state = .downloading
        completedBytes = 0
        totalBytes = 0
        downloadTask = Task { [weak self] in
            do {
                let info = try await ReaderOfflineDictionaryInstaller.install {
                    [weak self] completed, total in
                    await MainActor.run {
                        self?.completedBytes = completed
                        self?.totalBytes = total
                    }
                }
                guard !Task.isCancelled else { return }
                self?.state = .installed(info)
                self?.completedBytes = info.byteCount
                self?.totalBytes = info.byteCount
            } catch is CancellationError {
                self?.state = .notInstalled
            } catch {
                self?.state = .failed(error.localizedDescription)
            }
            self?.downloadTask = nil
        }
    }

    func removeDownloadedDictionary() {
        guard downloadTask == nil else { return }
        do {
            try ReaderOfflineDictionaryStore.removeAll()
            state = .notInstalled
            completedBytes = 0
            totalBytes = 0
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    private static func bytes(_ value: Int64) -> String {
        guard value > 0 else { return "0 MB" }
        return ByteCountFormatter.string(
            fromByteCount: value,
            countStyle: .file
        )
    }
}

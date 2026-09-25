import Foundation
import Combine
import UniformTypeIdentifiers
import ImageIO
import CoreTransferable
import UIKit
import PhotosUI
import SwiftUI

/// 已发送图片的侧栏缩略图，按附件编号存在本机缓存里（2026-09-26 用户：「本来就是本地上传，
/// 那就本地处理后显示就好了」）。服务器那份 thumb 只在本机没有时才去取（换设备 / 缓存被清）。
enum ReaderAttachmentThumbs {
    nonisolated static func isValidID(_ id: String) -> Bool { id.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil }
    nonisolated static func url(_ id: String) -> URL? {
        guard isValidID(id), let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first else { return nil }
        return base.appendingPathComponent("attachment-thumbs", isDirectory: true).appendingPathComponent(id + ".jpg")
    }
    nonisolated static func load(_ id: String) -> Data? { url(id).flatMap { try? Data(contentsOf: $0) } }
    nonisolated static func save(_ id: String, data: Data) {
        guard let target = url(id) else { return }
        try? FileManager.default.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: target, options: .atomic)
    }
}

struct ReaderPickedMedia: Transferable {
    let url: URL
    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(importedContentType: .data) { received in
            let size = try received.file.resourceValues(forKeys: [.fileSizeKey]).fileSize
            guard let size, size <= ReaderNativeMediaDraft.maximumBytes else {
                throw NSError(domain: "ReaderMedia", code: 1, userInfo: [NSLocalizedDescriptionKey: "请选择不超过 64 MB 的照片或视频。"])
            }
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let target = directory.appendingPathComponent(received.file.lastPathComponent)
            try FileManager.default.copyItem(at: received.file, to: target)
            return ReaderPickedMedia(url: target)
        }
    }
}

/// A draft is retained by the conversation model, including while its panel is closed.
/// Uploading files is not sending a message. Only the Send button dispatches ids.
@MainActor
final class ReaderNativeMediaDraft: ObservableObject {
    struct Item: Identifiable {
        let id: String
        let name: String
        let mode: String
        let origin: String
        var file: URL?
        var preview: URL?
        var thumb: URL?
        var mime = "application/octet-stream"
        var bytes = 0
        var uploaded = false
        var working = true
        var error: String?
        var link: String { origin + "/assistant-attachments/file/" + id }
        var icon: String {
            if mime.hasPrefix("image/") { return "photo" }
            if mime.hasPrefix("video/") { return "film" }
            if mime.hasPrefix("audio/") { return "waveform" }
            if mime == "application/pdf" { return "doc.richtext" }
            let ext = (name as NSString).pathExtension.lowercased()
            if ["xls", "xlsx", "csv", "numbers"].contains(ext) { return "tablecells" }
            if ["ppt", "pptx", "key"].contains(ext) { return "rectangle.on.rectangle" }
            if ["doc", "docx", "rtf"].contains(ext) { return "doc.richtext" }
            if ["json", "js", "ts", "py", "swift", "css", "html", "xml"].contains(ext) { return "curlybraces" }
            if mime.hasPrefix("text/") { return "doc.text" }
            if mime.contains("zip") || mime.contains("compressed") { return "doc.zipper" }
            return "doc"
        }
    }
    struct Submission {
        let ids: [String]
        let id: String
        let signature: String
        let referenceText: String
    }
    @Published private(set) var items: [Item] = []
    @Published private(set) var importing: [String: Int] = [:]
    @Published var error: String?
    private var tasks: [String: Task<Void, Never>] = [:]
    private var submissions: [String: String] = [:]
    nonisolated static let maximumBytes = 64 * 1024 * 1024

    func items(in mode: String) -> [Item] { items.filter { $0.mode == mode } }
    func ready(in mode: String) -> Bool {
        importing[mode, default: 0] == 0 && items(in: mode).allSatisfy { $0.uploaded && !$0.working }
    }

    // Photo-library export can take time (including an iCloud download). Reserve
    // its slots immediately so Send cannot race ahead of the selected files.
    func addPhotos(_ photos: [PhotosPickerItem], mode: String) {
        guard !photos.isEmpty else { return }
        guard items(in: mode).count + importing[mode, default: 0] + photos.count <= 10 else {
            error = "一条消息最多添加 10 个附件。"; return
        }
        error = nil
        importing[mode, default: 0] += photos.count
        let key = "import-" + UUID().uuidString
        tasks[key] = Task {
            defer { tasks.removeValue(forKey: key) }
            for photo in photos {
                do {
                    guard let picked = try await photo.loadTransferable(type: ReaderPickedMedia.self) else {
                        throw URLError(.cannotDecodeContentData)
                    }
                    importing[mode, default: 0] -= 1
                    add([picked.url], mode: mode, temporarySources: true)
                } catch {
                    importing[mode, default: 0] -= 1
                    self.error = "无法读取照片或视频：" + error.localizedDescription
                }
            }
        }
    }

    func add(_ urls: [URL], mode: String, temporarySources: Bool = false) {
        error = nil
        guard items(in: mode).count + importing[mode, default: 0] + urls.count <= 10 else {
            error = "一条消息最多添加 10 个附件。"
            if temporarySources { for url in urls { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) } }
            return
        }
        for source in urls {
            let id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
            items.append(Item(id: id, name: source.lastPathComponent, mode: mode, origin: ReaderServer.origin))
            tasks[id] = Task {
                let scoped = source.startAccessingSecurityScopedResource()
                defer {
                    if scoped { source.stopAccessingSecurityScopedResource() }
                    if temporarySources { try? FileManager.default.removeItem(at: source.deletingLastPathComponent()) }
                }
                do {
                    let prepared = try await Task.detached(priority: .utility) { try Self.prepare(source, id: id) }.value
                    guard !Task.isCancelled, let index = items.firstIndex(where: { $0.id == id }) else {
                        try? FileManager.default.removeItem(at: prepared.file.deletingLastPathComponent()); return
                    }
                    items[index].file = prepared.file; items[index].preview = prepared.preview; items[index].thumb = prepared.thumb
                    items[index].mime = prepared.mime; items[index].bytes = prepared.bytes
                    await upload(id)
                } catch {
                    fail(id, error)
                }
            }
        }
    }

    func retry(_ id: String) {
        guard let item = items.first(where: { $0.id == id }), !item.working, item.file != nil else { return }
        tasks[id] = Task { await upload(id) }
    }
    func remove(_ id: String) {
        tasks.removeValue(forKey: id)?.cancel()
        if let file = items.first(where: { $0.id == id })?.file {
            try? FileManager.default.removeItem(at: file.deletingLastPathComponent())
        }
        items.removeAll { $0.id == id }
    }
    func accepted(_ submission: Submission) {
        submissions.removeValue(forKey: submission.signature)
        submission.ids.forEach(remove)
    }

    func saveDrawing(_ image: UIImage, replacing id: String) {
        guard let item = items.first(where: { $0.id == id }) else { return }
        do {
            guard let data = image.pngData(), data.count <= Self.maximumBytes else {
                throw NSError(domain: "ReaderMedia", code: 1, userInfo: [NSLocalizedDescriptionKey: "标注后的图片超过 64 MB，请减少图片尺寸后重试。"])
            }
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let name = (item.name as NSString).deletingPathExtension + "（标注）.png"
            let url = directory.appendingPathComponent(name)
            try data.write(to: url, options: .atomic)
            // Once uploaded, the original is retained by the server. The edited copy gets
            // its own immutable id; it cannot overwrite a preview used by a sent turn.
            remove(id)
            add([url], mode: item.mode, temporarySources: true)
        } catch { self.error = "无法保存图片标注：" + error.localizedDescription }
    }

    func submission(text: String, mode: String) -> Submission? {
        let selected = items(in: mode)
        guard !selected.isEmpty, ready(in: mode) else { return nil }
        guard selected.allSatisfy({ $0.origin == ReaderServer.origin }) else {
            error = "服务器地址已变化，请移除附件并重新添加。"; return nil
        }
        let ids = selected.map(\.id)
        let signature = mode + "\n" + text + "\n" + ids.joined(separator: ",")
        if submissions[signature] == nil {
            submissions[signature] = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        }
        let records: [[String: Any]] = selected.map { ["name": $0.name, "mime": $0.mime, "bytes": $0.bytes, "url": $0.link] }
        let data = (try? JSONSerialization.data(withJSONObject: records, options: [.sortedKeys])) ?? Data()
        return Submission(ids: ids, id: submissions[signature]!, signature: signature,
            referenceText: "【用户附加文件】\n" + String(decoding: data, as: UTF8.self))
    }

    private struct Prepared: Sendable { let file: URL; let preview: URL?; let thumb: URL?; let mime: String; let bytes: Int }
    nonisolated private static func prepare(_ source: URL, id: String) throws -> Prepared {
        let values = try source.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey, .contentTypeKey])
        guard values.isRegularFile == true, let size = values.fileSize, size <= maximumBytes else {
            throw NSError(domain: "ReaderMedia", code: 1, userInfo: [NSLocalizedDescriptionKey: "请选择不超过 64 MB 的文件。"])
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("reader-media-" + id, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        do {
            let target = directory.appendingPathComponent("original").appendingPathExtension(source.pathExtension)
            try FileManager.default.copyItem(at: source, to: target)
            var preview: URL?
            let type = values.contentType ?? UTType(filenameExtension: source.pathExtension)
            if type?.conforms(to: .image) == true,
               let image = CGImageSourceCreateWithURL(target as CFURL, [kCGImageSourceShouldCache: false] as CFDictionary),
               let thumbnail = CGImageSourceCreateThumbnailAtIndex(image, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: 2048,
                kCGImageSourceShouldCacheImmediately: true,
               ] as CFDictionary) {
                let url = directory.appendingPathComponent(".reader-preview.jpg")
                if let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil) {
                    CGImageDestinationAddImage(destination, thumbnail, [kCGImageDestinationLossyCompressionQuality: 0.88] as CFDictionary)
                    if CGImageDestinationFinalize(destination) { preview = url }
                }
            }
            // 侧栏缩略图：320px、低质量 —— 本机缓存一份直接显示，也只把这份传给服务器留作同步。
            var thumb: URL?
            if type?.conforms(to: .image) == true,
               let image = CGImageSourceCreateWithURL(target as CFURL, [kCGImageSourceShouldCache: false] as CFDictionary),
               let small = CGImageSourceCreateThumbnailAtIndex(image, 0, [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: 320,
               ] as CFDictionary),
               let cached = ReaderAttachmentThumbs.url(id) {
                try? FileManager.default.createDirectory(at: cached.deletingLastPathComponent(), withIntermediateDirectories: true)
                if let destination = CGImageDestinationCreateWithURL(cached as CFURL, UTType.jpeg.identifier as CFString, 1, nil) {
                    CGImageDestinationAddImage(destination, small, [kCGImageDestinationLossyCompressionQuality: 0.45] as CFDictionary)
                    if CGImageDestinationFinalize(destination) { thumb = cached }
                }
            }
            return Prepared(file: target, preview: preview, thumb: thumb, mime: type?.preferredMIMEType ?? "application/octet-stream", bytes: size)
        } catch {
            try? FileManager.default.removeItem(at: directory); throw error
        }
    }

    private func upload(_ id: String) async {
        guard let index = items.firstIndex(where: { $0.id == id }), let file = items[index].file else { return }
        items[index].working = true; items[index].error = nil
        let item = items[index]
        do {
            var headers = ["X-BW-Attachment-Type": item.mime]
            headers["X-BW-Attachment-Name"] = item.name.addingPercentEncoding(withAllowedCharacters: .alphanumerics)
            _ = try await Self.post(file, url: item.origin + "/assistant-attachments/upload/" + id, origin: item.origin, headers: headers)
            if let preview = item.preview {
                _ = try await Self.post(preview, url: item.origin + "/assistant-attachments/preview/" + id, origin: item.origin)
            }
            if let thumb = item.thumb {
                _ = try await Self.post(thumb, url: item.origin + "/assistant-attachments/thumb/" + id, origin: item.origin)
            }
            try Task.checkCancellation()
            guard let current = items.firstIndex(where: { $0.id == id }) else { return }
            items[current].uploaded = true; items[current].working = false
            tasks.removeValue(forKey: id)
        } catch { fail(id, error) }
    }
    private func fail(_ id: String, _ failure: Error) {
        guard let index = items.firstIndex(where: { $0.id == id }) else { return }
        items[index].working = false; items[index].error = failure.localizedDescription
        tasks.removeValue(forKey: id)
    }
    private static func post(_ file: URL, url: String, origin: String, headers: [String: String] = [:]) async throws -> Data {
        guard let endpoint = URL(string: url) else { throw URLError(.badURL) }
        var request = URLRequest(url: endpoint); request.httpMethod = "POST"; request.timeoutInterval = 180
        request.setValue(origin, forHTTPHeaderField: "Origin")
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        for (name, value) in headers { request.setValue(value, forHTTPHeaderField: name) }
        let (data, response) = try await URLSession.shared.upload(for: request, fromFile: file)
        let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard (response as? HTTPURLResponse)?.statusCode == 200, result?["ok"] as? Bool == true else {
            throw NSError(domain: "ReaderMedia", code: 2, userInfo: [NSLocalizedDescriptionKey:
                result?["message"] as? String ?? "附件上传未完成，请确认服务器已更新并重试。"])
        }
        return data
    }
}

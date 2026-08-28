import Foundation

/// 跟「我的服务器」之间的书库往来。
///
/// ## ⚠ 为什么是一个新客户端，而不是改 `ReaderRemoteLibraryClient` 的地址
///
/// 那个类的 `baseURL` 写死了 Pi，而它同时在为 **Pi 书库的浏览与下载**
/// （`/pdf/api/library/catalog`）服务。整体改指向会把那些一起弄坏 ——
/// 而且服务器侧的路径也不一样（`/reader-library/*`）。
///
/// **不假装"迁移完了"**：新能力走新客户端，存量原样保留，什么时候搬是另一件事。
/// 这跟 `ReaderServer` 文件头那条是同一个道理 —— 半吊子的迁移比不迁移更糟，
/// 因为它让人以为旧的那套可以关掉了。
///
/// ## 它服务的规矩
///
/// > **本地的书必须先上传服务器才能开始使用。**（用户 2026-08-28 拍板 A 方案）
///
/// 所以**上传失败必须是一件很响的事** —— 它直接等于"这本书用不了"。
/// 每一种失败都要能让人知道下一步做什么，而不是一句"上传失败"。
enum ReaderServerLibrary {

    struct Book: Decodable, Identifiable {
        let name: String
        let bytes: Int
        let sha256: String
        var id: String { sha256 }
    }

    private struct ListResponse: Decodable {
        let ok: Bool
        let root: String
        let books: [Book]
    }

    private struct UploadResponse: Decodable {
        let ok: Bool
        let code: String
        let message: String
        let name: String?
        let duplicate: Bool
    }

    enum Failure: LocalizedError {
        case serverUnreachable(String)
        case rejected(code: String, message: String)
        case malformed
        /// 服务器在，但**还没有这个端点**（404）。
        ///
        /// ⚠ 这跟「服务器没开」是两件完全不同的事,必须分开：
        /// 没开 → 等它开机,规矩照常生效;
        /// **没有这个能力** → 规矩在服务器上还没落地,
        /// 而**强制一条根本不可能被满足的规则,等于把书全锁死**。
        /// 2026-08-28 我差点就这么发出去了:App 侧规矩先上线,服务器端点还在
        /// 另一个发布通道里没走 —— 那样每一本书都打不开。
        case capabilityMissing

        var errorDescription: String? {
            switch self {
            case .serverUnreachable(let detail):
                // ⚠ 这一条是 A 方案最常撞上的:服务器(现阶段是会关机的那台)
                // 没开。说清是"没开"而不是笼统的失败 —— 前者用户知道去开机,
                // 后者只会让人以为 App 坏了。
                return "连不上\(ReaderServer.displayName)（\(detail)）"
                    + "——开着它才能加新书"
            case .rejected(let code, let message):
                // 服务端的中文原文照抄。它本来就写给人看,而且区分了
                // "同名不同内容"和"名字不合法"这类需要不同处理的情况。
                return message + "（\(code)）"
            case .capabilityMissing:
                return "\(ReaderServer.displayName)上还没有书库功能"
            case .malformed:
                return "\(ReaderServer.displayName)返回的内容看不懂"
            }
        }
    }

    /// 服务器上有哪些书。
    static func list() async throws -> [Book] {
        guard let url = ReaderServer.url("/reader-library/list") else {
            throw Failure.malformed
        }
        let data = try await post(url, body: Data("{}".utf8),
                                  contentType: "application/json")
        guard let payload = try? JSONDecoder().decode(
            ListResponse.self, from: data), payload.ok else {
            throw Failure.malformed
        }
        return payload.books
    }

    /// 传一本书上去。
    ///
    /// - Returns: 服务器上的书名。⚠ **重传同一本不是失败** —— 换设备、
    ///   重装之后重传是正常操作，服务端按内容去重并如实说「已经有了」。
    @discardableResult
    static func upload(fileURL: URL) async throws -> String {
        guard let url = ReaderServer.url("/reader-library/upload") else {
            throw Failure.malformed
        }
        let name = fileURL.lastPathComponent
        let boundary = "bw-book-" + UUID().uuidString
        var body = Data()
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data(
            "Content-Disposition: form-data; name=\"file\"; filename=\"\(name)\"\r\n"
                .utf8))
        body.append(Data("Content-Type: application/octet-stream\r\n\r\n".utf8))
        body.append(try Data(contentsOf: fileURL))
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))

        let data = try await post(
            url, body: body,
            contentType: "multipart/form-data; boundary=" + boundary)
        guard let payload = try? JSONDecoder().decode(
            UploadResponse.self, from: data) else {
            throw Failure.malformed
        }
        guard payload.ok else {
            throw Failure.rejected(code: payload.code, message: payload.message)
        }
        return payload.name ?? name
    }

    private static func post(
        _ url: URL, body: Data, contentType: String
    ) async throws -> Data {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        // ⚠ 桥的第一道闸就是 Origin，少了它是 403 而不是"没鉴权"。
        request.setValue(ReaderServer.origin, forHTTPHeaderField: "Origin")
        // 书可能很大，给足时间但要有上限 —— 无限等在界面上跟死掉一样。
        request.timeoutInterval = 600
        request.httpBody = body
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse,
               http.statusCode == 404 || http.statusCode == 405 {
                // 端点不存在 —— 服务器还是旧版。**不是**它没开。
                throw Failure.capabilityMissing
            }
            if let http = response as? HTTPURLResponse,
               http.statusCode != 200,
               (try? JSONDecoder().decode(UploadResponse.self, from: data)) == nil {
                throw Failure.rejected(
                    code: "HTTP_\(http.statusCode)",
                    message: "服务器拒绝了这次请求")
            }
            return data
        } catch let failure as Failure {
            throw failure
        } catch {
            throw Failure.serverUnreachable(error.localizedDescription)
        }
    }
}

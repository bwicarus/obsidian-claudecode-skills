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
        // 只是问一张清单：20s 还没回就是"连不上"，别让「打开」在这里静静等 10 分钟
        // （09-02 用户："点击后没有任何反应"——闸在问清单时界面没有任何状态）。
        let data = try await post(url, body: Data("{}".utf8),
                                  contentType: "application/json", timeout: 20)
        guard let payload = try? JSONDecoder().decode(
            ListResponse.self, from: data), payload.ok else {
            throw Failure.malformed
        }
        return payload.books
    }

    /// 从服务器取回一本书（2026-09-02：Pi 退出书库线路，设备只从这里下书）。
    /// 流式写到 `destinationDirectory/<name>`；同名但字节数不同 → 加「 (服务器)」后缀，
    /// 绝不覆盖本机已有文件。返回最终文件名。
    static func download(
        _ book: Book,
        destinationDirectory: URL,
        progress: (@MainActor (Double) -> Void)? = nil
    ) async throws -> String {
        guard var components = URLComponents(string: ReaderServer.origin + "/reader-library/download") else {
            throw Failure.malformed
        }
        components.queryItems = [URLQueryItem(name: "name", value: book.name)]
        guard let url = components.url else { throw Failure.malformed }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue(ReaderServer.origin, forHTTPHeaderField: "Origin")
        request.timeoutInterval = 3600
        let bytes: URLSession.AsyncBytes
        let response: URLResponse
        do {
            (bytes, response) = try await URLSession.shared.bytes(for: request)
        } catch {
            throw Failure.serverUnreachable(error.localizedDescription)
        }
        if let http = response as? HTTPURLResponse {
            if http.statusCode == 404 || http.statusCode == 405 {
                // 404 可能是「端点没有」也可能是「这本没有」——桥用 JSON code 区分，
                // 但两种对用户的下一步都是"服务器上没有这本可下"。
                throw Failure.rejected(code: "HTTP_\(http.statusCode)", message: "服务器上没有这本，或它还没有下载能力")
            }
            if http.statusCode != 200 {
                throw Failure.rejected(code: "HTTP_\(http.statusCode)", message: "服务器拒绝了这次下载")
            }
        }
        var finalName = book.name
        var target = destinationDirectory.appendingPathComponent(finalName)
        if FileManager.default.fileExists(atPath: target.path) {
            let existing = (try? FileManager.default.attributesOfItem(atPath: target.path)[.size] as? Int) ?? -1
            if existing == book.bytes { return finalName }   // 已经有同一本，直接用
            let stem = (book.name as NSString).deletingPathExtension
            let ext = (book.name as NSString).pathExtension
            finalName = stem + " (服务器)" + (ext.isEmpty ? "" : "." + ext)
            target = destinationDirectory.appendingPathComponent(finalName)
        }
        let temporary = destinationDirectory.appendingPathComponent(finalName + ".part-download")
        FileManager.default.createFile(atPath: temporary.path, contents: nil)
        let handle = try FileHandle(forWritingTo: temporary)
        var written = 0
        var buffer = Data()
        buffer.reserveCapacity(1 << 20)
        let total = max(book.bytes, 1)
        do {
            for try await byte in bytes {
                buffer.append(byte)
                if buffer.count >= (1 << 20) {
                    try handle.write(contentsOf: buffer)
                    written += buffer.count
                    buffer.removeAll(keepingCapacity: true)
                    let fraction = min(1, Double(written) / Double(total))
                    if let progress { await progress(fraction) }
                }
            }
            if !buffer.isEmpty { try handle.write(contentsOf: buffer); written += buffer.count }
            try handle.close()
        } catch {
            try? handle.close()
            try? FileManager.default.removeItem(at: temporary)
            throw Failure.serverUnreachable(error.localizedDescription)
        }
        if written != book.bytes {
            try? FileManager.default.removeItem(at: temporary)
            throw Failure.rejected(code: "BW_LIBRARY_SHORT_READ",
                                   message: "下载到 \(written) 字节，服务器说应有 \(book.bytes) 字节")
        }
        try FileManager.default.moveItem(at: temporary, to: target)
        if let progress { await progress(1) }
        return finalName
    }

    /// 传一本书上去（流式 + 字节进度）。
    ///
    /// - Returns: 服务器上的书名。⚠ **重传同一本不是失败** —— 换设备、
    ///   重装之后重传是正常操作，服务端按内容去重并如实说「已经有了」。
    /// - 2026-09-02：改为 `URLSession.upload(fromFile:)` 流式上传，整本书不再
    ///   进内存；`progress` 收到 0…1 的已发送比例。老桥（只认 multipart）回
    ///   400 BW_LIBRARY_FORM_REQUIRED 时退回 multipart 老路。
    @discardableResult
    static func upload(
        fileURL: URL,
        progress: (@MainActor (Double) -> Void)? = nil
    ) async throws -> String {
        guard let url = ReaderServer.url("/reader-library/upload") else {
            throw Failure.malformed
        }
        let name = fileURL.lastPathComponent
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue(ReaderServer.origin, forHTTPHeaderField: "Origin")
        request.setValue(
            name.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? name,
            forHTTPHeaderField: "X-BW-Book-Name")
        // 大书 + Tailscale：给足时间，但要有上限 —— 无限等在界面上跟死掉一样。
        request.timeoutInterval = 3600
        let delegate = ReaderUploadProgressDelegate(progress)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.upload(
                for: request, fromFile: fileURL, delegate: delegate)
        } catch {
            throw Failure.serverUnreachable(error.localizedDescription)
        }
        if let http = response as? HTTPURLResponse,
           http.statusCode == 404 || http.statusCode == 405 {
            throw Failure.capabilityMissing
        }
        if let http = response as? HTTPURLResponse, http.statusCode == 400,
           let payload = try? JSONDecoder().decode(UploadResponse.self, from: data),
           payload.code == "BW_LIBRARY_FORM_REQUIRED" {
            // 旧桥只认 multipart —— 退回老路（整文件进内存、无进度）。
            return try await uploadMultipart(fileURL: fileURL)
        }
        guard let payload = try? JSONDecoder().decode(UploadResponse.self, from: data) else {
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                throw Failure.rejected(
                    code: "HTTP_\(http.statusCode)", message: "服务器拒绝了这次上传")
            }
            throw Failure.malformed
        }
        guard payload.ok else {
            throw Failure.rejected(code: payload.code, message: payload.message)
        }
        return payload.name ?? name
    }

    /// multipart 老路（整文件进内存，无进度）—— 只给还没升级的桥用。
    @discardableResult
    static func uploadMultipart(fileURL: URL) async throws -> String {
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

    // MARK: - 跨设备书籍状态（2026-09-19）
    //
    // ⚠ 在此之前这条链路**只有入口没有出口**：App 能从 Pi 拉一份状态包并原子导入，
    //   却没有任何一处把本机状态发布出去 —— 所以「多端同步」实际是单向的，
    //   iPad 上做的卡从来没被发布到任何地方，换台设备打开同一本书自然什么都没有。
    //   而那条仅有的入口还指着 Pi（用户 2026-08-30 已把 Pi 从架构里去掉，实测 502）。
    //   这里把出入两半都接到 Windows 桥上。

    /// 信封契约：桥把 POST 的**整个 body 当作一份包存下**，GET 原样返回。
    /// 所以 deviceId / contentSha256 / at 必须在顶层（桥要读它们），真正的包放 package。
    private static let userStateEnvelopeContract = "reader-book-user-state-envelope/1"

    private struct UserStateEnvelope: Codable {
        let contract: String
        let deviceId: String
        let contentSha256: String
        let at: Int64
        let package: ReaderBookUserStatePackage
    }

    /// 这台设备的稳定标识。桥按 (contentSha, deviceId) 分设备存包，靠它区分是谁推的。
    /// 随安装生成一次并持久化；重装当作换一台设备 —— 对这个用途足够，也不牵扯任何身份信息。
    static var deviceId: String {
        let key = "bw.reader.userstate.deviceId"
        if let existing = UserDefaults.standard.string(forKey: key),
           existing.count >= 8, existing.count <= 80,
           existing.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") }) {
            return existing
        }
        let fresh = "ios-" + UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: "")
        UserDefaults.standard.set(fresh, forKey: key)
        return fresh
    }

    /// 服务器上这本书最新的一份状态包（按内容 sha 找）。**没有任何设备推过 → nil**，
    /// 那不是错误：第一次用的时候本来就没有。
    ///
    /// ⚠ 交出的是**原始字节 + 服务器给的账号作用域摘要**，不是解析好的对象：
    ///   导入端要按原字节验摘要（"Its exact UTF-8 bytes are verified before the
    ///   renderer is allowed to parse or import it"），先解析再序列化回去就等于
    ///   把那道校验换成了对我自己的信任。
    static func userStatePayload(
        contentSha256: String
    ) async throws -> ReaderBookUserStateRemotePayload? {
        guard contentSha256.range(of: #"^[a-f0-9]{64}$"#, options: .regularExpression) != nil,
              var components = URLComponents(
                string: ReaderServer.url("/reader-library/user-state")?.absoluteString ?? ""
              ) else {
            throw Failure.malformed
        }
        components.queryItems = [
            URLQueryItem(name: "contentSha256", value: contentSha256),
        ]
        guard let url = components.url else { throw Failure.malformed }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        // ⚠ 桥的第一道闸就是 Origin，少了它是 403 而不是"没鉴权"。
        request.setValue(ReaderServer.origin, forHTTPHeaderField: "Origin")
        request.timeoutInterval = 60
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw Failure.malformed }
            if http.statusCode == 404 {
                // 端点不存在和"这本书还没人推过"都是 404。用响应体区分：
                // 后者带 code=BW_USER_STATE_NONE，那是正常的空，不该当成服务器旧版。
                if let body = try? JSONDecoder().decode(
                    UserStateAbsent.self, from: data
                ), body.code == "BW_USER_STATE_NONE" {
                    return nil
                }
                throw Failure.capabilityMissing
            }
            if http.statusCode == 405 { throw Failure.capabilityMissing }
            guard http.statusCode == 200 else {
                throw Failure.rejected(
                    code: "HTTP_\(http.statusCode)",
                    message: "服务器拒绝了这次请求")
            }
            // 契约与作用域都由服务器在响应头里给 —— 与 Pi 那条同形（ReaderRemoteLibrary:311）。
            guard http.value(forHTTPHeaderField: "X-Reader-User-State-Contract")
                    == ReaderBookUserStatePackage.currentContract,
                  let scope = http.value(
                    forHTTPHeaderField: "X-Reader-Account-Scope-Digest"
                  )?.lowercased(),
                  scope.range(of: #"^[a-f0-9]{64}$"#, options: .regularExpression) != nil,
                  data.count <= ReaderBookUserStatePackageCodec.maximumPackageBytes else {
                throw Failure.malformed
            }
            // 正文必须真是这本书的包 —— 只看头不看正文，等于没校验。
            guard let package = try? JSONDecoder().decode(
                ReaderBookUserStatePackage.self, from: data
            ), package.contract == ReaderBookUserStatePackage.currentContract,
                  package.contentSha256 == contentSha256 else {
                throw Failure.malformed
            }
            return ReaderBookUserStateRemotePayload(
                packageData: data,
                accountScopeDigest: scope
            )
        } catch let failure as Failure {
            throw failure
        } catch {
            throw Failure.serverUnreachable(error.localizedDescription)
        }
    }

    /// user-state 自己的回执形状 —— 只有这三个字段。
    private struct UserStatePublishResponse: Decodable {
        let ok: Bool
        let code: String?
        let message: String?
    }

    private struct UserStateAbsent: Decodable {
        let ok: Bool
        let code: String
    }

    /// 把本机这本书的状态包发布到服务器，供别的设备拉取。
    static func publishUserState(
        _ package: ReaderBookUserStatePackage
    ) async throws {
        guard let url = ReaderServer.url("/reader-library/user-state") else {
            throw Failure.malformed
        }
        let envelope = UserStateEnvelope(
            contract: userStateEnvelopeContract,
            deviceId: deviceId,
            contentSha256: package.contentSha256,
            at: Int64(Date().timeIntervalSince1970 * 1000),
            package: package
        )
        guard let body = try? JSONEncoder().encode(envelope) else {
            throw Failure.malformed
        }
        let data = try await post(
            url, body: body, contentType: "application/json", timeout: 120)
        // ⚠ **不要复用 UploadResponse**：它是传书用的，带一个非可选的 `duplicate`，
        //   而 user-state 的回执只有 {ok, code, message}。照搬的结果是解码必失败 ——
        //   于是一次**成功的保存**被判成失败，App 不停重试，桥侧日志里一串
        //   BW_USER_STATE_SAVED，用户界面上却是红条「服务器没有接受这份状态包」。
        //   2026-09-19 实录：04:41–04:47 七分钟里重复保存了 8 次。
        guard let result = try? JSONDecoder().decode(
            UserStatePublishResponse.self, from: data), result.ok else {
            let detail = (try? JSONDecoder().decode(
                UserStatePublishResponse.self, from: data))
            throw Failure.rejected(
                code: detail?.code ?? "BW_USER_STATE_PUBLISH",
                message: detail?.message ?? "服务器没有接受这份状态包")
        }
    }

    private static func post(
        _ url: URL, body: Data, contentType: String,
        timeout: TimeInterval = 600
    ) async throws -> Data {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        // ⚠ 桥的第一道闸就是 Origin，少了它是 403 而不是"没鉴权"。
        request.setValue(ReaderServer.origin, forHTTPHeaderField: "Origin")
        // 书可能很大，给足时间但要有上限 —— 无限等在界面上跟死掉一样。
        request.timeoutInterval = timeout
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

/// 上传字节进度：URLSession 的 didSendBodyData 回调（后台队列）→ 跳回主线程
/// 交给 0…1 比例的回调。回调是 @MainActor 的，界面与闸直接改状态不用再跳。
final class ReaderUploadProgressDelegate: NSObject, URLSessionTaskDelegate {
    private let progress: (@MainActor (Double) -> Void)?

    init(_ progress: (@MainActor (Double) -> Void)?) {
        self.progress = progress
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didSendBodyData bytesSent: Int64,
        totalBytesSent: Int64,
        totalBytesExpectedToSend: Int64
    ) {
        guard let progress, totalBytesExpectedToSend > 0 else { return }
        let fraction = min(1, Double(totalBytesSent) / Double(totalBytesExpectedToSend))
        Task { @MainActor in progress(fraction) }
    }
}

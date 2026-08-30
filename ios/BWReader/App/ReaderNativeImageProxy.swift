import Darwin
import Foundation
import Network
import Security

struct ReaderNativeImageProxyPayload: Sendable {
    let data: Data
    let contentType: String
}

enum ReaderNativeImageProxyError: LocalizedError {
    case invalidURL
    case blockedAddress
    case dnsUnavailable
    case tooManyRedirects
    case upstreamStatus(Int)
    case unsupportedContentType
    case tooLarge
    case empty
    case timedOut
    case transport
    case cancelled

    var httpStatus: Int {
        switch self {
        case .invalidURL, .blockedAddress, .dnsUnavailable:
            return 403
        case .unsupportedContentType:
            return 415
        case .tooLarge:
            return 413
        case .timedOut:
            return 504
        case .tooManyRedirects, .upstreamStatus, .empty, .transport,
             .cancelled:
            return 502
        }
    }

    var diagnosticCode: String {
        switch self {
        case .invalidURL: return "invalid-url"
        case .blockedAddress: return "blocked-address"
        case .dnsUnavailable: return "dns-unavailable"
        case .tooManyRedirects: return "too-many-redirects"
        case .upstreamStatus: return "upstream-status"
        case .unsupportedContentType: return "unsupported-content-type"
        case .tooLarge: return "too-large"
        case .empty: return "empty-image"
        case .timedOut: return "timeout"
        case .transport: return "transport"
        case .cancelled: return "cancelled"
        }
    }

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "BW_NATIVE_IMAGE_URL：只允许无凭据、无片段的公网 HTTPS 图片地址"
        case .blockedAddress:
            return "BW_NATIVE_IMAGE_ADDRESS：图片地址指向本机、内网或保留地址"
        case .dnsUnavailable:
            return "BW_NATIVE_IMAGE_DNS：图片主机无法解析为公网地址"
        case .tooManyRedirects:
            return "BW_NATIVE_IMAGE_REDIRECT：图片重定向次数过多"
        case .upstreamStatus(let status):
            return "BW_NATIVE_IMAGE_UPSTREAM：图片源返回 HTTP \(status)"
        case .unsupportedContentType:
            return "BW_NATIVE_IMAGE_TYPE：远端内容不是支持的图片格式"
        case .tooLarge:
            return "BW_NATIVE_IMAGE_SIZE：图片超过 16 MiB 上限"
        case .empty:
            return "BW_NATIVE_IMAGE_EMPTY：远端返回了空图片"
        case .timedOut:
            return "BW_NATIVE_IMAGE_TIMEOUT：图片请求超时"
        case .transport:
            return "BW_NATIVE_IMAGE_TRANSPORT：图片请求失败"
        case .cancelled:
            return "BW_NATIVE_IMAGE_CANCELLED：图片请求已取消"
        }
    }
}

/// 连接看门狗的超时旗（waitUntilReady 用）。跨队列读写，锁一下。
private final class ReaderNativeImageConnectFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func set() { lock.lock(); value = true; lock.unlock() }
    func get() -> Bool { lock.lock(); defer { lock.unlock() }; return value }
}

/// 图片代理的健康状态 —— 显示在「数据与同步」。
///
/// ⚠ 2026-08-30 的教训直通车：所有卡片图全裂时，Swift 端明明按十几种
/// 情况返回了具体错误码（DNS/被拒/超时/格式…），但 <img> 裂了只画一个
/// 问号 —— 错误码没有任何人看得到，排查只能整层整层地猜。
/// 「无控制台设备上沉默等于不可诊断」。
@MainActor
enum ReaderImageProxyHealth {
    private(set) static var line = "还没取过图"
    private static var failures = 0

    static func noteSuccess(host: String) {
        line = "正常（最近成功：\(host)）"
    }

    static func noteFailure(code: String, host: String) {
        failures += 1
        line = "失败：\(code) @ \(host)（本次运行累计 \(failures) 次）"
    }
}

/// Fetches display-only card images on-device. DNS policy and TCP routing use
/// the exact same address result, removing the DNS-rebinding window between a
/// public-address preflight and the actual connection. Every redirect gets a
/// fresh resolution and pin. TLS SNI/certificate identity and HTTP Host remain
/// the original URL hostname rather than the pinned IP.
actor ReaderNativeImageProxyBroker {
    static let maximumBytes = 16 * 1_024 * 1_024

    func fetch(rawURL: String) async throws -> ReaderNativeImageProxyPayload {
        let url = try ReaderNativeImageProxyPolicy.initialURL(rawURL)
        return try await ReaderNativeImageProxyTransport(
            maximumBytes: Self.maximumBytes
        ).start(url)
    }
}

private struct ReaderNativeImageResolvedHop: Sendable {
    let url: URL
    let hostname: String
    let port: UInt16
    let addresses: [NWEndpoint.Host]
}

private enum ReaderNativeImageProxyPolicy {
    static let supportedContentTypes: Set<String> = [
        "image/avif",
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/svg+xml",
        "image/webp",
    ]

    static func initialURL(_ rawValue: String) throws -> URL {
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              trimmed == rawValue,
              trimmed.utf8.count <= 4_096,
              !trimmed.contains("\\"),
              !trimmed.unicodeScalars.contains(where: {
                  $0.value < 0x20 || $0.value == 0x7f
              }),
              let url = URL(string: trimmed) else {
            throw ReaderNativeImageProxyError.invalidURL
        }
        try validateURLShape(url)
        return url
    }

    static func resolve(_ url: URL) throws -> ReaderNativeImageResolvedHop {
        try validateURLShape(url)
        guard let rawHost = url.host?.lowercased(), !rawHost.isEmpty else {
            throw ReaderNativeImageProxyError.invalidURL
        }
        let hostname = rawHost.hasSuffix(".")
            ? String(rawHost.dropLast())
            : rawHost
        let port = UInt16(url.port ?? 443)

        var hints = addrinfo()
        hints.ai_flags = AI_ADDRCONFIG
        hints.ai_family = AF_UNSPEC
        hints.ai_socktype = SOCK_STREAM
        hints.ai_protocol = IPPROTO_TCP
        var result: UnsafeMutablePointer<addrinfo>?
        let status = hostname.withCString { hostCString in
            String(port).withCString { portCString in
                getaddrinfo(hostCString, portCString, &hints, &result)
            }
        }
        guard status == 0, let first = result else {
            throw ReaderNativeImageProxyError.dnsUnavailable
        }
        defer { freeaddrinfo(first) }

        var endpoints: [NWEndpoint.Host] = []
        var seen = Set<String>()
        var cursor: UnsafeMutablePointer<addrinfo>? = first
        while let entry = cursor {
            defer { cursor = entry.pointee.ai_next }
            guard let address = entry.pointee.ai_addr else { continue }
            let numeric = numericAddress(
                address,
                length: socklen_t(entry.pointee.ai_addrlen)
            )
            guard let numeric, !numeric.isEmpty else {
                throw ReaderNativeImageProxyError.dnsUnavailable
            }
            if entry.pointee.ai_family == AF_INET {
                let bytes = address.withMemoryRebound(
                    to: sockaddr_in.self,
                    capacity: 1
                ) { pointer in
                    withUnsafeBytes(of: pointer.pointee.sin_addr) { Array($0) }
                }
                guard isPublicIPv4(bytes), let ip = IPv4Address(numeric) else {
                    throw ReaderNativeImageProxyError.blockedAddress
                }
                if seen.insert(numeric).inserted {
                    endpoints.append(.ipv4(ip))
                }
            } else if entry.pointee.ai_family == AF_INET6 {
                let bytes = address.withMemoryRebound(
                    to: sockaddr_in6.self,
                    capacity: 1
                ) { pointer in
                    withUnsafeBytes(of: pointer.pointee.sin6_addr) { Array($0) }
                }
                guard isPublicIPv6(bytes), let ip = IPv6Address(numeric) else {
                    throw ReaderNativeImageProxyError.blockedAddress
                }
                if seen.insert(numeric).inserted {
                    endpoints.append(.ipv6(ip))
                }
            }
        }
        guard !endpoints.isEmpty else {
            throw ReaderNativeImageProxyError.dnsUnavailable
        }
        // ⚠ IPv4 排最前（2026-08-30「所有卡片图全裂」排查的产物）。
        // 家庭网络常见"设备拿到了 IPv6 地址但路由是坏的"：getaddrinfo 把
        // AAAA 排在前面，而这里是逐个端点顺序尝试的裸 NWConnection ——
        // 没有 happy-eyeballs，先撞进坏 IPv6 就把整段预算耗光。
        // 取一张卡片图不需要 v6 优先，先走最可能通的那条。
        let ordered = endpoints.filter {
            if case .ipv4 = $0 { return true }
            return false
        } + endpoints.filter {
            if case .ipv4 = $0 { return false }
            return true
        }
        return ReaderNativeImageResolvedHop(
            url: url,
            hostname: hostname,
            port: port,
            addresses: ordered
        )
    }

    static func normalizedContentType(_ value: String?) -> String {
        String(value ?? "")
            .split(separator: ";", maxSplits: 1, omittingEmptySubsequences: true)
            .first.map(String.init)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased() ?? ""
    }

    private static func validateURLShape(_ url: URL) throws {
        let absolute = url.absoluteString
        guard absolute.utf8.count <= 4_096,
              !absolute.contains("\\"),
              !absolute.unicodeScalars.contains(where: {
                  $0.value < 0x20 || $0.value == 0x7f
              }),
              url.scheme?.lowercased() == "https",
              let host = url.host?.lowercased(), !host.isEmpty,
              url.user == nil, url.password == nil,
              url.fragment == nil,
              url.port == nil || (1...65_535).contains(url.port!) else {
            throw ReaderNativeImageProxyError.invalidURL
        }
        let canonicalHost = host.hasSuffix(".")
            ? String(host.dropLast())
            : host
        let blockedNames = [
            "localhost", ".localhost", ".local", ".internal", ".home.arpa",
        ]
        guard !blockedNames.contains(where: {
            canonicalHost == $0
                || ($0.hasPrefix(".") && canonicalHost.hasSuffix($0))
        }) else {
            throw ReaderNativeImageProxyError.blockedAddress
        }
    }

    private static func numericAddress(
        _ address: UnsafePointer<sockaddr>,
        length: socklen_t
    ) -> String? {
        var buffer = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        let status = buffer.withUnsafeMutableBufferPointer { output in
            getnameinfo(
                address,
                length,
                output.baseAddress,
                socklen_t(output.count),
                nil,
                0,
                NI_NUMERICHOST
            )
        }
        guard status == 0 else { return nil }
        return String(cString: buffer).split(separator: "%", maxSplits: 1)
            .first.map(String.init)
    }

    private static func isPublicIPv4(_ bytes: [UInt8]) -> Bool {
        guard bytes.count == 4 else { return false }
        let a = bytes[0], b = bytes[1], c = bytes[2]
        if a == 0 || a == 10 || a == 127 || a >= 224 { return false }
        if a == 100 && (64...127).contains(b) { return false }
        if a == 169 && b == 254 { return false }
        if a == 172 && (16...31).contains(b) { return false }
        if a == 192 && b == 168 { return false }
        if a == 192 && b == 0 && c == 0 { return false }
        if a == 192 && b == 0 && c == 2 { return false }
        if a == 198 && (b == 18 || b == 19) { return false }
        if a == 198 && b == 51 && c == 100 { return false }
        if a == 203 && b == 0 && c == 113 { return false }
        return true
    }

    private static func isPublicIPv6(_ bytes: [UInt8]) -> Bool {
        guard bytes.count == 16 else { return false }
        if bytes.allSatisfy({ $0 == 0 }) { return false }
        if bytes.dropLast().allSatisfy({ $0 == 0 }) && bytes.last == 1 {
            return false
        }
        if bytes[0] == 0xff { return false }
        if bytes[0] == 0xfe && (bytes[1] & 0xc0) == 0x80 { return false }
        if (bytes[0] & 0xfe) == 0xfc { return false }
        if bytes[0] == 0x20 && bytes[1] == 0x01
            && bytes[2] == 0x0d && bytes[3] == 0xb8 {
            return false
        }
        let mappedPrefix = bytes.prefix(10).allSatisfy({ $0 == 0 })
            && bytes[10] == 0xff && bytes[11] == 0xff
        if mappedPrefix {
            return isPublicIPv4(Array(bytes[12..<16]))
        }
        return true
    }
}

private struct ReaderNativeImageHTTPResponse {
    let status: Int
    let headers: [String: String]
    let body: Data
}

private final class ReaderNativeImageProxyTransport: @unchecked Sendable {
    private static let headerLimit = 64 * 1_024
    private static let transferOverheadLimit = 512 * 1_024
    private let maximumBytes: Int
    private let networkQueue = DispatchQueue(
        label: "bw.reader.native-image-proxy"
    )

    init(maximumBytes: Int) {
        self.maximumBytes = maximumBytes
    }

    func start(_ url: URL) async throws -> ReaderNativeImageProxyPayload {
        try await withThrowingTaskGroup(
            of: ReaderNativeImageProxyPayload.self
        ) { group in
            group.addTask { [self] in
                try await fetchRedirectChain(url)
            }
            group.addTask {
                try await Task.sleep(nanoseconds: 15_000_000_000)
                throw ReaderNativeImageProxyError.timedOut
            }
            guard let first = try await group.next() else {
                throw ReaderNativeImageProxyError.transport
            }
            group.cancelAll()
            return first
        }
    }

    private func fetchRedirectChain(
        _ initialURL: URL
    ) async throws -> ReaderNativeImageProxyPayload {
        var current = initialURL
        for redirectCount in 0...5 {
            try Task.checkCancellation()
            // This resolution result is both the policy evidence and the only
            // set of endpoints offered to NWConnection for this hop.
            let target = try ReaderNativeImageProxyPolicy.resolve(current)
            let response = try await request(target)
            if [301, 302, 303, 307, 308].contains(response.status),
               let location = response.headers["location"],
               !location.isEmpty {
                guard redirectCount < 5,
                      let next = URL(string: location, relativeTo: current)?
                        .absoluteURL else {
                    throw ReaderNativeImageProxyError.tooManyRedirects
                }
                current = next
                continue
            }
            guard response.status == 200 else {
                throw ReaderNativeImageProxyError.upstreamStatus(
                    response.status
                )
            }
            let contentType = ReaderNativeImageProxyPolicy
                .normalizedContentType(response.headers["content-type"])
            guard ReaderNativeImageProxyPolicy.supportedContentTypes
                .contains(contentType) else {
                throw ReaderNativeImageProxyError.unsupportedContentType
            }
            let contentEncoding = String(
                response.headers["content-encoding"] ?? ""
            ).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            guard contentEncoding.isEmpty || contentEncoding == "identity" else {
                throw ReaderNativeImageProxyError.transport
            }
            let body = try decodeBody(response)
            guard !body.isEmpty else {
                throw ReaderNativeImageProxyError.empty
            }
            return ReaderNativeImageProxyPayload(
                data: body,
                contentType: contentType
            )
        }
        throw ReaderNativeImageProxyError.tooManyRedirects
    }

    private func request(
        _ target: ReaderNativeImageResolvedHop
    ) async throws -> ReaderNativeImageHTTPResponse {
        var lastError: Error = ReaderNativeImageProxyError.transport
        for endpoint in target.addresses {
            do {
                return try await exchange(target, endpoint: endpoint)
            } catch ReaderNativeImageProxyError.cancelled {
                throw ReaderNativeImageProxyError.cancelled
            } catch ReaderNativeImageProxyError.tooLarge {
                throw ReaderNativeImageProxyError.tooLarge
            } catch {
                lastError = error
            }
        }
        if Task.isCancelled {
            throw ReaderNativeImageProxyError.cancelled
        }
        if let known = lastError as? ReaderNativeImageProxyError {
            throw known
        }
        throw ReaderNativeImageProxyError.transport
    }

    private func exchange(
        _ target: ReaderNativeImageResolvedHop,
        endpoint: NWEndpoint.Host
    ) async throws -> ReaderNativeImageHTTPResponse {
        let tls = NWProtocolTLS.Options()
        target.hostname.withCString { hostname in
            sec_protocol_options_set_tls_server_name(
                tls.securityProtocolOptions,
                hostname
            )
        }
        let parameters = NWParameters(
            tls: tls,
            tcp: NWProtocolTCP.Options()
        )
        guard let port = NWEndpoint.Port(rawValue: target.port) else {
            throw ReaderNativeImageProxyError.invalidURL
        }
        // endpoint is an IPv4Address/IPv6Address created from NI_NUMERICHOST,
        // never the hostname. Network.framework therefore cannot re-resolve it.
        let connection = NWConnection(
            host: endpoint,
            port: port,
            using: parameters
        )
        return try await withTaskCancellationHandler {
            try Task.checkCancellation()
            try await waitUntilReady(connection)
            try await send(makeRequest(target), over: connection)
            let wire = try await receiveAll(from: connection)
            connection.cancel()
            return try parseResponse(wire)
        } onCancel: {
            connection.cancel()
        }
    }

    private func waitUntilReady(_ connection: NWConnection) async throws {
        // ⚠ 连接阶段自带 5 秒上限（2026-08-30「所有卡片图全裂」的修复）。
        //
        // 原来只认 .ready/.failed/.cancelled，而 NWConnection 对"路由不通"
        // 给的是 **.waiting** —— 它会一直等下去、不算失败。于是第一个
        // 连不上的端点（典型：坏 IPv6）把外层 15 秒整段耗光，逐端点回退
        // 一次都轮不到，每张图都以 timeout 收场，且没有任何一处报错。
        //
        // 修法两条腿：.waiting 立即按不可达处理（对顺序回退来说，"现在
        // 连不上"就该马上换下一个地址）；外加 5 秒连接看门狗兜住
        // .preparing 里 SYN 黑洞那种连 .waiting 都不给的情况。
        //
        // 看门狗跑在 networkQueue 自己身上、通过 connection.cancel() 收口：
        // 只有 stateUpdateHandler 一条完成路径，续体不会泄。超时导致的
        // .cancelled 用旗子跟真正的任务取消区分开 —— 混起来的话，一个
        // 端点超时会被当成整个请求被取消，逐端点回退就废了。
        let timedOut = ReaderNativeImageConnectFlag()
        let watchdog = DispatchWorkItem {
            timedOut.set()
            connection.cancel()
        }
        networkQueue.asyncAfter(deadline: .now() + 5, execute: watchdog)
        defer { watchdog.cancel() }
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            var finished = false
            connection.stateUpdateHandler = { state in
                guard !finished else { return }
                switch state {
                case .ready:
                    finished = true
                    continuation.resume()
                case .failed, .waiting:
                    finished = true
                    continuation.resume(
                        throwing: ReaderNativeImageProxyError.transport
                    )
                case .cancelled:
                    finished = true
                    continuation.resume(
                        throwing: timedOut.get()
                            ? ReaderNativeImageProxyError.transport
                            : ReaderNativeImageProxyError.cancelled
                    )
                default:
                    break
                }
            }
            connection.start(queue: networkQueue)
        }
    }

    private func send(_ data: Data, over connection: NWConnection) async throws {
        try await withCheckedThrowingContinuation { continuation in
            connection.send(content: data, completion: .contentProcessed {
                error in
                if error == nil {
                    continuation.resume()
                } else {
                    continuation.resume(
                        throwing: ReaderNativeImageProxyError.transport
                    )
                }
            })
        }
    }

    private func receiveAll(from connection: NWConnection) async throws -> Data {
        var output = Data()
        let wireLimit = maximumBytes + Self.transferOverheadLimit
        while true {
            try Task.checkCancellation()
            let part = try await receive(from: connection)
            if let data = part.data, !data.isEmpty {
                guard output.count <= wireLimit - data.count else {
                    throw ReaderNativeImageProxyError.tooLarge
                }
                output.append(data)
            }
            if part.complete { return output }
        }
    }

    private func receive(
        from connection: NWConnection
    ) async throws -> (data: Data?, complete: Bool) {
        try await withCheckedThrowingContinuation { continuation in
            connection.receive(
                minimumIncompleteLength: 1,
                maximumLength: 64 * 1_024
            ) { data, _, complete, error in
                if error != nil {
                    continuation.resume(
                        throwing: ReaderNativeImageProxyError.transport
                    )
                } else {
                    continuation.resume(returning: (data, complete))
                }
            }
        }
    }

    private func makeRequest(_ target: ReaderNativeImageResolvedHop) -> Data {
        let components = URLComponents(
            url: target.url,
            resolvingAgainstBaseURL: false
        )
        var requestTarget = components?.percentEncodedPath ?? ""
        if requestTarget.isEmpty { requestTarget = "/" }
        if let query = components?.percentEncodedQuery, !query.isEmpty {
            requestTarget += "?" + query
        }
        var hostHeader = target.hostname
        if target.port != 443 { hostHeader += ":\(target.port)" }
        let text = [
            "GET \(requestTarget) HTTP/1.1",
            "Host: \(hostHeader)",
            "User-Agent: Mozilla/5.0 (BW Reader native image proxy)",
            "Accept: image/avif,image/webp,image/png,image/jpeg,image/gif,image/svg+xml;q=0.8,*/*;q=0.1",
            "Accept-Encoding: identity",
            "Connection: close",
            "",
            "",
        ].joined(separator: "\r\n")
        return Data(text.utf8)
    }

    private func parseResponse(_ wire: Data) throws
        -> ReaderNativeImageHTTPResponse {
        let separator = Data([13, 10, 13, 10])
        guard let boundary = wire.range(of: separator),
              boundary.lowerBound <= Self.headerLimit,
              let head = String(
                  data: wire[..<boundary.lowerBound],
                  encoding: .isoLatin1
              ) else {
            throw ReaderNativeImageProxyError.transport
        }
        let lines = head.components(separatedBy: "\r\n")
        guard let statusLine = lines.first else {
            throw ReaderNativeImageProxyError.transport
        }
        let statusParts = statusLine.split(separator: " ", maxSplits: 2)
        guard statusParts.count >= 2,
              statusParts[0].hasPrefix("HTTP/1."),
              let status = Int(statusParts[1]),
              (100...599).contains(status) else {
            throw ReaderNativeImageProxyError.transport
        }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":"),
                  colon != line.startIndex else {
                throw ReaderNativeImageProxyError.transport
            }
            let name = line[..<colon]
                .trimmingCharacters(in: .whitespacesAndNewlines)
                .lowercased()
            let value = line[line.index(after: colon)...]
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !name.isEmpty else {
                throw ReaderNativeImageProxyError.transport
            }
            if let existing = headers[name], !existing.isEmpty {
                headers[name] = existing + "," + value
            } else {
                headers[name] = value
            }
        }
        return ReaderNativeImageHTTPResponse(
            status: status,
            headers: headers,
            body: Data(wire[boundary.upperBound...])
        )
    }

    private func decodeBody(
        _ response: ReaderNativeImageHTTPResponse
    ) throws -> Data {
        let transferEncoding = String(
            response.headers["transfer-encoding"] ?? ""
        ).lowercased()
        if transferEncoding.split(separator: ",").map({
            $0.trimmingCharacters(in: .whitespacesAndNewlines)
        }).last == "chunked" {
            return try decodeChunked(response.body)
        }
        if !transferEncoding.isEmpty {
            throw ReaderNativeImageProxyError.transport
        }
        if let rawLength = response.headers["content-length"] {
            guard let length = Int(rawLength.trimmingCharacters(
                in: .whitespacesAndNewlines
            )), length >= 0 else {
                throw ReaderNativeImageProxyError.transport
            }
            guard length <= maximumBytes else {
                throw ReaderNativeImageProxyError.tooLarge
            }
            guard response.body.count >= length else {
                throw ReaderNativeImageProxyError.transport
            }
            return Data(response.body.prefix(length))
        }
        guard response.body.count <= maximumBytes else {
            throw ReaderNativeImageProxyError.tooLarge
        }
        return response.body
    }

    private func decodeChunked(_ input: Data) throws -> Data {
        let crlf = Data([13, 10])
        var cursor = 0
        var output = Data()
        while true {
            guard let lineRange = input.range(
                of: crlf,
                in: cursor..<input.count
            ), let line = String(
                data: input[cursor..<lineRange.lowerBound],
                encoding: .ascii
            ) else {
                throw ReaderNativeImageProxyError.transport
            }
            let sizeToken = line.split(separator: ";", maxSplits: 1)[0]
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard let size = Int(sizeToken, radix: 16), size >= 0 else {
                throw ReaderNativeImageProxyError.transport
            }
            cursor = lineRange.upperBound
            if size == 0 { return output }
            guard size <= maximumBytes - output.count,
                  cursor <= input.count,
                  size <= input.count - cursor,
                  input.count - cursor - size >= 2 else {
                if size > maximumBytes - output.count {
                    throw ReaderNativeImageProxyError.tooLarge
                }
                throw ReaderNativeImageProxyError.transport
            }
            output.append(input[cursor..<(cursor + size)])
            cursor += size
            guard input[cursor] == 13, input[cursor + 1] == 10 else {
                throw ReaderNativeImageProxyError.transport
            }
            cursor += 2
        }
    }
}

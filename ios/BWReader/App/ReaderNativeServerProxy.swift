import FlyingFox
import FlyingSocks
import Foundation

/// A one-use, loopback-only handoff from the manifest-authorized native
/// gateway to the local HTTP server. The browser sees only the opaque ticket;
/// the upstream URL, request body, cookies and account credentials stay in
/// native memory.
final class ReaderNativeServerProxyBroker: @unchecked Sendable {
    static let routeComponent = "pi-proxy"

    private struct Ticket {
        let request: URLRequest
        let scopeEpoch: UInt64
        let expiresAt: Date
    }

    private let lock = NSLock()
    private var scopeEpoch: UInt64 = 0
    private var tickets: [String: Ticket] = [:]
    private var active: [String: ReaderNativeServerUpstreamTransport] = [:]
    private var resourceRequestPreparer: (@Sendable (
        ReaderNativeServerResourceProxyRequest
    ) async throws -> ReaderNativeServerPreparedProxyRequest)?

    func installResourceRequestPreparer(
        _ preparer: @escaping @Sendable (
            ReaderNativeServerResourceProxyRequest
        ) async throws -> ReaderNativeServerPreparedProxyRequest
    ) {
        lock.lock()
        resourceRequestPreparer = preparer
        lock.unlock()
    }

    /// Invalidates both unused tickets and already-started streams. A request
    /// authorized for one book can therefore never survive a book/binding
    /// transition and be replayed in the next reading context.
    func rotateScope(to newEpoch: UInt64) {
        let transports: [ReaderNativeServerUpstreamTransport]
        lock.lock()
        scopeEpoch = newEpoch
        tickets.removeAll(keepingCapacity: false)
        transports = Array(active.values)
        active.removeAll(keepingCapacity: false)
        lock.unlock()
        transports.forEach { $0.cancel() }
    }

    func cancelAll() {
        let transports: [ReaderNativeServerUpstreamTransport]
        lock.lock()
        tickets.removeAll(keepingCapacity: false)
        resourceRequestPreparer = nil
        transports = Array(active.values)
        active.removeAll(keepingCapacity: false)
        lock.unlock()
        transports.forEach { $0.cancel() }
    }

    /// The sole caller is ReaderNativeServerGateway after manifest authorization,
    /// remote-book identity validation/rewrite and native cookie attachment.
    func issueAuthorizedRequest(
        _ prepared: ReaderNativeServerPreparedProxyRequest
    ) throws -> String {
        let request = prepared.request
        guard let target = request.url,
              target.scheme?.lowercased() == "https",
              target.host?.lowercased() == ReaderNativeServerGateway.serverHost,
              target.port == nil,
              target.path.hasPrefix("/") else {
            throw ReaderNativeServerProxyError.invalidUpstream
        }

        let now = Date()
        lock.lock()
        defer { lock.unlock() }
        guard prepared.scopeEpoch == scopeEpoch else {
            throw ReaderNativeServerProxyError.staleScope
        }
        tickets = tickets.filter { $0.value.expiresAt > now }
        var token = Self.makeTicketToken()
        while tickets[token] != nil {
            token = Self.makeTicketToken()
        }
        tickets[token] = Ticket(
            request: request,
            scopeEpoch: prepared.scopeEpoch,
            expiresAt: now.addingTimeInterval(15)
        )
        return token
    }

    func response(for ticketToken: String) async throws -> HTTPResponse {
        guard Self.isTicketToken(ticketToken) else {
            throw ReaderNativeServerProxyError.invalidTicket
        }

        let ticket: Ticket
        lock.lock()
        if let candidate = tickets.removeValue(forKey: ticketToken),
           candidate.expiresAt > Date(),
           candidate.scopeEpoch == scopeEpoch {
            ticket = candidate
            lock.unlock()
        } else {
            lock.unlock()
            throw ReaderNativeServerProxyError.expiredTicket
        }

        return try await streamedResponse(
            request: ticket.request,
            scopeEpoch: ticket.scopeEpoch,
            activeKey: ticketToken
        )
    }

    func responseForResource(
        _ input: ReaderNativeServerResourceProxyRequest
    ) async throws -> HTTPResponse {
        let preparer: @Sendable (
            ReaderNativeServerResourceProxyRequest
        ) async throws -> ReaderNativeServerPreparedProxyRequest
        lock.lock()
        guard let installed = resourceRequestPreparer else {
            lock.unlock()
            throw ReaderNativeServerProxyError.unavailable
        }
        preparer = installed
        lock.unlock()

        let prepared = try await preparer(input)
        return try await streamedResponse(
            request: prepared.request,
            scopeEpoch: prepared.scopeEpoch,
            activeKey: "resource-\(UUID().uuidString.lowercased())"
        )
    }

    struct DataResponse: Sendable {
        let status: Int
        let data: Data
    }

    /// Native SSE consumers receive bytes directly from URLSession, without
    /// routing them back through the loopback HTTP server and WebKit Fetch.
    func consumeStream(for prepared: ReaderNativeServerPreparedProxyRequest,
                       onResponse: ReaderNativeAssistantStream.Response,
                       onChunk: ReaderNativeAssistantStream.Chunk) async throws {
        guard let url = prepared.request.url, url.scheme == "https",
              url.host == ReaderNativeServerGateway.serverHost, url.port == nil else {
            throw ReaderNativeServerProxyError.invalidUpstream
        }
        let key = "native-stream-" + UUID().uuidString
        let transport = ReaderNativeServerUpstreamTransport(maximumBufferedBytes: 8 * 1024 * 1024) { [weak self] in self?.finish(ticketToken: key) }
        lock.lock()
        guard scopeEpoch == prepared.scopeEpoch else { lock.unlock(); throw ReaderNativeServerProxyError.staleScope }
        active[key] = transport; lock.unlock()
        defer { transport.cancel(); finish(ticketToken: key) }
        try await withTaskCancellationHandler {
            let upstream = try await transport.start(prepared.request)
            guard try await onResponse(upstream.response.statusCode) else { return }
            for try await chunk in upstream.body {
                transport.consumed(chunk.count)
                try Task.checkCancellation()
                lock.lock(); let current = scopeEpoch == prepared.scopeEpoch; lock.unlock()
                guard current else { throw ReaderNativeAssistantStream.Failure("阅读上下文已切换") }
                let keepGoing = try await onChunk(chunk)
                if !keepGoing { return }
            }
        } onCancel: { transport.cancel() }
    }
    /// Native feature controllers consume the already-authorized request
    /// directly. No loopback ticket, browser Fetch or hidden document is used.
    func data(for prepared: ReaderNativeServerPreparedProxyRequest, maximumBytes: Int = 8 * 1024 * 1024) async throws -> DataResponse {
        guard (1...(8 * 1024 * 1024)).contains(maximumBytes), let url = prepared.request.url,
              url.scheme == "https", url.host == ReaderNativeServerGateway.serverHost, url.port == nil else {
            throw ReaderNativeServerProxyError.invalidUpstream
        }
        let key = "native-data-" + UUID().uuidString
        let transport = ReaderNativeServerUpstreamTransport { [weak self] in self?.finish(ticketToken: key) }
        lock.lock()
        guard scopeEpoch == prepared.scopeEpoch else { lock.unlock(); throw ReaderNativeServerProxyError.staleScope }
        active[key] = transport
        lock.unlock()
        defer { transport.cancel(); finish(ticketToken: key) }
        let upstream = try await transport.start(prepared.request)
        var data = Data()
        for try await chunk in upstream.body {
            try Task.checkCancellation()
            guard chunk.count <= maximumBytes - data.count else { throw ReaderNativeServerProxyError.responseTooLarge }
            data.append(chunk)
        }
        lock.lock(); let current = scopeEpoch == prepared.scopeEpoch; lock.unlock()
        guard current else { throw ReaderNativeServerProxyError.staleScope }
        return DataResponse(status: upstream.response.statusCode, data: data)
    }

    private func streamedResponse(
        request: URLRequest,
        scopeEpoch expectedEpoch: UInt64,
        activeKey: String
    ) async throws -> HTTPResponse {
        let transport = ReaderNativeServerUpstreamTransport { [weak self] in
            self?.finish(ticketToken: activeKey)
        }
        lock.lock()
        guard expectedEpoch == scopeEpoch else {
            lock.unlock()
            throw ReaderNativeServerProxyError.staleScope
        }
        active[activeKey] = transport
        lock.unlock()

        do {
            let upstream = try await transport.start(request)
            var headers = HTTPHeaders()
            let forwardedHeaders = [
                "Content-Type", "Cache-Control", "Content-Disposition",
                "ETag", "Last-Modified", "Accept-Ranges", "Content-Range",
            ]
            for name in forwardedHeaders {
                if let value = upstream.response.value(forHTTPHeaderField: name) {
                    headers[HTTPHeader(name)] = value
                }
            }
            if headers[.cacheControl] == nil {
                headers[.cacheControl] = "no-store"
            }
            // Never forward Set-Cookie, Authorization, server identity or
            // Content-Length. URLSession may decode transfer/content encoding,
            // and FlyingFox frames this unknown-length stream as chunked data.
            headers[HTTPHeader("X-Content-Type-Options")] = "nosniff"
            headers[HTTPHeader("Referrer-Policy")] = "no-referrer"
            headers[HTTPHeader("X-BW-Native-Pi-Proxy")] = "stream/1"

            let phrase = HTTPURLResponse.localizedString(
                forStatusCode: upstream.response.statusCode
            ).capitalized
            let status = HTTPStatusCode(
                upstream.response.statusCode,
                phrase: phrase
            )
            let bytes = ReaderNativeServerDataByteSequence(stream: upstream.body)
            return HTTPResponse(
                statusCode: status,
                headers: headers,
                body: HTTPBodySequence(
                    from: bytes,
                    suggestedBufferSize: 16 * 1_024
                )
            )
        } catch {
            finish(ticketToken: activeKey)
            transport.cancel()
            if error is CancellationError {
                throw ReaderNativeServerProxyError.cancelled
            }
            throw error
        }
    }

    private func finish(ticketToken: String) {
        lock.lock()
        active.removeValue(forKey: ticketToken)
        lock.unlock()
    }

    private static func makeTicketToken() -> String {
        UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    }

    static func isTicketToken(_ value: String) -> Bool {
        value.count == 32 && value.allSatisfy {
            $0.isHexDigit && !$0.isUppercase
        }
    }
}

struct ReaderNativeServerPreparedProxyRequest: @unchecked Sendable {
    let request: URLRequest
    let scopeEpoch: UInt64
}

struct ReaderNativeServerResourceProxyRequest: Sendable {
    let requestTarget: String
    let surface: ReaderNativeInterfaceSurface
    let accept: String
    let range: String?
}

private struct ReaderNativeServerUpstream: Sendable {
    let response: HTTPURLResponse
    let body: AsyncThrowingStream<Data, Error>
}

/// URLSession's delegate stream preserves upstream chunk arrival instead of
/// collecting the whole response in `data(for:)`. Completing or abandoning the
/// local HTTP body cancels the native task, so SSE and ordinary binary/JSON
/// responses share the same Fetch-compatible transport.
private final class ReaderNativeServerUpstreamTransport:
    NSObject,
    URLSessionDataDelegate,
    URLSessionTaskDelegate,
    @unchecked Sendable
{
    private let lock = NSLock()
    private let completion: @Sendable () -> Void
    private let bodyStream: AsyncThrowingStream<Data, Error>
    private let bodyContinuation: AsyncThrowingStream<Data, Error>.Continuation
    private var responseContinuation: CheckedContinuation<HTTPURLResponse, Error>?
    private var task: URLSessionDataTask?
    private var finished = false
    private let maximumBufferedBytes: Int?
    private var bufferedBytes = 0

    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.timeoutIntervalForRequest = 120
        configuration.timeoutIntervalForResource = 10 * 60
        return URLSession(
            configuration: configuration,
            delegate: self,
            delegateQueue: nil
        )
    }()

    init(maximumBufferedBytes: Int? = nil, completion: @escaping @Sendable () -> Void) {
        self.completion = completion
        self.maximumBufferedBytes = maximumBufferedBytes
        let pair = AsyncThrowingStream<Data, Error>.makeStream()
        bodyStream = pair.stream
        bodyContinuation = pair.continuation
        super.init()
        bodyContinuation.onTermination = { [weak self] termination in
            guard case .cancelled = termination else { return }
            self?.cancel()
        }
    }

    func start(_ request: URLRequest) async throws -> ReaderNativeServerUpstream {
        let response = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<HTTPURLResponse, Error>) in
                let dataTask: URLSessionDataTask
                lock.lock()
                guard !finished, task == nil else {
                    lock.unlock()
                    continuation.resume(
                        throwing: ReaderNativeServerProxyError.cancelled
                    )
                    return
                }
                responseContinuation = continuation
                dataTask = session.dataTask(with: request)
                task = dataTask
                lock.unlock()
                dataTask.resume()
            }
        } onCancel: {
            self.cancel()
        }
        return ReaderNativeServerUpstream(
            response: response,
            body: bodyStream
        )
    }

    func cancel() {
        let dataTask: URLSessionDataTask?
        lock.lock()
        dataTask = task
        lock.unlock()
        dataTask?.cancel()
        if dataTask == nil {
            finish(with: ReaderNativeServerProxyError.cancelled)
        }
    }

    func consumed(_ count: Int) {
        lock.lock(); bufferedBytes = max(0, bufferedBytes - count); lock.unlock()
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        guard let response = response as? HTTPURLResponse else {
            completionHandler(.cancel)
            finish(with: ReaderNativeServerProxyError.missingHTTPResponse)
            return
        }
        let continuation: CheckedContinuation<HTTPURLResponse, Error>?
        lock.lock()
        continuation = responseContinuation
        responseContinuation = nil
        lock.unlock()
        continuation?.resume(returning: response)
        completionHandler(.allow)
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive data: Data
    ) {
        guard !data.isEmpty else { return }
        lock.lock()
        let mayYield = !finished
        let overflow = maximumBufferedBytes.map { data.count > $0 - bufferedBytes } ?? false
        if mayYield && !overflow && maximumBufferedBytes != nil { bufferedBytes += data.count }
        lock.unlock()
        if mayYield && overflow {
            // Fail closed instead of buffering an unbounded response while a
            // suspended consumer cannot acknowledge incoming actions.
            finish(with: ReaderNativeAssistantStream.Failure("对话接收缓存超过上限，请恢复后继续"))
            dataTask.cancel()
            return
        }
        if mayYield {
            bodyContinuation.yield(data)
        }
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?
    ) {
        finish(with: error)
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        // The manifest authorized one exact Pi route. Redirects must not turn
        // that decision into permission for a different endpoint or origin.
        completionHandler(nil)
    }

    private func finish(with error: Error?) {
        let continuation: CheckedContinuation<HTTPURLResponse, Error>?
        lock.lock()
        guard !finished else {
            lock.unlock()
            return
        }
        finished = true
        continuation = responseContinuation
        responseContinuation = nil
        task = nil
        lock.unlock()

        if let error {
            continuation?.resume(throwing: error)
            bodyContinuation.finish(throwing: error)
        } else if let continuation {
            let missing = ReaderNativeServerProxyError.missingHTTPResponse
            continuation.resume(throwing: missing)
            bodyContinuation.finish(throwing: missing)
        } else {
            bodyContinuation.finish()
        }
        completion()
        session.finishTasksAndInvalidate()
    }
}

private struct ReaderNativeServerDataByteSequence:
    AsyncBufferedSequence,
    Sendable
{
    typealias Element = UInt8

    let stream: AsyncThrowingStream<Data, Error>

    func makeAsyncIterator() -> Iterator {
        Iterator(iterator: stream.makeAsyncIterator())
    }

    struct Iterator: AsyncBufferedIteratorProtocol {
        typealias Element = UInt8
        typealias Buffer = Data

        var iterator: AsyncThrowingStream<Data, Error>.Iterator
        var pending: Data?
        var pendingOffset = 0

        mutating func next() async throws -> UInt8? {
            try await nextBuffer(suggested: 1)?.first
        }

        mutating func nextBuffer(suggested count: Int) async throws -> Data? {
            let desired = Swift.max(1, count)
            while pending == nil || pendingOffset >= pending!.count {
                pending = try await iterator.next()
                pendingOffset = 0
                guard let pending else { return nil }
                if pending.isEmpty { continue }
            }
            guard let pending else { return nil }
            let end = Swift.min(pending.count, pendingOffset + desired)
            let result = Data(pending[pendingOffset..<end])
            pendingOffset = end
            return result
        }
    }
}

enum ReaderNativeServerProxyError: LocalizedError {
    case responseTooLarge
    case invalidUpstream
    case staleScope
    case invalidTicket
    case expiredTicket
    case missingHTTPResponse
    case cancelled
    case unavailable

    var errorDescription: String? {
        switch self {
        case .responseTooLarge: return "服务器响应超过原生读取上限"
        case .invalidUpstream:
            return "BW_PI_PROXY_ROUTE：服务器流式请求地址无效"
        case .staleScope:
            return "BW_PI_PROXY_SCOPE：阅读书籍已经切换，请重试"
        case .invalidTicket:
            return "BW_PI_PROXY_TICKET：服务器流式票据无效"
        case .expiredTicket:
            return "BW_PI_PROXY_TICKET：服务器流式票据已使用或过期"
        case .missingHTTPResponse:
            return "BW_PI_PROXY_RESPONSE：服务器没有返回 HTTP 响应"
        case .cancelled:
            return "BW_PI_PROXY_CANCELLED：服务器流式请求已取消"
        case .unavailable:
            return "BW_PI_PROXY_UNAVAILABLE：服务器流式网关尚未准备好"
        }
    }
}

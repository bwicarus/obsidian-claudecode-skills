import FlyingFox
import FlyingSocks
import Foundation

/// A one-use, loopback-only handoff from the manifest-authorized native
/// gateway to the local HTTP server. The browser sees only the opaque ticket;
/// the upstream URL, request body, cookies and account credentials stay in
/// native memory.
final class ReaderNativePiProxyBroker: @unchecked Sendable {
    static let routeComponent = "pi-proxy"

    private struct Ticket {
        let request: URLRequest
        let scopeEpoch: UInt64
        let expiresAt: Date
    }

    private let lock = NSLock()
    private var scopeEpoch: UInt64 = 0
    private var tickets: [String: Ticket] = [:]
    private var active: [String: ReaderNativePiUpstreamTransport] = [:]
    private var resourceRequestPreparer: (@Sendable (
        ReaderNativePiResourceProxyRequest
    ) async throws -> ReaderNativePiPreparedProxyRequest)?

    func installResourceRequestPreparer(
        _ preparer: @escaping @Sendable (
            ReaderNativePiResourceProxyRequest
        ) async throws -> ReaderNativePiPreparedProxyRequest
    ) {
        lock.lock()
        resourceRequestPreparer = preparer
        lock.unlock()
    }

    /// Invalidates both unused tickets and already-started streams. A request
    /// authorized for one book can therefore never survive a book/binding
    /// transition and be replayed in the next reading context.
    func rotateScope(to newEpoch: UInt64) {
        let transports: [ReaderNativePiUpstreamTransport]
        lock.lock()
        scopeEpoch = newEpoch
        tickets.removeAll(keepingCapacity: false)
        transports = Array(active.values)
        active.removeAll(keepingCapacity: false)
        lock.unlock()
        transports.forEach { $0.cancel() }
    }

    func cancelAll() {
        let transports: [ReaderNativePiUpstreamTransport]
        lock.lock()
        tickets.removeAll(keepingCapacity: false)
        resourceRequestPreparer = nil
        transports = Array(active.values)
        active.removeAll(keepingCapacity: false)
        lock.unlock()
        transports.forEach { $0.cancel() }
    }

    /// The sole caller is ReaderNativePiGateway after manifest authorization,
    /// remote-book identity validation/rewrite and native cookie attachment.
    func issueAuthorizedRequest(
        _ prepared: ReaderNativePiPreparedProxyRequest
    ) throws -> String {
        let request = prepared.request
        guard let target = request.url,
              target.scheme?.lowercased() == "https",
              target.host?.lowercased() == ReaderNativePiGateway.piHost,
              target.port == nil,
              target.path.hasPrefix("/") else {
            throw ReaderNativePiProxyError.invalidUpstream
        }

        let now = Date()
        lock.lock()
        defer { lock.unlock() }
        guard prepared.scopeEpoch == scopeEpoch else {
            throw ReaderNativePiProxyError.staleScope
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
            throw ReaderNativePiProxyError.invalidTicket
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
            throw ReaderNativePiProxyError.expiredTicket
        }

        return try await streamedResponse(
            request: ticket.request,
            scopeEpoch: ticket.scopeEpoch,
            activeKey: ticketToken
        )
    }

    func responseForResource(
        _ input: ReaderNativePiResourceProxyRequest
    ) async throws -> HTTPResponse {
        let preparer: @Sendable (
            ReaderNativePiResourceProxyRequest
        ) async throws -> ReaderNativePiPreparedProxyRequest
        lock.lock()
        guard let installed = resourceRequestPreparer else {
            lock.unlock()
            throw ReaderNativePiProxyError.unavailable
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

    private func streamedResponse(
        request: URLRequest,
        scopeEpoch expectedEpoch: UInt64,
        activeKey: String
    ) async throws -> HTTPResponse {
        let transport = ReaderNativePiUpstreamTransport { [weak self] in
            self?.finish(ticketToken: activeKey)
        }
        lock.lock()
        guard expectedEpoch == scopeEpoch else {
            lock.unlock()
            throw ReaderNativePiProxyError.staleScope
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
            let bytes = ReaderNativePiDataByteSequence(stream: upstream.body)
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
                throw ReaderNativePiProxyError.cancelled
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

struct ReaderNativePiPreparedProxyRequest: @unchecked Sendable {
    let request: URLRequest
    let scopeEpoch: UInt64
}

struct ReaderNativePiResourceProxyRequest: Sendable {
    let requestTarget: String
    let surface: ReaderNativeInterfaceSurface
    let accept: String
    let range: String?
}

private struct ReaderNativePiUpstream: Sendable {
    let response: HTTPURLResponse
    let body: AsyncThrowingStream<Data, Error>
}

/// URLSession's delegate stream preserves upstream chunk arrival instead of
/// collecting the whole response in `data(for:)`. Completing or abandoning the
/// local HTTP body cancels the native task, so SSE and ordinary binary/JSON
/// responses share the same Fetch-compatible transport.
private final class ReaderNativePiUpstreamTransport:
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

    init(completion: @escaping @Sendable () -> Void) {
        self.completion = completion
        let pair = AsyncThrowingStream<Data, Error>.makeStream()
        bodyStream = pair.stream
        bodyContinuation = pair.continuation
        super.init()
        bodyContinuation.onTermination = { [weak self] termination in
            guard case .cancelled = termination else { return }
            self?.cancel()
        }
    }

    func start(_ request: URLRequest) async throws -> ReaderNativePiUpstream {
        let response = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<HTTPURLResponse, Error>) in
                let dataTask: URLSessionDataTask
                lock.lock()
                guard !finished, task == nil else {
                    lock.unlock()
                    continuation.resume(
                        throwing: ReaderNativePiProxyError.cancelled
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
        return ReaderNativePiUpstream(
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
            finish(with: ReaderNativePiProxyError.cancelled)
        }
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        guard let response = response as? HTTPURLResponse else {
            completionHandler(.cancel)
            finish(with: ReaderNativePiProxyError.missingHTTPResponse)
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
        lock.unlock()
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
            let missing = ReaderNativePiProxyError.missingHTTPResponse
            continuation.resume(throwing: missing)
            bodyContinuation.finish(throwing: missing)
        } else {
            bodyContinuation.finish()
        }
        completion()
        session.finishTasksAndInvalidate()
    }
}

private struct ReaderNativePiDataByteSequence:
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
            let desired = max(1, count)
            while pending == nil || pendingOffset >= pending!.count {
                pending = try await iterator.next()
                pendingOffset = 0
                guard let pending else { return nil }
                if pending.isEmpty { continue }
            }
            guard let pending else { return nil }
            let end = min(pending.count, pendingOffset + desired)
            let result = Data(pending[pendingOffset..<end])
            pendingOffset = end
            return result
        }
    }
}

enum ReaderNativePiProxyError: LocalizedError {
    case invalidUpstream
    case staleScope
    case invalidTicket
    case expiredTicket
    case missingHTTPResponse
    case cancelled
    case unavailable

    var errorDescription: String? {
        switch self {
        case .invalidUpstream:
            return "BW_PI_PROXY_ROUTE：Pi 流式请求地址无效"
        case .staleScope:
            return "BW_PI_PROXY_SCOPE：阅读书籍已经切换，请重试"
        case .invalidTicket:
            return "BW_PI_PROXY_TICKET：Pi 流式票据无效"
        case .expiredTicket:
            return "BW_PI_PROXY_TICKET：Pi 流式票据已使用或过期"
        case .missingHTTPResponse:
            return "BW_PI_PROXY_RESPONSE：Pi 没有返回 HTTP 响应"
        case .cancelled:
            return "BW_PI_PROXY_CANCELLED：Pi 流式请求已取消"
        case .unavailable:
            return "BW_PI_PROXY_UNAVAILABLE：Pi 流式网关尚未准备好"
        }
    }
}

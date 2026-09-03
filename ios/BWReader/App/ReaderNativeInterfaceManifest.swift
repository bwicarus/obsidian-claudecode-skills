import Foundation

enum ReaderNativeInterfaceSurface: String, Decodable, Sendable {
    case pdf
    case epub
}

enum ReaderNativeRemoteBookMode: String, Decodable, Sendable {
    case required
    case conditional
}

enum ReaderNativeRemoteBookScope: String, Decodable, Sendable {
    case current
    case catalog
}

enum ReaderNativeRemoteBookIdentityLocation: String, Decodable, Sendable {
    case query
    case json
}

enum ReaderNativeRemoteBookIdentityTransform: String, Decodable, Sendable {
    case exact
    case prefixBeforeDelimiter = "prefix-before-delimiter"
}

struct ReaderNativeRemoteBookIdentity: Decodable, Sendable {
    let methods: [String]
    let location: ReaderNativeRemoteBookIdentityLocation
    let pointer: String
    let transform: ReaderNativeRemoteBookIdentityTransform
}

struct ReaderNativeRemoteBookContinuation: Decodable, Sendable {
    enum Kind: String, Decodable, Sendable {
        case rid
    }

    let kind: Kind
    let pointer: String
    let fromPointer: String
}

struct ReaderNativeRemoteBookPolicy: Decodable, Sendable {
    let mode: ReaderNativeRemoteBookMode
    let scope: ReaderNativeRemoteBookScope
    let requiredMethods: [String]
    let identities: [ReaderNativeRemoteBookIdentity]
    let continuation: ReaderNativeRemoteBookContinuation?
}

struct ReaderNativePiRoutePolicy: Sendable {
    let path: String
    let methods: [String]
    let remoteBook: ReaderNativeRemoteBookPolicy?
}

/// The single, packaged authority for every API used by the native Reader
/// shell. The build script audits the real PDF/EPUB script closure against
/// this file; the App reads the same signed resource before forwarding a
/// request to Pi so JavaScript and Swift cannot drift into separate allowlists.
struct ReaderNativeInterfaceManifest: Decodable {
    static let contract = "reader-native-interface-manifest/2"
    static let resourceName = "native_reader_interface_manifest.json"

    private enum Match: String, Decodable {
        case exact
        case segment
    }

    private enum Owner: String, Decodable {
        case local
        case pi
        case native
    }

    private enum Status: String, Decodable {
        case supported
        case degraded
        case pending
    }

    private struct Route: Decodable {
        let path: String
        let match: Match
        let owner: Owner
        let methods: [String]
        let surfaces: [ReaderNativeInterfaceSurface]
        let status: Status
        let remoteBook: ReaderNativeRemoteBookPolicy?
        let description: String
    }

    private let manifestContract: String
    private let routes: [Route]

    private enum CodingKeys: String, CodingKey {
        case manifestContract = "contract"
        case routes
    }

    init(bundle: Bundle = .main) throws {
        guard let resourceRoot = bundle.resourceURL else {
            throw ManifestError("原生 Reader 接口清单资源目录不可用")
        }
        let url = resourceRoot
            .appendingPathComponent("ReaderBundle", isDirectory: true)
            .appendingPathComponent(Self.resourceName, isDirectory: false)
        let data = try Data(contentsOf: url, options: .mappedIfSafe)
        let decoded = try JSONDecoder().decode(Self.self, from: data)
        try decoded.validate()
        self = decoded
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        manifestContract = try values.decode(
            String.self,
            forKey: .manifestContract
        )
        routes = try values.decode([Route].self, forKey: .routes)
    }

    func piRoutePolicy(
        path: String,
        method: String,
        surface: ReaderNativeInterfaceSurface
    ) -> ReaderNativePiRoutePolicy? {
        guard let route = matchingRoute(path: path),
              route.owner == .pi,
              route.status == .supported,
              route.methods.contains(method),
              route.surfaces.contains(surface) else {
            return nil
        }
        return ReaderNativePiRoutePolicy(
            path: route.path,
            methods: route.methods,
            remoteBook: route.remoteBook
        )
    }

    private func matchingRoute(path: String) -> Route? {
        routes.first { route in
            switch route.match {
            case .exact:
                return route.path == path
            case .segment:
                return path.hasPrefix(route.path)
                    && path.count > route.path.count
            }
        }
    }

    private func validate() throws {
        guard manifestContract == Self.contract, !routes.isEmpty else {
            throw ManifestError("原生 Reader 接口清单合同无效")
        }
        let allowedMethods = Set(["GET", "POST", "PUT", "PATCH", "DELETE"])
        var identities = Set<String>()
        for route in routes {
            let identity = "\(route.match.rawValue):\(route.path)"
            guard identities.insert(identity).inserted,
                  Self.isCanonicalPath(route.path),
                  route.path.hasPrefix("/pdf/api/")
                    || route.path.hasPrefix("/api/assistant/"),
                  route.path != "/pdf/api/",
                  route.path != "/api/assistant/",
                  !route.methods.isEmpty,
                  Set(route.methods).count == route.methods.count,
                  route.methods.allSatisfy(allowedMethods.contains),
                  !route.surfaces.isEmpty,
                  Set(route.surfaces.map(\.rawValue)).count
                    == route.surfaces.count,
                  !route.description.trimmingCharacters(
                    in: .whitespacesAndNewlines
                  ).isEmpty else {
                throw ManifestError("原生 Reader 接口清单路由无效：\(identity)")
            }
            if route.match == .segment, !route.path.hasSuffix("/") {
                throw ManifestError("原生 Reader 段路由必须以斜线结束：\(route.path)")
            }
            if let remoteBook = route.remoteBook {
                guard route.owner == .pi else {
                    throw ManifestError("非服务器路由不得声明远端书策略：\(route.path)")
                }
                try Self.validate(
                    remoteBook,
                    routeMethods: route.methods,
                    path: route.path
                )
            }
        }
    }

    private static func validate(
        _ policy: ReaderNativeRemoteBookPolicy,
        routeMethods: [String],
        path: String
    ) throws {
        let routeMethodSet = Set(routeMethods)
        let requiredMethodSet = Set(policy.requiredMethods)
        guard requiredMethodSet.count == policy.requiredMethods.count,
              requiredMethodSet.isSubset(of: routeMethodSet),
              !policy.identities.isEmpty || policy.continuation != nil else {
            throw ManifestError("远端书策略无效：\(path)")
        }
        if policy.mode == .required,
           requiredMethodSet != routeMethodSet {
            throw ManifestError("required 远端书策略必须覆盖全部方法：\(path)")
        }

        var identityKeys = Set<String>()
        for identity in policy.identities {
            let methodSet = Set(identity.methods)
            let key = [
                identity.location.rawValue,
                identity.pointer,
                identity.transform.rawValue,
                identity.methods.joined(separator: ","),
            ].joined(separator: ":")
            guard !identity.methods.isEmpty,
                  methodSet.count == identity.methods.count,
                  methodSet.isSubset(of: routeMethodSet),
                  isJSONPointer(identity.pointer),
                  identityKeys.insert(key).inserted else {
                throw ManifestError("远端书身份规则无效：\(path)")
            }
            if identity.location == .query,
               identity.pointer.dropFirst().contains("/") {
                throw ManifestError("查询身份只能指向一个字段：\(path)")
            }
        }
        if let continuation = policy.continuation {
            guard continuation.kind == .rid,
                  isJSONPointer(continuation.pointer),
                  isJSONPointer(continuation.fromPointer),
                  continuation.pointer != continuation.fromPointer else {
                throw ManifestError("远端书续传策略无效：\(path)")
            }
        }
    }

    private static func isJSONPointer(_ value: String) -> Bool {
        guard value.utf8.count <= 256,
              value.hasPrefix("/"), value.count > 1 else { return false }
        return value.dropFirst().split(
            separator: "/",
            omittingEmptySubsequences: false
        ).allSatisfy { segment in
            !segment.isEmpty
                && !segment.contains("~")
                && !segment.unicodeScalars.contains(where: {
                    $0.value < 0x20 || $0.value == 0x7f
                })
        }
    }

    private static func isCanonicalPath(_ value: String) -> Bool {
        guard value.hasPrefix("/"), !value.hasPrefix("//"),
              !value.contains("\\"), !value.contains("?"),
              !value.contains("#"),
              !value.unicodeScalars.contains(where: {
                  $0.value < 0x20 || $0.value == 0x7f
              }) else {
            return false
        }
        let parts = value.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.first?.isEmpty == true, parts.count > 1 else { return false }
        return parts.dropFirst().dropLast(value.hasSuffix("/") ? 1 : 0)
            .allSatisfy { !$0.isEmpty && $0 != "." && $0 != ".." }
    }

    private struct ManifestError: LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }
}

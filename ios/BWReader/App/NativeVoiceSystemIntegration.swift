import Foundation
import MediaPlayer
import Network

let nativeAppBuildVersion = "1.0.3.1"

struct NativeVoiceDiagnosticEntry: Identifiable, Equatable {
    let id = UUID()
    let timestamp: Date
    let category: String
    let message: String

    init(
        timestamp: Date = Date(),
        category: String,
        message: String
    ) {
        self.timestamp = timestamp
        self.category = category
        self.message = message
    }
}

struct NativeVoiceNetworkPath: Equatable, Sendable {
    let available: Bool
    let interfaces: [String]
    let expensive: Bool
    let constrained: Bool

    init(path: NWPath) {
        available = path.status == .satisfied
        var names: [String] = []
        let candidates: [(NWInterface.InterfaceType, String)] = [
            (.wifi, "wifi"),
            (.cellular, "cellular"),
            (.wiredEthernet, "ethernet"),
            (.loopback, "loopback"),
            (.other, "other"),
        ]
        for (type, name) in candidates where path.usesInterfaceType(type) {
            names.append(name)
        }
        interfaces = names.sorted()
        expensive = path.isExpensive
        constrained = path.isConstrained
    }

    var summary: String {
        let route = interfaces.isEmpty ? "unknown" : interfaces.joined(separator: "+")
        return "\(available ? "online" : "offline")/\(route)"
            + (expensive ? "/expensive" : "")
            + (constrained ? "/constrained" : "")
    }
}

final class NativeVoicePathMonitor {
    var onUpdate: ((NativeVoiceNetworkPath) -> Void)?

    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(
        label: "space.bwicarus.reader.native-voice-network",
        qos: .utility
    )
    private var started = false

    func start() {
        guard !started else { return }
        started = true
        monitor.pathUpdateHandler = { [weak self] path in
            self?.onUpdate?(NativeVoiceNetworkPath(path: path))
        }
        monitor.start(queue: queue)
    }

    deinit {
        monitor.cancel()
    }
}

final class NativeVoiceRemoteControls {
    var onStop: (() -> Void)?
    var onSetMuted: ((Bool) -> Void)?
    var onToggleMuted: (() -> Void)?

    private let center = MPRemoteCommandCenter.shared()
    private var targets: [(MPRemoteCommand, Any)] = []
    private var enabled = false

    init() {
        targets.append((center.stopCommand, center.stopCommand.addTarget {
            [weak self] _ in
            guard let self, self.enabled else {
                return .commandFailed
            }
            self.onStop?()
            return .success
        }))
        targets.append((center.pauseCommand, center.pauseCommand.addTarget {
            [weak self] _ in
            guard let self, self.enabled else {
                return .commandFailed
            }
            self.onSetMuted?(true)
            return .success
        }))
        targets.append((center.playCommand, center.playCommand.addTarget {
            [weak self] _ in
            guard let self, self.enabled else {
                return .commandFailed
            }
            self.onSetMuted?(false)
            return .success
        }))
        targets.append((
            center.togglePlayPauseCommand,
            center.togglePlayPauseCommand.addTarget { [weak self] _ in
                guard let self, self.enabled else {
                    return .commandFailed
                }
                self.onToggleMuted?()
                return .success
            }
        ))
        setEnabled(false)
    }

    deinit {
        for (command, target) in targets {
            command.removeTarget(target)
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
    }

    func update(
        enabled: Bool,
        muted: Bool,
        targetName: String,
        status: String
    ) {
        setEnabled(enabled)
        let infoCenter = MPNowPlayingInfoCenter.default()
        guard enabled else {
            infoCenter.nowPlayingInfo = nil
            return
        }

        infoCenter.nowPlayingInfo = [
            MPMediaItemPropertyTitle: "BW Reader 电脑语音",
            MPMediaItemPropertyArtist: "\(targetName) · \(status)",
            MPNowPlayingInfoPropertyIsLiveStream: true,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: 0,
            MPNowPlayingInfoPropertyPlaybackRate: muted ? 0.0 : 1.0,
        ]
    }

    private func setEnabled(_ value: Bool) {
        enabled = value
        center.stopCommand.isEnabled = value
        center.pauseCommand.isEnabled = value
        center.playCommand.isEnabled = value
        center.togglePlayPauseCommand.isEnabled = value

        center.nextTrackCommand.isEnabled = false
        center.previousTrackCommand.isEnabled = false
        center.skipForwardCommand.isEnabled = false
        center.skipBackwardCommand.isEnabled = false
        center.seekForwardCommand.isEnabled = false
        center.seekBackwardCommand.isEnabled = false
        center.changePlaybackPositionCommand.isEnabled = false
    }
}

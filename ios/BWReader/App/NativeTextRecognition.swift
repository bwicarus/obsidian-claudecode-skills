import UIKit
import VisionKit

enum NativeTextRecognitionError: LocalizedError {
    case unsupported
    case noText

    var errorDescription: String? {
        switch self {
        case .unsupported:
            return "这台设备不支持系统实况文本识别"
        case .noText:
            return "当前视口没有识别到文字"
        }
    }
}

@MainActor
struct NativeReaderTextRecognizer {
    func recognize(_ image: UIImage) async throws -> String {
        guard ImageAnalyzer.isSupported else {
            throw NativeTextRecognitionError.unsupported
        }
        let analyzer = ImageAnalyzer()
        let configuration = ImageAnalyzer.Configuration([.text])
        let analysis = try await analyzer.analyze(
            image,
            configuration: configuration
        )
        let text = analysis.transcript.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        guard !text.isEmpty else {
            throw NativeTextRecognitionError.noText
        }
        return String(text.prefix(32_000))
    }
}

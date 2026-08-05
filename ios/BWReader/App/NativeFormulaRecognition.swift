import Foundation
import WebKit

struct NativeFormulaRecognitionStatus: Equatable {
    let total: Int
    let completed: Int
    let remaining: Int
    let running: Bool
    let detectingBoxes: Bool

    var summary: String {
        if detectingBoxes { return "正在用现有版面模型检测公式框" }
        if total == 0 { return "尚未检测到公式框" }
        if remaining == 0 { return "已完成 \(completed)/\(total)" }
        if running { return "后台识别中 \(completed)/\(total)" }
        return "待识别 \(remaining) 个公式"
    }
}

enum NativeFormulaRecognitionError: LocalizedError {
    case pdfRequired
    case invalidResponse
    case server(String)

    var errorDescription: String? {
        switch self {
        case .pdfRequired:
            return "公式批处理目前只支持 Reader 中的 PDF"
        case .invalidResponse:
            return "公式识别服务返回了无效结果"
        case .server(let message):
            return message
        }
    }
}

@MainActor
extension ReaderWebViewModel {
    func startNativeFormulaRecognition(
        file: String
    ) async throws -> NativeFormulaRecognitionStatus {
        guard file.lowercased().hasSuffix(".pdf") else {
            throw NativeFormulaRecognitionError.pdfRequired
        }
        let value = try await callReaderJSON(
            """
            const response = await fetch('/pdf/api/formula-ocr', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({file})
            });
            return JSON.stringify(await response.json());
            """,
            arguments: ["file": file]
        )
        guard value["ok"] as? Bool == true else {
            if value["error"] as? String == "no_boxes" {
                let detection = try await callReaderJSON(
                    """
                    const response = await fetch('/pdf/api/book-figures', {
                      method: 'POST',
                      headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify({file, enabled: true})
                    });
                    return JSON.stringify(await response.json());
                    """,
                    arguments: ["file": file]
                )
                guard detection["ok"] as? Bool == true else {
                    throw NativeFormulaRecognitionError.server(
                        (detection["error"] as? String)
                            ?? "公式框检测启动失败"
                    )
                }
                return NativeFormulaRecognitionStatus(
                    total: 0,
                    completed: 0,
                    remaining: 0,
                    running: true,
                    detectingBoxes: true
                )
            }
            let message = (value["msg"] as? String)
                ?? (value["error"] as? String)
                ?? "公式识别启动失败"
            throw NativeFormulaRecognitionError.server(message)
        }
        return Self.formulaStatus(from: value)
    }

    private static func formulaStatus(
        from value: [String: Any]
    ) -> NativeFormulaRecognitionStatus {
        let total = value["total"] as? Int ?? 0
        let completed = value["have"] as? Int ?? 0
        let remaining = value["remaining"] as? Int
            ?? max(0, total - completed)
        return NativeFormulaRecognitionStatus(
            total: total,
            completed: completed,
            remaining: remaining,
            running: value["running"] as? Bool
                ?? value["started"] as? Bool
                ?? value["already_running"] as? Bool
                ?? false,
            detectingBoxes: false
        )
    }

    private func callReaderJSON(
        _ body: String,
        arguments: [String: Any]
    ) async throws -> [String: Any] {
        let raw: Any = try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Any, Error>) in
            webView.callAsyncJavaScript(
                body,
                arguments: arguments,
                in: nil,
                contentWorld: .page
            ) { result in
                continuation.resume(with: result)
            }
        }
        guard
            let json = raw as? String,
            let data = json.data(using: .utf8),
            let value = try JSONSerialization.jsonObject(with: data)
                as? [String: Any]
        else {
            throw NativeFormulaRecognitionError.invalidResponse
        }
        return value
    }
}

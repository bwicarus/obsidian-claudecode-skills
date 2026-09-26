import Foundation
import CoreFoundation
import CryptoKit

/// 迁出 P4 之后只剩这一个工具：对话 / 卡片内容的稳定指纹（本机缓存去重、媒体替换核对）。
/// 原先的「网页消息增量」应用逻辑（apply / applyEvents）随网页停止投影消息一起删除（2026-09-27）。
enum ReaderNativeConversationStore {
    static func fingerprint(_ messages:[[String:Any]]) -> String? {
        guard JSONSerialization.isValidJSONObject(messages),
              let data = try? JSONSerialization.data(withJSONObject:messages,options:[.sortedKeys,.withoutEscapingSlashes]) else { return nil }
        return SHA256.hash(data:data).map { String(format:"%02x",$0) }.joined()
    }
}

import SwiftUI

/// 故障现场，**纯原生**。
///
/// ⚠ 它唯一的设计约束：**不碰阅读页**。
/// 2026-09-22 我第一版把诊断放进「阅读设置」，而那个面板要先从网页层读本书设置 ——
/// 页面一死它就卡在"阅读页尚未准备好，请稍后重试"，于是**最需要诊断的时候恰恰
/// 看不到诊断**（用户原话：「诊断都加载不出来就崩溃了好么怎么看」）。
/// 项目里那条规矩写得很清楚：诊断通道不能穿过被测对象。
///
/// 所以这里只读内存里的面包屑和发件箱，没有任何 await、没有任何网页调用 ——
/// 阅读页整个死掉它照样打得开。
@MainActor
struct ReaderNativeFaultLogView: View {
    @ObservedObject var reporter: ReaderNativeFaultReporter
    @ObservedObject private var profile = ReaderNativeStartupProfile.shared
    @Environment(\.dismiss) private var dismiss
    @State private var copied = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(reporter.statusLine)
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(reporter.pendingCount > 0 ? .orange : .secondary)
                    // 启动基线。⚠ 放在这一屏而不是"阅读设置"里，理由同上：
                    //   最需要看数字的时候（页面卡死/内存吃紧），阅读设置恰恰打不开。
                    Text("启动与内存")
                        .font(.footnote.weight(.semibold))
                    Text(profile.readable)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text("当前占用 \(ReaderNativeStartupProfile.footprintMB())MB")
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Divider()
                    Text(reporter.readableReport)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(16)
            }
            .background(ReaderNativeTheme.canvas)
            .navigationTitle("故障现场")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(copied ? "已复制" : "复制") {
                        UIPasteboard.general.string = profile.readable
                            + "  当前占用 \(ReaderNativeStartupProfile.footprintMB())MB  "
                            + reporter.readableReport
                        copied = true
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("完成") { dismiss() }
                }
            }
        }
    }
}

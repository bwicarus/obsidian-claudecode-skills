import Foundation
import SwiftUI

/// 「我的数据在哪」——一屏说清楚，而不是散在五个按钮里。
///
/// ## 为什么要有这个
///
/// 2026-08-28 用户的原话：
///
/// > 「目前的这一系列实在是太混乱了，应该有统一的登录……怎么会有现在这么多
/// > 没用的按钮和逻辑？」
///
/// 他是对的，而且原因不是设计，是**堆积**：Pi 书库同步、本机书籍文件夹、
/// 登录 Pi、手表 token 配给、复制账本 —— 每加一个能力就带来一套自己的入口，
/// 每一套单独看都有理由，合起来就是五个按钮和三种失败方式。
///
/// ## ⚠ 这一屏最重要的东西不是按钮，是**「哪些没同步」**
///
/// 同一天用户还说过「既然所有的操作在 Windows 上又有一份，那只需要登录后
/// 下载同步就好了」。查下来是**部分成立**，而"部分"正是危险所在：
///
/// - 实测账本(2026-08-28)：高亮 48 / 便签 34 / 插入页 3 / 墨迹 1，最早 8 月 24 日
/// - **阅读位置**当天补进复制
/// - **聊天记录在 Pi 上**（`where_does_this_route_run.py /api/assistant/history`
///   → owner=pi、无本地分支），重装不丢
/// - **书本身不在**：两边各是各的，不会自动互相补齐
///
/// 按"什么都有"这个前提行事的后果是丢数据（清掉设备以为能拉回来）。
/// 所以这一屏必须把**没覆盖的**列得跟覆盖的一样清楚 ——
/// 一个只显示"已同步 N 项"的界面，会让人以为剩下的也同步了。
///
/// ## 架构定位（CLAUDE.md 的原话，写在这里免得界面又把它搞反）
///
/// - **数据权威在 App 本地**
/// - **Windows 是云端存储**（用户 2026-08-28 拍板：那边的 AI 要看同一批文件）
/// - **Pi 只是中继**，不是数据权威，所以不该在界面上以「登录 Pi」「Pi 书库」
///   这种一等公民的形式出现 —— 它是实现细节
enum ReaderDataHub {

    /// 一类数据的现状。
    struct Category: Identifiable {
        let name: String
        /// 本地有多少（能数就数，数不了就 nil —— **不编**）。
        let localCount: Int?
        /// 有没有被复制到 Windows。
        let replicated: Bool
        /// 没被复制时，说清丢了会怎样。
        let caveat: String?

        var id: String { name }

        var statusText: String {
            let local = localCount.map { "本机 \($0)" } ?? "本机（数不出）"
            return replicated ? local + " · 已备份到电脑" : local + " · **只在本机**"
        }
    }

    /// ⚠ 这张表是**手写的**，因为它要说的是「设计上覆盖了什么」，
    /// 而不是「此刻账本里恰好有什么」。后者会随使用波动，
    /// 而人要判断的是「我清掉设备之后还剩什么」——那是前者。
    ///
    /// 改复制覆盖范围时**必须同步改这里**，否则它会以最坏的方式过期：
    /// 显示"已备份"而其实没有。
    ///
    /// ⚠ **改之前先用仓库自己的工具查，别凭印象填。**
    /// 第一版我把「聊天记录」写成"只在本机、重装会丢"，而
    /// `scripts/where_does_this_route_run.py /api/assistant/history` 的答案是
    /// **owner=pi、无本地分支** —— 它在 Pi 上，重装根本不丢。
    /// 一张写错的状态表比没有更糟：它会让人按错误的前提去清设备。
    static func categories(localBooks: Int?) -> [Category] {
        [
            Category(name: "高亮", localCount: nil, replicated: true, caveat: nil),
            Category(name: "便签", localCount: nil, replicated: true, caveat: nil),
            Category(name: "插入页", localCount: nil, replicated: true, caveat: nil),
            Category(name: "手写墨迹", localCount: nil, replicated: true, caveat: nil),
            // ⚠ 规矩变了(用户 2026-08-28 拍板 A):**本地的书必须先上传服务器
            // 才能开始使用**。做完之后这一行就恒为"已备份" ——
            // 因为不备份的书根本打不开。在那之前如实说现状。
            Category(
                name: "书籍", localCount: localBooks, replicated: false,
                caveat: "规矩已定：以后新书要先传到服务器才能打开（还没做完；"
                        + "现在两边各是各的，不会自动互相补齐）"),
            // ⚠ 钉在页面上的卡片**作为便签存**（native-local-runtime.js 的
            // 原注释：「A card or generic result pinned on the page is a note
            // placement」），所以它随便签一起备份。没钉的那些不在。
            Category(
                name: "卡片（已钉在页上的）", localCount: nil, replicated: true,
                caveat: nil),
            // ⚠ 聊天记录**在 Pi 上**，不是本机 —— 这一条我一开始写错了，
            // 靠 `scripts/where_does_this_route_run.py /api/assistant/history`
            // 查出来的（owner=pi，runtime 无本地分支）。
            // 一张写错的状态表比没有更糟：它会让人按错误的前提去清设备。
            Category(
                name: "聊天记录", localCount: nil, replicated: true,
                caveat: "存在服务器上，登录后就在，重装不丢"),
            Category(
                name: "阅读位置", localCount: nil, replicated: true,
                caveat: nil),
        ]
    }
}

/// 数据与同步的统一入口。
struct ReaderDataHubSection: View {
    @State private var serverHost = ReaderServer.host
    let localBookCount: Int?
    let isSignedIn: Bool
    let isSyncing: Bool
    let lastSyncSummary: String?
    let onSignIn: () -> Void
    let onSync: () -> Void

    var body: some View {
        Section("数据与同步") {
            // 账号。⚠ 刻意**不叫「Pi」**：Pi 是中继不是数据权威,
            // 把实现细节放在界面第一层,是这次混乱的来源之一。
            LabeledContent("账号", value: isSignedIn ? "已登录" : "未登录")
            // ⚠ 服务器地址可改 —— 用户 2026-08-28 说了将来可能换 Mac mini。
            // 界面上只说「我的服务器」,不出现具体是哪台机器:那是实现细节,
            // 而且它会变。把机器名当一等公民正是这次混乱的来源。
            LabeledContent(ReaderServer.displayName, value: serverHost)
            // 来电通道现在到哪一步。⚠ **iPad 上没有控制台** —— 不显示的话
            // 「电话打不进来」永远只能靠猜：2026-08-29 就卡在这里，
            // token 没上去，而"发了被拒"和"根本没发"完全分不开。
            LabeledContent("来电通道", value: ReaderVoipCall.shared.status)
            // 图片代理现在什么状态。⚠ 同一条教训的另一处应用：2026-08-30
            // 所有卡片图全裂，Swift 端返回着十几种具体错误码，而 <img>
            // 只画一个问号 —— 没有这行，排查只能整层整层地猜。
            LabeledContent("图片代理", value: ReaderImageProxyHealth.line)
            if !isSignedIn {
                Button("登录", action: onSignIn)
            }

            Button(isSyncing ? "同步中…" : "立即同步", action: onSync)
                .disabled(isSyncing || !isSignedIn)
            if let summary = lastSyncSummary {
                Text(summary)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            // ⚠ 这一段是这一屏存在的理由。**"没备份"要跟"已备份"一样显眼。**
            DisclosureGroup("每类数据在哪（重要）") {
                ForEach(ReaderDataHub.categories(localBooks: localBookCount)) { item in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(item.name).font(.system(size: 14, weight: .medium))
                            Spacer()
                            Text(item.replicated ? "已备份" : "只在本机")
                                .font(.system(size: 12))
                                .foregroundStyle(item.replicated
                                                 ? Color.secondary : Color.orange)
                        }
                        if let count = item.localCount {
                            Text("本机 \(count)")
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                        }
                        if let caveat = item.caveat {
                            Text(caveat)
                                .font(.system(size: 11))
                                .foregroundStyle(Color.orange)
                        }
                    }
                    .padding(.vertical, 2)
                }
                Text("电脑是云端存储（那边的 AI 要看同一批文件）；数据的权威副本"
                     + "在这台设备上。上面标「只在本机」的，重装 App 就会丢。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
    }
}

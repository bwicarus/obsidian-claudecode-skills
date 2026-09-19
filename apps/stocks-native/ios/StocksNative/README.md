# 股票 iPad 原生验证版 0.2.2

SwiftUI 界面、Swift Charts 分时/K 线/资金图、Apple 登录、本地缓存和 AVAudioEngine 双向音频；不含 WKWebView、远程网页界面或第三方依赖。

## 构建

- Xcode 15 或更高，iOS / iPadOS 17 或更高，支持 iPad 和 iPhone。
- 在本目录执行 `xcodegen generate`。
- 不签名编译：`xcodebuild -project StocksNative.xcodeproj -scheme StocksNative -sdk iphonesimulator -configuration Debug CODE_SIGNING_ALLOWED=NO build`。
- 真机需为独立 bundle `space.bwicarus.stocksnative` 配置签名；由独立 CI 工作流处理，不修改阅读器项目或证书设置。

## 设备使用

1. 打开 App，在设置中保留默认地址 `https://bwicarus.space/stocks-native`，使用 Apple 账号登录。配对码收在「审核与开发连接」中。
2. 凭证成功写入设备钥匙串后，App 先显示本机缓存，再更新市场概览、股票列表和实时行情。搜索代码或名称，选择股票读取详情；下拉刷新。
3. 分时、5/15/30/60 分钟和日/周/月 K 线均由本机 Swift Charts 绘制。点击「标记」可用手指或 Apple Pencil 画笔、直线、箭头和文字；标注按股票保存在本机。
4. 在右侧点击「开始语音」。首次需允许麦克风；服务端 ready / active 后才启动采集和上行。连接后可语音或文字问询。
5. 「结束通话」立即停止本机音频，仅关闭本设备会话。断线显示错误，需手动重连。MVP 进入后台时主动结束音频，不宣称后台保活。
6. 连接详情可查看本设备 thread / session ID 以及上下行包数，用于与另一台设备上的阅读器同时测试。

## 协议

- Apple 登录：`POST /api/auth/apple`，提交 identity token、原始 nonce、device ID 和设备名，返回设备绑定 token。
- 配对：`POST /api/pair` 仅用于审核和开发连接；`deviceId` 必须原样匹配请求。
- 市场与实时：`GET /api/market/overview`、`GET /api/realtime?codes=...`。
- 列表与详情：`GET /api/stocks?q=&limit=50`、`GET /api/stocks/{code}`。
- 图表：`GET /api/stocks/{code}/intraday`、`GET /api/stocks/{code}/kline?period=m5|m15|m30|m60|day|week|month`。
- `asOf`、`time` 为字符串；价格/涨跌幅/成交额/成交量允许 null，蜡烛 OHLC 数值必须为 number。缺失成交量不绘制假柱，也不替换为零。
- 数据接口与 WebSocket 均使用 `Authorization: Bearer <token>`。token 只保存在 Keychain，不写入 URL、普通设置或日志。
- WebSocket：`wss://bwicarus.space/stocks-native/voice?deviceId=<UUID>`。
- 客户端控制：`{type:"start",stockCode?,capabilities?}`、`{type:"ui.context",context}`、`{type:"text",text}`、`{type:"stock.select",code}`、`{type:"capability.result",...}`、`{type:"stop"}`。
- 服务端事件：`state`（connecting/ready/active/closed）、`transcript`（role/text/final）、`stock.selected`（code）、`capability.action`（本地标注动作）、`error`（message）。界面动作必须由 App 回执成功后才算完成。
- 双向音频：WebSocket 二进制，48,000 Hz、单声道、PCM16LE。客户端上行固定每帧 960 采样 / 1,920 字节 / 20 ms。输入通过 AVAudioConverter 处理设备真实采样率；输出转换到本地 float32 回放。
- 收到 `stock.selected` 会打开对应股票；发送队列拥塞、服务器错误、断线或系统音频中断均停止本机音频，不自动抢占重启。

## 验证边界

源码工程由 macOS CI 编译和签名。真实 Apple 登录、麦克风、回声消除、扬声器路由、蓝牙设备、实时上下文注入与网络质量仍需 iPad 安装验收。

本验证版提供真实行情、原生图表、语音与文字链路，以及第一版图表绘图标记；不包含交易、持仓写入、后台盯盘或旧站所有功能。

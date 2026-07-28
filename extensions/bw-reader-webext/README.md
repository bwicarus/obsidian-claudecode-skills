# BW网页伴读扩展 — Safari / Chromium

## 产品边界

扩展是网页功能的唯一实现：

- 任意 `http/https` 网页安装扩展后，获得查词、选区工具条、AI 助手、网页高亮、
  卡片固定、便签、临时绘图和句级沉浸翻译；
- 没有扩展时，普通网页没有 BW 阅读功能；
- PWA 本身只阅读真正的书：PDF、EPUB、本地 HTML/Markdown 与收藏入口；
- 书籍 PWA 没有扩展时使用自己的完整回退界面；
- 书籍 PWA 检测到扩展时，把共享 UI 交给扩展，自己保留文档渲染、精确锚点、
  翻页/缩放等本地动作及书籍专属数据。

服务器抓取网页再放进 `/pdf/web/live` 的旧网页阅读器不属于本扩展架构，也不在
manifest 或受信任 PWA 路由中。

## 运行结构

manifest 有两个注入块：

1. `document_start` 的 `src/pwa-marker.js` 只匹配四个精确书籍入口：
   `/pdf/view`、`/pdf/epub/view`、`/pdf/html/view`、`/pdf/fav/open`；
2. `document_idle` 的完整共享运行时匹配任意 `http/https` 顶层网页。

普通网页使用 `WebAdapter`。书籍 PWA 使用 `PwaAdapter`，根据页面返回的
`state.mode` 与 `capabilities` 调用同一套上层 UI。

接管协议为 `bw-reader-pwa/1`，采用两阶段切换：

1. `HELLO` 只发现并读取 DocumentHost，不隐藏 PWA 界面；
2. 完整 Shell 和 PwaAdapter 均挂载后发送 `TAKEOVER`；
3. 接管成功后每 5 秒 `HEARTBEAT`；
4. `GOODBYE` 或页面租约超时后，PWA 恢复自身共享界面；但扩展 marker 已为当前 document
   保留同步/直连所有权，同一页面不会悄悄再启动第二个 PWA sync owner。

这避免扩展只加载了一半时出现“两边都隐藏”的空白界面。

## 账户、网络与设置

- Bearer token 只由 `background.js` 与账户分区存储模块接触，内容脚本不可读取；
- 首次使用要打开一次已登录的书籍 PWA，由短期 provider ticket 证明账户；
- 证明成功后，扩展持久保存当前账户 namespace；普通网页不依赖任何 PWA 标签页
  继续打开，也能使用该账户已验证的设备令牌；
- 切换账户会使旧异步租约失效，防止晚到请求跨账户落盘；
- WebRTC 内容宿主只在内存中持有不透明 `accountProof`，不持有 namespace、Bearer 或 owner
  token；该证明仅作 RTC 账户围栏，不能换取服务端权限。服务端使用 `registryDigest` 作为
  协议代际盐，同账户同代际稳定、跨代际 fail closed；
- 后台网络仍只允许固定 Pi origin 与明确的 API 路由/方法；
- 扩展设置保存在 `bwReaderExtensionPreferencesV2`，是跨网站的本地权威；
  每个网站的 isolated-world `localStorage` 只是 RC 共享组件的兼容镜像；
- 进入书籍 PWA 时，PreferenceStore 只补充扩展尚未保存的兼容键，已有扩展值不会被旧镜像
  覆盖；进入 `sync-v3` 后缺失记录可合并，真实分叉必须显式暂停，不能自动选择扩展值。

跨设备同步只开放 DataRegistry 中的 `user-settings` 与 `vocabulary-state`，协议固定为
`sync-v3` + `record-parent-state/1` + `sync-gateway/2`。每次真实变化必须证明其父业务状态；
revision 更高也不能覆盖无关分支。派生缓存、页面几何和书籍私有数据均不上传。WebRTC 只是
同一 server baseline 之后的加速通道，失败时继续使用服务端中转。

同一安装中的 PWA 与扩展共享持久 `deviceFamilyId`，并通过服务端 `owner-lease/1` 保证只有
一个网络同步 owner；PWA 请求接管时优先。不同设备使用不同 family，仍可并行同步。所有
exchange/snapshot/signal 都必须通过当前 generation/token 围栏，内容脚本拿不到 token。
客户端用墙钟和单调时钟共同限制租约，并从请求发出时计算安全窗口；睡眠、时钟回退或迟到响应
不会延长写权限。PWA `pagehide` 以 keepalive 尽力释放；从 BFCache 恢复时最多等待 2 秒，
随后也必须重新领取成功才恢复网络 owner，普通关闭则释放并永久销毁本页 owner。

旧裸键只盘点、不读取、不迁移、不删除。网页绘图保持会话级：刷新或页面尺寸/排版
改变后丢弃，避免笔迹错误绑定到已经变化的网页内容。

## 源码

- `_server_deploy/static/pdf/rc-*.js`：共享 UI/能力源码；
- `vendor/`：`build.py` 的生成物，禁止直接手改；
- `src/web-*.js`：普通网页 DocumentHost 功能；
- `src/pwa-marker.js`、`src/pwa-adapter.js`：书籍 PWA 接管；
- `background.js`：账户分区、DataStore provider 与受限网络；
- `content.js`、`src/shell.js`：共享工具条和应用外壳。

共享源码更新后运行：

```bash
python3 build.py
```

## 验证

```bash
node --check background.js
python3 test_smoke.py
python3 test_card_drag.py
python3 test_sidebar_layout.py
python3 test_sidebar_quick_visibility.py
python3 test_review_candidates.py
```

书籍 PWA 部署完成后再运行：

```bash
python3 test_pwa_handoff.py
python3 test_pwa_native_contract.py
```

`test_smoke.py` 必须证明普通网页加载完整 WebAdapter；卡片与侧栏测试重新成为正式回归，
不再是“历史诊断”。

## Token

1. 登录 Pi 的 `/profile/` 创建独立设备令牌；
2. 打开一次已登录的书籍 PWA，让扩展确认账户；
3. 在任意网页打开扩展弹窗，保存并测试令牌。

令牌不会返回内容脚本或显示在弹窗状态中。

## 打包与发布

生成 Safari 输入包：

```bash
python3 package_safari.py
```

生成 Windows 测试包与 channel：

```bash
python3 publish_test_channel.py
```

只有用户明确要求部署时才加 `--deploy`。不要覆盖旧 ZIP。Windows 固定目录替换后，
必须显式执行一次 `chrome.runtime.reload()`（或在 `chrome://extensions` 点“重新加载”），
再刷新测试网页。

`--deploy` 会在替换生产文件前自动运行 `release_preflight.py`：候选版本必须严格高于当前
测试通道，并通过共享源码/vendor、语法、runtime、Chromium 合同、ZIP 内容和 SHA-256
门禁。门禁通过后，会先把生产 channel 的原始字节（或明确的 `missing` 状态）及候选
SHA-256 耐久记录到 `/home/bwicarus/deploy-backups/reader/webext-channel-*`，再创建
不可变版本文件，最后原子切换 channel。切换阶段失败会按该记录逐字节恢复并验证；
新建的不可变版本文件可以保留。命令会输出实际备份目录、审计记录和回滚结果。
Surface Pen 的硬件 direct-manipulation 仍必须按
[`windows/SURFACE-PEN-CHECKLIST.md`](windows/SURFACE-PEN-CHECKLIST.md) 真机验收。

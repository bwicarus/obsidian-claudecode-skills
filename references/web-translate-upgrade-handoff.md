# 网页整页翻译升级 — 交接文档

> 主线：浏览器扩展中的网页翻译统一为「Google 默认 + AI 无状态 / 页面短时会话 / 自动」。
> 截至 2026-07-25，编号切分协议、三模式路由、Claude 无工具短时会话、设置入口、扩展账户
> 分区缓存和部署门禁均已接线。旧记录中的“短页会话、长页无状态”是一次未重新验算的反向
> 结论；正式规则是短内容无状态，预计阅读内容超过阈值才启用会话。
>
> 相关：[`reader-extension-handoff.md`](reader-extension-handoff.md)、
> [`codex-integration.md`](codex-integration.md)。

## 0. 接手顺序

1. 读本文和 `reader-extension-handoff.md`。
2. 跑编号协议测试：
  `python3 scripts/vocab/test_batch_protocol.py`，应显示 `ALL PASS`（当前 23 项）。
3. 跑服务端/部署边界测试：
   `python3 -m unittest -v tests.test_web_translate_upgrade tests.test_reader_deploy_manifest tests.test_pwa_web_reader_retirement`。
4. 改共享前端 `web-immersive.js` 或 `rc-settings.js` 后，必须执行
   `python3 extensions/bw-reader-webext/build.py`，再执行完整 handoff 检查。

## 1. 当前生产契约

### 1.1 前端

- `web-immersive.js` 仍以**句子**为最小翻译单位；`flush()` 按句子数组发
  `POST /pdf/api/web-translate`。
- 请求体只允许：

  ```json
  {
    "texts": ["sentence 1", "sentence 2"],
    "backend": "ai",
    "glossary": {},
    "mode": "session"
  }
  ```

  `glossary` 和 `mode` 只允许 AI 请求携带。服务端只接受扩展后台已经解析好的
  `stateless/session`；`auto` 不出扩展。页面 URL、模型名、document UUID 和缓存 namespace
  均不能由网页正文或内容脚本指定。
- Google 默认每批最多 40 句；AI 每批最多 20 句。
- UI 的「网页翻译」页可选择 Google 或 AI；AI 内有自动、无状态批翻和页面短时会话。
  AI 后端、型号和深度复用统一的
  「AI·翻译 → 网页整页翻译（无工具）」配置。
- 自动模式由扩展权威设置决定。初始 idle discovery 只读纯文本并统计句子数，按
  `round(总句数 × 70%)` 与阈值比较；短内容走无状态，超过阈值且后端支持时才用会话。
  决策在当前导航内冻结，页面不得用请求体覆盖。
- 生词预翻译始终走 Google，避免仅打开页面就静默消耗 AI 配额。
- 译文仍以响应 `zh[i]` 映射到原句 `texts[i]`；编号协议只存在于服务端与 AI 之间。

### 1.2 服务端

- `/pdf/api/web-translate` 默认 Google；`backend="ai"` 可进入无状态或文档短时会话编号批翻。
- 路由严格拒绝未发布字段和模式，不接受 `url`、`model`、`session_key`、
  `cacheNamespace` 等客户端控制字段。
- session 的 canonical UUIDv4 只允许来自扩展后台生成的 `X-BW-Translate-Document`；
  会话以 `(uid, document UUID)` 隔离。同一页面请求串行，不同页面可并行；空闲 300 秒清理，
  全局最多 8 个会话。达到 32 轮或约 24k 上下文 token 时不再硬终止：旧会话先压缩出
  翻译风格、术语、实体、指代和必要上下文，服务端按白名单 schema 重建后，以
  `untrusted_context_summary` 注入全新会话继续当前页面。
- 压缩摘要从不拼入 trusted system；未知 `command/instruction/task` 等字段全部丢弃。
  摘要失败或格式非法时以空上下文重建并继续当前批，只有新进程也无法启动时才进入既有
  session → stateless → Google 降级。
- AI profile 和缓存身份只能由服务端根据当前账户配置生成。
- AI 未对齐、拒答或不可用时按段回退 Google，并在响应中返回
  `sources/degraded/reason`，不能把降级伪装成 AI 成功。
- 网页正文属于不可信输入：
  - Gemini 走纯文本 API；
  - Claude CLI 显式使用 `--tools ""`、`--setting-sources ""` 和
    `--no-session-persistence`，在隔离 cwd 运行；session 只复用当前内存中的
    `stream-json` 进程，不写磁盘；
  - Codex CLI/app-server 暂无可证明的 tools-off 边界，所以若 profile 配成 Codex，
    服务端会显式降级到无工具 Gemini，不能用 `read-only` 冒充“不可读文件”。

### 1.3 缓存与隐私

- 服务端只缓存 Google 与 AI 无状态译文；session 译文依赖页面上下文，不读取也不写入跨文档
  服务端文本缓存。session 中的 Google fallback 仍写 Google 桶。
- AI namespace 使用 v2 且按 `stateless/session` 分开。config 的兼容单值保持 stateless；
  翻译响应的 `cacheNamespace` 必须等于 `cacheNamespaces[modeResolved]`。
- 页面级译文缓存只存在浏览器扩展的账户分区 IndexedDB
  `translation-cache` collection 中，键包含服务端 namespace 与浏览器认证得到的页面 URL。
- 页面 URL 由扩展后台从 `sender.tab.url` 取得，不出现在页面消息参数或服务端请求中。
- `/pdf/api/web-trcache` 已永久退役并返回 HTTP 410；历史
  `state/web-trcache` 数据保留但运行时不再定位、读取或写入。
- `webTrCacheV1` 等旧裸缓存不会迁入新账户 Vault，也不会被命中。

## 2. 编号批翻协议

唯一源码是 `scripts/vocab/translate.py`。

- 发给 AI：每句加批内编号 `⟦n⟧`，末尾可返回术语块 `⟦G⟧`。
- 解析按编号键映射而非输出位置映射；重排或漏句不会让后续句子整体错位。
- 译文中的换行、AI 前后废话和源文伪造的 `⟦…⟧` 标记都有防护。
- 回退阶梯：先采纳有效段 → 命中率足够时只重试缺段 → 其余缺段回退 Google。
- 空原文保持原索引为空，不改变数组长度。
- AI 与 Google 文本缓存使用不同 namespace；具体 AI namespace 包含服务端 profile 身份。
- 短滚动术语表由服务端解析后回给前端，前端最多保留 40 项继续传给下一批。

主要入口：

| 函数 | 职责 |
|---|---|
| `ai_translate_batch(...)` | 分块、编号、校验、缺段重试、Google 回退、缓存 |
| `_ai_batch_call(...)` | 一次纯文本 AI 调用和编号输出解析 |
| `_parse_batch(...)` | 按编号恢复译文与术语 |
| `_clean_ai_zh(...)` | 清理前缀并识别拒答 |
| `_san_seg(...)` | 防止原文伪造协议标记 |

## 3. 唯一源码与原子部署

- 仓库只维护 `scripts/vocab/translate.py` 一份实现。
- `scripts/reader_deploy_manifest.py` 只允许这一条跨目录映射：

  `scripts/vocab/translate.py` → WebApp `web_translate_protocol.py`

- `html_reader.py` 的生产路径只 import `web_translate_protocol`；不能再动态加入开发仓库
  `scripts/vocab`，也不能在 `_server_deploy` 复制第二份实现。
- `deploy_reader.sh` 在切换版本前跑协议和路由测试，原子复制后再核验实际 import 的
  `module.__file__` 精确指向 WebApp 目标文件。
- 修改共享前端后，扩展 `build.py` 会把同一源文件同步到扩展 vendor，并由 release
  preflight 校验字节一致和版本化资源清单，避免浏览器继续使用旧 immutable 资源。

## 4. 已发布测试门禁

```bash
python3 scripts/vocab/test_batch_protocol.py

python3 -m unittest -v \
  tests.test_web_translate_upgrade \
  tests.test_reader_deploy_manifest \
  tests.test_pwa_web_reader_retirement \
  tests.test_vbook_route_policy

node --test --test-reporter=spec \
  tests/reader_contract/extension-provider.contract.test.mjs
```

当前覆盖包括：漏段、重排、拒答、低命中率降级、敌对网页提示、缓存 namespace、
严格请求 schema、Codex 安全降级、Claude 无工具无持久化会话、同页串行/跨页并发、TTL/LRU、
轮数与上下文预算触发的结构化摘要换会话、摘要注入隔离与失败续译、
session → stateless → Google 降级、URL 不出扩展、账户 A/B 缓存隔离、
session/stateless 实际 namespace 分桶、旧服务端页面缓存 410、唯一源码部署和生产 import 路径。

## 5. 已知边界和后续校准

- “缓存命中”不等于历史免费。Claude 的会话每轮仍携带增长中的历史，只是相同前缀以较低
  成本读取；因此当前自动阈值是可配置的初始策略，不应再把某个固定句数宣称为普适成本最优。
- 会话已经读取 Claude stream-json 的
  `input/cache_creation/cache_read/output` usage，并在 usage 缺失时用字符数估算；下一步应按
  实际阅读页面分布校准 32 轮/24k 压缩线和自动模式阈值，而不是反转“短内容无状态、长内容
  会话”的方向。
- Gemini 当前不提供本实现所需的安全 CLI 内存会话，选择 session 会明确解析为 stateless。
- Codex CLI/app-server 仍没有可证明的 tools-off 边界；网页正文不能交给它。`read-only`
  只禁止写入，不禁止读取本机文件。

## 6. 后续设计原则

- 句子切分、编号协议、页面缓存和翻译 UI 都必须各有唯一实现；PDF/EPUB/网页只通过适配器
  提供文本与落点，不复制算法。
- 页面 URL、账户 namespace、模型、缓存 namespace 等可信身份只能由扩展或服务端生成，
  不能相信网页参数。
- 新模式必须先补严格 schema、降级语义、账户切换围栏和部署测试，再进入设置 UI。
- 遇到不同宿主功能冲突时保留差异并列出决策点，不擅自删除其中一套用户功能。

## 7. 独立优化点

`_client/core/qa_browser.py` 的截图问答仍可能在每轮冷启动时重放完整历史与 OCR 长文，
比无状态网页翻译更值得优先做会话/压缩优化。它与本翻译协议是独立任务，不应为了优化 QA
而扩大网页翻译的权限边界。

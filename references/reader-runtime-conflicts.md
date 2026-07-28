# 阅读器共享运行时：冲突与迁移登记

目标是让普通网页扩展、真书 PWA fallback、真书 PWA + 扩展尽量共用上层语义、组件和接口，
同时保留 PDF、EPUB、导入 HTML 与普通网页必然不同的宿主实现。整合不以删减功能为手段。

架构基线见 `reader-runtime-architecture.md`。状态含义：

- `equivalent`：行为和数据语义已验证一致，可共享源码。
- `adapter`：用户意图相同，宿主 API/坐标/存储不同，保留多个 adapter/surface。
- `conflict`：现有行为不同或互相矛盾，用户决定前保留两边。
- `pending`：尚未完成盘点、迁移或真机证明，不删除旧实现。
- `decided`：用户已经明确选定目标行为，可以据此迁移。

`DocumentHost` 的 `supported/pending/unsupported` 是另一层 capability 状态，不要与本表混用。

## 当前登记

| 功能 | 状态 | 已确认共享部分 | 保留的差异/未完成项 |
|---|---|---|---|
| 产品入口 | decided | 普通网页只由扩展实现；PWA 只承载 PDF/EPUB/HTML/Markdown/Favorite 真书 | 旧 PWA web/proxy/RBI 已备份并退役，不得恢复 |
| UI 所有权 | adapter | 共用视觉令牌、组件和动作契约 | 普通网页=扩展；真书无扩展=PWA；真书有扩展=扩展 Shell。PWA 只在 TAKEOVER 后隐藏重复 UI，断线恢复 |
| 文档统一端口 | adapter | selection/content/location/read/navigate/search/highlight 等统一语义 | PDF 页几何、EPUB offset、HTML 文档锚、Web quote/DOM 锚必须保持私有 |
| PWA 真书 RPC | adapter | `book-host/1` + `bw-reader-pwa/1`；两阶段接管、心跳、GOODBYE | 每种书的 capability 与 payload 仍须逐项真机验证，缺失时 fail closed |
| 普通网页宿主 | adapter | `WebAdapter` 为正式产品实现，负责网页选区/锚点/高亮/搜索 | 不能复用 PDF 的页码、裁边、几何；书籍专属控件必须隐藏 |
| 选区控制与唯一工具条 | pending | 统一 selection 形状和 2A 横向浮条 | PDF 字符层、EPUB/HTML DOM、Web Selection 的监听和定位不同；完整 open/update/close controller 尚待收口 |
| 本地 DataStore | adapter | 稳定 ID、revision、mutationId、tombstone、冲突码和值域共享 | 卡片、词汇、收藏、对话、高亮等仍有旧真源；未逐项迁移前保留旧读取 |
| 设置权威 | equivalent | 有扩展时扩展本地设置跨站权威；无扩展真书用 PWA fallback | 新增键仍须逐键决定 global/document/device/session，不能按前缀整包迁移 |
| 当前账户 | equivalent | PWA session 验证账户；扩展保存最后一次已验证 namespace，普通网页不依赖 PWA 常开 | namespace 只隔离数据库，不代替 token/授权 |
| API token | equivalent | 后台私有 IndexedDB，内容脚本不可读 | 旧裸 token 只保留无明文审计存根，不自动传播 |
| 跨设备数据同步与命令补投 | conflict | SyncGateway 交换 DataStore changes；CommandOutbox 重试固定业务命令 | 两者不能互相导入；服务端 cursor/去重/冲突 UI 尚未完整实现 |
| 旧浏览器本地实体 | pending | 旧 pins/highlights/ink 不删除 | pins/highlights 如何无损认领仍待盘点；历史 `webInkV1` 保留但新 stroke 不再写 |
| 普通网页绘图 | decided | 工具和笔/橡皮状态机可共享 | 响应式重排使持久坐标失效，新墨迹只存当前标签页会话 |
| 真书绘图 | adapter | 共享工具意图和 Apple Pencil/触摸规则 | stroke 坐标和持久化归 PWA 各书籍 surface，不受网页 session-only 规则影响 |
| 卡片实体/placement | adapter | 全局唯一实体 ID；学习卡 `id=cid=gid`；拖放状态机共享 | Web DOM placement 与 PDF/EPUB/HTML 书内 placement 分别由宿主保存 |
| 卡片删除/收藏 | decided | 6A 删除区/收藏区和同一实体引用 | 删除 placement 不删除源对话或实体；各宿主 drop 命中不同 |
| 便签 controller/surface | pending | 内容模型、编辑状态和命令应唯一 | Web DOM、PDF 页、EPUB/HTML reflow 的落点与重绘不同 |
| 高亮 | adapter | 颜色、编辑器、列表和语义记录可共享 | PDF rect、EPUB/HTML offset、Web CSS Highlight/quote 恢复不同 |
| 查词当前词反馈 | adapter | “当前查询词呼吸高亮”意图共享 | PDF 字符层、EPUB hook、Web DOM/CSS Highlight 机制不同 |
| 振假名/注音 | adapter | 顶栏动作名和开关 UI 可共享 | PDF 字符叠层、EPUB/HTML/Web DOM 修改方式不同 |
| 沉浸翻译切分 | conflict | 开关、加载/错误状态和译文组件可共享 | 普通 Web 已确定为句级按钮/句级预翻译；EPUB 当前以段落/块为主。未确认前不强改 EPUB |
| 译文按钮行为 | decided | 网页句子按钮只展开对应句译文 | 不得同时打开大翻译面板 |
| 未掌握词预处理 | adapter | 下划线意图和句长/词数阈值可共享 | 分词、DOM 切句及 PDF/EPUB 文本层不同 |
| 助手 UI/SSE | adapter / conflict | `rc-assistant.js` 的气泡、轮次容器、工具卡层级共享 | PDF/EPUB/Web 的上下文、附件、历史端点语义不同，不强行拼成一种 payload |
| 跨书跳转 | adapter | `HOST.goToInBook(file,page)` 是统一意图 | WebAdapter 打开绝对 PWA URL；PWA host 在书内执行。不能依赖 isolated world 看不到的全局函数 |
| 编号图片 | adapter | asset ID、引用语法和卡片渲染共享 | 本地资产直接经 BW；remote-only 资产由 BW 受控代理，扩展不申请无约束全网 host permission |
| 解释结果呈现 | conflict | 后端请求和结果数据可共享 | PDF “呼吸高亮后点击”、Web 面板等现有交互未由用户统一，先保留 |
| 侧栏 controller | pending | 3A 磨砂、浮动/挤压、实时调宽、统一字体和滚动轨道共享 | PDF 历史、EPUB 章节、Web 目录/语法 tab 集合不同；不能靠删 tab 消除重复初始化 |
| 顶部栏 | adapter | 4B 可收起、统一按钮组件、capability 驱动 | 真书显示页码/缩放/裁边等；普通网页只显示真实支持项 |
| 网络执行器 | pending | 固定 operation + schema + 超时/大小限制 | 旧模块仍有多条请求路径；不能用一个任意 URL/method operation 假装统一 |
| 派生缓存 | adapter | 按账户隔离的 query/translation/dictionary cache | TTL、容量、退出保留和 PWA fallback 收束仍待逐类决定 |
| 服务器旧实体分区 | 首批完成 / 更广 pending | reading-pos、phrases、notes、PDF/EPUB/HTML highlights、entity/assets 已按 uid+namespace 分区并通过恢复/隔离测试 | deferred-owned 域和统一 mutation ledger 未完成；不得宣称整个数据层完成 |

## Anchor 不透明边界

| 宿主 | 私有 anchor 数据 | 所有者 |
|---|---|---|
| PDF | 页码、字符范围、页几何、revision | PWA PDF host |
| EPUB | 章节、offset、reflow 定位 | PWA EPUB host |
| 导入 HTML/Markdown | 导入文档内文本/DOM 定位 | PWA HTML host |
| 普通 Web | URL、quote、DOM 路径/上下文 | 扩展 WebAdapter |

共享层只保存 `{documentId, kind, revision, data}` 外壳。不得读取另一宿主的 `data`、改写
`kind` 后重试，或用普通 `navigate()` 成功冒充 `resolveAnchor()` 已实现。

## 已确认的旧服务端数据决定

2026-07-24 用户确认开发阶段只有本人。现存未分区服务端 sidecar 属于当前认证主账户，但 owner
必须来自认证解析的 uid + namespace，不能硬编码，也不能 first-login-wins。

旧源永久只读保留；迁移按 inventory、checksum、原子 copy、复核、immutable manifest-last。
未来账户为空；身份或校验歧义 fail closed；稳定 card/entity/asset ID 与引用不重编号。

首批已覆盖 reading-pos、phrases、notes、PDF/EPUB/HTML highlights、entity/assets。
vocab/Anki、conversation、attention、ink、favorites、userpages 等 deferred-owned 域继续原功能，
之后逐域无损迁移。

## 尚待用户决定

| 项目 | 需要决定 |
|---|---|
| `device-preferences` 后续细分 | 新设置属于账户、设备、书籍还是标签页会话 |
| 派生缓存 | 跨书复用、TTL、容量、退出保留和淘汰 |
| `ui-session` | 标签页/浏览器会话/安装生命周期，是否重启恢复 |
| 旧 pins/highlights | 认领、只读导出或分区迁移；当前不自动合并 |
| EPUB 翻译粒度 | 保持段落、改句级，或由书籍设置切换 |
| 解释结果交互 | PDF 呼吸高亮与 Web 面板选择何种统一外观/行为 |
| 不同宿主侧栏 tabs | 哪些全局共享，哪些按格式显示 |

## 变更门禁

1. 迁移前用测试固定现有行为。
2. `conflict` / `pending` 只能加 adapter、capability 或兼容读取，不能删除实现。
3. 默认行为、数据格式或物理归属改变前更新本表；冲突项先让用户决定。
4. 回归矩阵覆盖普通 Web 扩展、四类 PWA fallback、四类 PWA takeover。
5. capability 缺失时 fail closed；“按钮能点但什么也没做”不算兼容。
6. 网页绘图 session-only 是已确认决定；不得据此删除旧数据或改动真书墨迹。

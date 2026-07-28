# 阅读器共享视觉：组件与冲突登记

目标是让普通网页扩展、真书 PWA fallback、真书 PWA + 扩展共用唯一视觉令牌与 UI 组件。
普通网页由扩展渲染；真书无扩展时由 PWA 渲染；真书有扩展时扩展是唯一可见共享 UI，PWA
保留 renderer 与 DocumentHost。各宿主仍负责坐标、锚点和内容投影，视觉迁移也遵守
“不擅自放弃差异功能”。

| 组件 | 状态 | 共享范围 | 暂时保留的差异/冲突 |
|---|---|---|---|
| 颜色、字体、圆角、阴影、动画 | equivalent | `rc-ui.js` / `reader-ui/2` 为唯一令牌源 | 宿主页面正文不继承，只作用于阅读 UI |
| 按钮、输入框、普通弹层 | equivalent | `.rc-ui-button/.rc-ui-input/.rc-ui-popover` | 尺寸和布局仍由具体组件决定 |
| 工具卡与学习卡外壳 | decided: 1A | 卡片内部排版、状态色与外壳共享 | 页面钉入态只显示卡片本体，不再加外层窗口/标题栏 |
| 选中文字工具条 | decided: 2A / 生命周期 pending | 横向浮条的外观、紧贴选区定位共享；页面只允许一个工具条实例；四个动作读取当前 adapter；EPUB/HTML 字节级相同的查词词项判断已统一为 `RC.ui.isDictionaryWord()` | 完整 open/update/close 生命周期仍待收口；定位继续由 PDF 字符层、EPUB/HTML DOM、普通 Web Selection 适配。PDF 目前只把英文词分到查词组，EPUB/HTML 还包含日文假名，未裁决前 PDF 不接入该共享判断 |
| 侧栏磨砂与排版 | decided: 3A / 生命周期 pending | 半透明磨砂材质、字体、色彩、tab 状态、设置弹层共享；目标上 `SidebarController` 只初始化一次 | 浮动/挤压、宽度、正文可用视口与挂载方式交给 `HostLayoutAdapter`。当前抽屉初始化与助手挂载都会改 tab/pane，PDF 还有 shared/legacy 分支；先保留按钮/tab 差异，不能靠删掉一条链消除双初始化 |
| 阅读器顶部控制栏 | decided: 4B | 收起态与顶部小把手共享 | 各宿主可保留自己的按钮集合，但都必须支持收起释放正文高度 |
| AI 回答容器 | decided: 5V | 采用语音对话的“普通气泡 + 轮次容器 + 三态工具卡”层级 | 普通文字保持气泡；有工具时整轮进入唯一容器；可拖结果使用标记/长条/完整卡三态 |
| 页面卡片删除/收藏入口 | decided: 6A | 拖动时出现左上删除区与底部收藏区 | 静止态不显示卡片角落删除按钮；从侧栏拖出的是副本，落入删除区不得删源内容 |
| 高亮编辑器 | adapter | 弹层、色板、备注和操作按钮可共享 | PDF 字符矩形、网页 CSS Highlight、EPUB offset 不变 |
| 拖拽反馈 | adapter | 阴影、缩放、删除/收藏热区状态可共享 | PDF 行/词落点与网页 DOM 元素框不同 |
| 卡片 controller / surface | pending | 唯一 `CardController` 管稳定 ID、状态、收藏/删除命令和拖拽会话；同一实体可有多个 placement，但不是复制实体 | `CardSurface` 按 PDF page、EPUB/HTML reflow、Web DOM/quote 分别显示落点和绑定高亮；现有实现尚未收口，不删任一宿主行为 |
| 便签 controller / surface | pending | 唯一 `NoteController` 管内容、编辑状态、保存命令和实体引用 | `NoteSurface` 的锚点、词/元素命中、挂载与滚动归各 DocumentHost；视觉共享不能把 PDF 页便签强改为 Web DOM 便签，反之亦然 |
| 墨迹 controller / surface | decided(web session) / 其余 pending | 唯一 `InkController` 管笔/临时橡皮状态机、stroke 和撤销；既定触控/手写笔规则共享 | PDF/EPUB/HTML 书内 stroke 由 PWA 持久保存；普通 Web 因响应式重排只保留当前标签页会话。历史网页墨迹不删除。surface 不用 fixed + scroll 追赶 |
| 沉浸翻译排版与切分 | conflict | 开关、进行中/失败状态和译文组件可共享 | 实时 Web 为句级按钮及句级译文；EPUB 当前以段落/内容块为主。切分会改变对照排版和交互，用户确认前两种视觉/行为都保留，不因共用组件而暗改 |
| 滚动条与对话轮次导航 | equivalent | 阅读 UI 内的视觉与交互可共享 | 宿主网页自身滚动条不改 |

迁移门禁：以上 1A/2A/3A/4B/5V/6A 是已经确认的统一标准；其余未登记冲突仍需对照
普通 Web 扩展、PDF/EPUB/HTML/Favorite 的 PWA fallback 与扩展 takeover，不能为了复用代码
自行删掉任一宿主已有能力。普通网页扩展视觉是正式产品回归范围。

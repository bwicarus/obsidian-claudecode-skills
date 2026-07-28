# 统一实体编号协议(资产+卡片+…,2026-07-21 用户设计定稿)

> 终局形态(用户拍板):**所有工具结果一套 保存/引用/渲染 规则**——统一编号保存{id→内容+元数据+渲染器kind},
> 工具回报=编号+brief,前端见 #编号 就地渲染;**引用出来的卡=活的**(与新卡全功能等同)。

## P1 已上线:卡片入全局编号

- `_entity_reg_cards(cards, meta) → card_xxxxxx`(pdf_reader;registry 同表 kind='cards',data=卡数组,states=各卡状态)
- make_anki(assistant/voice 双路)返回加 `id`;note 带"编号 card_xxx,可用 #编号 引用"
- **`GET/PATCH /pdf/api/entity/<id>`** 统一 resolve:cards→卡数组+states;img/vid→url+元数据;PATCH {idx,state} 卡状态回写
- 前端:制卡卡 gid=全局编号(_chipEnd 两处;所有宿主同编号=rc-flashcard._groups 共享同一状态对象);
  入库/评分后 `_stateSync` PATCH 回写(离线 outbox 'entst' PATCH 兜);
  `_assetInline` 扩 `#card_hex` → fetch entity → `mountState(cards+states 合并, gid=编号)` = **对话里渲出活卡**
  (可翻面/评分/入库),重渲/刷新/他处引用一律还原到同一状态 → "一张卡两种状态"从根上消失
- E2E:真制卡→card_253720→#编号渲活卡→评分→服务端 states {_st:done,_nid}→第二容器重渲直接"已复习"态

## P2 已上线(2026-07-21)

- **结构卡入编号空间**:`_entity_reg_data(kind, card)` → `wea_/new_/fac_` 等({kind3}_{hex6});
  发号时同步写回 `card.cid=aid`，实时卡、轮次回放、收藏/钉页与 `#编号` 引用全部沿用这一号；
  web_search 结构卡分支发编号(返回体 id);entity GET 结构卡→{card:完整卡对象}
- **#编号 渲染通用化**:_assetInline 正则通配任意前缀——img/vid/ast→<img> 快路;其余 entity resolve
  按 kind 分发:cards→活卡(states 还原)/card→`__vcInfoCardEl`(实时与回放同一渲染器)/url→图
- **便签宿主 states 还原**:renderNoteCard mount 后 fetch entity,merged 状态**就地更新共享数组字段**
  (⚠ 换引用会被 register 换回旧共享——同 gid 复用铁律)再重挂;状态一致不重挂
- E2E:#wea_ 渲出天气 InfoCard/重开书便签内卡直接"已复习"态(服务端 states)/清理净

## P3(待做)

- recall 统一入口按前缀分流;创造物 c_ id 规整进同一语法
- states 与 anki-add-cards 幂等键(aid)联动;未使用条目 TTL 清理;视频卡(vid_ 数据版)


> 用户拍板:AI 上下文里不传图片 URL/图片本身——每张图发**编号**,{编号,URL,元数据} 服务端保存;
> AI 用编号引用,程序自动查询翻译;**贴到页面时才实际下载**落盘,之后打开不再拉外链;
> 前端**看到命令字符段就直接读取渲染**。借此把"类似数据的保存和调用"统一规则化。
> 姊妹系统:创造物库(#id 句柄 + recall_creation)——同一哲学,内容型归创造物库,外部媒体归本表。

## 规则

- **编号**:`{kind3}_{hex6}`(`img_ab12ef`);**服务端发放,AI 永远不自编**;同 URL 复用同编号(防膨胀)。
- **存储**:`state/assets/registry.json` `{id:{kind,url,ts,local,concept,source,matched_query,page_url}}`;
  本地文件 `state/assets/files/<id>.<ext>`。
- **解析**:`GET /pdf/api/asset/<id>` — local 有→本地文件(immutable 缓存);无→302 外链;无此编号→404 JSON。
- **本地化时机**(用户拍板):**贴页时**(不是搜到就下——搜 8 张只用 1 张)。`/api/notes` POST/PATCH 保存后扫
  html.content 里的 `/pdf/api/asset/<id>` 引用 → `_asset_localize_async` 后台线程下载;失败保留外链下次重试。
- **AI 引用协议**:search_image 的 `found_brief=["#img_xxxx 概念(命中词:…)"]`;_note 教 AI
  "回答中直接写编号(独立成词)→ 界面自动渲染成图;绝不展开 URL、绝不自编编号"。
- **前端就地渲染**:`rc-assistant._assetInline`(renderMd 内,_linkifyPages 后):文本节点里
  `#(img|vid|ast)_hex` → `<img src=/pdf/api/asset/id data-aid>`;code/pre/a 内不动;流式期间被 renderMd
  既有 img 占位策略自然接管(收尾才真渲)。
- **拖出贴页内链**:图卡渲染 img 带 `data-aid`(renderImgs items.aid → _infoHtml);单图拖出/拖进卡用
  `/pdf/api/asset/<aid>` 作 src(非外链)→ 便签保存即触发本地化。

## 实现位置

| 件 | 位置 |
|---|---|
| 注册表/发号/本地化/路由 | pdf_reader.py(`_asset_reg`/`_asset_localize_async`/`/api/asset/<aid>`/`_asset_ids_in`) |
| search_image 发号+brief | assistant.py `_t_search_image`(经 `_pdf()._asset_reg`) |
| notes 贴页钩子 | pdf_reader.py `/api/notes` POST/PATCH(html.content 扫引用) |
| #id 就地渲染 | rc-assistant.js `_assetInline`(renderMd) |
| aid 穿透 | rc-voicecall.js renderImgs(items.aid)→_infoHtml(data-aid)→单图拖出(_asrc 内链) |

## 防错要点(设计评审结论)

- AI 引用不存在编号 → asset 路由 404;(P2)工具侧应答"无此编号+现有清单"自愈
- 编号带 kind 前缀防混用;registry 与创造物库**分表同句柄语法**(生命周期不同:贴页的永久,未用的可 30 天清)
- 上下文零 URL(found_brief/[图:alt] 注入/_infoText 全部只元数据)——省 token+防 AI 抄错 URL

## P2(待做)

- recall 统一入口按前缀分流(img_→注册表元数据,cre_→创造物库)
- make_anki image_url 支持传 #id;视频纳入(vid_)
- 未使用条目 TTL 清理;豆包路 found_brief 同款(relay 已精简 images,brief 已带 #id ✓)

## 图卡交互修复(2026-07-21,用户实测)

**① ✕删除的图会"复活"**:`_igWire` 的 ✕ 只 `cell.remove()` DOM + 标 `card.data.items[i]._gone=1`,**不真删数组**;而 `_infoHtml`/`_infoText`/拖整框 payload 都从 `card.data` 重渲——`_infoHtml` 的 images/videos 分支没过滤 `_gone` → 三态重渲/回放/拖整框把删掉的图带回来。修:①`_infoHtml` images/videos map 开头 `if (it._gone) return ''`(data-i 保原索引供 ✕ 定位);②`_infoText` images 分支 `.filter(!_gone)`;③`_dragToDock` 的 `raw` 从**构建时静态快照**改为**调用时动态生成** `_infoHtml(card)`(反映当前删除态)。

**② 拖图入卡只改 DOM 不落数据**:「单图拖到另一张卡=插入」(rc-voicecall `pointerup` ① 分支)原来只 `insertAdjacentHTML`,`card.data.items` 没加 → 三态重渲/回放/上下文注入全丢,元数据没真进卡。修:建卡时 `el.__vcCard = card`(侧栏 `_infoCardEl` + 浮层 `renderInfo`,同 cid 实例共享同一对象);拖入图卡 → `push` 到目标 `card.data.items`(带 url/aid/title 元数据 + `_added:1`)+ 各实例 append 标准 `.vc-ig-cell`(`_igWire` 委托自动接管 ✕/单选)+ 收藏夹 rec 重存 `_infoHtml`。非图卡兜底 DOM 塞入。手势:图卡横滑防冲突的 300ms 静止阈值改为**只拦明显横向快滑**(`adx>ady*1.3`),纵向拖(拖出/拖到另一卡)立即起拖。E2E:✕删图重建卡不带回、拖图入卡 data.items+1 且重建卡持久、元数据跟进,全过。

# 资产注册表(统一编号规则,2026-07-21 用户设计)

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

---
name: organize-into-skill
description: 用户按了侧栏的「保存为工具」、或口头说「把刚才那个存成工具/做成 skill」时使用：取那一轮的真实轨迹，写 flow.json，用 bw_skill_build.py 校验+生成+干跑，存成一个新 skill。默认静默做完，只有拿不准才问。不用于用户没要求保存的场合。
---

# 把一轮对话整理成可复用的 skill

**只在用户要求时做**（按钮或口头）。你不判断"值不值得存"，也不主动提议存——那是用户的决定。
默认**静默完成**，只有拿不准的地方（比如某一步该不该交给模型）才开口问一句。

## 1. 取轨迹（不要靠回忆）

按钮触发时通知里带 `turn=<turn_id>`；口头触发时用 `--last`：

    python "%LOCALAPPDATA%\BWReader\voice_turn_trace.py" --turn <turn_id> --out "%TEMP%\bw-trace.json"
    python "%LOCALAPPDATA%\BWReader\voice_turn_trace.py" --last --out "%TEMP%\bw-trace.json"

轨迹里有：用户原话 `user`、每一步 `steps[]`（`kind` mcp/web/command/file、`tool`、`args`、`output`、`ms`）、最终回答。
`kind=mcp` 的步骤才是可回放的工具；`web`/`command` 是当时的辅助动作，通常变成 `needs_ai` 步骤
（让模型现场再查一次），或者直接省掉。

## 2. 写 flow.json

目录：`~/.codex/skills/<name>/`，`name` 用 kebab-case，能一眼看出做什么。用户给了名字就用用户的。

```json
{
  "contract": "bw-reader-skill-flow/1",
  "name": "selected-word-to-card",
  "summary": "把当前选中的词查资料做成一张知识卡送到页面上",
  "trigger": ["把选中的做一张卡", "给这个词做张卡"],
  "steps": [
    {"id": "snap", "tool": "reader_context_snapshot", "args": {"brief": true}, "note": "读当前选区"},
    {"id": "research", "needs_ai": true,
     "prompt": "根据 snap 里 selectedItems 的词，查一段可靠资料，产出 reader_card 的 card 对象（kind=general，bind 到选中的 page-chars）",
     "input": {"selection": {"$from": "snap", "path": "selectedItems"}}},
    {"id": "card", "tool": "reader_card", "args": {"card": {"$ai": "research"}}, "note": "送卡"}
  ]
}
```

规则：
- `args` 里的字面量按工具的 inputSchema 校验；上一步的输出用 `{"$from": "<id>", "path": "a.b[0]"}` 引用
  （运行器会把 MCP 文本结果先 JSON 解析再取 path；要原始结果加 `"raw": true`）。
- 需要模型现场生成/判断的一步写 `needs_ai: true` + `prompt`，它的产物在后面用 `{"$ai": "<id>"}` 引用。
- 当时轨迹里参数是"那一次的字面量"（比如那张卡的正文）——**凡是下次会变的，都要改成 `$from`/`$ai`**，
  别把一次性内容固化进去。
- 不要发明工具名；lint 会对着工具面查。

## 3. 编、验、存（一条命令）

    python "%LOCALAPPDATA%\BWReader\skill-kit\bw_skill_build.py" "<skill目录>" --trace "%TEMP%\bw-trace.json"

它做四件事：lint flow.json → 生成 `run.js`（运行器 + flow 内嵌，自包含）→ 用轨迹当假工具干跑
→ 写/回填 `SKILL.md`。输出 JSON：`ok:false` 就按 `errors` 改 flow.json 再跑，**不要手改 run.js**。

## 4. 回一句

「已存为 `<name>`，触发词：…；步骤：…；其中 <哪步> 需要我现场做。」多余的话不用说。

## 以后怎么用

用户说了触发词 → 读该 skill 的 SKILL.md → 把 `<!-- bw-flow:run.js -->` 里那段原样作为 `exec` 输入执行。
运行器每步 `text` 一行 `[bw-flow] … ok`，`needs_ai` 处给 `bwFlowHandoff`，你按提示 `store` 结果后原样重跑。

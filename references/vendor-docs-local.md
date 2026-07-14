# 本地厂商参考库(`/home/bwicarus/refs/`)

**为什么存在**:2026-07-14 这一整轮里,我犯的错几乎全是**没查官方资料就动手**——
拿营销文的 TTS 价格算成本(错了 2 次)、以为 ToolSearch 关不掉(其实有 env 开关)、
把 token 数量比当美元成本比(结论完全反了)。用户裁定:**把官方 cookbook 拉到本地备用**。

⚠ **这些仓库在 git 之外**(`/home/bwicarus/refs/`,不进 `claude` repo,避免几百 MB 污染历史)。
新 session 要用官方资料时,**先 grep 这里,别急着上网**。

## 清单

| 目录 | 大小 | 内容 |
|---|---|---|
| `openai-cookbook/` | 144M | OpenAI 官方 cookbook(稀疏 checkout:`examples/voice_solutions` + `examples/agents_sdk` + `articles`) |
| `openai-realtime-agents/` | 5.4M | **官方 Realtime 参考应用**(Next.js):WebRTC 建连 / data channel / function call 循环 / barge-in |
| `openai-realtime-console/` | 528K | 官方 Realtime 控制台 |
| `xai-cookbook/` | 29M | xAI/Grok 官方 cookbook,含 `voice-examples/`、iOS/Android 语音示例 |

## 最该看的几个文件

```
openai-cookbook/examples/
  Realtime_prompting_guide.ipynb              ← 官方**提示词**最佳实践(instructions 怎么写)
  Context_summarization_with_realtime_api.ipynb ← 会话内压缩(我们 #285 就是照这个做的)
  Realtime_out_of_band_transcription.ipynb    ← out-of-band response(conversation:"none")
  Data-intensive-Realtime-apps.ipynb
  Realtime_eval_guide.ipynb
  voice_solutions/realtime_translation_guide.mdx
xai-cookbook/voice-examples/                  ← Grok 语音接法
openai-realtime-agents/                       ← 端到端参考实现,WebRTC 客户端照它抄
```

## 更新方法

```sh
cd /home/bwicarus/refs/openai-cookbook && git pull
cd /home/bwicarus/refs/openai-realtime-agents && git pull
cd /home/bwicarus/refs/xai-cookbook && git pull
```

## 已有的其它官方资料(在 repo 内)

- `references/REALTIME_2_1_API_GUIDE.zhCN.md` —— 我们自己整理的 Realtime 2.1 指南 + **全部实测结论**(比官方文档更贴我们的场景,而且纠正了官方文档几处不准的措辞)
- `references/volcengine-api-map.md` —— 火山/豆包全线 API
- `references/doubao-realtime-voice.md` —— 豆包 S2S
- `references/grok-voice-realtime.md` —— Grok 语音
- Claude 侧:`/claude-api` skill(内置,覆盖 Claude API + Managed Agents;**不覆盖 Agent SDK**,那个看 `code.claude.com/docs/en/agent-sdk`)

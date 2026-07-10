# 用 `claude` CLI 当 LLM 后端时，剥掉 Claude Code 的"壳"省 token

> 适用：把 `claude` CLI 当成程序化 LLM 后端（Agent SDK / headless / `--print` 管道），自管工具协议、不需要 Claude Code 那套编程 agent 外壳的场景。
> 数字均为**实测**（`--output-format json` 读 `usage`，隔离环境，非估算）。

## TL;DR
`claude --print` 默认会在每轮请求前塞一大堆你用不到的东西。用四个 flag 剥掉，**纯净默认壳从 ~17.5K token 砍到 ~130 token**（壳几乎清零），剩下的全是你自己写的必要系统提示。这是 Anthropic 官方为"当后端调用"留的口子，不是 hack。

## 默认壳里有什么（纯净隔离实测）
发一句极短消息（"Reply with exactly: ok"），用 `--output-format json` 读 `usage`，把 `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` 相加 = 这轮喂进模型的总上下文：

| 配置 | token | 说明 |
|---|---|---|
| **纯净零配置默认壳** | **17534** | 全新空 config + 空 cwd |
| └ Claude Code 核心系统提示 | 11507 | 任何调用都背，只有 `--system-prompt` 能替换 |
| └ 默认 agent 提示 + 21 个内建工具 schema | 6024 | |
| **剥壳后** | **132** | 四个 flag 全开 + 占位系统提示（<1024 cache 阈值 → 全 input、无隐藏 cache） |
| 项目根带大 CLAUDE.md | 40559 | 一个 CLAUDE.md 文件就 +21.5K |

> ⚠️ 量壳前务必隔离 config 目录（见下），否则你机器装的插件 / user-skills 会被算进去（实测污染约 +1.5K）。

## ⚠️ 重要：token 数量 ≠ 计费成本（prompt cache 的影响）
上面的 17.5K 是**喂进模型的上下文 token 量**，不直接等于"花的钱/额度"。因为 Claude Code 默认壳的相当一部分（实测约 **11.5K 核心系统提示**）是 **Anthropic 服务端的持久/共享热缓存**，即使你"第一次"调用也走 `cache_read`（≈0.1× 权重，便宜）。一旦你用 `--system-prompt` 换成自己的，就**脱离那个热缓存**，私有提示要自己 `cache_create`（1.25× 权重）冷建。

**所以剥壳省的"计费"取决于场景**（实测，加权 = 非缓存×1 + 写缓存×1.25 + 读缓存×0.1）：

| 配置 | 冷（首次） | 热（缓存命中） |
|---|---|---|
| 默认壳（带大 CLAUDE.md） | create 29181 + read 11507 → **37630** | read 40688 → **4072** |
| 剥壳（私有短提示） | create 1028 → **1288** | read 1028 → **106** |

**什么时候剥壳真省 / 什么时候不省：**
- ✅ **省**：调用重复、保持缓存热（如预热进程池）、固定壳占比大（累积上下文小）、默认壳还背着大 CLAUDE.md。
- ⚠️ **可能略亏**：大量**独立冷启动**（私有壳每次冷建 1.25×）+ 累积上下文是大头（90K+，壳占比小）+ 不带 CLAUDE.md。这时你把"全局热 cache_read"换成"私有 cache_create"，省不了多少甚至略贵，而真正的大头（多轮累积上下文）剥壳也动不了。
- **上下文窗口占用是绝对的**（17.5K→0.13K，跟 cache 无关）——长对话挤占可用窗口，这条永远成立。

先量自己的真实场景（下面「怎么量」连跑两次看冷/热），别只看 token 数就下结论。

## 四个剥壳 flag（逐项，各管一块）

```bash
claude --print --input-format stream-json --output-format stream-json \
  --setting-sources "" \
  --tools TodoWrite \
  --disallowedTools "TodoWrite Bash Edit Write Read NotebookEdit WebFetch WebSearch Glob Grep Task" \
  --system-prompt "<你自己的完整系统提示>" --exclude-dynamic-system-prompt-sections \
  --model sonnet
# 并且：进程的 cwd 设成「项目树外的空目录」
```

| 手段 | 去掉什么 |
|---|---|
| **cwd = 项目树外空目录**（不是项目根） | 不加载 `CLAUDE.md`（它从 cwd 向上遍历父目录找，所以空目录**必须在项目树外**，否则照样被父目录命中） |
| **`--setting-sources ""`** | 不加载 user/project settings + 插件（**OAuth 登录不受影响**，另走） |
| **`--tools <一个无害的> --disallowedTools "<列全部内建工具>"`** | 把 21 个内建工具 schema 砍到 1 个，且全禁用（模型一个都调不了 → 既省 token 又是沙盒，防 prompt injection 读 .env / 改文件） |
| **`--system-prompt "<你的>" --exclude-dynamic-system-prompt-sections`** | **替换**默认 agent 壳（那 ~11.5K 核心提示）+ 去掉动态 env 段。剩下的 token 就全是你自己的提示了 |

## 关键坑
- **`--bare` 别用**：它会连 OAuth 一起跳过 → "Not logged in"。auth 必须留着。
- **系统提示拆成「静态 + 动态」**：恒定的规则/工具目录走 `--system-prompt`（恒定 → 可被 prompt cache 复用、也能给预热进程预设）；每轮变的上下文（当前页/选区等）留在 user message。拆点选一个唯一锚字符串切现成 prompt，不挪文本。
- **量壳要隔离 config**：用 `CLAUDE_CONFIG_DIR=<只放 credentials 的空目录>` 跑，否则你机器的插件/skills 污染结果。日常剥壳运行不需要它（`--setting-sources ""` 已够）。
- **每次只改一个变量做对比**：`env -C <cwd>` 控 CLAUDE.md、`CLAUDE_CONFIG_DIR` 控 skills/插件 —— 干净归因每块省多少。
- **模型手写 JSON 工具调用不可靠**：若你让模型输出 `{"tool":...,"args":{...}}` 自管协议，字符串值里的 LaTeX（`\frac` `\leftrightarrow`）、未转义引号/换行会让 JSON 非法。解析器要顽强：`raw_decode` 只取开头第一个 JSON + 把字面控制字符换空格 + **把非法反斜杠转义 `\x`（x∉`"\/bfnrtu`）改成 `\\`** + 失败时反馈模型重出（≤2 次）。

## 怎么量（一条命令）
```bash
claude --print "Reply with exactly: ok" --output-format json --model sonnet
```
读返回 JSON 的 `usage`，三个输入项之和 = 总上下文（**API 官方计费数，不是估算**）；发极短消息把 output 压到≈0，配置之间的差值就是壳。

## 为什么不能改用 Skill 来"省 token"
Skill 的渐进披露省的是「在那 17.5K 默认壳**之上**多挂能力的边际」——它**必须活在那个壳里**（靠 settings 被发现 + 默认 agent 壳承载 + 内建工具执行步骤）。而剥壳是把那 17.5K 整个干掉，两者**互斥**。Skill ≈ 在大房子里省电；剥壳 = 直接搬进小屋。且 skill 靠模型用 Bash/Read 自执行，跟「全禁内建工具 + 服务端自己执行工具」的沙盒不兼容。

## 一句话原理
Claude Code 作为**交互式编程产品**时，那 17.5K 是通用 agent 的刚性成本（拆了就不是它了）；作为**被代码调用的后端**时，官方本来就支持你剥成 ~130 token。你用的是它的"后端人格"。

## Python subprocess 落地骨架
```python
import subprocess, os
ASST_CWD = "/tmp/your-empty-cwd"        # 必须在项目树外
os.makedirs(ASST_CWD, exist_ok=True)
cmd = ["claude", "--print",
       "--input-format", "stream-json", "--output-format", "stream-json",
       "--include-partial-messages",     # 要逐字流式就加
       "--setting-sources", "",
       "--tools", "TodoWrite",
       "--disallowedTools", "TodoWrite Bash Edit Write Read NotebookEdit WebFetch WebSearch Glob Grep Task",
       "--system-prompt", YOUR_STATIC_SYSTEM_PROMPT, "--exclude-dynamic-system-prompt-sections",
       "--model", "sonnet", "--effort", "low"]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     text=True, bufsize=1, cwd=ASST_CWD)
# 往 stdin 喂 stream-json 用户消息，从 stdout 读事件流；同一进程内可多轮（工具循环）。
```

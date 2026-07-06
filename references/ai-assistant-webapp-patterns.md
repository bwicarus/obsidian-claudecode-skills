# 在网页里集成 AI 助手 —— 可复用架构要点

> 这是一份**可迁移的模式手册**,提炼自一个真实项目(网页 PDF 阅读器 + 侧边栏 AI 助手,移动端为主)。
> 给"要做另一个带 AI 的网页"的人/AI 当蓝本:每节是「问题 → 方案 → 为什么 → 代码骨架 → 坑」。
> 不绑任何具体框架;后端示例用 Python(Flask 风格)+ 一个"模型进程"(CLI 或 HTTP API 均可),前端用原生 JS。
> 术语:**编排器/orchestrator** = 主回答模型;**工具/tool** = 模型可调的能力;**rid** = 一次生成任务的 id。

---

## 0. 心智模型:四条数据通路

一个网页 AI 助手本质是四条通路,分别有各自的"别绑死"原则:

1. **生成通路**(用户问 → 模型答):**不要绑死在 HTTP 请求生命周期上**(见 §1)。
2. **网络通路**(浏览器 ↔ 服务端):移动端会随时掐断,**底层兜重试**(见 §3)。
3. **状态通路**(对话历史/上下文):**服务端持久化**,前端只是视图(见 §4)。
4. **渲染通路**(文本/公式/markdown):**节流 + 公式与 markdown 解耦**(见 §2、§5)。

下面每节都在加固其中一条。**§1 是这套东西和"普通 demo 级 AI 网页"最大的区别,优先看。**

---

## 1. ⭐ 生成与请求解耦:detached worker + rid 重连(最重要)

**问题**:最常见的写法是"一个 `/chat` 请求里同步跑模型、用 SSE 边生成边吐"。手机切后台/锁屏/网络抖一下 → 浏览器掐断这个请求 → 服务端的生成器收到断开就**被杀**(Python 里是 `GeneratorExit`)→ 答案没了 → 只能让用户"刷新重问"。这是 demo 级 AI 网页的头号坑。

**方案**:把生成**从请求里搬出来**,放进一个**后台任务**,用客户端给的 `rid` 编号。请求只是"尾随(tail)这个任务的事件缓冲区"。客户端断了,任务**照常跑完并落库**;客户端回来用**同一个 rid + 已读事件数 `from`** 重新连上,从断点续读。全程零"刷新"。

**为什么**:模型生成是"长耗时、有价值、和某个 TCP 连接无关"的工作。把它的生命周期跟一个随时会断的连接绑在一起,是错配。这跟"上传大文件要支持断点续传"是同一个道理。

**服务端骨架**:
```python
_jobs = {}          # rid -> {events, answer, done, lock, uid}
_jobs_lock = Lock()

def _worker(rid, message, ctx, uid):
    job = _jobs[rid]
    try:
        for ev in run_agent(message, ctx):       # 你的生成逻辑,yield {event,data}
            with job["lock"]:
                job["events"].append(ev)
                if ev["event"] == "answer": job["answer"] = ev["data"]
    finally:
        with job["lock"]:
            job["events"].append({"event":"done","data":{}}); job["done"] = True
        if job["answer"]:
            save_to_history(uid, job["answer"])   # 不管客户端在不在,跑完就落库
        Timer(180, lambda: _jobs.pop(rid, None)).start()   # 留几分钟给重连,再清

@app.post("/chat")
def chat():
    body = request.json
    rid  = body.get("rid") or gen_id()
    frm  = int(body.get("from") or 0)             # 重连:从第几个事件接着读
    with _jobs_lock:
        job = _jobs.get(rid)
        if job is None:                           # 新任务
            save_to_history(uid, body["message"], role="user")   # ⚠ 进模型前就存(见 §4)
            job = _jobs[rid] = {"events":[], "answer":"", "done":False, "lock":Lock(), "uid":uid}
            Thread(target=_worker, args=(rid, body["message"], body.get("context"), uid), daemon=True).start()
        elif job["uid"] != uid:
            return {"error":"forbidden"}, 403      # 别人的 rid 不给读
    def gen():
        yield sse("meta", {"rid": rid})            # 把 rid 回给前端(meta 不计入缓冲)
        i = frm
        while True:
            with job["lock"]:
                n = len(job["events"]); evs = job["events"][i:n]; done = job["done"]
            for ev in evs: yield sse(ev["event"], ev["data"])
            i = n
            if done and i >= n: return
            time.sleep(0.1)                        # 轮询缓冲;客户端断了这个 gen 自然结束,worker 不受影响
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})  # X-Accel-Buffering 关 nginx 缓冲
```

**客户端骨架**(关键:断了**自动重连**,不弹"请刷新"):
```js
async function send(text) {
  const rid = 'c' + Date.now() + '_' + (ridCtr++);
  let evSeen = 0, done = false, answer = '';
  const handle = (ev, data) => {
    if (ev === 'meta') return;                    // 不计数
    evSeen++;
    if (ev === 'done') { done = true; return; }
    if (ev === 'answer') { answer = data; render(answer); }   // answer 是**累积快照**,直接覆盖
    // ... tool / error / 其它事件
  };
  const once = async (body) => {
    const r = await fetch('/chat', {method:'POST', body: JSON.stringify(body), signal: ctrl.signal});
    if (r.status === 410) { done = 'gone'; return; }   // 任务已过期 → 走历史恢复
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = '';
    for (;;) { const {value,done:d} = await reader.read(); if (d) break;
      buf += dec.decode(value,{stream:true});
      let i; while ((i = buf.indexOf('\n\n')) >= 0) { const chunk = buf.slice(0,i); buf = buf.slice(i+2);
        const {ev,data} = parseSSE(chunk); handle(ev, data); if (done) return; } }
  };
  let tries = 0;
  while (!done && !aborted) {
    try { await once(tries === 0 ? {message:text, rid} : {rid, from: evSeen}); }
    catch (e) { if (e.name === 'AbortError' && userStopped) { aborted = true; break; } }   // 用户主动停才算 abort
    if (done || aborted) break;
    if (++tries > 40) break;
    await whenVisible();                          // 等回到前台再重连(后台重连也会被掐)
    await sleep(Math.min(400*tries, 2000));       // 退避
  }
  if (!answer) answer = await recoverFromHistory(rid);   // 兜底:任务过期/没续上 → 拉服务端历史(已落库)
}
```

**坑**:
- **`answer` 事件设计成"累积快照"**(每次是到目前为止的**完整**文本),不是增量 delta。这样重连只要拿到最新一条 answer 就完整了,不用拼。
- `meta` 事件**不进缓冲区**(每次连接都重发一次,用来回传 rid),否则 `from` 计数会错位。
- 服务端 `gen()` 里**别忘 `time.sleep`**,否则 1 worker 的 gunicorn 会被忙轮询占满。
- 任务清理用**定时器延迟清**(留几分钟给重连),清早了重连就 410 → 退回 §4 历史恢复。

---

## 2. 流式 = SSE + 渲染节流

**问题**:边生成边对**整段**文本重渲 markdown/MathJax,长答案末段会二次方级卡顿(每来一个字就重排几千字)。

**方案**:SSE 逐字吐;前端**节流 ~100ms** 才重渲一次,且流式期间**只轻量渲(不跑 MathJax)**,**收尾再完整渲一次 + 跑一次 MathJax**。
```js
// 流式中:
if (now - lastRender > 100) { lastRender = now; renderMd(el, text, /*withMath=*/false); }
// 收尾一次:
renderMd(el, finalText, /*withMath=*/true);
```
**坑**:SSE 用 `\n\n` 分隔事件;`data:` 可能多行要拼;`event:`/`data:` 行前缀解析。nginx 反代要 `X-Accel-Buffering: no` + 关 proxy buffering,否则不是"流式"是"一次性"。

---

## 3. 网络韧性:在最底层包一次 `fetch`

**问题**:移动端切后台会**掐死进行中的请求**,回来报 `TypeError: Load failed`。逐个 `try/catch` 改几十个调用点既累又漏。

**方案**:包装全局 `window.fetch`,**幂等读(GET/HEAD)瞬断时等回前台再自动重试**;**写(POST)不自动重试**(防重复提交);**主动取消(AbortError)不重试**。一处生效,覆盖所有现有+将来的请求。
```js
(function(){
  const orig = window.fetch.bind(window);
  const whenVisible = () => new Promise(res => {
    if (document.visibilityState !== 'hidden') return res();
    const h = () => { if (document.visibilityState !== 'hidden') { document.removeEventListener('visibilitychange', h); res(); } };
    document.addEventListener('visibilitychange', h);
  });
  async function run(input, init, max){ let i=0; for(;;){ try { return await orig(input, init); }
    catch(e){ if (e.name === 'AbortError') throw e; if (i++ >= max) throw e;
      await whenVisible(); await new Promise(r=>setTimeout(r, Math.min(300*i,1200))); } } }
  window.fetch = (input, init) => {
    const m = (init?.method || input?.method || 'GET').toUpperCase();
    return run(input, init, (m==='GET'||m==='HEAD') ? 3 : 0);   // 写请求 max=0
  };
})();
```
**关键边界(必须懂)**:`fetch()` **只在"还没收到响应(连接失败/被掐)"时 reject**;一旦返回 `Response` 就交还调用方 —— 所以**流式响应 body 读到一半断了不归这层管**,那是 §1 的 rid 重连负责。这两层是互补的,别想用一个解决全部。

---

## 4. 对话/状态持久化在服务端

**原则**:
- **用户消息进模型之前就落库**(不是答完才存)。这样断连/锁屏也不丢"这一轮"。
- **助手回答在 worker 的 `finally` 落库**(完整或到断点)。
- 前端**不存对话数组**,开页面时拉 `/history` 渲染 → 天然跨设备。
- 失败兜底 `recoverFromHistory()`:重连续不上时,拉历史里这轮的回答(worker 已存)。

**坑**:存历史时连**调用轨迹/元数据**一起存(见 §6 的 trace、§8 的选区/页码),否则"历史回看"功能(如反馈弹窗、上下文卡片)拿不到数据。读历史文件做**原子替换 + 坏文件备份**,别让一次半截写毁掉整个历史。

---

## 5. ⭐ Markdown 与数学公式共存(高频隐蔽坑)

**现象**:AI 回答里的公式不渲染,显示成裸 `$...$` 或乱码上下标。**两个叠加根因,都要修**:

**根因 A:模型没用 LaTeX**。在"口语聊天"语气下模型常把公式写成纯文本 / 反引号 `` `x^2` `` / Unicode 上标(x²)。
→ **系统 prompt 硬性要求**:数学一律 `$...$`/`$$...$$`;**禁反引号包数学**(会被当 `<code>`,而 MathJax 默认 `skipHtmlTags` 含 `code` → 跳过不渲染);**禁 Unicode 上下标**。

**根因 B:markdown 解析器破坏公式**。`marked.parse()` 会把 `$P(A_1)P(A_2)$` 里的 `_` 当斜体、`*` 当强调、`\` 当转义拆烂 —— 即便模型写对了 `$...$` 也渲染失败。
→ **占位符法**:跑 marked 之前先把 `$$..$$`/`\[..\]`/`$..$`/`\(..\)` 整段抠成占位符(如 `@@MJX0@@`,纯字母数字 marked 不动),marked 跑完再换回原公式交给 MathJax。
```js
function md(s){
  const math=[], hold=m=>'@@MJX'+(math.push(m)-1)+'@@';
  let t = s.replace(/\$\$[\s\S]+?\$\$/g,hold).replace(/\\\[[\s\S]+?\\\]/g,hold)
          .replace(/\$(?!\s)(?:\\\$|[^$\n])+?\$/g,hold).replace(/\\\([\s\S]+?\\\)/g,hold);
  let html = marked.parse(t);
  return html.replace(/@@MJX(\d+)@@/g,(_,i)=>math[+i]);   // 还原,交给 MathJax.typesetPromise([el])
}
```

**MathJax 配置**(放在加载前):
```html
<script>window.MathJax={tex:{inlineMath:[['$','$'],['\\(','\\)']],displayMath:[['$$','$$'],['\\[','\\]']]},
  options:{skipHtmlTags:['script','noscript','style','textarea','pre','code']}};</script>
```
**坑**:① 别忘了**追问 chip / 按钮里的公式也要 `MathJax.typesetPromise([那个元素])`**(只渲主回答会漏)。② 此类修复对**历史回答 retroactive**——老答案重载即正确渲染。

---

## 6. 模型/算力分档 + 反馈闭环

**原则**:**编排器用快模型,真正的内容生成步用强模型**。比如导航/路由/简单问答走便宜快模型;"总结整章/深度解释"在工具内部临时起一个强模型一次性出结果。别整条链都用最贵的。

**反馈机制**(让用户校准):每条回答挂一个「!」按钮,点开显示**这条回答经过的调用链**(每步:任务名 · 模型 · 耗时 · 完成时刻),再给两个动作:
- 「答得不够好」→ 沿**能力梯子**升一档重答 + 把该动作的预设钉到更高档;
- 「太慢了」→ 把该动作的预设调到**同质量更快**的档(不重答,只影响以后)。

**能力梯子要按 Pareto 清洗**(实测得来的硬经验):
- 实测 `大模型·低思考` 可能 ≈ `小模型·高思考` 的质量但**更快** → 后者被"帕累托支配",梯子里**直接换掉**它,别两个都放(否则"更强"按了反而更慢不更好)。
- 比小模型还弱的(如最小档)**不进"向上"梯子**,它只属于"更快"方向。
- 自动梯子要**每一级严格更强**;细粒度(各 effort 档)可在手动 ⚙ 里给,自动梯子保持少而清晰。

**按动作存预设**:不同动作(回答 / 总结 / 翻译)各存一份 `{model, effort}`;**导航/写动作恒走快档,不被预设拖慢**(保住秒回手感)。优先级:`一次性强制(重答) > 用户预设 > 系统默认`。

**坑**:墙钟时延会被"输出长度"和"进程冷启动"双重污染,**比快慢别只看单次墙钟**——要么固定输出长度、要么量吞吐(字/秒);"大模型在硬推理题上即便低思考也想得更久"是常态。

**多后端 × 三维(后端/型号/深度)可配置**(把"要不要换别家模型"做成设置而非写死):每个 AI 任务(编排 agent / 章节总结 / 看图)各存 `{backend, variant, depth}`——**后端决定后两维的可选集**(Claude=haiku/sonnet/opus × effort low..max;Gemini=flash/lite/pro × 思考开/关)。
- **叶子调用**(单次、无工具循环:总结/看图)切后端**几乎零成本**——两条后端路本就都在,改成"按配置选主后端 + 另一后端兜底"即可(主后端 429/故障自动用另一边,功能不中断)。
- ⚠ **同后端双 key failover 要"穷尽本后端再回退"**:Gemini 免费+付费两把 key,免费失败时**任何错误(限流 429/403、5xx 临时高负载、网络超时、构造错 400)都必须先切付费 key**,只有付费也挂才回退 Claude。**别只对 429/403 切、其它 `return None`**——实测默认型号 `gemini-3.5-flash` 会撞 `503`(high demand),若 503 直接回退 Claude 就等于付费 key 永远没机会(用户原话"免费不可用时应该用付费而不是直接跳 Claude")。① 非流式:循环里**任何非 200 都 `continue` 试下一把**,网络异常也 `continue` 不 `return`;② 流式:加 `emitted` 标志——**只在还没 yield 过任何 delta 时**才换 key 重试(已吐内容再换 key 会重复输出),已吐则只能 `yield err` 收尾;③ 5xx/网络异常**不给整把 key 设冷却**(是型号临时问题不是 key 坏),只有真限流(429/403)才 cooldown 整把。见 `_gemini_text`/`_gemini_vision`/`_gemini_stream`。
- **根 orchestrator** 换后端要把**整条工具循环**搬过去:系统提示/工具协议(裸 JSON)/SSE 事件/trace **全复用**,只把"Claude 进程多轮(进程内保历史)"换成"Gemini 无状态多轮 `contents`(每轮带完整历史数组)";看图工具的图用 `inlineData` 喂回;首轮失败回退另一后端保不挂。
- **换后端前先低成本验证**新模型在你那套手搓 JSON 工具协议上的稳定性:① JSON 工具调用格式正确率 ② 工具选对 ③ 多轮收敛 ④ 复合任务分解。实测 Gemini Flash·thinking 这四项全过(JSON 0 坏、复合"总结再做笔记"正确拆两步)才放心铺开。**复用生产的系统提示/解析/工具表写个离线验证脚本**(写类工具 stub 掉防副作用),别直接拿生产开刀。
- **向后兼容**:旧的两维 `{model, effort}`(无 `backend`)读出时视作默认厂商(claude);感叹号「更强重答」= 一次性强制升档,跟设置面板的常设默认**共用同一套 resolve**(优先级:强制 > 预设 > 出厂默认)。
- ⚠ **坑(Gemini Pro)**:`gemini-2.5-pro` 是 **thinking-only**,发 `thinkingConfig.thinkingBudget=0`(关思考)直接 **400**。底层构造 generationConfig 处必须守卫「型号含 `pro` → 永不发 `thinkingBudget=0`」(只有 Flash/Lite 能关思考)。否则一旦"看图"(常写死关思考省 token)或"导航/简单问题"(自动关思考)用了 Pro 就挂。Pro 还慢且贵,日常 orchestrator 用 Flash,Pro 留给偶尔要最强推理。
- ⚠ **坑(免费档"不支持"被伪装成临时 429)**:用 ListModels 动态列型号时,**别假设列出来的免费 key 都能用**。Google 对"免费档根本不含此型号"(全部 `*pro*`、连 `2.0-flash` 系)返回的不是干净的 404/403,而是**伪 429**——正文写「Please retry in 49s」装成临时限流,但 quota 明细里写死 `generate_content_free_tier_requests, limit: 0`(每日上限就是 0,重试永远没用)。① 判"永久不支持"的函数必须识别 `free_tier` + `limit: 0`(否则当临时冷却,面板永远把 pro 标"免费可用");② 真·RPM 限流的 limit 是非 0(如 `limit: 5`)→ 才走临时冷却;③ 别盲信 404(网关/URL 问题也会 404),判定以**正文语义**为准。
- ⚠ **主动探测免费档支持**(别等用户点了才发现):面板列型号前对"没探过"的型号各发一个 `maxOutputTokens:1` 的免费 key 试探请求(并发 + 持久缓存 30 天 → 之后命中缓存 0 请求),200=可用、`free_tier limit:0`=隐藏、503/限流=保持 unknown 下次再探。免费档不计费,1 token 即便被截断只要 200 也证明"放行了该型号"。**实测 17 个型号里 9 个免费 limit:0**(5 个 pro + 4 个 2.0-flash 系)被正确隐藏;flash 系若撞 503(high demand)保持 unknown→乐观显示"免费",下次探测确认。见 `_probe_free`/`_probe_free_batch`/`_free_state` + `state/gemini-free-ok.json`/`gemini-unsupported.json`。
- ⚠ **trace 要记「实际用了哪档」,不是「设了哪档」**:多 key/多后端下,设的是免费 Gemini 但可能实际走了付费(免费限流时切付费)或回退了 Claude。① 流式函数成功放行时 `yield ("tier", tier)`,编排器收到写进 `trace[0].tier`(free/paid)→ 感叹号弹窗给模型名旁标「免费/付费」彩色 chip,用户一眼知道这条到底烧没烧钱;② 回退到另一后端时 trace 写明「A→B」(见上条 fallback_from)。**别让前端拿"当前设置/当前状态"去推断**——那反映的是现在、不是这条回答当时实际发生的。
- ⚠ **每个动作的「调档入口」只留一个统一面板**:别同时存在「行内简易快捷档(只列某后端)」+「完整三维设置面板」两套——简易档迟早跟多后端打架(本例行内 ⚙ 只列 Claude haiku/sonnet,用户设了 Gemini 进去却只能选 Claude,且把 `3.5-flash·think` 误解析成 haiku)。统一成:行内 ⚙ 直接打开那个完整面板并**定位/高亮到对应动作**(`openModelSettings(focusAction)` + `scrollIntoView`)。一处真相,自动支持新后端 + 免费/付费标。
- 见 `_server_deploy/assistant.py`:`_resolve` / `_deep_ask` / `_vision_describe`(叶子)+ `_agent_run`→`_agent_run_claude`/`_agent_run_gemini`(根)+ `_gemini_stream` 的 `("tier",tier)` 上报 + `scripts/verify_gemini_orchestrator.py`(验证脚本);前端 `static/pdf/reader.src/25-assistant.js` 的 `_buildFbPop`(感叹号弹窗 trace+tier chip)/ `openModelSettings(focusAction)`。

---

## 7. Agentic 工具循环(若助手要"能做事"而不仅聊天)

**架构**:一轮对话起一个模型进程,**自管 JSON 工具协议**——模型输出 `{"tool":"名","args":{...}}` → 服务端执行 → 把【工具结果】喂回 → 模型决定继续调工具还是给最终回答。复合请求(如"总结这页再做成卡")靠它逐个执行。
```
系统prompt:能答就直接输出中文;要调工具就**整条消息只输出一行 JSON**。
循环:发 content → 读模型输出 → 是 JSON 工具调用?执行+把结果喂回:否则=最终回答,结束。
```
**要点/坑**:
- **沙盒**:禁掉模型自带的一切内建工具(Bash/读写文件/联网),只走你注册的 JSON 工具 —— 防 prompt injection(用户输入是不可信的)。
- **JSON 解析要顽强**:用 `raw_decode` 只取开头那个 JSON(容忍尾部多余字)+ 把字面控制字符(没转义的换行)换空格 + **自愈重试**(看着像工具调用却解析失败 → 反馈给模型让它重出一条合法 JSON,≤2 次)。否则"工具 JSON 被当成回答显示、工具没执行"。
- **步数上限设很高当 runaway 兜底,真正的护栏是总超时**(防卡死的模型进程占住 worker)。高思考档要**放宽单轮/总超时**,否则深答被腰斩成"没响应"。
- 写操作(制卡/存笔记/改高亮)给**撤销**:记 `owner=用户id`,撤销只能撤自己的。
- ⚠ **破坏性/批量操作 → 不让 AI 直接执行,改「列清单 + 每项带按钮,人来点」**:用户说"把这章高亮全删了",别真让模型批量删(误删难恢复、模型可能删错范围)。做法:工具只**查出匹配项**返回给模型(让它措辞),同时附一个 `client_action` 让前端把每项**逐条渲染成可操作行**(本例每条高亮一行:色块+文字+「↗跳转」+「🗑删除」),用户**自己点删/点跳转去看**——human-in-the-loop。复用已有的「撤销项」渲染/点击委托机制(同一个 thread 级事件代理认 `.asst-jump`/`.asst-xxx-del`),删完即时 `_reloadHighlights` 视觉移除。比"AI 直接删 + 给个总撤销"更安全可控,也省得模型纠结"我有没有这个工具"。见 `_t_find_highlights`(后端列清单)+ `window._showHlPicker`(前端渲染)+ `DELETE /pdf/api/highlights`。配套:工具描述里**明确写「这就是删X的工具,别说没有」**,并在系统 prompt 加一条对应规则,否则模型会答"我没有批量删除的工具"。
- ⭐ **助手操作『多来源拼接的合集』(collection/收藏集)时:给它「翻回原书」的工具 + 条件注入**。合集(如从不同书/位置精选的页面物化成的一本文档)条目间**不连续、各有原书出处**,普通「前后 section 连续」假设失效。做法:① 系统提示动态块声明它是合集 + 给目录概览(每条**出处/首句/相邻标记**,让 AI 别把邻条当连续正文);② 给一个 `read_source_page` 工具让助手**翻回原书**读任意页/节补前后文/查证(按合集条目 idx 或 book+page 定位、offset 读前后)。**该工具只在当前确实是合集时才并入执行注册表 + 在系统提示里广告**(普通文档模型压根看不到 → 零影响、也不稀释选工具注意力),跟 §12「按前置条件才暴露」同源——把它推广到**工具**层,不只 prompt 规则。见 `epub_assistant.py::_t_read_source_page` / `_fav_meta_for` / `_fav_sys_block` / `_tool_fn`(合集判定 `_fav_fid`)。

---

## 8. 上下文注入 + "只在助手开着时才收集"

**原则**:网页里的选区/图/当前页/钉住的焦点,**汇总成一个 context 对象**随消息发给后端,系统 prompt 里说明这些字段(如"用户当前选中:「…」""本页知识点:…")。

**关键门控(用户体验)**:**只在 AI 对话栏真正打开时,才把"点选/多选/带入图"加进上下文**。没开 AI 栏时点选只做查词/翻译/高亮,**别悄悄往对话攒东西**(否则用户一打开助手发现一堆没要的上下文)。

⚠ **页码/坐标在边界统一换算,且「每一个」吃页码或吐页码的工具都要做**:内部数据(高亮/图/历史)按 PDF 原始页存,但给 AI 看 + 用户看的是**印刷页**(差一个 `page_offset`)。系统 prompt 把可见页转成印刷页喂给 AI → AI 之后所有 `page=N` 都是印刷页。**漏一个工具没转就出诡异 bug**:本例 `highlight`/`goto` 转了、`read_highlights`/`find_highlights` 没转 → 用户明明看到高亮、AI 却说"这章没有高亮"(AI 拿印刷页 4 去比 PDF 页 24)。规则:**输入** AI 的 `page`→`_to_pdf` 转 PDF 再比/查;**输出**给 AI/显示的 `page`→`_to_disp` 转印刷页;但**跳转/定位**要的仍是 PDF 页(`jumpWithBack` 收 PDF 页)→ 列表项同时带 `page`(印刷,显示)+`pdf_page`(原始,跳转)。默认"当前页"用 `ctx.pages`(已是 PDF 页)不转。写新工具时:**只要签名里有 page,就先想清楚它是哪种页**。
```js
function asstOpen(){ const p=document.getElementById('panel'), a=document.getElementById('asst-pane');
  return !!(p?.classList.contains('open') && a?.classList.contains('active')); }
function setFocus(text){ if(!asstOpen()) return; /* 否则才钉入上下文 */ }
```
**坑**:① 选区会"跨页陈旧"——发消息时要校验选区还在当前可见内容里,别把上一页的选中带过来。② 显式的"问 AI"按钮是另一条路(用户明确意图),别被这个门控误伤。

---

## 9. 后台长任务 + 进度(非对话类,如批量处理)

**模式**:点按钮 → 起一个 **detached 子进程**(关页面/重启不中断)+ 写 **pid 文件防重复启** + 一个 **status 端点**轮询进度。前端进度条轮询直到进程结束。
```python
@app.post("/job/start")          # 已在跑(pid 活着)→ 不重复启;否则 Popen(detached, 低优先级) + 写 pid 文件
@app.get("/job/status")          # 从产物实时统计 done/total + pid 是否活着
```
**坑**:幂等(只补没做的)+ **每批原子写回**(断了重跑接着做);后台任务**降到低优先级**(`nice`),别抢满 CPU 拖慢交互请求。

---

## 10. 额度/成本护栏:只告警不阻断

后台周期查实时额度(**非阻塞**:守在快照里,请求端只读),近上限时给前端一句提醒条(不覆盖回答),**绝不自动降级到别的后端 / 不打断**——除非用户明确要。把"省钱"做成可选,别擅自降质量。

### 10b. ⭐ 用 `claude` CLI 当后端时,每轮先剥掉 Claude Code 那套壳(实测省 87% 输入 token)

把 `claude --print` 当 agent 后端(走 stream-json + 自管 JSON 工具协议)时,它**默认会在系统前缀塞一大堆你根本用不到的东西**:项目 CLAUDE.md、默认 agent 系统提示、21 个内建工具 schema、user/project 设置 + 插件、动态 env 段。本助手只走自管协议、禁了所有内建工具,这些全是白付。实测(同句 "hi",sonnet)逐项剥:
- **`cwd=项目树外空目录`**(不是项目根)→ 不加载 CLAUDE.md(从 cwd 向上遍历父目录找,所以空目录**必须在项目外**,子目录照样命中):33768 → 12249。
- **`--setting-sources ""`** → 不加载 user/project 设置 + 插件(**登录不受影响**,OAuth 另走):→ 10981。
- **`--tools <一个无害的>` + `--disallowedTools 全禁`** → 把 21 个内建工具 schema 砍到 1 个(模型一个都用不了,沙盒仍在):→ 6965。
- **`--system-prompt <你自己的系统提示>` + `--exclude-dynamic-system-prompt-sections`** → **替换**默认 agent 壳(~6.8K)+ 去掉动态 env 段:→ **4468**,其中 4465 就是你自己的提示(必要),Claude Code 开销≈0。
- 拆法零风险:你的系统提示本就分「静态规则 + 动态上下文」,把静态走 `--system-prompt`(恒定→可缓存、预热进程也能预设)、动态留 user message。我们按唯一锚 `rfind("【当前页面】")` 切现成的 prompt 输出,不挪文本。
- ⚠ `--bare` 会一并跳过 OAuth → "Not logged in",**别用**;auth 要留着。`--deep_ask` 那种「生成步」调用不传 system(各有自己的 prompt),也顺带免掉 agent 壳。
- 见 `_server_deploy/assistant.py::_spawn`(commits a4071fb/4a06619/aba9273)。

**纯净隔离复测(2026-06,别被上面逐 flag 链的数字误导)**：上面那条链是「逐项剥」的增量，且会被你机器上装的插件/skills 污染。用全新空 `CLAUDE_CONFIG_DIR`（只复制登录凭证、不带任何 settings/插件/skills）+ 空 cwd 重测，拿到干净的「地板」：
- **纯净·零配置默认壳 = 17534 token**（Claude Code 核心系统提示 11507 + 默认 agent 提示 & 21 个内建工具 schema 6024）。我机器上带 engineering 插件 + 5 个 user skills 时测出来是 19038 → **污染约 1504**（量壳大小前务必隔离 config 目录，否则把自己的插件算进去）。
- **剥壳骨架 = 132 token**（占位短系统提示 + 那句消息；< prompt cache 1024 阈值故全 `input`、无隐藏 cache）→ 即剥壳几乎把整个 17534 的壳**清零**，助手的 ~4468 几乎 100% 是你自己的必要提示，Claude Code 开销≈0。
- **怎么量**：`claude --print "Reply: ok" --output-format json` 回的 `usage` 里 `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` 之和 = 这轮喂进模型的总上下文（API 官方计费数，非估算）；发极短消息把 output 压到≈0，差值就是壳。`env -C <cwd>` 控 CLAUDE.md、`CLAUDE_CONFIG_DIR` 控 skills/插件，每次只动一个变量干净归因。
- **为什么不能改用 Claude Code 的 Skill 来「省 token」**：skill 的渐进披露省的是「在那 17534 默认壳**之上**多挂能力的边际」，它**必须活在那个壳里**（靠 settings 被发现 + 默认 agent 壳承载渐进披露逻辑 + 内建工具执行步骤）；我们恰恰把那 17534 整个剥成 132，两者**互斥**。skill 的省≈在大房子里省电，我们的省=直接搬进小屋。且 skill 靠模型用 Bash/Read 自执行，跟我们「服务端 Python 执行 JSON 工具 + 全禁内建工具」的沙盒**不兼容**。**剥壳那几个 flag 本就是 Claude Code 官方为「当后端调用」(Agent SDK/headless) 留的口子——我们用的是它的「后端人格」，不是 hack**；它作为交互编程产品时必须背全壳（通用 agent 的刚性成本，拆了就不是 Claude Code），被代码调用时本来就支持剥成 132。

**2026-06-26：脱壳从 PDF 助手扩到全项目所有 `claude` CLI 调用点 + 加 Gemini 兜底**。此前只有 `assistant.py::_spawn`(PDF 阅读器助手)脱了壳，其余两个 claude 入口仍背全壳每次白吃 CLAUDE.md：
- **`_client/core/ai_backends.py::ClaudeCli`**（QA 截图问答 创建笔记/Anki 制卡询问/对话 + KG 脚本 + vocab 翻译 + skilltree 共用）：原 `cwd` 继承服务 `WorkingDirectory=…/claude`（吃整本 CLAUDE.md）、无 `--setting-sources`。改：`cwd=_STRIP_CWD`（`/tmp/bwicarus-cli-cwd`，项目树外空目录）+ `--setting-sources ""`。**带图仍保留 `--allowedTools Read`**（图是按路径让模型 Read 的，去了读不到图）→ 不做工具/system-prompt 那两步剥，只吃 cwd+setting-sources 这 ~64% 的大头，零风险。`chat`/`chat_stream` 末尾加 **Gemini 兜底**：claude 失败/限流/空 → `_gemini_chat()`（纯 stdlib urllib，免费 key 优先付费兜底，含图 inline base64，型号 `settings.gemini_model` 默认 `gemini-3.5-flash`）。
- **`scripts/ai_client.py::claude_raw`**（Anki 制卡/summarize/connect/薄弱卡改写 + nightly daily 共用，唯一 claude 入口）：原 `cwd=PROJECT`。改 `cwd=CLI_CWD`（同 `/tmp/bwicarus-cli-cwd`）+ `--setting-sources ""`。**`--continue`(多轮)按 cwd 维护会话，cwd 固定不变故续写照常**。这些 prompt 全自带所需内容、不依赖项目记忆，故剥 CLAUDE.md 零损失。未加 Gemini 兜底（已有 codex `auto-claude→codex` 路由兜底，且多轮 `--continue` 与无状态 Gemini 不对应）。
- 为何不强剥 `--tools`/`--system-prompt`：这两条入口是「通用问答/制卡」，依赖 Claude **默认**系统提示与（带图时的）Read 工具，不像 PDF 助手有自管 JSON 协议可整壳替换。只做 cwd+setting-sources 是「安全大头」。
- `_STRIP_CWD`/`CLI_CWD` 都解析到 `/tmp/bwicarus-cli-cwd`（`tempfile.gettempdir()` 派生，跨平台）。Gemini key 文件复用 PDF 助手那组 `~/.config/gemini-api-key{,-free}`。

---

## 11. 移动端(iOS Safari)专项坑

- 切后台**掐死 fetch + 暂停 JS 定时器** → §1 rid 重连 + §3 fetch 韧性 + "回前台看门狗"(回来 N 秒无新进度就主动 abort 死流去重连)。
- **语音输入直接用系统键盘的听写**(iOS=Siri 级),别自造 STT;持续聆听靠 `onend` 自重启,但要管好"总时长软上限 + 空转退避 + 会话令牌防迟到结果回填"。
- 截图上传:HEIC 要服务端转 PNG。
- 滚动链:浮层/侧栏滚到头会把滚动**漏给底下内容** → `overscroll-behavior: contain` + `touch-action: pan-y`。
- 整页导航前**先画一帧反馈**(iPad 上整页跳转首绘前老页面冻住,没反馈像死机),再拆 DOM 再真正导航。

---

## 12. 系统 prompt 设计清单

- **角色 + 语气**一句话定调。
- **工具协议**:何时输出 JSON、格式、"整条消息只输出一行 JSON"。
- **复合请求必须每步做完**("别只做第一步就停")。
- **数学格式硬规则**(见 §5)。
- **可溯源**:引用具体内容标来源(页码/出处),且"不许编"。
- **追问建议**:回答末尾按固定标记给 2-3 个下一步问题(前端解析成可点 chip);注意**模型常漏闭合标记**,解析要容忍未闭合。
- **把动态上下文(选区/页/知识点)拼在 prompt 末尾**,固定指令在前。
- ⭐ **按前置条件动态拼 prompt(省 token + 提高选工具准确率)**:有些规则**只在特定条件下才相关**(选中处理→有选中才用;see_page 收紧→本书有插图才用;手写笔迹→本页有墨迹才用)。把它们**从恒定的系统提示移到「每轮动态块」,条件命中才加**——prompt 更纯净、模型注意力不被无关工具规则分散(选工具更准),无关轮也省 token。**关键约束**:系统提示(`--system-prompt`)必须**恒定**(否则破 prompt 缓存,每轮付全价),所以**条件规则只能放进 message 的动态块,绝不能放进随条件变的系统提示**。我们的 `assistant.py`:`_sys_static()`=恒定通用规则(身份/安全守卫/工具目录/格式)走系统提示;`_ctx_block(ctx)`=动态(选中/图/笔迹/知识点 + 各自条件规则)拼 message。安全类规则(如"没明说不准制卡")必须常驻、不可条件化。

---

## 13. 落地检查清单(抄这个就不容易漏)

- [ ] 生成跑在**后台任务**里,`rid` 可重连,客户端断了能续(§1)
- [ ] `fetch` **底层包了重试**,GET 重试 / POST 不重试 / Abort 不重试(§3)
- [ ] 用户消息**进模型前落库**,回答 `finally` 落库,前端能从历史恢复(§4)
- [ ] 流式**节流渲染**,收尾才跑一次 MathJax(§2)
- [ ] **公式占位符保护** + 系统 prompt 强制 `$...$` + 禁反引号包数学(§5)
- [ ] 编排器快模型、生成步强模型;反馈「!」显**调用链(任务·模型·耗时)** + 升降档(§6)
- [ ] 工具循环:**沙盒** + **顽强 JSON 解析 + 自愈** + 总超时护栏(§7)
- [ ] 上下文**只在助手开着时收集**(§8)
- [ ] 长任务 detached + pid 防重 + status 轮询(§9)
- [ ] 移动端:rid 重连 + fetch 韧性 + 回前台看门狗 + 系统听写(§11)

---

> 一句话总结这套东西和"demo 级 AI 网页"的区别:**把"生成"当成一个独立于连接的、会落库的后台任务,网络层和渲染层都按"随时会断/随时要重来"设计。** 其余都是细节。

## 追加:助手「搜索视频」+ 对话内可播放视频卡(2026-07-06)

阅读器助手加了 `search_video` 工具,能在对话框里内嵌播放 YouTube。可迁移要点:

- **AI 拟搜索词**(关键,`_optimize_video_query`):别直接拿用户原句/选中长句搜——YouTube 中文区对学术/机制类内容质量差。先用便宜 Gemini Flash 把主题转成**一个优质搜索词**:学术/科学/机制/数学类**优先英文**+教学词(explained/animation/lecture),通俗/文化类中文。实测「神经放电引起肌肉收缩」直接搜=眼皮跳/中风徵兆(垃圾),AI 拟成 `neuromuscular junction transmission animation explained` =Alila Medical Media/2-Minute Neuroscience 等专业讲解。⚠ Claude 原生 web search **不适合视频**:返回网页链接、拿不到 embeddable video id、不能内嵌播放。
- **搜索后端**(`youtube_search.py`):YouTube Data API v3 `search.list`,`videoEmbeddable=true`(**关键**:否则内嵌 iframe 会「视频不可用」)。search.list=100 units/次、项目每天 10k → **必须缓存**(query→结果,30 天),否则每天只够 100 次搜索。配额跟其它 youtube 用途(如视频轮播)共享同一池。
- **工具返回分层**:给 agent 的 `videos` 只留 title/channel(省 token,别把缩略图 URL/id 塞进模型上下文);完整数据(id/thumb)走 `client_action:{fn:'renderVideos', args:[vids]}` 给前端渲染。agent 只需说一句「给你找到这些」,别复述链接(卡片已显示)。
- **内嵌播放 = lite-embed**(YouTube 官方推荐):先渲染缩略图 + ▶,**点击才**换成 `youtube-nocookie.com/embed/<id>?autoplay=1` iframe。省资源、快、不预连一堆第三方。
- **CSP**:内嵌第三方 iframe 只需站点**没有** `Content-Security-Policy: frame-src` 限制;`X-Frame-Options: SAMEORIGIN` 只限**本站被别人嵌**,不限本站嵌别人。
- **流式覆盖坑**:视频卡必须插在助手气泡**外**(sibling,`host.parentNode.insertBefore(wrap, host.nextSibling)`)——气泡在流式生成时 `innerHTML` 会被反复覆盖,插内部会被清掉。
- **去重**:`client_action` 可能经「实时 actions 事件」+「收尾 client_actions」两条路径都触发,前端按视频 id 组签名去重。

### 输入框上的「配图/视频」偏好开关(2026-07-06)
把「要不要配图/视频」从 AI 自己猜改成**用户显式表达**(ChatGPT 工具开关 / Perplexity Focus 范式):
- 输入框上方两个独立 pill toggle(🖼 配图 / 🎬 视频),可各开可同开、默认都关、状态存 localStorage 跨会话。
- **零改后端**:开启是「倾向不强制」——`window.rcMediaBias()` 读 localStorage 返回一段偏好提示,sendChat 发送前拼到 `message` 末尾(`message + rcMediaBias()`)。提示写明「适合才配、纯推导/基础常识别硬塞」,避免对纯文字问答硬配。
- **气泡不受污染**:偏置只拼进 body 的 message 字段、不改 message 变量本身,用户气泡仍显示原问题(EPUB 气泡用 msg、PDF 用 text 原值,body 表达式拼接不回写变量)。
- 共享实现在 `rc-video.js`(`rcMediaBias` + `rcBuildMediaRow(inputEl)`),PDF(挂 #asst-input)/EPUB(挂 #ep-ai-input)各一行调用。


### 视口焦点上下文(2026-07-06):给 AI「用户此刻在看哪一段」而非只给章节
- **问题**:EPUB 一节=整章内容,助手上下文只有章节号 → AI 只知「在看 §3-3 生物学」,不知用户视口正盯着「酶/三羧酸循环」那几段 → 回答/找视频退回泛泛的章节主题(把「酶」搜成「生物物理」)。
- **修法**:前端 `_visibleText()` 采集**与滚动容器视口相交**的正文块文字(p/h*/li/blockquote/td),限长 1000 字防 prompt 膨胀,随 context 发 `visible_text`;后端 `learn_bits` 加一条「★用户此刻屏幕上正在看的部分(注意力焦点;其余是背景。回答/找视频/拟搜索词都紧扣这里)」。空则不加(有依据才占 prompt)。
- **效果**:AI 拟视频/配图搜索词、回答都紧扣当前视口内容,不再退回章节泛主题。跟 `_optimize_video_query` 协同(prompt 有焦点 → agent 传的 query 就准)。
- **未做(有依据地克制)**:用户提的「注意力时间线(查词/滚动/提问时序)」价值边际递减 + 每事件吃 prompt 长度,视口焦点已解决主要问题;真需要再做极简版(只带最近查的 2-3 个词,vocab 日志已有)。

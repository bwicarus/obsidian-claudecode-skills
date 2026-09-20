---
name: stocks-scheduling
description: 在股票App安排或修改一次、每日、每周的提醒及指定个股报价分析，查看执行记录，暂停恢复取消任务，或安排未来时刻的App来电。适用于明早提醒、每天查看这些股票、周一分析等明确请求。
---

# 定时任务

使用 `stocks_schedule`；它由 VPS 持久运行，与语音在线状态无关。普通提醒到点直接交付文本，不调用 AI；分析任务到点获取指定股票的最新可用报价，只启动一次有时限的文本分析，完毕释放进程。

1. `catalog` 获取当前 UTC 时间、支持范围及 `clientTimeZone`，`list` 取得账户 revision 和已登记任务。理解“明早”等相对日期时以用户设备时区为准；时区缺失或时刻含糊就问清，不用服务器时区猜。用户明确指定另一时区时遵从该时区。
2. 确定 kind：原话提醒用 reminder；需要届时根据数据分析用 analysis。当前 analysis 只支持 1–10 个明确六位代码的报价，缺历史/新闻无法完成的综合任务应说明限制。不要假装已安排执行时动态读取收藏夹或完整研究报告。
3. `mutate` 示例结构：

```json
{"requestId":"本次唯一意图编号","expectedRevision":0,"operation":"task.upsert","task":{"kind":"analysis","title":"任务标题","prompt":"具体的报价分析目的","codes":["000001"],"schedule":{"kind":"once","at":"用户时区的完整ISO时间","timezone":"Asia/Tokyo"},"deliveryMode":"auto"}}
```

daily 用 `schedule:{kind:"daily",time:"09:30",timezone:...}`；weekly 另加 `days:["MO","TU",...]`。不要把例子时区或时间当成用户选择。一次任务必须在未来；定时依据真实回执 nextRunAt 回报具体日期、时间、时区。

只有用户明确要“到时候打电话”才用 deliveryMode=call，其余 auto。到点任务生成视觉通知，并复用当前语音/推送/来电路径；创建成功只是登记，不是已经分析或已经拨通。不要另外立即调用 stocks_call，也不要维持语音等到指定时间。

修改已有任务用 task.upsert 并携带原 id 与完整新任务。暂停/恢复/取消分别用 task.pause/task.resume/task.cancel，传 id；先看状态，依实际回执答复。取消或改期使旧计算结果失效；已错过的一次任务不补跑，重复任务恢复后从下一时刻开始。

`get:{id}` 查当前设置，`runs:{id,limit}` 查实际执行与失败记录。定时分析的获取时间不等于行情时间，闭市报价必须标注 quoteTime。完成分析、通知入库、推送受理、来电接听是不同结果，不能相互冒充。

只在 `ok:true` 且 `result.success:true` 后说已登记/修改；同意图重试复用 requestId 与原 payload，版本冲突先重新读。账户由工具固定，不传 owner，不接受任意命令、网址或循环模型任务。

---
name: stocks-scheduling
description: 在股票App安排或修改一次、每日、每周的提醒及已保存分析流程，查看执行记录、暂停恢复取消或按明确要求立即运行，及安排未来时刻的App来电。适用于明早分析某收藏夹、每天研究指定股票、定时提醒等请求。
---

# 定时任务

使用 `stocks_schedule`；它由 VPS 持久运行，与语音在线状态无关。普通提醒到点直接交付文本，不调用 AI；研究任务引用已保存的版本化流程，在到点后组合原子数据能力和有限的结构化分析，完成后释放进程。

1. `catalog` 获取当前 UTC 时间、支持范围及 `clientTimeZone`，`list` 取得账户 revision 和已登记任务。理解“明早”等相对日期时以用户设备时区为准；时区缺失或时刻含糊就问清，不用服务器时区猜。用户明确指定另一时区时遵从该时区。
2. 确定 kind：原话提醒用 reminder 并携带 prompt；新研究任务用 workflow，按 catalog 的真实流程 id/version/params 组织。旧 analysis+codes 仅兼容已有报价任务。收藏夹应先通过 stocks_selection library 确认唯一 folderId，不猜名称对应关系；流程执行时再解析当时成员。
3. `mutate` 示例结构：

```json
{"requestId":"本次唯一意图编号","expectedRevision":0,"operation":"task.upsert","task":{"kind":"workflow","title":"任务标题","workflow":{"id":"stock-research","version":1,"params":{"selection":{"kind":"folder","folderId":"已确认的收藏夹编号"},"sections":["quote","technical","news"],"prompt":"根据当时资料分析买入观察条件及风险"}},"schedule":{"kind":"once","at":"用户时区的完整ISO时间","timezone":"Asia/Tokyo"},"deliveryMode":"auto"}}
```

daily 用 `schedule:{kind:"daily",time:"09:30",timezone:...}`；weekly 另加 `days:["MO","TU",...]`。不要把例子时区或时间当成用户选择。一次任务必须在未来；定时依据真实回执 nextRunAt 回报具体日期、时间、时区。

指定股票时 selection 使用 `{kind:"codes",codes:[六位代码]}`；sections 只选问题需要且 catalog 支持的组件。流程可以逐股保存报告与操作卡，不会替用户采用卡片或启用盯盘。按 catalog 的规模和额度限制如实说明；不可把未执行的部分说成已分析。

只有用户明确要“到时候打电话”才用 deliveryMode=call，其余 auto。到点任务生成视觉通知，并复用当前语音/推送/来电路径；创建成功只是登记，不是已经分析或已经拨通。不要另外立即调用 stocks_call，也不要维持语音等到指定时间。

修改已有任务用 task.upsert 并携带原 id 与完整新任务。暂停/恢复/取消分别用 task.pause/task.resume/task.cancel，传 id；先看状态，依实际回执答复。取消或改期使旧计算结果失效；已错过的一次任务不补跑，重复任务恢复后从下一时刻开始。

只有用户明确要求“现在执行”时，对已有任务调用 task.run 并传 id、requestId、expectedRevision。它追加一次执行而保留原定时；登记明早任务不等于授权现在先跑一次。中断且没有持久结果的模型步骤不会自动重复消耗；查 runs 后说明，再由用户决定是否重跑。

`get:{id}` 查当前设置，`runs:{id,limit}` 查实际执行与失败记录。定时分析的获取时间不等于行情时间，闭市报价必须标注 quoteTime。完成分析、通知入库、推送受理、来电接听是不同结果，不能相互冒充。

只在 `ok:true` 且 `result.success:true` 后说已登记/修改；同意图重试复用 requestId 与原 payload，版本冲突先重新读。账户由工具固定，不传 owner，不接受任意命令、网址或循环模型任务。

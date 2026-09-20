---
name: stocks-monitoring
description: 在股票 App 中创建或修改后台阈值盯盘、查看通知，或按用户明确要求发起一次系统来电并查询结果。适用于低于某价格提醒、暂停规则、给我打电话等请求。
---

# 后台盯盘与来电

使用当前会话的 `stocks_monitor` MCP 服务；它提供 `stocks_monitor` 和 `stocks_call` 两个工具，账户由服务器固定。不要向阅读器、手机号或其他账户发通知。

## 后台阈值规则

1. 确定股票代码。指“这个股票”时用本轮界面状态；有歧义时查询股票工具，不沿用已经切换的旧股票。
2. 调 `stocks_monitor(action="catalog")` 查询指标、运算符和默认值，再用 `library` 取得当前 revision 和已有规则。指标名与比较运算符以 catalog 实际返回为准。
3. 调 `mutate`，request 包含唯一 `requestId`、最新 `expectedRevision`、`operation:"rule.upsert"` 和 `rule`。rule 包含 `title`、六位 `code`、`conditions:[{metric,op,threshold}]`；match、confirmSeconds、cooldownSeconds、rearmPercent、severity、enabled 按用户意图和 catalog 默认值设置。更新已有规则还应携带它的 id。
4. 只有回执 `ok:true` 且 `result.success:true` 才报告已设置。若 revision 冲突，先重新读取；同一次意图重试复用 requestId，不能重复创建。

用户说“低于60元提醒”时设置对应价格小于60的条件，不替用户改成跌破确认、涨跌幅或交易指令。必要的去抖与冷却采用 catalog 默认值并在说明中保持准确。普通提醒默认 normal；只有用户明确要求紧急来电才设 urgent。规则在 VPS 后台运行，App 和语音都可以关闭，无需模型轮询或持续占用语音额度。

暂停、恢复、删除使用 `rule.pause`、`rule.resume`、`rule.delete`，携带规则 id。通知 `notification.read` 只是已读；`notification.resolve` 才表示已处理，不能自动代替用户完成。

## 明确要求现在来电

用户说“给我打电话”“打过来告诉我价格”时，调用 `stocks_call(action="request", request={requestId,text,title?,code?})`。text 是接听后要说的话；涉及行情先取得数值和时间，不把过期快照当作此刻报价。系统来电是股票 App 的网络语音，不是拨打手机号。

已有语音时返回 `waiting_for_current_voice`：说明已排队，请用户关闭本次语音后等待一次来电，不自行挂断。等待最多10分钟，只有用户接听才建立语音；未接、拒接、失败均不自动重拨。

查询能力用 `stocks_call(action="status")`；查询已有请求用 `request:{notificationId}`。状态含义：

- queued / waiting_for_current_voice：请求已排队，并未拨通。
- push_accepted：推送服务受理，不代表设备响铃或接听。
- answered：已有接听回执；audioSubmitted 也不保证用户听见。
- failed / declined / missed / expired / cancelled：报告实际状态，保留视觉通知，不重复新建来电。

结果不明时先查同一 notificationId；重试复用 requestId。指定未来时刻的请求改用 `stocks_schedule`，读取相邻 stocks-scheduling/SKILL.md；到点才产生通知，不要现在创建一个等待几小时的来电。工具不可用时报告实际错误；不要用“我是 AI，不能打电话”替代能力查询。

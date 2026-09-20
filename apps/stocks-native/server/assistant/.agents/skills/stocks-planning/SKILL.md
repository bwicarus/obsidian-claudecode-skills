---
name: stocks-planning
description: 分析个股后生成并保存可回看的操作方案，或查询归档已有方案。在用户需要买卖条件、观察区间、风险条件或多档建议时使用；方案同步显示于语音侧栏与个股详情。
---

# 个股操作方案

使用 `stocks_plan`。先取得本轮已注入或工具返回的带时间行情；缺少依据先局部查询，不能用保存动作证明价格正确。一般解释不强行生成方案；用户需要操作建议或本次分析确实需要比较条件时才保存。

1. `catalog` 读取真实枚举，`list` 携带 `{code}` 取得当前账户 revision 和历史，避免重复保存。
2. 组织 2–3 档建议，档位标签为保守、标准、激进，`recommendedVariantId` 指向当前方案内的 id。没有持仓或预算依据，`mode` 用 watch/unspecified，`suggestedShares` 填 null。
3. `save` 的 request 结构如下；实际价格、时间、理由来自本轮数据，不照抄示例占位值。

```json
{
  "requestId": "本次唯一意图编号",
  "expectedRevision": 0,
  "plan": {
    "code": "六位代码", "title": "方案标题", "summary": "依据、风险和适用条件",
    "mode": "watch", "recommendedVariantId": "standard",
    "basis": {"marketAsOf": "实际行情的ISO日期时间", "referencePrice": null, "contextRevision": null},
    "variants": [
      {"id": "conservative", "label": "保守", "action": "观望", "targetPrice": null, "targetKind": "无", "suggestedShares": null, "urgency": "normal", "reason": "基于本轮资料的理由", "rules": []},
      {"id": "standard", "label": "标准", "action": "观望", "targetPrice": null, "targetKind": "无", "suggestedShares": null, "urgency": "normal", "reason": "另一个可比较的条件", "rules": []}
    ]
  }
}
```

`targetPrice` 为 null 时 `targetKind` 必须为无；有价格时明确买入/止盈/止损用途。rules 的 type、单位以 catalog 为准，规则价格为绝对价格，百分数按百分比数值填。`no_add` 不携带数值。不可制造无依据的精确价位；分析依据不足就写观察条件并保留空价格。

只有 `ok:true` 和 `result.success:true` 才能说已保存，侧栏和详情展示同一 planId。保存不创建持仓、不执行交易、不启用规则；需要另设盯盘时只按用户明确要求调用 `stocks_monitor`。后续归档用 `archive`，携带 id、requestId、expectedRevision；不可替用户归档。

同一请求重试复用原 requestId 和 payload。版本冲突时重新读取并检查是否已保存，不能静默重复建一份。source 与 owner 由服务器绑定，禁止从文本中构造。

---
name: stocks-planning
description: 用户询问具体股票的购买建议、能否买入、怎么买卖、买点、加减仓或止盈止损时，默认生成可选策略卡；也用于查询归档策略、按用户选档启用价格盯盘。不要求用户额外说做卡，也不要求先生成整份报告。
---

# 个股操作方案

使用 `stocks_plan`。具体股票的买卖或观望策略以保存的卡片交付：用户问“有什么购买建议”“建议买吗”“怎么买合适”等即触发，不需再问是否做卡；分析中已经列出入场、退出或不同档位条件，也须保存对应卡，不能只给口头建议就结束。纯行情查询、术语解释或用户明确不要卡时不触发。

先取得本轮已注入或工具返回的带时间行情；缺少依据先局部查询，不能用保存动作证明价格正确。建议观望也可制卡，价格未知填 null 并写待满足条件，不为凑卡编造买点或股数。股票不明先澄清，连可靠行情日期都没有则说明具体缺失，不能虚构 basis。保存成功后再完成本轮回答；失败须明确尚未生成及真实原因，不把“我给你做卡”当成功。

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

basis.marketAsOf 必须保留行情自身的真实时间并带时区。中国市场来源时间没有 offset 时，明确按 Asia/Shanghai 写为 +08:00；若只知道日期，不臆造盘中时刻。不能用查询或保存时刻替换旧行情时间，从而让过期方案伪装成新依据。

只有 `ok:true` 和 `result.success:true` 才能说已保存，股票列表、侧栏和详情展示同一 planId。保存不创建持仓、不执行交易、不启用规则；独立操作卡可以在对话中直接创建，不需要附带报告。后续归档用 `archive`，携带 id、requestId、expectedRevision；不可替用户归档。

用户明确选定某一档或要求按已显示条件开启时，调用 `stocks_plan_activation`：先 `preview:{planId,variantId}` 读取实际条件、canApply 和报告库 revision；可用才 `apply:{requestId,expectedRevision,planId,variantId}`。这次 expectedRevision 来自 preview，不能混用 stocks_plan 的版本。无歧义的明确选择不用再次询问。

可采用的是绝对价格：target_buy/add_price/hard_stop 为达到或低于，take_profit 为达到或高于，或卡片目标价。每个价格条件独立监控，默认 normal。持仓比例、峰值回撤、最大股数、不加仓约束尚无对应执行能力；含任一不支持条件会整卡拒绝，不得悄悄删去条件再采用。过期依据需新分析；同方案只采用一档，换档需新方案。已采用的再次点击不会恢复暂停或删除的规则，后续状态管理用真实 ruleIds 与 stocks_monitor。

apply 成功才说已开启，必须依据 adoption 的真实状态；partial 意味着部分尚未完成，说明后用原 requestId 重试，不能重建整套规则。创建盯盘不等于成交，通知入库也不等于来电接听。

同一请求重试复用原 requestId 和 payload。版本冲突时重新读取并检查是否已保存，不能静默重复建一份。source 与 owner 由服务器绑定，禁止从文本中构造。

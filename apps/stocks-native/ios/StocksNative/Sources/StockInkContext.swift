import CryptoKit
import Foundation

extension AppModel {
    var workspaceInkContext: StockWorkspaceInkContext? {
        guard detailPresented, let code = selectedCode, let page = workspace.layout.selectedPage else { return nil }
        let material = [client.token ?? "local", code, page.id, requestedTimelineWindow?.lower ?? "",
                        requestedTimelineWindow?.upper ?? "", chartPeriod.rawValue, klinePeriod.rawValue,
                        workspaceInkDomainID].joined(separator: "|")
        let scope = SHA256.hash(data: Data(material.utf8)).map { String(format: "%02x", $0) }.joined()
        return StockWorkspaceInkContext(stockCode: code, scopeID: scope,
                                        sourceTime: displayedStock?.quoteTime ?? displayedDetail?.asOf)
    }

    func receiveWorkspaceInk(_ snapshot: StockWorkspaceInkSnapshot) {
        guard !isTimelineEditing, snapshot.stockCode == selectedCode,
              snapshot.scopeID == workspaceInkContext?.scopeID, let page = workspace.layout.selectedPage else { return }
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        guard let encoded = try? encoder.encode(snapshot),
              var object = (try? JSONSerialization.jsonObject(with: encoded)) as? [String: Any] else { return }
        object["cards"] = snapshot.cardIDs.compactMap { id -> [String: Any]? in
            guard let card = page.visibleCards.first(where: { $0.id == id }) else { return nil }
            return ["id": id, "kind": card.kind.rawValue, "title": card.kind.title,
                    "sourceTime": snapshot.sourceTime ?? "", "data": inkCardData(card.kind)]
        }
        guard let payload = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return }
        Task {
            // Publish the scope first; image reception alone never starts a model turn.
            await publishVoiceContext(action: snapshot.cleared ? "已擦除当前笔迹" : "在数据卡片上勾画", kind: "annotation")
            guard isAIEnabled, snapshot.scopeID == workspaceInkContext?.scopeID else { return }
            voice.updateInk(payload, stockCode: snapshot.stockCode, scopeID: snapshot.scopeID)
        }
    }

    private func inkCardData(_ kind: WorkspaceCardKind) -> Any {
        func json<T: Encodable>(_ value: T?) -> Any {
            guard let value, let data = try? JSONEncoder().encode(value), data.count <= 4500,
                  let result = try? JSONSerialization.jsonObject(with: data) else { return ["status": "see_composite_for_visible_values"] }
            return result
        }
        let detail = displayedDetail
        switch kind {
        case .quote, .orderBook, .valuation: return json(displayedStock)
        case .fund:
            return ["asOf": detail?.fund?.asOf ?? "", "status": "历史日期由卡内选择，所选日期和数值以合成图为准"]
        case .chipCosts: return json(detail?.chips)
        case .chipDistribution: return json(chipDistribution)
        case .macd, .kdj:
            let series = NativeChartIndicators.series(candles: displayedKlineCandles,
                panel: klinePeriod == .day ? detail?.technical : nil)
            let visible = NativeChartIndicators.visible(series, context: linkedKlineContext)
            return ["period": klinePeriod.rawValue, "range": timelineRangeLabel,
                    "points": visible.suffix(8).map { point -> [String: Any] in
                        var value: [String: Any] = ["time": point.time]
                        value["dif"] = point.dif; value["dea"] = point.dea; value["macd"] = point.histogram
                        value["k"] = point.k; value["d"] = point.d; value["j"] = point.j
                        return value
                    }]
        case .chart, .kline, .klineChips, .intraday:
            let period = kind == .intraday ? ChartPeriod.intraday :
                (kind == .chart ? chartPeriod : klinePeriod)
            return ["range": timelineRangeLabel, "period": period.rawValue,
                    "chart": period == .intraday ? ["tradeDate": displayedIntraday?.tradeDate ?? ""] : json(linkedKlineContext),
                    "quote": json(displayedStock)]
        case .peers: return json(detail?.peers.map { Array($0.prefix(8)) })
        case .announcements: return json(detail?.announcements.map { Array($0.prefix(5)) })
        case .signals: return json(detail?.signals)
        default: return ["status": "visible_values_in_composite"]
        }
    }
}

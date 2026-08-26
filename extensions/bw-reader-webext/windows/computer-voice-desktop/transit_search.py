#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日本电车路线查询 —— Yahoo!乗換案内结果页解析（2026-08-27）。

为什么是它：Google Routes API 拿不到日本电车（数据授权只给 Google
自家 App，DRIVE 通/TRANSIT 恒空实锤）；駅すぱあと免费版要法人身份；
ODPT 要自建换乘引擎。而 Yahoo!乗換案内的结果页是服务端渲染的
Next.js —— `__NEXT_DATA__` 里就是完整结构化 JSON（班次/票价/换乘/
每段线路），一个 GET 就够。

⚠ 边界与自律：
- 这是解析网页，不是 API。页面改版脚本就断 —— 断了会**响亮报错**
  （BW_TRANSIT_PARSE），绝不静默返回空结果冒充"没有班次"。
- 个人低频使用（AI 一次行程规划一两次查询）；不要循环轰炸。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class TransitError(RuntimeError):
    pass


def fetch(from_station: str, to_station: str, date: str, hh: int, mm: int,
          arrival: bool) -> dict:
    query = urllib.parse.urlencode({
        "from": from_station,
        "to": to_station,
        "y": date[:4], "m": date[5:7], "d": date[8:10],
        "hh": "%02d" % hh, "m1": str(mm // 10), "m2": str(mm % 10),
        # type: 1=出发时刻 4=到达时刻
        "type": "4" if arrival else "1",
        "ticket": "ic", "expkind": "1", "ws": "3",
    })
    url = "https://transit.yahoo.co.jp/search/result?" + query
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=25) as response:
        html = response.read().decode("utf-8", "replace")
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S)
    if not match:
        raise TransitError(
            "BW_TRANSIT_PARSE: 页面里没有 __NEXT_DATA__ —— Yahoo 改版了，"
            "这个脚本需要重写解析（不要把这当成'没有班次'）。")
    data = json.loads(match.group(1))
    try:
        features = (data["props"]["pageProps"]["naviSearchParam"]
                    ["featureInfoList"])
    except (KeyError, TypeError) as error:
        raise TransitError(
            "BW_TRANSIT_PARSE: NEXT_DATA 结构变了（%s）—— 需要重写解析。"
            % error) from error
    if not features:
        raise TransitError(
            "BW_TRANSIT_EMPTY: 没有候选路线。站名可能没被识别"
            "（试试正式站名，如「八王子」而非「八王子站」），"
            "或该时段确实无班次。")
    routes = []
    for feature in features:
        summary = feature.get("summaryInfo") or {}
        legs = []
        for edge in feature.get("edgeInfoList") or []:
            rail = edge.get("railName") or ""
            station = edge.get("stationName") or ""
            time_info = edge.get("timeInfo") or []
            stamp = ""
            if isinstance(time_info, list) and time_info:
                first = time_info[0] or {}
                stamp = str(first.get("time") or "")
            if station:
                legs.append({"station": station, "time": stamp, "rail": rail})
        routes.append({
            "departure": summary.get("departureTime"),
            "arrival": summary.get("arrivalTime"),
            "totalTime": summary.get("totalTime"),
            "priceYen": summary.get("totalPrice"),
            "transfers": summary.get("transferCount"),
            "fast": bool(summary.get("isFast")),
            "cheap": bool(summary.get("isCheap")),
            "easy": bool(summary.get("isEasy")),
            "legs": legs,
        })
    return {"from": from_station, "to": to_station, "date": date,
            "routes": routes}


def render(result: dict) -> str:
    lines = ["%s → %s（%s）" % (result["from"], result["to"], result["date"])]
    for index, route in enumerate(result["routes"], 1):
        tags = "".join(
            label for flag, label in [
                (route["fast"], "[早]"), (route["cheap"], "[安]"),
                (route["easy"], "[楽]")] if flag)
        lines.append(
            "%d. %s発 → %s着  %s  %s円  乗換%s回 %s" % (
                index, route["departure"], route["arrival"],
                route["totalTime"], route["priceYen"],
                route["transfers"], tags))
        for leg in route["legs"]:
            if leg["rail"]:
                lines.append("     %s %s  〔%s〕" % (
                    leg["time"], leg["station"], leg["rail"]))
            else:
                lines.append("     %s %s" % (leg["time"], leg["station"]))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("from_station")
    parser.add_argument("to_station")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--time", required=True, help="HH:MM")
    parser.add_argument("--arrival", action="store_true",
                        help="把 --time 当到达时刻（默认=出发时刻）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    hh, mm = args.time.split(":")
    try:
        result = fetch(args.from_station, args.to_station, args.date,
                       int(hh), int(mm), args.arrival)
    except TransitError as error:
        print("错误：%s" % error)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

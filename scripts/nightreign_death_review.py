"""死亡回放页生成器 — 游戏台账分析层的第一个物化视图。

输入(某 session 的 refined/ 目录):
  death-work.json   # 确定性提取的死亡证据包(时间/数值/阅读版帧路径)
  verdicts.json     # AI 裁定(凶手/是否真死/证据),由 Workflow 产出后落盘
输出:
  death-review.json # 合并后的提炼层(可重跑,版本化)
  death-review.html # Artifact 页面内容(图片内嵌 data URI)

用法: python nightreign_death_review.py <session_dir>
"""

from __future__ import annotations

import base64
import html
import json
import sys
from datetime import datetime
from pathlib import Path

REFINED_VERSION = 1

OUTCOME_META = {
    "died": ("阵亡", "died"),
    "downed": ("倒地待扶", "downed"),
    "menu-or-other": ("误判(菜单/过场)", "other"),
    "unclear": ("无法判定", "unclear"),
}
CONF_TXT = {"high": "高", "medium": "中", "low": "低"}


def b64(path: str | None) -> str | None:
    if not path or not Path(path).exists():
        return None
    return "data:image/jpeg;base64," + base64.b64encode(Path(path).read_bytes()).decode()


def build(session_dir: Path) -> Path:
    refined = session_dir / "refined"
    deaths = json.loads((refined / "death-work.json").read_text("utf-8"))
    verdicts = {v["id"]: v for v in json.loads((refined / "verdicts.json").read_text("utf-8"))}

    merged = []
    for d in deaths:
        v = verdicts.get(d["id"], {})
        merged.append({**{k: d[k] for k in ("id", "ts", "endTs", "lossPx", "drops", "durationMs")},
                       "readingFrames": d["frames"], "verdict": v})
    (refined / "death-review.json").write_text(
        json.dumps({"version": REFINED_VERSION, "generatedAt": datetime.now().isoformat(),
                    "session": session_dir.name, "deaths": merged}, ensure_ascii=False, indent=2),
        "utf-8")

    meta = json.loads((session_dir / "session.json").read_text("utf-8"))
    events = [json.loads(l) for l in (session_dir / "ledger.jsonl").read_text("utf-8").splitlines()]
    eps = [e for e in events if e["kind"] == "episode-end"]
    total_loss = sum(max(e.get("lossPx", e["pxBefore"] - e["pxAfter"]), 0) for e in eps)
    died_n = sum(1 for m in merged if m["verdict"].get("outcome") == "died")
    downed_n = sum(1 for m in merged if m["verdict"].get("outcome") == "downed")
    day = meta.get("startedAt", "")[:10]
    t0, t1 = meta.get("startedAt", "")[11:16], (events[-1]["ts"][11:16] if events else "?")

    cards = []
    for i, m in enumerate(merged, 1):
        v = m["verdict"]
        label, cls = OUTCOME_META.get(v.get("outcome", "unclear"), OUTCOME_META["unclear"])
        pct = round(m["lossPx"] / 85)  # 满条≈8500px
        strip = ""
        for tag, cap in (("before", "受击前一秒"), ("at", "首击瞬间"), ("death", "血条归零")):
            src = b64(m["readingFrames"].get(tag))
            if src:
                strip += (f'<figure><img src="{src}" alt="{cap}" loading="lazy" '
                          f'onclick="lb(this)"><figcaption>{cap}</figcaption></figure>')
        att = html.escape(v.get("attacker", "未裁定"))
        conf = CONF_TXT.get(v.get("attackerConfidence", ""), "?")
        action = html.escape(v.get("action", ""))
        ev = html.escape(v.get("outcomeEvidence", ""))
        scene = html.escape(v.get("sceneNotes", "") or "")
        cards.append(f'''
<article class="death">
  <div class="rail">
    <div class="no">{i:02d}</div>
    <time>{m["ts"][11:19] if len(m["ts"]) > 10 else m["ts"]}</time>
    <dl>
      <div><dt>承伤</dt><dd>{m["lossPx"]}px<span class="pct">≈{pct}%血</span></dd></div>
      <div><dt>受击</dt><dd>{m["drops"]} 次</dd></div>
      <div><dt>历时</dt><dd>{m["durationMs"] / 1000:.1f}s</dd></div>
    </dl>
  </div>
  <div class="body">
    <header>
      <h2>{att}</h2>
      <span class="pill {cls}">{label}</span>
      <span class="conf">识别置信:{conf}</span>
    </header>
    <p class="action">{action}</p>
    <p class="evidence"><b>判定证据</b> {ev}</p>
    {f'<p class="scene">{scene}</p>' if scene else ''}
    <div class="strip">{strip}</div>
  </div>
</article>''')

    page = f'''<title>夜渡败因录</title>
<style>
  :root {{
    --ground: #0e1220; --surface: #171c2e; --raise: #1e2438; --line: #2a3149;
    --text: #d9dce8; --muted: #8b92ab; --gold: #e0a94e; --blood: #e05545;
    --ok: #7fb069;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--ground); color: var(--text); margin: 0;
    font: 16px/1.65 "Segoe UI", "Microsoft YaHei", sans-serif;
  }}
  .wrap {{ max-width: 880px; margin: 0 auto; padding: 40px 20px 80px; }}
  .eyebrow {{
    color: var(--gold); font-size: 13px; letter-spacing: .35em;
    text-transform: uppercase;
  }}
  h1 {{
    font: 700 40px/1.2 Georgia, "Microsoft YaHei", serif; margin: 6px 0 4px;
    text-wrap: balance;
  }}
  .sub {{ color: var(--muted); margin: 0 0 28px; }}
  .stats {{
    display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 40px;
  }}
  .stats > div {{
    background: var(--surface); border: 1px solid var(--line); border-radius: 6px;
    padding: 12px 18px; min-width: 110px;
  }}
  .stats b {{
    display: block; font-size: 24px; font-variant-numeric: tabular-nums;
    color: var(--gold); font-family: Georgia, serif;
  }}
  .stats span {{ font-size: 13px; color: var(--muted); }}
  .death {{
    display: flex; gap: 20px; background: var(--surface);
    border: 1px solid var(--line); border-radius: 8px; padding: 20px;
    margin-bottom: 22px;
  }}
  .rail {{
    flex: 0 0 108px; border-right: 1px solid var(--line); padding-right: 16px;
  }}
  .no {{
    font: 700 30px/1 Georgia, serif; color: var(--blood);
  }}
  .rail time {{
    display: block; color: var(--muted); font: 13px/1.4 Consolas, monospace;
    margin: 6px 0 12px;
  }}
  .rail dl {{ margin: 0; font-size: 13px; }}
  .rail dt {{ color: var(--muted); display: inline; }}
  .rail dd {{
    display: inline; margin: 0; font-variant-numeric: tabular-nums;
  }}
  .rail dl > div {{ margin-bottom: 6px; }}
  .pct {{ color: var(--muted); margin-left: 4px; font-size: 12px; }}
  .body {{ flex: 1; min-width: 0; }}
  .body header {{
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }}
  h2 {{ font: 700 22px/1.3 Georgia, "Microsoft YaHei", serif; margin: 0; }}
  .pill {{
    font-size: 12px; padding: 3px 10px; border-radius: 999px; font-weight: 600;
  }}
  .pill.died {{ background: rgba(224, 85, 69, .15); color: var(--blood); border: 1px solid var(--blood); }}
  .pill.downed {{ background: rgba(224, 169, 78, .15); color: var(--gold); border: 1px solid var(--gold); }}
  .pill.other {{ background: rgba(139, 146, 171, .12); color: var(--muted); border: 1px solid var(--line); }}
  .pill.unclear {{ color: var(--muted); border: 1px dashed var(--muted); }}
  .conf {{ font-size: 12px; color: var(--muted); }}
  .action {{ margin: 10px 0 6px; }}
  .evidence, .scene {{ font-size: 13.5px; color: var(--muted); margin: 4px 0; }}
  .evidence b {{ color: var(--gold); font-weight: 600; margin-right: 4px; }}
  .strip {{
    display: flex; gap: 10px; margin-top: 14px; overflow-x: auto;
  }}
  .strip figure {{ margin: 0; flex: 1 1 0; min-width: 160px; }}
  .strip img {{
    width: 100%; border-radius: 4px; border: 1px solid var(--line);
    cursor: zoom-in; display: block;
  }}
  .strip figcaption {{
    font-size: 12px; color: var(--muted); margin-top: 4px; text-align: center;
  }}
  #lightbox {{
    position: fixed; inset: 0; background: rgba(8, 10, 18, .93); display: none;
    align-items: center; justify-content: center; cursor: zoom-out; z-index: 9;
  }}
  #lightbox img {{ max-width: 96vw; max-height: 96vh; }}
  footer {{
    color: var(--muted); font-size: 12.5px; border-top: 1px solid var(--line);
    padding-top: 16px; margin-top: 40px;
  }}
</style>
<div class="wrap">
  <div class="eyebrow">Elden Ring Nightreign</div>
  <h1>夜渡败因录</h1>
  <p class="sub">{day} {t0}–{t1} · 探针台账 {len(events)} 事件 · AI 逐帧裁定</p>
  <div class="stats">
    <div><b>{died_n}</b><span>阵亡</span></div>
    <div><b>{downed_n}</b><span>倒地待扶</span></div>
    <div><b>{len(eps)}</b><span>危机段</span></div>
    <div><b>{total_loss // 85}%</b><span>累计承伤(按满血折算)</span></div>
  </div>
  {"".join(cards)}
  <footer>数据源:HP 探针 session {session_dir.name}(8Hz 血条采样 + 事件截图)。
  裁定由 AI 阅读事发帧生成,置信标注随裁定给出;原始曲线与台账完整保留,可随时重放复核。</footer>
</div>
<div id="lightbox" onclick="this.style.display='none'"><img></div>
<script>
  function lb(img) {{
    const box = document.getElementById('lightbox');
    box.querySelector('img').src = img.src;
    box.style.display = 'flex';
  }}
</script>
'''
    out = refined / "death-review.html"
    out.write_text(page, "utf-8")
    print(f"页面 {out} ({out.stat().st_size // 1024}KB)")
    return out


if __name__ == "__main__":
    build(Path(sys.argv[1]))

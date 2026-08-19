#!/usr/bin/env python3
"""文档有没有漂回旧架构？—— 把「我又照着过时文档干活了」变成一条能跑的检查。

## 为什么有这个脚本

2026-08-19 用户的诊断（原话大意）：不是某次判断失误，而是**项目描述文件里
遗留了旧工程的说法，导致转型多次失败**。当天一晚上我就因为这个把
`owner=pi` 当成"要部署 Pi"错了不止一次，还给一条 App 内本地执行的路由
加过查询参数，撞上本地实现的精确参数白名单，把原本能用的功能弄坏。

那一轮修了 62 处。但"修一遍"解决不了下一次 —— 文档会继续写，旧说法会继续
被复制。所以把当时的判据固化成检查：**每条规则都从代码里取事实**，
不是硬编码一份"正确答案"，否则这个脚本自己也会过期。

## 检查什么

1. VPS 视角的命令（VPS 自 2026-06-10 暂停，当前活跃实例是 Pi）
2. 把 PWA 当交付表面（PWA 已废除，路由返回 410）
3. OCR profile 版本号与代码不一致
4. 说某路由"要部署 Pi 才生效"，而 runtime 里它是本地执行
5. 教人手工 cp 部署清单内的文件（应走 deploy_reader.sh）
6. 推荐已废弃的协作方式（旧 SQLite 邮箱 / agent_collaboration.py）
7. 行内重复片段 —— 上一轮批量改文档时把 8 行改成了自相矛盾的重复句，
   症状是同一长片段在一行里出现两次。这条纯粹是防我自己。

前六条是**内容问题**，什么时候写进去的都算，所以全仓扫。第七条不一样：
它是"某次批量改动把行改坏了"的痕迹，历史文本里的重复本来就正常，
所以默认只看本次改动的行（冷扫全仓要 `--all`，会报 39 处历史噪声）。

## 用法

    python scripts/check_docs_drift.py            # 报告，有问题退出码 1
    python scripts/check_docs_drift.py --quiet    # 只报数量
    python scripts/check_docs_drift.py --all      # 行内重复也冷扫历史文本
    python scripts/check_docs_drift.py --only 路由归属
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "CLAUDE.md"] + sorted((ROOT / "references").glob("*.md"))
SKILLS = sorted((ROOT / ".claude" / "skills").glob("*.md"))


class Finding:
    def __init__(self, path: Path, line_no: int, rule: str, detail: str):
        self.path = path
        self.line_no = line_no
        self.rule = rule
        self.detail = detail

    def __str__(self) -> str:
        rel = self.path.relative_to(ROOT).as_posix()
        return f"{rel}:{self.line_no}  [{self.rule}] {self.detail}"


def _iter_lines(paths):
    for path in paths:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            yield path, no, line


def _changed_lines(rev: str = "HEAD") -> set[str] | None:
    """本次改动新增的行（内容集合）。拿不到 git 信息就返回 None = 不限制范围。

    为什么按内容而不是行号：行号在同一次改动里会被前面的增删推着走，
    按行号对齐要处理 hunk 偏移；而这里只需要判断"这一行是不是这次新写的"，
    内容就够，且不会因为偏移算错而把干净的行误判成新行。
    """

    try:
        res = subprocess.run(
            ["git", "diff", "--unified=0", rev, "--",
             "CLAUDE.md", "references", ".claude/skills"],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    added = {
        raw[1:] for raw in res.stdout.splitlines()
        if raw.startswith("+") and not raw.startswith("+++")
    }
    # 未跟踪的新文档整份都算"新写的"
    try:
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard",
             "CLAUDE.md", "references", ".claude/skills"],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        untracked = []
    for rel in untracked:
        try:
            added.update((ROOT / rel).read_text("utf-8", errors="replace").splitlines())
        except OSError:
            pass
    return added


# ── 1. VPS 视角 ────────────────────────────────────────────────────────
_VPS_CMD = re.compile(r"ssh\s+root@bwicarus\.space|scp\s+[^\s]*root@bwicarus\.space")
_VPS_CAVEAT = re.compile(r"暂停|已停|VPS 侧|仅 ?VPS|重新启用|历史|归档|不要用|已废弃")


def check_vps_commands(paths) -> list[Finding]:
    """VPS 自 2026-06-10 暂停。照着这些命令干活，要么连不上，
    要么在一台停更的机器上查日志然后得出错误结论。"""

    out: list[Finding] = []
    for path, no, line in _iter_lines(paths):
        if not _VPS_CMD.search(line):
            continue
        # 允许两种说明：紧邻上下文里讲清楚了，或**整份文档开头**就声明过。
        # 后者是必要的 —— 一份 VPS 时代的 how-to，在开头写一次「命令已过时」
        # 就够了；逼它在每条命令旁边重复一遍，反而没人看。
        text = path.read_text("utf-8", errors="replace").splitlines()
        window = "\n".join(text[max(0, no - 6) : no + 2])
        preamble = "\n".join(text[:14])
        if _VPS_CAVEAT.search(window) or _VPS_CAVEAT.search(preamble):
            continue
        out.append(Finding(path, no, "VPS 视角",
                           "命令指向已暂停的 VPS，且附近没说明；当前活跃实例是 Pi（ssh pi）"))
    return out


# ── 2. PWA 当交付表面 ──────────────────────────────────────────────────
_PWA_CLAIM = re.compile(
    r"PWA (?:是|作为|仍是|仍然是)[^。；\n]{0,24}(?:客户端|交付表面|入口|正式产品)"
    r"|(?:正式产品|交付表面)[^。；\n]{0,30}PWA"
    r"|PWA (?:完整界面|全功能)"
)
_PWA_CAVEAT = re.compile(r"已废除|已退役|410|不再|历史|归档|勿据|过时|放弃")


def check_pwa_as_surface(paths) -> list[Finding]:
    """PWA 已于 2026-08-14 废除（路由 410），2026-08-18 用户确认不再投入。
    文档里把它当交付表面，会让人为一个不存在的表面做兼容取舍。"""

    retired = ROOT / "_server_deploy" / "reader_pwa_retirement.py"
    if not retired.exists():
        return []                       # 代码里没这东西就别按这条判
    out: list[Finding] = []
    for path, no, line in _iter_lines(paths):
        if _PWA_CLAIM.search(line) and not _PWA_CAVEAT.search(line):
            out.append(Finding(path, no, "PWA 当交付表面",
                               "PWA 已废除（reader_pwa_retirement.py 返回 410）"))
    return out


# ── 3. OCR profile 版本号 ──────────────────────────────────────────────
def check_ocr_profile(paths) -> list[Finding]:
    """文档写的 profile 版本必须等于代码里的。这条不硬编码版本号 ——
    从 worker 源码取，代码升版本时检查自动跟上。"""

    worker = ROOT / "_server_deploy" / "reader_book_ocr_worker.py"
    if not worker.exists():
        return []
    m = re.search(r'PROCESSING_PROFILES\s*=\s*\{[^}]*"pc"\s*:\s*"([^"]+)"',
                  worker.read_text("utf-8", errors="replace"))
    if not m:
        return []
    current = m.group(1)
    out: list[Finding] = []
    for path, no, line in _iter_lines(paths):
        for found in re.findall(r"quality-first-v\d+", line):
            if found == current:
                continue
            # 明确在讲历史/兼容旧值的行放过
            if re.search(r"旧|历史|不会冒充|兼容|曾经|v1 ?→|已修", line):
                continue
            out.append(Finding(path, no, "profile 版本",
                               f"文档写 {found}，代码是 {current}"
                               f"（reader_book_ocr_worker.py::PROCESSING_PROFILES）"))
    return out


# ── 4. 路由归属 ────────────────────────────────────────────────────────
_PI_WORDS = re.compile(r"部署 ?Pi|要部署|Pi 侧实现|由 Pi (?:执行|提供|处理)|服务端实现")
_LOCAL_CAVEAT = re.compile(r"本地|local|App 内|不出网|runtime|TestFlight")


def _local_routes() -> set[str]:
    """runtime 里真正有本地分发分支的路由。

    ⚠ 只按路径字面量找是不够的：路径也出现在 NATIVE_SYNC_BATCH_ENDPOINTS
    这类 outbox 白名单里，而出现在那儿恰恰说明它**要发去 Pi**。
    认两种真正的分发形状就够。"""

    runtime = ROOT / "_server_deploy" / "static" / "pdf" / "native-local-runtime.js"
    manifest = ROOT / "ios" / "BWReader" / "native_reader_interface_manifest.json"
    if not runtime.exists() or not manifest.exists():
        return set()
    src = runtime.read_text("utf-8", errors="replace")

    def walk(node):
        if isinstance(node, dict):
            if "owner" in node and "path" in node:
                yield node["path"]
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    paths = set(walk(json.loads(manifest.read_text("utf-8", errors="replace"))))
    return {
        p for p in paths
        if f"url.pathname === '{p}'" in src or f"path === '{p}'" in src
    }


def check_route_ownership(paths) -> list[Finding]:
    """说「改这条要部署 Pi」，而它在 App 内是本地执行 —— 照着做等于白改。"""

    local = _local_routes()
    if not local:
        return []
    out: list[Finding] = []
    for path, no, line in _iter_lines(paths):
        if not _PI_WORDS.search(line) or _LOCAL_CAVEAT.search(line):
            continue
        for route in local:
            if route in line:
                out.append(Finding(path, no, "路由归属",
                                   f"{route} 在 App 内本地执行，改服务端对 App 无效"))
                break
    return out


# ── 5. 手工 cp 部署清单内的文件 ────────────────────────────────────────
def _manifest_files() -> set[str]:
    script = ROOT / "scripts" / "reader_deploy_manifest.py"
    if not script.exists():
        return set()
    try:
        res = subprocess.run([sys.executable, str(script)], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return set()
    if res.returncode != 0:
        return set()
    return {ln.split("\t")[0] for ln in res.stdout.splitlines() if "\t" in ln}


def check_manual_cp(paths) -> list[Finding]:
    """清单内的文件必须走 deploy_reader.sh（自带摘要校验/原子安装/回滚/健康检查）。
    教人手工 cp，等于把这些保护绕过去。"""

    tracked = _manifest_files()
    if not tracked:
        return []
    basenames = {Path(f).name: f for f in tracked}
    out: list[Finding] = []
    for path, no, line in _iter_lines(paths):
        if not re.search(r"\b(?:cp|scp)\s", line):
            continue
        # 事故复盘里提到手工 cp，是在讲它的**坏处**，不是在教人这么做
        if re.search(r"deploy_reader\.sh|不要手工|别手工|已废弃|历史|勿"
                     r"|导致|事故|教训|后果|踩坑|不该|失败", line):
            continue
        for name, rel in basenames.items():
            if not name.endswith((".py", ".js", ".html")):
                continue
            if re.search(r"[/\s]" + re.escape(name) + r"\b", line):
                out.append(Finding(path, no, "手工 cp",
                                   f"{rel} 在部署清单内，应走 scripts/deploy_reader.sh"))
                break
    return out


# ── 6. 已废弃的协作方式 ────────────────────────────────────────────────
_OLD_COLLAB = re.compile(r"scripts/agent_collaboration\.py|SQLite 邮箱|旧邮箱")
# ⚠ 这份文档是英文写的，只认中文说明词会把"正在说明它已退役"的行报成问题。
_COLLAB_CAVEAT = re.compile(
    r"不要|别|已废弃|不再|旧|历史|归档|回退"
    r"|retired|deprecat|legacy|historical|no longer|superseded|instead of",
    re.IGNORECASE,
)


def check_collab(paths) -> list[Finding]:
    """协作已统一走 BW AgentBridge Lite；旧 SQLite 邮箱不要回退。"""

    out: list[Finding] = []
    for path, no, line in _iter_lines(paths):
        if _OLD_COLLAB.search(line) and not _COLLAB_CAVEAT.search(line):
            out.append(Finding(path, no, "旧协作方式",
                               "协作走 BWAB，不要回退 agent_collaboration.py / SQLite 邮箱"))
    return out


# ── 7. 行内重复片段（防我自己批量改文档时改坏） ────────────────────────
_SCAN_ALL = False   # --all 打开；默认只看本次改动的行
_LINK_SELF = re.compile(r"\[`?([^\]`]+)`?\]\(\1\)")   # [`x`](x) 天然重复
_CJK = re.compile(r"[一-鿿]")   # 真坏行重复的是中文散文，不是路径
_SENT_PUNCT = re.compile(r"[。，：；、！？]")


def check_duplicated_span(paths) -> list[Finding]:
    """同一长片段在一行里出现两次，基本可以断定是批量替换时只换了前缀、
    旧尾巴留在原地。2026-08-19 这样弄坏过 8 行，比修之前更糟 ——
    因为它看起来像是有人特意写的。

    ⚠ 判据调过两轮，两次都是靠夹具量出来的，不是凭感觉：

    第一版只看"长片段重复"，报 44 处、几乎全是误报 —— markdown 表格行本来
    就会重复路径（`| VPS | /root/claude | … | Pi | /home/bwicarus/claude |`）。
    44 个误报比没有检查更糟：它训练人忽略输出。

    第二版索性**整类排除表格行**并要求重复片段含中文标点，误报归零，
    但拿那 8 行真实坏行一测，**只抓到 4 行** —— 干净的代价是它不再干活，
    而且漏掉的恰好包括一整行坏掉的表格。

    这版给两类行各一套阈值：表格行天然重复的是**单元格内容**，所以要求
    重复片段长到 40 字符**且跨过 `|`**（整段单元格序列被复制才算）；
    普通行不该有 26 字符以上的重复，直接报。
    夹具实测：8/8 命中，对照组 0 误报。

    ⚠ 但拿这套阈值冷扫全仓仍报 39 处，抽查多数是正常重复（`====` 分隔线、
    同一行里前后对比同一个路径、代码片段）。那不是阈值还不够好 ——
    是**场景用错了**：这个症状是"某次批量改动引入"的，历史文本里的重复
    本来就正常。所以默认只看**本次改动的行**（见 `_changed_lines`），
    `--all` 才冷扫全仓。检查跟着症状出现的时机走，噪声自然就没了。"""

    scope = None if _SCAN_ALL else _changed_lines()
    out: list[Finding] = []
    for path, no, line in _iter_lines(paths):
        if len(line) < 80:
            continue
        if scope is not None and line not in scope:
            continue                       # 不是这次改的，历史重复不归它管
        stripped = _LINK_SELF.sub("", line)
        is_table = line.strip().startswith("|")
        # 开头出现重复的列表/引用记号（`- - **`、`> - > - `），是拼接事故的硬证据
        doubled_marker = bool(re.match(r"\s*(?:[-*>]\s+){2,}", line))
        widths = (40,) if is_table else (26,)
        hit = None
        for width in widths:
            for i in range(0, len(stripped) - width, 3):
                frag = stripped[i : i + width]
                if frag.count(" ") > width // 2:
                    continue
                if stripped.count(frag) < 2:
                    continue
                if is_table and "|" not in frag:
                    continue      # 表格里只有整段单元格序列被复制才算症状
                # ⚠ 真坏行重复的是**中文散文**；正常文档里重复的是标识符和路径
                # （同一句里提两次同一个脚本、两个共享后缀的函数名 …）。
                # 2026-08-19 第二轮审计实测：不加这一条会误报 4 处，全是后者。
                # 那 4 行已进夹具对照组。唯一的例外是开头记号被复制的行 ——
                # 那种即使重复的是纯 ASCII 路径列表也确凿是事故。
                if not doubled_marker and not _CJK.search(frag):
                    continue
                hit = frag
                break
            if hit:
                break
        if hit:
            out.append(Finding(path, no, "行内重复",
                               f"疑似替换残留：{hit[:30]!r} 在同一行出现两次"))
    return out


CHECKS = [
    ("VPS 视角", check_vps_commands),
    ("PWA 当交付表面", check_pwa_as_surface),
    ("profile 版本", check_ocr_profile),
    ("路由归属", check_route_ownership),
    ("手工 cp", check_manual_cp),
    ("旧协作方式", check_collab),
    ("行内重复", check_duplicated_span),
]


def main() -> int:
    global _SCAN_ALL

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--quiet", action="store_true", help="只报数量")
    parser.add_argument("--only", nargs="*", help="只跑这些检查")
    parser.add_argument("--all", action="store_true",
                        help="「行内重复」也冷扫全仓历史文本（默认只看本次改动行）")
    args = parser.parse_args()
    _SCAN_ALL = args.all

    targets = DOCS + SKILLS
    total: list[Finding] = []
    for name, fn in CHECKS:
        if args.only and name not in args.only:
            continue
        found = fn(targets)
        total.extend(found)
        if not args.quiet:
            print(f"{name}: {len(found)} 处")
            for f in found[:14]:
                print(f"    {f}")
            if len(found) > 14:
                print(f"    …另有 {len(found) - 14} 处")
            print()

    print(f"合计 {len(total)} 处")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())

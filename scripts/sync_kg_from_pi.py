"""把 Pi 上的知识图谱同步到本机，供电脑侧的 AI 直接读。

为什么要有本地副本：AI 要做批量处理 —— 跨书找同一个知识点、判断读某本书缺哪些
前置、把散落的笔记按知识结构串起来。每问一次跨一次网，既慢又要求 Pi 在线；
批量处理更是不可能一条条问。

**只拉图，不拉掌握度。** 图是"这本书讲了什么、什么在什么之前"，跟谁在学无关，
所以可以只读单向分发 —— 没有两边都改的问题，也就不需要冲突解决那一整套。
掌握度是另一回事（跟人走、两边都会写），不在这条通道里。

三个刻意的选择：

  · **按修订号跳过**。服务端的修订号是内容哈希，重跑建图但内容没变时不会变，
    所以副本不必重下。一本书的图不小，全量重拉是浪费。

  · **失败不删旧副本**。拉不到时保留手上那份，并把状态写进 manifest。
    一份"截至昨天"的图远好过没有图 —— 后者会让 AI 以为这本书没有知识点。

  · **新鲜度写在 manifest 里**。副本必须能回答"我这份是什么时候的"。
    不知道自己多旧的数据，比明确过时的数据更危险：AI 会拿它下断言。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONTRACT = "reader-kg-local-mirror/1"
MANIFEST_NAME = "_mirror.json"
DEFAULT_TIMEOUT_SECONDS = 30


def project_root() -> Path:
    override = os.environ.get("CLAUDE_PROJECT")
    return Path(override) if override else Path(__file__).resolve().parent.parent


def mirror_dir() -> Path:
    """副本单独放，**不占 `knowledge_graph/`**。

    那个名字在 Pi 上指的是权威文件（带掌握度、会被建图写）。同名会埋一个雷：
    哪天电脑上也跑了建图，这边的只读副本和那边的权威文件就会互相覆盖 ——
    而覆盖掉的是掌握度，也就是唯一不能重算出来的那部分。
    路径本身就该说清它是什么。
    """
    return project_root() / "state" / "kg-mirror"


def _request(url: str, token: str, timeout: int) -> dict:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(f"服务端返回异常: {str(payload)[:200]}")
    return payload


def load_manifest(directory: Path) -> dict:
    path = directory / MANIFEST_NAME
    if not path.is_file():
        return {"contract": CONTRACT, "books": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # 清单坏了不该让同步停摆 —— 重建它，代价只是这次全量重拉。
        return {"contract": CONTRACT, "books": {}}
    if not isinstance(value, dict) or "books" not in value:
        return {"contract": CONTRACT, "books": {}}
    return value


def save_manifest(directory: Path, manifest: dict) -> None:
    manifest["contract"] = CONTRACT
    manifest["updatedAtEpochSeconds"] = int(time.time())
    path = directory / MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    directory.mkdir(parents=True, exist_ok=True)
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def sync(base: str, token: str, timeout: int, only: str | None) -> int:
    directory = mirror_dir()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(directory)
    books = manifest.setdefault("books", {})

    index = _request(
        urllib.parse.urljoin(base, "/api/kg/index"), token, timeout)
    remote = index.get("books") or []
    if not remote:
        print("服务端没有任何图谱。本地副本保持不变。")
        return 0

    updated = skipped = failed = 0
    for entry in remote:
        name = str(entry.get("book") or "")
        if not name or (only and name != only):
            continue
        if entry.get("error"):
            # 服务端读不了这本 —— 说出来，但不动本地那份。
            print(f"  ! {name}: 服务端无法读取（{entry['error']}），保留本地副本")
            failed += 1
            continue
        revision = str(entry.get("revision") or "")
        local = books.get(name) or {}
        target = directory / f"{name}.json"
        if local.get("revision") == revision and target.is_file():
            skipped += 1
            continue
        try:
            url = urllib.parse.urljoin(base, f"/api/kg/graph/{urllib.parse.quote(name)}")
            payload = _request(f"{url}?since={urllib.parse.quote(local.get('revision',''))}",
                               token, timeout)
            if payload.get("unchanged"):
                books[name] = {
                    "revision": payload.get("revision") or revision,
                    "syncedAtEpochSeconds": int(time.time()),
                    "status": "ok",
                }
                skipped += 1
                continue
            graph = payload.get("graph")
            if not isinstance(graph, dict):
                raise RuntimeError("响应缺少 graph")
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, target)
            books[name] = {
                "revision": payload.get("revision") or revision,
                "syncedAtEpochSeconds": int(time.time()),
                "nodes": len(graph.get("nodes") or []),
                "status": "ok",
            }
            updated += 1
            print(f"  ✓ {name}: {len(graph.get('nodes') or [])} 个节点")
        except Exception as error:
            # 拉不到就保留旧副本。"截至昨天的图"远好过没有图 ——
            # 后者会让 AI 以为这本书没有知识点，那是一句错的断言。
            previous = books.get(name) or {}
            previous["status"] = "stale"
            previous["lastError"] = f"{type(error).__name__}: {str(error)[:160]}"
            previous["lastAttemptEpochSeconds"] = int(time.time())
            books[name] = previous
            failed += 1
            print(f"  ! {name}: {previous['lastError']}（保留本地副本）")

    save_manifest(directory, manifest)
    print(f"\n更新 {updated}，未变 {skipped}，失败 {failed}。副本目录 {directory}")
    # 失败不算致命：本地仍有可用副本。但要让调用方能区分。
    return 0 if failed == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 Pi 上的知识图谱（只读部分）同步到本机")
    parser.add_argument(
        "--base",
        default=os.environ.get("BW_PI_BASE", "https://bwicarus.taile44d0c.ts.net"),
        help="Pi 的基址，默认取 BW_PI_BASE")
    parser.add_argument(
        "--token",
        default=os.environ.get("BW_PI_TOKEN", ""),
        help="API token，默认取 BW_PI_TOKEN")
    parser.add_argument("--book", default=None, help="只同步这一本")
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    if not args.token:
        # 明说缺什么。没有 token 时静默失败会被当成"服务端没有图谱"。
        print("缺少 API token：设置 BW_PI_TOKEN 或传 --token。"
              "token 在 Pi 的 /profile/ 页面生成。", file=sys.stderr)
        return 1
    try:
        return sync(args.base, args.token, args.timeout, args.book)
    except urllib.error.HTTPError as error:
        # 401 的字面意思是「凭据无效」，但服务端还有一种同样返回 401 的情况：
        # token 完全有效，只是这条路径没挂到 app.py 的 Bearer 桥
        # （PROTECTED_PREFIXES）上，于是 token 压根没被解析。两种都写出来，
        # 免得只按字面去查 token、查不出问题。2026-08-16 实际踩过一次。
        if error.code in (401, 403):
            detail = ("凭据无效或已过期；也可能是服务端没把 /api/kg 挂到 "
                      "Bearer 桥上（app.py 的 PROTECTED_PREFIXES），"
                      "那种情况下 token 是好的但不会被解析")
        else:
            detail = str(error)
        print(f"同步失败：HTTP {error.code} —— {detail}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"同步失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

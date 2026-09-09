"""voice_start_failed — 两次都没成时，把这件事说出来（2026-09-09）。

用户定的流程：跑一次入口脚本 → 等 → 没进语音就再跑一次 → 还是不行就**放弃**，
改跑这个脚本报错。

**为什么"放弃"也要有个脚本。**
不报错的放弃跟"还在试"在界面上长得一模一样：按钮一直闪，人一直等，而其实
早就不会成了。这个仓库最贵的那一类毛病就是这个 —— 出了状况就悄悄什么都不做。
所以放弃必须留下痕迹：一条回执 + 一行梯子状态，App 据此把按钮从"正在打开"
切成"打不开"，并说得出卡在哪。

这里**只记录，不重试、不改变任何语音状态**。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import voice_ladder
import voice_status_receipt


def report(
    *,
    attempts: int,
    detail: str = "",
    runtime: Path | None = None,
    local_root: Path | None = None,
) -> dict[str, object]:
    """写一条 error 回执，并把梯子状态刷一次（附上放弃的原因）。"""
    receipt = voice_status_receipt.build_receipt(
        request_id="voice-start-failed-%d" % attempts,
        task_status="error",
        # ⚠ 这里填 unknown 而不是 ended：两次没进语音**不代表**语音是关着的，
        # 只代表我们没能确认它开了。把没确认说成"关着"就是编造证据。
        voice_status="unknown",
        evidence="语音入口尝试 %d 次后放弃%s" % (
            attempts, ("：" + detail) if detail else ""),
    )
    written = voice_status_receipt.append_receipt(receipt, runtime)
    published = None
    if local_root is not None:
        status = voice_ladder.ladder(
            local_root=local_root, runtime=runtime)
        status["startGaveUp"] = {
            "attempts": attempts,
            "detail": detail,
            "at": receipt["respondedAt"],
        }
        published = str(voice_ladder.publish(status, runtime))
    return {"ok": True, "receipt": str(written), "ladder": published}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="记录语音入口尝试失败（只记录，不重试）")
    parser.add_argument("--attempts", type=int, default=2,
                        help="一共试了几次")
    parser.add_argument("--detail", default="",
                        help="能说清的话就写一句，比如最后一次的错误")
    parser.add_argument("--runtime", type=Path, default=None)
    parser.add_argument("--local-root", type=Path, default=None,
                        help="BWReader 目录；给了才刷梯子状态")
    args = parser.parse_args(argv)
    print(json.dumps(report(
        attempts=args.attempts, detail=args.detail,
        runtime=args.runtime, local_root=args.local_root,
    ), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

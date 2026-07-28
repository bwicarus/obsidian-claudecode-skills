#!/usr/bin/env python3
"""Local-only voice-typist helper for the native computer-voice host.

The server cannot choose an action or path.  This executable has one operation:
idempotently ensure the existing, fixed-path voice-typist is running in the
same interactive Windows session, using the already tested supervisor checks.
"""
from __future__ import annotations

import json
import sys

from bw_computer_voice_supervisor import (
    CONTRACT,
    SupervisorError,
    VoiceTypistLauncher,
)


def main(argv: list[str]) -> int:
    if argv != ["--ensure-running"]:
        print(json.dumps({
            "contract": CONTRACT,
            "ok": False,
            "code": "BW_COMPUTER_VOICE_ACTION_DENIED",
        }, separators=(",", ":")))
        return 64
    try:
        result = VoiceTypistLauncher().ensure_running()
    except SupervisorError as error:
        print(json.dumps({
            "contract": CONTRACT,
            "ok": False,
            "code": error.code,
            "error": str(error),
        }, ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps({
        "contract": CONTRACT,
        "ok": True,
        **result,
    }, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

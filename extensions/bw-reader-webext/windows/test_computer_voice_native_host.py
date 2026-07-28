#!/usr/bin/env python3
"""No-audio smoke test for the Windows Chrome Native Messaging host."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile


EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
CONTRACT = "reader-computer-voice-native/1"


def parse_frames(data: bytes) -> list[dict]:
    frames: list[dict] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 4:
            raise AssertionError("truncated native-message prefix")
        length = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if length < 1 or length > 64 * 1024 or offset + length > len(data):
            raise AssertionError("invalid native-message length")
        frames.append(json.loads(data[offset : offset + length]))
        offset += length
    return frames


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Path, required=True)
    args = parser.parse_args()
    source = args.host.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="bw-native-host-smoke-") as raw:
        root = Path(raw)
        host = root / source.name
        shutil.copy2(source, host)
        (root / "computer-voice-native.config.json").write_text(
            json.dumps(
                {
                    "contract": "reader-computer-voice-native-host-config/1",
                    "localOptIn": False,
                    "microphoneEndpointId": "",
                    "allowedExtensionId": EXTENSION_ID,
                    "typistHelper": "",
                    "voiceStartShortcut": "Ctrl+Shift+C",
                    "outputScope": "process-only",
                    "appKind": "codex-desktop",
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [str(host), f"chrome-extension://{EXTENSION_ID}/"],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr.decode(
            "utf-8", "replace"
        )
        frames = parse_frames(completed.stdout)
        assert [frame["type"] for frame in frames] == [
            "hello",
            "capabilities",
        ]
        assert all(frame["contract"] == CONTRACT for frame in frames)
        capabilities = frames[1]
        assert capabilities["localOptIn"] is False
        assert capabilities["captureScope"] == "process-only"
        assert capabilities["systemOutputFallback"] is False
        assert capabilities["microphone"] == {
            "available": False,
            "selection": "explicit-device-only",
            "deviceId": None,
        }
        assert capabilities["mediaDestination"] == "extension-offscreen-only"
        denied = subprocess.run(
            [str(host), "chrome-extension://pppppppppppppppppppppppppppppppp/"],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        assert denied.returncode != 0
        assert denied.stdout == b""
        assert b"BW_COMPUTER_VOICE_NATIVE_ORIGIN_DENIED" in denied.stderr
    print(json.dumps({
        "ok": True,
        "contract": "reader-computer-voice-native-host-smoke/1",
        "audioActivated": False,
        "frames": ["hello", "capabilities"],
        "wrongOriginDenied": True,
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

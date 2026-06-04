"""YouTube 视频 → Cloud Speech-to-Text 高质量转录。

替代 YouTube 自动 caption 的低质量转录(尤其健身术语经常错)。
流程:
  yt-dlp 下载视频 audio
  → ffmpeg 切 50s chunks + 转 WAV(LINEAR16) 16kHz mono
  → 并发调 STT sync API(每片 <60s 限制内)
  → 合并 word-level + 调 timestamps
  → word → segments(每段 ~5-7 秒)
  返回 [{start, duration, en}, ...] 跟 youtube_subtitles 兼容

成本: Cloud STT v1 standard model $0.024/min(短录音 enhanced 模型贵一些)。
日常健身视频 10-15min 一条,~¥1.7 / 条,赠金 ¥47867 ≈ 28000 条视频。
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

VISION_KEY_FILE = Path("/home/bwicarus/.config/gcp-vision-key")
STT_URL = "https://speech.googleapis.com/v1/speech:recognize"
CHUNK_SEC = 50            # STT sync API 限 60s,留余量
SAMPLE_RATE = 16000
MAX_WORKERS = 4           # 并发 STT 请求


def _key() -> str:
    k = os.environ.get("GOOGLE_VISION_API_KEY") or os.environ.get("GCP_API_KEY")
    if k: return k.strip()
    return VISION_KEY_FILE.read_text().strip()


def _download_audio(video_id: str, out: Path) -> None:
    """yt-dlp 下载 audio,转 m4a 体积小。"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    r = subprocess.run(
        [
            "yt-dlp",
            "-f", "bestaudio/best",
            "-x", "--audio-format", "m4a",
            "--no-warnings",
            "-o", str(out.with_suffix("")) + ".%(ext)s",
            url,
        ],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {r.stderr[:300]}")
    # 找出实际下载的文件(扩展名可能 m4a / webm 等)
    candidates = list(out.parent.glob(out.stem + ".*"))
    if not candidates:
        raise RuntimeError("yt-dlp 没产出文件")
    candidates[0].rename(out)


def _split_to_chunks(audio: Path, chunk_dir: Path) -> list[Path]:
    """ffmpeg 切 CHUNK_SEC 秒,转 WAV(LINEAR16) 16kHz mono。
    ⚠ 不用 FLAC:`-f segment -c:a flac` 会把**整段**时长写进每片 FLAC 的 STREAMINFO total_samples,
       STT 据此判为 >60s → 400 'Sync input too long'。WAV(pcm_s16le)的 segment muxer 对每片
       finalize 时写正确的 data chunk 大小 → 头时长准确,STT 正常。"""
    chunk_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(audio),
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-f", "segment",
            "-segment_time", str(CHUNK_SEC),
            "-segment_format", "wav",
            "-c:a", "pcm_s16le",
            str(chunk_dir / "chunk_%03d.wav"),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[:300]}")
    return sorted(chunk_dir.glob("chunk_*.wav"))


def _stt_chunk(args) -> list[dict]:
    """同步调 STT,返回 [{word, start, end}]。"""
    chunk_path, key, language = args
    content = base64.b64encode(chunk_path.read_bytes()).decode()
    r = requests.post(
        f"{STT_URL}?key={key}",
        json={
            "config": {
                "encoding": "LINEAR16",
                "sampleRateHertz": SAMPLE_RATE,
                "languageCode": language,
                "enableAutomaticPunctuation": True,
                "enableWordTimeOffsets": True,
                "model": "latest_long",   # 长篇视频更准
                "useEnhanced": True,
            },
            "audio": {"content": content},
        },
        timeout=120,
    )
    # quota log
    try:
        sys.path.insert(0, "/home/bwicarus/claude/scripts")
        from google_api_quota import log_usage
        # STT 计费按 15s 块,latest_long enhanced = $0.024/min ≈ $0.006/15s
        # units 用 chunk 秒数估算(精确账单看 Cloud Billing)
        log_usage("stt", CHUNK_SEC, "speech:recognize",
                  note=f"chunk={chunk_path.name} status={r.status_code}")
    except Exception:
        pass
    if r.status_code != 200:
        raise RuntimeError(f"STT HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"STT: {data['error'].get('message')}")
    words: list[dict] = []
    for result in data.get("results", []):
        alt = (result.get("alternatives") or [{}])[0]
        for w in alt.get("words", []):
            words.append({
                "word":  w["word"],
                "start": float(w["startTime"].rstrip("s")),
                "end":   float(w["endTime"].rstrip("s")),
            })
    return words


def _words_to_segments(words: list[dict], max_sec: float = 5.0,
                       max_words: int = 15) -> list[dict]:
    """word-level → segment-level(类似 YT auto-caption 的分段密度)。"""
    if not words:
        return []
    segments = []
    cur_start = words[0]["start"]
    cur_words: list[dict] = [words[0]]
    for w in words[1:]:
        # 满 max_sec 或 max_words 或大停顿就切
        gap_too_big = (w["start"] - cur_words[-1]["end"]) > 0.5
        time_full = (w["start"] - cur_start) >= max_sec
        words_full = len(cur_words) >= max_words
        if time_full or words_full or gap_too_big:
            text = " ".join(x["word"] for x in cur_words)
            segments.append({
                "start": cur_start,
                "duration": cur_words[-1]["end"] - cur_start,
                "en": text,
                "zh": None,
            })
            cur_start = w["start"]
            cur_words = [w]
        else:
            cur_words.append(w)
    if cur_words:
        text = " ".join(x["word"] for x in cur_words)
        segments.append({
            "start": cur_start,
            "duration": cur_words[-1]["end"] - cur_start,
            "en": text,
            "zh": None,
        })
    return segments


def transcribe_youtube(video_id: str, language: str = "en-US") -> list[dict]:
    """完整 pipeline。返回 segments(跟 youtube_subtitles 同 schema:start,duration,en,zh=None)。"""
    key = _key()
    with tempfile.TemporaryDirectory(prefix="yt_stt_") as tmp:
        tmp_p = Path(tmp)
        audio = tmp_p / "audio.m4a"
        _download_audio(video_id, audio)
        chunks = _split_to_chunks(audio, tmp_p / "chunks")
        if not chunks:
            raise RuntimeError("ffmpeg 没产出 chunk")
        # 并发跑 STT
        args_list = [(c, key, language) for c in chunks]
        with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            results = list(ex.map(_stt_chunk, args_list))
        # 合并 + 加 chunk offset
        all_words: list[dict] = []
        for i, words in enumerate(results):
            offset = i * CHUNK_SEC
            for w in words:
                all_words.append({
                    "word":  w["word"],
                    "start": w["start"] + offset,
                    "end":   w["end"] + offset,
                })
        return _words_to_segments(all_words)


def _cli():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id", help="YouTube video_id 或 完整 URL")
    ap.add_argument("--lang", default="en-US")
    args = ap.parse_args()
    # 容忍完整 URL
    vid = args.video_id
    m = re.search(r"(?:v=|youtu\.be/|embed/)([\w-]{11})", vid)
    if m: vid = m.group(1)
    elif len(vid) != 11:
        print(f"非法 video_id: {vid!r}"); sys.exit(1)
    print(f"转录 video_id={vid}…")
    segs = transcribe_youtube(vid, args.lang)
    print(f"\n共 {len(segs)} 段:")
    for s in segs[:30]:
        print(f"  [{s['start']:6.1f}s +{s['duration']:.1f}s] {s['en']}")
    if len(segs) > 30:
        print(f"  ... 还有 {len(segs)-30} 段")


if __name__ == "__main__":
    _cli()

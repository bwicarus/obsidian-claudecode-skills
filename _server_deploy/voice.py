"""全站语音助手后端(阶段 0 管道)。

路由(注册到 app.py 的 register_voice):
  POST /api/voice/transcribe   multipart 'audio' 音频 → Cloud STT 转文字 → {ok,text}
  POST /api/voice/agent        {transcript, context} → agent → {ok,speak,client_actions,server_results,confirm}
  GET  /api/voice/ping         自检(key/ffmpeg 在否)

阶段 0 只打通"说话→文字→念回":transcribe 真转录;agent 先做"会话作答 + 结构化返回",
工具映射(112 个动作)+ 危险动作确认在阶段 1/2 接上。返回结构已定型,前端契约不变。

- STT:Google Cloud Speech-to-Text(GCP key `AIzaSy*`,走 GCP 赠金 —— Gemini key 额度已枯竭,
  改用这条;两个池子不通,见 skill google-apis §0.2)。中文为主 + 英/日 备选语言。
  iPad MediaRecorder 出的 mp4/webm 先 ffmpeg 转 flac 16k mono。
- Agent 大脑:复用 scripts/ai_client.ask(Claude;settings 里的后端)。
"""
from __future__ import annotations

import base64
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests
from flask import Blueprint, jsonify, request, session

bp = Blueprint("voice", __name__, url_prefix="/api/voice")

CLAUDE_DIR = Path(os.environ.get("CLAUDE_PROJECT", "/home/bwicarus/claude"))
GCP_KEY_FILE = Path(os.environ.get("GCP_API_KEY_FILE", "/home/bwicarus/.config/gcp-vision-key"))
VAULT_ROOT = Path(os.environ.get("OBSIDIAN_VAULT", "/home/bwicarus/obsidian"))
STT_URL = "https://speech.googleapis.com/v1/speech:recognize"


def _page_text(file_rel: str, page) -> str:
    """服务端按 file_rel+page 用 PyMuPDF 取该页正文(不依赖前端 char-layer,能拿全文)。"""
    try:
        rel = (file_rel or "").strip()
        if rel.startswith("web:"):   # 审计 #7:网页走 assistant 的统一 resolver(这份是独立副本,
            #   曾没跟上 → 语音「把这页做成笔记」恒报"没找到要整理的内容")
            try:
                import assistant as _AS
                return (_AS._page_text(rel, page) or "")[:4000]
            except Exception:
                return ""
        if not rel or ".." in rel:
            return ""
        ap = (VAULT_ROOT / rel).resolve()
        ap.relative_to(VAULT_ROOT.resolve())   # 防路径越界
        if not ap.exists():
            return ""
        import fitz
        doc = fitz.open(str(ap))
        try:
            idx = int(page or 1) - 1
            idx = max(0, min(idx, doc.page_count - 1))
            txt = (doc[idx].get_text("text") or "").strip()
        finally:
            doc.close()
        return txt[:2000]
    except Exception:
        return ""


def _gcp_key() -> str:
    try:
        return GCP_KEY_FILE.read_text("utf-8").strip()
    except Exception:
        return os.environ.get("GCP_API_KEY", "")


def _logged_in() -> bool:
    return bool(session.get("user_id"))


def _has_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def _ffmpeg_to_flac(src: Path, dst: Path) -> bool:
    """任意 iOS 录音(mp4/webm/m4a)→ flac 16k 单声道。失败回 False。"""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1", str(dst)],
            check=True, capture_output=True, timeout=60)
        return dst.exists() and dst.stat().st_size > 0
    except Exception:
        return False


# 常用命令/导航词:音频层发音偏置(屏幕实体另走高 boost,见 _cloud_stt)
_STT_CORE_HINTS = ["下一页", "上一页", "翻页", "跳到", "放大", "缩小", "适应宽度",
                   "双页", "单页", "连续滚动", "全屏", "去白边", "注音", "振假名", "整页翻译",
                   "搜索", "知识点", "侧栏", "生词本", "回书架", "返回", "确定", "取消",
                   "翻译", "健身", "学习看板", "技能树", "复习仪表盘"]
_LANG_ALTS = ["cmn-Hans-CN", "en-US", "ja-JP"]


def _stt_parse(data: dict) -> str:
    if "error" in data:
        raise RuntimeError("STT: " + data["error"].get("message", ""))
    parts = []
    for res in data.get("results", []):
        alt = (res.get("alternatives") or [{}])[0]
        t = alt.get("transcript", "").strip()
        if t:
            parts.append(t)
    return " ".join(parts).strip()


def _cloud_stt(audio_b64: str, encoding: str, sample_rate: int, lang: str, hints) -> str:
    """Cloud STT。command_and_search(短指令)+ speechContexts(屏幕实体当发音偏置,谐音纠错的音频层)。
    失败自动降级到最小 config(去掉 alternatives/speechContexts)再试一次 —— 优雅降级,别比啥都没做更差。"""
    key = _gcp_key()
    if not key:
        raise RuntimeError("no gcp key")
    lang = lang or "cmn-Hans-CN"
    ctx = []
    ents = [h for h in (hints or []) if isinstance(h, str) and h.strip()][:200]
    if ents:
        ctx.append({"phrases": ents, "boost": 15})        # 屏幕实体:核心偏置(研究建议 12-18,别全局 20)
    ctx.append({"phrases": _STT_CORE_HINTS, "boost": 14})  # 常用命令词:必中(研究建议 12-18)
    full_cfg = {
        "languageCode": lang,
        "model": "command_and_search",   # 老 model 对 inline speechContexts 稳定生效(latest_* 中文 adaptation 覆盖不确定)
        "encoding": encoding,
        "sampleRateHertz": sample_rate,
        "audioChannelCount": 1,
        "enableAutomaticPunctuation": False,
        "maxAlternatives": 1,
        "alternativeLanguageCodes": [c for c in _LANG_ALTS if c != lang][:2],
        "speechContexts": ctx,
    }
    min_cfg = {"languageCode": lang, "model": "command_and_search",
               "encoding": encoding, "sampleRateHertz": sample_rate, "audioChannelCount": 1}
    last = ""
    for cfg in (full_cfg, min_cfg):
        r = requests.post(f"{STT_URL}?key={key}",
                          json={"config": cfg, "audio": {"content": audio_b64}}, timeout=60)
        try:
            sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
            from google_api_quota import log_usage
            log_usage("stt", 1, "speech:recognize", note=f"voice status={r.status_code}")
        except Exception:
            pass
        if r.status_code == 200:
            return _stt_parse(r.json())
        last = f"STT HTTP {r.status_code}: {r.text[:160]}"
        # 仅在 full→min 之间重试(min 再失败就抛)
    raise RuntimeError(last)


@bp.route("/transcribe", methods=["POST"])
def voice_transcribe():
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    f = request.files.get("audio")
    if not f:
        return jsonify({"ok": False, "error": "no audio"}), 400
    lang = (request.form.get("lang") or "cmn-Hans-CN").strip()
    try:
        hints = json.loads(request.form.get("hints") or "[]")
        if not isinstance(hints, list):
            hints = []
    except Exception:
        hints = []
    raw = f.read()
    fn = (f.filename or "").lower()
    is_wav = fn.endswith(".wav") or raw[:4] == b"RIFF"
    try:
        if is_wav:
            # 浏览器 Web Audio 编的 16k mono LINEAR16 WAV → 直接喂(WAV 自带头,无需 ffmpeg)
            text = _cloud_stt(base64.b64encode(raw).decode(), "LINEAR16", 16000, lang, hints)
        else:
            # 老客户端 mp4/webm → ffmpeg 转 flac 兜底
            with tempfile.TemporaryDirectory() as td:
                src = Path(td) / ("in" + (Path(fn).suffix or ".bin"))
                src.write_bytes(raw)
                flac = Path(td) / "a.flac"
                if not _ffmpeg_to_flac(src, flac):
                    return jsonify({"ok": False, "error": "ffmpeg 转码失败"}), 500
                text = _cloud_stt(base64.b64encode(flac.read_bytes()).decode(), "FLAC", 16000, lang, hints)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    return jsonify({"ok": True, "text": text})


_VOICE_LOG = CLAUDE_DIR / "state" / "logs" / "voice.log"


@bp.route("/log", methods=["POST"])
def voice_log():
    """前端 beacon 日志(关键步骤)。sendBeacon 可能不带 cookie → 宽松接收,只落盘不鉴权。"""
    try:
        data = (request.get_data(as_text=True) or "")[:2000].replace("\n", " ").replace("\r", " ")
        _VOICE_LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            if _VOICE_LOG.exists() and _VOICE_LOG.stat().st_size > 2_000_000:
                _VOICE_LOG.replace(_VOICE_LOG.with_suffix(".log.1"))
        except Exception:
            pass
        with _VOICE_LOG.open("a", encoding="utf-8") as fp:
            fp.write(data + "\n")
    except Exception:
        pass
    return ("", 204)


# ── 快路径意图(规则匹配,零 LLM、即时):PDF 阅读器常用口令直接出客户端动作 ──
import re as _re

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}


def _parse_num(s: str):
    """从口令里抠页码:阿拉伯数字优先;否则解析中文数字(到几百足够)。"""
    m = _re.search(r"\d+", s)
    if m:
        return int(m.group())
    cn = _re.search(r"[零一二两三四五六七八九十百]+", s)
    if not cn:
        return None
    t = cn.group(); v = 0; section = 0
    for ch in t:
        n = _CN_NUM.get(ch, 0)
        if n == 100:
            section = (section or 1) * 100; v += section; section = 0
        elif n == 10:
            section = (section or 1) * 10; v += section; section = 0
        else:
            section += n
    return (v + section) or None


def _act(fn, args, speak):
    return {"speak": speak, "client_actions": [{"fn": fn, "args": args}],
            "server_results": [], "confirm": None}


# ── 动作清单(同时给:fast_intent 出动作 + LLM 兜底用作工具表 + 服务端白名单校验)──
# 每项 (fn, 说明, 参数格式)。fn 必须是前端 window 全局函数(reader.js 或 voice.js 定义)。
_PDF_ACTIONS = [
    ("changePage", "翻页", "args=[1]下一页 / [-1]上一页"),
    ("goToPage", "跳到指定页", "args=[页码整数]"),
    ("zoomChange", "缩放", "args=[0.15]放大 / [-0.15]缩小"),
    ("fitWidth", "适应宽度铺满", "args=[]"),
    ("toggleSpread", "单页↔双页 切换", "args=[]"),
    ("toggleReadMode", "单页模式↔连续滚动 切换", "args=[]"),
    ("toggleFullscreen", "全屏 开关", "args=[]"),
    ("toggleCrop", "去白边 开关", "args=[]"),
    ("toggleRuby", "注音(振假名/英文音标) 开关", "args=[]"),
    ("togglePageTranslate", "整页翻译 开关", "args=[]"),
    ("openSearch", "打开搜索框", "args=[]"),
    ("toggleSidebar", "知识点侧栏 开关", "args=[]"),
    ("toggleVocab", "生词本 开关", "args=[]"),
    ("goPdfList", "回到书架/书本列表", "args=[]"),
    ("__voiceOpenBook", "按书名打开一本书(切换到另一本书)", "args=[该书的 rel,必须从下方书架清单里挑]"),
]
_GLOBAL_ACTIONS = [
    ("__voiceGo", "跳转到网站某个页面",
     "args=[路径]: /pdf/ 书架, /insights/ 学习看板, /skilltree/ 技能树, "
     "/dashboard/ 复习仪表盘, /private/fitness/ 健身, /history/ 问答历史, /profile/ 个人设置"),
]
_WHITELIST = {fn for fn, _, _ in _PDF_ACTIONS} | {fn for fn, _, _ in _GLOBAL_ACTIONS}
_NAV_OK = {"/pdf/", "/insights/", "/skilltree/", "/dashboard/", "/private/fitness/", "/history/", "/profile/"}

# 全局导航(任何页面都可用),映射到 window.__voiceGo
_NAV = [
    (r"(健身|训练|锻炼|举铁|肌肉)", "/private/fitness/", "好,去健身"),
    (r"(学习看板|数据看板|学习数据|学习分析|洞察|insights)", "/insights/", "好,打开学习看板"),
    (r"(技能树|知识图谱|知识树|skilltree)", "/skilltree/", "好,打开技能树"),
    (r"(复习仪表盘|复习面板|今日复习|复习计划|仪表盘|dashboard)", "/dashboard/", "好,打开复习仪表盘"),
    (r"(书架|看书|读书|阅读器|去读|pdf)", "/pdf/", "好,去书架"),
    (r"(问答历史|历史记录|对话历史|history)", "/history/", "好,打开历史"),
    (r"(个人设置|账号设置|我的设置|profile)", "/profile/", "好,打开个人设置"),
]


def _nav_intent(s):
    for pat, path, speak in _NAV:
        if _re.search(pat, s):
            return _act("__voiceGo", [path], speak)
    return None


def _open_book_intent(s, context):
    """语音「打开X书」→ 把 X 跟书架清单(__voiceContext.books)做子串匹配,唯一对上就直接开。
    谐音兜底交给 LLM(它拿到带 rel 的书目清单)。STT 若靠 speechContexts 把书名听对了,这里零延迟命中。"""
    books = (context or {}).get("books") or []
    if not books or not _re.search(r"(打开|翻开|进入|切换到|读|看|开)", s):
        return None
    q = _re.sub(r"(打开|翻开|进入|切换到|读一?读|看一?下|看|读|开|这本|那本|书|文件|pdf|PDF)", "", s).strip()
    if len(q) < 2:
        return None
    best = None
    for b in books:
        nm = (b.get("name") or "")
        stem = nm.rsplit(".", 1)[0]
        rel = b.get("rel")
        if not stem or not rel:
            continue
        if q in stem or stem in q:                 # 双向子串:STT 把名字听对就命中
            score = len(stem)
            if best is None or score > best[0]:
                best = (score, stem, rel)
    if best:
        return _act("__voiceOpenBook", [best[2]], f"好,打开{best[1]}")
    return None


def _fast_intent(t: str, context: dict):
    """命中常用口令→即时出动作(零 LLM),否则 None(交给 LLM 兜底)。"""
    s = t.replace(" ", "")
    if (context or {}).get("page_type") == "pdf":
        # 翻页
        if _re.search(r"(下一?[页张个]|后一?[页张]|往[后下]|翻过去|next)", s):
            return _act("changePage", [1], "好,下一页")
        if _re.search(r"(上一?[页张个]|前一?[页张]|往[前上]|退回去|previous|back)", s):
            return _act("changePage", [-1], "好,上一页")
        # 跳页:第N页 / 跳到N / 翻到N。只取页码关键词紧跟的数字(避免「一点点」的「一」被当页码)
        mpage = _re.search(r"(?:第|跳到|翻到|去第|到第|page)\s*([0-9零一二两三四五六七八九十百]+)", s)
        if mpage:
            n = _parse_num(mpage.group(1))
            if n:
                return _act("goToPage", [n], f"好,翻到第{n}页")
        # 缩放 / 适应
        if _re.search(r"(放大|拉大|大一[点些]|zoom\s*in)", s):
            return _act("zoomChange", [0.15], "放大一点")
        if _re.search(r"(缩小|拉小|小一[点些]|zoom\s*out)", s):
            return _act("zoomChange", [-0.15], "缩小一点")
        if _re.search(r"(适应|铺满|满屏|自适应|合适宽度|fit)", s):
            return _act("fitWidth", [], "好,适应宽度")
        # 排版模式
        if _re.search(r"双页", s):
            return _act("toggleSpread", [], "切到双页")
        if _re.search(r"(单页模式|切.*单页|单页阅读|^单页$)", s):   # 不匹配「这一页/一页纸」等(那些是提问/数量)
            return _act("toggleReadMode", [], "切到单页")
        if _re.search(r"(连续模式|连续滚动|切.*连续|滚动模式)", s):
            return _act("toggleReadMode", [], "切到连续")
        if _re.search(r"(全屏|fullscreen)", s):
            return _act("toggleFullscreen", [], "全屏")
        if _re.search(r"(去边|裁边|裁切|去白边|边距)", s):
            return _act("toggleCrop", [], "切换去边")
        if _re.search(r"(振假名|假名|注音|音标|ruby)", s):
            return _act("toggleRuby", [], "切换注音")
        if _re.search(r"(整页翻译|译页|翻译这页|翻译本页|全页翻译|翻译整页)", s):
            return _act("togglePageTranslate", [], "切换整页翻译")
        # 面板
        if _re.search(r"(打开搜索|搜索框|查找框|开搜索|^搜索$|^查找$|^search$)", s):   # 仅「开搜索框」;「搜索X内容」交给综合任务
            return _act("openSearch", [], "打开搜索")
        if _re.search(r"(知识点|关联|侧栏)", s):
            return _act("toggleSidebar", [], "打开知识点")
        if _re.search(r"(生词本|单词本|生词列表)", s):
            return _act("toggleVocab", [], "打开生词本")
        if _re.search(r"(回书架|书本列表|回到列表|选书)", s):
            return _act("goPdfList", [], "回到书架")
        # 按书名开书(命中书架清单子串)
        ob = _open_book_intent(s, context)
        if ob:
            return ob
    # 全局导航(任意页面)
    nav = _nav_intent(s)
    if nav:
        return nav
    return None


# ── LLM 兜底:把自然语言映射成真实可执行动作(fast_intent 没命中时走这里)──
def _action_catalog(page_type: str) -> str:
    lines = []
    if page_type == "pdf":
        lines.append("【PDF 阅读器动作(仅当前在阅读器时可用)】")
        for fn, desc, args in _PDF_ACTIONS:
            lines.append(f"- {fn}:{desc}({args})")
    lines.append("【全站导航(任何页面都可用)】")
    for fn, desc, args in _GLOBAL_ACTIONS:
        lines.append(f"- {fn}:{desc}({args})")
    return "\n".join(lines)


def _validate_actions(actions):
    """只放行白名单函数 + 规整参数,挡住 LLM 幻觉的函数名/坏参数。"""
    out = []
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        fn = a.get("fn")
        if fn not in _WHITELIST:
            continue
        args = a.get("args", [])
        if not isinstance(args, list):
            args = [args]
        try:
            if fn == "goToPage":
                args = [int(args[0])]
            elif fn == "changePage":
                args = [1 if int(args[0]) >= 0 else -1]
            elif fn == "zoomChange":
                args = [float(args[0])]
            elif fn == "__voiceGo":
                p = str(args[0]).strip()
                if p not in _NAV_OK:
                    continue
                args = [p]
            elif fn == "__voiceOpenBook":
                p = str(args[0]).strip()
                if not p or ".." in p or not p.lower().endswith(".pdf"):
                    continue
                args = [p]
            else:
                args = []   # 其余都是无参 toggle
        except (IndexError, ValueError, TypeError):
            continue
        out.append({"fn": fn, "args": args})
    return out


def _extract_json(text: str):
    """从 LLM 输出里抠出第一个完整 JSON 对象(容忍前后噪声/代码块围栏)。"""
    i = text.find("{")
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:j + 1])
                except Exception:
                    return None
    return None


_LLM_SYS = (
    "你是学习网站的语音指挥助手。用户的话来自语音识别,可能有谐音/近音错字。\n"
    "0) 若用户提到屏幕上能看到的东西(书名/知识点/生词等,见下方【当前可见实体】),按读音把可能听错的词"
    "映射到清单里最接近的真实项:唯一对得上就直接执行对应动作(如开书用 __voiceOpenBook + 该书 rel);"
    "多个都像才用 say 反问让用户二选一,别瞎猜。\n"
    "1) 操作类(翻页/缩放/跳转/打开某书或功能/去某页面等)→ 从下方动作清单选出要执行的动作,"
    "返回 {\"say\":\"一句简短中文应承\",\"actions\":[{\"fn\":\"函数名\",\"args\":[参数]}]}。可一次多个动作。"
    "函数名必须严格来自清单,参数照清单格式,别编造。\n"
    "2) 提问/闲聊(不需要操作页面)→ 返回 {\"say\":\"简短中文口语回答,≤2句\",\"actions\":[]}。"
    "若下方给了【当前页正文】,据此回答用户关于「这页/这段讲什么、什么意思、解释一下」之类的内容问题。\n"
    "只输出 JSON 本身,不要解释、不要 markdown、不要代码块围栏。say 会被语音念出,务必简短自然。"
)

# 内容类提问(才去取页面正文,翻页/缩放等命令不触发,省 PDF 开销)
_CONTENT_Q = _re.compile(r"(讲|说|写|介绍|表达|意思|内容|大意|主旨|概括|总结|归纳|解释|说明|关于|是什么|什么意思|怎么回事|为什么|重点|这段|这句|这里|这部分)")


def _entities_block(context: dict) -> str:
    """把屏幕可见实体喂给大脑做谐音映射(书名带 rel 供开书)。"""
    lines = []
    books = (context or {}).get("books") or []
    if books:
        lines.append("书架(书名 → rel,开书用 __voiceOpenBook + 对应 rel):")
        for b in books[:80]:
            lines.append(f"  {b.get('name')} → {b.get('rel')}")
    nodes = (context or {}).get("visible_kg_nodes") or []
    if nodes:
        _kg = []   # 接线 KG summary(数据早在 ctx,原来只用 name)
        for n in nodes[:30]:
            _nm = (n.get("name") or "").strip()
            if not _nm:
                continue
            _sm = (n.get("summary") or "").strip()
            _kg.append(_nm + (f"——{_sm[:120]}" if _sm else ""))
        if _kg:
            lines.append("当前页知识点:" + "；".join(_kg))
    vocab = (context or {}).get("visible_vocab") or []
    if vocab:
        lines.append("当前页生词:" + "、".join(str(v) for v in vocab[:40]))
    return "\n".join(lines) if lines else "(无)"


# ── 预热常驻大脑(「开一个 claude 待机」)──
# 预热一个 claude --print stream-json 进程 idling(已完成 node 启动+鉴权,阻塞等 stdin);
# 来命令直接喂 → 只付推理(实测省 ~2.4s 冷启动);每轮用完即弃 + 后台补热 = 无历史累积(无状态)。
# 只在「聆听中」保活(前端 start 预热 / stop 回收),不长期空挂。失败回 None → 上层冷调用兜底。
# gunicorn 单进程多线程,模块级单例 + _brain_lock 串行(语音命令本就一条条来)。
_APP_CLAUDE = os.environ.get("APP_CLAUDE") or "claude"
_brain_lock = threading.Lock()
_brain_proc = None
_brain_wanted = False   # 仅聆听期间为 True;闸住「用完补热」与「停止回收」的竞态,停后不空挂


def _brain_spawn():
    try:
        return subprocess.Popen(
            [_APP_CLAUDE, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--model", "haiku", "--effort", "low"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=str(CLAUDE_DIR))
    except Exception:
        return None


def _brain_kill(p):
    if not p:
        return
    try:
        if p.stdin:
            p.stdin.close()
    except Exception:
        pass
    try:
        p.terminate()
    except Exception:
        pass


def _brain_prewarm():
    """聆听开始:置 wanted + 确保有一个 idling 进程待命。"""
    global _brain_proc, _brain_wanted
    with _brain_lock:
        _brain_wanted = True
        if _brain_proc is not None and _brain_proc.poll() is None:
            return
        _brain_proc = _brain_spawn()


def _brain_respawn():
    """用完后台补热:仅当仍在聆听(wanted)才补,避免停止后留下空挂进程。"""
    global _brain_proc
    with _brain_lock:
        if not _brain_wanted:
            return
        if _brain_proc is not None and _brain_proc.poll() is None:
            return
        _brain_proc = _brain_spawn()


def _brain_reap():
    global _brain_proc, _brain_wanted
    with _brain_lock:
        _brain_wanted = False
        p, _brain_proc = _brain_proc, None
    _brain_kill(p)


def _brain_ask(prompt: str, timeout: float = 28.0):
    """用预热进程发一轮,返回文本;失败/超时回 None(上层冷调用兜底)。用完丢弃 + 后台补热。"""
    global _brain_proc
    with _brain_lock:
        p, _brain_proc = _brain_proc, None
        if p is None or p.poll() is not None:
            _brain_kill(p)
            p = _brain_spawn()
        if p is None:
            return None
        out = None
        try:
            p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            p.stdin.flush()
            t0 = time.time()
            while time.time() - t0 < timeout:
                r, _, _ = select.select([p.stdout], [], [], 0.5)
                if r:
                    ln = p.stdout.readline()
                    if not ln:
                        break
                    if '"type":"result"' in ln:
                        try:
                            out = (json.loads(ln).get("result") or "").strip()
                        except Exception:
                            out = None
                        break
                if p.poll() is not None:
                    break
        except Exception:
            out = None
        finally:
            _brain_kill(p)
            threading.Thread(target=_brain_respawn, daemon=True).start()   # 后台补热(仅聆听中,停后不补)
    return out or None


# ── 豆包(火山方舟 Ark)大脑:server-config voice.brain=='doubao' 时优先走 ──
# 语音对话场景:中文应答自然 + flash 档快且便宜(省 Claude 额度)。失败(模型未开通/网络)秒回落原 Claude 链,
# 用户无感;在方舟控制台开通模型后无需改代码自动生效。key: ~/.config/doubao-api-key(600,不进 git)。
_DOUBAO_KEY_FILE = Path("~/.config/doubao-api-key").expanduser()


def _voice_cfg() -> dict:
    try:
        cfg = json.loads((CLAUDE_DIR / "state" / "server-config.json").read_text("utf-8"))
        return cfg.get("voice") or {}
    except Exception:
        return {}


def _doubao_ask(prompt: str, timeout: float = 20.0) -> str:
    """单发豆包 chat(OpenAI 兼容)。失败返回 ""(调用方回落 Claude)。"""
    try:
        key = _DOUBAO_KEY_FILE.read_text().strip()
    except Exception:
        return ""
    if not key:
        return ""
    model = _voice_cfg().get("doubao_model") or "doubao-seed-1-6-flash-250828"
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
    if "seed-1-6" in model:
        body["thinking"] = {"type": "disabled"}   # 语音要快:关深度思考(仅 1.6 系列认这个字段)
    try:
        r = requests.post("https://ark.cn-beijing.volces.com/api/v3/chat/completions",
                          headers={"Authorization": f"Bearer {key}"}, json=body, timeout=timeout)
        d = r.json()
        if d.get("error"):
            sys.stderr.write(f"[voice doubao] {d['error'].get('message', '')[:80]}\n")
            return ""
        return (d["choices"][0]["message"].get("content") or "").strip()
    except Exception as ex:
        sys.stderr.write(f"[voice doubao] {ex}\n")
        return ""


def _llm_intent(transcript: str, context: dict) -> dict:
    """Claude 把口令映射成真实动作(或简短作答)。返回定型结构,动作经白名单校验。"""
    ctx = context or {}
    pt = ctx.get("page_type")
    meta = {k: ctx.get(k) for k in ("page_type", "book_name", "page", "total", "read_mode", "selection") if ctx.get(k)}
    meta_txt = json.dumps(meta, ensure_ascii=False)[:500]
    # 内容类提问且在 PDF 页 → 取当前页正文喂给大脑(翻页/缩放等命令不触发,省 PDF 开销)
    page_block = ""
    if pt == "pdf" and _CONTENT_Q.search(transcript or ""):
        # Phase2:本页简述在手 → 注要点替整页(几十字够这条轻链路的 ≤2 句作答,省 token);缺失才降级注整页正文
        _brief_ln = ""
        try:
            _rel = (ctx.get("file_rel") or "").strip()
            if _rel and ".." not in _rel:
                _ap = (VAULT_ROOT / _rel).resolve(); _ap.relative_to(VAULT_ROOT.resolve())
                _brief_ln = _pdf_mod()._brief_inject_text(_ap, ctx.get("page", 0))
        except Exception:
            _brief_ln = ""
        if _brief_ln:
            page_block = f"\n\n【当前页要点(第{ctx.get('page', '?')}页,回答本页内容问题用)】\n{_brief_ln}"
        else:
            pgtxt = _page_text(ctx.get("file_rel", ""), ctx.get("page", 0))
            if pgtxt:
                page_block = f"\n\n【当前页正文(第{ctx.get('page', '?')}页,回答本页内容问题用)】\n{pgtxt}"
    prompt = (f"{_LLM_SYS}\n\n【可用动作清单】\n{_action_catalog(pt)}\n\n"
              f"【当前可见实体(谐音映射用)】\n{_entities_block(ctx)}\n\n"
              f"【当前页面】{meta_txt}{page_block}\n【用户说(语音,可能有错字)】{transcript}\n\n只输出 JSON:")
    raw = ""
    try:
        if _voice_cfg().get("brain") == "doubao":
            raw = (_doubao_ask(prompt) or "").strip()   # 豆包大脑(中文快+省 Claude 额度);失败落回下面 Claude 链
        if not raw:
            raw = (_brain_ask(prompt) or "").strip()   # 预热常驻进程,快
        if not raw:                                 # 没预热进程/失败 → 冷调用兜底
            sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
            import ai_client
            raw = (ai_client.ask(prompt, claude_model="haiku", claude_effort="low") or "").strip()
    except Exception as e:
        return {"speak": "我这边出了点问题:" + str(e)[:80],
                "client_actions": [], "server_results": [], "confirm": None}
    data = _extract_json(raw) or {}
    say = (data.get("say") or "").strip()
    actions = _validate_actions(data.get("actions"))
    if not say:
        # JSON 解析失败但原文像句答话 → 当作答;否则兜底
        say = raw if (raw and len(raw) < 200 and "{" not in raw) else (
            "好的" if actions else "没太听清,能再说一遍吗?")
    return {"speak": say, "client_actions": actions, "server_results": [], "confirm": None}


@bp.route("/agent", methods=["POST"])
def voice_agent():
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    transcript = (body.get("transcript") or "").strip()
    context = body.get("context") or {}
    if not transcript:
        return jsonify({"ok": False, "error": "empty transcript"}), 400
    out = _fast_intent(transcript, context)        # 常用口令:规则即时执行,不走 LLM
    if out is None:
        out = _composite_intent(transcript, context)   # 综合任务:后台 worker 编排多 skill
    if out is None:
        out = _llm_intent(transcript, context)     # 兜底:Claude 映射成真实动作 / 简短作答
    return jsonify({"ok": True, **out})


@bp.route("/ping")
def voice_ping():
    return jsonify({"ok": True, "has_gcp_key": bool(_gcp_key()), "ffmpeg": _has_ffmpeg()})


@bp.route("/prewarm", methods=["POST"])
def voice_prewarm():
    """聆听开始→预热待命大脑;聆听结束→回收,不长期空挂。fire-and-forget。"""
    if not _logged_in():
        return jsonify({"ok": False}), 401
    off = bool((request.get_json(silent=True) or {}).get("off"))
    threading.Thread(target=(_brain_reap if off else _brain_prewarm), daemon=True).start()
    return jsonify({"ok": True})


# ════════════════ 综合任务(后台 worker:多 skill 编排,锁屏不断)════════════════
# 复用现有稳定能力进程内直调(绕 session 鉴权):pdf_reader._run_snippets_to(笔记/制卡,opus)、
# build_vocab_note.update_word_note + anki_from_word.make_card(单词闭环)、_book_text_index(搜索)。
# 任务跑在服务端线程,前端轮询 /task-status,断连/锁屏不影响。这 4 类都是新增性质,不弹确认。
_vtasks = {}
_vtasks_lock = threading.Lock()
_vtask_seq = 0
_VTASK_KINDS = ("note", "anki", "vocab", "search")
_task_sema = threading.Semaphore(2)   # 限并发综合任务(每个可能跑 opus,防 Pi 过载/烧额度),超出排队
_anki_lock = threading.Lock()         # AnkiConnect 单实例:制卡/单词任务串行打它,防并发丢卡

# ── 撤销:凡写操作(制卡/笔记/生词)记下「句柄」,assistant /undo 反向(删卡/删笔记)──
import urllib.request as _ur

_undo_log = []
_undo_lock = threading.Lock()
_undo_seq = 0
_UNDO_FILE = CLAUDE_DIR / "state" / "assistant-undo-log.json"


def _undo_save():
    """把撤销记录落盘:webapp 重启(部署/凌晨 daily/崩溃)后『撤销』仍有效。
    卡/笔记的 id·路径在 Anki/vault 里本就是持久的,内存里丢的只是这张『记得删哪几张』的映射表,落盘即补上。
    须在 _undo_lock 内调(读 _undo_log/_undo_seq)。"""
    try:
        _UNDO_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _UNDO_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seq": _undo_seq, "log": _undo_log[-80:]}, ensure_ascii=False), "utf-8")
        tmp.replace(_UNDO_FILE)   # 原子替换,防写一半被读到
    except Exception:
        pass


def _undo_load():
    """进程启动时从盘里恢复撤销记录 + seq(seq 续上,旧 undo_id 仍能对上号)。"""
    global _undo_seq, _undo_log
    try:
        d = json.loads(_UNDO_FILE.read_text("utf-8"))
        _undo_log = d.get("log") or []
        _undo_seq = int(d.get("seq") or 0)
    except Exception:
        pass


_undo_load()


def _anki_req(action, params=None):
    rq = json.dumps({"action": action, "version": 6, "params": params or {}}).encode()
    url = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
    with _ur.urlopen(_ur.Request(url, data=rq, headers={"Content-Type": "application/json"}), timeout=10) as r:
        return json.loads(r.read())


def _undo_record(kind, label, handle, owner=None):
    global _undo_seq
    with _undo_lock:
        _undo_seq += 1
        uid = f"u{_undo_seq}"
        _undo_log.append({"id": uid, "kind": kind, "label": label, "handle": handle,
                          "owner": owner, "ts": time.time(), "undone": False})   # owner=用户隔离,防越权撤别人的写操作
        if len(_undo_log) > 80:
            del _undo_log[:-80]
        _undo_save()   # 落盘:重启后仍能按 note_ids 删卡
    return uid


def _undo_list(n=12):
    with _undo_lock:
        return [{"id": e["id"], "kind": e["kind"], "label": e["label"], "undone": e["undone"]} for e in _undo_log[-n:][::-1]]


def _undo_do(uid=None, owner=None):
    with _undo_lock:
        tgt = None
        for e in reversed(_undo_log):
            if e["undone"]:
                continue
            if owner is not None and e.get("owner") != owner:   # 用户隔离:传了 owner 就只撤销自己的(防越权删别人的卡/笔记/高亮)
                continue
            if uid is None or e["id"] == uid:
                tgt = e
                break
        if not tgt:
            return {"ok": False, "error": "没有可撤销的操作了"}
        tgt["undone"] = True
        _undo_save()   # 先记下『已撤销』(防删卡途中崩溃后重启又被当可撤销重删)
        kind, handle, label = tgt["kind"], tgt["handle"], tgt["label"]
    try:
        if kind in ("anki", "vocab"):
            ids = [i for i in (handle.get("note_ids") or ([handle.get("card_id")] if handle.get("card_id") else [])) if i]
            if ids:
                _anki_req("deleteNotes", {"notes": ids})
                try:
                    _anki_req("sync")
                except Exception:
                    pass
        elif kind == "note":
            p = (handle.get("path") or "").strip()
            if p and ".." not in p:
                ap = (VAULT_ROOT / p).resolve()
                ap.relative_to(VAULT_ROOT.resolve())
                if ap.exists():
                    ap.unlink()
        elif kind == "highlight":
            fr = (handle.get("file_rel") or "").strip()
            ids = set(handle.get("ids") or [])
            if fr and ids:
                import pdf_reader
                with pdf_reader._hl_edit(fr) as db:
                    db["highlights"] = [
                        h for h in db.get("highlights", [])
                        if h.get("id") not in ids
                    ]
        elif kind == "sticky":   # AI 建的便签:撤销=删掉(notes sidecar,PDF/EPUB 同一套)
            fr = (handle.get("file_rel") or "").strip()
            ids = set(handle.get("ids") or [])
            if fr and ids:
                import pdf_reader
                with pdf_reader._notes_edit(fr) as items:
                    items[:] = [n for n in items if n.get("id") not in ids]
        elif kind == "dict_fix":   # 词典修正撤销:恢复 prev(首次修正 prev=None → 删条目)
            w2 = (handle.get("word") or "").strip()
            if w2:
                import assistant as _as_df
                d2 = _as_df._dict_ovr_load()
                if handle.get("prev") is None:
                    d2.pop(w2, None)
                else:
                    d2[w2] = handle["prev"]
                _as_df._dict_ovr_save(d2)
        elif kind == "sticky_edit":   # AI 改的便签文字/颜色:撤销=恢复旧值快照(绝不动 strokes/anchor/尺寸)
            fr = (handle.get("file_rel") or "").strip()
            nid = handle.get("id")
            old = handle.get("old") or {}
            if fr and nid:
                import pdf_reader
                with pdf_reader._notes_edit(fr) as items:
                    n = next((x for x in items if x.get("id") == nid), None)
                    if n:
                        for k in ("text", "color"):
                            if k in old:
                                n[k] = old[k]
                        n["updated"] = int(time.time())
        return {"ok": True, "label": label, "kind": kind}   # kind 给前端:highlight/sticky 撤销后要重渲页面才能视觉清掉
    except Exception as e:
        with _undo_lock:
            tgt["undone"] = False   # 撤销失败 → 恢复可撤销
            _undo_save()
        return {"ok": False, "error": str(e)[:120]}


_CLI_TASK_DIR = Path("/home/bwicarus/claude/state/cli-tasks")   # vtask 落盘:重启不蒸发 + 铸造窗口 30 天(审查实锤:原先内存 30min)


def _vtask_persist(tid):
    """把 vtask 快照落盘(轻:只在关键节点调——status/steps 变化;不含 base64 类大字段)。"""
    try:
        with _vtasks_lock:
            v = _vtasks.get(tid)
            if not v:
                return
            snap = {k: v.get(k) for k in ("id", "kind", "status", "step", "speak", "client_actions",
                                          "result", "error", "ts", "steps", "instruction", "pid", "recipe", "orch")}
        _CLI_TASK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CLI_TASK_DIR / (tid + ".json.tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False, default=str), "utf-8")
        tmp.replace(_CLI_TASK_DIR / (tid + ".json"))
    except Exception:
        pass


def _vtask_disk_get(tid):
    try:
        return json.loads((_CLI_TASK_DIR / (tid + ".json")).read_text("utf-8"))
    except Exception:
        return {}


def _cli_tasks_boot_scan():
    """webapp 启动扫描:上一进程留下的非终态任务 → 标 error(否则前端空转 15 分钟才认输);
    记录的 CLI 子进程还活着且确是 claude/codex → 已成孤儿,杀掉(防继续烧额度)。顺手清 30 天前的老文件。"""
    try:
        if not _CLI_TASK_DIR.exists():
            return
        now = time.time()
        for f in _CLI_TASK_DIR.glob("vt*.json"):
            try:
                if now - f.stat().st_mtime > 30 * 86400:
                    f.unlink()
                    continue
                d = json.loads(f.read_text("utf-8"))
                if d.get("status") in ("done", "error"):
                    continue
                pid = d.get("pid")
                if pid:
                    try:
                        cmdl = Path("/proc/%d/cmdline" % int(pid)).read_bytes().decode(errors="replace")
                        if "claude" in cmdl or "codex" in cmdl:
                            os.kill(int(pid), 9)
                    except Exception:
                        pass
                d["status"], d["error"] = "error", "服务重启,任务已中断(重发一次即可)"
                f.write_text(json.dumps(d, ensure_ascii=False, default=str), "utf-8")
            except Exception:
                continue
    except Exception:
        pass


_cli_tasks_boot_scan()   # import 即扫:上一进程的未完任务标 error + 清孤儿 CLI


def _vtask_new(kind: str) -> str:
    global _vtask_seq
    with _vtasks_lock:
        _vtask_seq += 1
        tid = f"vt{int(time.time())}_{_vtask_seq}"
        _vtasks[tid] = {"id": tid, "kind": kind, "status": "running", "step": "",
                        "speak": "", "client_actions": [], "result": None, "error": "", "ts": time.time(),
                        "steps": []}   # 工具指示器 v2:内部步骤流水(长条滚动 + 「!」面板逐步查看)
        for k in [k for k, v in _vtasks.items() if time.time() - v.get("ts", 0) > 1800]:
            _vtasks.pop(k, None)   # 清 30min 前的老任务
        return tid


def _vtask_set(tid, **kw):
    with _vtasks_lock:
        if tid in _vtasks:
            _vtasks[tid].update(kw)
    if any(k in kw for k in ("status", "steps", "result", "error", "pid", "instruction", "recipe")):
        _vtask_persist(tid)   # 关键节点落盘(step 心跳类不写,防高频 IO)


def _vtask_get(tid):
    with _vtasks_lock:
        v = dict(_vtasks.get(tid) or {})
    return v or _vtask_disk_get(tid)   # 内存 miss(重启/30min TTL)→ 读盘:铸造窗口变 30 天


def _pdf_mod():
    import pdf_reader   # 同一 webapp 进程,已加载
    return pdf_reader


def _deep_link(base, file_rel, page):
    from urllib.parse import quote
    b = (base or "").rstrip("/")
    if isinstance(file_rel, str) and file_rel.startswith("web:"):
        # 审计 #5:网页材料的出处链接必须指向实况阅读器,旧写法 /pdf/view?file=web:… 实测 404,
        # 而它会被强制写进 Anki 卡背 → 永久死链。
        return f"{b}/pdf/web/live?url={quote(file_rel[4:], safe='')}"
    return f"{b}/pdf/view?file={quote(file_rel or '', safe='')}&page={page or 1}"


def _gen_title(content: str) -> str:
    """给笔记起个简短中文标题(haiku 快)。失败回空,调用方有兜底。"""
    try:
        sys.path.insert(0, str(CLAUDE_DIR / "scripts"))
        import ai_client
        t = (ai_client.ask("给下面内容起一个简短中文笔记标题,≤12字,只输出标题、不要书名号标点:\n\n" + content[:800],
                           claude_model="haiku", claude_effort="low") or "").strip()
        return t.splitlines()[0].strip().strip("《》\"'。，,. ")[:20] if t else ""
    except Exception:
        return ""


def _content_for(params, ctx):
    _sel = (ctx.get("selection") or "").strip()
    if (params.get("text") or "").strip():        # agent 工具显式给的内容
        base_c = params["text"].strip()
        # ★护栏(用户实锤 2026-07-19):有选中/钉住焦点时,agent 却传了**整页级**的大段文本
        #   (它把 read_page 的结果当 text 塞回来)→ 用户明明只想给那一段做卡。
        #   判据:选中存在 且 agent 给的文本比选中长得多 且 包含选中 ⇒ 收敛回选中。
        if _sel and len(base_c) > max(400, len(_sel) * 3) and _sel[:40] in base_c:
            base_c = _sel
    else:
        scope = params.get("scope") or ("sel" if _sel else "page")
        base_c = _sel if scope == "sel" else _page_text(ctx.get("file_rel", ""), ctx.get("page", 0))
    extra = (params.get("extra_ctx") or "").strip()   # 61b:对话现场(网页搜索/配图/近几轮)随卡走,制卡 AI 自行取舍
    if extra and base_c:
        base_c += "\n\n【通话现场资料(网页搜索结果/配图/近几轮对话;与主题相关就采用进卡片/笔记,无关的忽略)】\n" + extra
    return base_c


def _task_note(tid, params, ctx, base):
    content = _content_for(params, ctx)
    if not content or len(content) < 10:
        _vtask_set(tid, status="error", error="没找到要整理的内容(先选中文字或翻到有内容的页)")
        return
    _vtask_set(tid, step="拟标题")
    book = (ctx.get("book_name") or "").rsplit(".", 1)[0]
    title = _gen_title(content) or ((book + " 笔记") if book else "语音笔记")
    _vtask_set(tid, step="AI 整理中")
    link = _deep_link(base, ctx.get("file_rel", ""), ctx.get("page", 1))
    out = _pdf_mod()._run_snippets_to([{"text": content, "source": link}], True, False, title, "opus", "high")
    if out.get("ok"):
        uid = _undo_record("note", f"笔记《{title}》", {"path": out.get("note_path")}, owner=ctx.get("_uid"))
        _vtask_set(tid, status="done", speak=f"整理好了，笔记叫《{title}》",
                   result={"note_path": out.get("note_path"), "obsidian_url": out.get("obsidian_url"), "undo_id": uid})
    else:
        _vtask_set(tid, status="error", error=out.get("error", "整理失败"))


def _task_anki(tid, params, ctx, base):
    content = _content_for(params, ctx)
    if not content or len(content) < 6:
        _vtask_set(tid, status="error", error="没找到要做卡的内容(先选中文字)")
        return
    _vtask_set(tid, step="AI 制卡中")
    link = _deep_link(base, ctx.get("file_rel", ""), ctx.get("page", 1))
    text = content + f"\n\n【原文出处链接(务必原样放进卡片背面,做成可点链接)】{link}"
    image_url = (params.get("image_url") or "").strip()   # 助手 search_image 找到的图,若也要放进卡片就一并透传
    # 工具指示器 v2:把制卡内部阶段实时吐给前端(长条态滚动;steps 累积供「!」面板逐步查看)
    _steps = []

    def _on_step(s):
        _steps.append({"label": s, "t": round(time.time(), 1)})
        _vtask_set(tid, step=s, steps=list(_steps))

    # 2026-07-21 用户拍板:制卡工具统一走**草稿预览确认**(未确认不入库,与选段🎴/B1 一致);
    #   直接入库另立工具(未讨论,不在 make_anki)。→ defer_add=True 只生成卡草稿,前端确认后经
    #   /pdf/api/anki-add-cards 入库。AnkiConnect 不再在此调用 → 不必占 _anki_lock。
    _req = (params.get("requirement") or "").strip()
    _ex = (params.get("extra_ctx") or "").strip()   # 对话现场(含用户原话/要求)——此前没传进制卡=要求丢失(用户实锤)
    _fullreq = (_req + ("\n" + _ex if _ex else "")).strip()
    out = _pdf_mod()._run_snippets_to([{"text": text, "source": link}], False, True, "", "opus", "high",
                                      image_url=image_url or None, defer_add=True, requirement=_fullreq, on_step=_on_step)
    cards = out.get("anki_cards") or []
    if out.get("ok") and cards:
        _brief = []
        for c in cards[:12]:
            _f = (c.get("cloze") or c.get("front") or "").strip().replace("\n", " ")[:60]
            _b = (c.get("back") or "").strip().replace("\n", " ")[:40]
            _brief.append(_f + ((" → " + _b) if _b else ""))
        result = {"kind": "anki", "deferred": True, "n": len(cards), "cards_brief": _brief,
                  "deck": out.get("anki_deck") or "QA", "cards": cards, "source_ref": link[:4096]}
        selected = str(ctx.get("selection") or "").strip()
        if selected:
            if ctx.get("current_section_idx") is not None:
                try:
                    section = int(ctx.get("current_section_idx"))
                except (TypeError, ValueError):
                    section = -1
                if section >= 0:
                    result["source_highlight"] = {
                        "file": str(ctx.get("file_rel") or ""),
                        "target": {"kind": "epub", "section": section},
                        "text": selected[:8000], "color": "green",
                        "note": "Reader 卡片来源",
                    }
            else:
                try:
                    page = int(ctx.get("page") or 0)
                except (TypeError, ValueError):
                    page = 0
                if page >= 1:
                    result["source_highlight"] = {
                        "file": str(ctx.get("file_rel") or ""),
                        "target": {"kind": "pdf", "page": page},
                        "text": selected[:8000], "color": "green",
                        "note": "Reader 卡片来源",
                    }
        _vtask_set(tid, status="done", speak=f"做好了{len(cards)}张卡片草稿，在卡片上确认后入库",
                   steps=list(_steps),
                   result=result)
    elif out.get("ok"):
        _vtask_set(tid, status="error", error="AI 没生成卡片(内容可能不适合制卡)")
    else:
        _vtask_set(tid, status="error", error=out.get("error") or out.get("anki_error") or "制卡失败")


def _task_vocab(tid, params, ctx, base):
    word = (params.get("word") or ctx.get("selection") or "").strip()
    if not word:
        _vtask_set(tid, status="error", error="没拿到要学的单词(先选中那个词)")
        return
    try:
        sys.path.insert(0, str(CLAUDE_DIR / "scripts" / "vocab"))
        import build_vocab_note
        import anki_from_word
        _vtask_set(tid, step="查词建生词本")
        src = {"pdf": ctx.get("file_rel", ""), "page": ctx.get("page", 0), "context": ctx.get("selection", "")}
        build_vocab_note.update_word_note(word, add_source=src, online=True, download_audio=True)
        _vtask_set(tid, step="制卡")
        with _anki_lock:   # AnkiConnect 串行
            r = anki_from_word.make_card(word, force=False)
        if r.get("ok"):
            uid = _undo_record("vocab", f"{word} 生词卡", {"card_id": r.get("note_id")}, owner=ctx.get("_uid"))
            _vtask_set(tid, status="done", speak=f"{word} 已加到生词本并制卡了", result={"undo_id": uid})
        else:
            _vtask_set(tid, status="done", speak=f"{word} 已加到生词本(制卡没成:{r.get('error', '?')})")
    except Exception as e:
        _vtask_set(tid, status="error", error=str(e)[:160])


def _task_search(tid, params, ctx, base):
    q = (params.get("query") or "").strip()
    if len(q) < 2:
        _vtask_set(tid, status="error", error="没听清要找什么")
        return
    file_rel = ctx.get("file_rel", "")
    if not file_rel:
        _vtask_set(tid, status="error", error="先打开一本书再搜")
        return
    _vtask_set(tid, step="搜索中")
    try:
        ap = (VAULT_ROOT / file_rel).resolve()
        idx = _pdf_mod()._book_text_index(str(ap), file_rel)
        ql = q.lower()
        hits = []
        for ps, txt in idx.items():
            low = (txt or "").lower()
            pos = low.find(ql)
            if pos >= 0:
                snip = (txt[max(0, pos - 20):pos + len(q) + 30] or "").replace("\n", " ").strip()
                hits.append((int(ps), snip))
        hits.sort()
        if not hits:
            _vtask_set(tid, status="done", speak=f"这本书里没找到讲「{q}」的地方")
            return
        p0, snip0 = hits[0]
        more = f"，共{len(hits)}处" if len(hits) > 1 else ""
        _vtask_set(tid, status="done", speak=f"在第{p0}页找到了{more}，{snip0[:40]}",
                   client_actions=[{"fn": "goToPage", "args": [p0]}],
                   result={"hits": [{"page": p, "snippet": s} for p, s in hits[:8]]})
    except Exception as e:
        _vtask_set(tid, status="error", error=str(e)[:160])


# ── 147(用户点子):**通用 agent worker** ────────────────────────────────────────────
#   把「要连着调好几个工具才能干完」的任务整个甩给**无头 Claude CLI + 我们自己的 MCP**。
#   它自己规划、自己调工具、干完回一句话 —— 语音模型只花 **1 次**工具调用。
#
#   为什么值:一个 3 步任务走语音模型 ≈ 4~6 次 realtime response(每次都把整段会话按 input 重算,
#   而且工具结果全堆进语音上下文);走 worker = **2 次 response**,语音模型只看见最后那句摘要。
#   省的是 N-1 轮 realtime + 上下文膨胀。
#
#   CLI 走**订阅额度**(不是 API 计费)→ 对我们是白捡的算力。所以选型**只看成功率和速度**:
#     实测同一个 3 步任务:opus 3轮/6.8s(最稳) · sonnet 4轮/13.6s · haiku 7轮/29s(乱,还去试 Bash/Read)
#     → 默认 opus。
#
#   ⚠ **安全(实测踩到)**:`--allowedTools` 只是「**自动批准**」名单,**不是「能力」名单** ——
#     不在名单里的工具**依然存在**,haiku 真的去调了 Bash 和 Read。必须再用 `--disallowedTools`
#     把本地工具明确掐掉。加上之后 CLI 会老实回「没有 Bash 工具可用」。
_AGENT_DENY = ("Bash,Read,Write,Edit,MultiEdit,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit,BashOutput,KillShell"
               ",mcp__bwapp__assistant_log_chat,mcp__bwapp__assistant_history")   # 147d:MCP server 的 instructions 让**外部编排 agent**
#   每轮把对话写进助手历史(assistant_log_chat)——worker 是内部执行体,不需要,实测它真去调了,白烧 2 轮(17s→本可更快)
_AGENT_TIMEOUT = int(os.environ.get("AGENT_TASK_TIMEOUT", "240"))


def _agent_mcp_cfg() -> str:
    """无头 CLI 用的 MCP 配置(HTTP + 静态 Bearer)。600 权限,含 token,别进 git。"""
    import stat
    f = CLAUDE_DIR / "state" / "mcp-headless.json"
    tok = (Path("~/.config/mcp-http-token").expanduser().read_text().strip())
    f.write_text(json.dumps({"mcpServers": {"bwapp": {
        "type": "http", "url": os.environ.get("MCP_PUBLIC_URL", "https://bwicarus.space/mcp"),
        "headers": {"Authorization": "Bearer " + tok}}}}), encoding="utf-8")
    f.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return str(f)


_agent_cat = {"t": 0.0, "txt": ""}


def _agent_catalog() -> str:
    """147b(用户点子):**把工具目录预先写进 system prompt**。
    Claude Code 把 MCP 工具做成**延迟加载**(实测:初始工具 21 个,MCP 直接可见 **0** 个;
    --settings 里塞各种候选键都关不掉)→ 模型必须先 ToolSearch 才拿得到 schema。
    不给目录时它会"搜关键词 → 再搜 → 试错",白烧好几轮;把目录直接摆在 system prompt 里 +
    要求它**一次 `select:` 精确加载、禁止探索** → 轮数压到理论下限。
    实测同一个任务:15.5s / 4 轮 → **6.7s / 3 轮**(ToolSearch → read_page → 回答)。10 分钟缓存。"""
    if time.time() - _agent_cat["t"] < 600 and _agent_cat["txt"]:
        return _agent_cat["txt"]
    try:
        import urllib.request
        tok = Path("~/.config/mcp-http-token").expanduser().read_text().strip()
        url = os.environ.get("MCP_PUBLIC_URL", "https://bwicarus.space/mcp")
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "Authorization": "Bearer " + tok}

        def post(body, sid=None):
            hh = dict(h)
            if sid:
                hh["mcp-session-id"] = sid
            r = urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers=hh), timeout=20)
            return r.read().decode(), dict(r.headers)

        _b, hd = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "voice-agent", "version": "1"}}})
        sid = hd.get("mcp-session-id")
        post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        body, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, sid)
        lines = []
        for ln in body.splitlines():
            if ln.startswith("data:"):
                for t in (json.loads(ln[5:])["result"]["tools"]):
                    lines.append(f"- mcp__bwapp__{t['name']}: {(t.get('description') or '')[:80]}")
        _agent_cat["txt"] = "\n".join(lines)
        _agent_cat["t"] = time.time()
    except Exception as e:
        sys.stderr.write(f"[agent-catalog] {e}\n")
    return _agent_cat["txt"]


# 148:worker 后端**正常由 per-action 预设决定**(设置面板「agent」行 → params.backend);
#   这个 env 只是 params 没传时的兜底默认。默认 claude(用户 Max 5x,额度够用,且 opus worker
#   耗时几乎不随步数增长:2步 9.1s / 5步 11.1s,而 codex 是 15.3s / 18.8s)。
#   主后端挂了会自动换另一个 CLI 重跑(claude ↔ codex 双向兜底,见 _task_agent)。
_AGENT_BACKEND = os.environ.get("AGENT_TASK_BACKEND", "claude").strip().lower()   # claude | codex


def _agent_codex_fast_ok(model: str) -> bool:
    """Secondary Fast capability fence for the standalone voice worker."""
    try:
        import assistant
        return assistant._codex_fast_ok(model) is True
    except Exception:
        return False


def _agent_codex_cmd(
        prompt: str, model: str = "", effort: str = "", fast: bool = False) -> list:
    """148:codex exec 当 MCP worker —— **走 ChatGPT 订阅额度,与 Claude 额度完全独立**(白捡一路)。

    ⚠ **唯一的关键是 `default_tools_approval_mode="approve"`**(2026-07-14 单变量隔离实测):
      不加它 → MCP 工具被当成「需要人工审批」(用户 config.toml 里 approval_policy=on-request +
      approvals_reviewer=user),无头下没人批 → 立刻 `user cancelled MCP tool call`。
      那句措辞极具误导性(根本没人取消);debug 日志里的 `SSE stream disconnected /
      hyper::Error(IncompleteMessage)` **只是取消后 turn 被 abort 的下游现象,不是根因**。
      (cookbook 说 exec 模式 MCP 自动批准 —— 与实际不符,别信。)

    ⚠ **`features.shell_tool=false` 是安全底线**:worker 由语音驱动,绝不能让它跑 shell。
      (codex 的 `-s read-only` 只限制沙箱,shell 工具本身依然存在。)
    """
    url = os.environ.get("MCP_PUBLIC_URL", "https://bwicarus.space/mcp")
    # disabled_tools = codex 侧的 _AGENT_DENY:MCP server 的 instructions 让**外部编排 agent**每轮把对话
    #   写进助手历史(assistant_log_chat)——worker 是内部执行体,不需要;实测它真去调了,白烧一整轮。
    mcp = ('mcp_servers.bwapp={url="%s",bearer_token_env_var="BWAPP_TOKEN",required=true,'
           'default_tools_approval_mode="approve",'
           'disabled_tools=["assistant_log_chat","assistant_history"],'
           'startup_timeout_sec=20,tool_timeout_sec=60}' % url)
    cmd = [os.environ.get("APP_CODEX", "codex"), "exec", "--json",
           "--skip-git-repo-check", "--color", "never", "-s", "read-only",
           "-c", 'model="%s"' % (model or os.environ.get("AGENT_CODEX_MODEL", "gpt-5.6-luna")),
           "-c", 'model_reasoning_effort="%s"' % (effort or os.environ.get("AGENT_CODEX_EFFORT", "low"))]
    # Fast 是每个动作的显式选择,不能再用环境变量把全部 Codex worker 隐式升到 priority。
    # 上游 action-pref 已按模型能力校验 fast；这里仍严格只认 JSON bool true,避免
    # "false"/1 之类的宽松真值意外提高用量。Claude 路径从不消费该配置。
    selected_model = model or os.environ.get("AGENT_CODEX_MODEL", "gpt-5.6-luna")
    if fast is True and _agent_codex_fast_ok(selected_model):
        cmd += ["-c", 'service_tier="priority"']
    cmd += ["-c", mcp,
            "-c", "features.shell_tool=false",   # 安全底线:只能调 MCP
            prompt]
    return cmd


class _AgentTimeout(Exception):
    pass


_CA_SINK = {}   # tid → [client_action]:后台 agent 内部工具产生的 client_action(headless 不会转发,这里挖出)


def _extract_client_actions(txt):
    """从一段(可能被 MCP 再包一层的)tool_result 文本里挖出**所有** client_action 对象。
    括号配平 + 尊重字符串字面量/转义 —— 深嵌套的 __upStartTask(args→params→blocks[])也能完整吞下。
    非贪婪正则在这会翻车(blocks 第一个 `}}` 就截断),别退回去。"""
    out = []
    key = '"client_action"'
    i = 0
    while True:
        k = txt.find(key, i)
        if k < 0:
            break
        j = txt.find("{", k + len(key))
        if j < 0:
            break
        depth, in_str, esc, end = 0, False, False, -1
        for p in range(j, len(txt)):
            ch = txt[p]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = p
                    break
        if end < 0:
            break
        frag = txt[j:end + 1]
        try:
            ca = json.loads(frag)
            if isinstance(ca, dict):
                out.append(ca)
        except Exception:
            pass
        i = end + 1
    return out


def _unwrap_call(nm, inp):
    """CLI 只能经 MCP 的通用派发器 assistant_call_tool(name,args,file,page) 调内置工具。
    拆包成**真工具名 + 真 args**:① 工具卡显示真名(page_new 而非 assistant_call_tool×5)
    ② 保存轨迹时 name 对得上 assistant.TOOLS(裸名),run_trace 才能进程内回放(去壳)。
    file/page 不带进 args —— 回放时用**当前**书/页的 ctx(在当前位置重做,而非原书原页)。"""
    if nm == "assistant_call_tool" and isinstance(inp, dict) and inp.get("name"):
        return str(inp["name"]), (inp.get("args") if isinstance(inp.get("args"), dict) else {})
    return nm, (inp if isinstance(inp, dict) else {})


def _agent_run_cli(backend: str, prompt: str, sysp: str, tid, steps: list,
                   model: str = "", effort: str = "", fast: bool = False) -> str:
    """跑一个无头 CLI(claude | codex),流式解析事件驱动工具卡进度条。返回最终答案('' = 没给结果)。

    backend/model/effort/fast 由 assistant.py 的 **per-action 预设**(设置面板「agent」行)传进来;
    传空则回落 env → 出厂默认。两边的事件格式不同,但语义一一对应:
      claude  `--output-format stream-json`:{type:assistant, message.content[].type=tool_use} / {type:result}
      codex   `--json`:{type:item.started, item.type=mcp_tool_call} / {type:item.completed, item.type=agent_message}
    """
    codex = backend == "codex"
    if codex:
        # codex exec 没有 --append-system-prompt,并进 prompt
        cmd = _agent_codex_cmd(
            prompt + "\n\n" + sysp,
            model=model,
            effort=effort,
            fast=(fast is True),
        )
    else:
        cmd = [os.environ.get("APP_CLAUDE", "claude"), "-p", prompt,
               "--append-system-prompt", sysp,
               "--mcp-config", _agent_mcp_cfg(),
               "--allowedTools", "mcp__bwapp",
               "--disallowedTools", _AGENT_DENY,
               "--model", (model if model in ("haiku", "sonnet", "opus")
                           else os.environ.get("AGENT_TASK_MODEL", "opus")),
               "--setting-sources", "", "--output-format", "stream-json", "--verbose"]
        # claude 的 fast mode 是**默认开着**的(实测 DISABLE=1 会从 7.7s 慢到 9.1s),不用显式配。
        if effort in ("low", "medium", "high", "xhigh", "max"):
            cmd += ["--effort", effort]
    env = dict(os.environ)
    if codex:
        # codex 用 bearer_token_env_var 取 MCP token(不落盘,比 claude 的 --mcp-config 更干净)
        env["BWAPP_TOKEN"] = Path("~/.config/mcp-http-token").expanduser().read_text().strip()
    else:
        env["ENABLE_TOOL_SEARCH"] = "false"   # 147c:关掉工具延迟加载(见下)——轮数减半
    answer = ""
    _acc = ""   # CLI 输出的正文累积 → 增量推给前端(卡片 body 边跑边显示,像 web_search)
    _prose = ""  # 自上一个工具调用以来的散文(=决定下一步的思路)→ 随步存 rationale(节选保存时当"调用开端",用户设计)
    pr = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=env)
    _vtask_set(tid, pid=pr.pid)   # 落盘:重启扫描据此清孤儿 CLI(防继续烧额度)
    t0 = time.time()
    for line in pr.stdout:
        if time.time() - t0 > _AGENT_TIMEOUT:
            pr.kill()
            raise _AgentTimeout()
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if codex:
            it = d.get("item") or {}
            if d.get("type") == "item.started" and it.get("type") == "mcp_tool_call":
                nm = str(it.get("tool") or "").replace("mcp__bwapp__", "")
                if nm:
                    _arg = it.get("arguments")
                    try:
                        _arg = json.loads(_arg) if isinstance(_arg, str) else (_arg or {})
                    except Exception:
                        _arg = {}
                    nm, _arg = _unwrap_call(nm, _arg if isinstance(_arg, dict) else {})
                    steps.append({"name": nm, "status": "done", "args": _arg,
                                  "rationale": (_acc.strip()[-500:] or None)})   # codex 无 per-step 散文,拿当时累积文本近似
                    _vtask_set(tid, step=f"{nm}…", steps=list(steps))   # 工具卡长条实时滚
            elif d.get("type") == "item.completed" and it.get("type") == "agent_message":
                answer = (it.get("text") or "").strip()   # 覆盖式:最后一条 agent_message 即最终答案
                if answer:
                    _acc = answer
                    _vtask_set(tid, partial=_acc[:4000])   # 增量结果 → 前端卡片 body
        elif d.get("type") == "assistant":
            for c in ((d.get("message") or {}).get("content") or []):
                if c.get("type") == "tool_use":
                    nm = str(c.get("name") or "").replace("mcp__bwapp__", "")
                    if nm and nm != "ToolSearch":   # ToolSearch 是 CLI 内部找工具,不是"做事",别显示
                        _inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                        nm, _inp = _unwrap_call(nm, _inp)
                        # 记 name+args(输入)+ tool_use id(下面按它把工具**输出**配对挂回来,流程显示"输入→输出")
                        steps.append({"name": nm, "status": "done", "args": _inp, "_id": c.get("id"),
                                      "rationale": (_prose.strip()[-500:] or None)})   # 决定这一步之前 AI 说的话
                        _prose = ""
                        _vtask_set(tid, step=f"{nm}…", steps=list(steps))
                elif c.get("type") == "text" and (c.get("text") or "").strip():
                    _acc += c.get("text")   # claude 流式正文 → 边跑边推(增量结果显示在卡片 body)
                    _prose += c.get("text")
                    _vtask_set(tid, partial=_acc[:4000])
        elif d.get("type") == "user":
            # ★ 工具返回体里的 client_action(如 page_show 的 __upStartTask)—— headless agent 不会转发它,
            #   我们从 tool_result 里挖出来收集进 task,前端轮询 task-status 时应用 → 后台 agent 建的纸才真显示。
            #   (用户实测"页面没做成功"根因:do_task 就算调了造纸工具,client_action 也被丢在 stdout 里。)
            for c in ((d.get("message") or {}).get("content") or []):
                if c.get("type") != "tool_result":
                    continue
                _tc = c.get("content")
                _txt = _tc if isinstance(_tc, str) else " ".join(
                    (x.get("text") or "") for x in (_tc or []) if isinstance(x, dict))
                # 把工具**输出**按 tool_use_id 配对挂到对应步骤(去掉 client_action 那段 b64,只留可读结果)→ 流程显示输入/输出
                _tuid = c.get("tool_use_id")
                if _tuid:
                    import re as _re2
                    _clean = _re2.sub(r'"client_action"\s*:\s*\{.*', "", _txt, flags=_re2.S)[:500].strip()
                    for _st in steps:
                        if _st.get("_id") == _tuid:
                            _st["result"] = _clean
                            break
                    _vtask_set(tid, steps=list(steps))
                # ★ 括号配平提取(不能用非贪婪正则:__upStartTask 深嵌套 args→params→blocks[],
                #   `\{.*?\}\s*\}` 会在 blocks 第一个块的 `}}` 处截断 → json.loads 失败 → CLI 造的纸静默丢失,
                #   正是用户"页面没做成功"的根因)。
                for ca in _extract_client_actions(_txt):
                    if ca.get("fn"):
                        if ca["fn"] == "__upStartTask":
                            # ★用户设计(最佳实践:provenance 跟**工件**走,不靠时间侧信道猜关联):
                            #   把 CLI 到此为止的**查找类查询**(读了第几页/搜了什么)注进这张纸的 params
                            #   → run 记录 → 检查报告 → read_check_report 原样给出,后续 AI 可复用同样的查询。
                            try:
                                _, _lk0 = _flow_summary(steps)
                                _sp0 = (ca.get("args") or [None])[0]
                                if _lk0 and isinstance(_sp0, dict):
                                    _sp0.setdefault("params", {})["lookups"] = _lk0[:8]
                            except Exception:
                                pass
                        _CA_SINK.setdefault(tid, []).append(ca)
                        _vtask_set(tid, client_actions=list(_CA_SINK[tid]))
        elif d.get("type") == "result":
            answer = (d.get("result") or "").strip()
    pr.wait(timeout=10)
    return answer


# ★用户设计:CLI 流程摘要。**查找/查看类**工具(时效性/引用类)带**实际参数**(搜了啥、看第几页)→ 供 AI 复用查询;
#   机械类工具(建纸/加元素/写内容)一笔带过。{工具: (叙述模板, 从 args 取参数)}。
_LOOKUP_TOOLS = {
    "read_page":         ("读了第 {} 页", lambda a: a.get("page") or (a.get("pages") if not isinstance(a.get("pages"), list) else (a.get("pages") or [None])[0])),
    "search_book":       ("在这本书里搜了「{}」", lambda a: a.get("query")),
    "search_in_book":    ("在这本书里搜了「{}」", lambda a: a.get("query")),   # MCP 直连名(CLI 直调不经 assistant_call_tool;审查抓的:漏了它=搜索型出题查询全丢)
    "search_all_books":  ("跨书搜了「{}」", lambda a: a.get("query")),
    "web_search":        ("联网搜了「{}」", lambda a: a.get("query")),
    "lookup_word":       ("查了词「{}」", lambda a: a.get("word") or a.get("text")),
    "see_page":          ("看了第 {} 页的图", lambda a: a.get("page")),
    "see_figure":        ("看了带入的图", lambda a: None),
    "summarize_section": ("总结了第 {} 页所在章节", lambda a: a.get("page")),
    "toc":               ("看了目录", lambda a: None),
    "learning_focus":    ("查了「{}」的学习焦点", lambda a: a.get("when") or (("最近%s天" % a["days"]) if a.get("days") else "最近一周")),
    "read_selection":    ("读了选中内容", lambda a: None),
}


def _flow_summary(steps):
    """从 CLI steps 生成一句**流程叙述** + 结构化 lookups(可复用的查询)。查找类带实际参数,机械类归纳。"""
    parts, lookups = [], []
    made, blanks, btns = False, 0, 0
    for s in (steps or []):
        nm = s.get("name"); a = s.get("args") if isinstance(s.get("args"), dict) else {}
        if nm in _LOOKUP_TOOLS:
            tmpl, getk = _LOOKUP_TOOLS[nm]
            try:
                v = getk(a)
            except Exception:
                v = None
            if "{}" in tmpl and v not in (None, "", []):
                parts.append(tmpl.format(str(v)[:40])); lookups.append({"tool": nm, "arg": v})
            else:
                parts.append(tmpl.split("{")[0].rstrip("「 "))
                lookups.append({"tool": nm, "arg": None})
        elif nm in ("page_new", "page_add", "page_add_many", "page_show"):
            made = True
            for b in (a.get("blocks") or ([a] if a.get("kind") else [])):
                if isinstance(b, dict):
                    if b.get("kind") == "blank": blanks += 1
                    if b.get("kind") == "button": btns += 1
    if made:
        tail = "出了" + (f"{blanks}道题" if blanks else "一张纸")
        tail += "、放了个『让 AI 检查』按钮,现在等你作答" if btns else ",等你使用"
        parts.append(tail)
    return ("；".join(p for p in parts if p)), lookups


def _agent_registry_prompt() -> str:
    """Describe the one production registry to the CLI worker.

    The import stays lazy because ``app.py`` registers ``voice`` before
    ``assistant``.  Full schemas are still discovered through the MCP
    ``assistant_tools`` bridge and every call goes through
    ``assistant_call_tool`` into the registry-backed executor gate.
    """

    try:
        import assistant as A

        surface = A.SURFACE_MCP_WORKER
        groups = []
        for namespace in A.TOOL_REGISTRY.namespaces:
            count = len(
                A.TOOL_REGISTRY.tools_in(namespace.name, surface=surface)
            )
            if count:
                groups.append(f"{namespace.name}({count})")
        return (
            "【阅读器生产工具目录】"
            f"版本 {A.TOOL_REGISTRY.catalog_version}；"
            "工具域：" + "、".join(groups) + "。"
            "需要阅读器能力时先调用 assistant_tools 取得本版本的完整 schema，"
            "再用 assistant_call_tool 执行；不要根据旧记忆猜工具名或参数。"
        )
    except Exception:
        # 目录预读失败不能阻断已有 CLI 兜底，但必须强制重新发现，
        # 而不是相信 prompt 中可能过期的手写清单。
        return (
            "【阅读器生产工具目录暂不可预读】"
            "需要阅读器能力时必须先调用 assistant_tools，再用 "
            "assistant_call_tool 执行；不要猜工具名或参数。"
        )


def _task_agent(tid, params, ctx, base):
    instr = (params.get("instruction") or "").strip()
    if len(instr) < 3:
        _vtask_set(tid, status="error", error="没听清要做什么")
        return
    _vtask_set(tid, instruction=instr, recipe=(params.get("recipe") or ""))   # 起点即落盘(铸造/履历都要)
    _vtask_set(tid, step="交给助手规划…")
    # 语境:worker 不在浏览器里,得把「用户现在读哪本书第几页/选中了什么」显式喂给它
    ctx_lines = []
    try:   # 已存工具清单:CLI 编排时知道有哪些已验证路线可借(审查:复用入口原是断的)
        import task_runtime as TR
        _rl = TR.list_recipes()
        if _rl:
            ctx_lines.append("用户已保存的工具(各自带已验证路线):" + "、".join("《%s》" % r["name"] for r in _rl[:10]) +
                             "。若本次要求与某工具意图相符,可参考其做法;完整运行它则用 assistant_call_tool 调 run_saved_task。")
    except Exception:
        pass
    if ctx.get("file_rel"):
        ctx_lines.append(f"用户当前打开的书:{ctx.get('book_name') or ctx['file_rel']}(file 参数用 `{ctx['file_rel']}`)")
    if ctx.get("page"):
        ctx_lines.append(f"当前页码:{ctx['page']}")
    if (ctx.get("selection") or "").strip():
        ctx_lines.append(f"用户选中的文字:{ctx['selection'][:300]}")
    # #38:把**前后的聊天上下文**告诉专用 ai —— 编排把任务甩过来时往往只有一句话,
    #   之前聊的(出过什么题/纠正过什么/围绕哪些词)不喂就丢了。取侧栏对话最近几轮。
    convo_block = ""
    try:
        import assistant as A
        msgs = A._convo_load(str(ctx.get("_uid") or "")) or []
        rows = []
        for m in msgs[-8:]:
            role = "用户" if m.get("role") == "user" else "助手"
            c = (m.get("content") or "")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            c = str(c).strip().replace("\n", " ")
            if c:
                rows.append(f"{role}:{c[:200]}")
        if rows:
            convo_block = "\n【最近对话(理解用户想要什么的背景)】\n" + "\n".join(rows) + "\n"
    except Exception:
        pass
    prompt = (
        "你是这个自学 App 的后台助手,用 bwapp 这套 MCP 工具帮用户把事情做完。\n"
        + ("\n".join(ctx_lines) + "\n" if ctx_lines else "")
        + convo_block
        + f"\n用户的要求:{instr}\n\n"
        # 能力提示(不教做法,只让它知道有这个能力 —— 用户拍板:CLI 自己决定用哪些工具、怎么编排)。
        #   实测它面对"做填空题"去调了 make_anki_card,是因为不知道能造**交互纸**(让用户在页面上手写作答的那种)。
        "工具里有一组 **page_new / page_add / page_show**:能造一张让用户**在页面上手写作答**的交互纸"
        "(填空/练习/试卷/听写这类,用户要「让我写/在纸上做」时用它,不是 make_anki_card——那是背记卡不是作答纸)。\n\n"
        "要求:**你自己决定**用哪些工具、按什么顺序把这件事做成;做完后**只输出一句话**(40 字以内)"
        "如实说你做了什么(做了什么就说什么,别夸大),不要罗列过程、不要 markdown。做不到就直接说做不到和原因。"
    )
    # 147c:**ENABLE_TOOL_SEARCH=false** —— Claude Code 默认把 MCP 工具做成「延迟加载」
    #   (system.init 里 MCP 直接可见 **0** 个),模型必须先 ToolSearch 才拿得到 schema,白烧 1~2 轮。
    #   我一度以为关不掉(--settings 塞各种候选键全无效),后来在 CLI 二进制里挖出真开关:
    #     OBr(): process.env.ENABLE_TOOL_SEARCH 为假值 → 走 "standard" 模式 = **工具直接给,不用搜**
    #   实测同一任务:默认 4 轮/12.2s(还调了 2 次 ToolSearch) → **2 轮/6.0s,零 ToolSearch**,
    #   且 system.init 里 MCP 从 0 个变成 **20 个直接可见**。
    #   ⇒ 目录预注入(_agent_catalog)因此**不再需要**,留着只是白塞 token。
    sysp = (
        _agent_registry_prompt()
        + "\n做完只输出一句话(40 字内)告诉用户结果,不要罗列过程、不要 markdown。"
        "做不到就直说做不到和原因。"
    )
    # 148:后端/型号/深度来自 assistant.py 的 per-action 预设(设置面板「agent」行);没传就回落 env/默认
    backend = (params.get("backend") or _AGENT_BACKEND).strip().lower()
    model = (params.get("model") or "").strip()
    effort = (params.get("effort") or "").strip()
    # 服务端 action-pref 会先按 Codex 型号能力裁决；worker 再 fail-closed
    # 只消费真正的 bool,不把字符串/数字宽松转换成 Fast。
    fast = params.get("fast") is True
    steps, answer = [], ""
    # 148:双向兜底 —— 主后端挂了(限流/进程起不来/没给结果)就换另一个 CLI 再跑一遍。
    #   用户排序:**成功率 > 速度**。默认主 = claude/opus(Max 5x 额度够用),兜底 = codex(白嫖池,
    #   跟 Claude 额度完全独立 → Claude 真限流时它照样能干活)。
    alt = "codex" if backend == "claude" else "claude"
    try:
        answer = _agent_run_cli(
            backend, prompt, sysp, tid, steps,
            model=model, effort=effort,
            fast=(fast if backend == "codex" else False),
        )
    except _AgentTimeout:
        _vtask_set(tid, status="error", error="任务超时")   # 超时不降级:再来一轮又是 240s
        return
    except Exception as e:
        sys.stderr.write(f"[agent] {backend} 异常({e}),降级 {alt}\n")   # 进程起不来/崩了 → 往下走降级
    if not answer:
        # ★重试闸门看副作用,不只看 answer(审查实锤:第一轮的 client_action 已被前端即时应用、
        #   写操作已落盘——盲目全量重跑=两张一样的纸/双份高亮)。有写痕迹 → 按部分成功收尾,不重跑。
        _WRITE_STEPS = {"page_show", "page_new", "page_add", "page_add_many", "highlight",
                        "add_highlight", "auto_highlight", "make_anki", "make_anki_card",
                        "save_note", "write_note", "start_dictation"}
        _side = [x["name"] for x in steps if x.get("name") in _WRITE_STEPS] or                 (['client_action'] if _CA_SINK.get(tid) else [])
        if _side:
            _flow_txt0, _ = _flow_summary(steps)
            answer = "任务主体已执行(%s),但助手没有给出收尾说明。" % (_flow_txt0 or "、".join(_side[:4]))
        else:
            steps.clear()
            _vtask_set(tid, step="换个助手重试…")
            try:
                # Claude→Codex 兜底仍可继承该动作经服务端校验的 Fast；
                # Codex→Claude 则在调用边界就显式归零。
                answer = _agent_run_cli(
                    alt, prompt, sysp, tid, steps,
                    fast=(fast if alt == "codex" else False),
                )   # 兜底后端用它自己的默认型号/深度
            except _AgentTimeout:
                _vtask_set(tid, status="error", error="任务超时")
                return
            except Exception as e:
                _vtask_set(tid, status="error", error=str(e)[:140])
                return
    if not answer:
        try:
            if params.get("recipe"):
                import task_runtime as TR
                TR.recipe_log_run(params["recipe"], {"ts": int(time.time()), "ok": False})
        except Exception:
            pass
        _vtask_set(tid, status="error", error="助手没给出结果")
        return
    _flow_txt, _lookups = _flow_summary(steps)   # ★用户设计:流程摘要(查找类带实际参数)→ 进 CLI 返回,供显示 + AI 复用查询
    try:   # 创造物库:CLI 任务结果入册(跨轮可 recall"刚才那个任务的结果";「记忆」开关=do_task)
        import assistant as A
        if not A._creation_enabled(str((ctx or {}).get("_uid") or ""), "do_task"):
            raise RuntimeError("off")
        A._creation_add(str((ctx or {}).get("_uid") or ""), "cli_task",
                        "后台任务:" + (_flow_txt or instr[:60]).replace("\n", " ")[:160],
                        content=(answer or "")[:4000],
                        anchor={"file": str((ctx or {}).get("file_rel") or "")})   # 带上书 → 沙盒测试任务不入册(_creation_add 统一拦)
    except Exception:
        pass
    _res = {"answer": answer, "tools": [x["name"] for x in steps]}
    if _flow_txt:
        _res["flow"] = _flow_txt
    if _lookups:
        _res["lookups"] = _lookups
    try:   # 运行履历:工具发起的任务 → 回写配方 runs[-20:](工具库徽标「运行N次·上次成功」)
        if params.get("recipe"):
            import task_runtime as TR
            TR.recipe_log_run(params["recipe"], {"ts": int(time.time()), "ok": True})
    except Exception:
        pass
    _vtask_set(tid, status="done", speak=answer[:200], steps=list(steps), instruction=instr,   # instruction:保存工具判型用(生成型→存意图而非字面轨迹)
               client_actions=_CA_SINK.pop(tid, []),   # ★ 后台 agent 内部工具产生的 client_action(如建纸)→ 前端应用
               result=_res)


def _run_task(tid, kind, params, context, base):
    # This function always runs in a detached thread.  Restore the exact
    # request owner captured by assistant._bg_task before touching reader
    # sidecars (card entities, highlights or sticky notes).
    _pdf_mod()._reader_storage_identity_bind_for_thread(
        (context or {}).get("_reader_storage_identity")
    )
    _vtask_set(tid, step="排队中…")
    with _task_sema:   # 阻塞排队:同时最多 2 个综合任务跑(daemon 线程阻塞无碍)
        try:
            {"note": _task_note, "anki": _task_anki, "vocab": _task_vocab, "search": _task_search,
             "agent": _task_agent}[kind](tid, params, context, base)   # 147:通用 agent worker
        except Exception as e:
            _vtask_set(tid, status="error", error=str(e)[:160])


def _task_dir(kind, params, speak):
    return {"speak": speak, "task": {"kind": kind, "params": params},
            "client_actions": [], "server_results": [], "confirm": None}


def _composite_intent(t, context):
    """识别综合任务(笔记整理/制卡/单词闭环/查找跳转)→ 返回 task 指令。仅 PDF 页。"""
    ctx = context or {}
    if ctx.get("page_type") != "pdf":
        return None
    s = t.replace(" ", "")
    sel = (ctx.get("selection") or "").strip()
    # 笔记整理
    if _re.search(r"(整理成笔记|做成笔记|记成笔记|整理笔记|存成笔记|总结成笔记|整理一下笔记|做个笔记|记个笔记|存成一?篇?笔记)", s):
        return _task_dir("note", {"scope": "sel" if sel else "page"}, "好,我来整理成笔记,稍等")
    # Anki 制卡
    if _re.search(r"(做成卡片|制成卡片|做张卡|做几张卡|做成anki|加到anki|做成闪卡|背一下这个|做成记忆卡|制卡)", s):
        return _task_dir("anki", {"scope": "sel" if sel else "page"}, "好,我来做成卡片,稍等")
    # 单词闭环(查词→生词本→卡)
    if _re.search(r"(加(到)?生词本|收藏(这个)?(词|单词)|学(一下)?这个(词|单词)|记(到)?生词本|这个词学一下)", s):
        if not sel:
            return {"speak": "先选中那个单词再说哦", "client_actions": [], "server_results": [], "confirm": None}
        return _task_dir("vocab", {"word": sel}, f"好,把 {sel} 加到生词本并制卡")
    # 查找并跳转(用原文 t 保留 query 字符)
    mq = _re.search(r"(?:搜索|搜一?下|查找|全文搜索|找讲|找关于|找一下|哪一?页讲|哪里讲|哪一?页有|找有讲)\s*(.+)", t)
    if mq:
        qq = mq.group(1).strip()
        qq = _re.sub(r"^(讲|关于|的|有|是)+", "", qq)
        qq = _re.sub(r"(的那?一?页|的内容|这本书|在哪里?|的地方|讲(的)?什么)[\s。，,?？]*$", "", qq).strip(" 。，,?？的")
        if len(qq) >= 2:
            return _task_dir("search", {"query": qq}, f"好,找讲「{qq}」的地方")
    return None


@bp.route("/task", methods=["POST"])
def voice_task():
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    if kind not in _VTASK_KINDS:
        return jsonify({"ok": False, "error": "unknown kind"}), 400
    base = request.host_url.rstrip("/")   # 用户当前访问域(拼卡背深链,避开默认死链)
    tid = _vtask_new(kind)
    threading.Thread(target=_run_task, args=(tid, kind, body.get("params") or {}, body.get("context") or {}, base), daemon=True).start()
    return jsonify({"ok": True, "task_id": tid})


@bp.route("/task-status")
def voice_task_status():
    if not _logged_in():
        return jsonify({"ok": False, "error": "auth"}), 401
    t = _vtask_get(request.args.get("id", ""))
    if not t:
        return jsonify({"ok": False, "error": "unknown task"}), 404
    return jsonify({"ok": True, "status": t.get("status"), "step": t.get("step"),
                    "steps": t.get("steps") or [],   # 工具指示器 v2:内部步骤流水(长条滚动 + 「!」面板逐步查看)
                    "partial": t.get("partial") or "",   # CLI 正文增量 → 卡片 body 边跑边显示
                    "speak": t.get("speak"), "client_actions": t.get("client_actions") or [],
                    "result": t.get("result"), "error": t.get("error")})


def register_voice(app):
    app.register_blueprint(bp)

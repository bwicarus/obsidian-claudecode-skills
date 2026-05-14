"""一次性扫描：找 anki/records 里含有裸 LaTeX 命令但没用 MathJax 分隔符包裹的卡片字段。"""
import json, re, glob, os

# 常见 LaTeX 命令（足够覆盖数学卡片）
LATEX = re.compile(
    r'\\(?:frac|sum|int|sqrt|lim|prod|partial|nabla|infty|alpha|beta|gamma|theta|sigma|omega|lambda|delta|epsilon|'
    r'mathbb|mathcal|mathrm|operatorname|cdot|cdots|ldots|leq|geq|neq|approx|times|in|subset|forall|exists|to|'
    r'rightarrow|leftarrow|cos|sin|tan|log|ln|exp|left|right|begin|end)\b'
)

# 各种 MathJax / Anki LaTeX 分隔符
WRAP_PATTERNS = [
    re.compile(r'\\\(.*?\\\)', re.DOTALL),
    re.compile(r'\\\[.*?\\\]', re.DOTALL),
    re.compile(r'\$\$.*?\$\$', re.DOTALL),
    re.compile(r'\$[^$\n]+\$'),
    re.compile(r'\[\$\].*?\[/\$\]', re.DOTALL),
    re.compile(r'\[\$\$\].*?\[/\$\$\]', re.DOTALL),
]

def strip_wrapped(text: str) -> str:
    for p in WRAP_PATTERNS:
        text = p.sub('', text)
    return text

PROJECT_DIR = os.environ.get("CLAUDE_PROJECT") or str(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = []
for p in glob.glob(os.path.join(PROJECT_DIR, 'anki', 'records', '*.json')):
    try:
        rec = json.loads(open(p, encoding='utf-8').read())
    except Exception:
        continue
    for i, c in enumerate(rec.get('cards', [])):
        for fld in ('front', 'back', 'text'):
            v = c.get(fld) or ''
            if not LATEX.search(v):
                continue
            stripped = strip_wrapped(v)
            if LATEX.search(stripped):
                bad.append((os.path.basename(p), i, c.get('anki_note_id'), fld, v))

print(f'cards with bare LaTeX commands: {len(bad)}')
print()
for fname, idx, nid, fld, v in bad:
    print(f'[{fname}] card[{idx}] note={nid} {fld}:')
    print(f'  {v}')
    print()

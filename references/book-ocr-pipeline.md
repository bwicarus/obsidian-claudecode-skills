# 日文扫描 PDF OCR 流水线

## 概述

`资源/books/` 下的日文教材多是**扫描版 PDF**（无文字层，只有页面图像）。后续所有 AI 任务——PDF 阅读器的翻译/解释/问 AI、单词查词、知识点关联、Anki 制卡——都要**读到准确的文本内容**才能工作。扫描图像本身没有可选可搜索的文字，因此先要给每本书跑一遍 OCR，把识别结果回嵌成 PDF 的**不可见文字层**（invisible text layer）。

流水线分两个阶段，**解耦**成独立脚本：

1. **OCR 阶段**：逐页识别 → 写 sidecar JSON（不动 PDF）。耗时长，支持断点续传。
2. **嵌字阶段**：读 sidecar → 把文字按坐标嵌进 PDF 当透明文字层 → 存 `<书名>-ocr.pdf`。

这样设计的好处：OCR 跑完一次，嵌字逻辑可以反复重跑调试（PyMuPDF 定位算法迭代过多轮），不浪费几十分钟的 OCR 时间。

两条 OCR 路径任选其一：

| 路径 | 脚本 | 引擎 | sidecar 目录 | 嵌字脚本 |
|---|---|---|---|---|
| 本地免费 | `mokuro_ocr_book.py` | mokuro + manga-ocr（CPU） | `state/mokuro-ocr/<sha>/` | `embed_ocr_to_pdf.py` |
| 云端高质 | `google_vision_ocr.py` | Google Cloud Vision `DOCUMENT_TEXT_DETECTION` | `state/google-vision-ocr/<sha>/` | `embed_google_ocr_to_pdf.py` |

---

## 两条路径对比

| 维度 | mokuro（本地） | Google Vision（云端） |
|---|---|---|
| 费用 | 免费（纯 CPU 本地跑） | 烧 GCP 赠金；每月前 1000 units 免费（够一本 679 页书）|
| 速度 | 60–100 s/页（CPU，force_cpu=True）| ~1.5–3 s/页（API IO-bound），并发后整本几分钟 |
| 加速手段 | 无（单页串行） | `--workers` 默认 10 并发，10–20 倍提速 |
| char 级 bbox | 无，输出 line 级 bbox（`blocks → lines + lines_coords`），嵌字时要靠**列投影 segmentation + 权重推断**逐字定位 | **有**，API 直接给每个 symbol 的 boundingBox 顶点 → 嵌字时完美对齐，不需要任何推断 |
| 准确率 | 一般（漫画 OCR 模型） | 印刷日文 95%+，实测 ≥ mokuro |
| 适用 | 离线、不想花钱、可接受慢 | 要高质量、有赠金、要快 |

实践结论：印刷体日文教材优先 Google Vision（更准更快、bbox 精确，嵌字层选中体验好）；离线/省钱场景用 mokuro。两者 sidecar 格式**不同**，对应各自的嵌字脚本，不可混用。

---

## sidecar 断点续传机制

### state 目录结构

两条路径都按 PDF 路径的 sha 建独立工作目录：

```
state/mokuro-ocr/<sha>/          ← mokuro
  source.txt                     ← 记录源 PDF 绝对路径（仅 mokuro 写）
  progress.json                  ← 持续刷新的进度（completed/total/percent/ETA）
  p0000.json  p0001.json  ...    ← 每页一份 sidecar JSON

state/google-vision-ocr/<sha>/   ← Vision
  progress.json
  p0000.json  ...
```

### sha 命名

两脚本共用同一算法（`pdf_sha`）：

```python
hashlib.sha1(str(pdf_path.resolve()).encode()).hexdigest()[:16]
```

即 PDF **绝对路径**的 SHA-1 取前 16 位 hex。同一本书路径不变 → sha 不变 → 永远落到同一工作目录，跨重启复用。例：`応用情報技術者.pdf` 对应 `4eda8f8cdc69834f`。
注意：sha 来自**路径**不是文件内容，移动/改名 PDF 会换 sha（旧 sidecar 失联）。

### 断点续传逻辑

- 每页 OCR 完**立即**写 `p<num>.json`（页码 4 位补零，如 `p0042.json`）。
- 启动时扫描已存在的 `p*.json`，归入 `done` 集合，**跳过**已完成页。
  - mokuro：`for f in work.glob("p*.json")` 解析 `f.stem[1:]` 收页号。
  - Vision：`todo = [i for i in page_list if not (work / f"p{i:04d}.json").exists()]`。
- mokuro 全部完成（`len(done) >= n`）直接 return 0；Vision `todo` 为空直接 return 0。
- 配合 systemd `Restart=on-failure` → 重启/断电/崩溃后再起来接着跑，不重复。

### 每页 sidecar 内容

**mokuro** `p<num>.json`（manga-ocr 原始输出 + 注入字段）：
- `blocks[].lines`（每行识别文本）+ `blocks[].lines_coords`（每行 4 顶点 line-bbox，image 坐标系）
- 注入 `_page`（页 index）、`_ocr_seconds`（本页耗时）
- 失败页存 `{"error": "...", "_page": i, "_ocr_seconds": ...}`
- mokuro 输出含 numpy 标量/ndarray，用自定义 `_NumpyEncoder`（`np.ndarray→tolist()`、`np.generic→item()`）才能 json 序列化

**Vision** `p<num>.json`：
```json
{
  "chars": [{"c": "プ", "bbox": [x0, y0, x1, y1]}, ...],
  "text": "整页纯文本",
  "img_width": int, "img_height": int,
  "_page": int, "_seconds": float
}
```
- `chars` 来自遍历 `fullTextAnnotation.pages → blocks → paragraphs → words → symbols`，每 symbol 取 `boundingBox.vertices` 的 min/max 算 bbox
- 根据 `detectedBreak` 补虚拟字符：`SPACE/SURE_SPACE/EOL_SURE_SPACE` 插 `{"c":" ","sp":1}`，`LINE_BREAK` 插 `{"c":"\n","sp":1}`（`sp:1` 标记是分隔符，嵌字时跳过不渲染）
- 失败页存 `{"error": "...", "_page": i, "img_width":0, "img_height":0}`

### progress.json

每页跑完刷新一次，watchdog 和人工都靠它看进度：

- mokuro：`completed/total/percent/last_page_seconds/avg_seconds_per_page/eta_hours/updated_at`，ETA 只按**本次运行**新完成页平均（`completed_run / elapsed_run`）算，不被历史拖偏
- Vision：`completed/total/last_page/last_seconds/avg_seconds/eta_minutes/updated_at`（注意 `total` 是本次 `todo` 数，不是全书页数）

---

## 嵌字层原理（invisible text）

两个 embed 脚本都把识别出的字符用 `page.insert_text` 逐字写到 PDF，统一参数让文字**不可见但可选可搜索**：

```python
page.insert_text(
    fitz.Point(x_pdf, baseline_pdf), c,
    fontname="japan",        # PyMuPDF 内置 CJK 字体
    fontsize=fs,
    color=(0,0,0), fill=(0,0,0),
    render_mode=0,           # 注意:是 0 + 双 opacity=0,不是 render_mode=3
    fill_opacity=0, stroke_opacity=0,
)
```

> 用 `render_mode=0` + `fill_opacity=0` + `stroke_opacity=0` 而非 `render_mode=3`（PDF 标准的 invisible），是因为前者兼容性更好——iOS Files / Safari 等也能正确识别为可选文字。

坐标缩放：sidecar 是 **image 像素坐标系**（按 300 dpi render 出来的图），PDF 是 **point 坐标系**。每页算缩放因子：
```python
sx = page.rect.width  / img_w
sy = page.rect.height / img_h
```
所有 image 坐标乘 `sx/sy` 得 PDF 坐标。两脚本嵌完都 `src.save(out, garbage=4, deflate=True)` 输出 `<stem>-ocr.pdf`。

### Vision 嵌字（简单，char-level 直接对齐）

`embed_google_ocr_to_pdf.py` 每字一个 `insert_text`，因为有精确 bbox：
- 跳过 `sp` 分隔符和空 `c`
- 字号：`fs = clamp(char_w_img * sx / 0.78, 4, 80)`
  - **关键常数 `/0.78`**：PyMuPDF `japan` 字体的 char advance/bbox 宽 ≈ `fs × 0.78`。要让嵌入字符的 bbox 宽度刚好等于 visual char 宽度（`char_w_img * sx`），就反解 `fs = char_w / 0.78`。这样选中高亮跟图上的字大小一致。
- baseline：`y1 * sy - char_h_img * sy * 0.10`（bbox 底部上抬 10% 留 descender）
- x 起点：`x0 * sx`

### mokuro 嵌字（复杂，要从 line bbox 推断每字位置）

`embed_ocr_to_pdf.py` 只有 line 级 bbox，要把一行文本摊到行内每个字。`embed_page` 两条路径：

**主路径 — seg-based 逐字定位**（需要原始扫描图 `image`，效果最准）：
1. 截出该 line 的图像 patch，转灰度
2. `_segment_visual_chars`：垂直**列投影**（`patch<100` 二值化按列求和）找出有墨迹的列段 → 合并字内笔画散开的窄 gap（< 8% line_h）→ 估 median char 宽 → 把过宽段（> 1.4×median，多字粘连）等分成 N 个子段，得到逐 visual char 的 `(x0,x1)`
3. `_seg_is_bullet` 判首段是否为 bullet（实心圆/方块：blob 高 < 70% line_h、宽高比 0.6–1.7）。若首段是 bullet 而 OCR 文本首字不是强标点/括号（`_is_cjk_punct_or_bullet`），说明 mokuro 漏识了 bullet → 跳过首段对齐
4. 把 NFKC 规范化后的非空格字符按顺序映射到各 seg；尾部超出的字用 avg seg 宽 fallback
5. 字号 `fs = median_seg_w * sx / 0.78`（同 Vision 的 0.78 常数），baseline `(y2_img - line_h*0.10)*sy`，逐字 insert 后 `continue` 跳过 fallback

**fallback 路径 — weight-based 均分**（无 image 或 segmentation 失败时）：
- NFKC 规范化（mokuro 输出全角数字/标点如 `１１．３` → 还原半角）
- ASCII↔CJK 边界手动插空格（`11.3セキュリティ` → `11.3 セキュリティ`），空格占位不渲染，防拖选 CJK 时误带上尾部 ASCII
- 字符宽度权重：CJK 假名/汉字 = 1.0，ASCII/标点/空格/`ord<=0xFF` = 0.5（图里半宽印刷）
- 按权重累加在 line 宽度内分配 x，字号 `fs = min(line_h*0.95, char_w_cjk)` 同时受行高和单字间距约束（防大字号标题的 ASCII bbox 越界到下一字），并右偏 `char_w_cjk * 0.10` 修 mokuro line bbox 左偏

> 统一 `fontname='japan'`：不要切 `'helv'`，那会触发 PyMuPDF reflow 往整页 text 插空格污染选中。

两脚本嵌字时都会扫 sidecar 报 `已完成 X/N`，缺页打印警告；`error` 页和缺 `img_width/height` 的页直接跳过。

---

## systemd 自动化

unit 文件副本在 `references/systemd/`（部署到 `/etc/systemd/system/`）。

### `book-ocr.service` — OCR 主任务（低优先级）

- `Type=simple`，`User=bwicarus`，`WorkingDirectory=/home/bwicarus/claude`
- env：`CLAUDE_PROJECT` / `OBSIDIAN_VAULT` / `HOME`
- **低优先级三件套**（让 webapp / AI 调用 / SSH 正常用，OCR 跑闲时）：
  - `Nice=19`（最低 CPU 优先级）
  - `CPUWeight=20`
  - `IOWeight=10`
- `ExecStart`：`/home/bwicarus/manga-ocr-venv/bin/python .../mokuro_ocr_book.py --pdf "/home/bwicarus/obsidian/资源/books/応用情報技術者.pdf"`（OCR 引擎装在独立 venv `manga-ocr-venv`）
- `Restart=on-failure` + `RestartSec=60` → 配合 sidecar 断点续传，崩了 60s 后接着跑
- **防 fail-loop**：`StartLimitIntervalSec=300` + `StartLimitBurst=3`（5 分钟内 fail 3 次就停，避免代码 bug 时空烧资源）
- `RemainAfterExit=no`：OCR 跑完正常 exit 0，systemd 不视为需保持运行
- 日志 append 到 `state/logs/book-ocr.log`（stdout + stderr 同文件）

> service 里硬编码了具体书名 / mokuro venv，换书 / 切 Vision 路径需改 `ExecStart`。

### `book-ocr-watchdog.service` + `.timer` — 健康度自检

- watchdog 是 `Type=oneshot`，`User=root`，跑 `book_ocr_watchdog.py`
- timer：`OnBootSec=5min` + `OnUnitActiveSec=15min` + `Persistent=true` → 开机 5 分钟后首跑，之后每 **15 分钟**一次
- `book_ocr_watchdog.py` 检查（针对固定 sidecar `state/mokuro-ocr/4eda8f8cdc69834f`）：
  - service active 但 `progress.json` > 30 分钟没更新 → 卡死
  - service active 但 progress.json 不存在
  - service inactive 但 completed < total → 异常退出
  - journal 最近 30 分钟 `Failed with result` ≥ 3 次 → fail-loop 苗头
  - 对比上次记录，sidecar 数 ~14 分钟内没增长 → I/O 异常
- 输出：正常写 `/tmp/book-ocr-health.json` 并删 alert；异常写 `/tmp/book-ocr-alert.json` + log；**严重（fail ≥ 5）主动 `systemctl stop book-ocr`** 防烧资源
- 日志只 append 到 `state/logs/book-ocr-watchdog.log`，历史快照在 `/tmp/book-ocr-watchdog-history.json`

### book_ocr_watchdog.py 名义 vs 实际

文件名虽叫 "watchdog"，它**不是**“监听新书自动触发 OCR”的那种 watcher，而是**单本固定书 OCR 任务的健康度巡检**（sidecar sha 写死）。换书时需改脚本里的 `SIDECAR_DIR`。

---

## CLI 用法

mokuro（在专用 venv 跑，CPU 慢）：
```bash
/home/bwicarus/manga-ocr-venv/bin/python scripts/mokuro_ocr_book.py \
  --pdf "/home/bwicarus/obsidian/资源/books/応用情報技術者.pdf" [--dpi 300]
```

Google Vision（需 API key）：
```bash
python scripts/google_vision_ocr.py \
  --pdf "<PDF>" [--dpi 300] [--workers 10] \
  [--pages 10] [--pages 10,80,200] [--pages 10-15]   # 不指定 = 全本
```
key 来源：env `GOOGLE_VISION_API_KEY`，否则读 `/home/bwicarus/.config/gcp-vision-key`。

嵌字（OCR 跑完后）：
```bash
# mokuro sidecar → 文字层
python scripts/embed_ocr_to_pdf.py --pdf "<原PDF>" [--out <输出>] [--sidecar <目录>]
# Vision sidecar → 文字层
python scripts/embed_google_ocr_to_pdf.py --pdf "<原PDF>" [--out <输出>] [--sidecar <目录>]
```
默认 sidecar 走对应 state 目录（按 sha 解析），默认输出 `<原PDF>-ocr.pdf`。

---

## 踩坑

1. **render_mode=3 兼容性差** → 改用 `render_mode=0 + fill_opacity=0 + stroke_opacity=0` 实现不可见文字，iOS Files/Safari 才认。
2. **`/0.78` 字号常数**：PyMuPDF `japan` 字体 char bbox 宽 ≈ `fs × 0.78`，要让嵌入字 bbox 跟 visual char 一样宽得反解 `fs = char_w / 0.78`，否则选中高亮框大小对不上。
3. **不要用 `insert_textbox` 嵌 CJK**：line 窄时 PyMuPDF 会缩字号把一行塞成左上一小撮 → 必须逐字 `insert_text`。
4. **字体只用 `'japan'`，别切 `'helv'`**：切字体会触发 reflow 往整页 text 插空格，污染选中和搜索。
5. **mokuro numpy 序列化**：输出含 `np.int32/float32/bool_/ndarray`，直接 `json.dumps` 报错 → 用 `_NumpyEncoder`（`ndarray→tolist`、`generic→item`）。
6. **mokuro 全角/半角不一致**：输出 `１１．３` 这种全角，要 `unicodedata.normalize("NFKC", text)` 还原成图里印刷的半角，否则嵌字跟 visual 错位。
7. **mokuro line bbox 左偏**：detector 输出比 visual 文本起点偏左 2–4 px，fallback 路径右偏 `char_w_cjk * 0.10` 修正。
8. **mokuro 漏识 bullet**：`●◆■` 等行首项目符号常漏识，靠 `_seg_is_bullet`（blob 高 < 70% line_h、近正方形）几何判定跳过首段对齐，避免整行右移。
9. **fitz 非线程安全**：Vision 并发不能共享一个 `Doc`，每 worker 各自 `fitz.open` 同一 PDF（独立 file handle 安全）render + API call。
10. **sha 基于路径非内容**：移动/改名 PDF 会换 sha 导致旧 sidecar 失联、OCR 从头跑。
11. **mokuro 跑完删 PNG**：每页 render 出的 `p<num>.png` OCR 完即 `unlink` 省空间，只留 JSON sidecar。
12. **watchdog sidecar sha 写死**：`SIDECAR_DIR` 硬编码当前书的 sha，换书要改脚本。
13. **service 硬编码书名**：`book-ocr.service` 的 `ExecStart` 写死具体 PDF + mokuro venv，换书/切 Vision 要改 unit 重新 `daemon-reload`。
14. **巨幅扫描页把 Vision 撑爆 + OOM（2026-06，料理师1.pdf 实战）**：有的扫描书单页原生尺寸极大（料理师1：`2227×3242 pt`），固定 `--dpi 300` 渲染 → `9280×13509 px / 单页 PNG 49MB`，base64 后请求体 ~66MB **远超 Vision API 上限**（`SSLError: Max retries exceeded` 上传超时反复重试），且 10 个 worker 同时各持一张 49MB pixmap 直接把内存吃光 → **编排进程被 OOM 杀**，状态停在 `ocr` 无终态 → 前端进度条永久卡住。修复：`google_vision_ocr.py` 渲染改 **按 DPI 但长边封顶 `--max-long-side`（默 4000）等比缩小** + **上传改 JPEG（`--jpeg-quality` 默 90）**——49MB→1.2MB，OCR 质量无可感知损失；正常尺寸书（A4 842pt → 3508px < 4000）不受影响仍 300dpi。
15. **断点续传把"报错页"误当完成**：旧逻辑 `todo = 没有 p*.json 的页`，但报错页也写了带 `error` 的 `p*.json` → 重跑永久跳过它，错误页一直缺 OCR。改 `_page_done()` 只把**成功（无 `error` 键）的页**算完成，错误页重跑。配合 `ocr_one_page` 对 SSL/连接/超时**重试 3 次**（退避 1.5/3s），瞬时网络抖动当场自愈。
16. **全页失败别动原书**：OCR 子进程整本都 SSLError 时仍 `return 0`（不抛异常）。`preprocess_book.py` 在 OCR 后统计 sidecar，**0 页有文字 → 报 `error`，不嵌入空文字层、不替换原书**，避免把好书换成"有壳无字"的 PDF。

---

## 相关 reference

- [`google-cloud-apis.md`](google-cloud-apis.md) — Vision API key 隔离、赠金/配额管理、`google_api_quota.py` 本地计数器（`ocr_one_page` 每页调 `log_usage("vision", 1, ...)` 记账）
- [`pdf-reader.md`](pdf-reader.md) — 网页 PDF 阅读器：char-layer 选中机制（消费这里嵌出来的文字层）、AI 翻译/解释/问 AI、高亮编辑系统

## 网页按需触发（2026-06，接到文件列表 UI）

OCR 流水线原来只能改 systemd unit 硬编码 PDF 路径跑。现在在 PDF 阅读器**文件列表页**（`/pdf/`）每本书加了 🔧 **预处理** 按钮 + 长按删除，把现有脚本编排起来（**不重写 OCR**）：

- **编排器 `scripts/preprocess_book.py --pdf <绝对路径>`**（纯粘合）：① `has_text_layer`（抽样 8 页累计可提取文字 ≥20 字符）检测；有 → 写 `done has_text` 直接跳过。② 无 → `subprocess` 跑 `google_vision_ocr.py`（现成，断点续传）边跑边把它的 `progress.json` 同步进状态。③ `embed_google_ocr_to_pdf.py`（现成）嵌入不可见文字层到库外临时文件。④ **原地替换**原 PDF（先备份到 `state/book-preprocess/<sha>.orig.pdf`，**不放 vault 以免污染书列表**）。状态全程写 `state/book-preprocess/<sha>.json`（`<sha>`=OCR 流水线同款 `sha1(resolve路径)[:16]`）。
- **路由（`pdf_reader.py`）**：`POST /api/preprocess-async`（用 `APP_PYTHON` + `start_new_session=True` **detached** 起编排器 → 关网页/webapp 重启都不中断；重复启动守卫**按进程存活判定**：状态里 `pid` 进程还活着才拦，死进程留的陈旧 in-progress 状态允许直接重跑）；`GET /api/preprocess-status?file=`（读状态文件，文件驱动 → 重启不丢进度；**存活检测**：进行中相位但 `pid` 进程已退出 [`os.kill(pid,0)` 不存在] 且 >30s 未更新 → 返回 `error` 让进度条停在 ✗ 而非永久卡住）；`POST /api/delete-pdf`（删 PDF + 清 OCR/预处理/备份 sidecar，`_safe_vault_path` 挡路径穿越）。编排器启动时把自己 `pid` 写进状态文件供前两者用。
- **前端（`pdf_index.html`）**：每本书 🔧 预处理 → 进度条轮询 `preprocess-status`（轮询断了后台不停，重点按钮 `already:true` 续看）；长按 ~550ms / 右键 → 确认删除。
- **「不中断」三层**：编排器 detached（关网页不停）+ 状态写文件（webapp 重启进度不丢）+ OCR sidecar 断点续传（进程被杀重跑自动续）。
- ⚠ 默认走 **Google Vision**（烧 GCP 赠金，~2s/页，配额计数见 `google_vision_ocr.ocr_one_page` → `log_usage`）；已有文字层的书秒判跳过、零成本。巨幅扫描页的尺寸/JPEG/重试坑见上面踩坑 14–16。

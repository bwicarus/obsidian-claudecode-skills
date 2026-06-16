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
18. **统一页宽:必须"先 OCR 锐利原图 → 再归一 → 把 OCR 嵌到归一页",且归一要用栅格化（2026-06，料理师1.pdf）**：扫描书各页原生宽参差(1568~2394pt)→ 阅读器逐页按原生宽渲染、宽窄不一。前端"每页独立 scale"会打乱浮层(选中/高亮/句子框/振假名)的绝对定位锚点(单页模式容器 `#page-container` 无定位、浮层锚到 `#main`、canvas `margin:0 auto` 居中,只在画布宽==容器宽时贴合)→ 全部错位,已回退。改在**数据层**把 PDF 归一等宽(`scripts/preprocess_book.py::normalize_page_widths`),前端单一全局 scale 即天然等宽,完全不碰浮层。三个坑:① **顺序**:先归一再 OCR 会糊——重排多一次重采样,Vision 命中率暴跌(实测同页 126→34 字);必须先在锐利原图 OCR、再归一、最后嵌。② **不能 normalize 已嵌文字的页**:旋转页 `show_pdf_page` 嵌文字 save 后整体错位(同坑 17 的旋转坐标问题)。③ **归一用栅格化(`get_pixmap`)而非 `show_pdf_page`**:`get_pixmap` 自动应用 /Rotate → 正立图 + 烘焙 `rotation=0`(于是 embed 无需 derotation、坐标平凡);`show_pdf_page` 的 `rotate`/`keep_proportion` 各种组合都解决不了旋转页的 letterbox/save 后坐标错位。栅格化嵌 JPEG(q82,2× 分辨率保缩放清晰),18 页扫描书 26→34MB 可接受(扫描书本就是图,重采样无损阅读)。`_needs_width_norm` 判宽参差才做;born-digital(真文字层无 `.orig` 备份)不动;`--no-uniform` 关。
17. **旋转页(/Rotate 90)嵌字层丢字 —— 选不中的真凶（2026-06，料理师1.pdf 实战）**：扫描书常见整本 `/Rotate 90`（横扫描、显示时转正）。`page.get_pixmap()` 渲染**已应用旋转**→ OCR bbox 在「视觉(竖)」坐标系；但 `page.insert_text` 用 **mediabox(未旋转、横)坐标系**。直接喂视觉坐标 → 视觉 y 超出 mediabox 高度(横书 mediabox 高 = 视觉宽)的字**落到页框外被 PyMuPDF 静默裁掉**。实测某 90° 页 sidecar 107 字、嵌入读回只剩 35，**页面中下部整片丢失**（"上半能选、下半选不中"，且 OCR 其实全识别了，极易误判成"OCR 漏识"去换引擎）。修复：`embed_page` 用 `page.derotation_matrix` 把视觉点转回 mediabox 点、`rotate=page.rotation` 让字形朝向对齐（读回 bbox 才对得上选中层）。`rotation=0` 时 derotation 是单位阵 + `rotate=0` → 对所有非旋转书完全无变化。两个 embed 脚本（google char-level / mokuro seg+weight 两路径）都改。诊断手法：把嵌入文字临时设可见红色渲染 PNG，看红字是否压在印刷字上，一眼定位。
20. **嵌字 `fontname="japan"` → 简体专用字被丢字（2026-06-16，费曼中文译本,坑19 的姊妹坑）**：`embed_google_ocr_to_pdf.py::embed_page` 嵌不可见文字层时 `fontname` 写死 `"japan"`(也是"为日语书做")。日语内置字体对**简体专用字形**(费/查/纽/约/获/战/间…简化字)无 glyph → `insert_text` **静默丢字** → 选字层缺字、阅读器"浮层近一半盖不全"。实测费曼 p6:OCR sidecar 991 字,japan 嵌入读回只剩 809(丢 18%);症状跟坑19(OCR 乱码)叠加,极易混淆——**坑19 是 OCR 文字本身错/乱,坑20 是 OCR 对但嵌入时字形丢**(get_text 出来的是对的字但缺了一部分=坑20)。修复:`fontname="china-s"`——实测 insert_text+读回,china-s 是 **pan-CJK 超集**:简体/繁体/假名(あいうカタ)/和制汉字(働峠込)/共用全覆盖一个不丢,japan 只丢简体。故 china-s 中日繁通吃、日语书也不退化(⚠ `Font.has_glyph()` 对内置 CJK 字体**不可靠**:它说 japan 有这些字但 insert_text 实际丢——必须用 insert_text+get_text 实测判断,别信 has_glyph)。**已嵌错字体的书免重 OCR 修法**:逐页 `add_redact_annot(page.rect)`+`apply_redactions(images=PDF_REDACT_IMAGE_NONE, graphics=PDF_REDACT_LINE_ART_NONE, text=PDF_REDACT_TEXT_REMOVE)` 去旧文字层(保扫描图)→ 复用现成 sidecar 用 china-s 重嵌 → save garbage=1 → 替换;几何不变、不重 OCR、不重栅格化。三连根因都是"为日语书做"的硬编码:OCR `languageHints=ja`(坑19,乱码)+ 嵌字 `font=japan`(本坑,丢简体)+ `save garbage=4` 大书病态慢。
19. **Vision `languageHints` 写死 `["ja"]` → 非日语书识成乱码（2026-06-16，费曼讲义中文译本实战）**：`google_vision_ocr.py::ocr_one_page` 原把 `imageContext.languageHints` **硬编码 `["ja"]`**（流水线本是给日语书做的）。**简体中文**扫描书被当日语识别 → 整页繁体杂字+漏字+整段乱码，字符层忠实照画 → 阅读器"选字浮层近一半盖不全"（症状极像 OCR 漏识或缓存陈旧，实为语言提示错）。铁证(费曼 p6 同图)：`ja`→768字乱码 `美于費恩曼 ±¥\.*=#F★£…`；`zh`/`zh-Hans`/**自动检测**→991字完美 `关于费恩曼 理查德·费恩曼(R.P.Feynman)1918年生于纽约市…`。日语书回归(料理师 p20)：`ja` 421 ≈ 自动 420，一致不退化。修复：`ocr_one_page(…, lang_hints=None)` + `--lang-hints`(逗号分隔，留空=自动)，**默认自动检测**（中/日/英都对，杜绝单一硬编码语言毁掉别的语种）。重 OCR 一本书：`preprocess_book.py --pdf <书> --engine vision`（检测到 `.orig.pdf`→从干净扫描重做+`rmtree` 清旧 sidecar+自动检测重 OCR+重嵌）；嵌完书 mtime 变→阅读器 `cv`(含 mtime)变→重开即拿完整正确字符层。**判型手法**：选不中的页先 `get_text` 看文字层是不是乱码/繁体杂字——是→语言提示问题(改这条)，不是→才查旋转(坑17)/分辨率(坑14)。

---

## 相关 reference

- [`google-cloud-apis.md`](google-cloud-apis.md) — Vision API key 隔离、赠金/配额管理、`google_api_quota.py` 本地计数器（`ocr_one_page` 每页调 `log_usage("vision", 1, ...)` 记账）
- [`pdf-reader.md`](pdf-reader.md) — 网页 PDF 阅读器：char-layer 选中机制（消费这里嵌出来的文字层）、AI 翻译/解释/问 AI、高亮编辑系统

## 网页按需触发（2026-06，接到文件列表 UI）

OCR 流水线原来只能改 systemd unit 硬编码 PDF 路径跑。现在在 PDF 阅读器**文件列表页**（`/pdf/`）每本书加了 **📄 文档 / 🎴 漫画** 两个引擎按钮 + 长按删除，把现有脚本编排起来（**不重写 OCR**）：

- **编排器 `scripts/preprocess_book.py --pdf <绝对路径> [--engine vision|manga] [--enhance] [--no-uniform]`**（纯粘合）：
  - **① 检查流程**（2026-06，根治"合并书被整体跳过/宽度没修"）：先 `_needs_width_norm`（各页视觉宽差 > 2% 即判不一致）查页宽；再 `_pages_missing_text`（**逐页**而非抽样，每页可提取文字 < 4 字符算缺）查文字层覆盖。**早退**只在：vision 引擎 + 没要 `--enhance` + 页宽已一致 + **所有页**都有文字层 + 非"曾处理过的扫描书" → 写 `done` 直接跳过、零成本。
    - ⚠ 旧逻辑用抽样 `has_text_layer`（仍保留但不再决策）：合并书"前段有文字、后段没有"时，抽样只看到前几页有文字就误判整书 OK → 被整体跳过；且早退在 `--enhance`/宽度检查之前，点「增加清晰度」也被拦。新逻辑逐页判 + 把 enhance/宽度纳入早退条件，根治。
  - **② 重建（栅格化）触发条件**：`--enhance` / 页宽不一致(归一) / **部分**页缺文字层（`0 < 缺 < 总` → 重建后全页统一 OCR，避免给已有文字的页叠重复层）。整书都没文字层（缺==全部）且没要增强/归一时**不重建**，直接在锐利原图 OCR（质量更好，embed 也不重复）。重建毁掉所有现存文字层 → 之后对重建图**全页** OCR = "所有页都 OCR 过"的落实。`uniform`→统一中位宽、`enhance`→`_enhance_jpeg` 锐化+对比；`get_pixmap` 烘焙 `/Rotate`→`rotation=0`→OCR/embed 坐标平凡。manga 引擎仍强制 OCR。
  - **③** `subprocess` 跑 OCR（vision=`google_vision_ocr.py`，manga=`mokuro_ocr_book.py` 用 `MANGA_OCR_PYTHON`=manga-ocr-venv），边跑边把 `progress.json`（vision `eta_minutes` / mokuro `eta_hours`）同步进状态。**④** 嵌入脚本（vision=`embed_google_ocr_to_pdf.py` / manga=`embed_ocr_to_pdf.py`）嵌不可见文字层到库外临时文件。**⑤ 原地替换**原 PDF（先备份到 `state/book-preprocess/<sha>.orig.pdf`，**不放 vault 以免污染书列表**）。状态全程写 `state/book-preprocess/<sha>.json`（`<sha>`=`sha1(resolve路径)[:16]`）。**全失败 guard** 兼容 `text/chars`(vision)+`blocks`(mokuro)。
- **引擎怎么选**：默认 **📄 文档=Vision**（快 ~2s/页，印刷/正文/表格质量高，**含漫画气泡也能读**——只要修了上面踩坑 17 的旋转 bug；料理师1.pdf 实测 Vision 完全够用）。**🎴 漫画=manga-ocr** 是 Vision 真读不出的高度风格化手写漫画的兜底：CPU ~40s/页慢，且**会幻觉**（难读区吐出 `そういえば、そうでしょうか` 之类重复假文本 → 污染文字层）。所以**优先 Vision**，manga 仅在 Vision 明显漏识时手动选（带确认弹窗警告慢）。
- **✨ 增加清晰度按钮（2026-06）**：第三个按钮 = Vision OCR + 统一页宽 + **清晰度增强**（`--enhance`）。给模糊扫描书锐化：栅格化每页后 `_enhance_jpeg`（PIL `UnsharpMask(2.2,170,2)` + `Contrast 1.35`，**保留色彩**，封面/橙边框不变灰），字迹明显变黑变利、网点不崩。比纯 OCR 慢（栅格化+PIL，每本 ~2min）。📄/🎴 默认**不**增强（忠实原图）；增强只在点「增加清晰度」时做。后端 `rebuild_pages(uniform, enhance, progress_cb)` 统一栅格化重建（`rebuild = enhance or uniform or 部分页缺文字`，逐页回 progress_cb 报 normalizing 进度防卡 3%）。实测料理师1/2/3 三本扫描书:等宽+rot0+全文可选+字迹增强。⚠ 增强=2× 栅格化+锐化(锐化加高频→JPEG 更大),产物反而比原扫描大(料理师 part2 80 页 143MB→225MB),靠下面「压缩」回收。
- **🗜 压缩按钮（2026-06）**：第四个按钮 = `scripts/compress_pdf.py`（`/api/compress-async` 起 detached 子进程，复用 book-preprocess 状态文件 → 进度条/刷新恢复/KillMode 全共用，phase=`compressing` 已进各 active 白名单）。**PyMuPDF 逐图重压**：`page.get_images` → `extract_image` → PIL 降采样(长边≤2400px,BICUBIC)+ JPEG 重压(q72) → `page.replace_image(xref, stream)` → `doc.save(garbage=1)`(不用 garbage=4/deflate,Pi 上对 100MB+ 卡 6min+) → qpdf 重线性化 → 原地替换。实测扫描书 **省 ~3/4**（part2 切片 11MB→2.5MB=22%），文字层完整。
  - ⚠⚠ **绝对不能用 Ghostscript（致命坑，2026-06-02 踩）**：gs `-dPDFSETTINGS=/ebook` 压完体积是省了（省 ~1/3），但 **gs pdfwrite 重写/子集化字体时破坏了 OCR 文字层的 ToUnicode CMap** → PDF.js 靠字形照常显示日文,但 **PyMuPDF `get_text` 抽出乱码**（`P Q ͳ Μ ๏…` 希腊/组合符）→ 无 CJK → **只能选单字、字典/搜索/振假名全废**。part1/part2 都被压坏过,靠 `restore_textlayer.py` 救回。**改用 PyMuPDF 只换图像流、文字/字体对象一字不动** → ToUnicode 完好,根治。
  - 有损图像但**安全**：`.orig.pdf`（真·扫描原图）不动 → 不满意可重跑「增加清晰度」从原图重建。压缩后 mtime 变 → char 缓存 + 前端 IndexedDB 缓存自动失效重建/重下；page-chars 坐标按 pt（页尺寸不变）→ 选中不受影响。额外:压到 <220MB 后进前端 IndexedDB 缓存上限 → 可本地缓存秒开。
- **`scripts/restore_textlayer.py`（2026-06）**：救被 gs 压坏文字层的扫描书,**免重跑 OCR**：`.orig.pdf` 按原参数 `rebuild_pages(uniform,enhance)` 重建增强图(几何与现存 sidecar 对齐)→ 复用 `state/google-vision-ocr/<sha>/` 现存 char sidecar `embed_google_ocr_to_pdf.py` 重新嵌入 → qpdf 线性化。前提:orig.pdf + sidecar 都在。
- **路由（`pdf_reader.py`）**：`POST /api/preprocess-async`（body 带 `engine`，用 `APP_PYTHON` + `start_new_session=True` **detached** 起编排器 → 关网页/webapp 重启都不中断；重复启动守卫**按进程存活判定**：状态里 `pid` 进程还活着才拦，死进程留的陈旧 in-progress 状态允许直接重跑）；`GET /api/preprocess-status?file=`（读状态文件，文件驱动 → 重启不丢进度；**存活检测**：进行中相位但 `pid` 进程已退出 [`os.kill(pid,0)` 不存在] 且 >30s 未更新 → 返回 `error` 让进度条停在 ✗ 而非永久卡住）；`POST /api/delete-pdf`（删 PDF + 清 OCR/预处理/备份 sidecar，`_safe_vault_path` 挡路径穿越）。编排器启动时把自己 `pid` 写进状态文件供前两者用。
- **前端（`pdf_index.html`）**：每本书三个引擎按钮 → 共用进度条轮询 `preprocess-status`（轮询断了后台不停；跑时按钮都禁用）；长按 ~500ms / 右键 → 弹菜单（重命名/删除）。
- **刷新后恢复进度条（2026-06）**：纯前端轮询的进度条刷新页面就没了，但后台 detached 任务还在跑。加 `GET /api/preprocess-active`（扫 `book-preprocess/*.json`，挑「进行中相位且 `pid` 存活」的，从 `pdf` 字段反查 rel）；`pdf_index.html` 加载时 `resumeActiveJobs()` 据此重挂进度条 + `pollPrep`。
  - ⚠ **「进行中相位」白名单必须含 `normalizing`**：相位有 `detecting/normalizing/ocr/embedding/done/error`，最初 `preprocess-active`/`preprocess-status` 存活判定只列了 `detecting/ocr/embedding`，漏 `normalizing`（统一页宽/锐化的栅格化重建阶段，几十页要好几分钟）→ 重建期间任务对接口隐形、刷新看不到进度条。三处白名单（active 过滤 + status 存活检测 + async 重复启动守卫）统一为 `("detecting","normalizing","ocr","embedding")`。
- **任务别被 webapp 重启杀掉**：detached 子进程虽 `start_new_session=True`，但仍在 webapp 的 systemd **cgroup** 内 → 默认 `KillMode=control-group` 会在 `systemctl restart/stop webapp` 时连子进程一起 SIGKILL（部署一次就把在跑的 OCR 杀了）。Pi 的 `webapp.service` 改 **`KillMode=process`**（只杀主进程，留子进程跑完）→ 重启 webapp 不中断 OCR。注：单纯**浏览器刷新**本就不重启 webapp、不受影响；这条只针对 webapp 进程级重启。
- **「不中断」三层**：编排器 detached（关网页不停）+ 状态写文件（webapp 重启进度不丢）+ OCR sidecar 断点续传（进程被杀重跑自动续）。
- ⚠ Vision 烧 GCP 赠金（配额计数见 `google_vision_ocr.ocr_one_page` → `log_usage`）；已有文字层的书 vision 引擎秒判跳过、零成本。巨幅扫描页/旋转页的尺寸/JPEG/重试/旋转坑见上面踩坑 14–17。

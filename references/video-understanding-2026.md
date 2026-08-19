# 视频理解 2026：调研结论与我们的架构判据

2026-08-17 一晚上五份独立调研（含直接读 llama.cpp / transformers / vLLM 源码）的沉淀。
**下次想"换个更好的视频模型"之前先读这里**——大概率不用换。

---

## 一句话结论

> **没有任何生产级 VLM 的 video encoder 有真正的时序建模。所谓"视频输入"，
> 在 Qwen3-VL / Gemini / llama.cpp 全家都是"抽帧 + 文本时间戳"。**
>
> 而工业界的通用生产形态是：**廉价确定性信号扫全部 → 结构化状态 → 昂贵模型
> 只看被选中的候选**。我们的架构（血条测 when，VLM 解释 what/why）就是这个形态。

---

## 一、为什么"抽帧"不是妥协

### 官方设计就是这样（读源码验证）

Qwen3-VL **主动废除了时序位置编码**：`time_interval` 硬编码为 1，源码注释原文
「t_index 恒为 0，因为我们用 timestamps 编码时序」。视频被拆成 N 个独立帧 chunk，
帧间插入文本 `<3.0 seconds>`。transformers / vLLM / SGLang 实现一致。

官方理由：Qwen2.5-VL 的 time-aligned MRoPE 在长视频下产生**过大且稀疏**的时序
position ID，反而劣化长时序理解。所以这是主动权衡，不是偷懒。

Gemini 同样：官方文档只写 1 FPS 采样、258 tokens/帧（low 66）、音频 32 tokens/秒，
**从未描述过 encoder 层有时序结构**。

llama.cpp 的 `--video` 更直接——**它就是 `--image` 的别名**（同一个参数、同一个
vector），额外做的只有调 ffmpeg 按写死的 `fps=4` 抽帧 + 每 5 秒插一条文本时间戳。
⚠️ 且 ffmpeg 的 fps filter 在目标帧率高于源帧率时会**复制帧**（实测 8 帧素材被复制
成 27 帧，近 70% 冗余）。**自己抽帧比 `--video` 更好。**

### 时序顺序对现有模型的贡献 ≈ 0

| 实验 | 结果 |
|---|---|
| MVBench 打乱帧序 | VideoChat2 **91%** 答案不变，Tarsier 82% |
| GPT-4o 纯图像（无时序） | 比随机高 **20.5%**，几乎追平视频模型 |
| Llama-3 纯文本（不看视频） | 比随机高 **10.8%** |
| 16 模型四条件消融 | 「**帧多样性提供绝大部分收益，时序顺序贡献接近零**」 |
| Gemini-3-Pro 严格时空定位 | **1.0%**（要求答案对 + 定位对） |
| Gemini-3-Pro 时序 tIoU | **32.0%** |

**推论：选对帧的边际收益，远大于换架构。**

### 但纯时序信号上，所有模型是 0 分

SpookyBench（CVPR 2026）：信息**只**存在于帧序列中（单帧是噪声）。
人类 >98%，**所有 VLM 全 0%**（GPT-4o、Gemini 无一例外）。

论文诊断：「VLM 缺少 frame differencing / 时序积分机制。
**信息在计算上是可提取的，只是从来没到达语言模型。**」

---

## 二、⭐ 最高性价比的改进：把运动画进帧里

同一篇论文的缓解实验——用经典 Farneback 光流算 motion boundary **叠加到帧上**：

    Qwen2-VL-7B   0% → 51.54%
    GPT-4o        0% → 59.10%

**不换模型、不训练，只改送进去之前的图像预处理。**
已实现于 `scripts/motion_overlay.py`（flow / rgb / diff 三种渲染）。

### 我们实测踩到的两个坑

1. **游戏镜头本身在动**：玩家转视角时全画面有位移，直接叠光流会整屏染色、
   局部动作反被淹没。论文素材是固定摄像头。→ 已加**全局运动补偿**（取光流中位数
   当镜头位移减掉）。
2. **光流需要相邻帧**：Farneback 假设小位移。拿间隔 1–2.7 秒的证据帧去算，
   光流本身就是噪声。→ 探针已加**自适应帧率**：平时 2.5Hz，确认血量变化后
   临时提到 8Hz 连拍 1.2 秒。

### 下次可以升级的（本次未做）

- **RTX 40 系有硬件光流引擎 NVOFA**，比 Ampere 快 2.5×：1080p 4×4/FAST 档
  **1296 fps**（我们现在用的是 CPU Farneback）。
  ⚠️ `cv2.cuda.NvidiaOpticalFlow_2_0` **不在 pip 的 opencv-python 里**，要自己编
  opencv_contrib；且**必须喂灰度图**。
- **压缩域 motion vector**：`PyNvVideoCodec` v2.2.0（MIT，**有 Windows wheel**，
  需驱动 590+）的 `SimpleDecoder(enableDecodeStats=True)` 返回每 16×16 块的
  MV/QP/CU 类型，**H.264 与 H.265 都支持**。
  ⚠️ FFmpeg 的 `-flags2 +export_mvs` **至今不支持 HEVC**（源码里 `hevcdec.c` 的
  `EXPORT_MVS` 出现 0 次），别走这条路。

---

## 三、工业界实际在跑什么（全部一手来源）

**通用形态：廉价信号扫全部 → 结构化状态 → 昂贵模型只看候选。**

| 领域 | 实际做法 |
|---|---|
| **安防（Verkada 白皮书）** | 边缘 10fps 检测 → 建 tracklet → 抽最有信息量的**高清裁剪** → **只把裁剪送云端模型，不送整帧** → 输出缓存 1 万条 |
| **体育（SoccerNet 25/26 冠军）** | YOLO + 跟踪 + ReID + 相机标定 + 几何。**VLM 只赢了五个任务里的语言那个**，且需先跑检测器给球员编号（set-of-marks） |
| **内容审核** | **1 fps 采样是全行业标准**。AWS 明说"视频和图像审核用同一个模型"——所谓视频审核就是图像分类器在循环 |
| **电竞** | 服务器遥测完胜。CV 输不是因为精度，是**延迟**：公开直播流被延迟 5–10 分钟防偷跑 |
| **游戏 highlight** | NVIDIA Highlights = 纯游戏端 SDK（已 legacy）；Medal = replay/日志解析（开 Streamer Mode 就失效）；**唯一做像素级 CV 的 Powder.gg 破产了** |

### 可直接抄的参数（Frigate NVR，开源可读）

三层级联：**帧差**（阈值 30 亮度差、轮廓面积 10px、`lightning_threshold` 0.8 拒绝
全局突变）→ 只在 motion box 内跑目标检测 → **被跟踪目标生命周期结束时**才送 VLM。

**我们的 Stage 1 比它更好**：血条是语义直接相关的信号，帧差不是。

### 一句值得记住的警告

Google NeurIPS'23：置信度门控的级联「**在理论与实践上都显著次优**」。
**难的是那道门，不是那个梯子。** 我们的门（血条变化）比大多数置信度阈值都硬。

---

## 四、有真时序的模型：存在、可用，但不是 VLM

| 模型 | 时序机制 | 可用性 | 适合做什么 |
|---|---|---|---|
| **V-JEPA 2** (Meta) | 3D tubelet + 3D RoPE，**16 帧联合编码** | 已进 HF transformers，冻结 encoder + attentive probe 单卡可训 | **细粒度动作分类**（"这一招是横扫还是突刺"）。SSv2 **77.3%** |
| **VideoPrism** (DeepMind) | ViViT factorized：ViT + **4 层 temporal attention** | HF 有权重，LvT 变体支持 zero-shot 文搜视频 | 开集检索、少样本敌人识别 |
| StreamFormer / OmniStream | encoder 内因果时序 + 持久 KV | ✅ 架构最正确 | ❌ 采用率≈0（月下载个位数） |

**关键**：它们输出 embedding，**不会说话、不会解释因果**。正确用法是
**它们做检测、VLM 做解释**——而这正是我们已有的架构，只是把"血条"换成/补上
"V-JEPA probe"。

---

## 五、明确不值得做

- **TAL（时序动作定位）**：领域已停滞。EPIC-Kitchens 两年 31.97 → **31.98**；
  四年里检测头架构只带来 1.3 点；零样本 5–27% mAP；HuggingFace 上**零个 TAL 模型**，
  权重挂 Google Drive，号称 2025 SOTA 的 TimeLoc 仓库**只有一个 README**。
  而且它解决的正是我们已经用血条解决了的问题。
- **等更好的通用视频模型**：唯一在做真时序的两个采用率接近零，没有生态。
- **Streaming VLM**（StreamingVLM / LiveCC 等）：16GB 装不下 7-8B；且我们做的是
  **回放分析**，可以随便重看全片，流式的所有优势对我们都是零。
- **视频生成模型的 encoder**（Sora/Veo/Cosmos）：latent 确实是真时空的，但没有任何
  一家把它开放/复用于理解任务，且它是为重建优化而非判别。
- **PySceneDetect 之类场景检测**：一整段 gameplay 是**一个连续镜头**，
  它会被镜头急转和特效误触发，同时漏掉所有真事件。

---

## 六、还没做的（按性价比）

| 优先级 | 做什么 | 证据 |
|---|---|---|
| **P0** | **音频信号**：受击音/弹反/Boss 吼/BGM 切换的 RMS + onset 检测 | 体育 highlight 实测**音频 89% > 视频帧 83%**；游戏厂商专利就是靠音频指示器；**音效比血条更早**（血条是结果，音效是原因） |
| **P1** | **IG-VLM 帧网格**：6 帧拼成 3×2 单图送 | 10 个 zero-shot benchmark 里 **9 个**胜过逐帧送；省 token 且像素级保留时序 |
| **P1** | **Set-of-Mark**：先跑检测器，把物体 ID/框叠到帧上再喂 VLM | NVIDIA VSS 的做法；SoccerNet 冠军也用（给球员编号） |
| **P2** | **V-JEPA 2 + attentive probe** 做招式/敌人分类器 | 唯一能补 VLM 时序短板的实用路径，2–5 天含标注 |
| **P2** | **PyNvVideoCodec MV** 做第二触发源 | 补上"血条没变但画面剧变"的事件 |

---

## 相关

- [[evidence-quality-lessons]]——证据可判读性的四条规则（同一晚沉淀，讲的是
  "存什么"；本文讲的是"用什么模型"）
- `scripts/motion_overlay.py` / `scripts/frame_pick.py` / `scripts/video_prep.py`
- `scripts/nightreign_probe.py`——参考实现：确定性信号 + 信号驱动取帧 + 自适应连拍

---
name: look-through-camera
description: Take a fresh photo from a house camera when the answer can only come from seeing the scene. Use when the user asks what something looks like, whether a light or appliance is on/off, whether the trash was taken out, what is on the desk, whether someone or a pet is there, or when a judgement you must make depends on the current physical state of a room. Cameras - pi (Pi, C920), usb (desk), builtin (rear 13MP), usb5m (front).
---

# 看一眼现场

```bash
python %LOCALAPPDATA%\BWReader\camera_capture.py list        # 有哪些
python %LOCALAPPDATA%\BWReader\camera_capture.py snap usb    # 现在拍一张
```

`snap` 打印一行 JSON，`path` 就是刚拍的照片，直接读它。约 5 秒
（连拍 8 帧挑最清晰的一张，`sharpness` / `brightness` 一并返回）。

| id | 是什么 |
|---|---|
| `pi` | C920，挂在 Pi 上 |
| `usb` | 书桌 |
| `builtin` | 机身后置 13MP |
| `usb5m` | 机身前置 5MP |

用户没指定哪台时：问的是桌面/手边的事用 `usb`，问房间里的事用 `pi`，
拿不准就先 `list` 再按 label 选，或者直接问用户。

---

## ⚠ 什么时候该用

**只在"看一眼就能定"的时候用，而且是你自己决定要看的那一刻。**

- ✅ 只有画面能答的问题：桌上那个是什么 / 灯关了没 / 垃圾拿出去了吗
- ✅ 你要做的判断依赖现场状态，而且**拍一张就够**

- ❌ **不要为了"顺便了解一下"而拍。** 图像**故意**不进快照载荷，就是为了
  不让你每取一次上下文就被塞一张家里的照片 —— 这条是用户明说的边界。
- ❌ 不要连拍、不要轮询。要看的是"变化"的话，那是另做一套的事，不是这个。
- ❌ 判断归你。脚本不做任何画面判断，它只负责把一张清楚的图交给你。

## ⚠ 拍不到就说拍不到

失败时 JSON 是 `ok:false` 并带原因（设备被占用、Pi 不可达等）。

**如实说**，不要拿旧图或猜测顶替 —— 一张过期的现场图比没有图更危险，
因为它看起来同样可信。

## ⚠ 隐私

这是**家里的**摄像头。拍到的东西只用于回答当下那个问题：

- 不要转述与问题无关的画面内容
- 不要把图或它的描述写进会长期留存的地方（笔记、待办正文、日志），
  除非用户明确要求

## 硬件会变

摄像头登记在 `camera-sources.json`，一台一条。将来换带云台/补光的机器时，
`snap <id>` 这句**一个字都不用改**；云台和补光会作为新子命令
（`pan` / `light`）与 `snap` 平级。

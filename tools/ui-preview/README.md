# 设置面板本地预览

**为什么有它**：2026-09-20 之前，改样式的循环是「用户报问题 → 我猜根因 → 出
TestFlight 构建 → 用户再报」。一天之内因此来回了五次（分段控件折行、「语言」
错位、面板能横拖、卡片发蓝、tab 溢出），而**每一个都能在出构建前看出来**。

这个预览把 `rc-ui.js` + `rc-settings.js` 挂进一张静态页，面板就能在浏览器里
打开、量尺寸、扫 DOM。第一次用它就当场查到三件：

- 分段控件在 App 里溢出 88px（6 个分类要 387px，容器只有 379px），而滚动条
  是隐藏的 —— 用户既看不见也不知道能滑
- 界面上还剩 17 个 emoji 没换成图标（上一轮的映射表没覆盖到）
- 一处说明文字写着「👁 跟踪」，而那个按钮早就是星 —— 两边对不上

## 用法

```bash
python3 tools/ui-preview/serve.py        # 起在 http://127.0.0.1:8931
```

然后在浏览器里打开。常用的几条检查（浏览器控制台里跑）：

```js
// 分段控件有没有溢出（滚动条是隐藏的，溢出了用户也看不出来）
const t = document.querySelector('.rc-set-mask .set-tabs');
({ 容器: t.clientWidth, 内容: t.scrollWidth, 溢出: t.scrollWidth - t.clientWidth })

// 界面上还有没有 emoji 当图标用
const re = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/gu, hit = {};
const w = document.createTreeWalker(document.getElementById('settings-mask'), NodeFilter.SHOW_TEXT);
for (let n; (n = w.nextNode());) for (const c of (n.textContent.match(re) || [])) hit[c] = (hit[c]||0)+1;
hit
```

## 它不能替代什么

- **宿主门控**：预览里 host='pdf' 且没有原生桥，所以「设备」「网页翻译」这两个
  分类、以及 epub 专属的区块都是隐藏的。量 tab 溢出时要手动 `display=''` 放出来。
- **真机材质**：`backdrop-filter` 在桌面浏览器和 iPad 上观感不同，材质还得真机看。
- **真实数据**：模型配置表、词典状态这些由桩顶替，只能看布局不能看内容。

⚠ 别把这里的 `*.js` 当成源 —— `serve.py` 每次启动都从 `_server_deploy/static/pdf/`
重新拷贝。改代码改那边。

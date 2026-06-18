// ── 23-bookshelf.js:已退役(2026-06-18)──
// 原来这里是「阅读器内浮层书单」(返回书架=零跳转秒开的临时选择层)。用户嫌它临时,改成
// 返回书架直接进**完整书架页 /pdf/**(正经书库:压缩/预处理/预热/🧮公式识别 都在那)。
// goPdfList 已改为 location.href='/pdf/'(见 03-loader.js)→ 本浮层不再被调用,整体移除。
// 公式 OCR 按钮迁到 templates/pdf_index.html(formulaOCR,走 /pdf/api/formula-ocr,Claude 视觉无需 PC)。

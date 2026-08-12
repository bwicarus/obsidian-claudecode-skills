#!/usr/bin/env python3
"""build.py — 从阅读器源码拉共享层文件,零改动包一层 document 门面参数 → vendor/

设计铁律(用户拍板):完全复用阅读器原有能力,不 fork 不重写。
所以 vendor/ 下的文件是**生成物**:
    ;(function(document){ <阅读器源码逐字> })(window.__bwReaderDoc);
参数遮蔽全局 document → rc-* 的所有 DOM 根引用进扩展 Shadow DOM(见 src/facade.js)。
阅读器侧更新后重跑本脚本即同步,永不手改 vendor/。

用法: python3 build.py        # 生成 + 校验(包装体与源逐字节一致)
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SRC = ROOT / "_server_deploy" / "static" / "pdf"
DST = HERE / "vendor"

# 按加载顺序列出(manifest content_scripts 同序)。里程碑推进时往后追加。
FILES = [
    "rc-core.js",        # window.RC 地基:use/adapter/config/endpoints + esc/debounce/reqJson/toast
    "rc-offline-dictionary.js", # App 私有按需词典；扩展包不携带词典数据
    "rc-ui.js",          # Reader UI Kit:共享视觉令牌 + 基础按钮/输入/卡片/弹层/拖动态
    "rc-flashcard.js",   # 草稿卡/复习状态机/钉到页面
    "rc-snippets.js",    # 选段 → 笔记 / Anki
    "rc-review.js",      # 跨书复习队列
    "rc-md.js",          # markdown+数学占位渲染(marked 前置)
    "rc-result.js",      # AI 结果模态 + 草稿系统
    "rc-wordpop.js",     # 查词弹框(英:音标/词频/变形;日:音调线/变形/例句/汉字拆解/AI 深入讲解)
    "rc-phrasepop.js",   # 词组浮层(收藏/掌握/翻译)
    "rc-figures.js",     # 图片结果框 chrome(PDF 图像数据仍由 PWA 提供)
    "rc-highlight.js",   # 高亮编辑器/列表/手势(UI 层)
    "rc-knowledge.js",   # KG 节点展示与跟踪
    "rc-sidedrawer.js",  # 右侧统一抽屉:把手/tab/磨砂玻璃/悬浮/iOS 命中盒修复
    "rc-assistant.js",   # AI 助手侧栏(SSE 流式/历史/🎤语音输入=页面侧 Web Speech)
    "rc-grammar.js",     # 跨页面语法分析卡
    "rc-settings.js",    # 跨站 AI/翻译/语法设置
    "rc-favorites.js",   # 跨书收藏
    "rc-video.js",       # 助手图片/视频偏好与结果卡
    "rc-videoplayer.js", # 浮动视频播放器
    "rc-toolchip.js",    # 工具流程条(turnCard 依赖)
    "rc-turncard.js",    # 轮次卡容器(text/card/hlcard/tool part)
    "rc-voicectx.js",    # 语音上下文统一注入端口
    "rc-computer-voice.js", # 电脑客户端桥接器:固定 WSS / direct v3 / 双向 PCM
    "rc-voicecall.js",   # 实时语音通话与卡片渲染
    "rc-stickynote.js",  # 原版便签(普通网页复用；PDF 经桥交回阅读器本体)
]

# 纯库,不包装直接拷贝(无 DOM 根/fetch 耦合): {源相对路径: vendor 文件名}
LIBS = {
    "html2canvas.min.js": "html2canvas.min.js",
    "../reader-runtime/account-context.js": "reader-runtime-account-context.js",
    "../reader-runtime/context-selection-registry.js": "reader-runtime-context-selection-registry.js",
    "../reader-runtime/computer-voice-webrtc.js": "reader-runtime-computer-voice-webrtc.js",
    "../reader-runtime/card-improvement-actions.js": "reader-runtime-card-improvement-actions.js",
    "../reader-runtime/extension-account-storage.js": "reader-runtime-extension-account-storage.js",
    "../reader-runtime/data-store.js": "reader-runtime-data-store.js",
    "../reader-runtime/indexeddb-store.js": "reader-runtime-indexeddb-store.js",
    "../reader-runtime/data-registry.js": "reader-runtime-data-registry.js",
    "../reader-runtime/sync-owner-lease.js": "reader-runtime-sync-owner-lease.js",
    "../reader-runtime/sync-gateway.js": "reader-runtime-sync-gateway.js",
    "../reader-runtime/server-sync-transport.js": "reader-runtime-server-sync-transport.js",
    "../reader-runtime/direct-sync-protocol.js": "reader-runtime-direct-sync-protocol.js",
    "../reader-runtime/direct-sync-signal-transport.js": "reader-runtime-direct-sync-signal-transport.js",
    "../reader-runtime/direct-sync-host.js": "reader-runtime-direct-sync-host.js",
    "../reader-runtime/direct-sync-leader.js": "reader-runtime-direct-sync-leader.js",
    "../reader-runtime/sync-coordinator.js": "reader-runtime-sync-coordinator.js",
    "../reader-runtime/sync-runtime.js": "reader-runtime-sync-runtime.js",
    "../reader-runtime/sync-conflict-control.js": "reader-runtime-sync-conflict-control.js",
    "../reader-runtime/document-note-repository.js": "reader-runtime-document-note-repository.js",
    "../reader-runtime/interaction-policy.js": "reader-runtime-interaction-policy.js",
    "../reader-runtime/vocabulary-state.js": "reader-runtime-vocabulary-state.js",
    # marked 只部署在 nginx 静态目录(模板引 /static/qa/marked.js),仓库里没有源副本
    "/var/www/html/static/qa/marked.js": "marked.js",
    # tex-chtml-full 构建(全 TeX 扩展内置,零运行时组件拉取;字体 woff 运行时回落 jsdelivr,
    # 布局样式由 shell 镜像进 shadow,@font-face 留真 head 注册全局字体)
    "/var/www/html/static/qa/mathjax-full.js": "mathjax-full.js",
}

GUARDED_LIBS = {
    "rc-ink.js": "rc-ink.js",  # 便签手写依赖的纯几何核心
    "web-immersive.js": "web-immersive.js",  # 真实网页沉浸翻译：滚到哪译到哪
}

PROVIDER_GUARD = "if (window.__bwPwaProviderOnly) return;\n"
WRAP_TOP = ";(function(document, fetch){\n" + PROVIDER_GUARD
WRAP_BOT = "\n})(window.__bwReaderDoc || document, window.__bwReaderFetch || window.fetch.bind(window));\n"
LIB_WRAP_TOP = ";(function(){\n" + PROVIDER_GUARD
LIB_WRAP_BOT = "\n})();\n"


def read_text_lf(path: pathlib.Path) -> str:
    """Read source without platform newline translation."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text_lf(path: pathlib.Path, text: str) -> None:
    """Generated JavaScript is always LF, including on Windows."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def main() -> int:
    DST.mkdir(exist_ok=True)
    for rel, name in LIBS.items():
        rp = pathlib.Path(rel)
        # A POSIX absolute path is not considered absolute by WindowsPath.
        # Keep that distinction explicitly for Pi-only nginx assets.
        pi_absolute = rel.startswith("/")
        source = rp if (rp.is_absolute() or pi_absolute) else SRC / rel
        destination = DST / name
        if not source.is_file():
            # Pi-only nginx assets have no Windows source path.  Reuse the
            # already tracked immutable vendor copy; never fabricate or
            # newline-normalize it on the development machine.
            if (rp.is_absolute() or pi_absolute) and destination.is_file():
                print(f"↷ {name}: Windows 无 Pi 静态源，保留现有 vendor 字节")
                continue
            raise FileNotFoundError(source)
        payload = source.read_bytes()
        destination.write_bytes(payload)
        print(f"✓ {name}: {len(payload)} bytes → vendor/(纯库逐字节直拷)")
    for rel, name in GUARDED_LIBS.items():
        text = read_text_lf(SRC / rel)
        header = f"/* AUTO-GENERATED by build.py — 源=_server_deploy/static/pdf/{name} 逐字包装,勿手改 */\n"
        out = header + LIB_WRAP_TOP + text + LIB_WRAP_BOT
        body = out[len(header) + len(LIB_WRAP_TOP): -len(LIB_WRAP_BOT)]
        assert body == text, f"{name}: 包装体与源文件不一致"
        write_text_lf(DST / name, out)
        print(f"✓ {name}: {len(text)} bytes → vendor/(provider guard + 逐字包装)")
    for name in FILES:
        src_path = SRC / name
        text = read_text_lf(src_path)
        if "window.__bwReaderDoc" in text:
            print(f"✗ {name}: 源文件里出现门面符号,拒绝(防双重包装)", file=sys.stderr)
            return 1
        header = f"/* AUTO-GENERATED by build.py — 源=_server_deploy/static/pdf/{name} 逐字包装,勿手改 */\n"
        out = header + WRAP_TOP + text + WRAP_BOT
        # 零漂移校验:包装体去头去尾必须与源文件逐字节一致
        body = out[len(header) + len(WRAP_TOP): -len(WRAP_BOT)]
        assert body == text, f"{name}: 包装体与源文件不一致"
        write_text_lf(DST / name, out)
        print(f"✓ {name}: {len(text)} bytes → vendor/(逐字包装,校验通过)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

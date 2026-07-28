#!/usr/bin/env python3
"""真实 Chromium 合同：侧栏设置可管理迟到注入的下方快捷按钮。"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "_server_deploy/static/pdf/rc-sidedrawer.js"
DEFAULT_CHROME = (
    Path.home() / ".cache/ms-playwright/chromium-1223/chrome-linux/chrome"
)
CHROME = Path(os.environ.get("BW_PLAYWRIGHT_CHROME", DEFAULT_CHROME))


def main() -> None:
    assert SOURCE.is_file(), SOURCE
    assert CHROME.is_file(), CHROME

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(CHROME),
            headless=True,
        )
        try:
            page = browser.new_page()
            page.route(
                "http://sidebar-contract.test/*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=(
                        "<!doctype html><meta charset=utf-8>"
                        "<body><aside id=ep-side>"
                        "<section class=ep-side-pane data-pane=asst></section>"
                        "<section class=ep-side-pane data-pane=kg></section>"
                        "</aside></body>"
                    ),
                ),
            )
            page.goto("http://sidebar-contract.test/")
            page.evaluate("window.RC = {}")
            page.add_script_tag(path=str(SOURCE))
            page.evaluate(
                """() => RC.sidedrawer.init({
                  defaultTab: 'asst',
                  tabs: [
                    {name:'asst', label:'助手'},
                    {name:'kg', label:'知识点'}
                  ]
                })"""
            )

            # 快捷栏故意在 drawer.init() 之后挂载，复现 rc-assistant /
            # rc-review / rc-voicecall 的真实迟到注入顺序。
            page.evaluate(
                """() => {
                  const quick = document.createElement('div');
                  quick.id = 'asst-quick';
                  const defs = [
                    ['explicit', '复习模式'],
                    ['clear', '清空'],
                    ['media', '配图'],
                    ['legacy-id', '模型']
                  ];
                  defs.forEach(([kind, label]) => {
                    const button = document.createElement('button');
                    button.textContent = label;
                    if (kind === 'explicit') {
                      button.dataset.quickKey = 'review-mode';
                      button.dataset.quickLabel = '复习模式';
                      button.addEventListener('click', () => {
                        window.__reviewClicks = (window.__reviewClicks || 0) + 1;
                      });
                    } else if (kind === 'clear') {
                      button.dataset.q = 'clear';
                    } else if (kind === 'media') {
                      button.className = 'rc-media-tg';
                    } else {
                      button.id = 'legacy-model';
                    }
                    quick.appendChild(button);
                  });
                  document.getElementById('ep-side').appendChild(quick);
                }"""
            )
            page.wait_for_timeout(50)
            page.evaluate(
                "() => document.getElementById('ep-side-set-btn').click()"
            )

            result = page.evaluate(
                """() => ({
                  groups: [...document.querySelectorAll(
                    '#ep-side-settings .ss-row.ss-col>span'
                  )].map(node => node.textContent.trim()),
                  actions: [...document.querySelectorAll(
                    '#ep-action-toggles input[data-quick-key]'
                  )].map(input => ({
                    key: input.dataset.quickKey,
                    label: input.parentNode.textContent.trim(),
                    checked: input.checked
                  })),
                  tabs: document.querySelectorAll(
                    '#ep-tab-toggles input[type=checkbox]'
                  ).length
                })"""
            )
            assert "上方 Tab" in result["groups"], result
            assert "下方快捷按钮" in result["groups"], result
            assert result["tabs"] == 2, result
            keys = {item["key"] for item in result["actions"]}
            assert "key:review-mode" in keys, result
            assert "q:clear" in keys, result
            assert "id:legacy-model" in keys, result
            assert any(key.startswith("media:") for key in keys), result
            assert all(item["checked"] for item in result["actions"]), result

            # 取消勾选只加隐藏 class：按钮节点和点击事件仍然存在。
            page.evaluate(
                """() => {
                  const input = document.querySelector(
                    '#ep-action-toggles input[data-quick-key="key:review-mode"]'
                  );
                  input.checked = false;
                  input.dispatchEvent(new Event('change', {bubbles:true}));
                }"""
            )
            hidden = page.evaluate(
                """() => {
                  const button = document.querySelector(
                    '#asst-quick [data-quick-key="review-mode"]'
                  );
                  button.click();
                  return {
                    count: document.querySelectorAll(
                      '#asst-quick [data-quick-key="review-mode"]'
                    ).length,
                    hidden: button.classList.contains('rc-quick-user-hidden'),
                    display: getComputedStyle(button).display,
                    clicks: window.__reviewClicks,
                    stored: JSON.parse(
                      localStorage.getItem('ep-side-actions-off') || '[]'
                    )
                  };
                }"""
            )
            assert hidden == {
                "count": 1,
                "hidden": True,
                "display": "none",
                "clicks": 1,
                "stored": ["key:review-mode"],
            }, hidden

            # 同 key 的迟到按钮，以及事先已隐藏但刚出现的按钮，都应被
            # MutationObserver 自动应用；打开中的设置菜单也应即时补条目。
            page.evaluate(
                """() => {
                  const quick = document.getElementById('asst-quick');
                  const duplicate = document.createElement('button');
                  duplicate.dataset.quickKey = 'review-mode';
                  duplicate.textContent = '复习模式镜像';
                  quick.appendChild(duplicate);

                  const off = JSON.parse(
                    localStorage.getItem('ep-side-actions-off') || '[]'
                  );
                  off.push('key:late-action');
                  localStorage.setItem(
                    'ep-side-actions-off',
                    JSON.stringify(off)
                  );
                  const late = document.createElement('button');
                  late.dataset.quickKey = 'late-action';
                  late.dataset.quickLabel = '迟到动作';
                  late.textContent = '迟到动作';
                  quick.appendChild(late);
                }"""
            )
            page.wait_for_function(
                """() => {
                  const buttons = [...document.querySelectorAll(
                    '#asst-quick [data-quick-key="review-mode"]'
                  )];
                  const late = document.querySelector(
                    '#asst-quick [data-quick-key="late-action"]'
                  );
                  return buttons.length === 2 &&
                    buttons.every(button =>
                      button.classList.contains('rc-quick-user-hidden')
                    ) &&
                    late?.classList.contains('rc-quick-user-hidden') &&
                    document.querySelector(
                      '#ep-action-toggles input[data-quick-key="key:late-action"]'
                    );
                }"""
            )

            # 恢复勾选会让同 key 的全部镜像重新显示，节点总数不变。
            page.evaluate(
                """() => {
                  const input = document.querySelector(
                    '#ep-action-toggles input[data-quick-key="key:review-mode"]'
                  );
                  input.checked = true;
                  input.dispatchEvent(new Event('change', {bubbles:true}));
                }"""
            )
            restored = page.evaluate(
                """() => {
                  const buttons = [...document.querySelectorAll(
                    '#asst-quick [data-quick-key="review-mode"]'
                  )];
                  return {
                    count: buttons.length,
                    hidden: buttons.filter(button =>
                      button.classList.contains('rc-quick-user-hidden')
                    ).length,
                    stored: JSON.parse(
                      localStorage.getItem('ep-side-actions-off') || '[]'
                    )
                  };
                }"""
            )
            assert restored["count"] == 2, restored
            assert restored["hidden"] == 0, restored
            assert "key:review-mode" not in restored["stored"], restored
            assert "key:late-action" in restored["stored"], restored
            print(
                "OK: sidebar tabs/actions settings, legacy discovery, "
                "persistence and late injection"
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()

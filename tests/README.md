# Smoke tests

只跑核心 invariant，不追求覆盖率。设计目标：

- daily timer / control 面板触发前可选跑一次，提前发现"路由名打错 / 部署没同步 / 关键脚本崩了"这种低级回归
- 用 Python 内置 `unittest`，**不引入新依赖**（pytest / mock 都不要）
- 单文件 < 100 行，运行时间 < 5s

## 运行

```bash
# 全部
python3 -m unittest discover tests -v

# 单个
python3 -m unittest tests.test_task_tracker -v
```

## 当前覆盖

| 文件 | 测什么 | 为什么重要 |
|---|---|---|
| `test_task_tracker.py` | Handle 在非 TTY 时镜像 print 到 stdout | control 面板 webapp_trigger.log 能看到子进程运行细节 |
| `test_pending_notes.py` | pending_notes.py 干净退出 + 输出格式 | register_notes 第一步依赖；输出格式被 daily timer 解析 |
| `test_nav_links_api.py` | `/api/nav-links` 路由存在 + `nav.js` 已部署且含新逻辑 | 全站左侧导航 + per-user 链接持久化的部署 sanity |

## 加新测试的标准

只在以下情况加：

1. 这次 bug 是别处的脚本 silent 失败（光看输出看不出错）
2. 部署忘同步某个文件，下次还可能再忘
3. 跨平台兼容性回归（Windows-only 调用 fall through 到 Linux）

不加：

- AI 输出格式（变化太快）
- Anki 卡片内容（业务逻辑，不是 invariant）
- 性能 / latency（不是这个项目的重点）

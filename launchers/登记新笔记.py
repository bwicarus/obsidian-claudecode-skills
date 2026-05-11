import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"C:\claude\scripts")))

from task_tracker import track
from register_notes import run


def main():
    with track("登记新笔记") as handle:
        try:
            results = run(handle)
        except Exception as e:
            handle.update(f"异常: {e}")
            sys.exit(1)

    if results:
        for r in results:
            print(
                f"{r['name']} | PDF:{r['pdf']} 图片:{r['images']} | "
                f"索引:{r['index']} | 链接:{r['links']} 反向:{r.get('back_links', 0)} | "
                f"Anki:{r['anki']} | {r['time']}s"
            )
    else:
        print("无待处理笔记")


if __name__ == "__main__":
    main()

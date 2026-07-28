#!/usr/bin/env bash
# 远程部署前的共享检出安全闸(在 Pi 上跑,只读 + 一次 fetch,绝不改工作树)。
#
# 背景:Pi 的 /home/bwicarus/claude 是**共享检出** —— 我和 Codex 同用,而且每晚
# daily 流程会重写 anki/records 与 dashboard.json,所以"工作树是脏的"是常态,
# 不能一刀切拒绝。真正危险的只有一种情况:**这次要拉的提交会改到别人正在改的文件**,
# 那样 pull 要么失败、要么让对方的测试跑在被换掉的代码上。
#
# 用法: bash scripts/deploy_remote_guard.sh <目标分支>
# 退出码: 0=可以拉  2=分支不符  3=脏文件与来袭改动有交集  4=git 操作失败

set -uo pipefail
BRANCH="${1:?用法: deploy_remote_guard.sh <目标分支>}"
cd "$(dirname "$0")/.." || exit 4

CUR="$(git rev-parse --abbrev-ref HEAD)" || exit 4
if [ "$CUR" != "$BRANCH" ]; then
  echo "❌ Pi 当前在分支 '$CUR',目标是 '$BRANCH'。"
  echo "   远程不切分支 —— 共享检出上换分支会动到别人的工作。请上机确认。"
  exit 2
fi

git fetch origin --prune --quiet || exit 4

# 来袭改动:这次 ff 会动到哪些文件
INCOMING="$(git diff --name-only "HEAD..origin/$BRANCH")" || exit 4
if [ -z "$INCOMING" ]; then
  echo "✓ Pi 已是最新,无来袭改动"
  exit 0
fi

# 脏文件:已跟踪的改动(未跟踪文件不会被 ff 覆盖,git 自己会拦,这里不重复判)
DIRTY="$(git status --porcelain --untracked-files=no | cut -c4- | sed 's/^"//; s/"$//')"

CONFLICT="$(comm -12 <(printf '%s\n' "$INCOMING" | sort -u) <(printf '%s\n' "$DIRTY" | sort -u))"

if [ -n "$CONFLICT" ]; then
  echo "❌ 有人正在改这些文件,而本次 pull 恰好也要改它们:"
  printf '   %s\n' $CONFLICT
  echo "   不自动 stash、不自动 reset —— 请上机与对方协调后再部署。"
  exit 3
fi

echo "✓ 闸门通过:来袭 $(printf '%s\n' "$INCOMING" | wc -l) 个文件,与 Pi 上 $(printf '%s\n' "$DIRTY" | grep -c . ) 个脏文件无交集"
exit 0

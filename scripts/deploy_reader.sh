#!/bin/bash
# 阅读器安全部署(Pi)：无副作用预检 → 不可变 KG release → 原子切换 → 健康检查。
# 用法: bash scripts/deploy_reader.sh [--preflight-only] [--no-e2e] [--pc]
#
# 唯一允许的生产写入口。普通文件由 reader_deploy_manifest.py 精确列出；
# KG 代码作为一棵只读 release 发布，绝不逐文件覆盖 current，也绝不回退到工作树。
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")/.."

RUN_E2E=1
SYNC_PC=0
PREFLIGHT_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --preflight-only) PREFLIGHT_ONLY=1 ;;
    --no-e2e) RUN_E2E=0 ;;
    --pc) SYNC_PC=1 ;;
    *)
      echo "未知参数: $arg" >&2
      exit 2
      ;;
  esac
done
if [ "$(id -u)" = "0" ]; then
  echo "请以 bwicarus 用户运行；脚本只在精确安装点调用 sudo。" >&2
  exit 2
fi

PROJECT_ROOT="$(pwd -P)"
SRC_REL="_server_deploy/static/pdf/reader.src"
OUT_REL="_server_deploy/static/pdf/reader.js"
SRC="$PROJECT_ROOT/$SRC_REL"
OUT="$PROJECT_ROOT/$OUT_REL"
WEBAPP_ROOT="/home/bwicarus/webapp"
STATIC_ROOT="/var/www/html/static"
SYSTEMD_ROOT="/etc/systemd/system"
KG_RUNTIME_ROOT="/home/bwicarus/reader-runtime/kg"
BACKUP_ROOT="/home/bwicarus/deploy-backups/reader"
DEPLOY_ID="$(date -u +%Y%m%dT%H%M%SZ)-${BASHPID}"
BACKUP_DIR="$BACKUP_ROOT/$DEPLOY_ID"
STAGE_DIR="$(mktemp -d /tmp/bw-reader-deploy.XXXXXX)"
DEPLOY_MANIFEST="$STAGE_DIR/deploy-manifest.tsv"
CANDIDATE_ROOT="$STAGE_DIR/candidate"
MANIFEST_HELPER="$CANDIDATE_ROOT/scripts/reader_deploy_manifest.py"
RELEASE_HELPER="$CANDIDATE_ROOT/scripts/reader_kg_release.py"
BACKUP_MANIFEST="$BACKUP_DIR/files.tsv"
UNIT_STATE_FILE="$BACKUP_DIR/active-units.txt"
KG_STATE_BEFORE="$BACKUP_DIR/kg-state-before.sha256"
DEPLOY_LOCK="$KG_RUNTIME_ROOT/deploy.lock"
ACTIVE_MARKER="$KG_RUNTIME_ROOT/deploy-in-progress.json"
VOICE_RT_UNIT="voice-rt.service"
VOICE_RT_PORT=8767
WEBAPP_UNIT="webapp.service"
WEBAPP_PORT=5000
OBSIDIAN_VAULT_ROOT="${OBSIDIAN_VAULT:-/home/bwicarus/obsidian}"
WRITER_WAIT_SECONDS="${BW_READER_WRITER_WAIT_SECONDS:-30}"
VOICE_STABILITY_SECONDS="${BW_READER_VOICE_STABILITY_SECONDS:-3}"
WRITER_TIMERS=(
  "bwicarus-quick-sync.timer"
  "bwicarus-daily.timer"
  "concept-graph.timer"
)
WRITER_SERVICES=(
  "bwicarus-quick-sync.service"
  "bwicarus-daily.service"
  "concept-graph.service"
)
MANAGED_SERVICES=("webapp.service" "$VOICE_RT_UNIT")
KG_MUTABLE_PATHS=(
  # Core KG outputs owned by the three frozen jobs.  The same exact inventory
  # is used for both the before/after digest and the forensic tar snapshot.
  "knowledge_graph"
  "state/attention"
  "state/pdf-page-brief"
  "state/pdf-page-brief-rename"
  "state/pdf-book-briefs.json"
  "state/pdf-char-cache"
  "state/pdf-figures"
  "state/pdf-search.db"
  "state/kg_audit.json"
  "state/rescan_progress.json"
)
KG_EXTERNAL_MUTABLE_PATHS=(
  # concept-graph.service may create/update generated concept notes here.
  "$OBSIDIAN_VAULT_ROOT/资源/概念"
)
KG_DWELL_REL="state/attention/dwell.jsonl"
DEPLOY_STARTED=0
DEPLOY_FINISHED=0
CURRENT_SWITCHED=0
WRITERS_FROZEN=0
OLD_KG_ID=""
NEW_KG_ID=""
CANDIDATE_DIGEST=""
PAYLOAD_DIGEST=""
VALIDATION_DIGEST=""
E2E_CONFIG_DIGEST=""
ORIGINAL_EXIT=0
ROOT_SHELL_BASHPID="$BASHPID"

PRE_OUT_SHA="$(
  if [ -f "$OUT" ]; then sha256sum "$OUT" | awk '{print $1}'; else echo missing; fi
)"
PRE_OUT_STAT="$(
  if [ -f "$OUT" ]; then stat -c '%s:%Y:%i' "$OUT"; else echo missing; fi
)"

cleanup_stage() {
  case "$STAGE_DIR" in
    /tmp/bw-reader-deploy.*)
      # The isolated probe deliberately seals its release 0444/0555.  Unseal
      # only this mktemp-owned tree before deleting it; production releases
      # are outside STAGE_DIR and are never touched here.
      chmod -R u+w -- "$STAGE_DIR" 2>/dev/null || true
      rm -rf -- "$STAGE_DIR"
      ;;
  esac
}

run_manifest_helper() {
  local root="$1"
  python3 -B - "$MANIFEST_HELPER" "$root" <<'PY'
from pathlib import Path
import runpy
import sys

helper, root = sys.argv[1:]
namespace = runpy.run_path(helper, run_name="reader_deploy_manifest_pinned")
root = Path(root).resolve()
namespace["main"].__globals__["ROOT"] = root
# validate_entries captured the helper's original ROOT as a default argument;
# pass the pinned root explicitly instead of relying on that mutable default.
entries = namespace["validate_entries"](
    namespace["_raw_entries"](),
    root=root,
)
for entry in entries:
    print(
        entry.source_rel,
        entry.target_group,
        entry.target_rel,
        entry.policy,
        sep="\t",
    )
PY
}

hash_candidate_tree() {
  python3 -B - "$CANDIDATE_ROOT" <<'PY'
import hashlib
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
if root.is_symlink() or not root.is_dir():
    raise SystemExit("候选根不是普通目录")
digest = hashlib.sha256()
for path in sorted(root.rglob("*")):
    relative = path.relative_to(root).as_posix()
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise SystemExit(f"候选包含符号链接: {relative}")
    if path.is_dir():
        continue
    if not stat.S_ISREG(mode):
        raise SystemExit(f"候选包含非普通文件: {relative}")
    payload = path.read_bytes()
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(len(payload)).encode("ascii"))
    digest.update(b"\0")
    digest.update(hashlib.sha256(payload).digest())
print(digest.hexdigest())
PY
}

verify_candidate_digest() {
  local actual
  actual="$(hash_candidate_tree)"
  if [ "$actual" != "$CANDIDATE_DIGEST" ]; then
    echo "候选快照摘要漂移: expected=$CANDIDATE_DIGEST actual=$actual" >&2
    return 2
  fi
}

hash_deploy_payload() {
  python3 -B - "$STAGE_DIR" webapp static systemd kg_runtime <<'PY'
import hashlib
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
digest = hashlib.sha256()
for top_name in sys.argv[2:]:
    top = root / top_name
    if top.is_symlink() or not top.is_dir():
        raise SystemExit(f"部署 payload 目录无效: {top_name}")
    for path in sorted(top.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise SystemExit(f"部署 payload 包含符号链接: {relative}")
        if path.is_dir():
            continue
        if not stat.S_ISREG(mode):
            raise SystemExit(f"部署 payload 包含非普通文件: {relative}")
        payload = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
print(digest.hexdigest())
PY
}

verify_deploy_payload_digest() {
  local actual
  actual="$(hash_deploy_payload)"
  if [ "$actual" != "$PAYLOAD_DIGEST" ]; then
    echo "实际部署 payload 摘要漂移: expected=$PAYLOAD_DIGEST actual=$actual" >&2
    return 2
  fi
}

hash_validation_inputs() {
  python3 -B - "$PROJECT_ROOT" <<'PY'
import hashlib
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
inputs = (
    root / "tests",
    root / "scripts" / "deploy_reader.sh",
    root / "scripts" / "reader_deploy_manifest.py",
    root / "scripts" / "reader_kg_release.py",
    root / "scripts" / "reader_e2e.py",
    root / "scripts" / "audit_reader_network.py",
    root / "scripts" / "reader_network_audit_baseline.json",
    root / "scripts" / "vocab" / "test_batch_protocol.py",
)
files = []
for source in inputs:
    if source.is_symlink() or not source.exists():
        raise SystemExit(f"验证输入无效: {source.relative_to(root)}")
    if source.is_file():
        files.append(source)
        continue
    for path in source.rglob("*"):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise SystemExit(f"验证输入含符号链接: {relative}")
        if path.is_dir():
            continue
        if not stat.S_ISREG(mode):
            raise SystemExit(f"验证输入含非普通文件: {relative}")
        files.append(path)
digest = hashlib.sha256()
for path in sorted(files):
    relative = path.relative_to(root).as_posix()
    payload = path.read_bytes()
    digest.update(relative.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(len(payload)).encode("ascii"))
    digest.update(b"\0")
    digest.update(hashlib.sha256(payload).digest())
print(digest.hexdigest())
PY
}

verify_validation_digest() {
  local actual
  actual="$(hash_validation_inputs)"
  if [ "$actual" != "$VALIDATION_DIGEST" ]; then
    echo "验证合同/夹具在预检期间发生漂移" >&2
    return 2
  fi
}

hash_e2e_config() {
  python3 -B - "$PROJECT_ROOT/.env" "$WEBAPP_ROOT/.env" <<'PY'
import hashlib
from pathlib import Path
import stat
import sys

digest = hashlib.sha256()
for raw in sys.argv[1:]:
    path = Path(raw)
    digest.update(str(path).encode("utf-8"))
    digest.update(b"\0")
    if not path.exists() and not path.is_symlink():
        digest.update(b"missing\0")
        continue
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise SystemExit(f"E2E 配置不是普通文件: {path}")
    payload = path.read_bytes()
    digest.update(str(len(payload)).encode("ascii"))
    digest.update(b"\0")
    digest.update(hashlib.sha256(payload).digest())
print(digest.hexdigest())
PY
}

verify_e2e_config_digest() {
  local actual
  actual="$(hash_e2e_config)"
  if [ "$actual" != "$E2E_CONFIG_DIGEST" ]; then
    echo "E2E 运行配置在部署期间发生漂移" >&2
    return 2
  fi
}

verify_checkout_inputs_match_candidate() {
  local current_manifest="$STAGE_DIR/deploy-manifest.current.tsv"
  local source_rel _target_group _target_rel _policy relative_part
  run_manifest_helper "$PROJECT_ROOT" > "$current_manifest"
  cmp -s "$DEPLOY_MANIFEST" "$current_manifest" || {
    echo "工作树部署清单在候选冻结后发生漂移" >&2
    return 2
  }
  while IFS=$'\t' read -r source_rel _target_group _target_rel _policy; do
    cmp -s \
      "$PROJECT_ROOT/$source_rel" \
      "$CANDIDATE_ROOT/$source_rel" || {
      echo "已验证源码在候选冻结后发生漂移: $source_rel" >&2
      return 2
    }
  done < "$DEPLOY_MANIFEST"
  for relative_part in "${READER_PARTS_REL[@]}"; do
    cmp -s \
      "$PROJECT_ROOT/$relative_part" \
      "$CANDIDATE_ROOT/$relative_part" || {
      echo "reader bundle 输入在候选冻结后发生漂移: $relative_part" >&2
      return 2
    }
  done
  cmp -s \
    "$PROJECT_ROOT/extensions/bw-reader-webext/manifest.json" \
    "$CANDIDATE_ROOT/extensions/bw-reader-webext/manifest.json" || {
    echo "扩展 manifest 在候选冻结后发生漂移" >&2
    return 2
  }
  for source_rel in \
    scripts/deploy_reader.sh \
    scripts/reader_deploy_manifest.py \
    scripts/reader_kg_release.py \
    scripts/reader_e2e.py; do
    cmp -s \
      "$PROJECT_ROOT/$source_rel" \
      "$CANDIDATE_ROOT/$source_rel" || {
      echo "部署/验证入口在候选冻结后发生漂移: $source_rel" >&2
      return 2
    }
  done
}

target_root_for() {
  case "$1" in
    webapp) printf '%s\n' "$WEBAPP_ROOT" ;;
    static) printf '%s\n' "$STATIC_ROOT" ;;
    systemd) printf '%s\n' "$SYSTEMD_ROOT" ;;
    kg_runtime)
      echo "kg_runtime 必须整棵发布，禁止逐文件安装" >&2
      return 2
      ;;
    *)
      echo "清单含未知目标组: $1" >&2
      return 2
      ;;
  esac
}

write_json_atomic() {
  local target="$1" status="$2"
  python3 -B - \
    "$target" "$status" "$DEPLOY_ID" "$OLD_KG_ID" "$NEW_KG_ID" \
    "$CANDIDATE_DIGEST" "$PAYLOAD_DIGEST" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile
import time

target = Path(sys.argv[1])
payload = {
    "contract": "bw-reader-deploy-transaction/1",
    "status": sys.argv[2],
    "deployId": sys.argv[3],
    "previousKgRelease": sys.argv[4] or None,
    "candidateKgRelease": sys.argv[5] or None,
    "candidateDigest": sys.argv[6] or None,
    "payloadDigest": sys.argv[7] or None,
    "updatedAt": int(time.time()),
}
target.parent.mkdir(parents=True, exist_ok=True)
fd, raw = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(raw, target)
    dfd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
finally:
    try:
        os.unlink(raw)
    except FileNotFoundError:
        pass
PY
}

atomic_install() {
  local src="$1" dst="$2" mode="$3" uid="$4" gid="$5"
  local tmp="${dst}.deploy-${DEPLOY_ID}"
  sudo install -D -m "$mode" -- "$src" "$tmp"
  sudo chown "$uid:$gid" -- "$tmp"
  sudo mv -f -- "$tmp" "$dst"
  cmp -s -- "$src" "$dst"
}

backup_target() {
  local target="$1" group="$2" rel="$3"
  local backup="$BACKUP_DIR/files/$group/$rel"
  mkdir -p -- "$(dirname "$backup")"
  if [ -e "$target" ] || [ -L "$target" ]; then
    if [ -L "$target" ] || [ ! -f "$target" ]; then
      echo "生产目标不是普通文件: $target" >&2
      return 2
    fi
    cp -a -- "$target" "$backup"
    printf 'present\t%s\t%s\t%s\t%s\t%s\n' \
      "$group" "$rel" \
      "$(stat -c '%a' "$target")" \
      "$(stat -c '%u' "$target")" \
      "$(stat -c '%g' "$target")" >> "$BACKUP_MANIFEST"
  else
    printf 'missing\t%s\t%s\t-\t-\t-\n' \
      "$group" "$rel" >> "$BACKUP_MANIFEST"
  fi
}

restore_backup() {
  local status group rel mode uid gid target_root target backup
  [ -f "$BACKUP_MANIFEST" ] || return 0
  while IFS=$'\t' read -r status group rel mode uid gid; do
    target_root="$(target_root_for "$group")"
    target="$target_root/$rel"
    backup="$BACKUP_DIR/files/$group/$rel"
    if [ "$status" = "present" ]; then
      atomic_install "$backup" "$target" "$mode" "$uid" "$gid"
    elif [ "$status" = "missing" ]; then
      sudo rm -f -- "$target"
    else
      echo "备份清单状态无效: $status" >&2
      return 2
    fi
  done < "$BACKUP_MANIFEST"
}

record_active_units() {
  : > "$UNIT_STATE_FILE"
  local unit state
  for unit in "${MANAGED_SERVICES[@]}" "${WRITER_TIMERS[@]}"; do
    state="$(unit_active_state "$unit")" || return
    case "$state" in
      active) printf '%s\n' "$unit" >> "$UNIT_STATE_FILE" ;;
      inactive) ;;
      *)
        echo "部署前 unit 状态不稳定，拒绝猜测恢复目标: $unit=$state" >&2
        return 2
        ;;
    esac
  done
}

was_active() {
  grep -Fx -- "$1" "$UNIT_STATE_FILE" >/dev/null 2>&1
}

restore_active_units() {
  local unit
  for unit in "${MANAGED_SERVICES[@]}"; do
    if was_active "$unit"; then
      sudo systemctl start "$unit"
      wait_unit_active "$unit" 15
    else
      stop_units_and_confirm "$unit"
    fi
  done
  for unit in "${WRITER_TIMERS[@]}"; do
    if was_active "$unit"; then
      sudo systemctl start "$unit"
      wait_unit_active "$unit" 15
    else
      stop_units_and_confirm "$unit"
    fi
  done
  if was_active "$WEBAPP_UNIT"; then
    assert_webapp_runtime_stable
  fi
  if was_active "$VOICE_RT_UNIT"; then
    assert_voice_runtime_stable
  fi
}

unit_active_state() {
  local state
  state="$(systemctl show --property=ActiveState --value "$1")" || {
    echo "无法读取 systemd ActiveState: $1" >&2
    return 2
  }
  case "$state" in
    active|activating|deactivating|reloading|inactive|failed)
      printf '%s\n' "$state"
      ;;
    *)
      echo "systemd ActiveState 不可证明: $1=${state:-<empty>}" >&2
      return 2
      ;;
  esac
}

wait_unit_still() {
  local unit="$1" attempts="${2:-$WRITER_WAIT_SECONDS}"
  local state i
  for ((i = 0; i <= attempts; i += 1)); do
    state="$(unit_active_state "$unit")" || return
    case "$state" in
      inactive|failed) return 0 ;;
    esac
    [ "$i" -lt "$attempts" ] && sleep 1
  done
  echo "systemd unit 未静止: $unit (ActiveState=$state)" >&2
  return 2
}

wait_unit_active() {
  local unit="$1" attempts="${2:-15}"
  local state i
  for ((i = 0; i <= attempts; i += 1)); do
    state="$(unit_active_state "$unit")" || return
    [ "$state" = "active" ] && return 0
    [ "$i" -lt "$attempts" ] && sleep 1
  done
  echo "systemd unit 未恢复 active: $unit (ActiveState=$state)" >&2
  return 2
}

confirm_units_still() {
  local unit
  for unit in "$@"; do
    wait_unit_still "$unit" 0 || return
  done
}

stop_units_and_confirm() {
  local unit
  [ "$#" -gt 0 ] || return 0
  sudo systemctl stop "$@"
  for unit in "$@"; do
    wait_unit_still "$unit" 15 || return
  done
}

freeze_writers() {
  # Never use `is-active` as a stopped predicate: systemd intentionally
  # reports a oneshot as "not active" while it may still be activating or
  # deactivating.  Only exact inactive/failed states are safe to cross.
  stop_units_and_confirm "${WRITER_TIMERS[@]}" || return
  stop_units_and_confirm "${MANAGED_SERVICES[@]}" || return

  # 不杀正在运行的 daily/quick/concept 事务。先阻止下一轮，再给当前轮
  # WRITER_WAIT_SECONDS 收尾；超时即在任何代码/指针回切前 fail closed。
  local unit
  for unit in "${WRITER_SERVICES[@]}"; do
    wait_unit_still "$unit" "$WRITER_WAIT_SECONDS" || {
      echo "KG writer 仍在运行，部署中止: $unit" >&2
      return 2
    }
  done
  confirm_units_still \
    "${WRITER_TIMERS[@]}" \
    "${WRITER_SERVICES[@]}" \
    "${MANAGED_SERVICES[@]}" || return
  WRITERS_FROZEN=1
}

hash_kg_state() {
  local output="$1"
  shift
  python3 -B - "$PROJECT_ROOT" "$output" "$@" <<'PY'
import hashlib
import json
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1])
out = Path(sys.argv[2])
paths = sys.argv[3:]
if not paths:
    raise SystemExit("KG 状态清单为空")
rows = {}
for relative in paths:
    path = root / relative
    if not path.exists() and not path.is_symlink():
        rows[relative] = None
        continue
    if path.is_symlink():
        raise SystemExit(f"KG 状态路径不得是符号链接: {relative}")
    if path.is_file():
        rows[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        continue
    if not path.is_dir():
        raise SystemExit(f"KG 状态路径类型无效: {relative}")
    nested = {}
    for child in sorted(path.rglob("*")):
        child_rel = child.relative_to(path).as_posix()
        mode = child.lstat().st_mode
        if stat.S_ISLNK(mode) or (not child.is_dir() and not child.is_file()):
            raise SystemExit(f"KG 状态目录含不安全条目: {relative}/{child_rel}")
        if child.is_file():
            nested[child_rel] = hashlib.sha256(child.read_bytes()).hexdigest()
    rows[relative] = nested
out.write_text(
    json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    + "\n",
    encoding="utf-8",
)
PY
}

verify_kg_state_change() {
  local before_inventory="$1" after_inventory="$2" snapshot_tar="$3"
  python3 -B - \
    "$PROJECT_ROOT" "$before_inventory" "$after_inventory" \
    "$snapshot_tar" "$KG_DWELL_REL" <<'PY'
import hashlib
import json
from pathlib import Path
import stat
import sys
import tarfile

root = Path(sys.argv[1])
before_path = Path(sys.argv[2])
after_path = Path(sys.argv[3])
snapshot_path = Path(sys.argv[4])
dwell_relative = sys.argv[5]
dwell_parent, dwell_name = dwell_relative.rsplit("/", 1)


def fail(message):
    raise SystemExit(message)


def load_inventory(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"KG 状态清单无效: {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"KG 状态清单顶层必须是对象: {path}")
    return value


def without_dwell(inventory):
    # Only the App-owned dwell stream may move during the health window.  An
    # absent state/attention directory and one containing only dwell.jsonl are
    # equivalent after that single file is removed; every other entry remains
    # byte-digest exact.
    cloned = dict(inventory)
    attention = cloned.get(dwell_parent)
    if isinstance(attention, dict):
        attention = dict(attention)
        attention.pop(dwell_name, None)
        cloned[dwell_parent] = attention or None
    return cloned


def snapshot_dwell_bytes(inventory):
    attention = inventory.get(dwell_parent)
    expected_digest = (
        attention.get(dwell_name) if isinstance(attention, dict) else None
    )
    matches = []
    try:
        with tarfile.open(snapshot_path, mode="r:") as archive:
            for member in archive.getmembers():
                if member.name.lstrip("./") == dwell_relative:
                    matches.append(member)
            if len(matches) > 1:
                fail(f"KG 状态快照含重复条目: {dwell_relative}")
            if not matches:
                if expected_digest is not None:
                    fail(f"KG 状态快照缺失条目: {dwell_relative}")
                return b""
            member = matches[0]
            if not member.isfile():
                fail(f"KG 状态快照条目不是普通文件: {dwell_relative}")
            stream = archive.extractfile(member)
            if stream is None:
                fail(f"KG 状态快照条目无法读取: {dwell_relative}")
            data = stream.read()
    except (OSError, tarfile.TarError) as exc:
        fail(f"KG 状态快照无法读取: {snapshot_path}: {exc}")
    actual_digest = hashlib.sha256(data).hexdigest()
    if expected_digest != actual_digest:
        fail(f"KG 状态快照与部署前清单不一致: {dwell_relative}")
    return data


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail(f"dwell JSON 含重复字段: {key}")
        value[key] = item
    return value


def reject_constant(value):
    fail(f"dwell JSON 含非标准数值: {value}")


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_record(line, line_number):
    try:
        record = json.loads(
            line,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"dwell JSONL 第 {line_number} 行无效: {exc}")
    if not isinstance(record, dict):
        fail(f"dwell JSONL 第 {line_number} 行必须是对象")
    required = {"ts", "secs", "file", "uid", "page"}
    allowed = required | {"upage"}
    fields = set(record)
    if fields != required and fields != allowed:
        fail(f"dwell JSONL 第 {line_number} 行字段与 read-dwell schema 不符")
    if not integer(record["ts"]) or not (0 <= record["ts"] <= 2**63 - 1):
        fail(f"dwell JSONL 第 {line_number} 行 ts 无效")
    if not integer(record["secs"]) or not (0 <= record["secs"] <= 600):
        fail(f"dwell JSONL 第 {line_number} 行 secs 无效")
    file_value = record["file"]
    if (
        not isinstance(file_value, str)
        or not file_value
        or file_value != file_value.strip()
        or "/.sandbox/" in file_value
    ):
        fail(f"dwell JSONL 第 {line_number} 行 file 无效")
    if not isinstance(record["uid"], str):
        fail(f"dwell JSONL 第 {line_number} 行 uid 无效")
    if not integer(record["page"]):
        fail(f"dwell JSONL 第 {line_number} 行 page 无效")
    if "upage" in record:
        upage = record["upage"]
        if not isinstance(upage, str) or not upage or len(upage) > 40:
            fail(f"dwell JSONL 第 {line_number} 行 upage 无效")
        if record["page"] != 0:
            fail(f"dwell JSONL 第 {line_number} 行虚拟页 page 必须为 0")


before = load_inventory(before_path)
after = load_inventory(after_path)
if without_dwell(before) != without_dwell(after):
    fail("部署/健康检查阶段意外写入 KG 状态（非 dwell append）")

snapshot_data = snapshot_dwell_bytes(before)
live_path = root / dwell_relative
if not live_path.exists() and not live_path.is_symlink():
    if snapshot_data:
        fail(f"部署窗口内删除了 KG 状态: {dwell_relative}")
    raise SystemExit(0)
mode = live_path.lstat().st_mode
if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
    fail(f"KG 状态路径类型无效: {dwell_relative}")
live_data = live_path.read_bytes()
if live_data == snapshot_data:
    raise SystemExit(0)
if len(live_data) <= len(snapshot_data) or not live_data.startswith(snapshot_data):
    fail(f"KG dwell 不是相对备份的严格字节前缀追加: {dwell_relative}")
suffix_bytes = live_data[len(snapshot_data):]
if not suffix_bytes.endswith(b"\n") or b"\r" in suffix_bytes:
    fail("KG dwell 追加后缀不是完整 LF JSONL")
try:
    suffix = suffix_bytes.decode("utf-8", errors="strict")
except UnicodeDecodeError as exc:
    fail(f"KG dwell 追加后缀不是完整 UTF-8: {exc}")
lines = suffix.split("\n")[:-1]
if not lines or any(not line for line in lines):
    fail("KG dwell 追加后缀含空行或没有 JSONL 记录")
for index, line in enumerate(lines, start=1):
    validate_record(line, index)
print(f"保留部署窗口 read-dwell 追加: {len(lines)} 条")
PY
}

restore_kg_pointer() {
  [ "$CURRENT_SWITCHED" = "1" ] || return 0
  local desired="${OLD_KG_ID:--}"
  python3 -B "$RELEASE_HELPER" switch \
    --runtime-root "$KG_RUNTIME_ROOT" \
    --release-id "$desired" \
    --expected "$NEW_KG_ID" >/dev/null
  CURRENT_SWITCHED=0
}

rollback_deploy() {
  local rollback_failed=0 after_state="$STAGE_DIR/kg-state-after-error.sha256"
  set +e
  # The pointer/files must not be moved again until every possible writer,
  # timer and managed process is *proven* still.  A failed stop or an
  # activating/deactivating oneshot leaves the marker and candidate in place
  # for manual recovery instead of performing an unprovable rollback.
  freeze_writers || rollback_failed=1
  if [ "$rollback_failed" = "0" ]; then
    confirm_units_still \
      "${WRITER_TIMERS[@]}" \
      "${WRITER_SERVICES[@]}" \
      "${MANAGED_SERVICES[@]}" || rollback_failed=1
  fi
  if [ "$rollback_failed" = "0" ]; then
    restore_kg_pointer || rollback_failed=1
  fi
  if [ "$rollback_failed" = "0" ]; then
    restore_backup || rollback_failed=1
  fi
  if [ "$rollback_failed" = "0" ]; then
    sudo systemctl daemon-reload || rollback_failed=1
  fi
  if [ "$rollback_failed" = "0" ]; then
    if [ -f "$KG_STATE_BEFORE" ]; then
      hash_kg_state "$after_state" \
        "${KG_MUTABLE_PATHS[@]}" "${KG_EXTERNAL_MUTABLE_PATHS[@]}" \
        || rollback_failed=1
      if [ -f "$after_state" ] \
          && ! verify_kg_state_change \
            "$KG_STATE_BEFORE" "$after_state" "$BACKUP_DIR/kg-state.tar"; then
        echo "❌ 部署窗口内 KG 数据已变化；拒绝盲目覆盖数据快照。" >&2
        rollback_failed=1
      fi
    fi
  fi
  if [ "$rollback_failed" = "0" ]; then
    restore_active_units || rollback_failed=1
  fi
  if [ "$rollback_failed" = "0" ]; then
    write_json_atomic "$BACKUP_DIR/result.json" "rolled_back" || rollback_failed=1
    sudo rm -f -- "$ACTIVE_MARKER"
  else
    write_json_atomic "$BACKUP_DIR/result.json" "rollback_blocked" || true
    echo "❌ 回滚未能被完整证明；保留 $ACTIVE_MARKER，服务/定时器保持冻结。" >&2
  fi
  set -e
  return "$rollback_failed"
}

voice_tcp_probe() {
  python3 -B - "$VOICE_RT_PORT" <<'PY'
import base64
import os
import socket
import sys

port = int(sys.argv[1])
key = base64.b64encode(os.urandom(16)).decode("ascii")
request = (
    "GET / HTTP/1.1\r\n"
    f"Host: 127.0.0.1:{port}\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\n"
    "Sec-WebSocket-Version: 13\r\n\r\n"
).encode("ascii")
with socket.create_connection(("127.0.0.1", port), timeout=3) as stream:
    stream.sendall(request)
    response = stream.recv(4096)
if not response.startswith(b"HTTP/1.1 101"):
    raise SystemExit("voice websocket handshake did not return 101")
PY
}

webapp_http_probe() {
  local code
  if ! code="$(
    curl -s -o /dev/null -w '%{http_code}' -m 8 \
      "http://127.0.0.1:${WEBAPP_PORT}/login"
  )"; then
    return 1
  fi
  [ "$code" = "200" ] || {
    echo "webapp /login=$code" >&2
    return 1
  }
}

wait_webapp_http() {
  local attempts="${1:-30}" i
  for ((i = 0; i <= attempts; i += 1)); do
    if webapp_http_probe; then
      return 0
    fi
    [ "$i" -lt "$attempts" ] && sleep 1
  done
  echo "webapp /login 在 ${attempts}s 内未就绪" >&2
  return 2
}

wait_voice_tcp() {
  local attempts="${1:-30}" i
  for ((i = 0; i <= attempts; i += 1)); do
    if voice_tcp_probe; then
      return 0
    fi
    [ "$i" -lt "$attempts" ] && sleep 1
  done
  echo "voice websocket 在 ${attempts}s 内未就绪" >&2
  return 2
}

assert_webapp_runtime_stable() {
  local pid_before pid_after restarts_before restarts_after
  wait_unit_active "$WEBAPP_UNIT" 15
  pid_before="$(
    systemctl show --property=MainPID --value "$WEBAPP_UNIT"
  )"
  restarts_before="$(
    systemctl show --property=NRestarts --value "$WEBAPP_UNIT"
  )"
  case "$pid_before" in
    ''|*[!0-9]*|0) echo "webapp MainPID 无效: $pid_before" >&2; return 2 ;;
  esac
  case "$restarts_before" in
    ''|*[!0-9]*) echo "webapp NRestarts 无效: $restarts_before" >&2; return 2 ;;
  esac
  wait_webapp_http 30
  sleep "$VOICE_STABILITY_SECONDS"
  wait_unit_active "$WEBAPP_UNIT" 0
  pid_after="$(
    systemctl show --property=MainPID --value "$WEBAPP_UNIT"
  )"
  restarts_after="$(
    systemctl show --property=NRestarts --value "$WEBAPP_UNIT"
  )"
  [ "$pid_before" = "$pid_after" ] \
    && [ "$restarts_before" = "$restarts_after" ] || {
      echo "webapp 在稳定窗口内重启: pid $pid_before->$pid_after," \
        "restarts $restarts_before->$restarts_after" >&2
      return 2
    }
  webapp_http_probe
}

assert_voice_runtime_stable() {
  local pid_before pid_after restarts_before restarts_after
  wait_unit_active "$VOICE_RT_UNIT" 15
  pid_before="$(
    systemctl show --property=MainPID --value "$VOICE_RT_UNIT"
  )"
  restarts_before="$(
    systemctl show --property=NRestarts --value "$VOICE_RT_UNIT"
  )"
  case "$pid_before" in
    ''|*[!0-9]*|0) echo "voice-rt MainPID 无效: $pid_before" >&2; return 2 ;;
  esac
  case "$restarts_before" in
    ''|*[!0-9]*) echo "voice-rt NRestarts 无效: $restarts_before" >&2; return 2 ;;
  esac
  wait_voice_tcp 30
  sleep "$VOICE_STABILITY_SECONDS"
  wait_unit_active "$VOICE_RT_UNIT" 0
  pid_after="$(
    systemctl show --property=MainPID --value "$VOICE_RT_UNIT"
  )"
  restarts_after="$(
    systemctl show --property=NRestarts --value "$VOICE_RT_UNIT"
  )"
  [ "$pid_before" = "$pid_after" ] \
    && [ "$restarts_before" = "$restarts_after" ] || {
      echo "voice-rt 在稳定窗口内重启: pid $pid_before->$pid_after," \
        "restarts $restarts_before->$restarts_after" >&2
      return 2
    }
  voice_tcp_probe
}

assert_deployed_python_health() {
  BW_READER_KG_RUNTIME_ROOT="$KG_RUNTIME_ROOT" \
  CLAUDE_PROJECT="$PROJECT_ROOT" \
  OBSIDIAN_VAULT="${OBSIDIAN_VAULT:-/home/bwicarus/Obsidian}" \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$WEBAPP_ROOT" \
  /usr/bin/python3 -B - "$NEW_KG_ID" <<'PY'
from pathlib import Path
import sys

expected = sys.argv[1]
import kg_runtime

release = kg_runtime.current_release()
if release.name != expected:
    raise SystemExit(
        f"production KG current mismatch: {release.name!r} != {expected!r}"
    )
for name in ("concept_node_service", "gen_page_brief"):
    module = kg_runtime.import_module(name)
    Path(module.__file__).resolve().relative_to(release)

# Exercise the actual lazy import seam used by POST /api/review-queue.  This
# constructs the deterministic index only; it does not call Anki or mutate it.
import pdf_reader
import card_candidate_service
service = pdf_reader._review_candidate_service()
if (
    card_candidate_service.CONTRACT != "card-candidate-service/1"
    or not isinstance(service, card_candidate_service.CardCandidateService)
):
    raise SystemExit("card candidate lazy service contract mismatch")
PY
}

on_error() {
  local error_exit=$?
  # `set -E` also propagates ERR into command substitutions.  A child shell
  # must only return its failure to the root shell; it must never perform a
  # second deployment rollback with a private copy of transaction state.
  if [ "$BASHPID" != "$ROOT_SHELL_BASHPID" ]; then
    return "$error_exit"
  fi
  ORIGINAL_EXIT="$error_exit"
  trap - ERR INT TERM HUP
  if [ "$DEPLOY_STARTED" = "1" ] && [ "$DEPLOY_FINISHED" != "1" ]; then
    echo "❌ 部署失败；按 $BACKUP_DIR 回滚。" >&2
    if ! rollback_deploy; then
      exit 70
    fi
  fi
  exit "$ORIGINAL_EXIT"
}

on_signal() {
  ORIGINAL_EXIT=130
  trap - ERR INT TERM HUP
  if [ "$DEPLOY_STARTED" = "1" ] && [ "$DEPLOY_FINISHED" != "1" ]; then
    echo "❌ 部署被中断；按 $BACKUP_DIR 回滚。" >&2
    if ! rollback_deploy; then
      exit 70
    fi
  fi
  exit "$ORIGINAL_EXIT"
}

trap cleanup_stage EXIT
trap on_error ERR
trap on_signal INT TERM HUP

# 实际部署从候选捕获开始就持有唯一锁。否则较早启动、较慢完成预检的
# 旧候选可能在另一次较新的部署完成后重新取得锁并合法回退 current。
# 纯 preflight 不创建生产目录或锁文件。
if [ "$PREFLIGHT_ONLY" != "1" ]; then
  mkdir -p -- "$KG_RUNTIME_ROOT"
  exec 9>"$DEPLOY_LOCK"
  flock -n 9 || {
    echo "已有 reader 部署事务在运行" >&2
    exit 2
  }
  if [ -e "$ACTIVE_MARKER" ] || [ -L "$ACTIVE_MARKER" ]; then
    echo "存在未清理部署事务: $ACTIVE_MARKER" >&2
    exit 2
  fi
fi

echo "── ① 构建临时 bundle 与完整发布预检"
mkdir -p "$CANDIDATE_ROOT/scripts"
for helper in scripts/reader_deploy_manifest.py scripts/reader_kg_release.py; do
  if [ ! -f "$PROJECT_ROOT/$helper" ] || [ -L "$PROJECT_ROOT/$helper" ]; then
    echo "发布辅助脚本不是普通文件: $helper" >&2
    exit 2
  fi
  install -D -m 0444 -- \
    "$PROJECT_ROOT/$helper" "$CANDIDATE_ROOT/$helper"
done
for validation_entry in scripts/deploy_reader.sh scripts/reader_e2e.py; do
  if [ ! -f "$PROJECT_ROOT/$validation_entry" ] \
      || [ -L "$PROJECT_ROOT/$validation_entry" ]; then
    echo "部署/验证入口不是普通文件: $validation_entry" >&2
    exit 2
  fi
  install -D -m 0444 -- \
    "$PROJECT_ROOT/$validation_entry" "$CANDIDATE_ROOT/$validation_entry"
done

# The first pass uses the already pinned helper against the checkout only to
# enumerate inputs.  Every enumerated byte is then copied to the private
# candidate root; the same helper is run again there and the manifests must
# match before the checkout is no longer consulted for release content.
BOOTSTRAP_MANIFEST="$STAGE_DIR/deploy-manifest.bootstrap.tsv"
run_manifest_helper "$PROJECT_ROOT" > "$BOOTSTRAP_MANIFEST"
[ -s "$BOOTSTRAP_MANIFEST" ]
while IFS=$'\t' read -r source_rel _target_group _target_rel _policy; do
  source_path="$PROJECT_ROOT/$source_rel"
  if [ ! -f "$source_path" ] || [ -L "$source_path" ]; then
    echo "候选源不是普通文件: $source_rel" >&2
    exit 2
  fi
  install -D -m 0444 -- "$source_path" "$CANDIDATE_ROOT/$source_rel"
done < "$BOOTSTRAP_MANIFEST"

if [ ! -f "$PROJECT_ROOT/extensions/bw-reader-webext/manifest.json" ] \
    || [ -L "$PROJECT_ROOT/extensions/bw-reader-webext/manifest.json" ]; then
  echo "扩展 manifest 不是普通文件" >&2
  exit 2
fi
install -D -m 0444 -- \
  "$PROJECT_ROOT/extensions/bw-reader-webext/manifest.json" \
  "$CANDIDATE_ROOT/extensions/bw-reader-webext/manifest.json"

READER_PARTS_REL=()
while IFS= read -r source_part; do
  relative_part="${source_part#"$PROJECT_ROOT/"}"
  if [ ! -f "$source_part" ] || [ -L "$source_part" ]; then
    echo "reader bundle 输入不是普通文件: $relative_part" >&2
    exit 2
  fi
  install -D -m 0444 -- "$source_part" "$CANDIDATE_ROOT/$relative_part"
  READER_PARTS_REL+=("$relative_part")
done < <(
  find "$PROJECT_ROOT/$SRC_REL" -maxdepth 1 -type f -name '*.js' -print \
    | LC_ALL=C sort
)
[ "${#READER_PARTS_REL[@]}" -gt 0 ]

run_manifest_helper "$CANDIDATE_ROOT" > "$DEPLOY_MANIFEST"
cmp -s "$BOOTSTRAP_MANIFEST" "$DEPLOY_MANIFEST" || {
  echo "候选快照中的部署清单与枚举清单不一致" >&2
  exit 2
}
CANDIDATE_DIGEST="$(hash_candidate_tree)"
[ -n "$CANDIDATE_DIGEST" ]
find "$CANDIDATE_ROOT" -type d -exec chmod 0555 {} +
find "$CANDIDATE_ROOT" -type f -exec chmod 0444 {} +
verify_candidate_digest

MANIFEST_COUNT="$(wc -l < "$DEPLOY_MANIFEST")"
READER_VERSION="$(
  python3 -B - "$CANDIDATE_ROOT/extensions/bw-reader-webext/manifest.json" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])
PY
)"
mapfile -t READER_PARTS < <(
  find "$CANDIDATE_ROOT/$SRC_REL" \
    -maxdepth 1 -type f -name '*.js' -print | LC_ALL=C sort
)
[ "${#READER_PARTS[@]}" -gt 0 ]
mkdir -p "$STAGE_DIR/generated"
cat "${READER_PARTS[@]}" > "$STAGE_DIR/generated/reader.js"
node --check "$STAGE_DIR/generated/reader.js"

GIT_REV="$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
GIT_STATE="clean"
if [ -n "$(git status --porcelain -- "$SRC_REL" "$OUT_REL")" ]; then
  GIT_STATE="dirty"
fi
ARTIFACT_STAMP="${GIT_REV}+${GIT_STATE}·$(date +%m%d-%H%M)"

PYTHON_STAGE_FILES=()
MANIFEST_SOURCES=()
while IFS=$'\t' read -r source_rel target_group target_rel policy; do
  stage_target="$STAGE_DIR/$target_group/$target_rel"
  mkdir -p -- "$(dirname "$stage_target")"
  stage_source="$CANDIDATE_ROOT/$source_rel"
  if [ "$source_rel" = "$OUT_REL" ]; then
    stage_source="$STAGE_DIR/generated/reader.js"
  fi
  case "$policy" in
    exact)
      install -m 0644 -- "$stage_source" "$stage_target"
      ;;
    reader_git_stamp)
      {
        cat "$stage_source"
        printf "\n;window.__READER_GIT='%s';\n" "$ARTIFACT_STAMP"
      } > "$stage_target"
      ;;
    *)
      echo "清单含未知部署策略: $policy" >&2
      exit 2
      ;;
  esac
  MANIFEST_SOURCES+=("$source_rel")
  case "$stage_target" in
    *.py) PYTHON_STAGE_FILES+=("$stage_target") ;;
    *.js) node --check "$stage_target" ;;
  esac
done < "$DEPLOY_MANIFEST"

python3 -B - "${PYTHON_STAGE_FILES[@]}" <<'PY'
import ast
import pathlib
import sys
for raw in sys.argv[1:]:
    path = pathlib.Path(raw)
    ast.parse(path.read_text("utf-8"), filename=str(path))
PY
python3 -B "$RELEASE_HELPER" prepare \
  --stage "$STAGE_DIR/kg_runtime" \
  --reader-version "$READER_VERSION" > "$STAGE_DIR/kg-runtime.json"
NEW_KG_ID="$(
  python3 -B - "$STAGE_DIR/kg-runtime.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["deployId"])
PY
)"

# 从这一刻起，实际会安装/发布的派生 payload 也被冻结并单独摘要。
# candidateDigest 证明输入源码；payloadDigest 证明 reader 拼接/stamp 后的
# 最终安装字节。后续任何同用户进程改写临时 stage 都会在生产写前失败。
for payload_dir in \
  "$STAGE_DIR/webapp" "$STAGE_DIR/static" \
  "$STAGE_DIR/systemd" "$STAGE_DIR/kg_runtime"; do
  find "$payload_dir" -type d -exec chmod 0555 {} +
  find "$payload_dir" -type f -exec chmod 0444 {} +
done
PAYLOAD_DIGEST="$(hash_deploy_payload)"
[ -n "$PAYLOAD_DIGEST" ]
verify_deploy_payload_digest

# 用临时 current 验证 resolver、完整摘要及关键入口的代码来源；数据根也是临时目录。
PROBE_RUNTIME="$STAGE_DIR/probe-runtime"
mkdir -p "$STAGE_DIR/probe-data/state/attention" "$STAGE_DIR/probe-vault"
python3 -B "$RELEASE_HELPER" publish \
  --stage "$STAGE_DIR/kg_runtime" \
  --runtime-root "$PROBE_RUNTIME" >/dev/null
python3 -B "$RELEASE_HELPER" switch \
  --runtime-root "$PROBE_RUNTIME" \
  --release-id "$NEW_KG_ID" \
  --expected - >/dev/null
BW_READER_KG_RUNTIME_ROOT="$PROBE_RUNTIME" \
CLAUDE_PROJECT="$STAGE_DIR/probe-data" \
OBSIDIAN_VAULT="$STAGE_DIR/probe-vault" \
PYTHONDONTWRITEBYTECODE=1 \
python3 -B - "$STAGE_DIR/webapp" "$PROBE_RUNTIME/releases/$NEW_KG_ID" <<'PY'
from pathlib import Path
import sys
webapp, release = map(Path, sys.argv[1:])
sys.path.insert(0, str(webapp))
import kg_runtime
for name in (
    "concept_node_service",
    "gen_page_brief",
    "promote_concepts",
    "propose_concept_notes",
    "audit_edges",
    "build_unified_graph",
    "mastery_overrides",
):
    module = kg_runtime.import_module(name)
    Path(module.__file__).resolve().relative_to(release.resolve())
assert kg_runtime.current_release() == release.resolve()
PY
mkdir -p "$STAGE_DIR/probe-webapp-data"
BW_READER_KG_RUNTIME_ROOT="$PROBE_RUNTIME" \
CLAUDE_PROJECT="$STAGE_DIR/probe-data" \
OBSIDIAN_VAULT="$STAGE_DIR/probe-vault" \
WEBAPP_DATA="$STAGE_DIR/probe-webapp-data" \
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="$STAGE_DIR/webapp" \
/usr/bin/python3 -B - <<'PY'
import card_candidate_service
import pdf_reader

service = pdf_reader._review_candidate_service()
if (
    card_candidate_service.CONTRACT != "card-candidate-service/1"
    or not isinstance(service, card_candidate_service.CardCandidateService)
):
    raise SystemExit("staged card candidate lazy service contract mismatch")
PY
BW_READER_KG_RUNTIME_ROOT="$PROBE_RUNTIME" \
CLAUDE_PROJECT="$STAGE_DIR/probe-data" \
OBSIDIAN_VAULT="$STAGE_DIR/probe-vault" \
PYTHONDONTWRITEBYTECODE=1 \
python3 -B \
  "$PROBE_RUNTIME/releases/$NEW_KG_ID/scripts/concept_graph_daily.py" \
  --gate-only

# 多数合同测试仍需要完整仓库布局，因此运行在工作树；在运行前后同时
# 固定测试/夹具摘要，并逐文件证明所有部署输入仍与私有候选相等。
# 这样并行编辑只会让部署失败，不会出现“测试新字节、安装旧字节”。
VALIDATION_DIGEST="$(hash_validation_inputs)"
[ -n "$VALIDATION_DIGEST" ]
verify_checkout_inputs_match_candidate
python3 -B scripts/audit_reader_network.py --check
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
  tests.test_reader_kg_release \
  tests.test_kg_runtime_release \
  tests.test_kg_runtime_orchestrators \
  tests.test_kg_runtime_launchers \
  tests.test_reader_deploy_manifest \
  tests.test_reader_deploy_transaction \
  tests.test_reader_deploy_e2e_readonly \
  tests.test_reader_asset_proxy \
  tests.test_pwa_web_reader_retirement \
  tests.test_vbook_route_policy \
  tests.test_web_translate_upgrade
python3 -B scripts/vocab/test_batch_protocol.py
node --test \
  tests/reader_contract/book-extension-handoff.contract.test.mjs \
  tests/reader_contract/reader-service-worker.contract.test.mjs
systemd-analyze verify \
  "$STAGE_DIR/systemd/voice-rt.service" \
  "$STAGE_DIR/systemd/bwicarus-quick-sync.service" \
  "$STAGE_DIR/systemd/bwicarus-quick-sync.timer" \
  "$STAGE_DIR/systemd/bwicarus-daily.service" \
  "$STAGE_DIR/systemd/bwicarus-daily.timer" \
  "$STAGE_DIR/systemd/concept-graph.service" \
  "$STAGE_DIR/systemd/concept-graph.timer"
verify_validation_digest
verify_checkout_inputs_match_candidate
verify_candidate_digest
verify_deploy_payload_digest

POST_OUT_SHA="$(
  if [ -f "$OUT" ]; then sha256sum "$OUT" | awk '{print $1}'; else echo missing; fi
)"
POST_OUT_STAT="$(
  if [ -f "$OUT" ]; then stat -c '%s:%Y:%i' "$OUT"; else echo missing; fi
)"
[ "$PRE_OUT_SHA" = "$POST_OUT_SHA" ] && [ "$PRE_OUT_STAT" = "$POST_OUT_STAT" ] || {
  echo "预检意外改写了工作树 reader.js" >&2
  exit 2
}
echo "   reader.js(stage) $(wc -c < "$STAGE_DIR/generated/reader.js")B；清单 ${MANIFEST_COUNT} 项；KG $NEW_KG_ID ✓"

if [ "$PREFLIGHT_ONLY" = "1" ]; then
  echo "✅ 无副作用预检通过；未创建生产备份、release 或 current。"
  exit 0
fi

echo "── ② 在部署锁内备份生产文件与 KG 状态"
verify_candidate_digest
verify_deploy_payload_digest
verify_validation_digest
verify_checkout_inputs_match_candidate
OLD_KG_ID="$(
  python3 -B "$RELEASE_HELPER" current \
    --runtime-root "$KG_RUNTIME_ROOT" \
  | python3 -B -c 'import json,sys; print(json.load(sys.stdin)["deployId"] or "")'
)"
mkdir -p -- "$BACKUP_DIR"
: > "$BACKUP_MANIFEST"
record_active_units
while IFS=$'\t' read -r _source_rel target_group target_rel _policy; do
  [ "$target_group" = "kg_runtime" ] && continue
  target_root="$(target_root_for "$target_group")"
  backup_target "$target_root/$target_rel" "$target_group" "$target_rel"
done < "$DEPLOY_MANIFEST"
write_json_atomic "$ACTIVE_MARKER" "prepared"
DEPLOY_STARTED=1

# 发布 release 但暂不激活；同 ID 已存在时必须逐字验证并幂等复用。
verify_candidate_digest
verify_deploy_payload_digest
python3 -B "$RELEASE_HELPER" publish \
  --stage "$STAGE_DIR/kg_runtime" \
  --runtime-root "$KG_RUNTIME_ROOT" >/dev/null

freeze_writers
hash_kg_state "$KG_STATE_BEFORE" \
  "${KG_MUTABLE_PATHS[@]}" "${KG_EXTERNAL_MUTABLE_PATHS[@]}"
KG_STATE_PRESENT=()
for state_path in "${KG_MUTABLE_PATHS[@]}"; do
  if [ -e "$PROJECT_ROOT/$state_path" ] \
      || [ -L "$PROJECT_ROOT/$state_path" ]; then
    KG_STATE_PRESENT+=("$state_path")
  fi
done
if [ "${#KG_STATE_PRESENT[@]}" -gt 0 ]; then
  tar -C "$PROJECT_ROOT" -cpf "$BACKUP_DIR/kg-state.tar" \
    "${KG_STATE_PRESENT[@]}"
else
  tar -C "$PROJECT_ROOT" -cpf "$BACKUP_DIR/kg-state.tar" --files-from /dev/null
fi
KG_VAULT_CONCEPT_REL="资源/概念"
if [ -e "$OBSIDIAN_VAULT_ROOT/$KG_VAULT_CONCEPT_REL" ] \
    || [ -L "$OBSIDIAN_VAULT_ROOT/$KG_VAULT_CONCEPT_REL" ]; then
  tar -C "$OBSIDIAN_VAULT_ROOT" \
    -cpf "$BACKUP_DIR/kg-vault-concepts.tar" "$KG_VAULT_CONCEPT_REL"
else
  tar -C "$PROJECT_ROOT" \
    -cpf "$BACKUP_DIR/kg-vault-concepts.tar" --files-from /dev/null
fi
write_json_atomic "$ACTIVE_MARKER" "writers_frozen"

echo "── ③ 原子安装普通文件并切换 KG current"
verify_candidate_digest
verify_deploy_payload_digest
verify_validation_digest
verify_checkout_inputs_match_candidate
BW_UID="$(id -u bwicarus)"
BW_GID="$(id -g bwicarus)"
while IFS=$'\t' read -r _source_rel target_group target_rel _policy; do
  [ "$target_group" = "kg_runtime" ] && continue
  target_root="$(target_root_for "$target_group")"
  if [ "$target_group" = "systemd" ]; then
    owner_uid=0
    owner_gid=0
  else
    owner_uid="$BW_UID"
    owner_gid="$BW_GID"
  fi
  atomic_install \
    "$STAGE_DIR/$target_group/$target_rel" \
    "$target_root/$target_rel" \
    0644 "$owner_uid" "$owner_gid"
done < "$DEPLOY_MANIFEST"
sudo systemctl daemon-reload

EXPECTED_OLD="${OLD_KG_ID:--}"
python3 -B "$RELEASE_HELPER" switch \
  --runtime-root "$KG_RUNTIME_ROOT" \
  --release-id "$NEW_KG_ID" \
  --expected "$EXPECTED_OLD" >/dev/null
CURRENT_SWITCHED=1
write_json_atomic "$ACTIVE_MARKER" "current_switched"

while IFS=$'\t' read -r _source_rel target_group target_rel _policy; do
  [ "$target_group" = "kg_runtime" ] && continue
  target_root="$(target_root_for "$target_group")"
  cmp -s \
    "$STAGE_DIR/$target_group/$target_rel" \
    "$target_root/$target_rel"
done < "$DEPLOY_MANIFEST"
python3 -B "$RELEASE_HELPER" verify \
  --runtime-root "$KG_RUNTIME_ROOT" \
  --release-id "$NEW_KG_ID" >/dev/null
verify_deploy_payload_digest

sudo systemctl start "$WEBAPP_UNIT"
sudo systemctl start "$VOICE_RT_UNIT"
assert_webapp_runtime_stable
assert_voice_runtime_stable
systemctl cat "$VOICE_RT_UNIT" \
  | grep -F -- "/home/bwicarus/webapp/voice_realtime_relay.py" >/dev/null
systemctl cat bwicarus-quick-sync.service \
  | grep -F -- "$KG_RUNTIME_ROOT/current/scripts/quick_sync.py" >/dev/null
systemctl cat concept-graph.service \
  | grep -F -- "$KG_RUNTIME_ROOT/current/scripts/concept_graph_daily.py" >/dev/null
assert_deployed_python_health

if [ "$RUN_E2E" = "1" ]; then
  echo "── ④ E2E 冒烟"
  verify_validation_digest
  verify_checkout_inputs_match_candidate
  verify_candidate_digest
  verify_deploy_payload_digest
  E2E_CONFIG_DIGEST="$(hash_e2e_config)"
  [ -n "$E2E_CONFIG_DIGEST" ]
  verify_e2e_config_digest
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env" 2>/dev/null || true
  set +a
  python3 -B "$CANDIDATE_ROOT/scripts/reader_e2e.py"
  verify_e2e_config_digest
  verify_validation_digest
  verify_checkout_inputs_match_candidate
  verify_candidate_digest
  verify_deploy_payload_digest
fi

verify_validation_digest
verify_checkout_inputs_match_candidate
verify_candidate_digest
verify_deploy_payload_digest
KG_STATE_AFTER="$STAGE_DIR/kg-state-after.sha256"
hash_kg_state "$KG_STATE_AFTER" \
  "${KG_MUTABLE_PATHS[@]}" "${KG_EXTERNAL_MUTABLE_PATHS[@]}"
verify_kg_state_change \
  "$KG_STATE_BEFORE" "$KG_STATE_AFTER" "$BACKUP_DIR/kg-state.tar" || {
  echo "部署/健康检查阶段意外写入 KG 状态" >&2
  false
}

restore_active_units
write_json_atomic "$BACKUP_DIR/result.json" "complete"
sudo rm -f -- "$ACTIVE_MARKER"
DEPLOY_FINISHED=1

if [ "$SYNC_PC" = "1" ]; then
  echo "── ⑤ 同步 Windows 开发仓库（部署成功后的可选动作）"
  if tar -C "$CANDIDATE_ROOT" -cf - \
      "${MANIFEST_SOURCES[@]}" "${READER_PARTS_REL[@]}" \
      | ssh -o ConnectTimeout=10 bwicarus@100.99.9.124 \
        'tar -C C:/claude -xf -'; then
    echo "   Windows 开发仓库 ✓"
  else
    echo "   ⚠ Windows 离线/同步失败；生产部署不回滚" >&2
  fi
fi

echo "✅ 部署完成：reader=$READER_VERSION kg=$NEW_KG_ID"
echo "   普通文件回滚备份与 KG 状态取证快照: $BACKUP_DIR"

#!/usr/bin/env python3
"""Audit browser-side reader network calls against interaction-policy/1.

The first version is intentionally conservative: existing unclassified calls
live in an exact, count-based baseline, while any new call shape fails
``--check``.  Retired allowances must be removed from that baseline in the
same change, so an old call cannot silently return later.  Moving a call
between lines does not create debt; changing its file, callee, or normalized
expression does.

This static layer recognizes direct fetch/bridge/stream calls.  Transport
aliases such as ``const F = window.fetch; F(...)`` and XMLHttpRequest belong
to the delayed/offline browser-instrumentation layer; this scanner must not be
treated as proof that no network access occurred.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = (
    ROOT / "_server_deploy/static/reader-runtime/interaction-policy.js"
)
DEFAULT_BASELINE = ROOT / "scripts/reader_network_audit_baseline.json"
DEFAULT_SCAN_TARGETS = (
    "_server_deploy/static/pdf",
    "_server_deploy/static/reader-runtime",
    # Only the product's PWA reader/library surfaces belong in this gate.
    # Scanning the whole templates tree makes unrelated control, fitness and
    # skill-tree work break a reader release.
    "_server_deploy/templates/pdf_reader.html",
    "_server_deploy/templates/epub_html_reader.html",
    "_server_deploy/templates/html_reader.html",
    "_server_deploy/templates/pdf_index.html",
    "extensions/bw-reader-webext/src",
    "extensions/bw-reader-webext/background.js",
    "extensions/bw-reader-webext/content.js",
    "extensions/bw-reader-webext/popup.js",
)
SKIP_DIRS = {
    "vendor",
    "node_modules",
    "dist",
    "build",
    "windows",
    "__pycache__",
}
NETWORK_CALL_RE = re.compile(
    # Do not recognize the ``fetch`` suffix of an unrelated identifier such
    # as ``prefetch``.  Keep the common global-qualified form in the captured
    # callee so fingerprints and diagnostics remain truthful.
    r"(?<![A-Za-z0-9_$])(?P<callee>"
    r"RC\s*\.\s*reqJson|"
    r"__bwReaderFetch|"
    r"bwFetch|"
    r"(?:navigator\s*\.\s*)?sendBeacon|"
    r"(?:new\s+)?EventSource|"
    r"(?:new\s+)?WebSocket|"
    r"(?:(?:window|globalThis|self|root)\s*\.\s*)?fetch"
    r")\s*\("
)
ANNOTATION_RE = re.compile(r"@interaction\s+([a-z][a-z0-9.-]+)")
METHOD_RE = re.compile(
    r"\bmethod\s*:\s*(['\"`])(?P<method>[A-Za-z]+)\1",
    re.DOTALL,
)
@dataclass(frozen=True)
class CallSite:
    path: str
    line: int
    callee: str
    expression: str
    url: str | None
    method: str
    annotation: str | None


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    line: int
    callee: str
    expression: str
    detail: str

    @property
    def fingerprint(self) -> str:
        # Line numbers deliberately do not participate: harmless line movement
        # must not turn accepted legacy debt into a false "new call".
        return "|".join(
            (
                self.code,
                self.path,
                normalize_expression(self.callee),
                normalize_expression(self.expression),
                normalize_expression(self.detail),
            )
        )


def normalize_expression(value: str, limit: int = 420) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _masked_javascript(text: str) -> str:
    """Mask comments and string contents while preserving offsets/newlines."""

    out = list(text)
    state = "code"
    quote = ""
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                out[index] = out[index + 1] = " "
                index += 2
                state = "line-comment"
                continue
            if char == "/" and nxt == "*":
                out[index] = out[index + 1] = " "
                index += 2
                state = "block-comment"
                continue
            if char in ("'", '"', "`"):
                quote = char
                state = "string"
                index += 1
                continue
            index += 1
            continue
        if state == "line-comment":
            if char == "\n":
                state = "code"
            else:
                out[index] = " "
            index += 1
            continue
        if state == "block-comment":
            if char == "*" and nxt == "/":
                out[index] = out[index + 1] = " "
                index += 2
                state = "code"
            else:
                if char != "\n":
                    out[index] = " "
                index += 1
            continue
        if state == "string":
            if char == "\n":
                # JavaScript template literals may contain newlines; preserving
                # them keeps line numbers exact.
                index += 1
                escaped = False
                continue
            if escaped:
                out[index] = " "
                escaped = False
                index += 1
                continue
            if char == "\\":
                out[index] = " "
                escaped = True
                index += 1
                continue
            if char == quote:
                state = "code"
                quote = ""
                index += 1
                continue
            out[index] = " "
            index += 1
    return "".join(out)


def _split_call_arguments(text: str, open_paren: int) -> tuple[list[str], int]:
    """Return top-level call arguments and the closing-paren offset."""

    args: list[str] = []
    start = open_paren + 1
    index = start
    stack = ["("]
    state = "code"
    quote = ""
    escaped = False
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "line-comment":
            if char == "\n":
                state = "code"
            index += 1
            continue
        if state == "block-comment":
            if char == "*" and nxt == "/":
                index += 2
                state = "code"
            else:
                index += 1
            continue
        if state == "string":
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                state = "code"
                quote = ""
            index += 1
            continue
        if char == "/" and nxt == "/":
            index += 2
            state = "line-comment"
            continue
        if char == "/" and nxt == "*":
            index += 2
            state = "block-comment"
            continue
        if char in ("'", '"', "`"):
            quote = char
            state = "string"
            index += 1
            continue
        if char in "([{":
            stack.append(char)
            index += 1
            continue
        if char in ")]}":
            expected = {")": "(", "]": "[", "}": "{"}[char]
            if not stack or stack[-1] != expected:
                return [], min(len(text), open_paren + 1)
            stack.pop()
            if not stack:
                tail = text[start:index].strip()
                if tail or args:
                    args.append(tail)
                return args, index + 1
            index += 1
            continue
        if char == "," and len(stack) == 1:
            args.append(text[start:index].strip())
            start = index + 1
        index += 1
    return [], min(len(text), open_paren + 1)


def _literal_string(expression: str) -> str | None:
    value = expression.lstrip()
    if not value or value[0] not in ("'", '"', "`"):
        return None
    quote = value[0]
    escaped = False
    chars: list[str] = []
    for index in range(1, len(value)):
        char = value[index]
        if escaped:
            # Endpoint paths only need the ordinary JS escapes. Keeping an
            # unknown escaped character is safer than interpreting code.
            chars.append(
                {"n": "\n", "r": "\r", "t": "\t"}.get(char, char)
            )
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote:
            literal = "".join(chars)
            if quote == "`" and "${" in literal:
                return None
            # ``'/prefix/' + id`` is a dynamic expression, not a literal URL.
            # Treating only its first string token as the URL makes a correct
            # @interaction annotation look mismatched and can also classify
            # the wrong endpoint.
            if value[index + 1 :].strip():
                return None
            return literal
        chars.append(char)
    return None


def _nearby_annotation(
    lines: Sequence[str],
    line_index: int,
    used: set[tuple[int, int]],
) -> str | None:
    """Bind one nearby, preceding annotation to at most one network call."""

    candidates: list[tuple[int, int, int, str]] = []
    # Never look below the call: a comment there belongs to a later call.
    for current in range(max(0, line_index - 3), line_index + 1):
        for ordinal, found in enumerate(ANNOTATION_RE.findall(lines[current])):
            key = (current, ordinal)
            if key in used:
                continue
            candidates.append((line_index - current, current, ordinal, found))
    if not candidates:
        return None
    _, current, ordinal, found = sorted(candidates, key=lambda item: item[:3])[0]
    used.add((current, ordinal))
    return found


def _method_for(callee: str, args: Sequence[str]) -> str:
    compact = re.sub(r"\s+", "", callee)
    if compact == "RC.reqJson":
        method = _literal_string(args[0]) if args else None
        return (method or "UNKNOWN").upper()
    if compact.endswith("sendBeacon"):
        return "POST"
    if compact.endswith("EventSource") or compact.endswith("WebSocket"):
        return "STREAM"
    if len(args) < 2 or not args[1].strip():
        return "GET"
    found = METHOD_RE.search(args[1])
    if found:
        return found.group("method").upper()
    if args[1].lstrip().startswith("{"):
        return "GET"
    return "UNKNOWN"


def find_calls(path: Path, relative_path: str) -> list[CallSite]:
    text = path.read_text(encoding="utf-8", errors="replace")
    masked = _masked_javascript(text)
    lines = text.splitlines()
    calls: list[CallSite] = []
    used_annotations: set[tuple[int, int]] = set()
    for found in NETWORK_CALL_RE.finditer(masked):
        open_paren = masked.find("(", found.start(), found.end() + 1)
        if open_paren < 0:
            continue
        args, end = _split_call_arguments(text, open_paren)
        if end <= open_paren + 1:
            continue
        callee = normalize_expression(text[found.start():open_paren])
        compact = re.sub(r"\s+", "", callee)
        url_index = 1 if compact == "RC.reqJson" else 0
        url = _literal_string(args[url_index]) if len(args) > url_index else None
        line_index = text.count("\n", 0, found.start())
        expression = text[found.start():end]
        calls.append(
            CallSite(
                path=relative_path,
                line=line_index + 1,
                callee=callee,
                expression=expression,
                url=url,
                method=_method_for(callee, args),
                annotation=_nearby_annotation(lines, line_index, used_annotations),
            )
        )
    return calls


def should_scan(path: Path) -> bool:
    if path.suffix.lower() not in {".js", ".mjs", ".html"}:
        return False
    if path.name == "reader.js" or path.name.endswith(".min.js"):
        return False
    return not any(part in SKIP_DIRS for part in path.parts)


def discover_files(root: Path, targets: Sequence[str] = DEFAULT_SCAN_TARGETS) -> list[Path]:
    files: set[Path] = set()
    for target in targets:
        path = (root / target).resolve()
        if not path.exists():
            continue
        if path.is_file():
            if should_scan(path):
                files.add(path)
            continue
        files.update(item for item in path.rglob("*") if item.is_file() and should_scan(item))
    return sorted(files)


def load_policies(policy_path: Path) -> list[dict]:
    script = """
const policyPath = process.argv[1];
const api = require(policyPath);
const checked = api.validate();
if (!checked || checked.ok !== true) {
  process.stderr.write(JSON.stringify(checked || {ok:false}) + "\\n");
  process.exit(2);
}
process.stdout.write(JSON.stringify(api.policies()));
"""
    result = subprocess.run(
        ["node", "-e", script, str(policy_path.resolve())],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "cannot load interaction policy: "
            + (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
        )
    policies = json.loads(result.stdout)
    if not isinstance(policies, list):
        raise RuntimeError("interaction policy did not export a list")
    return policies


def _path_matches(pattern: str, path: str) -> bool:
    path = path.split("?", 1)[0].split("#", 1)[0]
    if "://" in path:
        match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]+(?P<path>/.*)?$", path)
        path = (match.group("path") if match else None) or "/"
    pieces: list[str] = []
    index = 0
    for found in re.finditer(r"\{[A-Za-z][A-Za-z0-9_]*\}|\*$", pattern):
        pieces.append(re.escape(pattern[index:found.start()]))
        pieces.append(".*" if found.group(0) == "*" else "[^/]+")
        index = found.end()
    pieces.append(re.escape(pattern[index:]))
    return re.fullmatch("".join(pieces), path) is not None


def policy_for(policies: Sequence[dict], url: str, method: str) -> dict | None:
    method = str(method or "").upper()
    for policy in policies:
        for item in policy.get("matches") or ():
            methods = {str(value).upper() for value in item.get("methods") or ()}
            if method in methods and _path_matches(str(item.get("path") or ""), url):
                return policy
    return None


def audit_calls(calls: Iterable[CallSite], policies: Sequence[dict]) -> list[Issue]:
    by_id = {str(policy.get("id") or ""): policy for policy in policies}
    issues: list[Issue] = []
    for call in calls:
        annotated = by_id.get(call.annotation or "")
        matched = (
            policy_for(policies, call.url, call.method)
            if call.url is not None and call.method != "UNKNOWN"
            else None
        )
        if call.annotation and not annotated:
            issues.append(
                Issue(
                    "annotation-unknown",
                    call.path,
                    call.line,
                    call.callee,
                    call.expression,
                    f"unknown @{call.annotation}",
                )
            )
            continue
        if call.annotation and call.url is not None and call.method != "UNKNOWN":
            if not matched or matched.get("id") != call.annotation:
                actual = matched.get("id") if matched else "unclassified"
                issues.append(
                    Issue(
                        "annotation-mismatch",
                        call.path,
                        call.line,
                        call.callee,
                        call.expression,
                        f"@{call.annotation} does not match {call.method} {call.url} ({actual})",
                    )
                )
                continue
        if call.url is None or call.method == "UNKNOWN":
            if not call.annotation:
                issues.append(
                    Issue(
                        "dynamic-unannotated",
                        call.path,
                        call.line,
                        call.callee,
                        call.expression,
                        "dynamic endpoint/method needs @interaction <id>",
                    )
                )
            continue
        if not matched:
            issues.append(
                Issue(
                    "unclassified-literal",
                    call.path,
                    call.line,
                    call.callee,
                    call.expression,
                    f"{call.method} {call.url}",
                )
            )
            continue
        if matched.get("ui") == "local-immediate" and not call.annotation:
            issues.append(
                Issue(
                    "local-immediate-unannotated",
                    call.path,
                    call.line,
                    call.callee,
                    call.expression,
                    f"{matched.get('id')} must prove local effect before network",
                )
            )
            continue
        if matched.get("ui") == "cache-first" and not call.annotation:
            issues.append(
                Issue(
                    "cache-first-unannotated",
                    call.path,
                    call.line,
                    call.callee,
                    call.expression,
                    f"{matched.get('id')} must prove cached/local read before network",
                )
            )
    return issues


def audit_paths(
    root: Path,
    policies: Sequence[dict],
    targets: Sequence[str] = DEFAULT_SCAN_TARGETS,
) -> tuple[list[CallSite], list[Issue], list[Path]]:
    files = discover_files(root, targets)
    calls: list[CallSite] = []
    for path in files:
        try:
            relative = path.relative_to(root.resolve()).as_posix()
        except ValueError:
            relative = path.as_posix()
        calls.extend(find_calls(path, relative))
    return calls, audit_calls(calls, policies), files


def issue_counts(issues: Iterable[Issue]) -> Counter[str]:
    return Counter(issue.fingerprint for issue in issues)


def baseline_payload(issues: Sequence[Issue]) -> dict:
    counts = issue_counts(issues)
    grouped = Counter(issue.code for issue in issues)
    return {
        "schema": 1,
        "contract": "reader-network-audit-baseline/1",
        "note": (
            "Existing debt only. Retired allowances must be removed in the "
            "same change; new fingerprints or higher counts fail --check."
        ),
        "summary": dict(sorted(grouped.items())),
        "allowances": dict(sorted(counts.items())),
    }


def read_baseline(path: Path) -> Counter[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"baseline not found: {path}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid baseline JSON: {path}: {error}") from error
    if data.get("schema") != 1 or not isinstance(data.get("allowances"), dict):
        raise RuntimeError(f"unsupported baseline schema: {path}")
    counts: Counter[str] = Counter()
    for key, value in data["allowances"].items():
        count = int(value)
        if count < 0:
            raise RuntimeError(f"negative baseline allowance: {key}")
        counts[str(key)] = count
    return counts


def compare_baseline(
    issues: Sequence[Issue],
    baseline: Counter[str],
) -> tuple[list[Issue], int]:
    current = issue_counts(issues)
    seen: Counter[str] = Counter()
    new_issues: list[Issue] = []
    for issue in issues:
        fingerprint = issue.fingerprint
        seen[fingerprint] += 1
        if seen[fingerprint] > baseline.get(fingerprint, 0):
            new_issues.append(issue)
    retired = sum(max(0, count - current.get(key, 0)) for key, count in baseline.items())
    return new_issues, retired


def report(
    calls: Sequence[CallSite],
    issues: Sequence[Issue],
    files: Sequence[Path],
    *,
    new_issues: Sequence[Issue] = (),
    retired: int = 0,
) -> dict:
    classified = len(calls) - sum(
        1
        for issue in issues
        if issue.code in {"dynamic-unannotated", "unclassified-literal"}
    )
    return {
        "files": len(files),
        "calls": len(calls),
        "classifiedCalls": max(0, classified),
        "debt": len(issues),
        "debtByCode": dict(sorted(Counter(issue.code for issue in issues).items())),
        "newDebt": len(new_issues),
        "retiredBaselineEntries": retired,
    }


def _print_human(summary: dict, new_issues: Sequence[Issue]) -> None:
    print(
        "reader network audit: "
        f"{summary['files']} files, {summary['calls']} calls, "
        f"{summary['debt']} baseline debt, {summary['newDebt']} new debt"
    )
    if summary["debtByCode"]:
        print(
            "debt by rule: "
            + ", ".join(
                f"{name}={count}"
                for name, count in summary["debtByCode"].items()
            )
        )
    if summary["retiredBaselineEntries"]:
        print(f"baseline debt retired: {summary['retiredBaselineEntries']}")
        print("BASELINE STALE: run --write-baseline after reviewing the retired debt")
    for issue in new_issues[:50]:
        print(
            f"NEW {issue.path}:{issue.line} [{issue.code}] "
            f"{issue.detail} :: {normalize_expression(issue.expression, 180)}"
        )
    if len(new_issues) > 50:
        print(f"... {len(new_issues) - 50} more new issues")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--scan",
        action="append",
        default=[],
        help="relative file/directory to scan; repeat to replace the default targets",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    if args.check and args.write_baseline:
        parser.error("--check and --write-baseline are mutually exclusive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    policy_path = args.policy
    if not policy_path.is_absolute():
        policy_path = root / policy_path
    baseline_path = args.baseline
    if not baseline_path.is_absolute():
        baseline_path = root / baseline_path
    targets = tuple(args.scan) if args.scan else DEFAULT_SCAN_TARGETS
    try:
        policies = load_policies(policy_path)
        calls, issues, files = audit_paths(root, policies, targets)
        new_issues: list[Issue] = []
        retired = 0
        if args.write_baseline:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(
                json.dumps(baseline_payload(issues), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif args.check:
            new_issues, retired = compare_baseline(
                issues,
                read_baseline(baseline_path),
            )
        summary = report(
            calls,
            issues,
            files,
            new_issues=new_issues,
            retired=retired,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"reader network audit failed: {error}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_human(summary, new_issues)
        if args.write_baseline:
            print(f"baseline written: {baseline_path}")
    return 1 if new_issues or (args.check and retired) else 0


if __name__ == "__main__":
    raise SystemExit(main())

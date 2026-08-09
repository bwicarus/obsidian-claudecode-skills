import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const PACKAGE = read("ios/BWReader/package_local_reader.py");
const MANIFEST = JSON.parse(
  read("ios/BWReader/native_reader_interface_manifest.json"),
);
const ROOT_PATH = fileURLToPath(ROOT);
const SURFACES = new Set(["pdf", "epub"]);
const OWNERS = new Set(["local", "pi", "native"]);
const STATUSES = new Set(["supported", "degraded", "pending"]);
const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];
const REMOTE_POINTERS = new Set([
  "/file", "/context/file", "/context/file_rel", "/ctx/file_rel",
  "/item/file", "/remove_item/file",
]);

function findPython() {
  const candidates = [process.env.PYTHON, "python3", "python"].filter(Boolean);
  if (process.platform === "win32" && process.env.LOCALAPPDATA) {
    const root = join(process.env.LOCALAPPDATA, "Programs", "Python");
    if (existsSync(root)) {
      for (const name of readdirSync(root).sort().reverse()) {
        candidates.push(join(root, name, "python.exe"));
      }
    }
  }
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ["-c", "import sys; print(sys.version_info[0])"], {
      encoding: "utf8",
    });
    if (probe.status === 0 && probe.stdout.trim() === "3") return candidate;
  }
  throw new Error("Python 3 is required by the native Reader packager");
}

function interactionPolicies() {
  const context = { module: { exports: {} } };
  context.globalThis = context;
  vm.runInNewContext(
    read("_server_deploy/static/reader-runtime/interaction-policy.js"),
    context,
    { filename: "interaction-policy.js" },
  );
  return context.module.exports.policies();
}

test("native manifest admits every method declared by the loaded interaction policy", () => {
  for (const policy of interactionPolicies()) {
    for (const match of policy.matches) {
      if (!match.path.startsWith("/pdf/api/") &&
          !match.path.startsWith("/api/assistant/")) continue;
      const literalPrefix = match.path
        .replace(/\*$/, "")
        .replace(/\{[^}]+\}.*$/, "");
      const route = MANIFEST.routes.find((candidate) => (
        candidate.match === "exact"
          ? candidate.path === match.path
          : literalPrefix.startsWith(candidate.path)
      ));
      // A policy can be loaded for a different document host (currently the
      // web-only translation adapter). Route coverage separately proves which
      // literals are reachable from each packaged native shell.
      if (!route) continue;
      for (const method of match.methods) {
        assert.ok(
          route.methods.includes(method),
          `${policy.id} calls ${method} ${match.path}, but the native manifest rejects it`,
        );
      }
    }
  }
});

test("native packager derives Pi methods from Flask sources and rejects server drift", () => {
  const python = findPython();
  const script = String.raw`
import importlib.util, json, sys, tempfile
from pathlib import Path

source = Path("ios/BWReader/package_local_reader.py")
spec = importlib.util.spec_from_file_location("native_reader_server_probe", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
manifest = json.loads(Path("ios/BWReader/native_reader_interface_manifest.json").read_text(encoding="utf-8"))
module.validate_native_pi_server_routes(manifest)
with tempfile.TemporaryDirectory() as temporary:
    temporary = Path(temporary)
    assistant = Path("_server_deploy/assistant.py").read_text(encoding="utf-8")
    needle = '@bp.route("/creations-brief")'
    assert assistant.count(needle) == 1
    changed = temporary / "assistant.py"
    changed.write_text(
        assistant.replace(
            needle,
            '@bp.route("/creations-brief", methods=["GET", "POST"])',
        ),
        encoding="utf-8",
    )
    sources = tuple(
        (changed, prefix) if path.name == "assistant.py" else (path, prefix)
        for path, prefix in module.NATIVE_INTERFACE_SERVER_SOURCES
    )
    try:
        module.validate_native_pi_server_routes(manifest, sources=sources)
    except SystemExit as error:
        assert "method drift" in str(error)
    else:
        raise AssertionError("packager accepted Flask method drift")
print("server-method-gate=PASS")
`;
  const result = spawnSync(python, ["-c", script], {
    cwd: ROOT_PATH,
    encoding: "utf8",
    env: { ...process.env, PYTHONUTF8: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  assert.equal(result.stdout.trim(), "server-method-gate=PASS");
});

test("native packager rejects local handler owner, method and surface drift", () => {
  const python = findPython();
  const script = String.raw`
import copy, importlib.util, json, sys
from pathlib import Path

source = Path("ios/BWReader/package_local_reader.py")
spec = importlib.util.spec_from_file_location("native_reader_dispatch_probe", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
manifest = json.loads(Path("ios/BWReader/native_reader_interface_manifest.json").read_text(encoding="utf-8"))
module.validate_native_runtime_dispatch(manifest)

def rejected(field, value):
    probe = copy.deepcopy(manifest)
    route = next(item for item in probe["routes"] if item["path"] == "/pdf/api/book-langs")
    route[field] = value
    try:
        module.validate_native_runtime_dispatch(probe)
    except SystemExit as error:
        assert "dispatch" in str(error)
        return
    raise AssertionError(f"packager accepted local handler {field} drift")

rejected("owner", "pi")
rejected("methods", ["GET"])
rejected("surfaces", ["pdf"])
print("runtime-dispatch-gate=PASS")
`;
  const result = spawnSync(python, ["-c", script], {
    cwd: ROOT_PATH,
    encoding: "utf8",
    env: { ...process.env, PYTHONUTF8: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  assert.equal(result.stdout.trim(), "runtime-dispatch-gate=PASS");
});

test("native Reader interface manifest has a strict non-overlapping schema", () => {
  assert.equal(MANIFEST.contract, "reader-native-interface-manifest/2");
  assert.deepEqual(Object.keys(MANIFEST).sort(), ["contract", "routes", "scanIgnores"]);
  assert.ok(MANIFEST.routes.length > 0, "the native Reader surface must not be empty");
  const keys = [];
  for (const route of MANIFEST.routes) {
    assert.deepEqual(Object.keys(route).sort(), [
      "description", "match", "methods", "owner", "path",
      "remoteBook", "status", "surfaces",
    ]);
    assert.match(route.path, /^\/(?:pdf\/api|api\/assistant)\/[A-Za-z0-9._~:@%+\/-]*$/);
    assert.notEqual(route.path, "/pdf/api/");
    assert.notEqual(route.path, "/api/assistant/");
    assert.ok(["exact", "segment"].includes(route.match));
    assert.equal(route.path.endsWith("/"), route.match === "segment");
    assert.ok(OWNERS.has(route.owner));
    assert.ok(STATUSES.has(route.status));
    assert.ok(route.methods.length > 0);
    assert.deepEqual(
      route.methods,
      [...new Set(route.methods)].sort((a, b) => METHODS.indexOf(a) - METHODS.indexOf(b)),
    );
    assert.ok(route.methods.every((method) => METHODS.includes(method)));
    assert.ok(route.surfaces.length > 0);
    assert.deepEqual(route.surfaces, [...new Set(route.surfaces)].sort());
    assert.ok(route.surfaces.every((surface) => SURFACES.has(surface)));
    if (route.remoteBook === null) {
      assert.equal(route.remoteBook, null);
    } else {
      assert.equal(route.owner, "pi");
      assert.deepEqual(Object.keys(route.remoteBook).sort(), [
        "continuation", "identities", "mode", "requiredMethods", "scope",
      ]);
      assert.ok(["required", "conditional"].includes(route.remoteBook.mode));
      assert.ok(["current", "catalog"].includes(route.remoteBook.scope));
      assert.deepEqual(
        route.remoteBook.requiredMethods,
        route.methods.filter((method) => route.remoteBook.requiredMethods.includes(method)),
      );
      assert.equal(
        route.remoteBook.mode === "required",
        route.remoteBook.requiredMethods.length === route.methods.length,
      );
      assert.ok(route.remoteBook.identities.length > 0);
      const identityMethods = new Set();
      for (const identity of route.remoteBook.identities) {
        assert.deepEqual(Object.keys(identity).sort(), [
          "location", "methods", "pointer", "transform",
        ]);
        assert.ok(["query", "json"].includes(identity.location));
        assert.ok(REMOTE_POINTERS.has(identity.pointer));
        assert.ok(["exact", "prefix-before-delimiter"].includes(identity.transform));
        assert.deepEqual(
          identity.methods,
          route.methods.filter((method) => identity.methods.includes(method)),
        );
        identity.methods.forEach((method) => identityMethods.add(method));
      }
      assert.ok(route.remoteBook.requiredMethods.every((method) => identityMethods.has(method)));
      if (route.remoteBook.continuation !== null) {
        assert.deepEqual(route.remoteBook.continuation, {
          kind: "rid", pointer: "/rid", fromPointer: "/from",
        });
      }
    }
    assert.ok(typeof route.description === "string" && route.description.trim().length > 0);
    keys.push(`${route.path}\0${route.match}`);
  }
  assert.deepEqual(keys, [...keys].sort());
  assert.equal(new Set(keys).size, keys.length);
  assert.equal(
    MANIFEST.routes.every((route) => route.status === "supported"),
    true,
    "a native app bundle may not ship pending or degraded old Reader interfaces",
  );
  for (let left = 0; left < MANIFEST.routes.length; left += 1) {
    for (let right = left + 1; right < MANIFEST.routes.length; right += 1) {
      const a = MANIFEST.routes[left];
      const b = MANIFEST.routes[right];
      assert.equal(
        (a.match === "segment" && b.path.startsWith(a.path))
          || (b.match === "segment" && a.path.startsWith(b.path)),
        false,
        `overlapping route declarations: ${a.path}, ${b.path}`,
      );
    }
  }
  assert.equal(MANIFEST.routes.some((route) => route.path === "/pdf/api/assistant"), false);
  assert.deepEqual(
    MANIFEST.routes.find((route) => route.path === "/pdf/api/epub-assistant")
      .remoteBook.identities[0].pointer,
    "/context/file",
  );
  assert.equal(
    MANIFEST.routes.find((route) => route.path === "/pdf/api/favorites")
      .remoteBook.mode,
    "conditional",
  );
});

test("the packager audits the actual rendered PDF and EPUB script closure", () => {
  const python = findPython();
  const script = String.raw`
import copy, importlib.util, json, sys, tempfile
from pathlib import Path

repo = Path.cwd()
source = repo / "ios/BWReader/package_local_reader.py"
spec = importlib.util.spec_from_file_location("native_reader_packager_contract", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
manifest = module.load_native_interface_manifest()
pending = json.loads(json.dumps(manifest))
pending["routes"][0]["status"] = "pending"
try:
    module.validate_native_interface_manifest(pending, label="pending-probe")
except SystemExit as error:
    assert "not release-compatible" in str(error)
else:
    raise AssertionError("a pending old Reader interface did not fail the build gate")
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    module.copy_raw_static(root)
    module.write_bytes(root, module.PDF_SHELL, module.build_pdf_shell(manifest).encode("utf-8"))
    module.write_bytes(root, module.EPUB_SHELL, module.build_epub_shell(manifest).encode("utf-8"))
    module.validate_native_interface_coverage(root, manifest)
    pdf_shell = (root / module.PDF_SHELL).read_text(encoding="utf-8")
    epub_shell = (root / module.EPUB_SHELL).read_text(encoding="utf-8")
    assert pdf_shell.index("window.__BW_NATIVE_INTERFACE_MANIFEST__=") < pdf_shell.index("/static/pdf/native-local-runtime.js")
    assert epub_shell.index("window.__BW_NATIVE_INTERFACE_MANIFEST__=") < epub_shell.index("/static/pdf/native-local-runtime.js")
    probe = root / "static/pdf/rc-core.js"
    original_probe = probe.read_text(encoding="utf-8")
    probe.write_text(original_probe + "\nfetch('/pdf/api/manifest-gate-probe');\n", encoding="utf-8")
    try:
        module.validate_native_interface_coverage(root, manifest)
    except SystemExit as error:
        assert "manifest-gate-probe" in str(error)
    else:
        raise AssertionError("an unclassified route did not fail the packaging gate")
    probe.write_text(original_probe, encoding="utf-8")

    pdf_path = root / module.PDF_SHELL
    original_pdf = pdf_path.read_text(encoding="utf-8")
    pdf_path.write_text(
        original_pdf.replace(
            "</body>",
            "<script nonce=\"__BW_LOCAL_CSP_NONCE__\">fetch('/pdf/api/inline-gate-probe')</script></body>",
        ),
        encoding="utf-8",
    )
    try:
        module.validate_native_interface_coverage(root, manifest)
    except SystemExit as error:
        assert "inline-gate-probe" in str(error)
    else:
        raise AssertionError("an unclassified inline-script route passed packaging")
    pdf_path.write_text(original_pdf, encoding="utf-8")

    no_consumer = copy.deepcopy(manifest)
    no_consumer["routes"].append({
        "path": "/pdf/api/no-consumer-gate-probe",
        "match": "exact",
        "owner": "pi",
        "methods": ["POST"],
        "surfaces": ["pdf"],
        "status": "supported",
        "remoteBook": None,
        "description": "No loaded consumer probe.",
    })
    no_consumer["routes"].sort(key=lambda route: (route["path"], route["match"]))
    try:
        module.validate_native_interface_coverage(root, no_consumer)
    except SystemExit as error:
        assert "no loaded consumer or native dispatch evidence" in str(error)
        assert "no-consumer-gate-probe" in str(error)
    else:
        raise AssertionError("a manifest route without an entry point passed packaging")

    serverless = copy.deepcopy(manifest)
    serverless["routes"].append({
        "path": "/pdf/api/serverless-gate-probe",
        "match": "exact",
        "owner": "pi",
        "methods": ["GET"],
        "surfaces": ["pdf"],
        "status": "supported",
        "remoteBook": None,
        "description": "Loaded consumer without a Pi definition probe.",
    })
    serverless["routes"].sort(key=lambda route: (route["path"], route["match"]))
    pdf_path.write_text(
        original_pdf.replace(
            "</body>",
            "<script nonce=\"__BW_LOCAL_CSP_NONCE__\">fetch('/pdf/api/serverless-gate-probe')</script></body>",
        ),
        encoding="utf-8",
    )
    try:
        module.validate_native_interface_coverage(root, serverless)
    except SystemExit as error:
        assert "no parsed server definition" in str(error)
        assert "serverless-gate-probe" in str(error)
    else:
        raise AssertionError("a loaded Pi route without a server definition passed packaging")
print("coverage-gates=PASS")
`;
  const result = spawnSync(python, ["-c", script], {
    cwd: ROOT_PATH,
    encoding: "utf8",
    env: { ...process.env, PYTHONUTF8: "1" },
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  assert.equal(result.stdout.trim(), "coverage-gates=PASS");
});

test("packaging copies one manifest and injects it before native-local-runtime under CSP", () => {
  assert.match(PACKAGE, /NATIVE_INTERFACE_SOURCE = HERE \/ "native_reader_interface_manifest\.json"/);
  assert.match(PACKAGE, /shutil\.copyfile\(\s*NATIVE_INTERFACE_SOURCE, staging \/ NATIVE_INTERFACE_NAME\s*\)/s);
  assert.equal(
    [...PACKAGE.matchAll(/\+ _native_interface_bootstrap\(interface_manifest\)/g)].length,
    2,
  );
  assert.match(PACKAGE, /return f"<script>window\.\{NATIVE_INTERFACE_GLOBAL\}=\{encoded\};<\/script>\\n"/);
  assert.match(PACKAGE, /flag < interface_manifest < purifier < runtime < marked/);
  assert.match(PACKAGE, /interface_manifest < jszip < purifier < runtime/);
  assert.match(PACKAGE, /validate_native_interface_coverage\(root, interface_manifest\)/);
  assert.match(PACKAGE, /validate_bundle\(staging, require_manifest=False\)[\s\S]*write_manifest\(staging\)/);
  assert.match(PACKAGE, /<script\\b\(\?!\[\^>\]\*\\bnonce/);
});

test("formula recognition is a real Pi interface and its Swift consumer is packaging evidence", () => {
  const formula = MANIFEST.routes.find((route) => route.path === "/pdf/api/formula-ocr");
  const status = MANIFEST.routes.find((route) => route.path === "/pdf/api/formula-ocr-status");
  assert.deepEqual(formula?.methods, ["POST"]);
  assert.deepEqual(formula?.remoteBook?.requiredMethods, ["POST"]);
  assert.equal(formula?.remoteBook?.identities?.[0]?.location, "json");
  assert.deepEqual(status?.methods, ["GET"]);
  assert.deepEqual(status?.remoteBook?.requiredMethods, ["GET"]);
  assert.equal(status?.remoteBook?.identities?.[0]?.location, "query");

  const swift = read("ios/BWReader/App/NativeFormulaRecognition.swift");
  const toolsView = read("ios/BWReader/App/NativeReaderToolsView.swift");
  const gateway = read("ios/BWReader/App/ReaderNativePiGateway.swift");
  assert.match(swift, /fetch\('\/pdf\/api\/formula-ocr'/);
  assert.match(swift, /new URL\('\/pdf\/api\/formula-ocr-status'/);
  assert.match(swift, /ReaderLocalLibraryManager\.shared\.books\.contains/);
  assert.match(swift, /\$0\.id == localBookID && \$0\.format == \.pdf/);
  assert.equal(
    [...swift.matchAll(/error: String\(error && error\.message \|\| error\)/g)].length,
    3,
    "gateway and JSON failures must remain visible to the native tools sheet",
  );
  assert.match(toolsView, /reader\.supportsNativeFormulaRecognition\(/);
  assert.match(toolsView, /refreshNativeFormulaRecognitionStatus\(/);
  assert.match(gateway, /请先在书库同步书体到 Pi，再选择 Pi 预处理/);
  assert.doesNotMatch(swift, /native.*latex.*=/i);
  assert.match(PACKAGE, /NATIVE_INTERFACE_SWIFT_CONSUMERS = \(/);
  assert.match(PACKAGE, /NATIVE_FORMULA_RECOGNITION_SOURCE/);
  assert.match(
    PACKAGE,
    /source_path\.read_text\(encoding="utf-8", errors="replace"\)/,
  );
});

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const SERVER = read("ios/BWReader/App/ReaderLocalRuntimeServer.swift");
const PROXY = read("ios/BWReader/App/ReaderNativeImageProxy.swift");
const PACKAGER = read("ios/BWReader/package_local_reader.py");
const MANIFEST = JSON.parse(read(
  "ios/BWReader/native_reader_interface_manifest.json",
));

test("App image cards use a capability-gated native route rather than Pi", () => {
  const route = MANIFEST.routes.find(
    (candidate) => candidate.path === "/pdf/api/img-proxy",
  );
  assert.equal(route?.owner, "native");
  assert.deepEqual(route?.methods, ["GET"]);
  assert.deepEqual(route?.surfaces, ["epub", "pdf"]);
  assert.equal(route?.remoteBook, null);

  const localRoute = SERVER.indexOf('if decodedPath == "/pdf/api/img-proxy"');
  const piBranch = SERVER.indexOf("if Self.isDirectPiResourcePath(decodedPath)");
  assert.ok(localRoute >= 0 && localRoute < piBranch);
  assert.match(
    SERVER.slice(localRoute, piBranch),
    /trustedResourceSurface\([\s\S]*Referer/,
  );
  const piRoutes = SERVER.slice(
    SERVER.indexOf("private static func isDirectPiResourcePath"),
    SERVER.indexOf("private func serveNativeImageProxy"),
  );
  assert.doesNotMatch(piRoutes, /img-proxy/);
  assert.match(
    PACKAGER,
    /"\/pdf\/api\/img-proxy": \([\s\S]*"native"[\s\S]*"__native_owner__"/,
  );
});

test("native image transport is bounded at every external-input boundary", () => {
  assert.match(PROXY, /scheme\?\.lowercased\(\) == "https"/);
  assert.match(PROXY, /url\.user == nil, url\.password == nil/);
  assert.match(PROXY, /url\.fragment == nil/);
  assert.match(PROXY, /getaddrinfo\(/);
  // 2026-08-30 起桥资产主机（CGNAT 100.64/10）经 trustedPrivateHosts 豁免
  // 公网检查 —— guard 带上豁免位。豁免面本身也要锁：只认精确主机名集合。
  assert.match(PROXY, /guard hostIsTrusted \|\| isPublicIPv4\(bytes\)/);
  assert.match(PROXY, /guard hostIsTrusted \|\| isPublicIPv6\(bytes\)/);
  assert.match(PROXY, /trustedPrivateHosts\.contains\(hostname\)/);
  assert.match(
    PROXY,
    /for redirectCount in 0\.\.\.5[\s\S]*ReaderNativeImageProxyPolicy\.resolve\(current\)/,
  );
  assert.match(PROXY, /guard redirectCount < 5/);
  assert.match(PROXY, /maximumBytes = 16 \* 1_024 \* 1_024/);
  assert.match(PROXY, /length <= maximumBytes/);
  assert.match(PROXY, /maximumBytes - output\.count/);
  assert.match(PROXY, /supportedContentTypes[\s\S]*\.contains\(contentType\)/);
  assert.match(PROXY, /Task\.sleep\(nanoseconds: 15_000_000_000\)/);
});

test("native image socket is pinned to the validated address while TLS keeps the URL host", () => {
  assert.match(PROXY, /import Network/);
  assert.match(PROXY, /NI_NUMERICHOST/);
  assert.match(PROXY, /endpoints\.append\(\.ipv4\(ip\)\)/);
  assert.match(PROXY, /endpoints\.append\(\.ipv6\(ip\)\)/);
  assert.match(
    PROXY,
    /let target = try ReaderNativeImageProxyPolicy\.resolve\(current\)[\s\S]*request\(target\)/,
  );
  assert.match(
    PROXY,
    /sec_protocol_options_set_tls_server_name\([\s\S]*target\.hostname/,
  );
  assert.match(
    PROXY,
    /let connection = NWConnection\(\s*host: endpoint,\s*port: port,/,
  );
  assert.ok(PROXY.includes('"Host: \\(hostHeader)"'));
  assert.doesNotMatch(PROXY, /URLSession/);
});

test("local image proxy does not widen the App CSP to arbitrary HTTPS", () => {
  const directive = SERVER.match(/img-src [^;]+;/)?.[0] || "";
  assert.equal(directive, "img-src 'self' blob: data:;");
  assert.doesNotMatch(directive, /https:/);
  assert.match(SERVER, /"X-BW-Native-Image-Proxy"\): "local\/1"/);
  assert.match(SERVER, /"X-BW-Reader-Error"\): error\.diagnosticCode/);
});

#!/usr/bin/env node
/* 本文件只访问本机临时 HTTP 服务。运行：
 *   npm install --no-save playwright
 *   node tests/reader_contract/indexeddb-store.playwright.mjs
 */
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

const workspace = resolve(import.meta.dirname, "../..");
const pagePath = "/tests/reader_contract/indexeddb-store.browser.html";
const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
};

async function loadPlaywright() {
  try {
    return await import("playwright");
  } catch {
    throw new Error(
      "未安装 Playwright。请先运行 npm install --no-save playwright，再执行本文件。",
    );
  }
}

const server = createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(new URL(request.url, "http://127.0.0.1").pathname);
    const target = resolve(workspace, "." + pathname);
    if (target !== workspace && !target.startsWith(workspace + sep)) {
      response.writeHead(403).end("Forbidden");
      return;
    }
    if (!(await stat(target)).isFile()) {
      response.writeHead(404).end("Not found");
      return;
    }
    response.writeHead(200, {
      "content-type": types[extname(target)] || "application/octet-stream",
      "cache-control": "no-store",
    });
    response.end(await readFile(target));
  } catch {
    response.writeHead(404).end("Not found");
  }
});

let browser;
try {
  const { chromium } = await loadPlaywright();
  await new Promise((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const address = server.address();
  const url = `http://127.0.0.1:${address.port}${pagePath}`;
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  page.on("console", (message) => {
    if (message.type() === "error") process.stderr.write(message.text() + "\n");
  });
  await page.goto(url);
  await page.waitForFunction(() => window.__BW_IDB_TEST_RESULT__, null, { timeout: 30_000 });
  const result = await page.evaluate(() => window.__BW_IDB_TEST_RESULT__);
  if (!result.passed) throw new Error(result.error || "浏览器契约测试失败");
  process.stdout.write(`IndexedDB browser contract: ${result.assertions} assertions passed\n`);
} finally {
  if (browser) await browser.close();
  await new Promise((resolveClose) => server.close(resolveClose));
}


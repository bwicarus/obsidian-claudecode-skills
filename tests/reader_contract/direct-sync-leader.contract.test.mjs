import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Leader = require(
  "../../_server_deploy/static/reader-runtime/direct-sync-leader.js",
);

function host() {
  const calls = [];
  return {
    calls,
    start(reason) { calls.push(["start", reason]); },
    pause(reason) { calls.push(["pause", reason]); },
    destroy(reason) { calls.push(["destroy", reason]); },
    status() { return { state: "test" }; },
  };
}

test("Web Lock leader 才启动 PWA RTC，pause 会先停 host 再释放锁", async () => {
  const target = host();
  let callback;
  let releaseRequest;
  const requestFinished = new Promise((resolve) => { releaseRequest = resolve; });
  const locks = {
    request(_name, options, fn) {
      assert.deepEqual(options, { mode: "exclusive", ifAvailable: true });
      callback = fn;
      return Promise.resolve(fn({ name: "held" })).then((value) => {
        releaseRequest(value);
        return value;
      });
    },
  };
  const leader = Leader.createDirectSyncLeader({
    locks,
    host: target,
    lockName: "reader-direct-test",
  });

  leader.start("boot");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof callback, "function");
  assert.equal(leader.status().leader, true);
  assert.equal(target.calls[0][0], "start");

  leader.pause("provider-attaching");
  await requestFinished;
  assert.equal(leader.status().leader, false);
  assert.equal(
    target.calls.some((entry) =>
      entry[0] === "pause" && entry[1] === "provider-attaching"
    ),
    true,
  );
  leader.destroy();
});

test("没有 Web Locks 时直连 fail closed，服务器同步宿主不会被启动", async () => {
  const target = host();
  const leader = Leader.createDirectSyncLeader({
    locks: null,
    host: target,
    lockName: "reader-direct-test",
  });
  assert.equal(leader.start("boot"), true);
  await leader.acquire();
  assert.equal(leader.status().supported, false);
  assert.equal(target.calls.some((entry) => entry[0] === "start"), false);
  leader.destroy();
});

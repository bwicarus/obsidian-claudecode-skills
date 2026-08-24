import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 活动账本 §3.2：修改/删除的版本记录。
//
// ⚠ 这份测试最重要的不是"功能在不在"，而是**覆盖面有没有被说清楚**。
//   九类对象里有四类（高亮/便签/插入页/墨迹）的权威写入点是 App 内本地执行，
//   根本不出网 —— Pi 这一侧看不见。只记 Pi 侧却宣称"改删都有记录了"，
//   是这条链上最容易造成的误解。
const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

const ASSIST = read("_server_deploy/assistant.py");
const ATTN = read("scripts/attention_profile.py");
const RELAY = read("_server_deploy/reader_sync_relay.py");

test("mutate 埋点存在，且用 append_raw 而不是直写 events.db", () => {
  assert.match(ASSIST, /def _ledger_mutate\(/, "埋点必须存在");
  const fn = ASSIST.slice(
    ASSIST.indexOf("def _ledger_mutate("),
    ASSIST.indexOf("def _creation_register("),
  );
  assert.match(
    fn, /AP\.append_raw\(/,
    "必须走账本 —— 直写 events.db 的数据 --rebuild 时会永久消失",
  );
  assert.match(fn, /"mutate"/, "渠道名必须是 mutate");
});

test("⚠ 覆盖面必须写在代码里，别让人以为改删都有记录了", () => {
  const fn = ASSIST.slice(
    ASSIST.indexOf("# ── 活动账本 · mutate 渠道"),
    ASSIST.indexOf("def _creation_register("),
  );
  assert.match(
    fn, /只记\*\*助手发起的\*\*改删/,
    "必须明说只覆盖助手侧",
  );
  assert.match(
    fn, /owner=local/,
    "必须点明 App 本地那四条路由不出网 —— 这是覆盖面缺口的根因",
  );
});

test("⚠ before 只存摘要不存全文 —— 账本是永不删的", () => {
  const fn = ASSIST.slice(
    ASSIST.indexOf("def _ledger_mutate("),
    ASSIST.indexOf("def _creation_register("),
  );
  assert.match(fn, /before_hash/, "存 hash");
  assert.match(fn, /before_len/, "存长度，好回答'改了多少'");
  assert.doesNotMatch(
    fn, /extra\["before"\] = before/,
    "全文进冷归档，不进账本 —— append-only 的东西灌全文会无界膨胀",
  );
});

test("actor=ai —— 冲突判定要靠它（用户永远赢）", () => {
  const fn = ASSIST.slice(
    ASSIST.indexOf("def _ledger_mutate("),
    ASSIST.indexOf("def _creation_register("),
  );
  assert.match(
    fn, /actor="ai"/,
    "助手发起的改删必须标 ai —— references/reader-data-authority.md 的冲突规则靠它",
  );
});

test("记账失败不能让业务操作跟着失败", () => {
  const fn = ASSIST.slice(
    ASSIST.indexOf("def _ledger_mutate("),
    ASSIST.indexOf("def _creation_register("),
  );
  assert.match(fn, /except Exception:\s*\n\s*pass/, "静默兜底");
  // 但要有边界：不能连"为什么记不上"都无从查。这里的取舍写在注释里。
  assert.match(fn, /记账失败不该让业务操作跟着失败/);
});

test("两个写工具都埋了点", () => {
  for (const fn of ["_t_userpage_edit", "_t_userpage_delete"]) {
    const body = ASSIST.slice(
      ASSIST.indexOf(`def ${fn}(`),
      ASSIST.indexOf(`def ${fn}(`) + 1800,
    );
    assert.match(body, /_ledger_mutate\(/, `${fn} 必须记一笔`);
  }
});

test("⚠ mutate 事件不能被无声的分词门槛吃掉", () => {
  // add_event 的 `if not terms: return 0` 没有任何日志。
  // "删除了卡片 c_ab12" 抽不出词 —— 于是导入计数看着正常、查询里永远找不到。
  assert.match(
    ATTN, /TERMLESS_CHANNELS = \{"mutate"\}/,
    "mutate 必须豁免分词门槛，否则记了等于没记",
  );
  assert.match(
    ATTN, /if not terms and channel not in TERMLESS_CHANNELS:/,
    "豁免要真的接在门槛上",
  );
});

test("⚠ 豁免面不能扩大", () => {
  const at = ATTN.indexOf("TERMLESS_CHANNELS = ");
  const line = ATTN.slice(at, ATTN.indexOf("\n", at));
  assert.equal(
    line.match(/"/g).length, 2,
    "只有 mutate 一个 —— 扩大会让画像被无词噪声灌满",
  );
});


// ── 用户 2026-08-24 指出的更好的路子 ───────────────────────────────
//   「我在 app 或者扩展里进行删除之类的操作时不还是会留下记录然后需要
//     同步到 pi 和 windows 么，那这个时候进行记录不就好了么」
//   他是对的：删除本来就以 tombstone 越界到同步中继，在那里记
//   **不用改任何客户端**，而且连用户自己手动删的也记得上。
test("⚠ 同步流上的改/删也要记账 —— 不用改客户端的那条路", () => {
  assert.match(RELAY, /def _ledger_sync_mutation\(/, "同步侧埋点必须存在");
  assert.match(
    RELAY, /_ledger_sync_mutation\(connection, change, device_id, now\)/,
    "必须真的接在变更落库之后，不能只定义不调用",
  );
});

test("同步侧的 client 直接读 owner lease，不猜", () => {
  const fn = RELAY.slice(
    RELAY.indexOf("def _ledger_sync_mutation("),
    RELAY.indexOf("def _push_locked("),
  );
  assert.match(
    fn, /SELECT owner_role FROM sync_owner_leases/,
    "ownerRole 是服务端本来就在强校验的三值枚举 —— 比任何推断都硬",
  );
  assert.match(
    fn, /pwa-install-v1-/,
    "注释里要写明为什么不能拿 deviceId 前缀判：App 内 JS 铸的也是这个前缀",
  );
});

test("⚠ 两处埋点要能分辨来源，否则同一次改动可能出现两条", () => {
  const fn = RELAY.slice(
    RELAY.indexOf("def _ledger_sync_mutation("),
    RELAY.indexOf("def _push_locked("),
  );
  assert.match(fn, /"via": "sync"/, "同步侧标 via:sync");
});

test("⚠ 记账失败绝不能让同步失败", () => {
  const fn = RELAY.slice(
    RELAY.indexOf("def _ledger_sync_mutation("),
    RELAY.indexOf("def _push_locked("),
  );
  assert.match(fn, /except Exception:\s+pass/);
  assert.match(fn, /同步是用户数据的生命线/, "取舍要写在代码里");
});

test("⚠ 覆盖面边界要写清：高亮/便签/笔迹今天不跨设备同步", () => {
  assert.match(
    RELAY, /unsupportedDomains/,
    "必须指向那份写死的清单，否则读代码的人会以为改删全覆盖了",
  );
});

test("账本脚本路径不得硬编码 Pi 绝对路径", () => {
  const at = RELAY.indexOf("_LEDGER_SCRIPTS");
  const line = RELAY.slice(at, at + 260);
  assert.doesNotMatch(
    line, /\/home\/bwicarus/,
    "assistant.py 里那处历史写法硬编码了 Pi 路径，换机就断 —— 别再犯",
  );
  assert.match(line, /CLAUDE_PROJECT/);
});

// ── 用户 2026-08-24 定的判据 ───────────────────────────────────────
//   「对于本身不落库的内容我们可以暂时认为其不够重要不需要落库，
//     或者像选中和高亮事件这种本身频率就太高」
test("⚠ 白名单闸存在 —— 防的是未来，不是现在", () => {
  assert.match(
    RELAY, /_LEDGER_MUTATE_COLLECTIONS = frozenset\(\{/,
    "必须是白名单而不是照单全收",
  );
  // ⚠ 断言"符号出现过"是不够的：把判定改成 `if False:` 之后常量还在，
  //   断言照样通过 —— 变异验证抓到的正是这一点（今晚第二次栽在同一形态上）。
  //   要钉住的是**闸真的挡在路上**。
  const fnBody = RELAY.slice(
    RELAY.indexOf("def _ledger_sync_mutation("),
    RELAY.indexOf("def _push_locked("),
  );
  assert.match(
    fnBody, /if collection not in _LEDGER_MUTATE_COLLECTIONS:/,
    "闸必须真的判白名单，不能只声明常量",
  );
  assert.match(
    fnBody, /\n {12}return\n/,
    "不在白名单里必须**直接返回** —— 否则闸判了也不拦",
  );
  const at = RELAY.indexOf("_LEDGER_MUTATE_COLLECTIONS = frozenset({");
  const block = RELAY.slice(at, at + 260);
  for (const c of ["card-entities", "card-states", "user-settings", "vocabulary-state"]) {
    assert.ok(block.includes(c), `白名单里少了 ${c}`);
  }
});

test("⚠ 白名单必须与同步注册表一致 —— 两边分叉就是静默漏记", () => {
  // 同步注册表摘要决定"什么会越界到这里"；白名单决定"越界了记不记"。
  // 前者多一个而后者没跟上 → 那类东西永远没有版本记录，而且没人知道。
  const digestAt = RELAY.indexOf("CARD_REGISTRY_DIGEST = (");
  const digest = RELAY.slice(digestAt, digestAt + 320);
  const inDigest = [...digest.matchAll(/"([a-z-]+):explicit/g)].map((m) => m[1]);
  const wlAt = RELAY.indexOf("_LEDGER_MUTATE_COLLECTIONS = frozenset({");
  const wl = RELAY.slice(wlAt, wlAt + 260);
  assert.ok(inDigest.length >= 4, `注册表里只解析出 ${inDigest.length} 个集合`);
  for (const c of inDigest) {
    assert.ok(
      wl.includes(`"${c}"`),
      `${c} 进了同步注册表却不在账本白名单里 —— 它的改/删将永远没有记录`,
    );
  }
});

test("⚠ 表外集合要出声，但每个只出一次", () => {
  const fn = RELAY.slice(
    RELAY.indexOf("def _ledger_sync_mutation("),
    RELAY.indexOf("def _push_locked("),
  );
  // 同样不能只断言符号出现：要钉住**判定**和**记录**两件事都在。
  assert.match(
    fn, /if collection and collection not in _LEDGER_UNKNOWN_SEEN:/,
    "必须真的按集合去重判定，不能只提到那个集合名",
  );
  assert.match(
    fn, /_LEDGER_UNKNOWN_SEEN\.add\(collection\)/,
    "喊过要记下来 —— 不记就成了每条都喊，日志会被刷爆",
  );
  assert.match(
    fn, /print\(\s*"\[ledger\] 集合 %s 的改\/删不进账本"/,
    "必须真的打印出来 —— 静默跳过会让'新集合没有版本记录'永远无人发现",
  );
});

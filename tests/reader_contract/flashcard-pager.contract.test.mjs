import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-flashcard.js", import.meta.url),
  "utf8"
);
const VOICE_SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-voicecall.js", import.meta.url),
  "utf8"
);

class FakeNode {
  constructor() {
    this.listeners = new Map();
    this.attributes = new Map();
    this.values = new Set();
    this.classList = {
      toggle: (name, force) => {
        const on = force === undefined ? !this.values.has(name) : Boolean(force);
        if (on) this.values.add(name);
        else this.values.delete(name);
        return on;
      },
      contains: (name) => this.values.has(name)
    };
    this.scrollLeft = 0;
    this.clientWidth = 100;
    this.offsetLeft = 0;
    this.offsetWidth = 100;
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    this.listeners.set(type, listeners.filter((entry) => entry !== listener));
  }

  dispatch(type, event = {}) {
    for (const listener of this.listeners.get(type) || []) {
      listener({
        stopPropagation() {},
        ...event
      });
    }
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }

  getAttribute(name) {
    return this.attributes.get(String(name)) ?? null;
  }

  scrollTo({ left }) {
    this.scrollLeft = Number(left || 0);
  }
}

function harness() {
  const styles = new Map();
  const document = {
    head: {
      appendChild(node) {
        if (node.id) styles.set(node.id, node);
        return node;
      }
    },
    createElement() {
      return { id: "", textContent: "" };
    },
    getElementById(id) {
      return styles.get(String(id)) || null;
    }
  };
  const window = { RC: {} };
  const sandbox = {
    window,
    RC: window.RC,
    document,
    console,
    setTimeout,
    clearTimeout
  };
  vm.runInContext(SOURCE, vm.createContext(sandbox), {
    filename: "rc-flashcard.js"
  });
  return window.RC;
}

test("shared pager owns dot navigation and native scroll midpoint tracking", async () => {
  const RC = harness();
  const track = new FakeNode();
  const slides = [0, 1, 2].map((index) => {
    const slide = new FakeNode();
    slide.offsetLeft = index * 100;
    return slide;
  });
  const dots = [new FakeNode(), new FakeNode(), new FakeNode()];
  const changes = [];

  const pager = RC.flashcard.bindPager({}, {
    track,
    slides,
    dots,
    index: 0,
    onChange(next, previous, reason) {
      changes.push([next, previous, reason]);
    }
  });

  assert.equal(pager.index(), 0);
  assert.equal(dots[0].classList.contains("on"), true);
  dots[2].dispatch("click");
  assert.equal(track.scrollLeft, 200);
  assert.equal(pager.index(), 2);
  assert.deepEqual(changes, [[2, 0, "dot"]]);
  assert.equal(dots[2].getAttribute("aria-current"), "true");

  track.scrollLeft = 100;
  track.dispatch("scroll");
  await new Promise((resolve) => setTimeout(resolve, 110));
  assert.equal(pager.index(), 1);
  assert.deepEqual(changes.at(-1), [1, 2, "scroll"]);
  assert.equal(slides[1].classList.contains("on"), true);

  pager.destroy();
  track.scrollLeft = 0;
  track.dispatch("scroll");
  await new Promise((resolve) => setTimeout(resolve, 110));
  assert.equal(changes.length, 2, "destroyed pagers must not emit stale changes");
});

test("flashcard batches call the same pager controller and card selection uses body", () => {
  assert.match(
    SOURCE,
    /container\.__fcPager\s*=\s*bindPager\(container,\s*\{/
  );
  assert.equal(
    (SOURCE.match(/addEventListener\('scroll'/g) || []).length,
    1,
    "scroll midpoint logic must have one implementation"
  );
  assert.match(
    SOURCE,
    /RC\.voiceCard\.pinBind\([\s\S]*?selection,\s*result\.bd\s*\);/
  );
});

test("Anki 页面用确定网格行约束卡面并让固定底栏留在可视区", () => {
  assert.ok(
    VOICE_SOURCE.includes(
      ".vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare{display:flex;flex-direction:column;overflow:hidden}"
    ),
    "页面 placement 的 Anki body 必须始终成为纵向 flex 容器"
  );
  assert.ok(
    VOICE_SOURCE.includes(
      ".vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare>.fc-wrap{display:grid;grid-template-rows:minmax(0,1fr) auto;flex:1 1 auto;min-height:0}"
    ),
    "fc-wrap 必须以可收缩网格行明确分配卡面与圆点高度"
  );
  assert.ok(
    VOICE_SOURCE.includes(
      ".vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare>.fc-wrap>.fc-track{grid-row:1;min-height:0;height:auto;align-items:stretch;overflow-y:hidden}"
    ),
    "fc-track 必须被网格行约束且不能产生第二条纵向滚动链"
  );
  assert.ok(
    VOICE_SOURCE.includes(
      ".vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare>.fc-wrap>.fc-track>.fc-slide{height:auto;min-height:0;max-height:100%;align-self:stretch;overflow:hidden}"
    ),
    "Anki 滑页必须沿横向轨道交叉轴伸展而不能按内容继续增高"
  );
  assert.ok(
    VOICE_SOURCE.includes(
      ".vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare>.fc-wrap>.fc-track>.fc-slide>.fc-card{box-sizing:border-box;height:100%;max-height:100%;overflow:hidden}"
    ),
    "页面 Anki 的真实卡面必须被外壳可视高度约束，正文只进内部滚动区"
  );
  assert.ok(
    VOICE_SOURCE.includes(
      ".vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare>.fc-wrap>.fc-dots{grid-row:2}"
    ),
    "多卡圆点必须保留独立底行，不能覆盖卡面或评分栏"
  );
  assert.doesNotMatch(
    VOICE_SOURCE,
    /\.vc-card\.vc-page-placement\.vc-user-sized[^']*?\.fc-card\{[^}]*max-height:min\(46vh,300px\)/,
    "页面尺寸覆盖不能重新套用默认卡面高度上限"
  );
  assert.doesNotMatch(
    VOICE_SOURCE,
    /\.vc-card\.vc-page-placement\.vc-user-sized:not\(\.vc-dot\):not\(\.vc-min\)>\.vc-card-bd\.fc-bare/,
    "高度传递不能只对双击调整过尺寸的页面卡生效",
  );
});

test("Anki 三层滚动容器隐藏滚动槽但继续保留原生滚动", () => {
  assert.match(
    SOURCE,
    /\.fc-card,.fc-track,.vc-card>\.vc-card-bd\.fc-bare\{scrollbar-width:none\}/
  );
  assert.match(
    SOURCE,
    /\.fc-card::\-webkit-scrollbar,.fc-track::\-webkit-scrollbar,.vc-card>\.vc-card-bd\.fc-bare::\-webkit-scrollbar\{width:0;height:0;display:none\}/
  );
  assert.match(
    SOURCE,
    /\.fc-card\{[^}]*overflow-y:auto[^}]*\-webkit-overflow-scrolling:touch/
  );
  assert.match(
    SOURCE,
    /\.fc-track\{[^}]*overflow-x:auto[^}]*\-webkit-overflow-scrolling:touch/
  );
  assert.match(
    VOICE_SOURCE,
    /\.vc-card-bd\{[^}]*overflow-y:auto[^}]*\-webkit-overflow-scrolling:touch/
  );
  assert.doesNotMatch(
    SOURCE,
    /\.fc-card\{[^}]*overflow-y:hidden/
  );
  assert.doesNotMatch(
    SOURCE,
    /\.fc-track\{[^}]*overflow-x:hidden/
  );
});

import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const REVIEW = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-review.js", import.meta.url),
  "utf8",
);

function functionSource(name, nextName) {
  const startMarker = `  function ${name}(`;
  const endMarker = `\n  function ${nextName}(`;
  const start = REVIEW.indexOf(startMarker);
  const end = REVIEW.indexOf(endMarker, start);
  assert.notEqual(start, -1, `missing production function ${name}`);
  assert.notEqual(end, -1, `missing production boundary ${nextName}`);
  return REVIEW.slice(start, end).trim();
}

class Anchor {
  constructor(href) {
    this.href = String(href || "");
  }

  getAttribute(name) {
    return name === "href" ? this.href : null;
  }
}

class MaterialNode {
  constructor(href) {
    const hrefs = Array.isArray(href) ? href : [href];
    this.anchors = hrefs
      .filter((value) => value != null)
      .map((value) => new Anchor(value));
    this.removed = false;
    this.textContent = "";
  }

  querySelector(selector) {
    return selector === "a[href]" ? this.anchors[0] || null : null;
  }

  querySelectorAll(selector) {
    return selector === "a[href]" ? this.anchors.slice() : [];
  }

  remove() {
    this.removed = true;
  }
}

function attribute(attributes, name) {
  const match = new RegExp(
    `\\b${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)')`,
    "i",
  ).exec(attributes);
  return match ? match[1] ?? match[2] ?? "" : "";
}

function materialNodesFromHtml(html) {
  const nodes = [];
  const element = /<(div|span)\b([^>]*)>([\s\S]*?)<\/\1>/gi;
  for (const match of String(html || "").matchAll(element)) {
    const classes = attribute(match[2], "class").split(/\s+/);
    if (!classes.includes("url") && !classes.includes("src")) continue;
    const anchors = Array.from(
      match[3].matchAll(/<a\b([^>]*)>/gi),
      (anchor) => attribute(anchor[1], "href"),
    );
    nodes.push(new MaterialNode(anchors));
  }
  return nodes;
}

function htmlRoot(html) {
  const materials = materialNodesFromHtml(html);
  return {
    querySelectorAll(selector) {
      return selector === ".url,.src" ? materials : [];
    },
  };
}

function loadProductionHelpers() {
  const sourceFromMaterialNode = functionSource(
    "_sourceFromMaterialNode",
    "_legacyMaterialSource",
  );
  const legacyMaterialSource = functionSource(
    "_legacyMaterialSource",
    "_bookSourceRef",
  );
  const removeDisplayMetadata = functionSource(
    "_removeDisplayMetadata",
    "_foldSupplementary",
  );
  const sandbox = {
    URL,
    window: {
      location: { href: "https://reader.example/library/current" },
    },
    _htmlRoot: htmlRoot,
    _stripProvenance() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(
    `${sourceFromMaterialNode}
${legacyMaterialSource}
${removeDisplayMetadata}
globalThis.helpers = {
  sourceFromMaterialNode: _sourceFromMaterialNode,
  legacyMaterialSource: _legacyMaterialSource,
  removeDisplayMetadata: _removeDisplayMetadata
};`,
    sandbox,
  );
  return sandbox.helpers;
}

const helpers = loadProductionHelpers();

test("legacy .url and .src promote only strict safe PDF-view material links", () => {
  const urlCard = {
    question:
      '<span class="url"><a href="https://reader.example/pdf/view?' +
      'file=notes%2FLinear%20Algebra.md&page=17">来源</a></span>',
    answer: '<a href="https://example.test/ordinary-answer">普通答案链接</a>',
  };
  assert.deepEqual(
    { ...helpers.legacyMaterialSource(urlCard) },
    {
      source_ref: "book:notes/Linear Algebra.md#p17",
      source_url:
        "https://reader.example/pdf/view?" +
        "file=notes%2FLinear%20Algebra.md&page=17",
      file: "notes/Linear Algebra.md",
      page: 17,
    },
  );

  const srcCard = {
    question:
      '<span class="url"><a href="https://example.test/answer">' +
      "不是阅读器来源</a></span>",
    answer:
      '<div class="src"><a href="/pdf/view?' +
      'file=%E6%97%A5%E8%AF%AD%2F%E8%AF%AD%E6%B3%95.pdf&page=8">' +
      "原书</a></div>",
  };
  assert.deepEqual(
    { ...helpers.legacyMaterialSource(srcCard) },
    {
      source_ref: "book:日语/语法.pdf#p8",
      source_url:
        "https://reader.example/pdf/view?" +
        "file=%E6%97%A5%E8%AF%AD%2F%E8%AF%AD%E6%B3%95.pdf&page=8",
      file: "日语/语法.pdf",
      page: 8,
    },
  );
});

test("unrelated, malformed, and unsafe material URLs never become a source", () => {
  const rejected = [
    "https://example.test/answer",
    "https://reader.example/pdf/view?file=notes%2Fa.md",
    "https://reader.example/pdf/view?page=2",
    "https://reader.example/pdf/view?file=a.md&file=b.md&page=2",
    "https://reader.example/pdf/view?file=a.md&page=2&page=3",
    "https://reader.example/pdf/view?file=..%2Fsecret.md&page=2",
    "https://reader.example/pdf/view?file=%2Fetc%2Fpasswd&page=2",
    "https://reader.example/pdf/view?file=C%3A%2Fsecret.md&page=2",
    "https://reader.example/pdf/view?file=notes%2F%2Fsecret.md&page=2",
    "https://reader.example/pdf/view?file=notes%2Fa.md&page=2&mode=review",
    "https://reader.example/pdf/view?mode=review&file=notes%2Fa.md&page=2",
    "https://reader.example/pdf/view?file=%252e%252e%252fsecret.md&page=2",
    "https://reader.example/pdf/view?file=%25252e%25252e%25252fsecret.md&page=2",
    "https://user:secret@reader.example/pdf/view?file=a.md&page=2",
    "https://reader.example/pdf/view?file=a.md&page=2#fragment",
    "javascript:alert(1)",
  ];

  rejected.forEach((href) => {
    assert.equal(
      helpers.sourceFromMaterialNode(new MaterialNode(href)),
      null,
      href,
    );
  });

  assert.equal(
    helpers.legacyMaterialSource({
      question:
        '<a href="https://reader.example/pdf/view?' +
        'file=notes%2Fnaked.md&page=3">不在 .url/.src 内</a>',
      answer: '<a href="https://example.test/ordinary">答案链接</a>',
    }),
    null,
    "a valid-looking naked answer link is not legacy source material",
  );
});

test("one material container with multiple anchors is ambiguous and rejected", () => {
  const source = "https://reader.example/pdf/view?file=book%2Fa.md&page=3";
  const second = "https://reader.example/pdf/view?file=book%2Fa.md&page=3";
  assert.equal(
    helpers.sourceFromMaterialNode(new MaterialNode([source, second])),
    null,
  );

  assert.equal(
    helpers.legacyMaterialSource({
      question:
        '<span class="url">' +
        `<a href="${source}">来源一</a>` +
        `<a href="${second}">来源二</a>` +
        "</span>",
      answer: "答案",
    }),
    null,
  );
});

test("front and back legacy sources must resolve to one canonical book source", () => {
  const conflicting = {
    question:
      '<span class="url"><a href="/pdf/view?' +
      'file=books%2Fvolume-a.pdf&page=6">正面来源</a></span>',
    answer:
      '<span class="src"><a href="/pdf/view?' +
      'file=books%2Fvolume-b.pdf&page=6">背面来源</a></span>',
  };
  assert.equal(
    helpers.legacyMaterialSource(conflicting),
    null,
    "two distinct book source_ref values are ambiguous",
  );

  const repeatedCanonical = {
    question:
      '<span class="url"><a href="https://reader.example/pdf/view?' +
      'file=books%2FShared%20Source.pdf&page=6">正面来源</a></span>',
    answer:
      '<span class="src"><a href="/pdf/view?' +
      'page=6&file=books%2FShared+Source.pdf">背面来源</a></span>',
  };
  const promoted = helpers.legacyMaterialSource(repeatedCanonical);
  assert.equal(promoted.source_ref, "book:books/Shared Source.pdf#p6");
  assert.equal(promoted.file, "books/Shared Source.pdf");
  assert.equal(promoted.page, 6);
});

test("projection removes only proven material containers and preserves ordinary links", () => {
  const proven = new MaterialNode(
    "https://reader.example/pdf/view?file=book%2Fchapter.md&page=4",
  );
  const unrelatedUrlClass = new MaterialNode(
    "https://example.test/answer-reference",
  );
  const unsafe = new MaterialNode(
    "https://reader.example/pdf/view?file=..%2Fsecret.md&page=4",
  );
  const ordinaryAnswerLink = {
    href: "https://example.test/ordinary-answer",
    removed: false,
  };
  const root = {
    querySelectorAll(selector) {
      if (selector === ".url,.src") {
        return [proven, unrelatedUrlClass, unsafe];
      }
      return [];
    },
    ordinaryAnswerLink,
  };

  helpers.removeDisplayMetadata(root);

  assert.equal(proven.removed, true);
  assert.equal(unrelatedUrlClass.removed, false);
  assert.equal(unsafe.removed, false);
  assert.equal(
    ordinaryAnswerLink.removed,
    false,
    "ordinary answer anchors are outside the metadata-removal selector",
  );
});

test("legacy source discovery never mutates raw card question or answer", () => {
  const card = {
    id: 4815,
    question:
      '<div class="word">front</div><div class="url">' +
      '<a href="/pdf/view?file=books%2Foriginal.pdf&page=5">来源</a></div>',
    answer:
      '<div class="answer">back <a href="https://example.test/keep">' +
      "保留链接</a></div>",
  };
  const before = structuredClone(card);

  assert.equal(
    helpers.legacyMaterialSource(card).source_ref,
    "book:books/original.pdf#p5",
  );
  assert.deepEqual(card, before);
  assert.equal(card.question, before.question);
  assert.equal(card.answer, before.answer);
});

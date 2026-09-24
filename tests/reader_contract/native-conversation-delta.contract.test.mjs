import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const script = readFileSync(new URL('../../ios/BWReader/App/ReaderNativeConversationScript.swift',import.meta.url),'utf8');
function host() {
  const from = script.indexOf('function prepareMessageDelta(');
  const to = script.indexOf('// 选区变化',from);
  assert.ok(from > 0 && to > from);
  const context = vm.createContext({});
  vm.runInContext('let messageRevision=0, messageSignatures=new Map(), messageOrder=[], resetMessages=true;'+script.slice(from,to),context);
  return context;
}
const message = (id,text) => ({id,role:'assistant',text,streaming:false,parts:[]});
test('conversation batches send only changed messages and retain explicit ordering', () => {
  const context = host(), a=message('a','已有的大段内容'.repeat(10000)), b=message('b','新回复');
  const first = context.prepareMessageDelta([a,b]);
  assert.equal(first.reset,true);
  assert.equal(first.upserts.length,2);
  assert.equal(context.prepareMessageDelta([a,b]),null);
  const updated = {...b,text:'回复完成'};
  const next = context.prepareMessageDelta([a,updated]);
  assert.equal(next.baseRevision,first.revision);
  assert.equal(next.reset,false);
  assert.deepEqual(Array.from(next.upserts).map(x=>x.id),['b']);
  assert.ok(JSON.stringify(next).length < 1000,'unchanged long history crossed the bridge again');
  const reordered = context.prepareMessageDelta([updated,a]);
  assert.equal(reordered.upserts.length,0);
  assert.deepEqual(Array.from(reordered.order),['b','a']);
  const clear = context.prepareMessageDelta([]);
  assert.equal(clear.order.length,0);
  assert.equal(context.prepareMessageDelta([]),null);
});
test('explicit resync retransmits data even when bodies did not change', () => {
  const context = host(), a=message('a','保留');
  const first = context.prepareMessageDelta([a]);
  vm.runInContext('resetMessages=true;',context);
  const next = context.prepareMessageDelta([a]);
  assert.equal(next.reset,true);
  assert.equal(next.upserts.length,1);
  assert.ok(next.revision > first.revision);
});

test('native inline images do not inspect the hidden document or card renderer', () => {
  const from = script.indexOf('function inlineImageActions(');
  const to = script.indexOf('// 学习卡组', from);
  assert.ok(from > 0 && to > from);
  const context = vm.createContext({});
  vm.runInContext('let nativeMode=true;' + script.slice(from, to), context);
  // No RC, DOM, action registry or inspection callback exists in this host.
  assert.equal(JSON.stringify(context.inlineImageActions({}, null, null)), '{}');
});

test('plain assistant replies retain Markdown without hidden parsing, media, layout or reveal animation', () => {
  const source = readFileSync(new URL('../../_server_deploy/static/pdf/rc-assistant.js', import.meta.url), 'utf8');
  const events = [];
  const forbidden = () => assert.fail('native message touched web rendering');
  const context = vm.createContext({
    window: { __BW_NATIVE_CONVERSATION_DATA__: true, dispatchEvent: event => events.push(event.type) },
    document: { createElement: forbidden },
    CustomEvent: class { constructor(type) { this.type = type; } },
    md: forbidden, esc: forbidden, _linkifyPages: forbidden, _assetInline: forbidden,
    requestAnimationFrame: forbidden, setTimeout: forbidden,
  });
  vm.runInContext(source.slice(source.indexOf('function _nativeOwnsThread()'), source.indexOf('function _splitFollowups(text)')), context);
  vm.runInContext(source.slice(source.indexOf('function scrollDown(target)'), source.indexOf('function addMsg(cls, html)')), context);
  const node = { textContent: 'old rendered body', querySelector: forbidden, appendChild: forbidden };
  Object.defineProperty(node, 'innerHTML', { get: forbidden, set: forbidden });
  const body = '**bold** $x$ [link](https://example.com/) ![](image.png)\n'.repeat(1000);
  context.renderMd(node, body, false);
  assert.equal(node.__bwNativeMessageSource.text, body);
  assert.equal(node.__bwNativeMessageSource.streaming, true);
  assert.equal(node.textContent, '');
  context.renderMd(node, body, false);
  assert.equal(events.length, 1, 'unchanged text was re-published');
  context.renderMd(node, body, true);
  assert.equal(node.__bwNativeMessageSource.streaming, false);
  assert.equal(events.length, 2, 'completion must publish even when text is unchanged');
  context._appendCaret(node); context._streamWrap(node, 0); context._fadeInAfter(node); context.scrollDown(node);
  assert.match(source, /renderMd\(aMsg, _at, false\);[^\n]*\n\s*if \(_nativeOwnsThread\(\)\) \{ _stopReveal\(\); return; \}/);

  const project = vm.createContext({
    messageID: () => 'message', rc: () => ({}), flashGroup: () => null,
    cleanText: forbidden, text: value => value || '',
  });
  vm.runInContext(script.slice(script.indexOf('function projectMessage('), script.indexOf('// 找这组学习卡当前挂着的容器')), project);
  const message = project.projectMessage({
    __bwNativeMessageSource: node.__bwNativeMessageSource, getAttribute: () => '',
    classList: { contains: () => false }, querySelectorAll: () => [], querySelector: () => null,
  }, 0);
  assert.equal(message.text, body, 'native original text was truncated or replaced by DOM text');
  assert.equal(message.streaming, false);
});

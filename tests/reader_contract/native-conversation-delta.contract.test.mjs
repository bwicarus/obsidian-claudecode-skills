import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const script = readFileSync(new URL('../../ios/BWReader/App/ReaderNativeConversationScript.swift',import.meta.url),'utf8');
// 迁出 P4（2026-09-26）：侧栏消息全部由原生对话流给，投影脚本不再有消息源 / 增量协议
// （原 createMessageSources / prepareMessageDelta 的测试随代码一起删除）。留下的薄壳只做一件事：
// 网页直接写进对话区的提示在挂载时交给原生；其余挂载钩子不再有意义，也不得读 DOM。
function shimHost(feed) {
  const from=script.indexOf('const forwardedNotes = new WeakSet();'), to=script.indexOf('function registerAction(',from);
  assert.ok(from>0 && to>from);
  const posted=[];
  const context=vm.createContext({window:{__bwNativeConversationFeed:feed},handler:{postMessage:value=>posted.push(value)},
    cleanText:node=>node.textContent});
  vm.runInContext(script.slice(from,to),context);
  return {messages:context.window.__bwNativeMessages,posted};
}
const note=(text,turn='')=>({textContent:text,classList:{contains:name=>name==='asst-note'},getAttribute:name=>name==='data-turn'?turn:''});
test('P4 message shim forwards only direct notes, once, and only when the native feed owns the conversation',()=>{
  const {messages,posted}=shimHost(true);
  const warning=note('⚠ 语音:断开');
  messages.publish(warning,{id:'asst-thread'}); messages.publish(warning,{id:'asst-thread'});
  messages.replace(note('第二条'),warning,{id:'asst-thread'});
  messages.publish(note('轮次里的','turn-1'),{id:'asst-thread'});
  messages.publish({textContent:'普通回答',classList:{contains:()=>false},getAttribute:()=>''},{id:'asst-thread'});
  for (const name of ['remove','clear','stage','commit']) messages[name]({get children(){throw Error('DOM scan');}});
  assert.deepEqual(posted.map(x=>[x.type,x.text]),[['feed-note','⚠ 语音:断开'],['feed-note','第二条']]);
  const legacy=shimHost(undefined);
  legacy.messages.publish(note('旧界面'),{id:'asst-thread'});
  assert.equal(legacy.posted.length,0,'legacy web interface renders its own notes');
});
test('P4 snapshot carries no message projection or delta',()=>{
  const from=script.indexOf('      function snapshot() {'), to=script.indexOf('      function schedule() {',from);
  const body=script.slice(from,to);
  assert.ok(from>0 && to>from);
  for (const gone of ['messageDelta','projectMessage','messageSources','prepareMessageDelta','reviewSelections'])
    assert.equal(body.includes(gone),false,gone+' is still produced by the web snapshot');
  assert.match(body,/cardInputs: cardInputs\(\)/);
});

test('native artifact data retains full originals without rendering them for inspection or dropping', () => {
  const from = script.indexOf('function safeFields('), to = script.indexOf('// 找这组学习卡当前挂着的容器', from);
  const context = vm.createContext({});
  vm.runInContext(`let nativeMode=true;
    const actions=new Map();
    const text=(v,limit=32000)=>String(v??'').slice(0,limit);
    const artifact=(id,node,title,body)=>{actions.set(id,{});return {id,kind:'artifact',title,text:body||'',data:{},actionId:id};};
    const registerAction=(id)=>{actions.set(id,{});return id;};
    const window={BWReaderRuntime:{contextSelections:{isSelected:id=>id.endsWith('/item:2')}}};
    const rc=()=>({voiceCard:{mediaPresentation:()=>{throw new Error('hidden media projection was used');}}});
    ${script.slice(from,to)}`, context);
  const original={cid:'original',kind:'fact',title:'标题',data:{answer:'完整正文'.repeat(10000),detail:'详情'},sources:[{url:'https://example.com'}]};
  const node={querySelector:()=>null};
  const projected=context.projectPart({kind:'card',card:original},'part',node,'turn')[0];
  assert.equal(projected.text,'','web adapter must not repeat native subtitle/content projection');
  assert.equal(projected.data.answer,undefined);
  assert.equal(JSON.stringify(projected.data.nativeDetail.content),JSON.stringify(original));
  const updated={...original,data:{answer:'更新后的原文'}};
  const replaced=context.projectPart({kind:'card',card:original},'part',{__vcCard:updated},'turn')[0];
  assert.equal(replaced.data.nativeDetail.content.data.answer,'更新后的原文');
  const tool={kind:'tool',tool:'reader_card',status:'completed',result:{content:'实际回执'}};
  assert.equal(context.projectPart(tool,'tool',node,'turn')[0].data.nativeDetail.content.result.content,'实际回执');
  const media={cid:'media',kind:'images',data:{items:[{url:'https://example.com/a'}, {_gone:1}, {url:'https://example.com/c'}]}};
  const image=context.projectPart({kind:'card',card:media},'media',node,'turn')[0];
  assert.equal(image.data.items,undefined,'web created a second per-image control registry');
  assert.equal(vm.runInContext("Array.from(actions.keys()).some(id=>id.startsWith('media-image-'))",context),false);
  assert.equal(JSON.stringify(image.data.nativeDetail.content),JSON.stringify(media));
  assert.equal(projected.actionId,undefined,'native action ID must be issued by Swift');
  assert.equal(projected.data.nativeActionKey,'part');
  const operations={kind:'hlcard',nativePartID:'p-operations',file:'book',items:[{id:'h1',pdf_page:4,text:'原文'}]};
  const operation=context.projectPart(operations,'ops',{querySelector(){throw Error('hidden operation HTML read');}},'turn')[0];
  assert.equal(operation.kind,'operations');
  assert.equal(operation.data.nativeOperation.partID,'p-operations');
  assert.equal(operation.data.nativeDetail.content,operations);
  assert.equal(vm.runInContext('actions.size',context),0,'native originals kept hidden-node action closures');
});

test('operation controls use exact source identity, prevent duplicate mutation, and need no DOM', async()=>{
  const source=readFileSync(new URL('../../_server_deploy/static/pdf/rc-turncard.js',import.meta.url),'utf8');
  const start=source.indexOf('  var nativeOperationBusy ='),end=source.indexOf('  function markOp(',start);
  const item={id:'h1',pdf_page:4,undone:false}, part={kind:'hlcard',_nativeID:'p1',items:[item]}, turn={tid:'t1',parts:[part]};
  let finish,calls=0;
  const runtime={_lookup:id=>id==='t1'?turn:null,RC:{turnCard:{settle:async()=>{}}},
    opAction:async entry=>{calls++;await new Promise(resolve=>{finish=resolve;});entry.item.undone=true;return true;}};
  vm.runInNewContext(source.slice(start,end)+'globalThis.perform=performOperation;',runtime);
  const input={tid:'t1',partID:'p1',index:0,expectedID:'h1',expectedUndone:false,action:'toggle'};
  const pending=runtime.perform(input);
  await assert.rejects(runtime.perform(input),/正在保存/);
  finish();await pending;
  await assert.rejects(runtime.perform(input),/已变化/);
  assert.equal(calls,1);
});

test('multi-group turns resolve the requested learning identity rather than the first mounted group', () => {
  const from=script.indexOf('function flashGroup('),to=script.indexOf('function inlineImageSources(',from);
  const first={__fc:{gid:'first',cards:[{front:'第一组'}]}};
  const second={__fc:{gid:'second',cards:[{front:'第二组'}]}};
  const registered=new Map([['second',second]]);
  const context=vm.createContext({rc:()=>({flashcard:{containerOf:gid=>registered.get(gid)}})});
  vm.runInContext(script.slice(from,to),context);
  const node={querySelectorAll:()=>[first,second]};
  assert.equal(context.flashGroup(node,'second'),second);
  registered.clear();
  assert.equal(context.flashGroup(node,'second'),second);
  assert.equal(context.flashGroup(node,'missing'),null,'missing identity may not borrow another card group');
  assert.equal(context.flashGroup(node),first,'legacy unscoped inspection stays available');
});

test('native media operations need no hidden image cell and notify removal only after acknowledgement', async () => {
  const source=readFileSync(new URL('../../_server_deploy/static/pdf/rc-voicecall.js',import.meta.url),'utf8');
  const requests=[],events=[];
  let finish;
  const context=vm.createContext({window:{__BW_NATIVE_CONVERSATION_DATA__:true,
    __bwNativeContextSelections:{media(...args){requests.push(args);return new Promise(resolve=>{finish=resolve;});}},
    dispatchEvent:e=>events.push(e.type)},
    CustomEvent:class{constructor(type){this.type=type;}},
    _imgGoneNote:item=>events.push('removed:'+item.title), _pinSync(){}, _chipRender(){}});
  vm.runInContext(source.slice(source.indexOf('function _mediaItemAction('),source.indexOf('function _igWire(')),context);
  const card={cid:'media',kind:'images',data:{items:[{title:'one'}]}};
  const pending=context._mediaItemAction(null,card,0,'remove');
  assert.equal(card.data.items[0]._gone,undefined);
  assert.equal(events.length,0);
  finish();await pending;
  assert.equal(card.data.items[0]._gone,1);
  assert.deepEqual(events,['removed:one','rc:assistant-message-changed']);
  assert.throws(()=>context._mediaItemAction(null,card,0,'remove'),/已移除/);
  const changed={cid:'media2',kind:'images',data:{items:[{title:'old'}]}};
  const race=context._mediaItemAction(null,changed,0,'remove');
  changed.data.items[0]={title:'new'};finish();await assert.rejects(race,/已更新/);
  assert.equal(changed.data.items[0]._gone,undefined);
  assert.equal(events.length,2);
});

test('native media receipts notify once, keep offline retry possible, and never repeat a mutation', () => {
  const source=readFileSync(new URL('../../_server_deploy/static/pdf/rc-voicecall.js',import.meta.url),'utf8');
  const sent=[],RC={};
  const context=vm.createContext({RC});
  vm.runInContext(source.slice(source.indexOf('var _nativeMediaReceipts ='),source.indexOf('function _mediaItemAction(')),context);
  const receipt={id:'removal-1',cid:'media',index:2,action:'remove',item:{aid:'im_abcd',title:'原图'}};
  assert.equal(context._acceptNativeMediaReceipt(receipt),false,'missing notification port acknowledged a lost event');
  RC.voiceCtx={event:(...args)=>sent.push(args)};
  assert.equal(context._acceptNativeMediaReceipt(receipt),true);
  assert.equal(context._acceptNativeMediaReceipt(receipt),true);
  assert.equal(sent.length,1,'receipt retry notified twice');
  assert.equal(sent[0][0],'removed_imgs');
  assert.equal(sent[0][1].aid,'im_abcd');
  assert.equal(sent[0][2].mergeMs,800);
  assert.equal(receipt.item._gone,undefined,'notification rewrote the original event');
});

test('native semantic card mount does not render media, Markdown or start the web map engine', () => {
  const source=readFileSync(new URL('../../_server_deploy/static/pdf/rc-voicecall.js',import.meta.url),'utf8');
  const forbidden=()=>assert.fail('native media started hidden rendering');
  const context=vm.createContext({window:{__BW_NATIVE_CONVERSATION_DATA__:true},RC:{},
    _renderInflow(_root,options){assert.equal(options.text,'');return {el:{}};},
    _infoHtml:forbidden,_pinBind(){},_dragToDock(){},_upgradeMapCells:forbidden,injectCss(){}});
  vm.runInContext(source.slice(source.indexOf('function _igWire('),source.indexOf('window.__vcInfoCardEl =')),context);
  const card={cid:'image',kind:'images',data:{items:[{url:'https://example.com/image'}]}};
  assert.equal(context._infoCardEl(card).__vcCard,card);
});

test('committed native media context updates outgoing focus without creating a second selection or cancelling a covered child',()=>{
  const source=readFileSync(new URL('../../_server_deploy/static/pdf/rc-voicecall.js',import.meta.url),'utf8');
  const from=source.indexOf('function _pinAdoptNativeMedia('),to=source.indexOf('  try {\n    var _contextRegistry0',from);
  assert.ok(from>0&&to>from);
  const focus=[],cancel=[];
  const pins={map:{},els:{},ids:{},kinds:{},cidOf:{},cids:{}};
  const context=vm.createContext({_pins:pins,RC:{outgoing:{focus:(...args)=>focus.push(args),cancelKind:x=>cancel.push(x)}},_pinReproject(){}});
  vm.runInContext(source.slice(from,to),context);
  const item={id:'card:m/item:0',kind:'image-item',label:'figure',text:'original image context',source:{cid:'m',item:0}};
  let visible=true;
  const registry={toLegacy:()=>({items:visible?[item]:[],labels:visible?['figure']:[]}),get:()=>item,
    select(){assert.fail('native selection was repeated');},deselect(){assert.fail('covered native child was deselected');}};
  context._pinAdoptNativeMedia(registry);
  assert.equal(pins.ids.figure,'card:m/item:0');
  assert.equal(focus[0][0],'image');assert.equal(focus[0][1].cid,'m#0');
  context._pinAdoptNativeMedia(registry);assert.equal(focus.length,1);
  visible=false;context._pinAdoptNativeMedia(registry);
  assert.equal(Object.keys(pins.map).length,0);assert.equal(cancel.length,1);
  visible=true;context._pinAdoptNativeMedia(registry);assert.equal(focus.length,2);
  const whole={id:'card:m',kind:'card',label:'whole',text:'whole card',source:{cid:'m'},meta:{nativeOwner:true}};
  const parent={toLegacy:()=>({items:[whole],labels:['whole']}),get:()=>item};
  context._pinAdoptNativeMedia(parent);
  assert.equal(Object.keys(pins.map).length,1,'covered media remained in focus alongside the whole card');
  assert.equal(pins.ids.whole,'card:m');assert.equal(focus.at(-1)[0],'card');
  context._pinAdoptNativeMedia(registry);
  assert.equal(pins.ids.figure,'card:m/item:0');assert.equal(pins.ids.whole,undefined);
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

  const ref={session:'session',tid:'native-reply:request',revision:7};
  node.__bwNativeTurnRef=ref;node.__bwNativeTurnText=body;
  context.renderMd(node,body,true);
  assert.equal(node.__bwNativeTurnRef,ref,'same native body lost its source reference');
  context.renderMd(node,'连接中断，请重试',true);
  assert.equal(node.__bwNativeTurnRef,undefined,'transport error still pointed to the preceding successful answer');
});

// 迁出 P4b：复习回答的选择项改由原生生成（ReaderNativeReviewAnswers），与网页 rc-review
// `_presentationSelections` 是同一身份的两份副本 —— 格式与哈希漂移，选用就对不上旧回答。
test('P4b native review answer identity matches the web rc-review copy',()=>{
  const swift=readFileSync(new URL('../../ios/BWReader/App/ReaderNativeAssistantHistory.swift',import.meta.url),'utf8');
  const review=readFileSync(new URL('../../_server_deploy/static/pdf/rc-review.js',import.meta.url),'utf8');
  const hashSource=review.slice(review.indexOf('  function _hash(value) {'),review.indexOf('\n  }\n',review.indexOf('  function _hash(value) {'))+4);
  const context=vm.createContext({}); vm.runInContext(hashSource,context);
  // Swift: FNV-1a over UTF-16 code units, lowercase hex without padding.
  const fnv=value=>{let h=2166136261;for(const unit of Array.from({length:value.length},(_,i)=>value.charCodeAt(i))){h^=unit;h=Math.imul(h,16777619);}return (h>>>0).toString(16);};
  for (const sample of ['anki_card_1\n问\n答','native\n段落 😀','']) assert.equal(context._hash(sample),fnv(sample));
  assert.match(swift,/var h: UInt32 = 2_166_136_261/); assert.match(swift,/h = h &\* 16_777_619/); assert.match(swift,/value\.utf16/);
  assert.match(review,/'review-answer:' \+ _hash\(cardKey \+ '\\n' \+ question \+ '\\n' \+ text\)/);
  assert.match(swift,/"review-answer:" \+ Self\.hash\(cardKey \+ "\\n" \+ question \+ "\\n" \+ text\)/);
  assert.match(review,/answerId \+ ':part:' \+ index \+ ':' \+ _hash\('native\\n' \+ part\)/);
  assert.match(swift,/answerID \+ ":part:" \+ String\(\$0\.offset\) \+ ":" \+ Self\.hash\("native\\n" \+ \$0\.element\)/);
  for (const label of ['复习整条回答','复习回答段落 ']) { assert.ok(review.includes(label)); assert.ok(swift.includes(label)); }
});

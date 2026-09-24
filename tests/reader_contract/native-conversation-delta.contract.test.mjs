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

function sourceHost() {
  const from=script.indexOf('function createMessageSources()'),to=script.indexOf('const messageSources =',from);
  let scope='one';
  const context=vm.createContext({getScopeKey:()=>scope,messageID:node=>node.id,scheduleMessages(){},resetMessages:true,messagesDirty:true});
  vm.runInContext(script.slice(from,to),context);
  return {sources:context.createMessageSources(),changeScope:value=>{scope=value;}};
}
test('message lifecycle preserves late voice ordering without reading hidden DOM children',()=>{
  const {sources}=sourceHost(),thread={id:'asst-thread',get children(){throw Error('DOM scan');}},answer={id:'answer'},user={id:'user'};
  sources.publish(answer,thread);sources.publish(user,thread,answer);
  assert.deepEqual(Array.from(sources.sources(thread),node=>node.id),['user','answer']);
  const initial=sources.events(true);
  const delta=host().prepareMessageDelta([message('user','问'),message('answer','答')],initial);
  assert.equal(delta.contract,'reader-native-conversation-delta/2');
  assert.equal(delta.order,undefined);
  sources.remove(user);
  assert.equal(sources.events(false)[0].action,'remove');
  assert.deepEqual(Array.from(sources.sources(thread),node=>node.id),['answer']);
});
test('history adoption preserves a live response and cancellation preserves active messages',()=>{
  const {sources,changeScope}=sourceHost(),thread={id:'asst-thread'},stage={},old={id:'old'},live={id:'live'},replay={id:'live'},past={id:'past'};
  sources.publish(old,thread);sources.publish(live,thread);sources.stage(stage);
  sources.publish(past,stage);sources.publish(replay,stage);
  assert.equal(sources.sources(thread).length,2,'uncommitted history replaced visible messages');
  sources.replace(live,replay,stage);sources.commit(stage);sources.clear(stage);
  const moves=sources.events(false);
  assert.equal(moves.some(event=>event.action==='remove' && event.id==='live'),false,'discarded replay removed its live replacement');
  assert.deepEqual(Array.from(sources.sources(thread),node=>node.id),['past','live']);
  const aborted={};sources.stage(aborted);sources.publish({id:'never'},aborted);sources.clear(aborted);
  assert.deepEqual(Array.from(sources.sources(thread),node=>node.id),['past','live']);
  sources.publish({id:'nested'},{id:'internal-part'});
  assert.equal(sources.events(true).some(event=>event.id==='nested'),false,'nested component became a conversation');
  const stale={};sources.stage(stale);sources.publish({id:'old-account'},stale);
  changeScope('two');sources.publish({id:'new'},thread);sources.commit(stale);
  assert.deepEqual(Array.from(sources.sources(thread),node=>node.id),['new']);
});
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

test('native source references eliminate round-trip bodies while retaining live operation handles', () => {
  const context=host();
  const original='完整内容😀'.repeat(20000);
  const source={id:'turn',nativeTurnRef:{session:'session',tid:'turn',revision:8},role:'assistant',text:original,streaming:false,
    parts:[{id:'tool',kind:'tool',actionId:'inspect-tool',data:{nativeTurnPart:{id:'p1'},nativeDetail:{kind:'tool',content:{result:original}}}},
      {id:'card',kind:'anki',data:{nativeTurnPart:{id:'p2',cardIndex:2},nativeDetail:{content:{card:{front:original}}},
        nativeCardActions:{add:'save-card'},dragId:'drag-card'}},
      {id:'edited',kind:'images',data:{nativeDetail:{content:{cid:'media',data:{items:[{_gone:1}]}}}}}]};
  const first=context.prepareMessageDelta([source]);
  assert.ok(JSON.stringify(first).length<1400,'native originals made another trip through WebKit');
  const sent=first.upserts[0];
  assert.equal(sent.text,undefined);
  assert.equal(sent.parts[0].data.nativeDetail,undefined);
  assert.equal(sent.parts[1].data.nativeCardActions.add,'save-card');
  assert.equal(sent.parts[1].data.nativeTurnPart.cardIndex,2);
  assert.equal(sent.parts[2].data.nativeDetail.content.data.items[0]._gone,1,'uncommitted media state was replaced with an old original');
  assert.equal(source.parts[0].data.nativeDetail.content.result,original,'packing mutated source used by legacy inspection');
  assert.equal(context.prepareMessageDelta([source]),null);
  const next=context.prepareMessageDelta([{...source,nativeTurnRef:{...source.nativeTurnRef,revision:9}}]);
  assert.equal(next.upserts.length,1,'native text-only update did not refresh presentation');
});

test('native artifact data retains full originals without rendering them for inspection or dropping', () => {
  const from = script.indexOf('function safeFields('), to = script.indexOf('function projectMessage(', from);
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

  const project = vm.createContext({
    nativeMode:true, window:{},
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
  const ref={session:'session',tid:'native-reply:request',revision:7};
  node.__bwNativeTurnRef=ref;node.__bwNativeTurnText=body;
  context.renderMd(node,body,true);
  assert.equal(node.__bwNativeTurnRef,ref,'same native body lost its source reference');
  context.renderMd(node,'连接中断，请重试',true);
  assert.equal(node.__bwNativeTurnRef,undefined,'transport error still pointed to the preceding successful answer');
  const hidden=project.projectMessage({__bwNativeMessageHidden:true},0);
  assert.equal(hidden,null,'tool takeover repeated the preceding plain reply');
});

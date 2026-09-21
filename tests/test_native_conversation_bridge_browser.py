"""Offline Chromium integration of the injected projection and real turnCard.

No account, voice session, remote request or card write is performed. Card widget
renderers and the existing transport are controlled fixtures; turn reconciliation
and the existing assistant's voice-routing branch run from repository sources.
"""
import json
from pathlib import Path
import sys
import unittest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'extensions/bw-reader-webext'))
from browser_exe import CHROME


class NativeConversationBridgeBrowser(unittest.TestCase):
    def test_native_review_uses_original_queue_and_semantic_answer_selection(self):
        bridge = (ROOT / 'ios/BWReader/App/ReaderNativeConversationScript.swift').read_text(encoding='utf-8').split('#"""', 1)[1].rsplit('"""#', 1)[0]
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(CHROME), headless=True)
            page = browser.new_page()
            page.route('**/*', lambda r: r.fulfill(status=200, body='<html></html>') if r.request.url == 'http://reader.test/' else r.abort())
            page.goto('http://reader.test/')
            page.set_content('<div id="side-pane-asst"><div id="asst-quick"></div><div id="asst-thread"></div></div>')
            page.evaluate('''() => {
              window.receipts=[];window.writes=[];window.__asstSend=()=>{};
              window.webkit={messageHandlers:{bwNativeConversation:{postMessage:x=>receipts.push(x)}}};
              window.RC={adapter:()=>({getContext:()=>({file:'native.pdf',page:1})}),toast:()=>{},
                turnCard:{presentationOf:id=>({role:'assistant',streaming:false,parts:[{kind:'text',text:'第一段\\n\\n第二段'}]})},
                assistant:{setMode:mode=>{document.getElementById('side-pane-asst').dataset.assistantMode=mode;window.dispatchEvent(new Event('rc:assistant-mode-changed'));}}};
              window.__bwExtensionStore={get:async()=>null,set:async()=>true};
              window.fetch=async(url,opts)=>{
                writes.push({url,body:opts?.body});
                return {ok:true,json:async()=>({ok:true,context_key:'server-context',due_total:1,related_total:1,cards:[{id:41,question:'完整问题',answer:'完整答案',entity_id:'original-entity'}]})};
              };
            }''')
            page.add_script_tag(path=str(ROOT / '_server_deploy/static/reader-runtime/context-selection-registry.js'))
            page.add_script_tag(path=str(ROOT / '_server_deploy/static/pdf/rc-review.js'))
            page.add_script_tag(content=bridge)
            page.evaluate('__bwNativeConversation.setNativeMode(true)')
            page.wait_for_timeout(150)
            scope = page.evaluate('receipts.at(-1).scope')
            self.assertTrue(page.evaluate('(scope)=>__bwNativeConversation.perform({action:"openReview",scope})', scope)['ok'])
            page.wait_for_function('receipts.at(-1)?.review?.current?.id === "anki_card_41"')
            self.assertFalse(page.evaluate('receipts.at(-1).legacyVisible'))
            def act(key, **values):
                return page.evaluate('''({key,values})=>{const latest=receipts.at(-1);return __bwNativeConversation.perform({action:'reviewAction',scope:latest.scope,value:{key,contextKey:latest.review.contextKey,cardId:latest.review.current?.id||'',...values}});}''', dict(key=key, values=values))
            self.assertTrue(act('reveal')['ok'])
            page.wait_for_function('receipts.at(-1).review.showingAnswer')
            self.assertTrue(act('rate', ease=3)['ok'])
            page.wait_for_function('receipts.at(-1).review.canUndo')
            self.assertEqual(page.evaluate('writes.filter(x=>x.url.endsWith("review-answer")).length'), 0)
            self.assertTrue(act('undo')['ok'])
            page.wait_for_function('receipts.at(-1).review.current?.id === "anki_card_41"')
            page.evaluate('''() => {
              const answer=document.createElement('div');answer.className='rc-turn asst-a';answer.dataset.turn='answer-1';
              document.getElementById('asst-thread').appendChild(answer);
              __bwNativeConversation.snapshot();
            }''')
            page.wait_for_function('receipts.at(-1).messages[0]?.reviewSelections?.length === 3')
            choice = page.evaluate('receipts.at(-1).messages[0].reviewSelections[1].id')
            self.assertTrue(act('selectAnswer', selectionId=choice)['ok'])
            page.wait_for_function('receipts.at(-1).review.selectedPairs[0]?.answer === "第一段"')
            self.assertFalse(page.evaluate('receipts.at(-1).legacyVisible'))
            stale = act('source', contextKey='stale-context')
            self.assertFalse(stale['ok'])
            browser.close()

    def test_native_settings_preserve_catalog_and_confirmed_writes(self):
        bridge = (ROOT / 'ios/BWReader/App/ReaderNativeConversationScript.swift').read_text(encoding='utf-8').split('#"""', 1)[1].rsplit('"""#', 1)[0]
        source = (ROOT / '_server_deploy/static/pdf/rc-assistant.js').read_text(encoding='utf-8')
        computer = (ROOT / '_server_deploy/static/pdf/rc-computer-voice.js').read_text(encoding='utf-8')
        computer_settings = computer[computer.index('  async function readSettingsState()'):computer.index('  function mountSettings(container)')]
        end = source.index('})();', source.index('  RC.assistant =')) + len('})();')
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(CHROME), headless=True)
            page = browser.new_page()
            page.route('**/*', lambda r: r.fulfill(status=200, body='<html></html>') if r.request.url == 'http://reader.test/' else r.abort())
            page.goto('http://reader.test/')
            page.set_content('<div id="side-pane-asst"><div id="asst-thread"></div></div>')
            page.evaluate('''() => {
              window.receipts=[];window.writes=[];window.rejectSave=false;window.voice={rt_engine:'openai_rtc',rt_voice:'cedar',rt_tool_reply:true};
              window.webkit={messageHandlers:{bwNativeConversation:{postMessage:x=>receipts.push(x)}}};
              window.RC={turnCard:{}};window.__asstSend=()=>{};
              window.preferences={ok:true,actions:{explain:{pref:{backend:'codex',variant:'verified',depth:'high'},default:{backend:'codex',variant:'verified',depth:'low'}}},names:{explain:'解释'},locked:{},
                catalog:{backends:['codex'],variants:{codex:['verified','blocked']},depths:{codex:['low','high']},codex_depths_by_model:{verified:['low','high']},codex_capabilities:{verified:{selectable:true,fast:true},blocked:{selectable:false}}}};
              window.fetch=async (url,opts)=>{
                let result;
                if (opts?.method==='POST') {
                  const body=JSON.parse(opts.body);writes.push({url,body});
                  if (rejectSave) result={ok:false,error:'保存失败测试'};
                  else if (url.endsWith('/action-pref')) result={ok:true,pref:body.backend?body:null};
                  else if (url.endsWith('/voice-config')) {Object.assign(voice,body);result={ok:true,cfg:voice};}
                  else result={ok:true,profiles:[body.name],active:body.name};
                } else if (url.endsWith('/action-prefs')) result=preferences;
                else if (url.endsWith('/voice-config')) result={ok:true,cfg:voice};
                else result={ok:true,profiles:['原方案'],active:'原方案'};
                return {ok:true,json:async()=>JSON.parse(JSON.stringify(result))};
              };
            }''')
            page.add_script_tag(content=source[:end])
            page.evaluate('''() => {
              window.target='codex-desktop';window.targetBusy=false;window.targetWrites=[];window.connectionFails=false;
              window.computerTargetLoaded=true;window.bridgeVoiceEnabledKnown=true;window.bridgeVoiceEnabled=true;
              window.lastClientFailure={code:'RECENT_ERROR',message:'可复制的连接错误',at:'now'};
              window.loadComputerTarget=async()=>target;
              window.getComputerTarget=()=>target;
              window.computerTargetBusy=()=>targetBusy;
              window.statusReasonMessage=x=>x;
              window.availability=async()=>{if(connectionFails)throw new Error('连接离线测试');return {state:'ready',status:{codexVoice:{status:'available',active:false}}};};
              RC.computerVoice={setTargetApp:async value=>{
                if(targetBusy)throw new Error('请先结束当前电脑语音');
                targetWrites.push(value);target=value;return target;
              }};
            }''')
            page.add_script_tag(content=computer_settings)
            page.evaluate('RC.computerVoice.readSettingsState=readSettingsState')
            page.add_script_tag(content=bridge)
            page.wait_for_timeout(100)
            scope = page.evaluate('receipts[receipts.length-1].scope')
            def command(action, **values):
                return page.evaluate('(c)=>__bwNativeConversation.perform(c)', dict(action=action, scope=scope, **values))
            self.assertIn('nativeSettings', page.evaluate('receipts[receipts.length-1].capabilities'))
            models = command('settingsRead', section='models')
            self.assertTrue(models['ok'], models)
            self.assertEqual(models['value']['actions']['explain']['pref']['depth'], 'high')
            blocked = command('settingsWrite', section='models', value={'action':'explain','backend':'codex','variant':'blocked','depth':'low','fast':False})
            self.assertFalse(blocked['ok'])
            self.assertEqual(page.evaluate('writes'), [])
            valid = {'action':'explain','backend':'codex','variant':'verified','depth':'low','fast':True}
            self.assertTrue(command('settingsWrite', section='models', value=valid)['ok'])
            voice = command('settingsRead', section='voice')['value']
            self.assertEqual(next(f for f in voice['fields'] if f['key']=='rt_voice')['value'], 'cedar')
            self.assertTrue(next(f for f in voice['fields'] if f['key']=='rc-voice-cue')['disabled'])
            self.assertFalse(command('settingsWrite', section='voice', key='rt_speed', value=9)['ok'])
            self.assertFalse(command('settingsWrite', section='voice', key='rc-voice-cue', value=True, device=True)['ok'])
            self.assertTrue(command('settingsWrite', section='voice', key='rt_voice', value='marin')['ok'])
            self.assertEqual(page.evaluate('voice.rt_voice'), 'marin')
            self.assertTrue(command('settingsWrite', section='voice', key='rc-voice-sub', value=False, device=True)['ok'])
            self.assertEqual(page.evaluate("localStorage.getItem('rc-voice-sub')"), '0')
            self.assertFalse(command('settingsWrite', section='voice', key='openai_api_key', value='not-allowed')['ok'])
            self.assertTrue(command('settingsWrite', section='profiles', op='save', name='新方案')['ok'])
            computer_state = command('settingsRead', section='computer')['value']
            self.assertEqual(computer_state['target'], 'codex-desktop')
            self.assertEqual(computer_state['clientError']['code'], 'RECENT_ERROR')
            self.assertFalse(computer_state['status']['codexVoice']['active'])
            self.assertEqual(page.evaluate('targetWrites'), [])
            self.assertFalse(command('settingsWrite', section='computer', value='unknown')['ok'])
            page.evaluate('targetBusy=true')
            self.assertFalse(command('settingsWrite', section='computer', value='chatgpt-classic')['ok'])
            self.assertEqual(page.evaluate('targetWrites'), [])
            page.evaluate('targetBusy=false')
            self.assertTrue(command('settingsWrite', section='computer', value='chatgpt-classic')['ok'])
            page.evaluate('connectionFails=true')
            computer_state = command('settingsRead', section='computer')['value']
            self.assertEqual(computer_state['target'], 'chatgpt-classic')
            self.assertIsNone(computer_state['status'])
            self.assertEqual(computer_state['errors'], ['连接离线测试'])
            page.evaluate('rejectSave=true')
            failed = command('settingsWrite', section='models', value=valid)
            self.assertFalse(failed['ok'])
            self.assertIn('保存失败', failed['error'])
            self.assertFalse(page.evaluate('receipts[receipts.length-1].legacyVisible'))
            browser.close()

    def test_projection_commands_and_lifecycle(self):
        source = (ROOT / 'ios/BWReader/App/ReaderNativeConversationScript.swift').read_text(encoding='utf-8').split('#"""', 1)[1].rsplit('"""#', 1)[0]
        assistant = (ROOT / '_server_deploy/static/pdf/rc-assistant.js').read_text(encoding='utf-8')
        existing_send = assistant[assistant.index('  async function send(text, opts) {'):assistant.index('  window.__asstSend = send;')]
        voice = (ROOT / '_server_deploy/static/pdf/rc-voicecall.js').read_text(encoding='utf-8')
        existing_voice_send = voice[voice.index('  window.__vcSendText = function (text) {'):voice.index('  function _rtcShimWs()')]
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(CHROME), headless=True)
            page = browser.new_page(viewport={'width': 900, 'height': 650})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', lambda route: route.abort())
            page.set_content('''<title>测试书</title><style>body.grammar-open #main{padding-right:320px}</style>
              <div id="main">书页</div><aside id="ep-side"><div id="ep-side-tabs">
              <button class="ep-side-tab active" data-pane="toc">目录</button><button class="ep-side-tab" data-pane="asst">助手</button></div>
              <div id="side-pane-asst" data-assistant-mode="normal"><div id="asst-thread"></div>
              <div id="asst-input"><textarea id="asst-ta"></textarea><button id="asst-send">发送</button>
              <button id="asst-call"></button><button id="asst-computer"></button></div></div></aside>''')
            page.evaluate('''() => {
              window.receipts=[];window.calls=[];window.accountID='a';window.accountListeners=[];
              window.webkit={messageHandlers:{bwNativeConversation:{postMessage:x=>receipts.push(x)}}};
              window.BWReaderRuntime={accountContext:{snapshot:()=>({contextId:'ctx',namespace:accountID,active:true}),subscribe:f=>{accountListeners.push(f);return ()=>{};}}};
              window.__asstHistUrl=()=>'/private/history?account='+accountID;window.__asstBusy=()=>false;
              window.__asstSend=t=>calls.push(['existing-send',t]);
              window.RC={assistant:{renderMd:(el,t)=>el.textContent=t,reloadHistory:()=>calls.push(['history']),openModelSettings:()=>calls.push(['models'])},
                toolChip:{flowBtn:fn=>{let b=document.createElement('button');b.onclick=fn;return b;}},
                sidedrawer:{_open:false,isOpen(){return this._open},setTab(tab){document.querySelectorAll('.ep-side-tab').forEach(n=>n.classList.toggle('active',n.dataset.pane===tab));},
                  open(tab){this._open=true;this.setTab(tab);document.body.classList.add('grammar-open');},close(){this._open=false;document.body.classList.remove('grammar-open');}},
                flashcard:{renderEntity(node,data){let group=document.createElement('div');group.__fcActive=data.cards.map((_,i)=>i);group.__fcPager={goto:i=>calls.push(['card-index',i])};node.appendChild(group);}}
              };
              window.__vcInfoCardEl=card=>{let el=document.createElement('div');el.className='vc-card';el.__vcCard=card;el.innerHTML='<div class="vc-card-hd"></div><div class="vc-card-bd"></div>';el.querySelector('.vc-card-hd').textContent=card.title;el.querySelector('.vc-card-bd').textContent=card.data.answer||card.data.text||'复杂生成物';return el;};
              document.querySelector('#asst-call').onclick=()=>{calls.push(['toggle-realtime']);document.querySelector('#asst-call').classList.toggle('on');};
              document.querySelector('#asst-computer').onclick=()=>{calls.push(['toggle-computer']);document.querySelector('#asst-computer').classList.toggle('connecting');};
              document.querySelector('#asst-send').onclick=()=>calls.push(['stop']);
            }''')
            page.add_script_tag(path=str(ROOT / '_server_deploy/static/pdf/rc-turncard.js'))
            page.add_script_tag(content=source)
            def snapshot():
                page.wait_for_timeout(90)
                return page.evaluate('receipts[receipts.length-1]')
            first = snapshot()
            self.assertTrue(first['ready'])
            self.assertIn('openTOC', first['capabilities'])
            self.assertFalse(page.evaluate("document.documentElement.classList.contains('bw-native-conversation-active')"))
            page.evaluate('__bwNativeConversation.setNativeMode(true)')
            self.assertNotEqual(page.locator('#ep-side').evaluate('(n)=>getComputedStyle(n).visibility'), 'hidden')
            self.assertEqual(page.locator('#main').evaluate('(n)=>getComputedStyle(n).paddingRight'), '0px')
            self.assertFalse(page.evaluate('RC.sidedrawer.isOpen()'))
            self.assertFalse(snapshot()['sidebarOpen'])
            page.evaluate('__bwNativeConversation.perform({action:"toggleAssistant"})')
            self.assertTrue(snapshot()['sidebarOpen'])
            self.assertEqual(page.locator('#main').evaluate('(n)=>getComputedStyle(n).paddingRight'), '0px')
            self.assertEqual(page.locator('#ep-side').evaluate('(n)=>getComputedStyle(n).visibility'), 'hidden')
            page.evaluate("RC.turnCard.draftText('user:u','我的','user','u-item','runner')")
            user = snapshot()['messages'][0]
            # A web renderer may shorten/reformat the bubble. Native speech
            # still follows the semantic draft, including its Markdown.
            page.evaluate("document.querySelector('[data-turn=\"user:u\"] .rc-part-text').textContent='错误网页文字'")
            self.assertEqual(snapshot()['messages'][0]['text'], '我的')
            page.evaluate("RC.turnCard.draftText('user:u','我的问题','user','u-item','runner');RC.turnCard.freezeDraft('user:u','u-item','runner','user')")
            final_user = snapshot()['messages'][0]
            self.assertEqual(user['id'], final_user['id'])
            self.assertEqual(final_user['text'], '我的问题')
            self.assertTrue(user['streaming'])
            self.assertFalse(final_user['streaming'])
            page.evaluate("RC.turnCard.draftText('a','回答','assistant','a-item','runner')")
            reply = snapshot()['messages'][1]
            page.evaluate("RC.turnCard.draftText('a','**回答** [来源](https://example.org) $x^2$','assistant','a-item','runner');RC.turnCard.freezeDraft('a','a-item','runner','assistant')")
            self.assertEqual(reply['id'], snapshot()['messages'][1]['id'])
            self.assertIn('**回答**', snapshot()['messages'][1]['text'])
            page.evaluate('''() => {
              RC.turnCard.addPart('a',{kind:'tool',tool:'reader_card',status:'completed',steps:[{status:'done'},{status:'error'},{status:'running'},{}],args:{secret:'not-prose'},result:'private tool body'});
              RC.turnCard.addPart('a',{kind:'card',card:{kind:'fact',cid:'fact1',title:'知识卡',data:{answer:'解答',detail:'详细说明'}}});
              RC.turnCard.addPart('a',{kind:'cards',gid:'card_abcd',draft:false,cards:[{front:'一',back:'壱'},{front:'二',back:'弐'}]});
              RC.turnCard.addPart('a',{kind:'card',card:{kind:'html',cid:'html1',title:'交互图',data:{html:'<button>操作</button>'}}});
            }''')
            rich = snapshot()['messages'][1]
            self.assertNotIn('reader_card', rich['text'])
            self.assertNotIn('private tool body', json.dumps(rich))
            tool = next(part for part in rich['parts'] if part['kind'] == 'tool')
            self.assertEqual([tool['data'][key] for key in ['stepCount','successCount','failureCount','runningCount']], [4,1,1,1])
            inspected = page.evaluate('(command)=>__bwNativeConversation.perform(command)', {
                'action':'inspectArtifact','scope':snapshot()['scope'],'actionId':tool['actionId']})
            self.assertTrue(inspected['ok'])
            self.assertEqual(inspected['detail']['content']['result'], 'private tool body')
            self.assertFalse(snapshot()['legacyVisible'])
            self.assertFalse(page.evaluate('(command)=>__bwNativeConversation.perform(command)', {
                'action':'inspectArtifact','scope':'stale','actionId':tool['actionId']})['ok'])
            self.assertEqual(next(part for part in rich['parts'] if part['kind']=='fact')['data']['detail'], '详细说明')
            page.evaluate('''() => {
              let quote=document.createElement('div');quote.className='asst-ctx-card';quote.textContent='选择引用不应混入用户发言';
              document.querySelector('[data-turn="user:u"]').appendChild(quote);
            }''')
            quoted = snapshot()['messages'][0]
            self.assertEqual(quoted['text'], '我的问题')
            self.assertTrue(any(part['text']=='选择引用不应混入用户发言' and part['actionId'] for part in quoted['parts']))
            cards = [part for part in rich['parts'] if part['kind']=='anki']
            self.assertEqual(len(cards), 2)
            self.assertTrue(all(part['actionId'] for part in rich['parts']))
            page.evaluate('(id)=>__bwNativeConversation.perform({action:"openArtifact",actionId:id})', cards[1]['actionId'])
            self.assertIn(['card-index', 1], page.evaluate('calls'))
            self.assertTrue(snapshot()['legacyVisible'])
            page.evaluate('__bwNativeConversation.perform({action:"hideLegacy"})')
            page.evaluate('(id)=>__bwNativeConversation.perform({action:"openArtifact",actionId:id})', tool['actionId'])
            self.assertTrue(page.locator('.rc-turn-flow').last.is_visible())
            self.assertEqual(page.evaluate("RC.turnCard.partsOf('a').find(p=>p.kind==='tool').result"), 'private tool body')
            page.evaluate('__bwNativeConversation.perform({action:"hideLegacy"});__bwNativeConversation.perform({action:"toggleVoice"})')
            self.assertEqual(snapshot()['voice'], {'mode':'realtime','active':True,'busy':False,'label':'通话中'})
            page.evaluate('__bwNativeConversation.perform({action:"toggleVoice"});__bwNativeConversation.perform({action:"toggleComputerVoice"})')
            self.assertTrue(snapshot()['voice']['busy'])
            page.evaluate('__bwNativeConversation.perform({action:"toggleComputerVoice"})')
            # The exact existing assistant send function and voice text dispatch
            # run locally, while transport side effects are replaced by receipts.
            page.evaluate('''() => { let streaming=false,_clearing=false,_assistantMode='normal',_modeEpoch=1;
              const _chatUrl=()=>'/not-used',pane=document.querySelector('#side-pane-asst');
              const ctx=()=>{throw Error('unexpected text pipeline')};
              ''' + existing_send + '''window.__asstSend=send;
              let _rtc={on:false},ws=null,mode='s2s';const capUser=()=>{};
              const _pcTypedSend=t=>{calls.push(['pc-typed-send',t]);return true;};
              ''' + existing_voice_send + '''}''')
            accepted = page.evaluate('__bwNativeConversation.perform({action:"send",text:"继续解释"})')
            self.assertTrue(accepted['ok'])
            page.wait_for_timeout(90)
            self.assertEqual(page.evaluate("calls.filter(x=>x[0]==='pc-typed-send')"), [['pc-typed-send','继续解释']])
            page.evaluate('''() => {
              window.__asstSend=t=>calls.push(['text-session',t]);
              window.__vcSendText=t=>{calls.push(['voice-session',t]);return false;};
              document.querySelector('#asst-call').classList.add('on');
            }''')
            refused = page.evaluate('__bwNativeConversation.perform({action:"send",text:"通话失败不可串文字"})')
            self.assertFalse(refused['ok'])
            self.assertEqual(page.evaluate("calls.filter(x=>x[0]==='text-session')"), [])
            page.evaluate("document.querySelector('#asst-call').classList.add('connecting')")
            refused = page.evaluate('__bwNativeConversation.perform({action:"send",text:"等待语音"})')
            self.assertFalse(refused['ok'])
            page.evaluate("document.querySelector('#asst-call').classList.remove('connecting');document.querySelector('#side-pane-asst').dataset.assistantMode='review';dispatchEvent(new Event('rc:assistant-mode-changed'))")
            self.assertEqual(snapshot()['conversationMode'], 'review')
            self.assertTrue(page.evaluate('__bwNativeConversation.perform({action:"send",text:"独立复习文字"})')['ok'])
            self.assertEqual(page.evaluate("calls.filter(x=>x[0]==='text-session')"), [['text-session','独立复习文字']])
            old_scope = snapshot()['scope']
            page.evaluate("accountID='b';accountListeners.forEach(f=>f())")
            switched = snapshot()
            self.assertNotEqual(old_scope, switched['scope'])
            self.assertEqual(switched['messages'], [])
            self.assertFalse(page.evaluate('(scope)=>__bwNativeConversation.perform({action:"send",text:"旧消息",scope})', old_scope)['ok'])
            page.evaluate("RC.turnCard.reset();document.querySelector('#asst-thread').replaceChildren();RC.turnCard.draftText('new-account','新账户消息','user')")
            self.assertEqual(snapshot()['messages'][0]['text'], '新账户消息')
            page.evaluate("dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}))")
            self.assertFalse(page.evaluate('__bwNativeConversation.perform({action:"send",text:"旧页面"})')['ok'])
            page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}))")
            snapshot()
            page.evaluate("RC.turnCard.draftText('new-account','恢复后继续','user')")
            self.assertEqual(snapshot()['messages'][0]['text'], '恢复后继续')
            page.evaluate("RC.sidedrawer.setTab('toc');RC.sidedrawer.close();__bwNativeConversation.setNativeMode(false)")
            self.assertFalse(page.evaluate('RC.sidedrawer.isOpen()'))
            self.assertEqual(page.locator('.ep-side-tab.active').get_attribute('data-pane'), 'toc')
            self.assertNotEqual(page.locator('#ep-side').evaluate('(n)=>getComputedStyle(n).visibility'), 'hidden')
            self.assertEqual(errors, [])
            browser.close()


    def test_original_card_drag_closed_delivery_and_selection(self):
        source = (ROOT / 'ios/BWReader/App/ReaderNativeConversationScript.swift').read_text(encoding='utf-8').split('#"""', 1)[1].rsplit('"""#', 1)[0]
        figures = (ROOT / '_server_deploy/static/pdf/reader.src/26-figures.js').read_text(encoding='utf-8')
        focus = figures[figures.index('  window.__focusSel = null;'):figures.index('  window.__renderFocusSel = _renderFocusSel;') + len('  window.__renderFocusSel = _renderFocusSel;')]
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(CHROME), headless=True)
            page = browser.new_page(viewport={'width': 1200, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<html><body></body></html>') if route.request.url == 'http://reader.test/' else route.abort())
            page.goto('http://reader.test/')
            page.set_content('''<title>交互验证</title><div id="main">阅读正文</div>
              <aside id="ep-side"><div id="side-pane-asst" class="ep-side-pane active" data-pane="asst">
              <div id="asst-thread"></div><div id="asst-input"><textarea id="asst-ta"></textarea></div></div></aside>''')
            page.evaluate('''() => {
              window.receipts=[];window.drops=[];window.RC={};
              window.webkit={messageHandlers:{bwNativeConversation:{postMessage:x=>receipts.push(x)}}};
              window.__asstSend=()=>{}; window.__asstBusy=()=>false;
            }''')
            page.add_script_tag(path=str(ROOT / '_server_deploy/static/reader-runtime/context-selection-registry.js'))
            for name in ['rc-ui.js', 'rc-sidedrawer.js', 'rc-flashcard.js', 'rc-voicecall.js', 'rc-turncard.js']:
                page.add_script_tag(path=str(ROOT / '_server_deploy/static/pdf' / name))
            page.evaluate('''() => {
              RC.sidedrawer.init({tabs:[{name:'asst',label:'助手'}],defaultTab:'asst'});
              RC.stickynote={createHtmlAt:(x,y,payload)=>{drops.push({x,y,payload});return true;},
                createCardAt:(x,y,cards,gid)=>{drops.push({x,y,cards,gid});return true;}};
            }''')
            page.add_script_tag(content=source)
            page.evaluate('__bwNativeConversation.setNativeMode(true)')
            page.wait_for_timeout(100)
            self.assertFalse(page.evaluate('RC.sidedrawer.isOpen()'))
            page.evaluate('__bwNativeConversation.perform({action:"toggleAssistant"})')
            page.wait_for_timeout(420)
            self.assertTrue(page.evaluate('RC.voiceCard.sideOpen()'))
            self.assertFalse(page.locator('#asst-input').is_visible())
            page.evaluate('__bwNativeConversation.perform({action:"showLegacy"})')
            page.wait_for_timeout(100)
            self.assertTrue(page.locator('#asst-input').is_visible())
            page.locator('#ep-side-handle').click()
            page.wait_for_timeout(420)
            self.assertFalse(page.evaluate('receipts[receipts.length-1].sidebarOpen'))
            page.evaluate('__bwNativeConversation.perform({action:"toggleAssistant"})')
            page.wait_for_timeout(420)
            self.assertTrue(page.evaluate('receipts[receipts.length-1].sidebarOpen'))

            page.evaluate('__bwNativeConversation.perform({action:"showLegacy"})')
            page.wait_for_timeout(100)

            # The actual original focus chip remains above the actual composer.
            page.add_script_tag(content='(() => {' + focus + ';window.testNativeSelectionHeld=_fsSelectionStillHeld;})();')
            page.evaluate("__setFocusSel('刚才选择的段落', 'text')")
            self.assertTrue(page.locator('#asst-sel-chip').is_visible())
            self.assertIn('刚才选择的段落', page.locator('#asst-sel-chip').inner_text())
            self.assertTrue(page.evaluate("!!(document.querySelector('#asst-sel-chip').compareDocumentPosition(document.querySelector('#asst-input')) & Node.DOCUMENT_POSITION_FOLLOWING)"))
            page.locator('#asst-sel-chip .asc-x').click()
            self.assertIsNone(page.evaluate('window.__focusSel'))

            # Real Anki renderer and charged drag; only persistence at the
            # book-placement boundary is replaced by an in-memory receipt.
            for name in ['data-store.js', 'card-repository.js']:
                page.add_script_tag(path=str(ROOT / '_server_deploy/static/reader-runtime' / name))
            page.evaluate('''async () => {
              const data=BWReaderRuntime.dataStore;
              window.cardStore=data.createDataStore({backend:data.createMemoryBackend(),deviceId:'native-test',causalCollections:['card-entities','card-states']});
              BWReaderRuntime.cardRepository=BWReaderRuntime.cardRepository.createCardRepository({store:cardStore});
              await BWReaderRuntime.cardRepository.registerDraft({id:'card_abc12345',cid:'card_abc12345',gid:'card_abc12345',
                cards:[{type:'basic',front:'問題',back:'解答'},{type:'basic',front:'二問',back:'二答'}],
                source:{kind:'reader-card-entity',sourceId:'card_abc12345',tool:'rc-flashcard',legacy:{piEntityRegistered:true}}});
            }''')
            page.evaluate('''() => {
              window.entity=RC.flashcard.renderEntity(document.querySelector('#asst-thread'), {
                surface:'inflow',mode:'state',form:'full',gid:'card_abc12345',
                cards:[{front:'問題',back:'解答',_st:'draft'},{front:'二問',back:'二答',_st:'draft'}]});
            }''')
            self.assertEqual(page.locator('[data-learning-card-id="card_abc12345"] .fc-slide').count(), 2)
            self.assertEqual(page.evaluate('entity.bd.__fc.cards[0].front'), '問題')
            self.assertTrue(page.evaluate('!!entity.bd.__fcPager'))
            self.assertGreater(page.locator('[data-learning-card-id="card_abc12345"] button').count(), 1)
            # Native view reads the live state machine, not original stale parts.
            page.evaluate('__bwNativeConversation.perform({action:"hideLegacy"})')
            page.evaluate("__setFocusSel('原生输入区的选区', 'text')")
            page.wait_for_timeout(100)
            native = page.evaluate('receipts[receipts.length-1]')
            self.assertEqual(native['selection']['text'], '原生输入区的选区')
            cards = [part for message in native['messages'] for part in message['parts'] if part['kind']=='anki']
            self.assertEqual(len(cards), 2)
            first = cards[0]
            self.assertTrue(first['data']['live'])
            self.assertEqual(first['data']['state'], 'draft')
            self.assertIn('保存到 Reader 卡库', [c['title'] for c in first['data']['controls']])
            # Native pagination must pick the visible card, even though the
            # hidden legacy pager is still showing index zero. Use the same
            # source / pending-state snapshot and selection registry as web.
            pinned = page.evaluate('(command)=>__bwNativeConversation.perform(command)', {
                'action':'liveAction','scope':native['scope'],'actionId':cards[1]['data']['pinId']})
            self.assertTrue(pinned['ok'])
            selected_card = page.evaluate('BWReaderRuntime.contextSelections.snapshot().items[0]')
            self.assertEqual(selected_card['id'], 'card:card_abc12345')
            self.assertEqual(selected_card['source']['index'], 1)
            self.assertEqual(selected_card['meta']['active_index'], 1)
            self.assertEqual(selected_card['meta']['cards'][1]['front'], '二問')
            self.assertEqual(page.evaluate('entity.bd.__fc.idx'), 0)
            page.wait_for_timeout(100)
            context = page.evaluate('receipts[receipts.length-1].attachments')
            self.assertEqual(len(context), 1)
            self.assertNotIn('meta', context[0])
            self.assertTrue(page.evaluate('(command)=>__bwNativeConversation.perform(command)', {
                'action':'liveAction','scope':native['scope'],'actionId':context[0]['removeId']})['ok'])
            self.assertEqual(page.evaluate('BWReaderRuntime.contextSelections.snapshot().items'), [])
            self.assertFalse(page.evaluate("entity.el.classList.contains('vc-picked')"))
            selected = page.evaluate('(command)=>__bwNativeConversation.perform(command)', {
                'action':'liveAction','scope':native['scope'],'actionId':first['data']['selectId'],'text':'問題'})
            self.assertTrue(selected['ok'])
            self.assertEqual(page.evaluate('__focusSel.text'), '問題')
            self.assertTrue(page.evaluate("testNativeSelectionHeld('問題')"))
            page.evaluate('(command)=>__bwNativeConversation.perform(command)', {
                'action':'liveAction','scope':native['scope'],'actionId':first['data']['selectId'],'text':''})
            self.assertFalse(page.evaluate("testNativeSelectionHeld('問題')"))
            self.assertEqual(page.evaluate('__focusSel.text'), '問題', 'release starts the original TTL rather than clearing the context')
            # Neither a hidden textarea nor an original button owns the action.
            page.evaluate("entity.bd.querySelectorAll('textarea,button').forEach(node=>node.remove())")
            field = first['data']['fields'][0]
            self.assertTrue(page.evaluate('(c)=>__bwNativeConversation.perform(c)', {
                'action':'liveAction','scope':native['scope'],'actionId':field['id'],'text':'修改过的問題'})['ok'])
            self.assertEqual(page.evaluate('entity.bd.__fc.cards[0].front'), '修改过的問題')
            stored = page.evaluate("BWReaderRuntime.cardRepository.load('card_abc12345')")
            self.assertEqual(stored['states']['0']['exactState']['front'], '修改过的問題')
            # Native placement carries original gid and the edited full snapshot.
            self.assertTrue(page.evaluate('(c)=>__bwNativeConversation.perform(c)', {
                'action':'liveAction','scope':native['scope'],'actionId':first['data']['dragId'],'x':0.25,'y':0.4})['ok'])
            placed = page.evaluate('drops.pop()')
            self.assertEqual(placed['gid'], 'card_abc12345')
            self.assertEqual(placed['cards'][0]['front'], '修改过的問題')
            self.assertEqual([placed['x'],placed['y']], [300,360])
            self.assertFalse(page.evaluate('(c)=>__bwNativeConversation.perform(c)', {
                'action':'liveAction','scope':'stale','actionId':first['data']['dragId'],'x':0.25,'y':0.4})['ok'])
            self.assertFalse(page.evaluate('(c)=>__bwNativeConversation.perform(c)', {
                'action':'liveAction','scope':native['scope'],'actionId':first['data']['dragId'],'x':2,'y':0.4})['ok'])
            # Reveal comes from rc-flashcard, including its original four ratings.
            page.evaluate("BWReaderRuntime.cardRepository.patchState('card_abc12345',0,{exactState:{front:'学習',back:'答案',_st:'learn',_showBack:false}})")
            page.evaluate("RC.flashcard.mountState(entity.bd,[{front:'学習',back:'答案',_st:'learn',_showBack:false},{front:'二問',back:'二答',_st:'draft'}],{gid:'card_abc12345',authoritative:true})")
            page.wait_for_timeout(120)
            state = page.evaluate('receipts[receipts.length-1]')
            learning = next(part for message in state['messages'] for part in message['parts'] if part['kind']=='anki')
            reveal = next(c for c in learning['data']['controls'] if c['title']=='显示答案')
            self.assertEqual([f['content'] for f in learning['data']['faces']], ['学習'])
            page.evaluate("entity.bd.querySelectorAll('button,[data-fc]').forEach(node=>node.remove())")
            self.assertTrue(page.evaluate('(c)=>__bwNativeConversation.perform(c)', {
                'action':'liveAction','scope':state['scope'],'actionId':reveal['id']})['ok'])
            page.wait_for_timeout(100)
            state = page.evaluate('receipts[receipts.length-1]')
            learning = next(part for message in state['messages'] for part in message['parts'] if part['kind']=='anki')
            self.assertEqual([f['content'] for f in learning['data']['faces']], ['学習','答案'])
            self.assertEqual(len(learning['data']['controls']), 4)
            # A controlled review keeps its existing callback and refuses a
            # second rating while the original submission is pending.
            rating = page.evaluate('''async () => {
              const st=entity.bd.__fc, calls=[];
              st.opts.onRate=(ease,index,card)=>{calls.push({ease,index});card._ratingPending=true;};
              await RC.flashcard.performInteraction(entity.bd,0,'rate-3');
              let refused=false;
              try { await RC.flashcard.performInteraction(entity.bd,0,'rate-4'); } catch (_) { refused=true; }
              const disabled=RC.flashcard.interactionState(entity.bd,0).controls.every(c=>c.disabled);
              st.cards[0]._ratingPending=false;
              delete st.opts.onRate;
              return {calls,refused,disabled};
            }''')
            self.assertEqual(rating['calls'], [{'ease':3,'index':0}])
            self.assertTrue(rating['refused'])
            self.assertTrue(rating['disabled'])
            # Restore draft for the existing original charged-drag test.
            page.evaluate("BWReaderRuntime.cardRepository.patchState('card_abc12345',0,{exactState:{front:'修改过的問題',back:'解答',_st:'draft',_showBack:false}})")
            page.evaluate("RC.flashcard.mountState(entity.bd,[{front:'修改过的問題',back:'解答',_st:'draft'},{front:'二問',back:'二答',_st:'draft'}],{gid:'card_abc12345',authoritative:true})")
            page.evaluate('__bwNativeConversation.perform({action:"clearSelection"})')
            self.assertIsNone(page.evaluate('window.__focusSel'))
            page.evaluate('__bwNativeConversation.perform({action:"showLegacy"})')
            page.wait_for_timeout(100)
            handle = page.locator('[data-learning-card-id="card_abc12345"] .vc-card-hd')
            rect = handle.bounding_box()
            self.assertIsNotNone(rect)
            page.mouse.move(rect['x'] + 60, rect['y'] + 12)
            page.mouse.down()
            page.wait_for_timeout(470)
            page.mouse.move(180, 220, steps=12)
            page.mouse.up()
            page.wait_for_timeout(100)
            drops = page.evaluate('drops')
            self.assertEqual(len(drops), 1)
            self.assertEqual(drops[0]['gid'], 'card_abc12345')
            self.assertEqual(drops[0]['cards'][0]['back'], '解答')
            self.assertEqual(page.locator('[data-learning-card-id="card_abc12345"]').count(), 1)

            # Native close must really close the shared drawer. The unchanged
            # live output dispatcher then puts its card into the reading area.
            page.evaluate('__bwNativeConversation.perform({action:"toggleAssistant"})')
            page.wait_for_timeout(420)
            self.assertFalse(page.evaluate('RC.voiceCard.sideOpen()'))
            page.evaluate("__vcDispatch('renderInfoCard', [{kind:'fact',cid:'card_closed1234',title:'关栏后的生成物',data:{answer:'阅读区可见',detail:'原卡片状态机'}}])")
            page.wait_for_timeout(150)
            self.assertTrue(page.locator('.vc-card:not(.vc-inflow)').filter(has_text='关栏后的生成物').is_visible())
            # Save and delete await the real card repository receipt without
            # clicking web controls. Stable batch indexes and identity survive.
            mutation = page.evaluate('''async () => {
              const gid='card_cafe1234', repo=BWReaderRuntime.cardRepository;
              const cards=[{type:'basic',front:'保存',back:'save'},{type:'basic',front:'删除',back:'delete'}];
              await repo.registerDraft({id:gid,cid:gid,gid,cards,source:{kind:'reader-card-entity',sourceId:gid,tool:'rc-flashcard',legacy:{piEntityRegistered:true}}});
              const e=RC.flashcard.renderEntity(document.querySelector('#asst-thread'),{surface:'inflow',mode:'state',gid,cards});
              e.bd.querySelectorAll('textarea,button').forEach(node=>node.remove());
              const save=await RC.flashcard.performInteraction(e.bd,0,'add');
              const confirmed=await repo.load(gid);
              let duplicateRefused=false;
              try { await RC.flashcard.performInteraction(e.bd,0,'add'); } catch (_) { duplicateRefused=true; }
              await RC.flashcard.performInteraction(e.bd,1,'del');
              const removed=await repo.load(gid);
              return {save,confirmed,removed,duplicateRefused};
            }''')
            self.assertTrue(mutation['save']['accepted'])
            self.assertEqual(mutation['confirmed']['states']['0']['phase'], 'confirmed')
            self.assertTrue(mutation['duplicateRefused'])
            self.assertTrue(mutation['removed']['states']['1']['removed'])
            self.assertEqual(mutation['removed']['id'], 'card_cafe1234')
            self.assertEqual(len(mutation['removed']['cards']), 2)
            page.evaluate('''() => {
              RC.turnCard.addPart('images-native', {kind:'card',card:{kind:'images',cid:'images_native',title:'配图',data:{items:[
                {title:'第一张',url:'https://example.org/a.png',src:'来源'},
                {title:'第二张',url:'https://example.org/b.png',src:'来源'}]}}});
            }''')
            page.wait_for_timeout(100)
            state = page.evaluate('receipts[receipts.length-1]')
            media = next(p for m in state['messages'] for p in m['parts'] if p['kind'] == 'images')
            self.assertEqual(len(media['data']['items']), 2)
            media_item = media['data']['items'][1]
            image_command = {'action':'mediaResource','scope':state['scope'],'actionId':media_item['mediaID']}
            resource = page.evaluate('(c)=>__bwNativeConversation.perform(c)', image_command)
            self.assertEqual(resource['resource'], '/pdf/api/img-proxy?url=https%3A%2F%2Fexample.org%2Fb.png')
            self.assertTrue(page.evaluate('(c)=>__bwNativeConversation.perform(c)', {
                'action':'liveAction','scope':state['scope'],'actionId':media_item['selectID']})['ok'])
            self.assertEqual(page.evaluate('BWReaderRuntime.contextSelections.snapshot().items[0].source'), {'cid':'images_native','item':1})
            page.evaluate('''() => {
              const original = document.querySelector('[data-vc-cid="images_native"]');
              window.imageMirror = __vcInfoCardEl(original.__vcCard);
              document.body.appendChild(imageMirror);
              RC.voiceCard.mediaAction(imageMirror, original.__vcCard, 1, 'toggle');
            }''')
            self.assertEqual(page.evaluate('BWReaderRuntime.contextSelections.snapshot().items'), [])
            self.assertTrue(page.evaluate('(c)=>__bwNativeConversation.perform(c)', {
                'action':'liveAction','scope':state['scope'],'actionId':media_item['selectID']})['ok'])
            self.assertTrue(page.evaluate('(c)=>__bwNativeConversation.perform(c)', {
                'action':'liveAction','scope':state['scope'],'actionId':media_item['removeID']})['ok'])
            self.assertEqual(page.evaluate('BWReaderRuntime.contextSelections.snapshot().items'), [])
            self.assertFalse(page.evaluate('(c)=>__bwNativeConversation.perform(c)', image_command)['ok'])
            page.wait_for_timeout(100)
            media = next(p for m in page.evaluate('receipts[receipts.length-1].messages') for p in m['parts'] if p['kind'] == 'images')
            self.assertEqual([i['title'] for i in media['data']['items']], ['第一张'])
            self.assertEqual(errors, [])
            browser.close()


if __name__ == '__main__':
    unittest.main()

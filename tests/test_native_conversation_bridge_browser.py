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
            self.assertEqual(page.locator('#ep-side').evaluate('(n)=>getComputedStyle(n).visibility'), 'hidden')
            self.assertEqual(page.locator('#main').evaluate('(n)=>getComputedStyle(n).paddingRight'), '0px')
            self.assertTrue(page.evaluate('RC.sidedrawer.isOpen()'))
            page.evaluate("RC.turnCard.draftText('user:u','我的','user','u-item','runner')")
            user = snapshot()['messages'][0]
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
            page.evaluate('__bwNativeConversation.setNativeMode(false)')
            self.assertFalse(page.evaluate('RC.sidedrawer.isOpen()'))
            self.assertEqual(page.locator('.ep-side-tab.active').get_attribute('data-pane'), 'toc')
            self.assertNotEqual(page.locator('#ep-side').evaluate('(n)=>getComputedStyle(n).visibility'), 'hidden')
            self.assertEqual(errors, [])
            browser.close()


if __name__ == '__main__':
    unittest.main()

import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// 迁出 3b（2026-09-26）：后台通话期间原生接管快照会话（ReaderNativeBackgroundContext），
// 与网页 rc-computer-voice 的快照链接是同一合同的两份发送方 —— 合同名、动作名、心跳窗口漂移，
// 桥就会拒收或把快照判过期。
const swift=readFileSync(new URL('../../ios/BWReader/App/ReaderNativeBackgroundContext.swift',import.meta.url),'utf8');
const socket=readFileSync(new URL('../../ios/BWReader/App/DirectVoiceSocket.swift',import.meta.url),'utf8');
const web=readFileSync(new URL('../../_server_deploy/static/pdf/rc-computer-voice.js',import.meta.url),'utf8');
const view=readFileSync(new URL('../../ios/BWReader/App/ReaderWebView.swift',import.meta.url),'utf8');

test('native background snapshot uses the web snapshot link contracts and actions',()=>{
  assert.match(web,/var OUTGOING_CONTEXT_CONTRACT = "reader-outgoing-context\/1";/);
  assert.match(web,/var ACTIVE_READING_CONTRACT = "reader-active-reading\/1";/);
  assert.match(web,/state\.channel\.request\("context", \{\s*sessionId: state\.sessionId,\s*contextContract: OUTGOING_CONTEXT_CONTRACT,\s*event: event,/);
  assert.match(web,/state\.channel\.request\("active-reading", \{\s*sessionId: state\.sessionId,\s*activeContract: ACTIVE_READING_CONTRACT,\s*active: activeReading,/);
  assert.match(swift,/"contextContract": \.string\("reader-outgoing-context\/1"\), "event": pageContext/);
  assert.match(swift,/"activeContract": \.string\("reader-active-reading\/1"\), "active": \.object\(object\)/);
  assert.match(swift,/object\["observedAtEpochMs"\]/,'active-reading must be re-stamped at send time like the web pump');
  assert.match(socket,/\["context", "active-reading"\]\.contains\(action\)/);
});

test('native heartbeat stays inside the bridge active-reading window',()=>{
  const windowMs=Number(web.match(/var ACTIVE_READING_HEARTBEAT_MS = (\d+);/)[1]);
  const nativeNs=Number(swift.match(/heartbeatNanoseconds: UInt64 = ([\d_]+)/)[1].replace(/_/g,''));
  assert.ok(nativeNs/1e6 < windowMs,'native heartbeat is slower than the bridge freshness window');
});

test('handoff runs before the background JS and only while a native call is active',()=>{
  const at=view.indexOf('case .background:'), end=view.indexOf('case .inactive:',at);
  const body=view.slice(at,end);
  assert.ok(body.indexOf('beginBackgroundSnapshotHandoff()')>0 && body.indexOf('beginBackgroundSnapshotHandoff()')<body.indexOf('setReaderForeground(false'),
    'handoff JS must be queued before the foreground=false script closes the web link');
  assert.match(body,/nativeVoiceBridge, bridge\.state\.phase != \.idle/);
  assert.match(web,/backgroundSnapshotHandoff: function \(\) \{/);
  assert.match(web,/if \(contextDeliveryMode === CONTEXT_DELIVERY_LEGACY\) return null;/);
});

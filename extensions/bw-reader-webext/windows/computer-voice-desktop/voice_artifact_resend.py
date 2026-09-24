"""Direct artifact resend: enqueue now, coordinate now, report only failure later."""
import asyncio
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

URL = 'http://127.0.0.1:43128/voice-core/artifacts'


def artifact_rpc(body=None, request_key=None):
    url = URL + ('?' + urllib.parse.urlencode({'requestKey': request_key}) if request_key else '')
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def latest_artifact():
    try:
        saved = json.loads((Path.home() / 'bw-computer-voice-bridge/runtime/reader-latest-artifact.json').read_text(encoding='utf-8'))
        if saved.get('kind') not in ('card', 'anki-draft'):
            return None
        return {'id': saved['correlation'], 'kind': saved['kind'],
                'title': ((saved.get('payload') or {}).get('card') or {}).get('title') or 'Anki 草稿'}
    except Exception:
        return None


class ArtifactResender:
    def __init__(self, runner, rpc=artifact_rpc):
        self.runner, self.rpc = runner, rpc
        self.jobs = set()

    async def prepared(self, rec, snapshot):
        r = self.runner
        result = rec.get('result') or {}
        if result.get('choice') != 'resend_latest_artifact' or result.get('probability', 0) <= .4:
            return
        if not r.settings.get('jevContextEnabled') or not r.settings.get('jevResendEnabled', False) or rec['key'] != r._jev.latest:
            return
        artifact = rec.get('artifact') or {}
        if not artifact.get('id'):
            rec['actionNotice'] = '未启动重发：没有可核实的最新生成物记录，请后台核实目标后处理。'
            return
        page = snapshot.get('currentPage') or {}
        active = snapshot.get('activeReading') or {}
        body = {'requestKey': rec['key'], 'artifactId': artifact['id'],
                'sourceInstanceId': active.get('sourceInstanceId') or page.get('sourceInstanceId'),
                'snapshotRevision': snapshot.get('revision'), 'file': page.get('file'), 'page': page.get('page')}
        if any(value is None for value in body.values()):
            rec['actionNotice'] = '未启动重发：缺少有效发送目标，请后台补取必要信息后处理。'
            return
        try:
            receipt = await asyncio.to_thread(self.rpc, body)
        except Exception as error:
            # An enqueue response lost in transit is not proof the send failed.
            receipt = {'ok': True, 'status': 'unknown', 'title': artifact.get('title'),
                       'artifactId': artifact['id']}
            r.log('artifact_resend_enqueue_unknown', requestKey=rec['key'], errorType=type(error).__name__)
        title = receipt.get('title') or artifact.get('title') or '最新生成物'
        if not receipt.get('ok'):
            rec['actionNotice'] = '未启动重发《' + title + '》：' + str(receipt.get('error')) + '。请后台核实后处理。'
            return
        rec['actionNotice'] = ('【重发协调通知·不是送达回执】已根据本轮用户请求尝试重发《' + title +
            '》（生成物编号：' + str(artifact['id']) + '）。程序已接管发送，无需等待成功回执。'
            '若目标和意图无误，请保持沉默，不要重复发送；若理解有误，请纠正。只有失败才会另行通知。')
        if receipt.get('status') == 'unknown':
            rec['actionNotice'] = ('【重发协调通知】已发起《' + title +
                '》的重发请求，但启动回执尚未确认，正在核实；请暂勿重复发送。若目标或意图有误，请纠正。')
        r.log('artifact_resend_attempted', requestKey=rec['key'], artifactId=artifact['id'], title=title)
        job = asyncio.create_task(self._watch(rec, title, r.thread_id))
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def _watch(self, rec, title, thread_id):
        # This observer is independent of Jev and remains alive when voice ends.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            await asyncio.sleep(.25)
            try:
                receipt = await asyncio.to_thread(self.rpc, request_key=rec['key'])
            except Exception:
                continue
            status = receipt.get('status')
            if status == 'delivered':
                self.runner.log('artifact_resend_delivered', requestKey=rec['key'])
                return  # successful sends never inject a second message
            if status in ('failed', 'unknown', 'not-found'):
                text = ('【生成物发送异常回执】《' + title + '》' +
                        ('发送失败' if status == 'failed' else '未取得确定的发送回执，送达结果未知') +
                        '；对应用户请求：' + str(rec.get('request_used') or rec.get('text')) +
                        '；原因：' + str(receipt.get('error') or status) +
                        '。请核对并处理。未知结果先核实，勿盲目重复发送。')
                await self._failure(thread_id, text, rec['key'])
                return
        await self._failure(thread_id, '【生成物发送异常回执】《' + title +
                            '》持续无法核实送达，不能视为成功；请核实发送状态。', rec['key'])

    async def _failure(self, thread_id, text, key):
        r = self.runner
        r.log('artifact_resend_failed', requestKey=key, text=text)
        # Explicit receipt, never old book context, always the originating thread.
        if r.thread_id == thread_id and r.backend_busy:
            result = await r.steer_running_turn(text, tag='artifact-failure')
            if result.get('ok'):
                r.log('ctx_steer', chars=len(text), withText=False, body=text,
                      threadId=thread_id, requestKey=key, backendTurnId=result.get('turnId'),
                      contextSource='artifact-resend-failure', dataFields=['artifact_failure'], images=[])
                return
        try:
            result = await r.app.call('turn/start', {'threadId': thread_id,
                                                   'input': [{'type': 'text', 'text': text}]}, timeout=20)
        except Exception as error:
            r.log('artifact_failure_notice_error', requestKey=key, errorType=type(error).__name__)
        else:
            r.log('ctx_backend', chars=len(text), withText=False, body=text,
                  threadId=thread_id, requestKey=key,
                  backendTurnId=((result or {}).get('turn') or {}).get('id'),
                  contextSource='artifact-resend-failure', dataFields=['artifact_failure'], images=[])

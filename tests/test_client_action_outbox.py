"""实跑 Pi 侧 client_action 队列：把真实函数从 relay 里抠出来，用假 socket 跑。

不 import voice_realtime_relay（它有一堆重依赖），而是把那几个函数的源码
原样 exec —— 验的是文件里那段真代码。
"""
import asyncio, io, os, re, sys, json, time, hashlib, tempfile, pathlib, unittest

SRC = io.open(
    str(pathlib.Path(__file__).resolve().parents[1] / '_server_deploy' / 'voice_realtime_relay.py'),
    encoding='utf-8').read()


def grab(name, kind='def'):
    m = re.search(rf'^(async def|def) {re.escape(name)}\(', SRC, re.M)
    assert m, f'找不到 {name}'
    start = m.start()
    # 到下一个顶格 def/async def 为止
    nxt = re.search(r'^(async def|def|# ──)', SRC[start + 10:], re.M)
    end = start + 10 + (nxt.start() if nxt else 4000)
    return SRC[start:end]


consts = re.search(
    r'_CLIENT_ACTION_OUTBOX_DIR = .*?_CLIENT_ACTION_OUTBOX_TTL = [^\n]*', SRC, re.S)
assert consts, '找不到常量段'

tmp = tempfile.mkdtemp()
ns = {'os': os, 'json': json, 'time': time, 'hashlib': hashlib, 'print': print}
exec(consts.group(0), ns)
ns['_CLIENT_ACTION_OUTBOX_DIR'] = os.path.join(tmp, 'outbox')
for fn in ('_action_outbox_path', '_action_is_replayable',
           '_action_outbox_append', '_replay_client_actions',
           '_send_client_action'):
    exec(grab(fn), ns)


class FakeWS:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []
    async def send(self, payload):
        if self.fail:
            raise ConnectionError('socket closed')
        self.sent.append(json.loads(payload))


def card_action(cid):
    return {'fn': '_vcCardPush', 'args': [{'cid': cid, 'kind': 'videos'}]}


async def main():
    P, F = [], []
    def check(name, ok):
        (P if ok else F).append(name)

    book = 'library/x.pdf'

    # ① 判据：带 cid 可重放，不带的不可
    check('带 cid 判为可重放', ns['_action_is_replayable'](card_action('c_1')) is True)
    check('跳页不可重放', ns['_action_is_replayable'](
        {'fn': '_goPage', 'args': [{'page': 5}]}) is False)
    check('无 args 不可重放', ns['_action_is_replayable']({'fn': 'x'}) is False)

    # ② 投递成功 → 不入队
    ok_ws = FakeWS()
    sent = await ns['_send_client_action'](ok_ws, card_action('c_ok'), book)
    check('送达返回 True', sent is True)
    check('送达后没有落地文件', not os.path.exists(ns['_action_outbox_path'](book)))
    check('对端收到 1 条', len(ok_ws.sent) == 1)

    # ③ 投递失败 → 落地
    bad = FakeWS(fail=True)
    sent = await ns['_send_client_action'](bad, card_action('c_q1'), book)
    check('失败返回 False', sent is False)
    check('失败后有落地文件', os.path.exists(ns['_action_outbox_path'](book)))

    await ns['_send_client_action'](bad, card_action('c_q2'), book)

    # ④ 不可重放的失败 → 不落地（有意丢弃）
    await ns['_send_client_action'](bad, {'fn': '_goPage', 'args': [{'page': 9}]}, book)
    with open(ns['_action_outbox_path'](book), encoding='utf-8') as fh:
        queued = json.load(fh)
    check('队列里只有 2 条卡片动作', len(queued) == 2)
    check('跳页没被入队', all(
        q['action']['fn'] == '_vcCardPush' for q in queued))

    # ⑤ 重连补投
    fresh = FakeWS()
    n = await ns['_replay_client_actions'](fresh, book)
    check('补投 2 条', n == 2)
    check('对端收到 2 条', len(fresh.sent) == 2)
    check('补投后文件已删（不会反复涌出）',
          not os.path.exists(ns['_action_outbox_path'](book)))
    cids = [s['payload']['args'][0]['cid'] for s in fresh.sent]
    check('顺序保持 c_q1 → c_q2', cids == ['c_q1', 'c_q2'])

    # ⑥ 补投时又断 → 剩下的重新入队
    for c in ('c_r1', 'c_r2'):
        await ns['_send_client_action'](FakeWS(fail=True), card_action(c), book)
    n = await ns['_replay_client_actions'](FakeWS(fail=True), book)
    check('全断时补投 0 条', n == 0)
    check('断了的重新入队', os.path.exists(ns['_action_outbox_path'](book)))

    # ⑦ 不同书互不干扰
    await ns['_send_client_action'](FakeWS(fail=True), card_action('c_other'), 'other.pdf')
    check('按书分文件', ns['_action_outbox_path'](book)
          != ns['_action_outbox_path']('other.pdf'))

    # ⑧ 上限
    for i in range(50):
        await ns['_send_client_action'](FakeWS(fail=True), card_action(f'c_{i}'), 'big.pdf')
    with open(ns['_action_outbox_path']('big.pdf'), encoding='utf-8') as fh:
        big = json.load(fh)
    check(f'有上限（实得 {len(big)}）', len(big) <= ns['_CLIENT_ACTION_OUTBOX_MAX'])

    return P, F


class ClientActionOutboxTests(unittest.TestCase):
    """Pi → 客户端方向的 client_action 队列。

    ⚠ 方向说明：Pi 上已有的 `command-outbox/2` 是**客户端 → Pi**（阅读器离线时
    攒着写操作，回来批量提交），跟这套相反，不能复用。

    这条链此前是**裸** `await bws.send(...)`：socket 一断就抛异常，不但卡片没了，
    整条工具结果处理（含后面要口播的内容）也一起中断。而 App 只要进 .inactive
    就会拆链 —— 于是用户报的"卡片传输总是失败"。
    """

    def test_outbox_end_to_end(self):
        passed, failed = asyncio.run(main())
        self.assertEqual(failed, [], f"失败项：{failed}")
        self.assertGreaterEqual(
            len(passed), 15, "断言数量骤减 —— 是不是有用例被静默跳过了")


if __name__ == '__main__':
    unittest.main()

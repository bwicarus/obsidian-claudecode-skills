"""Jev context preparation, with an explicit direct-resend callback."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
import urllib.request


CONTEXT_QUESTION = {
    'type': 'choice',
    'instructions': (
        '独立判断本次请求是否需要通过阅读器状态注入通道，向后台额外提供上下文。'
        '用户原话和已有对话由委派通道正常传递，不会被此选择删除。'
        '只判断最后这次请求，前文用于解释指代。不要因为系统恰好提供了书页就认为它相关。'
        '这不是选工具；选转交后台不代表一定要提供或省略上下文。'
        '如果请求依赖当前书页、选中对象、既有卡片、待复习卡库或相关阅读任务状态，选 reader_context；'
        '要求取卡复习时需要卡库资料，即使没有打开任何书页也选 reader_context；'
        '这些资料缺失时也选 reader_context，不要把缺失当成不需要。'
        '只有请求本身不需要这些附加资料时才选 no_extra_context。'),
    'criteria': {
        'no_extra_context': '无需附加上下文。仅凭用户请求与已有对话即可处理；'
                            '例如闲聊、独立的知识问题、与当前书页无关的软件开发或 Jev 接入进度。'
                            '程序必须完全跳过这次注入，不发送空消息，也不回退补发阅读器状态。',
        'reader_context': '需要附加上下文，或尚不能排除这种需要。'
                          '例如给这个词加注解、解释所选内容、操作本页、核实某张阅读器卡片的状态。'
                          '程序继续提供相关状态；缺失的信息仍需后续核实。'
    }
}


# Fixed data requirements, not another model-selected hierarchy. Empty tuples
# mean tool advice only; None means no injection at all.
TOOL_DATA = {
    'resend_latest_artifact': (),
    'reader_card': ('page_identity', 'selected_items', 'source_passage', 'source_binding', 'target_cards'),
    'reader_anki_draft': ('page_identity', 'selected_items', 'source_passage', 'source_binding', 'knowledge_nodes'),
    'reader_highlight_range': ('page_identity', 'selected_items', 'source_passage', 'source_binding'),
    'reader_paper_start': ('page_identity', 'selected_items', 'source_passage'),
    'reader_browser_control': ('page_identity', 'selected_items'),
    'reader_command': ('page_identity',),
    'reader_context_snapshot': ('page_identity', 'selected_items', 'recent_actions', 'review'),
    'reader_page_text': ('page_identity', 'selected_items'),
    'reader_page_cards': ('page_identity',),
    'reader_page_card_read': ('page_identity', 'target_cards'),
    'reader_page_card_edit': ('page_identity', 'target_cards'),
    'reader_page_card_delete': ('page_identity', 'target_cards'),
    'reader_learning_card_read': ('learning_target',),
    'reader_learning_card_edit': ('learning_target',),
    'reader_learning_card_delete': ('learning_target',),
    'reader_learning_cards': ('search_terms',),
    'reader_review_answer': ('review',),
    'reader_review_current_card': ('review',),
    'reader_visual_image': ('page_identity', 'selected_items', 'selection_regions'),
    'kj_node_ensure': ('selected_items', 'source_passage', 'knowledge_nodes'),
    'task_cancel': ('task_receipts',),
    'task_pause': ('task_receipts',),
    'notify_ack': ('task_receipts',),
    'schedule_delete': ('task_receipts',),
    'schedule_enable': ('task_receipts',),
    'schedule_run_now': ('task_receipts',),
    'schedule_runs': ('task_receipts',),
    'reader_camera_snap': (), 'reader_capability_guide': (), 'reader_flow_progress': (),
    'review_deck': ('review_candidates',), 'schedule_create': (), 'schedule_list': (),
    'voice_call': (), 'voice_say': (), 'voice_session_start': (), 'voice_session_stop': (),
    'voice_status': (), 'voice_tell': (), 'voice_transcript': (),
    'assistant_handoff': None, 'knowledge_answer': None,
}

# These are data contracts, not instructions to execute the recommended action.
# Keep missing requirements explicit instead of filling them with unrelated page data.
TOOL_NEEDS = {
    'resend_latest_artifact': '固定流程：程序重显已记录的最新生成物，后台只核对已尝试发送通知，不重复执行。',
    'reader_card': '注解需要选中内容、完整相关原文和绑定位置；天气等独立展示卡不需要书页正文。修改或重发已有卡时核对目标卡；发送结果以实际回执为准。',
    'reader_anki_draft': '需要学习内容和知识节点 nodeIds；已有节点可复用，缺少时用 kj_node_ensure。引用书本才传 file、target、原样 sourceText；草稿成功不等于用户已经保存。',
    'reader_highlight_range': '需要当前选区或原文 [NN] 块地址。用户明确指当前选区时可用 at={selection:true}；颜色和备注按用户要求。',
    'reader_paper_start': '需要出题范围和对应原文；选中部分就用相关段落，整页任务才用整页。生成练习 blocks；仅支持 PDF。',
    'reader_browser_control': '需要当前文档位置以及用户指定的滚动方向、标题或定位目标；无需其他卡片正文。',
    'reader_command': '翻页需要当前文档位置和目标页或方向，无需书页正文。',
    'reader_context_snapshot': '仅在所需状态缺失、过期、冲突或用户要求刷新时读取；已有匹配状态直接使用。',
    'reader_page_text': '读取指定书页；已知原词时用 contains 缩小结果，文件和页码不明时再补定位。',
    'reader_page_cards': '查询当前页卡片索引；若已知目标卡编号，优先单卡读取。',
    'reader_page_card_read': '需要页面卡 id 或当前可见 number；续读还需该次返回的 revision 和 next_offset。',
    'reader_page_card_edit': '需要目标页面卡 id、revision 和替换内容。局部修改要保留富媒体或布局时，先按 id 读取该卡；学习卡实体用学习卡编辑工具。',
    'reader_page_card_delete': '只需要目标页面卡 id、revision（可选 number），不需要卡片正文；仅删除页面放置，不删除学习卡实体。',
    'reader_learning_card_read': '需要学习卡实体 card_* id 和 cardIndex；页面放置 c_* id 不能代替。缺少实体编号时按目标查询卡库。',
    'reader_learning_card_edit': '需要实体 id、cardIndex、entityRevision，以及目标卡原内容或来源。缺少时单卡读取；source 是完整替换且影响同批卡片。',
    'reader_learning_card_delete': '需要实体 id、cardIndex、stateRevision 和删除范围；不需要题目答案正文。编号或版本缺失时先读取，不能使用页面卡版本代替。',
    'reader_learning_cards': '按用户提供的 id 或 contains 检索学习卡库；当前选中词仅是候选检索词，不能当成已保存的证据。',
    'reader_review_answer': '需要本次复习卡身份、题目、答案和当前复习状态；用户作答来自对话，缺少当前卡时用 reader_review_current_card 获取。',
    'reader_review_current_card': '读取当前复习卡及状态，无需整页书本正文；没有当前复习卡不等于卡库没有待复习内容。',
    'reader_visual_image': '需要当前页面与截图范围；selection-near 的 selectionId 必须来自现有选区。图片由工具获取，不把文字选区当图片。',
    'kj_node_ensure': '需要概念名称、别名及用于消歧的原文；已有匹配 nodeIds 时复用，不凭名称编造编号。',
    'task_cancel': '需要明确的未完成任务和状态；这里只能提供已记录工具事件，不能把取消理解成删除产物。后台核实任务并处理。',
    'task_pause': '需要明确的未完成任务和状态；保留已有产物。后台核实任务并处理，不凭缺少回执断定任务未开始。',
    'notify_ack': '需要本次通知的真实 id；从通知消息取，普通工具完成事件不能当通知编号。',
    'schedule_create': '需要用户指定的任务、时区、时间或触发条件及执行内容；从本次请求和对话整理，独立定时任务不需要当前书页。',
    'schedule_delete': '需要指定定时任务 id；指代不明时用 schedule_list，不能用普通工具事件编号代替。',
    'schedule_enable': '需要定时任务 id 和启用或停用意图；编号不明时用 schedule_list。',
    'schedule_run_now': '需要指定定时任务 id；编号不明时用 schedule_list，运行完成情况用 schedule_runs 查询。',
    'schedule_runs': '需要定时任务 id，读取真实运行记录；不把其他 Reader 工具事件当作任务结果。',
    'schedule_list': '直接读取定时任务列表；不需要书页、选区或学习卡正文。',
    'reader_camera_snap': '读取实体摄像头，位置或设备范围以用户请求为准，不需要阅读器页面。',
    'reader_capability_guide': '需要要查询的能力或话题名，不需要阅读器正文。',
    'reader_flow_progress': '这是固定流程运行器使用的进度接口；技能名、步数和状态应由运行器给出，后台不要手动猜测调用。',
    'review_deck': '本次只提前读取卡片，未启动复习、未播题、未判答或写入评分。后台依据用户请求和对话决定复习范围、选题、问法，再安排语音交流。候选清单不是已经选定的题目；范围包含新卡和到期卡，未按本次要求过滤。完整且匹配的资料直接复用，不要重复取卡；缺失或状态变化时再按需读取。评分前仍须核实当前复习卡身份及真实作答，副本中的 gid/index/noteId 不能冒充当前复习 cardId。',
    'voice_call': '需要要告知的内容和拨打理由；从当前请求与对话获取。语音委派轮不要重复播报。',
    'voice_say': '需要实际要播报的文字；用户语音委派轮的正常回答会自动播报，不额外调用造成重复。',
    'voice_session_start': '需要开始通话的意图或理由，不需要书页资料。',
    'voice_session_stop': '需要结束通话的意图，默认等正在播报的话说完，不需要书页资料。',
    'voice_status': '直接查询实际语音会话状态，不以阅读器状态替代。',
    'voice_tell': '需要要追加给语音模型的文字；不需要无关书页。',
    'voice_transcript': '需要条数或时间范围；真实对话由工具获取，不把阅读器状态当对话。',
    'assistant_handoff': '', 'knowledge_answer': '',
}


def skip_context_reason(bundle):
    if bundle.get('probability', 0) <= .4:
        return 'low_confidence_handoff'
    if bundle.get('choice') in ('assistant_handoff', 'knowledge_answer'):
        return 'no_catalog_tool'
    if bundle.get('contextChoice') == 'no_extra_context' and bundle.get('contextProbability', 0) > .4:
        return 'not_needed'
    if bundle.get('choice') not in TOOL_DATA:
        return 'unmapped_tool_handoff'
    return None


def selected_items(snapshot):
    items = [dict(x) for x in snapshot.get('selectedItems', []) if isinstance(x, dict)]
    if any(x.get('live') and x.get('kind', 'text') == 'text' for x in items):
        items = [x for x in items if x.get('live') or x.get('kind', 'text') != 'text' or x.get('ref')]
    if not items:
        selection = snapshot.get('selection') or {}
        if selection.get('state') == 'active' and selection.get('text'):
            items = [dict(kind='text', **selection)]
    return items


def passage_for_selection(text, items):
    if not items:
        return {'scope': 'page_without_selection', 'text': text}
    needles = [str(x.get('text') or x.get('what') or '') for x in items if x.get('kind', 'text') == 'text']
    if not needles:
        return {'scope': 'focused_object', 'text': '', 'note': '对象内容以 selected_items 为准；没有选择正文。'}
    marks = list(re.finditer(r'\[(\d{2,})\]', text))
    blocks = [text[m.start():marks[i+1].start() if i+1 < len(marks) else len(text)].strip()
              for i,m in enumerate(marks)]
    compact = lambda s: re.sub(r'\s+', '', s)
    matches = [b for b in blocks if any(compact(n) and compact(n) in compact(b) for n in needles)]
    if matches:
        return {'scope': 'selected_blocks', 'text': '\n'.join(matches),
                'note': '保留原文的 [NN] 块号；同词多处出现时须按选区进一步确认。'}
    # Multi-block selections still retain their complete text in selected_items.
    contexts = [x['context'] for x in items if isinstance(x.get('context'), str)]
    return {'scope': 'selection_only', 'text': '\n'.join(contexts),
            'note': '未定位到唯一原文块；选中文字完整保留在 selected_items，按需读取定位，勿猜块号。'}


CARD_SPAN = re.compile(r'⟦CARD_START\b(?P<attrs>[^⟧]*)⟧(?P<body>.*?)⟦CARD_END⟧', re.S)


def original_passage(text):
    # Card bodies are annotations, not quoted book text (especially Anki sourceText).
    return CARD_SPAN.sub(lambda m: m[0] if '⟦CARD_START' in m['body'] else '', text)


def present(value):
    return value is not None and value != '' and value != [] and value != {}


def compact_data(value):
    if isinstance(value, dict):
        return {k: compact_data(v) for k, v in value.items() if present(v)}
    if isinstance(value, list):
        return [compact_data(v) for v in value if present(v)]
    return value


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def page_card_records(page_text, embeds):
    records = {}
    for match in CARD_SPAN.finditer(page_text):
        if '⟦CARD_START' in match['body']:
            continue
        item = dict(re.findall(r'(\w+)="([^"\n]*)"', match['attrs']))
        if not item.get('id'):
            continue
        if 'n' in item:
            item['number'] = item.pop('n')
        for key in ('number', 'revision', 'cardIndex', 'entityRevision', 'stateRevision'):
            if str(item.get(key, '')).isdigit():
                item[key] = int(item[key])
        item['content'] = match['body']
        records[item['id']] = item
    for item in walk_dicts(embeds):
        if isinstance(item.get('id'), str):
            records[item['id']] = {**records.get(item['id'], {}), **item}
    return list(records.values())


def referenced_cards(records, items, request):
    identities = {v for item in walk_dicts(items) for k, v in item.items()
                  if k in ('id', 'ref', 'cardId', 'cid', 'gid') and isinstance(v, str)}
    terms = {str(item.get('text') or '') for item in items if item.get('kind', 'text') == 'text'} - {''}
    # A visible number explicitly spoken in this request outranks the current focus.
    number = re.search(r'第\s*(\d+)\s*(?:张|个)?\s*(?:卡|注解)', request)
    if number:
        return [c for c in records if str(c.get('number')) == number[1]]
    by_id = [c for c in records if c.get('id') in identities
             or (isinstance(c.get('learning'), str) and c['learning'] in identities)
             or (isinstance(c.get('learning'), dict) and c['learning'].get('id') in identities)]
    if by_id:
        return by_id
    return [c for c in records if any(c.get(k) in terms for k in ('anchor', 'label', 'title'))]


def page_card_data(records, include_content=False):
    keys = ('id', 'revision', 'number', 'type', 'label', 'title', 'anchor', 'learning', 'cardIndex')
    if include_content:
        keys += ('content',)
    return [{k: c[k] for k in keys if k in c and present(c[k])} for c in records]


def learning_card_data(items, targets, review, include_content=False):
    keys = ('id', 'cardIndex', 'entityRevision', 'stateRevision', 'nodeIds')
    if include_content:
        keys += ('card', 'front', 'back', 'cloze', 'source', 'content')
    found = {}
    for item in walk_dicts([items, targets, review]):
        identity = item.get('id') or item.get('ref')
        if isinstance(identity, str) and identity.startswith('card_'):
            row = {k: item[k] for k in keys if k in item}
            row['id'] = identity
            index = item.get('cardIndex')
            found[(identity, index)] = {**found.get((identity, index), {}), **row}
        # Inline placement markers often carry only learning="card_...".
        learning = item.get('learning')
        if isinstance(learning, str) and learning.startswith('card_'):
            row = {'id': learning}
            if 'cardIndex' in item:
                row['cardIndex'] = item['cardIndex']
            found.setdefault((learning, row.get('cardIndex')), row)
    return list(found.values())


def related_receipts(choice, tasks):
    if choice in ('task_cancel', 'task_pause'):
        return tasks  # These routes coordinate the outstanding task, whatever its tool.
    allowed = ({'notify_ack'} if choice == 'notify_ack' else
               {'schedule_create', 'schedule_delete', 'schedule_enable', 'schedule_run_now', 'schedule_runs', 'schedule_list'})
    return [t for t in tasks if str(t.get('tool') or '').split('__')[-1] in allowed]


def build_review_candidates():
    """Read existing local replicas only; never start review or write progress."""
    import review_deck
    root = review_deck.default_root()
    if not (root / 'replication-data').is_dir():
        raise FileNotFoundError('review_replica_unavailable')
    cards = review_deck.collect(root)
    keep = ('gid', 'index', 'cid', 'noteId', 'book', 'bookTitle', 'page',
            'kind', 'dueAtMs', 'createdAtMs', 'type', 'frontText', 'backText',
            'cloze', 'gradable', 'blockedReason')
    result = {'status': 'ready', 'readAtMs': int(time.time()*1000),
              'total': len(cards), 'due': sum(c.get('kind') == 'due' for c in cards),
              'new': sum(c.get('kind') == 'new' for c in cards),
              'cards': [{k: c.get(k) for k in keep if c.get(k) is not None} for c in cards]}
    # Never silently truncate a card or call a partial queue the complete set.
    if len(json.dumps(result, ensure_ascii=False)) > 48000:
        return {'status': 'too_large', 'total': len(cards),
                'note': '完整候选超过单次资料预算，未发送部分卡片冒充全部；请后台按用户范围取卡。'}
    return result


def review_snapshot_path():
    return Path(__file__).resolve().parent / 'voice-cli' / 'review-candidates.json'


def refresh_review_candidates():
    """Background producer. Replace the derived snapshot atomically."""
    result = build_review_candidates()
    now = int(time.time()*1000)
    result.update(contract='review-candidates/1', refreshedAtMs=now, expiresAtMs=now+15000)
    path = review_snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.' + str(time.time_ns()) + '.tmp')
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {k: result[k] for k in ('status', 'total', 'refreshedAtMs') if k in result}


def load_review_candidates():
    """Request path: one snapshot read, no card-library traversal or rebuild."""
    try:
        with review_snapshot_path().open(encoding='utf-8') as handle:
            raw = handle.read(100001)
        if len(raw) > 100000:
            raise ValueError('snapshot_too_large')
        result = json.loads(raw)
        if result.get('contract') != 'review-candidates/1':
            raise ValueError('snapshot_contract')
        now = int(time.time()*1000)
        if not isinstance(result.get('expiresAtMs'), (int, float)) or not now < result['expiresAtMs'] <= now+15000:
            return {'status': 'stale', 'note': '预先整理的复习清单已过期；本次未现场重建卡库'}
        if result.get('status') == 'ready' and (not isinstance(result.get('cards'), list) or result.get('total') != len(result['cards'])):
            raise ValueError('snapshot_incomplete')
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {'status': 'unavailable', 'note': '预先整理的复习清单尚未就绪；本次未现场查询卡库'}


def render_review_candidates(value):
    if value.get('status') != 'ready':
        return ('复习卡片未能提前取齐：' + str(value.get('note') or value.get('status') or '未知')
                + '。这不代表没有待复习卡，请后台按需取卡。')
    cards = value.get('cards', [])
    lines = ['已提前读取本地副本中的全部待复习候选：共 %s 张，新卡 %s 张，到期卡 %s 张。'
             % (value['total'], value['new'], value['due']),
             '下列不是已选定的考题；由后台按用户要求筛选和组织提问。仅供本轮使用，卡片状态以读取时为准。']
    if value.get('refreshedAtMs'):
        lines.append('程序已提前维护此清单，本次只读取现成文件；最近整理时间：' +
                     time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(value['refreshedAtMs']/1000)) + '。')
    if value.get('readingScope'):
        lines.append('发言时阅读位置（仅供解释“这本书／这页”）：' + json.dumps(value['readingScope'], ensure_ascii=False))
    for n, card in enumerate(cards, 1):
        identity = {k: card[k] for k in ('gid', 'index', 'cid', 'noteId') if k in card}
        location = {k: card[k] for k in ('book', 'bookTitle', 'page', 'createdAtMs', 'dueAtMs') if k in card}
        lines.extend(['卡片 %s｜%s｜%s' % (n, '到期卡' if card.get('kind') == 'due' else '新卡',
                     '副本标记可评分（执行前仍需核实）' if card.get('gradable') else
                     '暂不能评分：' + str(card.get('blockedReason') or '状态未知')),
                     '身份：' + json.dumps(identity, ensure_ascii=False),
                     '来源与时间：' + json.dumps(location, ensure_ascii=False),
                     '题面：\n' + str(card.get('frontText') or ''),
                     '答案：\n' + str(card.get('backText') or '')])
        if card.get('type') == 'cloze' or card.get('cloze'):
            lines.append('填空原文：\n' + str(card.get('cloze') or '未提供；不能凭空推定挖空位置'))
    return '\n'.join(lines)


def render_tool_context(bundle, data, missing):
    choice = bundle['choice']
    lines = ['【本次任务资料；不是新请求或完成回执】',
             '用户请求：' + str(bundle.get('request') or ''),
             'Jev 推荐：' + choice + '（概率 ' + format(bundle['probability'], '.0%') + '）']
    labels = {'target_cards': '目标页面卡', 'card_candidates': '页面卡索引（目标尚未唯一确定）',
              'learning_target': '目标学习卡', 'review': '当前复习卡与状态',
              'knowledge_nodes': '已提供的知识节点', 'search_terms': '候选检索词',
              'selection_regions': '可用图像选区', 'recent_actions': '最近操作',
              'task_receipts': '相关工具记录（不是完整任务状态）',
              'source_binding': '调用所需的来源参数'}
    for field, value in data.items():
        value = compact_data(value)
        if not present(value):
            continue
        if field == 'review_candidates':
            lines.append(render_review_candidates(value))
        elif field == 'page_identity':
            title = value.get('title') or '未提供书名'
            where = ('第 %s 页' % value['page'] if value.get('page') is not None else
                     '第 %s 节（从 0 计）' % value['section'] if value.get('section') is not None else '位置未提供')
            lines.append('当前位置：《' + str(title) + '》' + where + '。')
            if value.get('file'):
                lines.append('文件标识（调用时原样使用）：' + str(value['file']))
            if value.get('contextStatus') != 'ready':
                lines.append('页面资料状态：' + str(value.get('contextStatus') or '未知') + '，不能当作可用原文。')
            if value.get('truncated'):
                lines.append('页面原文不完整，以下仅是已提供部分。')
        elif field == 'selected_items':
            for item in value:
                kind = item.get('kind', 'text')
                if kind == 'text':
                    lines.append('选中文字：' + str(item.get('text') or '未提供'))
                else:
                    lines.append('选中对象：' + json.dumps(compact_data(item), ensure_ascii=False))
        elif field == 'source_passage':
            if value.get('text'):
                lines.append('相关原文（保留原 [NN] 段号）：\n' + value['text'])
            if value.get('scope') == 'selection_only':
                lines.append('尚未定位到原文段号；勿猜位置，绑定需要时再补取。')
        else:
            lines.append(labels.get(field, field) + '：' + json.dumps(compact_data(value), ensure_ascii=False))
    if missing:
        lines.append('未提供的信息：' + '；'.join(dict.fromkeys(missing)) + '。按任务需要补取，不把缺失当作不存在。')
    lines.append(TOOL_NEEDS[choice])
    lines.append('已有资料足够时直接使用；缺失、过期或冲突再按需读取。')
    return '\n'.join(lines)


def build_tool_context(bundle, snapshot, page_text, tasks):
    fields = TOOL_DATA[bundle['choice']]
    if fields is None:
        raise ValueError('no_context_route')
    choice = bundle['choice']
    cp = snapshot.get('currentPage') or {}
    items = selected_items(snapshot)
    records = page_card_records(page_text, cp.get('embeds'))
    targets = referenced_cards(records, items, str(bundle.get('request') or ''))
    # Keep all original prose. Embedded card bodies live separately, only when the
    # operation actually needs the target card's content, never inside sourceText.
    page_text = original_passage(page_text)
    items = [{k: original_passage(v) if k == 'context' and isinstance(v, str) else v
              for k, v in item.items()} for item in items]
    ready = snapshot.get('contextStatus') == 'ready' and cp.get('textAvailable', True)
    data = {}
    missing = []
    for field in fields:
        if field == 'page_identity':
            data[field] = {k: cp.get(k) for k in ('file', 'title', 'page', 'section', 'kind', 'truncated')}
            data[field]['contextStatus'] = snapshot.get('contextStatus')
        elif field == 'selected_items':
            data[field] = items
            if not items:
                missing.append('当前选中对象，不能用历史选区代替')
        elif field == 'source_passage':
            data[field] = passage_for_selection(page_text, items) if ready else {}
            if not data[field].get('text'):
                missing.append('可用的相关原文')
        elif field == 'source_binding':
            source = cp.get('highlightSource') or {}
            if choice == 'reader_anki_draft':
                data[field] = {'target': source.get('target')}
                if not source.get('target'):
                    missing.append('引用书本所需 target（PDF 页或 EPUB 节），不可猜测类型')
            else:
                data[field] = {k: source.get(k) for k in ('revision', 'expiresAt') if source.get(k) is not None}
                data[field]['page'] = cp.get('page')
        elif field == 'target_cards':
            include = choice in ('reader_card', 'reader_page_card_edit')
            data[field] = page_card_data(targets, include_content=include) if ready else []
            if not targets and choice != 'reader_card':
                data['card_candidates'] = page_card_data(records) if ready else []
                missing.append('唯一目标页面卡；仅有索引时先定位，不要把全部卡正文读出来')
            if choice in ('reader_page_card_edit', 'reader_page_card_delete') and any(c.get('revision') is None for c in targets):
                missing.append('目标页面卡 revision')
        elif field == 'learning_target':
            review = cp.get('review') or snapshot.get('review')
            # Only a focused learning entity or an active review card supplies identity.
            data[field] = learning_card_data(items, targets, None,
                include_content=choice == 'reader_learning_card_edit')
            if not data[field] and not items and not targets:
                data[field] = learning_card_data([], [], review,
                    include_content=choice == 'reader_learning_card_edit')
            required = ['id', 'cardIndex']
            if choice == 'reader_learning_card_edit': required.append('entityRevision')
            if choice == 'reader_learning_card_delete': required.append('stateRevision')
            if not data[field]:
                missing.append('目标学习卡实体身份；先按目标读取或查询学习卡库')
            elif any(any(x.get(k) is None for k in required) for x in data[field]):
                missing.append('学习卡所需 ' + '、'.join(required) + ' 尚不齐全，不能用页面卡版本代替')
        elif field == 'knowledge_nodes':
            data[field] = sorted({node for obj in walk_dicts([items, targets])
                for node in (obj.get('nodeIds') or []) if isinstance(node, str) and node.startswith('kj:')})
            if not data[field]: missing.append('知识节点编号；制 Anki 草稿时由 kj_node_ensure 获取')
        elif field == 'search_terms':
            data[field] = [x['text'] for x in items if x.get('kind', 'text') == 'text' and x.get('text')]
        elif field == 'task_receipts':
            data[field] = related_receipts(choice, tasks)
            if not data[field]: missing.append('此类任务的工具记录；缺少回执不代表未开始或已完成')
        elif field == 'review_candidates':
            data[field] = bundle.get('reviewCandidates') or {'status': 'unavailable'}
        elif field == 'review':
            data[field] = cp.get('review') or snapshot.get('review')
            if not data[field]: missing.append('当前复习卡与复习状态')
        elif field == 'recent_actions':
            data[field] = snapshot.get('recentActions', [])[-3:]
        elif field == 'selection_regions':
            data[field] = cp.get('selectionRegions')
    return render_tool_context(bundle, data, missing), fields


def top_choice(answer, criteria):
    probabilities = answer['probabilities']
    if not probabilities or any(k not in criteria or isinstance(v, bool)
            or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
            for k, v in probabilities.items()):
        raise ValueError('invalid_probabilities')
    return max(probabilities.items(), key=lambda item: item[1])


def snapshot_key(snapshot):
    cp = (snapshot or {}).get('currentPage') or {}
    identity = {'status': (snapshot or {}).get('contextStatus'),
                'page': {k: cp.get(k) for k in ('file', 'page', 'text', 'textAvailable', 'highlightSource')},
                'selectedItems': (snapshot or {}).get('selectedItems'),
                'selection': (snapshot or {}).get('selection')}
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def predict(state, settings):
    # Credentials are used only for this fixed provider endpoint, never in state/logs.
    key = next((s.strip() for s in Path(settings['jevKeyFile']).read_text(encoding='utf-8-sig').splitlines()
                if re.fullmatch(r'[A-Za-z0-9_.+/=-]{40,}', s.strip())), None)
    if not key:
        raise ValueError('credential_unavailable')
    question = json.loads(Path(settings['jevQuestionFile']).read_text(encoding='utf-8'))
    if not settings.get('jevResendEnabled', False):
        question['criteria'].pop('resend_latest_artifact', None)
    payload = {'model': 'jev-1.13.0', 'state': state,
               'questions': {'primary_tool': question, 'context_policy': CONTEXT_QUESTION}}
    request = urllib.request.Request('https://api.typesafe.ai/v1/systemone',
        data=json.dumps(payload, ensure_ascii=False).encode(), method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=3) as response:
        raw = response.read().decode()
    if key in raw:
        raise ValueError('unsafe_response')
    answers = json.loads(raw)['answers']
    choice, probability = top_choice(answers['primary_tool'], question['criteria'])
    context_choice, context_probability = top_choice(answers['context_policy'], CONTEXT_QUESTION['criteria'])
    return {'choice': choice, 'probability': probability,
            'contextChoice': context_choice, 'contextProbability': context_probability}


class JevContext:
    def __init__(self, settings, snapshot, dialogue, task_state, log, predictor=predict,
                 artifact_source=None, on_prepared=None, review_source=load_review_candidates):
        self.settings, self.snapshot, self.dialogue = settings, snapshot, dialogue
        self.task_state, self.log, self.predictor = task_state, log, predictor
        self.artifact_source, self.on_prepared = artifact_source, on_prepared
        self.review_source = review_source
        self.review_cache_status = {'status': 'starting'}
        self.records = {}
        self.latest = None
        self.active = set()
        self.seen = set()
        self.completed_user = None

    async def maintain_review_snapshot(self, stopping):
        # Independent of user requests and voice sessions. This worker only
        # maintains a derived file; it never injects, speaks, or grades cards.
        while not stopping():
            try:
                self.review_cache_status = await asyncio.to_thread(refresh_review_candidates)
            except Exception as error:
                if self.review_cache_status.get('status') != 'unavailable':
                    self.log('review_cache_refresh_error', errorType=type(error).__name__)
                self.review_cache_status = {'status': 'unavailable'}
            await asyncio.sleep(5)

    def observe(self, key, text='', new_turn=False, completed=False):
        if not key:
            return
        # Replayed notifications (including evicted rounds) cannot become current.
        if key in self.seen and key not in self.records:
            return
        if (new_turn and key not in self.seen) or self.latest is None:
            self.latest = key
        self.seen.add(key)
        rec = self.records.setdefault(key, {'key': key, 'text': '', 'request': '',
                                           'created': time.monotonic(), 'event': asyncio.Event()})
        if text:
            rec['text'] = text
        if completed and key == self.latest:
            self.completed_user = key
        for old in list(self.records):
            if len(self.records) <= 8:
                break
            if old != self.latest:
                self.records.pop(old, None)

    def delegated(self, key, request):
        self.observe(key, request)
        if key in self.records:
            self.records[key]['request'] = request

    def assistant_started(self, key):
        # The caller captures the completed user round at the voice event, not
        # at the later point when this callback happens to run.
        self.start(key, 'assistant_reply')

    def start(self, key, trigger):
        if not self.settings.get('jevContextEnabled') or not key or key != self.latest:
            return
        rec = self.records.get(key)
        if not rec or rec.get('task') or not rec.get('text'):
            return
        if len(self.active) >= 2:
            self.log('jev_prepare_skipped', requestKey=rec['key'], reason='busy')
            return
        rec['started_at'] = time.monotonic()
        rec['trigger'] = trigger
        task = asyncio.create_task(self._prepare(rec))
        rec['task'] = task
        self.active.add(task)
        task.add_done_callback(self.active.discard)

    def claim_delegation(self, key):
        rec = self.records.get(key)
        if not key or key != self.latest or not rec or rec.get('delegation_claimed'):
            self.log('jev_context_skipped', requestKey=key, reason='duplicate_or_unknown_round', chars=0)
            return False
        rec['delegation_claimed'] = True
        return True

    def claim_injection(self, bundle):
        rec = self.records.get(bundle.get('requestKey'))
        if not rec or rec.get('injection_claimed'):
            return False
        # Reserve before awaiting the network; an ambiguous failure must not
        # cause a second send or carry this context into the next backend turn.
        rec['injection_claimed'] = True
        return True

    async def _prepare(self, rec):
        started = time.monotonic()
        try:
            snap = self.snapshot() or {}
            cp = snap.get('currentPage') or {}
            rec['snapshot_key'] = snapshot_key(snap)
            tasks = self.task_state()
            rec['task_key'] = json.dumps(tasks, ensure_ascii=False, sort_keys=True)
            rec['request_used'] = rec.get('request') or rec['text']
            rec['artifact'] = await asyncio.to_thread(self.artifact_source) if self.artifact_source else None
            history = self.dialogue()
            base = ('用户正在通过语音与阅读助手交谈。以下是系统实际提供的资料；'
                    '只判断最后这次请求，前文用于解释指代；语音承诺不是执行成功回执。\n'
                    '当前阅读状态：' + json.dumps({'contextStatus': snap.get('contextStatus'),
                        'page': {k: cp.get(k) for k in ('file', 'title', 'page', 'textAvailable', 'text', 'highlightSource')},
                        'selectedItems': snap.get('selectedItems'), 'selection': snap.get('selection')}, ensure_ascii=False)
                    + '\n后台执行记录（缺少回执时状态未知）：' + rec['task_key'])
            base += '\n可重发的最新生成物（空值表示无记录）：' + json.dumps(rec['artifact'], ensure_ascii=False)
            tail = '\n用户（本次请求）：' + rec['request_used']
            # Drop whole older utterances, never truncate the selected text or page.
            lines = list(history)[-40:]
            while lines and len(base + '\n'.join(lines) + tail) > 24000:
                lines.pop(0)
            state = base + '\n' + '\n'.join(lines) + tail
            if len(state) > 24000:
                raise ValueError('state_too_large')
            self.log('jev_prepare_started', requestKey=rec['key'], chars=len(state), utterances=len(lines),
                     trigger=rec.get('trigger'))
            rec['result'] = await asyncio.to_thread(self.predictor, state, dict(self.settings))
            rec['ready_at'] = time.monotonic()
            self.log('jev_prepared', requestKey=rec['key'], ms=round((rec['ready_at']-started)*1000, 1),
                     choice=rec['result']['choice'], probability=rec['result']['probability'],
                     contextChoice=rec['result'].get('contextChoice'),
                     contextProbability=rec['result'].get('contextProbability'))
            if rec['key'] == self.latest and rec['result'].get('choice') == 'review_deck' and not skip_context_reason(rec['result']):
                read_started = time.monotonic()
                try:
                    rec['review_candidates'] = await asyncio.wait_for(
                        asyncio.to_thread(self.review_source), timeout=.5)
                    if rec['review_candidates'].get('status') == 'ready':
                        rec['review_candidates']['readingScope'] = compact_data(
                            {k: cp.get(k) for k in ('title', 'page', 'section')})
                except Exception as error:
                    rec['review_candidates'] = {'status': 'unavailable', 'note': '本地取卡未完成（' + type(error).__name__ + '）'}
                rec['ready_at'] = time.monotonic()
                self.log('jev_review_prefetched', requestKey=rec['key'],
                         ms=round((rec['ready_at']-read_started)*1000, 1),
                         status=rec['review_candidates'].get('status'),
                         total=rec['review_candidates'].get('total'))
            if self.on_prepared:
                await self.on_prepared(rec, snap)
        except Exception as error:
            # Exception messages may contain URLs/headers; log only the type.
            rec['error'] = type(error).__name__
            self.log('jev_prepare_error', requestKey=rec['key'], errorType=rec['error'])
        finally:
            rec['event'].set()

    async def for_delegation(self, key):
        if not self.settings.get('jevContextEnabled') or not key:
            return None
        rec = self.records.get(key)
        if not rec or key != self.latest:
            return None
        # Delegation itself is an early trigger, even before the voice reply.
        self.start(key, 'delegation')
        if not rec.get('task'):
            return None
        started = time.monotonic()
        try:
            timeout = max(0.05, min(3.0, float(self.settings.get('jevWaitMilliseconds', 1500))/1000))
            remaining = rec['started_at'] + timeout - time.monotonic()
            if not rec['event'].is_set():
                await asyncio.wait_for(rec['event'].wait(), max(0, remaining))
            if rec.get('ready_at', float('inf')) > rec['started_at'] + timeout:
                raise asyncio.TimeoutError
        except asyncio.TimeoutError:
            self.log('jev_fallback', requestKey=key, reason='timeout')
            return None
        bundle = dict(rec.get('result') or {})
        bundle.update(requestKey=key, request=rec.get('request_used'),
                      actionNotice=rec.get('actionNotice'),
                      reviewCandidates=rec.get('review_candidates'),
                      snapshotKey=rec.get('snapshot_key'), taskKey=rec.get('task_key'),
                      readyAt=rec.get('ready_at'), waitMs=round((time.monotonic()-started)*1000, 1))
        reason = self.invalid_reason(bundle)
        if reason:
            self.log('jev_fallback', requestKey=key, reason=reason)
            return None
        return bundle

    def invalid_reason(self, bundle):
        if not self.settings.get('jevContextEnabled'):
            return 'disabled'
        key = bundle.get('requestKey')
        rec = self.records.get(key)
        if not rec or key != self.latest:
            return 'request_changed'
        # Voice transcription and delegation wording need not be identical.
        # The authoritative association is thread + session + user turn ID.
        if not bundle.get('readyAt') or time.monotonic()-bundle['readyAt'] > 30:
            return 'expired_or_error'
        if bundle.get('choice') == 'resend_latest_artifact':
            return None  # the artifact/destination was frozen before enqueue
        if bundle.get('choice') == 'review_deck':
            return None  # local card replicas do not require an unchanged open page
        # Valid handoff / no-tool results are retained: the caller must skip
        # injection instead of treating them as failures and sending the old page.
        if skip_context_reason(bundle) in ('no_catalog_tool', 'low_confidence_handoff', 'unmapped_tool_handoff'):
            return None
        if snapshot_key(self.snapshot() or {}) != bundle.get('snapshotKey'):
            return 'reader_changed'
        if json.dumps(self.task_state(), ensure_ascii=False, sort_keys=True) != bundle.get('taskKey'):
            return 'task_changed'
        return None

    def reset(self):
        self.latest = None
        self.completed_user = None
        self.seen.clear()
        self.records.clear()
        for task in list(self.active):
            task.cancel()

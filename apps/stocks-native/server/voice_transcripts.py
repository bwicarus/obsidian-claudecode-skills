"""Bounded transcript snapshots: separate realtime turns, roles and backend items."""
from collections import OrderedDict
import uuid


class TranscriptStreams:
    def __init__(self, session_id):
        self.session_id = session_id
        self.entries = OrderedDict()
        self.roles = {}
        self.finished_backend = OrderedDict()

    def _entry(self, message_id, role, **extra):
        if message_id not in self.entries:
            self.entries[message_id] = {'messageId': message_id, 'role': role, 'text': '',
                                        'final': False, 'segments': [], 'delta': '', **extra}
            while len(self.entries) > 128:
                self.entries.popitem(last=False)
        return self.entries[message_id]

    @staticmethod
    def event(entry):
        return {key: entry[key] for key in ('messageId', 'role', 'text', 'final')} | {'type': 'transcript'}

    def realtime_start(self, role, turn_id=None):
        if role not in ('user', 'assistant'):
            return None
        current = self.entries.get(self.roles.get(role))
        # Stdio may deliver the first delta before the data-channel turn.created.
        if current and not current['final'] and current.get('realtimeTurnId') in (None, turn_id):
            if turn_id:
                current['realtimeTurnId'] = turn_id
            return current
        message_id = f'realtime:{self.session_id}:{turn_id or uuid.uuid4()}:{role}'
        entry = self._entry(message_id, role, realtimeTurnId=turn_id)
        if entry['final']:
            return entry
        self.roles[role] = message_id
        return entry

    def realtime_delta(self, role, delta):
        if role not in ('user', 'assistant') or not isinstance(delta, str) or not delta:
            return None
        entry = self.entries.get(self.roles.get(role)) or self.realtime_start(role)
        if entry['final']:
            return None
        entry['delta'] = (entry['delta'] + delta)[:32000]
        entry['text'] = '\n'.join(entry['segments'] + [entry['delta']])[:32000]
        return self.event(entry)

    def realtime_segment(self, role, text):
        if role not in ('user', 'assistant') or not isinstance(text, str) or not text:
            return None
        entry = self.entries.get(self.roles.get(role)) or self.realtime_start(role)
        if entry['final']:
            return None
        entry['segments'].append(text[:32000])
        entry['segments'] = entry['segments'][-64:]
        entry['delta'] = ''
        entry['text'] = '\n'.join(entry['segments'])[:32000]
        return self.event(entry)

    def realtime_final(self, role, text, turn_id=None):
        if role not in ('user', 'assistant'):
            return None
        entry = self.entries.get(self.roles.get(role))
        previous = entry
        if not entry or (turn_id and entry.get('realtimeTurnId') not in (None, turn_id)):
            message_id = f'realtime:{self.session_id}:{turn_id or uuid.uuid4()}:{role}'
            entry = self._entry(message_id, role, realtimeTurnId=turn_id)
        if previous is None:
            self.roles[role] = entry['messageId']
        if turn_id:
            entry['realtimeTurnId'] = turn_id
        if entry['final']:
            return None
        if isinstance(text, str) and text:
            entry['text'] = text[:32000]
        entry['final'] = True
        return self.event(entry) if entry['text'] else None

    def backend_item(self, turn_id, item_id, *, delta=None, text=None):
        if not turn_id or not item_id or turn_id in self.finished_backend:
            return None
        entry = self._entry(f'backend:{self.session_id}:{turn_id}:{item_id}:assistant',
                            'assistant', backendTurnId=turn_id)
        if entry['final']:
            return None
        if isinstance(text, str):
            entry['text'] = text[:32000]
        elif isinstance(delta, str):
            entry['text'] = (entry['text'] + delta)[:32000]
        return self.event(entry) if entry['text'] else None

    def finish_backend(self, turn_id, failure=None):
        events = []
        for entry in self.entries.values():
            if entry.get('backendTurnId') != turn_id or entry['final']:
                continue
            if failure:
                entry['text'] = failure
            entry['final'] = True
            if entry['text']:
                events.append(self.event(entry))
        self.finished_backend[turn_id] = True
        while len(self.finished_backend) > 128:
            self.finished_backend.popitem(last=False)
        return events

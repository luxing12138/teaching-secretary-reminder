"""Optional local productivity features; no network or automatic stage changes."""
from copy import deepcopy
import hashlib
import json
import os
import time
from pathlib import Path
import uuid

from core import ValidationError, now_local, parse_datetime


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def recipient_progress(task):
    stored = task.get('metadata', {}).get('recipient_progress', {})
    names = list(dict.fromkeys(str(name).strip() for name in task.get('recipients', []) if str(name).strip()))
    return {name: {field: bool(stored.get(name, {}).get(field, False))
                   for field in ('notified', 'feedback')} for name in names}


def focus_tasks(tasks, view, at=None):
    at = at or now_local()
    return [task for task in tasks if task['status'] != 'done' and (
        parse_datetime(task['due_at']).date() == at.date() if view == 'today'
        else parse_datetime(task['due_at']) < at)]


def sync_signature(task):
    keys = ('status', 'workflow_mode', 'title', 'due_at', 'external_sync_enabled', 'external_todo_id')
    value = {key: task.get(key) for key in keys}
    value['retired'] = task.get('metadata', {}).get('retired_todo_ids', [])
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


class SyncStatus:
    def __init__(self, store):
        self.root = Path(store.task_dir).parent / 'SyncStatus'

    def record(self, task, state='ok', error=''):
        path = self.root / (task['id']+'.json')
        value = {'signature': sync_signature(task), 'state': state, 'error': str(error)[:1200]}
        old = read_json(path, {})
        if all(old.get(key) == item for key, item in value.items()):
            return
        value['checked_at'] = now_local().isoformat()
        try:
            write_json(path, value)
        except OSError:
            pass  # An optional status indicator must not block the actual sync.

    def get(self, task):
        value = read_json(self.root / (task['id']+'.json'), {})
        if value.get('signature') == sync_signature(task):
            return value
        if task.get('external_sync_error'):
            return {'state': 'error', 'error': task['external_sync_error']}
        return {'state': 'pending', 'error': ''}

    def revision(self):
        result = []
        for path in self.root.glob('*.json'):
            try:
                result.append((path.name, path.stat().st_mtime_ns))
            except FileNotFoundError:
                pass
        return tuple(sorted(result))


USER_FIELDS = ('title', 'details', 'status', 'due_at', 'scheduled_at', 'recipients',
               'source_department', 'category', 'source_notice', 'related_files',
               'semester', 'source_id', 'recurrence', 'workflow_mode')


def user_snapshot(task):
    if task is None:
        return None
    value = {key: deepcopy(task.get(key)) for key in USER_FIELDS}
    value['recipient_progress'] = recipient_progress(task)
    return value


class ActionHistory:
    def __init__(self, store):
        self.store = store
        self.root = Path(store.task_dir).parent / 'Undo'

    def latest(self):
        available = []
        for path in self.root.glob('*.json'):
            record = read_json(path, {})
            if record.get('state') == 'available':
                available.append((record.get('created_ns', path.stat().st_ctime_ns), path, record))
        if not available:
            return None, None
        _, path, record = max(available, key=lambda item: item[0])
        return path, record

    def record(self, value):
        value['created_ns'] = time.time_ns()
        write_json(self.root / (f'{value["created_ns"]:020d}-'+uuid.uuid4().hex+'.json'), value)

    def perform(self, label, ids, operation):
        """Caller holds PublishLock. Journal partial batch success as well."""
        ids = list(dict.fromkeys(ids))
        before = {tid: user_snapshot(self.store.get_task(tid)) for tid in ids}
        try:
            return operation()
        finally:
            after = {tid: user_snapshot(self.store.get_task(tid)) for tid in ids}
            changed = [tid for tid in ids if before[tid] != after[tid]]
            if changed:
                self.record({'label': label, 'state': 'available', 'before': {tid: before[tid] for tid in changed},
                             'after': {tid: after[tid] for tid in changed}, 'undone_ids': []})

    def created(self, task_id):
        self.record({'label': '新建事项', 'state': 'available', 'before': {task_id: None},
                     'after': {task_id: user_snapshot(self.store.get_task(task_id))}, 'undone_ids': []})

    def undo(self):
        """Never restore adapter-owned IDs, timestamps or DingTalk completions."""
        path, record = self.latest()
        if not record:
            raise ValidationError('没有可以撤销的操作')
        remaining = [tid for tid in record['after'] if tid not in record['undone_ids']]
        for tid in remaining:
            if user_snapshot(self.store.get_task(tid)) != record['after'][tid]:
                raise ValidationError('事项已被钉钉、Agent 或其他编辑更新，不能覆盖后续变更。请手动调整。')
            if record['after'][tid] is None:
                archived = next((t for t in self.store.deleted_tasks() if t['id'] == tid), None)
                if user_snapshot(archived) != record['before'][tid]:
                    raise ValidationError('回收站中的事项已被修改，不能覆盖后续变更。请从回收站恢复。')
        for tid in remaining:
            before, after = record['before'][tid], record['after'][tid]
            if before is None:
                self.store.delete_task(tid)
            elif after is None:
                self.store.restore_task(tid)
            else:
                patch = {key: value for key, value in before.items() if key != 'status'}
                self.store.update_task(tid, patch)
                if before['status'] != after['status']:
                    self.store.move_task(tid, before['status'])
            record['undone_ids'].append(tid)
            write_json(path, record)
        record['state'] = 'undone'
        write_json(path, record)
        return record['label']


TEMPLATE_FIELDS = ('title', 'details', 'recipients', 'source_department', 'category', 'semester', 'workflow_mode')


class Templates:
    BUILTINS = [
        {'name': '一次性通知', 'payload': {'workflow_mode': 'notice', 'title': '', 'details': '## 通知内容\n\n\n## 传达记录\n'}},
        {'name': '材料收集', 'payload': {'workflow_mode': 'staged', 'title': '', 'details': '## 材料要求\n\n\n## 提交方式\n\n\n## 反馈记录\n'}},
        {'name': '会议通知', 'payload': {'workflow_mode': 'notice', 'title': '', 'details': '## 会议时间\n\n\n## 地点\n\n\n## 参会对象\n'}},
    ]

    def __init__(self, store):
        self.root = Path(store.task_dir).parent / 'Templates'

    def list(self):
        values = deepcopy(self.BUILTINS)
        for path in sorted(self.root.glob('*.json')):
            value = read_json(path, {})
            if value.get('name') and isinstance(value.get('payload'), dict):
                values.append(value)
        return values

    def save(self, name, payload):
        if not name.strip():
            raise ValidationError('请填写模板名称')
        fields = {key: deepcopy(payload[key]) for key in TEMPLATE_FIELDS if key in payload}
        due = parse_datetime(payload.get('due_at'))
        fields['due_time'] = due.strftime('%H:%M') if due else '17:00'
        write_json(self.root / (uuid.uuid4().hex+'.json'), {'name': name.strip(), 'payload': fields})

    @staticmethod
    def draft(template, at=None):
        payload = deepcopy(template['payload'])
        clock = payload.pop('due_time', '17:00')
        payload['due_at'] = (at or now_local()).date().isoformat()+'T'+clock
        return payload

"""改动记录（changeset）：智能体每动一次文件，都要留得下证据、看得见 diff、退得回去。

为什么不是「用 git 就行」：
aiwriter 的工作区可以是任意目录（`aiwriter.workspace`），未必是 git 仓库；
即便是仓库，模型改到一半的半成品也不该污染提交历史。所以这里自己记：

- **写前快照**：`write` / `edit` 落盘前抓原文（`before`），落盘后抓新文（`after`）；
- **一次提问 = 一个 changeset**：`run()` 期间所有写入归到同一个 id 下，
  审查与回滚都是「整轮」语义，而不是零散的单文件补丁；
- **回滚前校验**：当前内容必须仍等于记录里的 `after` 才允许还原。
  别人（或模型自己）后来改过就报冲突并跳过——宁可不动，也不能把人的改动吃掉；
- **大文件/二进制**：只记哈希与尺寸，标记 `rollbackable=False`，如实告知不可回滚。

落盘位置：`<data_dir>/changes/<id>.json`。纯 JSON + 原子替换，不引外部依赖。

安全边界：
- 所有路径必须落在工作区内（`file_path()` 逐一校验），changeset 里的 path 是
  **相对路径**，回滚时重新拼根并按 realpath 复验，防止「改 json 就能写到外面」；
- id 只允许 `[0-9A-Za-z_-]`，杜绝 `../` 拼进文件名。
"""
import difflib
import hashlib
import json
import logging
import os
import re
import secrets
import time

logger = logging.getLogger('zcbot')

FORMAT_VERSION = 1

# 单个文件超过这个大小就不存正文了（只记哈希），避免 changeset 目录被日志/数据文件撑爆
MAX_FILE_BYTES = 2 * 1024 * 1024
# 整轮改动正文总量上限；超了之后新条目只记哈希，仍能审查「改了哪些文件」
MAX_TOTAL_BYTES = 16 * 1024 * 1024
# 单文件 diff 最多输出的行数（前端渲染与响应体积的折中）
MAX_DIFF_LINES = 4000

_ID_RE = re.compile(r'^[0-9A-Za-z_-]{4,64}$')

OP_WRITE = 'write'
OP_EDIT = 'edit'
OP_CREATE = 'create'

STATUS_APPLIED = 'applied'
STATUS_ROLLED_BACK = 'rolled_back'
STATUS_PARTIAL = 'partial'


class ChangeError(Exception):
    """改动记录/回滚失败（给人看的中文提示）。"""


def _sha(text):
    if text is None:
        return ''
    return hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()


def _now():
    return int(time.time())


def _new_id():
    return time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2)


def _safe_id(cid):
    cid = str(cid or '').strip()
    if not _ID_RE.match(cid):
        raise ChangeError('非法的改动编号：%s' % (cid or '(空)'))
    return cid


class ChangeJournal:
    """一次智能体执行期间的改动账本。

    用法::

        j = ChangeJournal(changes_dir, root=workspace, session=key)
        tools = copy.copy(tools); tools.journal = j
        ... agent 跑一轮，write/edit 自己记账 ...
        j.save()          # 无改动时不会落盘
    """

    def __init__(self, changes_dir, root, session='', note=''):
        self.dir = os.path.abspath(changes_dir)
        self.root = os.path.abspath(root)
        self.session = session or ''
        self.note = note or ''
        self.id = _new_id()
        self.created = _now()
        self.entries = []
        self._total = 0

    # ── 路径 ───────────────────────────────────────────────────────────
    def rel(self, path):
        """绝对路径 → 相对工作区路径（越界直接抛错，不记账）。"""
        p = os.path.abspath(path)
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ChangeError('路径越界（超出工作区）：%s' % path)
        return os.path.relpath(p, self.root).replace(os.sep, '/')

    def abs_path(self, rel):
        """相对路径 → 绝对路径，并复验仍在工作区内（防伪造 changeset）。"""
        rel = str(rel or '').replace('\\', '/')
        p = os.path.abspath(os.path.join(self.root, rel))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ChangeError('改动记录里的路径越界：%s' % rel)
        return p

    # ── 记账 ───────────────────────────────────────────────────────────
    def record(self, path, op, before, after):
        """记一条文件改动。`before` 为 None 表示这次是新建。"""
        rel = self.rel(path)
        entry = {
            'path': rel,
            'op': op,
            'binary': False,
            'rollbackable': True,
            'reason': '',
            'before': before,
            'after': after,
            'before_sha': _sha(before),
            'after_sha': _sha(after),
            'size': len((after or '').encode('utf-8', 'replace')),
        }
        # 同一文件一轮内被改多次时，只保留「最早的原样 → 最新的结果」，
        # 否则回滚会按条目顺序互相覆盖，最后一步才是对的、前面全白做。
        for i, old in enumerate(self.entries):
            if old['path'] != rel:
                continue
            entry['before'] = old['before']
            entry['before_sha'] = old['before_sha']
            entry['op'] = old['op']
            self.entries[i] = entry
            return entry

        self._total += len((before or '').encode('utf-8', 'replace'))
        self._total += entry['size']
        self.entries.append(entry)
        return entry

    def mark_unrollbackable(self, path, op, reason):
        """二进制 / 超限 / 读不了的文件：仍记一笔，但说清为什么退不回去。"""
        rel = self.rel(path)
        if any(e['path'] == rel for e in self.entries):
            return None
        entry = {
            'path': rel, 'op': op, 'binary': True, 'rollbackable': False,
            'reason': reason, 'before': None, 'after': None,
            'before_sha': '', 'after_sha': '', 'size': 0,
        }
        self.entries.append(entry)
        return entry

    def over_budget(self, size):
        return self._total + int(size or 0) > MAX_TOTAL_BYTES

    @property
    def changed(self):
        return bool(self.entries)

    # ── 落盘 ───────────────────────────────────────────────────────────
    def to_dict(self):
        return {
            'version': FORMAT_VERSION,
            'id': self.id,
            'session': self.session,
            'note': self.note,
            'root': self.root,
            'created': self.created,
            'updated': _now(),
            'status': STATUS_APPLIED,
            'entries': self.entries,
        }

    def save(self):
        """有改动才落盘；返回 changeset id（无改动返回 None）。"""
        if not self.entries:
            return None
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, self.id + '.json')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)
        os.replace(tmp, path)
        return self.id


# ── 读取 / 列表 / diff / 回滚（模块级，供接口层调用）──────────────────

def _load_raw(changes_dir, cid):
    cid = _safe_id(cid)
    path = os.path.join(os.path.abspath(changes_dir), cid + '.json')
    if not os.path.isfile(path):
        raise ChangeError('改动记录不存在：%s' % cid)
    try:
        with open(path, encoding='utf-8') as f:
            rec = json.load(f)
    except Exception as e:
        raise ChangeError('改动记录损坏：%s' % e)
    if not isinstance(rec, dict) or not isinstance(rec.get('entries'), list):
        raise ChangeError('改动记录格式不对：%s' % cid)
    return rec


def list_changes(changes_dir, limit=50):
    """列出改动记录摘要（按时间倒序）。不返回文件正文，避免列表接口过重。"""
    d = os.path.abspath(changes_dir)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        if not fn.endswith('.json') or fn.endswith('.tmp'):
            continue
        cid = fn[:-5]
        if not _ID_RE.match(cid):
            continue
        try:
            rec = _load_raw(d, cid)
        except ChangeError:
            continue
        entries = rec.get('entries') or []
        text_entries = [e for e in entries if not e.get('binary')]
        out.append({
            'id': rec.get('id') or cid,
            'session': rec.get('session', ''),
            'note': rec.get('note', ''),
            'created': rec.get('created', 0),
            'updated': rec.get('updated', rec.get('created', 0)),
            'status': rec.get('status', STATUS_APPLIED),
            'files': len(entries),
            'additions': sum(len((e.get('after') or '').splitlines()) for e in text_entries),
            'deletions': sum(len((e.get('before') or '').splitlines()) for e in text_entries),
            'rollbackable': all(e.get('rollbackable', True) for e in entries),
            'paths': [e.get('path', '') for e in entries][:20],
        })
    out.sort(key=lambda x: (x.get('created', 0), x.get('id', '')), reverse=True)
    return out[:max(1, int(limit))]


def entry_diff(entry):
    """单个文件的 unified diff（带 ±3 行上下文）。"""
    before = entry.get('before') or ''
    after = entry.get('after') or ''
    if not entry.get('rollbackable', True) and entry.get('binary'):
        return '', 0, 0
    a = before.splitlines()
    b = after.splitlines()
    lines = list(difflib.unified_diff(a, b, fromfile='a/' + entry.get('path', ''),
                                      tofile='b/' + entry.get('path', ''), lineterm='', n=3))
    add = sum(1 for ln in lines if ln.startswith('+') and not ln.startswith('+++'))
    dele = sum(1 for ln in lines if ln.startswith('-') and not ln.startswith('---'))
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + ['…（diff 过长，已截断，共 %d 行）' % len(lines)]
    return '\n'.join(lines), add, dele


def load_change(changes_dir, cid, with_diff=True):
    """取一条改动记录；`with_diff=True` 时附带每个文件的 diff 文本。"""
    rec = _load_raw(changes_dir, cid)
    entries = []
    for e in rec.get('entries') or []:
        item = {
            'path': e.get('path', ''),
            'op': e.get('op', ''),
            'binary': bool(e.get('binary')),
            'rollbackable': bool(e.get('rollbackable', True)),
            'reason': e.get('reason', ''),
            'size': e.get('size', 0),
            'additions': 0,
            'deletions': 0,
        }
        if with_diff:
            diff, add, dele = entry_diff(e)
            item['diff'] = diff
            item['additions'] = add
            item['deletions'] = dele
        entries.append(item)
    return {
        'id': rec.get('id', ''),
        'session': rec.get('session', ''),
        'note': rec.get('note', ''),
        'root': rec.get('root', ''),
        'created': rec.get('created', 0),
        'updated': rec.get('updated', rec.get('created', 0)),
        'status': rec.get('status', STATUS_APPLIED),
        'files': entries,
    }


def rollback(changes_dir, cid):
    """把一次改动整轮退回去。

    逐条校验「当前内容 == 记录里的 after」才还原：
    - 等于 after → 还原成 before（before 为 None 则删掉新建的文件）；
    - 不等于 → 跳过并报冲突（绝不覆盖别人后来的修改）。

    返回 ``{'id', 'status', 'restored': [...], 'skipped': [{'path','reason'}]}``。
    """
    cid = _safe_id(cid)
    rec = _load_raw(changes_dir, cid)
    if rec.get('status') == STATUS_ROLLED_BACK:
        raise ChangeError('这条改动已经回滚过了：%s' % cid)
    journal = ChangeJournal(changes_dir, root=rec.get('root') or os.getcwd(),
                            session=rec.get('session', ''))

    restored, skipped = [], []
    for entry in reversed(rec.get('entries') or []):
        rel = entry.get('path', '')
        if not entry.get('rollbackable', True):
            skipped.append({'path': rel, 'reason': entry.get('reason') or '该文件不可回滚'})
            continue
        try:
            target = journal.abs_path(rel)
        except ChangeError as e:
            skipped.append({'path': rel, 'reason': str(e)})
            continue

        current = None
        if os.path.isfile(target):
            try:
                with open(target, encoding='utf-8') as f:
                    current = f.read()
            except (OSError, UnicodeDecodeError) as e:
                skipped.append({'path': rel, 'reason': '当前文件读不出来：%s' % e})
                continue
        if _sha(current) != entry.get('after_sha', ''):
            skipped.append({'path': rel, 'reason': '文件在改动之后又被修改过，跳过以免覆盖'})
            continue

        before = entry.get('before')
        try:
            if before is None:
                if os.path.isfile(target):
                    os.remove(target)
                    _prune_empty_dirs(os.path.dirname(target), journal.root)
                restored.append({'path': rel, 'action': 'deleted'})
            else:
                os.makedirs(os.path.dirname(target) or journal.root, exist_ok=True)
                with open(target, 'w', encoding='utf-8', newline='') as f:
                    f.write(before)
                restored.append({'path': rel, 'action': 'restored'})
        except OSError as e:
            skipped.append({'path': rel, 'reason': '写回失败：%s' % e})

    status = STATUS_ROLLED_BACK if not skipped else STATUS_PARTIAL
    rec['status'] = status
    rec['updated'] = _now()
    path = os.path.join(os.path.abspath(changes_dir), cid + '.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, path)
    return {'id': cid, 'status': status, 'restored': restored, 'skipped': skipped}


def _prune_empty_dirs(start, root):
    """删掉回滚后残留的空目录（只往上走到工作区根为止）。"""
    cur = os.path.abspath(start)
    root = os.path.abspath(root)
    while cur.startswith(root + os.sep):
        try:
            os.rmdir(cur)
        except OSError:
            return
        cur = os.path.dirname(cur)


def read_text(path, limit=MAX_FILE_BYTES):
    """读文本文件用于快照。

    返回 ``(text, None)`` 或 ``(None, 不可快照的原因)``。原因会原样写进
    changeset，前端据此告诉用户「这个文件退不回去」。
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return None, '读不到文件信息：%s' % e
    if size > limit:
        return None, '文件超过 %d KB，只记录不存正文' % (limit // 1024)
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        return None, '读取失败：%s' % e
    try:
        return raw.decode('utf-8'), None
    except UnicodeDecodeError:
        return None, '二进制或非 UTF-8 文件，无法记录正文'

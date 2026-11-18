"""改动账本（changeset）回归：快照、diff、整轮回滚、冲突保护。

这几条是「AI 改完能退回去」的全部依据，所以断言都对着**磁盘上的真实内容**：
- 快照不是「我以为的原文」，而是写之前真读到的字节；
- diff 必须能看出增删；
- 回滚要么真的还原，要么明确报冲突并**不动**文件；
- 越界路径、伪造编号一律拒绝。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core_plugins.aiwriter import changeset
from core_plugins.aiwriter.tools import ToolRegistry


def read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write(text)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / 'ws'
    root.mkdir()
    return str(root)


@pytest.fixture
def journal(ws, tmp_path):
    return changeset.ChangeJournal(str(tmp_path / 'changes'), root=ws, session='会话A')


def tools_with(journal, ws, mode='workspace'):
    reg = ToolRegistry(ws, mode=mode)
    reg.journal = journal
    return reg


# ── 记账 ───────────────────────────────────────────────────────────────
def test_write_and_edit_are_recorded_with_earliest_before(ws, journal):
    """同一文件一轮内改两次，账上只留一条：最早的原样 → 最新的结果。

    否则回滚会按顺序把中间态写回去，最后一步才碰巧是对的、前面全白做。
    """
    write(os.path.join(ws, 'a.py'), '旧内容\n')
    reg = tools_with(journal, ws)

    reg.call('write', {'path': 'a.py', 'content': '中间态\n'})
    reg.call('edit', {'path': 'a.py', 'oldString': '中间态', 'newString': '新内容'})

    assert len(journal.entries) == 1
    entry = journal.entries[0]
    assert entry['path'] == 'a.py'
    assert entry['before'] == '旧内容\n'
    assert entry['after'] == '新内容\n'
    assert read(os.path.join(ws, 'a.py')) == '新内容\n'


def test_new_file_marked_as_create(ws, journal):
    reg = tools_with(journal, ws)
    reg.call('write', {'path': 'sub/新插件.py', 'content': 'x = 1\n'})
    entry = journal.entries[0]
    assert entry['op'] == changeset.OP_CREATE
    assert entry['before'] is None
    assert entry['after'] == 'x = 1\n'


def test_no_change_writes_nothing(ws, journal):
    """只读操作不该留下一条空账。"""
    write(os.path.join(ws, 'a.py'), 'x\n')
    reg = tools_with(journal, ws)
    reg.call('read', {'path': 'a.py'})
    reg.call('list', {})
    assert journal.changed is False
    assert journal.save() is None
    assert not os.path.isdir(journal.dir) or not os.listdir(journal.dir)


def test_tools_without_journal_behave_as_before(ws):
    """没挂账本时行为必须与旧版一致——不是所有调用方都需要审计。"""
    reg = ToolRegistry(ws, mode='workspace')
    reg.call('write', {'path': 'a.py', 'content': 'hi\n'})
    assert read(os.path.join(ws, 'a.py')) == 'hi\n'
    assert reg.journal is None


# ── diff ───────────────────────────────────────────────────────────────
def test_diff_shows_added_and_removed_lines(ws, tmp_path):
    write(os.path.join(ws, 'a.py'), 'line1\nline2\nline3\n')
    j = changeset.ChangeJournal(str(tmp_path / 'changes'), root=ws)
    reg = tools_with(j, ws)
    reg.call('write', {'path': 'a.py', 'content': 'line1\nline2改\nline3\n'})
    cid = j.save()

    detail = changeset.load_change(j.dir, cid)
    assert detail['files'][0]['path'] == 'a.py'
    diff = detail['files'][0]['diff']
    assert '-line2' in diff and '+line2改' in diff
    assert detail['files'][0]['additions'] == 1
    assert detail['files'][0]['deletions'] == 1


def test_list_summarises_without_bodies(ws, journal):
    write(os.path.join(ws, 'a.py'), 'a\n')
    reg = tools_with(journal, ws)
    reg.call('write', {'path': 'a.py', 'content': 'b\n'})
    cid = journal.save()

    rows = changeset.list_changes(journal.dir)
    assert [r['id'] for r in rows] == [cid]
    row = rows[0]
    assert row['files'] == 1
    assert row['paths'] == ['a.py']
    assert row['status'] == changeset.STATUS_APPLIED
    assert row['rollbackable'] is True
    # 列表接口不该把正文带出去（否则一屏记录能把响应撑到几 MB）
    assert 'before' not in row and 'after' not in row


# ── 回滚 ───────────────────────────────────────────────────────────────
def test_rollback_restores_original_content(ws, journal):
    original = 'def f():\n    return 1\n'
    write(os.path.join(ws, 'a.py'), original)
    reg = tools_with(journal, ws)
    reg.call('edit', {'path': 'a.py', 'oldString': 'return 1', 'newString': 'return 2'})
    cid = journal.save()

    result = changeset.rollback(journal.dir, cid)
    assert result['status'] == changeset.STATUS_ROLLED_BACK
    assert result['skipped'] == []
    assert read(os.path.join(ws, 'a.py')) == original


def test_rollback_of_create_deletes_file_and_empty_dir(ws, journal):
    reg = tools_with(journal, ws)
    reg.call('write', {'path': 'deep/子目录/new.py', 'content': 'x\n'})
    cid = journal.save()

    result = changeset.rollback(journal.dir, cid)
    assert result['restored'][0]['action'] == 'deleted'
    assert not os.path.exists(os.path.join(ws, 'deep'))
    assert result['status'] == changeset.STATUS_ROLLED_BACK


def test_rollback_refuses_when_file_changed_afterwards(ws, journal):
    """改动之后文件又被别人改过 → 跳过并报冲突，绝不覆盖。

    宁可留下「回滚了一半」的明确状态，也不能把人后来的手改吃掉。
    """
    path = os.path.join(ws, 'a.py')
    write(path, 'original\n')
    reg = tools_with(journal, ws)
    reg.call('write', {'path': 'a.py', 'content': 'ai 改的\n'})
    cid = journal.save()

    write(path, '人手又改了一次\n')          # 模拟 AI 之后的其它修改
    result = changeset.rollback(journal.dir, cid)

    assert result['status'] == changeset.STATUS_PARTIAL
    assert result['restored'] == []
    assert result['skipped'][0]['path'] == 'a.py'
    assert '又被修改' in result['skipped'][0]['reason']
    assert read(path) == '人手又改了一次\n'


def test_rollback_twice_is_rejected(ws, journal):
    write(os.path.join(ws, 'a.py'), 'a\n')
    reg = tools_with(journal, ws)
    reg.call('write', {'path': 'a.py', 'content': 'b\n'})
    cid = journal.save()
    changeset.rollback(journal.dir, cid)
    with pytest.raises(changeset.ChangeError):
        changeset.rollback(journal.dir, cid)


def test_binary_file_is_recorded_but_not_rollbackable(ws, journal):
    path = os.path.join(ws, 'blob.bin')
    with open(path, 'wb') as f:
        f.write(b'\xff\xfe\x00binary')
    reg = tools_with(journal, ws)
    reg.call('write', {'path': 'blob.bin', 'content': 'now text\n'})

    entry = journal.entries[0]
    assert entry['rollbackable'] is False
    assert '二进制' in entry['reason']
    cid = journal.save()

    result = changeset.rollback(journal.dir, cid)
    assert result['status'] == changeset.STATUS_PARTIAL
    assert '二进制' in result['skipped'][0]['reason']


# ── 安全 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize('bad', ['../escape', '..', '/etc/passwd'])
def test_change_id_cannot_escape_directory(journal, bad):
    with pytest.raises(changeset.ChangeError):
        changeset.load_change(journal.dir, bad)


def test_record_outside_workspace_is_rejected(ws, journal):
    outside = os.path.join(os.path.dirname(ws), 'elsewhere.py')
    with pytest.raises(changeset.ChangeError):
        journal.record(outside, changeset.OP_WRITE, 'a', 'b')


def test_forged_change_with_escaping_path_is_skipped(ws, tmp_path):
    """即使有人改了 changes/*.json 把 path 改成 ../，回滚也不能写到工作区外。"""
    import json
    d = tmp_path / 'changes'
    d.mkdir()
    target = os.path.join(os.path.dirname(ws), 'victim.txt')
    write(target, 'original\n')
    forged = {
        'version': 1, 'id': 'forged-0001', 'session': '', 'note': '', 'root': ws,
        'created': 1, 'updated': 1, 'status': 'applied',
        'entries': [{'path': '../victim.txt', 'op': 'write', 'binary': False,
                     'rollbackable': True, 'reason': '', 'before': 'pwned\n',
                     'after': 'original\n',
                     'before_sha': changeset._sha('pwned\n'),
                     'after_sha': changeset._sha('original\n'), 'size': 9}],
    }
    (d / 'forged-0001.json').write_text(json.dumps(forged, ensure_ascii=False),
                                        encoding='utf-8')

    result = changeset.rollback(str(d), 'forged-0001')
    assert result['restored'] == []
    assert result['skipped'][0]['path'] == '../victim.txt'
    assert read(target) == 'original\n'

"""消息处理链工作流：按 QQ 选链、节点顺序、默认行为保持。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from framework.workflow import (
    WorkflowStore,
    apply_workflow,
    no_plan,
    resolve,
    validate_nodes,
)


def test_no_workflow_keeps_all_plugins_in_original_order():
    plan = no_plan()
    assert plan.allows('anything')
    assert plan.ordered(['b', 'a', 'c']) == ['b', 'a', 'c']
    assert plan.workflow_id == 0


def test_user_node_selects_by_qq():
    wf = {
        'id': 7, 'name': '测试链',
        'nodes': [
            {'type': 'user', 'config': {'values': ['123', 456]}},
            {'type': 'plugin', 'config': {'plugin': 'echo'}},
            {'type': 'plugin', 'config': {'plugin': 'aiwriter'}},
        ],
    }
    plan = apply_workflow(wf, user_id=123)
    assert plan.matched_user is True
    assert plan.ordered(['aiwriter', 'echo', 'other']) == ['echo', 'aiwriter']
    assert plan.allows('other') is False

    # 没点名的 QQ 退回默认：全部放行、原顺序
    miss = apply_workflow(wf, user_id=999)
    assert miss.allowed is None
    assert miss.ordered(['a', 'b']) == ['a', 'b']


def test_group_node_and_exclude():
    wf = {
        'id': 8, 'name': '群链',
        'nodes': [
            {'type': 'group', 'config': {'values': ['1001']}},
            {'type': 'plugin', 'config': {'plugin': 'echo'}},
            {'type': 'exclude', 'config': {'plugin': 'echo'}},
        ],
    }
    plan = apply_workflow(wf, group_id=1001)
    assert plan.allows('echo') is False
    assert plan.ordered(['echo']) == []


def test_stop_node_keeps_selected_plugins():
    wf = {
        'id': 9, 'name': 'stop',
        'nodes': [
            {'type': 'plugin', 'config': {'plugin': 'echo'}},
            {'type': 'stop', 'config': {}},
            {'type': 'plugin', 'config': {'plugin': 'aiwriter'}},
        ],
    }
    plan = apply_workflow(wf, user_id=1)
    assert plan.stopped is True
    assert plan.order == ['echo']


def test_duplicate_plugin_only_once():
    wf = {
        'id': 10, 'name': 'dup',
        'nodes': [
            {'type': 'plugin', 'config': {'plugin': 'echo'}},
            {'type': 'plugin', 'config': {'plugin': 'echo'}},
        ],
    }
    assert apply_workflow(wf).order == ['echo']


def test_validation_rejects_bad_nodes():
    assert validate_nodes([]) == []
    assert validate_nodes('not a list')
    assert validate_nodes([{'type': 'unknown'}])
    assert validate_nodes([{'type': 'plugin', 'config': {}}])
    assert validate_nodes([{'type': 'user', 'config': {'values': []}}])
    assert validate_nodes([{'type': 'plugin', 'config': {'plugin': '../bad'}}])


class FakeDB:
    def __init__(self):
        self.workflows = []
        self.bindings = []
        self.next_id = 1

    def query(self, sql, params=None):
        if 'SELECT id FROM workflows WHERE name=' in sql:
            return [{'id': r['id']} for r in self.workflows if r['name'] == params[0]]
        if 'FROM workflow_bindings' in sql:
            return [dict(r) for r in self.bindings]
        if 'FROM workflows' in sql:
            return [dict(r) for r in self.workflows]
        return []

    def execute(self, sql, params=None):
        if sql.startswith('INSERT INTO workflows'):
            self.workflows.append({
                'id': self.next_id, 'name': params[0], 'description': params[1],
                'nodes': params[2], 'enabled': params[3], 'is_default': params[4],
                'created_at': params[5], 'updated_at': params[6],
            })
            self.next_id += 1
        elif sql.startswith('INSERT INTO workflow_bindings'):
            self.bindings.append({
                'user_id': params[0], 'workflow_id': params[1],
                'enabled': params[2] if len(params) > 2 else 1,
            })
        elif sql.startswith('DELETE FROM workflow_bindings'):
            wid = params[0]
            self.bindings = [b for b in self.bindings if b['workflow_id'] != wid]
        elif sql.startswith('DELETE FROM workflows'):
            wid = params[0]
            self.workflows = [w for w in self.workflows if w['id'] != wid]
        elif sql.startswith('UPDATE workflows'):
            pass
        return 1


def test_store_routes_by_qq_and_default():
    db = FakeDB()
    store = WorkflowStore(db)
    wid = store.create('按用户', [
        {'type': 'user', 'config': {'values': ['555']}},
        {'type': 'plugin', 'config': {'plugin': 'echo'}},
    ])
    store.set_bindings(wid, ['555'])
    default_id = store.create('默认', [
        {'type': 'plugin', 'config': {'plugin': 'default_plugin'}},
    ], is_default=True)

    assert resolve(store, user_id=555).workflow_id == wid
    plan = resolve(store, user_id=999)
    assert plan.workflow_id == default_id
    assert plan.order == ['default_plugin']


def test_store_invalidate_sees_new_binding():
    db = FakeDB()
    store = WorkflowStore(db)
    wid = store.create('新链', [
        {'type': 'plugin', 'config': {'plugin': 'echo'}},
    ])
    assert resolve(store, user_id=111).allowed is None
    store.set_bindings(wid, ['111'])
    assert resolve(store, user_id=111).order == ['echo']


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])

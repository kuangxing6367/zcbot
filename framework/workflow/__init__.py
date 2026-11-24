"""消息处理链工作流：按 QQ 号选择节点链，决定本次消息的插件集合与顺序。

公开入口：

- ``WorkflowStore``：工作流定义与 QQ 绑定的数据库存取；
- ``resolve(store, user_id, group_id)``：生成一次事件的编排计划；
- ``validate_nodes(nodes)``：管理面保存前的节点校验。
"""
from framework.workflow.engine import apply_workflow, resolve, validate_nodes
from framework.workflow.models import WorkflowPlan, no_plan
from framework.workflow.store import WorkflowStore

__all__ = [
    'WorkflowPlan',
    'WorkflowStore',
    'apply_workflow',
    'no_plan',
    'resolve',
    'validate_nodes',
]

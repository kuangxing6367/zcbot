"""消息处理链工作流的数据模型。

一条工作流由**有序节点**组成，事件进来后按节点顺序逐条执行；节点本身不改
事件内容，只决定「本次消息允许哪些插件参与、按什么顺序参与、是否到此为止」。
插件真正的调用仍走现有的 raw handler 循环与 router 命令循环——工作流不复制
一套插件执行逻辑，只在两个循环之前算出本次的插件集合与顺序。

节点类型（``type``）：

- ``user``：按 QQ 号筛选，命中才继续往下走；不命中直接结束本次链；
- ``group``：按群号筛选（私聊视为空群号）；
- ``plugin``：把插件加入本次允许集合；同一插件可出现多次，按首次出现排序；
- ``exclude``：把插件从本次允许集合中移除（优先级高于 plugin）；
- ``stop``：终止链，后续节点不再执行。

节点字段 ``config`` 的类型随 type 变化：

- user/group：``{"values": [123456, 789]}``，支持整数或字符串形式的号码；
- plugin/exclude：``{"plugin": "echo"}``；
- stop：空字典。

未绑定任何工作流，或命中的工作流节点为空时，返回 ``None``——调用方必须
保持原有行为（全部插件按原优先级参与），这是兼容性的硬要求。
"""
from dataclasses import dataclass, field

#: 已知节点类型。未知类型在校验阶段直接拒绝，避免配置写错被静默忽略。
NODE_TYPES = frozenset({'user', 'group', 'plugin', 'exclude', 'stop'})


@dataclass
class WorkflowPlan:
    """一次事件的编排结果。

    ``allowed`` 为 None 表示「没有工作流，全部放行」；空集合表示「有工作流，
    但没有选中任何插件」，两者语义不同，调用方不能混用。
    """

    workflow_id: int = 0
    workflow_name: str = ''
    allowed: set = field(default_factory=set)
    excluded: set = field(default_factory=set)
    order: list = field(default_factory=list)
    stopped: bool = False
    matched_user: bool = False

    def allows(self, plugin_name: str) -> bool:
        """该插件本次是否可参与。没有工作流时一律放行。"""
        if plugin_name in self.excluded:
            return False
        if self.allowed is None:
            return True
        return plugin_name in self.allowed

    def ordered(self, plugin_order):
        """按工作流顺序重排插件队列；未被点名的插件一律不参与。

        ``excluded`` 的优先级高于 ``plugin``：先排除再排序，避免同一个插件
        既被选中又被排除时仍出现在队列里。
        """
        if self.allowed is None:
            return list(plugin_order)
        candidates = set(plugin_order) - self.excluded
        return [name for name in self.order
                if name in candidates and name not in self.excluded]

    def as_dict(self):
        return {
            'workflow_id': self.workflow_id,
            'workflow_name': self.workflow_name,
            'allowed': sorted(self.allowed or []),
            'excluded': sorted(self.excluded),
            'order': list(self.order),
            'stopped': self.stopped,
            'matched_user': self.matched_user,
        }


def no_plan():
    """没有命中工作流时的哨兵结果：放行全部插件，保持原顺序。"""
    plan = WorkflowPlan()
    plan.allowed = None
    return plan

"""消息处理链工作流的解析与执行计划生成。

设计约束：

- 纯内存、纯同步、无 IO。工作流定义由 store 从库里读到后交给本模块，
  每次事件只做几次字符串比较，不引入额外数据库往返；
- 没有命中工作流时返回 ``no_plan()``，调用方行为与改造前完全一致；
- 节点配置写错时不抛异常打断消息处理，只跳过该节点并留一条 debug 日志，
  由管理面的校验接口负责把错误暴露给使用者。
"""
import logging

from framework.workflow.models import NODE_TYPES, WorkflowPlan, no_plan

logger = logging.getLogger('zcbot')


def _as_ids(values):
    """节点里的 QQ/群号统一成字符串集合，避免 123 与 '123' 判成两条规则。"""
    if isinstance(values, (str, int)):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {str(v).strip() for v in values if str(v).strip()}


def validate_nodes(nodes):
    """校验节点定义，返回问题清单（空列表 = 合法）。

    给管理面保存前调用，也可以在加载时兜底。只允许已知 type，且每种 type
    的必要字段必须存在；未知字段忽略而不是报错，方便前端以后加字段。
    """
    issues = []
    if not isinstance(nodes, list):
        return ['节点列表必须是数组']
    if len(nodes) > 100:
        issues.append('节点数超过 100，已拒绝')
        return issues
    for idx, node in enumerate(nodes):
        label = f'节点 {idx + 1}'
        if not isinstance(node, dict):
            issues.append(f'{label} 不是对象')
            continue
        kind = str(node.get('type') or '').strip()
        if kind not in NODE_TYPES:
            issues.append(f'{label} 类型 [{kind}] 未知')
            continue
        config = node.get('config') or {}
        if not isinstance(config, dict):
            issues.append(f'{label} config 必须是对象')
            continue
        if kind in ('user', 'group') and not _as_ids(config.get('values')):
            issues.append(f'{label} 缺少 values')
        if kind in ('plugin', 'exclude'):
            name = str(config.get('plugin') or '').strip()
            if not name:
                issues.append(f'{label} 缺少 plugin')
            elif '/' in name or '\\' in name or '..' in name:
                issues.append(f'{label} 插件名包含非法字符')
    return issues


def apply_workflow(workflow, user_id=None, group_id=None):
    """按节点链生成一次事件的计划。

    :param workflow: dict，字段 ``id``/``name``/``nodes``；None 表示无工作流
    :param user_id: 本次事件的 QQ 号
    :param group_id: 本次事件的群号，私聊传 None
    """
    if not workflow or not isinstance(workflow, dict):
        return no_plan()

    nodes = workflow.get('nodes')
    if not isinstance(nodes, list) or not nodes:
        return no_plan()

    uid = str(user_id or '').strip()
    gid = str(group_id or '').strip()
    plan = WorkflowPlan(
        workflow_id=int(workflow.get('id') or 0),
        workflow_name=str(workflow.get('name') or ''),
        allowed=set(),
    )

    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = str(node.get('type') or '').strip()
        config = node.get('config') or {}
        if not isinstance(config, dict):
            continue
        if kind == 'user':
            if uid and uid in _as_ids(config.get('values')):
                plan.matched_user = True
            else:
                # 没点名这个 QQ：整条链不生效，退回默认行为。
                return no_plan()
        elif kind == 'group':
            if gid not in _as_ids(config.get('values')):
                return no_plan()
        elif kind == 'plugin':
            name = str(config.get('plugin') or '').strip()
            if name:
                plan.allowed.add(name)
                if name not in plan.order:
                    plan.order.append(name)
        elif kind == 'exclude':
            name = str(config.get('plugin') or '').strip()
            if name:
                plan.excluded.add(name)
        elif kind == 'stop':
            plan.stopped = True
            break
        else:
            logger.debug('工作流 [%s] 含未知节点类型 %r，已跳过',
                         plan.workflow_name, kind)

    # stop 节点之前的插件仍然允许执行；stop 只表示「链到此结束」，
    # 不是「清空已选插件」。若想一个插件都不跑，就不写 plugin 节点。
    return plan


def resolve(store, user_id=None, group_id=None):
    """从 store 取绑定并按 QQ 选工作流。

    匹配顺序：精确 QQ 绑定 → 默认工作流（is_default=1）。没有绑定也没有默认
    工作流时返回 ``no_plan()``。
    """
    if not store:
        return no_plan()
    workflow = store.for_user(user_id)
    if workflow is None:
        return no_plan()
    return apply_workflow(workflow, user_id=user_id, group_id=group_id)

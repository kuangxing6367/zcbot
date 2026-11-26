"""核心插件配置（core_plugins.yaml）与数据库的双向同步。

权威划分（用户拍板）
====================
- **文件是权威**：用户手改 core_plugins.yaml 后，启动/工具检测到变化即把
  文件内容**全量覆写**进 core_config 表（敏感键以 secretbox enc:v1 密文落库），
  并写入同步时间 synced_at。面板此前在库里的改动会被文件覆盖——这是刻意的：
  「用户直接改了原配置文件，重启后框架自动迁移」。
- **面板改动写库 + 回写文件**：面板 PUT 同时更新 core_config 与 yaml，
  保证下次启动文件不会把面板改动盖掉。
- **敏感值不暴露**：任何读取接口（含超管）对敏感键只给 `***`；要改就填新值覆盖。

yaml 文件带三类元信息（不破坏用户已有注释/结构）：
1. 顶部说明块（文件是权威 + 同步规则）；
2. 顶部 `synced_at: <unix>` 同步时间注释；
3. 每个配置项行的 `# id:N` 注释，指向 core_config 表主键。

实现刻意不引新依赖：db 用现有 Database 对象（query/execute），加密复用
framework.secretbox，yaml 解析/文本处理用 pyyaml + 逐行正则。
"""
import logging
import os
import re
import time

import yaml

logger = logging.getLogger('zcbot')

TABLE = 'core_config'

# 与 secretbox.SECRET_KEYS 对齐：这些键在库里存密文、在接口里给 ***
SECRET_KEYS = frozenset({
    'access_token', 'app_secret', 'api_key', 'client_secret', 'bot_secret',
    'webhook_secret', 'secret', 'token', 'secret_key', 'metrics_token',
})
MASK = '***'

# 顶部说明块（首次写入；已存在则不重复写）
_HEADER_TITLE = '# ===== ZCBOT 核心插件配置（core_plugins.yaml） ====='
_HEADER_LINES = [
    _HEADER_TITLE,
    '# 此文件是核心插件配置的权威来源。直接修改后重启框架，内容会自动覆写数据库。',
    '# 面板修改会同步写回本文件；每项 "# id:N" 对应数据库 core_config 表主键。',
    '# 敏感凭据（token/api_key/app_secret）在库中加密存储，接口一律显示 ***。',
    '# =====',
]

_SYNCED_RE = re.compile(r'^#\s*synced_at:\s*(\d+)\s*$')
# 匹配「缩进 + key: value（可带行尾注释）」，用于定位配置项行并插入 id 注释
_ITEM_RE = re.compile(r'^(\s*)([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$')
# 已有的 id 注释（避免在重复同步时叠加）
_ID_RE = re.compile(r'\s*#\s*id:\d+\s*$')


def _mask_value(key, value):
    if key in SECRET_KEYS:
        return MASK
    return value


def _is_secret(key):
    return key in SECRET_KEYS


def _read_yaml(path):
    """读 yaml 的 core_plugins 段；损坏返回空 dict（不抛，启动链路不该因它挂掉）"""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        cps = data.get('core_plugins') if isinstance(data, dict) else None
        return cps if isinstance(cps, dict) else {}
    except Exception as exc:
        logger.warning('读取 core_plugins.yaml 失败: %s', exc)
        return {}


def _encrypt_if_secret(key, value):
    """敏感键加密入库；空值/占位符不加密"""
    if not _is_secret(key) or not isinstance(value, str) or not value:
        return value
    if value == MASK or '${' in value or value.startswith('enc:v1:'):
        return value
    from framework import secretbox
    return secretbox.encrypt_value(value)


def _decrypt_if_secret(key, value):
    if not _is_secret(key) or not isinstance(value, str):
        return value
    from framework import secretbox
    return secretbox.decrypt_value(value)


def _flatten(cps):
    """展平成 (plugin_name, config_key, value) 列表；容器值 JSON 序列化。"""
    import json
    out = []
    for plugin, block in cps.items():
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if isinstance(value, (dict, list, tuple)):
                value = json.dumps(value, ensure_ascii=False)
            out.append((plugin, key, value))
    return out


def sync_file_to_db(db, yaml_path, synced_at=None):
    """把 core_plugins.yaml 全量覆写进 core_config（文件赢）。

    敏感键加密落库；写 synced_at 与 source='file'。
    返回 {'count': n, 'synced_at': int, 'ids': {(plugin,key): id}}。
    """
    if db is None:
        return {'count': 0, 'synced_at': 0, 'ids': {}}
    cps = _read_yaml(yaml_path)
    now = int(synced_at if synced_at is not None else time.time())
    items = _flatten(cps)
    synced = str(now)

    try:
        # 内容未变则只更新 synced_at，不重建行 —— 避免每次启动 id 无谓漂移，
        # 也避免把面板刚写的 source='panel' 误标回 'file'。
        existing_rows = db.query(
            f'SELECT id, plugin_name, config_key, config_value, source FROM {TABLE}')
        existing = {(r['plugin_name'], r['config_key']): r for r in existing_rows}
        want = {(p, k): _encrypt_if_secret(k, v) for p, k, v in items}
        same = set(existing) == set(want)
        if same:
            for key, row in existing.items():
                # 密文用 HMAC 化后比较：每次加密 nonce 不同，不能直接比字符串。
                # 用 decrypt 回原文比对（内部调用，不对外暴露）。
                cur = _decrypt_if_secret(key[1], row['config_value'])
                orig = want[key]
                orig_plain = _decrypt_if_secret(key[1], orig)
                if cur != orig_plain:
                    same = False
                    break
        if same:
            db.execute(f'UPDATE {TABLE} SET synced_at = %s', (synced,))
            ids = {(r['plugin_name'], r['config_key']): r['id'] for r in existing_rows}
            return {'count': len(items), 'synced_at': now, 'ids': ids, 'changed': False}

        # 全量覆写：先清表，再逐项插入，保证文件里删掉的配置项也从库里消失
        db.execute(f'DELETE FROM {TABLE}')
        ids = {}
        for plugin, key, value in items:
            stored = want[(plugin, key)]
            db.execute(
                f'INSERT INTO {TABLE} (plugin_name, config_key, config_value, synced_at, source) '
                'VALUES (%s, %s, %s, %s, %s)',
                (plugin, key, stored, synced, 'file'))
            row = db.query_one(
                f'SELECT id FROM {TABLE} WHERE plugin_name = %s AND config_key = %s',
                (plugin, key))
            ids[(plugin, key)] = row['id'] if row else None
        return {'count': len(items), 'synced_at': now, 'ids': ids, 'changed': True}
    except Exception as exc:
        logger.warning('同步 core_plugins.yaml → 数据库失败: %s', exc)
        return {'count': 0, 'synced_at': now, 'ids': {}, 'changed': False}


def get_id_map(db):
    """返回 {(plugin, key): id} 映射，供 yaml 注释使用。"""
    if db is None:
        return {}
    try:
        rows = db.query(f'SELECT id, plugin_name, config_key FROM {TABLE}')
        return {(r['plugin_name'], r['config_key']): r['id'] for r in rows}
    except Exception as exc:
        logger.warning('读取 core_config id 映射失败: %s', exc)
        return {}


def annotate_yaml(yaml_path, id_map, synced_at):
    """给 yaml 写顶部说明/同步时间，并给每个配置项加 # id:N 注释。

    保留用户原有的键顺序、值与行尾注释；只做三件事：
    - 顶部没有说明块 → 插入；
    - 顶部 synced_at 注释 → 更新为本次时间；
    - 配置项行 → 插入或更新 # id:N（已有则替换，不叠加）。
    """
    if not yaml_path or not os.path.isfile(yaml_path):
        return 0
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError as exc:
        logger.warning('读取 yaml 以写注释失败: %s', exc)
        return 0

    # 1. 顶部说明块
    if lines and lines[0] == _HEADER_TITLE:
        # 已有说明块：找到块尾 '# =====' 的第二个（块首也是），在其后更新 synced_at
        end = 1
        while end < len(lines) and lines[end] != '# =====':
            end += 1
        end += 1  # 跳过块尾
        # 移除旧的 synced_at 注释行（连续空行一并压缩）
        while end < len(lines) and (lines[end].strip() == '' or lines[end].startswith('# synced_at:')):
            lines.pop(end)
        header_end = end
    else:
        lines = _HEADER_LINES + [''] + lines
        header_end = len(_HEADER_LINES) + 1
    lines.insert(header_end, f'# synced_at: {int(synced_at)}')

    # 2. 给配置项行加 id 注释（只在 core_plugins: 段内）
    in_core = False
    changed = 0
    for i in range(header_end + 1, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith('core_plugins:'):
            in_core = True
            continue
        if not in_core or not stripped or stripped.startswith('#'):
            continue
        # 顶层新段开始（无缩进的 key）→ 离开 core_plugins 段
        if line and not line[0].isspace() and ':' in line:
            in_core = False
            continue
        m = _ITEM_RE.match(line)
        if not m:
            continue
        indent, key, rest = m.groups()
        # 只处理「plugin 块内的配置项」，跳过 plugin 名这一层（rest 为空或纯注释）
        if not rest.strip() or rest.strip().startswith('#'):
            continue
        # 推断 plugin 名：向上找最近的无更深缩进的 key 行
        plugin = None
        for j in range(i - 1, -1, -1):
            pm = _ITEM_RE.match(lines[j])
            if not pm:
                continue
            pindent, pkey, prest = pm.groups()
            if len(pindent) < len(indent):
                plugin = pkey
                break
        if not plugin:
            continue
        cid = id_map.get((plugin, key))
        if cid is None:
            continue
        # 去掉旧 id 注释再追加新的
        rest_clean = _ID_RE.sub('', rest).rstrip()
        new_line = f'{indent}{key}: {rest_clean}  # id:{cid}'
        if new_line != line:
            lines[i] = new_line
            changed += 1

    try:
        tmp = yaml_path + '.part'
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            f.write('\n'.join(lines) + '\n')
        os.replace(tmp, yaml_path)
        return changed
    except OSError as exc:
        logger.warning('写回 yaml 注释失败: %s', exc)
        return 0


def sync_and_annotate(db, yaml_path):
    """文件→库同步 + 回写 yaml 元信息（启动/工具调用的统一入口）。"""
    info = sync_file_to_db(db, yaml_path)
    if info['count']:
        id_map = get_id_map(db)
        annotated = annotate_yaml(yaml_path, id_map, info['synced_at'])
        logger.info('核心插件配置已同步: %d 项 → 数据库，yaml 更新 %d 处注释',
                    info['count'], annotated)
    return info


def get_config_for_api(db, masked=True):
    """从 core_config 读配置给面板：解密敏感值，按 masked 决定是否打码。

    返回 [{'id','plugin','key','value','synced_at','source','sensitive'}]。
    注意：masked=False 仅用于内部运行时读取，**任何对外接口都应 masked=True**
    （超管也一样，用户明确要求"一律只看 ***"）。
    """
    if db is None:
        return []
    try:
        rows = db.query(
            f'SELECT id, plugin_name, config_key, config_value, synced_at, source '
            f'FROM {TABLE} ORDER BY plugin_name, config_key')
    except Exception as exc:
        logger.warning('读取 core_config 失败: %s', exc)
        return []
    out = []
    for r in rows:
        key = r['config_key']
        sensitive = _is_secret(key)
        value = _decrypt_if_secret(key, r['config_value'])
        if masked and sensitive:
            value = MASK
        out.append({
            'id': r['id'],
            'plugin': r['plugin_name'],
            'key': key,
            'value': value,
            'synced_at': r['synced_at'],
            'source': r['source'],
            'sensitive': sensitive,
        })
    return out


def update_from_panel(db, yaml_path, plugin, key, value):
    """面板修改单项配置：写库（敏感值加密）+ 回写 yaml 对应行。

    敏感值若回传 MASK 占位符则忽略（不把 '***' 当真值写进去）。
    写库后同步 yaml 的 synced_at 与该项 id 注释。
    """
    if db is None or not plugin or not key:
        return False
    if value == MASK:
        return False
    stored = _encrypt_if_secret(key, value)
    try:
        existing = db.query_one(
            f'SELECT id FROM {TABLE} WHERE plugin_name = %s AND config_key = %s',
            (plugin, key))
        now = str(int(time.time()))
        if existing:
            db.execute(
                f'UPDATE {TABLE} SET config_value = %s, synced_at = %s, source = %s '
                'WHERE plugin_name = %s AND config_key = %s',
                (stored, now, 'panel', plugin, key))
        else:
            db.execute(
                f'INSERT INTO {TABLE} (plugin_name, config_key, config_value, synced_at, source) '
                'VALUES (%s, %s, %s, %s, %s)',
                (plugin, key, stored, now, 'panel'))
        # 回写 yaml:改对应行 + 更新同步时间/id 注释
        _patch_yaml_value(yaml_path, plugin, key, value)
        id_map = get_id_map(db)
        annotate_yaml(yaml_path, id_map, now)
        return True
    except Exception as exc:
        logger.warning('面板更新 core_config 失败: %s', exc)
        return False


def _patch_yaml_value(yaml_path, plugin, key, value):
    """把 yaml 里 plugin.key 的值替换为 value（保留缩进/注释风格）。"""
    if not yaml_path or not os.path.isfile(yaml_path):
        return False
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    import json
    in_plugin = False
    plugin_indent = None
    for i, line in enumerate(lines):
        m = _ITEM_RE.match(line)
        if not m:
            continue
        indent, k, rest = m.groups()
        depth = len(indent)
        # plugin 块头：键名等于 plugin 且该行无值（rest 为空/注释）
        if k == plugin and (not rest.strip() or rest.strip().startswith('#')):
            in_plugin = True
            plugin_indent = depth
            continue
        # 遇到同级或更浅的新块 → 离开当前 plugin
        if in_plugin and depth <= plugin_indent and k != plugin:
            in_plugin = False
        if in_plugin and depth > plugin_indent and k == key:
            # 一行值（容器用 JSON 内联，不追求完全还原用户排版）
            if isinstance(value, (dict, list, tuple)):
                val = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, str):
                val = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
                if val.endswith('...\n'):
                    val = val[:-4]
            else:
                val = str(value)
            prefix = len(indent) - len(indent.lstrip(' '))
            lines[i] = f'{indent}{k}: {val}'
            break
    try:
        tmp = yaml_path + '.part'
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            f.write('\n'.join(lines) + '\n')
        os.replace(tmp, yaml_path)
        return True
    except OSError:
        return False

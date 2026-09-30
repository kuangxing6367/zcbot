# -*- coding: utf-8 -*-
"""
LLM 核心装载器（llm_load）

职责只有一件：**保证 plugins/llm_core 存在且与官方载荷逐字节一致**。

为什么要绕这么一圈：LLM 这套东西会持续演进（多模型、多 MCP server、新工具），
它不该像普通官方插件那样锁在 core_plugins 里等下一次框架升级；把它放到 plugins/
既能热改、又能由官方保证「不会缺件、不会被改坏」。于是分成两半：

    core_plugins/llm_load        官方插件，很薄，只做校验 / 释放 / 重启
    core_plugins/llm_load/llm_core.zip   官方载荷（md5 manifest）
    plugins/llm_core            运行时主体，由上面的 zip 释放得到

启动流程：

    1. 读 zip 里的 manifest.json（版本 + 每个文件的 md5）
    2. 逐个文件比对目标目录
    3. 有缺失或哈希不符 → 解压覆盖 → 立刻 os.execv 就地重启框架

第 3 步的重启是必需的：插件加载发生在启动早期，晚于本插件才轮到用户插件，
不重启这一轮就用不上。之所以敢「即刻重启」而不是先报警等人来：写入的是 manifest
里写死的字节，写完后会**再校验一遍**，校验不过就不重启——因此不存在重启死循环。

对齐策略是**严格的**：任何被 manifest 收录的文件只要和目标不一致就覆盖回去，
包括在运行时被人手改坏的部分。想长期保留自己的改动，请改
``core_plugins/llm_load/src/llm_core/`` 里的源码再重新打包
（``python tools/build_llm_payload.py --write``）——释放由每个文件的 md5 比对
触发，内容变了即生效，无需手动加版本号。

manifest 未收录的文件（比如你自己放的数据文件）不会被删。

命令：

    /llmload status     查看装载状态（版本 / 文件数 / 差异）
    /llmload verify     立即校验，输出不一致的文件
    /llmload reinstall  强制重新释放并重启（慎用：会抹掉目标目录里被改过的文件）

启用（core_plugins.yaml 是唯一权威）：

    python tools/scan_core_plugins.py --enable llm_load
"""
import hashlib
import json
import logging
import os
import sys
import zipfile

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "LLM 装载器",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "保证 plugins/llm_core 与官方载荷一致：缺失/损坏即释放并重启框架",
    "priority": 5,          # 仅作展示：llm_load 是 core 插件，核心阶段先于所有用户插件加载，
                            # 自检因此在 plugins/llm_core 被当作用户插件加载之前完成
    "official": True,
}

_MANIFEST_NAME = 'manifest.json'
_DEFAULT_TARGET = 'llm_core'

# 框架自身的约定：插件根目录下的配置/文档文件会被 migrate_legacy_configs 迁到
# plugins_dat/<插件名>/（见 framework/loader/base.py 的 _CONFIG_FILE_NAMES /
# _CONFIG_FILE_EXTS）。这批文件的「家」在 plugins_dat，不在 plugins 目录——
# 装载器必须跟着这个约定走，否则每次启动都会认为它们「缺失」，
# 于是每次都释放、每次都重启，形成启动即重启的死循环。
_MIGRATABLE_NAMES = {
    'plugin.yaml', '_conf_schema.json', 'metadata.yaml',
    'README.md', 'README_zh.md', 'README_ru.md', 'CHANGELOG.md', 'LICENSE',
}
_MIGRATABLE_EXTS = ('.yaml', '.yml', '.toml', '.cfg', '.ini', '.md')


def is_migratable(rel: str) -> bool:
    """该文件是否属于「会被框架迁到 plugins_dat 的那批」"""
    name = os.path.basename(rel or '')
    return name in _MIGRATABLE_NAMES or name.lower().endswith(_MIGRATABLE_EXTS)


# ── 纯函数：不依赖框架，测试直接调用 ──────────────────────────

def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def read_manifest(zip_path: str) -> dict:
    """读取载荷里的 manifest（版本 + 文件哈希表）"""
    if not os.path.isfile(zip_path):
        raise FileNotFoundError(f"载荷不存在: {zip_path}")
    with zipfile.ZipFile(zip_path, 'r') as zf:
        if _MANIFEST_NAME not in zf.namelist():
            raise ValueError(f"载荷缺少 {_MANIFEST_NAME}: {zip_path}")
        return json.loads(zf.read(_MANIFEST_NAME).decode('utf-8'))


def verify(target_dir: str, manifest: dict, dat_dir: str = None) -> dict:
    """比对目标目录与 manifest，返回差异报告。

    :param dat_dir: 插件数据目录（plugins_dat/<插件名>）。给了它之后，
        配置/文档类文件会按框架约定优先在数据目录里找，避免把「已被迁移」
        误判成「缺失」。
    :return: {'ok': bool, 'version': str, 'total': int,
              'missing': [...], 'mismatch': [...]}
    """
    files = manifest.get('files') or {}
    missing, mismatch = [], []
    for rel, spec in files.items():
        candidates = [os.path.join(target_dir, rel)]
        if dat_dir and is_migratable(rel):
            # 数据目录优先：文件被迁移后就只在那儿
            candidates.insert(0, os.path.join(dat_dir, rel))
        path = next((p for p in candidates if os.path.isfile(p)), None)
        if path is None:
            missing.append(rel)
            continue
        try:
            with open(path, 'rb') as f:
                digest = md5_of(f.read())
        except OSError as e:
            mismatch.append(f"{rel}（不可读: {e}）")
            continue
        if digest != spec.get('md5'):
            mismatch.append(rel)
    return {
        'ok': not missing and not mismatch,
        'version': manifest.get('version', '?'),
        'total': len(files),
        'missing': missing,
        'mismatch': mismatch,
    }


def release(zip_path: str, target_dir: str, manifest: dict, dat_dir: str = None) -> dict:
    """把 manifest 收录的文件全部写进目标目录（缺目录连带创建）。

    配置/文档类文件在给了 dat_dir 时直接写到数据目录——那是框架给它们定的家，
    写着代码目录的话下一次启动又会被迁走，白折腾一轮。

    :return: {'written': [...], 'failed': [(rel, reason), ...]}
    """
    written, failed = [], []
    plugin_name = manifest.get('plugin') or _DEFAULT_TARGET
    prefix = f"{plugin_name}/"
    with zipfile.ZipFile(zip_path, 'r') as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    for rel, spec in (manifest.get('files') or {}).items():
        member = prefix + rel
        if member not in members:
            failed.append((rel, '载荷内缺少该文件'))
            continue
        data = members[member]
        if spec.get('md5') and md5_of(data) != spec['md5']:
            failed.append((rel, '载荷内文件哈希与 manifest 不符'))
            continue
        base = (dat_dir if (dat_dir and is_migratable(rel)) else target_dir)
        dest = os.path.join(base, *rel.split('/'))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + '.tmp_release'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, dest)
            written.append(rel)
        except OSError as e:
            failed.append((rel, str(e)))
    return {'written': written, 'failed': failed}


# ── 注册入口 ──────────────────────────────────────────────────

def register(ctx):
    fw = ctx._framework
    cfg_dict = fw.config.get('llm_load', {}) or {}

    here = os.path.dirname(os.path.abspath(__file__))
    zip_path = cfg_dict.get('payload') or os.path.join(here, 'llm_core.zip')
    target_name = cfg_dict.get('plugin_name') or _DEFAULT_TARGET
    auto_restart = cfg_dict.get('auto_restart', True)

    plugins_dir = getattr(fw.plugin_loader, 'plugins_dir', None)
    if not plugins_dir:
        logger.error("[llm_load] 取不到 plugins 目录，跳过自检")
        return
    target_dir = os.path.join(plugins_dir, target_name)
    dat_dir = getattr(fw.plugin_loader, 'plugins_dat_dir', None)
    dat_dir = os.path.join(dat_dir, target_name) if dat_dir else None

    # 模块级状态：给命令处理器用
    global _CTX, _CFG
    _CTX, _CFG = ctx, {
        'zip_path': zip_path, 'target_dir': target_dir, 'dat_dir': dat_dir,
        'target_name': target_name, 'auto_restart': auto_restart,
    }

    try:
        manifest = read_manifest(zip_path)
    except FileNotFoundError as e:
        logger.error(f"[llm_load] {e}；请先执行 python tools/build_llm_payload.py --write")
        return
    except Exception as e:  # noqa: BLE001
        logger.error(f"[llm_load] 载荷不可用: {e}")
        return

    report = verify(target_dir, manifest, dat_dir)
    if report['ok']:
        logger.info(f"[llm_load] {target_name} v{report['version']} 校验通过"
                    f"（{report['total']} 个文件）")
        _register_commands(ctx)
        return

    logger.warning(f"[llm_load] {target_name} v{report['version']} 需要修复："
                   f"缺失 {len(report['missing'])} 个 / "
                   f"不一致 {len(report['mismatch'])} 个 → 开始释放")

    outcome = release(zip_path, target_dir, manifest, dat_dir)
    if outcome['failed']:
        logger.error(f"[llm_load] 释放失败 {len(outcome['failed'])} 个文件，"
                     f"放弃重启：" + '; '.join(f"{r}: {m}" for r, m in outcome['failed'][:5]))
        _register_commands(ctx)
        return

    # 关键：写入后再校验一遍。只有真的对齐了才重启，杜绝反复重启。
    after = verify(target_dir, manifest, dat_dir)
    logger.info(f"[llm_load] 已释放 {len(outcome['written'])} 个文件到 plugins/{target_name}"
                f"（v{manifest.get('version')}）")
    _register_commands(ctx)

    if not after['ok']:
        logger.error(f"[llm_load] 释放后仍不一致（缺失 {after['missing']} / "
                     f"不一致 {after['mismatch']}），不重启，请检查磁盘权限")
        return
    if not auto_restart:
        logger.warning("[llm_load] auto_restart=false，已释放但未重启，"
                       "新版本将在下次启动生效")
        return
    _restart_now(ctx, fw)


def _register_commands(ctx):
    try:
        ctx.command('/llmload', handle_llmload, require_admin=True,
                    description="LLM 装载器：status / verify / reinstall")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[llm_load] 注册命令失败: {e}")


def _restart_now(ctx, fw):
    """os.execv 原地重启当前进程。

    双进程模式下宿主进程不能自己 execv 成 main.py（会变成第二个核心进程），
    这种情况跳过重启，由核心进程处理。
    """
    role = getattr(fw, '_role', 'standard')
    if role == 'host':
        logger.warning("[llm_load] 宿主进程不自行重启，等待核心进程处理")
        return

    main_py = os.path.abspath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'main.py'))
    if not os.path.isfile(main_py):
        logger.error(f"[llm_load] 找不到 main.py，无法重启: {main_py}")
        return

    logger.info(f"[llm_load] 载荷已更新，正在重启框架使其生效: {main_py}")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        os.chdir(os.path.dirname(main_py))
        os.execv(sys.executable, [sys.executable, main_py] + sys.argv[1:])
    except Exception as e:  # noqa: BLE001
        logger.error(f"[llm_load] 重启失败: {e}")


def _state():
    """返回当前装载状态报告（命令与测试共用）"""
    cfg = _CFG or {}
    zip_path = cfg.get('zip_path')
    if not zip_path:
        return {'ok': False, 'error': '装载器尚未初始化'}
    try:
        manifest = read_manifest(zip_path)
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}
    report = verify(cfg['target_dir'], manifest, cfg.get('dat_dir'))
    report.update({
        'plugin': manifest.get('plugin'),
        'payload_version': manifest.get('version'),
        'built_at': manifest.get('built_at'),
        'target': cfg.get('target_dir'),
        'zip': zip_path,
    })
    return report


def handle_llmload(event, match):
    """查看/修复 LLM 核心装载状态: /llmload status|verify|reinstall"""
    if _CTX is None:
        return
    arg = ((match.group(1) if match else '') or 'status').strip().lower() or 'status'
    report = _state()

    if arg == 'status':
        if report.get('error'):
            msg = f"llm_load 未就绪：{report['error']}"
        else:
            msg = (f"llm_core v{report['version']}\n"
                   f"文件 {report['total']} 个，状态："
                   f"{'一致' if report['ok'] else '不一致'}\n"
                   f"缺失 {len(report['missing'])} / 不一致 {len(report['mismatch'])}")
    elif arg == 'verify':
        if report.get('error'):
            msg = f"llm_load 未就绪：{report['error']}"
        elif report['ok']:
            msg = f"校验通过：{report['total']} 个文件与载荷一致"
        else:
            detail = '\n'.join(
                [f"  缺失 {r}" for r in report['missing'][:10]] +
                [f"  不一致 {r}" for r in report['mismatch'][:10]])
            msg = f"发现差异：\n{detail}\n\n重启框架即可自动修复"
    elif arg == 'reinstall':
        try:
            manifest = read_manifest(_CFG['zip_path'])
            outcome = release(_CFG['zip_path'], _CFG['target_dir'], manifest,
                                 _CFG.get('dat_dir'))
            after = verify(_CFG['target_dir'], manifest, _CFG.get('dat_dir'))
            msg = (f"已重新释放 {len(outcome['written'])} 个文件"
                   + (f"，失败 {len(outcome['failed'])} 个" if outcome['failed'] else "")
                   + f"\n状态：{'一致' if after['ok'] else '仍不一致'}"
                   + "\n需要重启框架后生效")
        except Exception as e:  # noqa: BLE001
            msg = f"重装失败：{e}"
    else:
        msg = "用法：/llmload status | verify | reinstall"

    _CTX.send_msg(user_id=event.user_id,
                  group_id=event.group_id if event.is_group else None,
                  message=msg)


_CTX = None
_CFG = None

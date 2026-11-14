"""启动前依赖自检：扫描 requirements.txt，自动安装缺失依赖。

解决移机/首次部署时依赖缺失导致 import 报错的问题。

依赖项可被 config.yaml 的 dependencies.allow_missing 豁免：豁免的缺失包只记
WARNING，不安装、不打断启动、不退出（详见该配置段说明）。
"""
import importlib.metadata as _imd
import os
import re
import sys
import tempfile

# 统一走 paths（stdlib only，可在 yaml 未安装之前 import）
from framework.paths import code_root, resolve_config_path

_PROJECT_ROOT = code_root()


def _norm(name: str) -> str:
    return name.strip().lower().replace('_', '-')


def read_self_check_allow_missing(config_path=None):
    """读取 config.yaml 的 dependencies.allow_missing（在 import framework 之前，只用标准库 yaml）。

    返回规范名集合（小写、连字符/下划线等价）。读取失败返回空集（不影响既有行为）。
    """
    path = resolve_config_path(config_path)
    try:
        import yaml
        with open(path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        deps = cfg.get('dependencies') or {}
        allow = deps.get('allow_missing') or []
        if isinstance(allow, str):
            allow = [allow]
        norm = lambda n: str(n).strip().lower().replace('_', '-')
        return {norm(x) for x in allow if x}
    except Exception:
        return set()


def resolve_self_check_missing(missing, allow_missing):
    """把 requirements 解析出的缺失规格，按配置过滤出「必须处理」清单。

    :param missing: requirements 中「已缺失」的原始规格行列表（含版本约束）
    :param allow_missing: 允许缺失的规范包名集合
    :return: (to_install, skipped_allowed)
        to_install   —— 必须安装（不在 allow_missing 中）的规格
        skipped_allowed —— 被 allow_missing 豁免、跳过安装也不报错的规格
    """
    norm = lambda n: n.strip().lower().replace('_', '-')
    allowed = {norm(x) for x in allow_missing}
    to_install, skipped = [], []
    for spec in missing:
        pkg = re.match(r'^([a-zA-Z0-9_.\-]+)', spec)
        if pkg and norm(pkg.group(1)) in allowed:
            skipped.append(spec)
        else:
            to_install.append(spec)
    return to_install, skipped


def check_and_install_deps():
    """
    启动前自检：扫描 requirements.txt，自动安装缺失的依赖
    解决移机/首次部署时依赖缺失导致 import 报错的问题。

    依赖项可被 config.yaml 的 dependencies.allow_missing 豁免：豁免的缺失包只记
    WARNING，不安装、不打断启动、不退出（详见该配置段说明）。
    """
    # 一次扫描所有已安装发行版，建立规范名集合，避免对每个依赖各做一次 metadata 查找
    installed = {_norm(d.metadata['Name']) for d in _imd.distributions()}

    req_file = os.path.join(code_root(), 'requirements.txt')
    if not os.path.isfile(req_file):
        return

    # 解析 requirements.txt，提取缺失规格（保留原始写法，含版本约束）
    missing = []
    with open(req_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.match(r'^([a-zA-Z0-9_.\-]+)', line)
            if not m:
                continue
            if _norm(m.group(1)) not in installed:
                missing.append(line)

    if not missing:
        return

    allow_missing = read_self_check_allow_missing()
    to_install, skipped = resolve_self_check_missing(missing, allow_missing)

    if skipped:
        print(f"[自检] 以下缺失依赖在配置中标记为允许缺失，已跳过（不安装、不报错）: "
              f"{', '.join(skipped)}")

    if not to_install:
        return

    print(f"[自检] 检测到 {len(to_install)} 个缺失依赖，正在自动安装...")
    print(f"[自检] 缺失: {', '.join(to_install)}")

    # 只安装「必需子集」：把 to_install 规格写入临时 requirements，避免把 allow_missing
    # 豁免的包也塞进整文件安装（那些包即便缺失也按配置容忍）。
    # 只走 framework.deps.pip（纯标准库），不能 import framework.loader ——
    # loader/base 顶层 import yaml，缺的正是它，会让自检安装器自身崩溃
    tmp_req = None
    result = {'success': False, 'mirror': '', 'error': '未执行安装'}
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='zcbot_req_', suffix='.txt')
        os.close(fd)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(to_install) + '\n')
        tmp_req = tmp_path
        from framework.deps.pip import pip_install_requirements
        result = pip_install_requirements(sys.executable, tmp_req, timeout=300)
    finally:
        if tmp_req and os.path.isfile(tmp_req):
            try:
                os.remove(tmp_req)
            except OSError:
                pass

    if result['success']:
        print(f"[自检] 依赖安装完成（镜像: {result['mirror']}）")
    else:
        print(f"[自检] 依赖安装失败: {result['error']}")
        print("[自检] 请手动执行: pip install -r requirements.txt")
        sys.exit(1)


def ensure_optional_driver(config_path=None):
    """启动前（**事件循环之外**）按需补装可选数据库驱动。

    requirements.txt 里 pymysql / DBUtils 是注释掉的可选依赖，通用自检不会装。
    以前由 `db_conn._init_mysql()` 在 Framework() 构造里 `subprocess.run(pip)`
    兜底，而那条调用栈已经在 `asyncio.run(amain())` 里面 —— 缺驱动时会同步
    阻塞整个事件循环最长 120s，启动期所有异步任务全部停摆。

    这里把它前移到 main() 的同步阶段：只有配置明确为 MySQL 且驱动确实缺失
    时才装，走与通用自检相同的镜像/回退管线；装不上只打印指引，不打断启动
    （运行期会给出清晰的 ImportError）。
    """
    path = resolve_config_path(config_path)
    try:
        import yaml
        with open(path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return  # 读不到配置就不猜；自定义配置格式由运行期自己处理
    db = cfg.get('database') or {}
    if not isinstance(db, dict) or str(db.get('type') or '').lower() != 'mysql':
        return

    import importlib.util
    need = []
    if importlib.util.find_spec('pymysql') is None:
        need.append('pymysql>=1.0.0')
    if importlib.util.find_spec('dbutils') is None:
        need.append('DBUtils>=3.0.0')
    if not need:
        return

    print(f"[自检] MySQL 模式缺少驱动（{', '.join(need)}），启动前安装...")
    tmp_req = None
    try:
        fd, tmp_req = tempfile.mkstemp(prefix='zcbot_dbdrv_', suffix='.txt')
        os.close(fd)
        with open(tmp_req, 'w', encoding='utf-8') as f:
            f.write('\n'.join(need) + '\n')
        from framework.deps.pip import pip_install_requirements
        result = pip_install_requirements(sys.executable, tmp_req, timeout=180)
    except Exception as e:  # noqa: BLE001
        print(f"[自检] 数据库驱动安装异常: {e}")
        return
    finally:
        if tmp_req and os.path.isfile(tmp_req):
            try:
                os.remove(tmp_req)
            except OSError:
                pass

    if result.get('success'):
        print(f"[自检] 数据库驱动安装完成（镜像: {result.get('mirror', '')}）")
    else:
        print(f"[自检] 数据库驱动安装失败: {result.get('error', '')}")
        print("[自检] 请手动执行: pip install pymysql DBUtils")

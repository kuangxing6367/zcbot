"""数据库自动备份（企业视角）。

为什么单独一层，而不是继续堆在 ``migrations.backup_snapshot`` 里：
迁移前备份的语义是「马上要改结构，先留个回退点」，失败可以接受（记 binlog
位点或明确告警）。自动备份的语义不同——它长期无人看着跑，必须自己回答
「空间够不够 / 写进去是不是真的落地 / 太大要不要停下来问人」，还得能轮转。
两者共用同一套路径解析与轮转口径，但决策流程不一样，所以分开放。

一次备份的流程（每一步都可能中止，中止原因必须让人看得懂）::

    估算体积 → 可用空间判定（不够则弹性探测）→ 快照到 .part
    → 体积上限检查 → 超过阈值则 zip → 读回复核 → 改名转正 → 轮转

几条刻意的取舍：

- **快照先写 .part，校验过了才改名**。有些环境（LXC / overlay 上层 / 网络盘）
  写文件会「报成功但内容不对」，直接写正式文件名等于把坏文件当备份用。
- **弹性探测只探不收**：写满到 ``min_free_after``（默认 512KB）立刻停手并删除
  探针文件。这不是「填满磁盘」，是「探到边界就撤」——真填满会让正在跑的
  数据库/日志写入失败，那比没有备份更糟。
- **超过 ``refuse_above_mb`` 就停下来问人**，不自动删、不自动截断。库突然从
  几 MB 涨到几百 MB 通常意味着出了事（日志表爆炸 / 循环写入），这时候悄悄
  备份反而是掩盖问题。
"""
import logging
import os
import shutil
import threading
import time
import zipfile
from datetime import datetime, timezone

logger = logging.getLogger('zcbot')

# 自动备份文件名前缀。与迁移备份（zcbot-schema-）刻意分开：
# 轮转要按来源分开数份数，混在一个前缀里会互相挤掉。
BACKUP_PREFIX = 'zcbot-backup-'
STATE_FILE = 'state.json'
PART_SUFFIX = '.part'

DEFAULTS = {
    'enabled': True,
    # 每 6 小时一次。用户拍板的口径就是「按间隔」，不是每天定点。
    'interval_hours': 6,
    # 启动后等一会儿再跑首次，避开「刚起来插件还在加载、库还在迁移」的窗口。
    # 0 表示不在启动后自动跑首次。
    'initial_delay_minutes': 5,
    # 保留份数。用户拍板：与迁移前备份统一都是 2 份。
    'keep': 2,
    'compress': True,
    # 「默认不大于 100KB」：超过这个体积就打包压缩试试，而不是硬拒绝。
    'compress_threshold_kb': 100,
    'compress_level': 1,
    # 超过 100MB 就不备份了，提示人去确认（除非配置放开）。
    'refuse_above_mb': 100,
    'allow_large': False,
    # 探针停手线：写到只剩这么点就收。
    'min_free_after_mb': 0.5,
    # 快照体积 ≈ 库体积，额外留 10% 给临时文件与日志。
    'headroom_ratio': 1.10,
    'probe_chunk_kb': 1024,
    # 探针硬上限，防止 need 算错时写出几十 GB。
    'probe_max_mb': 4096,
    # 读回复核：zip 走 CRC，sqlite 走 quick_check。
    'verify': True,
    'verify_sqlite': True,
}


def _num(cfg, key, default=None, cast=float, minimum=None, maximum=None):
    """取数值并钳制。配置写错不该让备份线程崩，只该让它退回默认值。"""
    raw = cfg.get(key, DEFAULTS.get(key, default) if default is None else default)
    try:
        val = cast(raw)
    except (TypeError, ValueError):
        logger.warning("备份配置 %s=%r 不是有效数字，退回默认值 %r",
                       key, raw, DEFAULTS.get(key))
        return cast(DEFAULTS.get(key, 0))
    if minimum is not None and val < minimum:
        logger.warning("备份配置 %s=%r 低于下限，已按 %r 处理", key, val, minimum)
        val = minimum
    if maximum is not None and val > maximum:
        logger.warning("备份配置 %s=%r 超过上限，已按 %r 处理", key, val, maximum)
        val = maximum
    return val


def load_config(cfg=None) -> dict:
    """把 ``database.backup`` 归一成可直接用的 dict（含钳制）。"""
    cfg = cfg or {}
    if not isinstance(cfg, dict):
        cfg = {}
    out = {
        'enabled': bool(cfg.get('enabled', DEFAULTS['enabled'])),
        'interval_hours': _num(cfg, 'interval_hours', minimum=0.05, maximum=24 * 30),
        'initial_delay_minutes': _num(cfg, 'initial_delay_minutes', minimum=0),
        'keep': int(_num(cfg, 'keep', minimum=1, maximum=100)),
        'compress': bool(cfg.get('compress', DEFAULTS['compress'])),
        'compress_threshold_kb': _num(cfg, 'compress_threshold_kb', minimum=0),
        'compress_level': int(_num(cfg, 'compress_level', minimum=0, maximum=9)),
        'refuse_above_mb': _num(cfg, 'refuse_above_mb', minimum=0),
        'allow_large': bool(cfg.get('allow_large', DEFAULTS['allow_large'])),
        'min_free_after_mb': _num(cfg, 'min_free_after_mb', minimum=0.05),
        'headroom_ratio': _num(cfg, 'headroom_ratio', minimum=1.0, maximum=10.0),
        'probe_chunk_kb': _num(cfg, 'probe_chunk_kb', minimum=64),
        'probe_max_mb': _num(cfg, 'probe_max_mb', minimum=1),
        'verify': bool(cfg.get('verify', DEFAULTS['verify'])),
        'verify_sqlite': bool(cfg.get('verify_sqlite', DEFAULTS['verify_sqlite'])),
    }
    if not out['compress']:
        out['compress_threshold_kb'] = float('inf')
    return out


# ── 路径与空间 ────────────────────────────────────────────────

def resolve_db_path(database) -> str:
    """拿到数据库**实际打开**的路径。

    配置里的 ``data/zcbot.db`` 是相对值，连接层按 ``get_home()`` 解析成
    ``<home>/data/zcbot.db``；这里若对它做 ``os.path.abspath()``，会被解析成
    ``<cwd>/data/zcbot.db`` —— 两者在「工作目录 ≠ home」时（pip 安装态、
    systemd 的 WorkingDirectory、容器挂载）根本不是同一个文件。

    这段解析口径与 ``migrations.backup_snapshot`` 共用，改一处即可。
    """
    path = str(getattr(database, '_db_path', '') or '')
    if path:
        return path
    path = str((getattr(database, 'config', {}) or {}).get('path') or '')
    if not path:
        return ''
    if not os.path.isabs(path):
        try:
            from framework.config import get_home
            path = os.path.normpath(os.path.join(get_home(), path))
        except Exception as _exc:
            logger.debug('[resolve_db_path] get_home 不可用，退回 cwd: %s', _exc)
            path = os.path.abspath(path)
    return path


def free_bytes(path: str) -> int:
    """目标目录所在卷的可用字节数；取不到返回 -1（-1 = 未知，不是「没空间」）。"""
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return int(shutil.disk_usage(probe or '.').free)
    except Exception as e:
        logger.debug('[free_bytes] 读取可用空间失败 %s: %s', probe, e)
        return -1


def _mb(n, unit=None) -> str:
    """人类可读体积。小文件按 KB 显示，否则 `0.0 MB` 这种数字看不出差别。"""
    n = float(n or 0)
    if unit is None:
        unit = 'KB' if abs(n) < 1048576 else 'MB'
    if unit == 'KB':
        return '%.1f KB' % (n / 1024.0)
    return '%.1f MB' % (n / 1048576.0)


# 探测写缓冲：固定 64KB 复用。**不按配置的块长分配**——否则
# `probe_chunk_kb: 4096` 会在写第一个字节之前先吃掉 4GB 内存。
_PROBE_SLICE = 64 * 1024
_PROBE_BUF = b'\xa5' * _PROBE_SLICE
# 每片在停手线之上再留的余量：文件系统元数据、日志、别的进程都可能在这
# 一瞬间吃掉几 KB，留白是为了不踩线。
_PROBE_MARGIN = 256 * 1024


def _write_slices(f, size: int) -> None:
    """按 64KB 切片写入，最后一片按需截断。

    写非零字节：稀疏文件只写 0 可能根本不分配块，测出来的「空间够」是假的。
    """
    left = int(size)
    while left > 0:
        n = _PROBE_SLICE if left >= _PROBE_SLICE else left
        f.write(_PROBE_BUF if n == _PROBE_SLICE else _PROBE_BUF[:n])
        left -= n


def elastic_probe(out_dir: str, need_bytes: int, floor_bytes: int,
                  chunk_bytes: int = 1 << 20, max_bytes: int = None) -> dict:
    """弹性存储探测：只在「用 statvfs 算出来的可用空间不够」时调用。

    目的：有些存储报出来的 free 是假的——云盘薄 provisioning（写多少后台扩多少）、
    配额、overlay 上层、以及把 tmpfs 当磁盘用的容器。真写一遍才知道能不能写。

    **安全边界（这个函数存在的全部意义就在这里）**：

    - 写到剩余空间只剩 ``floor_bytes``（默认 512KB）**立即停手**，不是填满；
    - 探针文件无论成功、失败、异常，都在 ``finally`` 里删除；
    - ``max_bytes`` 是硬上限，防止 ``need_bytes`` 算错时写出几十 GB；
    - 每片写完 fsync，否则内容停在页缓存里，测出来的「写成功」是假的。

    **为什么按「剩余额度」切片，而不是按固定块长猛写**：

    写满磁盘只需要一次判断失误。所以每一片的大小都现算::

        片上界 = 当前可用 - 停手线 - 安全余量

    再和「还差多少到目标」「单片上限」取最小值。这样即使某一刻可用空间
    被别的进程吃掉，这一片也只会写到停手线之上，**永远不越线**。
    固定 1MB 块长 + 512KB 停手线的写法会冲过线（写完才发现已经低于地板），
    那正是「代码错一点就把盘填满」的那一点。

    缓冲区固定 64KB 复用，不按 ``chunk_bytes`` 分配——否则有人把
    ``probe_chunk_kb`` 写成 4096，会先吃 4GB 内存再谈探测。
    """
    floor_bytes = max(0, int(floor_bytes))
    # 单片上限硬钳在 8MB：探测要的是「能不能写」，不是吞吐。
    chunk = min(max(4096, int(chunk_bytes)), 8 * 1024 * 1024)
    need = max(0, int(need_bytes))
    target = need
    capped = False
    if max_bytes is not None and target > int(max_bytes):
        # 需求超过探测上限：只探到上限就停，但**不能**据此判定「空间够」——
        # 写满 4GB 不代表还写得下第 5GB。宁可判不确定，也不要往快满的盘上
        # 丢一个几百 MB 的快照。
        target = max(0, int(max_bytes))
        capped = True

    free_before = free_bytes(out_dir)
    probe = os.path.join(
        out_dir, '.zcbot-disk-probe-%d.tmp' % int(time.time() * 1000))
    written = 0
    hit_floor = False
    report = {'ok': False, 'kind': 'insufficient', 'written': 0,
              'free_before': free_before, 'free_after': free_before,
              'target': target, 'note': ''}

    # 可用空间本来就低于停手线：一片都不写，直接判不足。
    if free_before >= 0 and free_before <= floor_bytes:
        report['note'] = ('起始可用空间 %s 已低于停手线 %s，不做写入测试，直接判定不足'
                          % (_mb(free_before), _mb(floor_bytes)))
        return report

    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(probe, 'wb') as f:
            while written < target:
                free_now = free_bytes(out_dir)
                if free_now < 0:
                    # 读不到可用空间就不敢再写：宁可判「无法确认」
                    report['kind'] = 'unknown'
                    report['note'] = '无法读取可用空间，停止探测以避免写满磁盘'
                    break
                budget = free_now - floor_bytes - _PROBE_MARGIN
                if budget <= 0:
                    hit_floor = True
                    report['note'] = (
                        '可用空间已接近停手线（剩余 %s，停手线 %s），停止探测并清理'
                        % (_mb(free_now), _mb(floor_bytes)))
                    break
                size = min(chunk, target - written, budget)
                _write_slices(f, size)
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except OSError as _exc:  # 某些网络盘不支持 fsync
                    logger.debug('[elastic_probe] fsync 不可用: %s', _exc)
                written += size
                report['free_after'] = free_bytes(out_dir)
        report['written'] = written
        if report['kind'] == 'unknown' and not written:
            report['ok'] = False
        elif hit_floor:
            # 顺序要紧：最后一片可能正好写满目标，同时可用空间也触到地板。
            # 此时「写满了」不代表「还有空间」——必须按触底处理，否则会把
            # 一个快满的盘判成弹性存储，接着就把快照丢上去把盘写爆。
            report['kind'] = 'insufficient'
            report['ok'] = False
            report['note'] = report['note'] or (
                '实写 %s 后触底，判定空间确实不足' % _mb(written))
        elif report['kind'] == 'unknown':
            report['ok'] = False
        elif written >= target and capped:
            report['ok'] = False
            report['kind'] = 'capped'
            report['note'] = ('需求 %s 超过探测上限 %s，只确认了 %s 可写，'
                              '不足以判定能否完成本次备份'
                              % (_mb(need), _mb(target), _mb(written)))
        elif written >= target:
            report['ok'] = True
            report['kind'] = 'elastic'
            report['note'] = ('实写 %s 成功（statvfs 起始可用 %s）→ 判定为弹性存储'
                              % (_mb(written), _mb(free_before) if free_before >= 0 else '未知'))
        else:
            report['kind'] = 'insufficient'
            report['note'] = report['note'] or (
                '实写 %s 后触底，判定空间确实不足' % _mb(written))
    except OSError as e:
        report['written'] = written
        report['kind'] = 'insufficient'
        report['note'] = '写入失败（%s: %s），实写 %s' % (
            type(e).__name__, e, _mb(written))
    except Exception as e:  # 兜底：探测本身不能把备份流程带崩
        report['written'] = written
        report['kind'] = 'error'
        report['note'] = '探测异常（%s: %s）' % (type(e).__name__, e)
        logger.debug('[elastic_probe] 异常: %s', e)
    finally:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except OSError as _exc:
            # 删不掉要吼一声：留着就是在用户磁盘上埋一个不知道多大的垃圾
            logger.warning('[elastic_probe] 探针文件删除失败，请手工清理: %s (%s)',
                           probe, _exc)
        report['free_after'] = free_bytes(out_dir)
    return report


# ── 快照 / 压缩 / 校验 ────────────────────────────────────────

def snapshot_to(database, db_path: str, target: str) -> str:
    """把库快照到 target（已存在的文件会被覆盖）。返回实际使用的方式。"""
    try:
        database.execute("VACUUM INTO %s", (target,))
        return 'vacuum'
    except Exception as e:
        logger.debug('[snapshot_to] VACUUM INTO 不可用，退化为文件复制: %s', e)
        shutil.copy2(db_path, target)
        return 'copy'


def compress_zip(src: str, dst: str, level: int = 1,
                 yield_every: int = 32) -> dict:
    """把 src 压成 zip。分块写 + 周期性让出，避免长时间占满 CPU。

    说明：用户的口径是「打包成 zip 就行，只要压缩时不卡别的事（事件处理等）」。
    所以这里不用 9 档死磕体积，默认 1 档；每 ``yield_every`` 块 sleep 1ms，
    让事件循环和其它线程拿到 GIL。1ms × (256MB/1MB/32) ≈ 8ms，代价可忽略。
    """
    chunk = 1 << 20
    written = 0
    started = time.time()
    try:
        with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED, compresslevel=level,
                             allowZip64=True) as zf:
            with open(src, 'rb') as fin, \
                    zf.open(os.path.basename(src), 'w', force_zip64=True) as fout:
                i = 0
                while True:
                    buf = fin.read(chunk)
                    if not buf:
                        break
                    fout.write(buf)
                    written += len(buf)
                    i += 1
                    if yield_every and i % yield_every == 0:
                        time.sleep(0.001)
        return {'ok': True, 'path': dst, 'bytes_in': written,
                'bytes_out': os.path.getsize(dst),
                'elapsed': time.time() - started, 'note': ''}
    except Exception as e:
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except OSError as _exc:
            logger.debug('[compress_zip] 清理失败产物失败: %s', _exc)
        return {'ok': False, 'path': dst, 'bytes_in': written, 'bytes_out': 0,
                'elapsed': time.time() - started,
                'note': '压缩失败（%s: %s），保留未压缩文件' % (type(e).__name__, e)}


def verify_backup(path: str, expect_bytes: int = None,
                  sqlite_check: bool = True) -> dict:
    """读回复核。返回 ``{'ok', 'kind', 'note'}``。

    - zip：`testzip()` 会把每个条目重新解压算 CRC，等于真的读了一遍；
    - db：先比字节数，再用 sqlite 的 ``quick_check`` 确认页结构没坏。
    """
    if not os.path.isfile(path):
        return {'ok': False, 'kind': 'missing', 'note': '备份文件不存在'}
    size = os.path.getsize(path)
    if expect_bytes is not None and size != expect_bytes:
        return {'ok': False, 'kind': 'size',
                'note': '体积不符（期望 %d，实际 %d）' % (expect_bytes, size)}
    if size == 0:
        return {'ok': False, 'kind': 'empty', 'note': '备份文件为空'}

    # 在 .part 上校验（转正之前），所以不能直接 endswith('.zip')：
    # 那样会一路落到「只比体积」的分支，CRC/sqlite 校验被静默跳过 ——
    # 等于「校验看起来跑过了，其实什么都没验」。
    logical = path[:-len(PART_SUFFIX)] if path.endswith(PART_SUFFIX) else path

    if logical.endswith('.zip'):
        try:
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
                if bad is not None:
                    return {'ok': False, 'kind': 'crc',
                            'note': 'zip 条目 CRC 校验失败: %s' % bad}
                names = zf.namelist()
            return {'ok': True, 'kind': 'zip-crc',
                    'note': 'zip CRC 校验通过（%d 个条目）' % len(names)}
        except Exception as e:
            return {'ok': False, 'kind': 'zip',
                    'note': 'zip 打不开（%s: %s）' % (type(e).__name__, e)}

    if sqlite_check and logical.endswith('.db'):
        try:
            import sqlite3
            conn = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
            try:
                row = conn.execute('PRAGMA quick_check(1)').fetchone()
            finally:
                conn.close()
            if row and str(row[0]).lower() != 'ok':
                return {'ok': False, 'kind': 'sqlite',
                        'note': 'sqlite quick_check 未通过: %s' % (row[0],)}
            return {'ok': True, 'kind': 'sqlite', 'note': 'sqlite quick_check 通过'}
        except Exception as e:
            # 校验做不了就明确说「没校验」，不假装通过
            return {'ok': True, 'kind': 'size-only',
                    'note': '无法做 sqlite 校验（%s），仅核对体积' % type(e).__name__}
    return {'ok': True, 'kind': 'size', 'note': '体积核对通过'}


# ── 轮转 ──────────────────────────────────────────────────────

def list_backups(out_dir: str, prefix: str = BACKUP_PREFIX) -> list:
    """按修改时间倒序列出备份（新的在前）。"""
    try:
        names = [n for n in os.listdir(out_dir)
                 if n.startswith(prefix) and not n.endswith(PART_SUFFIX)]
    except OSError:
        return []
    items = []
    for n in names:
        p = os.path.join(out_dir, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        items.append({'name': n, 'path': p, 'size': st.st_size,
                      'mtime': st.st_mtime})
    items.sort(key=lambda x: x['mtime'], reverse=True)
    return items


def prune(out_dir: str, keep: int, prefix: str = BACKUP_PREFIX) -> list:
    """只保留最近 keep 份，返回被删掉的文件名。"""
    if keep is None or keep < 0:
        return []
    removed = []
    for item in list_backups(out_dir, prefix)[int(keep):]:
        try:
            os.remove(item['path'])
            removed.append(item['name'])
        except OSError as e:
            logger.warning('备份轮转删除失败 %s: %s', item['path'], e)
    if removed:
        logger.info('备份轮转：保留 %d 份，清理 %d 份（%s）',
                    keep, len(removed), ', '.join(removed))
    return removed


def cleanup_parts(out_dir: str) -> None:
    """清掉上次中断留下的 .part。它们不是可用备份，留着只会占空间。"""
    try:
        names = [n for n in os.listdir(out_dir) if n.endswith(PART_SUFFIX)]
    except OSError:
        return
    for n in names:
        try:
            os.remove(os.path.join(out_dir, n))
        except OSError as _exc:
            logger.debug('[cleanup_parts] 删除残留失败 %s: %s', n, _exc)


def _unique_base(out_dir: str, base: str) -> str:
    """保证这一份的文件名没被占过。

    同一秒内连跑两次（手动点两下、定时与手动撞上、单元测试批量跑）时间戳会一样，
    后来者直接覆盖前者 —— 表现是「备份了，但翻来覆去只有一份」，轮转也就没意义了。
    """
    candidate, n = base, 1
    while (any(os.path.exists(os.path.join(out_dir, candidate + ext))
               for ext in ('.db', '.db' + PART_SUFFIX,
                           '.zip', '.zip' + PART_SUFFIX))):
        n += 1
        candidate = '%s-%d' % (base, n)
    return candidate


# ── 主流程 ────────────────────────────────────────────────────

def run_backup(database, cfg: dict = None, reason: str = 'auto',
               out_dir: str = None) -> dict:
    """跑一次备份，返回结构化报告（永远不抛异常）。

    ``status`` 取值：``ok`` / ``skipped`` / ``refused`` / ``failed``。
    """
    started = time.time()
    conf = load_config(cfg)
    report = {'ok': False, 'status': 'failed', 'reason': reason,
              'path': '', 'size': 0, 'compressed': False,
              'elapsed': 0.0, 'note': '', 'probe': None,
              'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}

    def _finish(status, ok, note, **extra):
        report.update({'status': status, 'ok': ok, 'note': note})
        report.update(extra)
        report['elapsed'] = round(time.time() - started, 3)
        if ok:
            logger.info('数据库自动备份完成（%s）：%s', reason, note)
        else:
            logger.warning('数据库自动备份未完成（%s）：%s', reason, note)
        return report

    if not conf['enabled']:
        return _finish('skipped', False, '自动备份已关闭（database.backup.enabled=false）')

    db_type = getattr(database, 'db_type', '')
    if db_type == 'mysql':
        # MySQL 不在这一层 dump：mysqldump 会长时间锁表，体积也不可控。
        return _finish('skipped', False,
                       'MySQL 不走文件快照（请用实例级备份 / binlog 恢复点）')

    db_path = resolve_db_path(database)
    if not db_path or not os.path.exists(db_path):
        return _finish('failed', False, '库文件不存在，无法备份: %s' % (db_path or '路径未知'))

    try:
        db_size = os.path.getsize(db_path)
    except OSError as e:
        return _finish('failed', False, '读取库体积失败: %s' % e)

    directory = out_dir or os.path.join(os.path.dirname(db_path), 'backups')
    try:
        os.makedirs(directory, exist_ok=True)
        cleanup_parts(directory)
    except OSError as e:
        return _finish('failed', False, '备份目录不可用: %s' % e)

    # ── 空间判定：不够就先探一探是不是弹性存储 ──
    need = int(db_size * conf['headroom_ratio'])
    avail = free_bytes(directory)
    floor = int(conf['min_free_after_mb'] * 1048576)
    if avail >= 0 and avail < need:
        logger.warning(
            '数据库自动备份：可用空间不足（%s < 需要 %s，库 %s），启动弹性探测',
            _mb(avail), _mb(need), _mb(db_size))
        probe = elastic_probe(
            directory, need_bytes=need, floor_bytes=floor,
            chunk_bytes=int(conf['probe_chunk_kb'] * 1024),
            max_bytes=int(conf['probe_max_mb'] * 1048576))
        report['probe'] = probe
        if not probe.get('ok'):
            return _finish(
                'failed', False,
                '可用空间不足且探测确认写不下（%s）。已清理探针文件，未产生备份。'
                % (probe.get('note') or '未知原因'))

    # ── 快照到 .part ──
    # 时间戳带毫秒：同一秒内连跑两次不该互相覆盖（见 _unique_base）。
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')[:-3]
    tag = ''.join(ch for ch in (reason or 'auto') if ch.isalnum() or ch in '-_')
    base = _unique_base(directory, '%s%s%s' % (
        BACKUP_PREFIX, stamp, ('-' + tag) if tag else ''))
    part = os.path.join(directory, base + '.db' + PART_SUFFIX)
    final = ''
    try:
        how = snapshot_to(database, db_path, part)
        size = os.path.getsize(part)
    except Exception as e:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError as _exc:
            logger.debug('[run_backup] 清理失败快照失败: %s', _exc)
        return _finish('failed', False, '快照失败（%s: %s）' % (type(e).__name__, e))

    # ── 体积上限：太大不备份，先问人 ──
    limit = int(conf['refuse_above_mb'] * 1048576)
    if limit and size > limit and not conf['allow_large']:
        try:
            os.remove(part)
        except OSError as _exc:
            logger.debug('[run_backup] 清理超限快照失败: %s', _exc)
        return _finish(
            'refused', False,
            '快照 %s 超过上限 %s，已暂停备份。确认库体积正常后可调大 '
            'database.backup.refuse_above_mb，或临时置 allow_large=true。'
            % (_mb(size), _mb(limit)), size=size)

    # ── 压缩（超过阈值才压） ──
    compressed = False
    if conf['compress'] and size > conf['compress_threshold_kb'] * 1024:
        zpath = os.path.join(directory, base + '.zip' + PART_SUFFIX)
        res = compress_zip(part, zpath, level=conf['compress_level'])
        if res.get('ok'):
            try:
                os.remove(part)
            except OSError as _exc:
                logger.debug('[run_backup] 删除中间快照失败: %s', _exc)
            part, size, compressed = zpath, os.path.getsize(zpath), True
            logger.info('备份已压缩：%s → %s（%.0f%%）',
                        _mb(res['bytes_in']), _mb(res['bytes_out']),
                        100.0 * res['bytes_out'] / max(1, res['bytes_in']))
        else:
            logger.warning('备份压缩未生效：%s', res.get('note'))

    # ── 读回复核，过了才改名转正 ──
    if conf['verify']:
        v = verify_backup(part, expect_bytes=size,
                          sqlite_check=conf['verify_sqlite'] and not compressed)
        if not v.get('ok'):
            try:
                os.remove(part)
            except OSError as _exc:
                logger.debug('[run_backup] 清理校验失败产物失败: %s', _exc)
            return _finish('failed', False,
                           '备份校验未通过（%s），已丢弃: %s'
                           % (v.get('kind'), v.get('note')), size=size)
        report['verify'] = v
    else:
        report['verify'] = {'ok': True, 'kind': 'disabled', 'note': '未启用校验'}

    final = os.path.join(directory, os.path.basename(part)[:-len(PART_SUFFIX)])
    try:
        os.replace(part, final)
    except OSError as e:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError as _exc:
            logger.debug('[run_backup] 清理转正失败产物失败: %s', _exc)
        return _finish('failed', False, '备份转正失败: %s' % e)

    removed = prune(directory, int(conf['keep']))
    note = '已备份 %s（快照方式 %s，%s%s）' % (
        os.path.basename(final), how, _mb(size), '，已压缩' if compressed else '')
    if removed:
        note += '；轮转清理 %d 份' % len(removed)
    return _finish('ok', True, note, path=final, size=size,
                   compressed=compressed, removed=removed)


# ── 定时服务 ──────────────────────────────────────────────────

class BackupService:
    """按 interval 跑备份的守护线程。

    为什么不用框架的 scheduler：备份可能跑几分钟（几百 MB 的库），塞进调度器
    会占住一个 worker、还要受 ``scheduler.task_timeout`` 约束——超时只是「不再
    等待」，线程仍在跑，于是下一轮又起一个，最后两个线程同时 VACUUM。
    独立线程 + 自己的 sleep 循环，语义最清楚。
    """

    def __init__(self, framework, cfg: dict = None, out_dir: str = None):
        self.fw = framework
        self._cfg = cfg if cfg is not None else (
            (getattr(framework, 'config', {}) or {}).get('database', {}) or {}
        ).get('backup', {})
        self._out_dir = out_dir
        self._conf = load_config(self._cfg)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._last = None
        self._runs = 0
        self._failures = 0

    # ---- 生命周期 ----
    def start(self):
        if not self._conf['enabled']:
            logger.debug('自动备份未启用，跳过定时线程')
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(
            target=self._loop, name='db-backup', daemon=True)
        self._thread.start()
        logger.info('数据库自动备份已启动：每 %s 小时一次，保留 %d 份',
                    self._conf['interval_hours'], self._conf['keep'])
        return True

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    def _loop(self):
        delay = self._conf['initial_delay_minutes'] * 60
        interval = max(60.0, self._conf['interval_hours'] * 3600)
        if delay > 0 and self._stop.wait(delay):
            return
        while not self._stop.is_set():
            self.run_once('auto')
            if self._stop.wait(interval):
                return

    # ---- 执行 ----
    def run_once(self, reason: str = 'manual') -> dict:
        with self._lock:  # 手动触发与定时触发撞上时排队，不并发 VACUUM
            self._runs += 1
            try:
                report = run_backup(self.fw.db, self._cfg, reason=reason,
                                    out_dir=self._out_dir)
            except Exception as e:  # 兜底：线程不能因为一次异常就死掉
                logger.error('数据库自动备份异常: %s', e, exc_info=True)
                report = {'ok': False, 'status': 'failed', 'reason': reason,
                          'note': '异常（%s: %s）' % (type(e).__name__, e),
                          'elapsed': 0.0, 'path': '', 'size': 0}
            if not report.get('ok'):
                self._failures += 1
            self._last = report
            self._save_state(report)
            return report

    # ---- 状态 ----
    def _state_path(self) -> str:
        directory = self._out_dir
        if not directory:
            try:
                db_path = resolve_db_path(getattr(self.fw, 'db', None))
                directory = os.path.join(os.path.dirname(db_path), 'backups')
            except Exception:
                return ''
        return os.path.join(directory, STATE_FILE) if directory else ''

    def _save_state(self, report: dict) -> None:
        """落一份 state.json：进程重启后 WebUI 仍能看到上次结果。"""
        path = self._state_path()
        if not path:
            return
        try:
            import json
            os.makedirs(os.path.dirname(path), exist_ok=True)
            payload = {'last': report, 'runs': self._runs,
                       'failures': self._failures,
                       'config': {k: v for k, v in self._conf.items()
                                  if not isinstance(v, float) or v != float('inf')}}
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.debug('[BackupService] 状态写入失败: %s', e)

    def status(self) -> dict:
        directory = self._out_dir
        if not directory:
            try:
                directory = os.path.join(
                    os.path.dirname(resolve_db_path(getattr(self.fw, 'db', None))),
                    'backups')
            except Exception:
                directory = ''
        items = list_backups(directory) if directory else []
        return {
            'enabled': self._conf['enabled'],
            'dir': directory,
            'interval_hours': self._conf['interval_hours'],
            'keep': self._conf['keep'],
            'running': bool(self._thread and self._thread.is_alive()),
            'runs': self._runs,
            'failures': self._failures,
            'last': self._last,
            'backups': items,
            'config': {k: v for k, v in self._conf.items()
                       if not isinstance(v, float) or v != float('inf')},
        }

"""媒体段的文件引用解析与落盘。

为什么单独一个模块
------------------
入站媒体在规范段里只是一个「引用」——URL、base64、`file_id` 或本地路径。
插件要真正处理它（读像素、转码、存档）必须先拿到字节，而各协议给的引用
形态不一样。每个插件自己写一套，就会重复踩同样的坑：

- base64 动辄几十 MB。先 `decode` 再判长度，内存峰值直接翻倍；
- 边下边存不设上限，一个异常响应就能把磁盘写满；
- 文件名来自消息发送方，`../../etc/passwd` 是普通输入而不是攻击尝试；
- 下载中断留下的半成品与成功文件长得一样，后续读取的人分不清。

所以统一在这里实现，顺序固定为 **预检 → 流式写入 → 复核**，不做
「先写了再说」。任何一步超限或失败都删掉半成品，磁盘上不会留下
一个看起来正常的残文件。

约定返回结构
------------
所有分支都返回同一形状的 dict，调用方不需要按引用种类分支::

    {'status': 'ok' | 'failed' | 'unsupported' | 'needs_adapter',
     'path': str, 'size': int, 'ref': str, 'msg': str}

`needs_adapter` 是留给上层处理器的信号：`file_id:` 这类引用只有接入端
能换出真实地址，本模块不猜也不联网试。
"""
import base64
import binascii
import logging
import os
import re
import shutil
import tempfile
import urllib.parse

logger = logging.getLogger('zcbot')

#: 单文件默认上限。媒体文件远超这个值通常说明发来的不是用户以为的东西。
DEFAULT_MAX_BYTES = 50 * 1024 * 1024

#: 流式读取的分片大小。固定 64KB，不复用调用方给的上限值——
#: 上限可能被配成几百 MB，按上限分配缓冲区等于先把内存吃掉。
_CHUNK = 64 * 1024

#: 文件名长度上限（含扩展名）。多数文件系统限 255 字节，中文名按 3 字节算。
_MAX_NAME_CHARS = 120

#: Windows 保留设备名。带扩展名同样非法（`CON.txt` 也是设备）。
_WIN_RESERVED = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL']
    + [f'COM{i}' for i in range(1, 10)]
    + [f'LPT{i}' for i in range(1, 10)]
)

#: 文件名里允许保留的字符之外一律替换。用白名单而不是黑名单，
#: 因为黑名单永远漏（各平台还会冒出新的保留字）。
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def default_media_dir() -> str:
    """默认落盘目录：`<状态目录>/media`。"""
    from framework.paths import state_dir
    return os.path.join(state_dir(), 'media')


def safe_filename(name, default: str = 'media.bin') -> str:
    """
    把任意来源的文件名收敛成一个安全的单层文件名。

    这不是"美化"，是防目录穿越的第一道闸：消息发送方能完全控制
    `data['name']`，`../../config.yaml` 是合法输入。切掉所有分隔符后
    结果只能落在目标目录内。
    """
    if not isinstance(name, str) or not name.strip():
        return default
    # 两种分隔符都要切：OneBot 在 Windows 上会给反斜杠路径
    raw = name.replace('\\', '/').split('/')[-1]
    # 先剥控制字符与 NUL，再做其它替换（NUL 会让 open() 直接抛 ValueError）
    raw = _UNSAFE_CHARS.sub('_', raw).strip()
    # 前后的点：`.` 与 `..` 是目录，Windows 还不允许文件名以点结尾
    raw = raw.strip('.')
    if not raw:
        return default
    stem, dot, ext = raw.rpartition('.')
    if not dot:
        stem, ext = raw, ''
    # 保留名判定只看主干，`CON.txt` 与 `CON` 一样是设备
    if stem.upper() in _WIN_RESERVED:
        stem = f'_{stem}'
    if len(stem) > _MAX_NAME_CHARS:
        stem = stem[:_MAX_NAME_CHARS]
    if len(ext) > 16:
        # 超长"扩展名"多半是伪装成文件名的路径残留，不保留
        return stem
    return f'{stem}.{ext}' if ext else stem


def segment_file_id(segment) -> str:
    """
    取段里的协议侧文件标识。

    `file_id` 是"文件先上传、消息只带标识"这类协议的写法（Telegram / QQ 官方）。
    QQ 官方的标识还多藏一层，在 `file_info.file_uuid` 里。取不到返回空串——
    上层据此判断"这个接入端换不出地址"，而不是拿空串去发请求。
    """
    if not isinstance(segment, dict):
        return ''
    data = segment.get('data')
    fields = data if isinstance(data, dict) else segment
    for key in ('file_id', 'file_uuid'):
        val = fields.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    info = fields.get('file_info')
    if isinstance(info, dict):
        for key in ('file_uuid', 'file_id', 'id', 'url'):
            val = info.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ''


def _name_from_ref(ref: str, segment=None) -> str:
    """从段字段或引用本身猜一个文件名，猜不到交给 safe_filename 兜底。"""
    if isinstance(segment, dict):
        data = segment.get('data')
        if isinstance(data, dict):
            for key in ('name', 'filename'):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return val
    # base64 载荷与 file_id 都不是文件名，拿它们当名字只会得到一串乱码
    if ref.startswith(('base64://', 'file_id:')):
        return 'media.bin'
    if ref.startswith(('http://', 'https://')):
        path = urllib.parse.urlsplit(ref).path
        if path:
            return urllib.parse.unquote(path.rsplit('/', 1)[-1])
        return 'media.bin'
    if ref.startswith('file://'):
        return urllib.parse.unquote(ref[7:].replace('\\', '/').rsplit('/', 1)[-1])
    if os.sep in ref or '/' in ref or '\\' in ref:
        return ref.replace('\\', '/').rsplit('/', 1)[-1]
    return 'media.bin'


def _unique_path(dest_dir: str, name: str) -> str:
    """同目录同名不覆盖：追加 `-1`、`-2`……"""
    candidate = os.path.join(dest_dir, name)
    if not os.path.exists(candidate):
        return candidate
    stem, dot, ext = name.rpartition('.')
    if not dot:
        stem, ext = name, ''
    for i in range(1, 1000):
        cand = os.path.join(dest_dir, f'{stem}-{i}.{ext}' if ext else f'{stem}-{i}')
        if not os.path.exists(cand):
            return cand
    raise OSError(f'目标目录内同名文件过多: {name}')


def _inside(dest_dir: str, path: str) -> bool:
    """最终路径必须留在 dest_dir 内（safe_filename 之外的冗余校验）。"""
    try:
        root = os.path.realpath(dest_dir)
        target = os.path.realpath(path)
        return os.path.commonpath([root, target]) == root
    except (ValueError, OSError):
        # 不同盘符时 commonpath 抛 ValueError，直接判定为越界
        return False


def _finish(tmp: str, dest_dir: str, name: str, ref: str) -> dict:
    """把临时文件转正。写入阶段的成败判断只在这里发生一次。"""
    final = _unique_path(dest_dir, name)
    if not _inside(dest_dir, final):
        os.unlink(tmp)
        return {'status': 'failed', 'path': '', 'size': 0, 'ref': ref,
                'msg': '文件名解析后越出目标目录，已拒绝'}
    os.replace(tmp, final)
    return {'status': 'ok', 'path': final, 'size': os.path.getsize(final),
            'ref': ref, 'msg': ''}


def _fail(msg: str, ref: str) -> dict:
    return {'status': 'failed', 'path': '', 'size': 0, 'ref': ref, 'msg': msg}


def _too_large(size: int, max_bytes: int, ref: str) -> dict:
    return {'status': 'failed', 'path': '', 'size': 0, 'ref': ref,
            'msg': f'媒体 {size} 字节超过上限 {max_bytes}，已拒绝下载'}


def _write_base64(payload: str, dest_dir: str, name: str, ref: str,
                  max_bytes: int) -> dict:
    """base64 引用：先按长度估算，超限不进解码器。"""
    if payload.startswith('data:'):
        # data URL 的头部不参与解码，逗号前全丢
        _, _, payload = payload.partition(',')
    payload = re.sub(r'\s+', '', payload)
    if not payload:
        return _fail('base64 引用为空', ref)
    # 估算值可能因填充位略大于真实长度，宁可多拦不可少拦
    estimate = len(payload) * 3 // 4
    if estimate > max_bytes:
        return _too_large(estimate, max_bytes, ref)
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as e:
        return _fail(f'base64 解码失败: {e}', ref)
    if len(raw) > max_bytes:
        return _too_large(len(raw), max_bytes, ref)
    tmp = _new_tmp(dest_dir)
    try:
        with open(tmp, 'wb') as f:
            f.write(raw)
    except Exception:
        _discard(tmp)
        raise
    return _finish(tmp, dest_dir, name, ref)


def _new_tmp(dest_dir: str) -> str:
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix='.dl-', suffix='.part')
    os.close(fd)
    return tmp


class _OverLimit(Exception):
    """写入过程中越过上限。用异常而不是就地清理，是因为清理不能在文件
    句柄还开着的时候做——Windows 上 unlink 一个打开中的文件会直接失败，
    半成品就留在磁盘上了。"""

    def __init__(self, size):
        super().__init__(f'超过上限: {size}')
        self.size = size


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError as e:
        logger.warning('媒体半成品删除失败（需人工清理）: %s (%s)', path, e)


def _read_local(path: str, dest_dir: str, name: str, ref: str,
                max_bytes: int) -> dict:
    """本地路径引用：先看大小，再复制。不移动原文件。"""
    if not os.path.isfile(path):
        return _fail(f'本地文件不存在: {path}', ref)
    size = os.path.getsize(path)
    if size > max_bytes:
        return _too_large(size, max_bytes, ref)
    tmp = _new_tmp(dest_dir)
    try:
        shutil.copyfile(path, tmp)
    except Exception:
        _discard(tmp)
        raise
    return _finish(tmp, dest_dir, name, ref)


def _download_http(ref: str, dest_dir: str, name: str, max_bytes: int,
                   timeout: float, fetcher) -> dict:
    """HTTP 引用：Content-Length 预检 + 边读边累计，超限立刻停。"""
    import requests

    get = fetcher or requests.get
    try:
        resp = get(ref, stream=True, timeout=(5, timeout),
                   headers={'User-Agent': 'zcbot-media/1.0'})
    except Exception as e:
        return _fail(f'请求失败: {type(e).__name__}: {e}', ref)

    try:
        status = getattr(resp, 'status_code', 200)
        if not isinstance(status, int) or status < 200 or status >= 300:
            return _fail(f'HTTP 状态 {status}', ref)
        headers = getattr(resp, 'headers', None) or {}
        try:
            declared = int(headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > max_bytes:
            return _too_large(declared, max_bytes, ref)
        tmp = _new_tmp(dest_dir)
        written = 0
        try:
            with open(tmp, 'wb') as f:
                for chunk in resp.iter_content(_CHUNK):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        # 抛出而不是就地 unlink：此刻文件还开着，
                        # Windows 上删不掉，半成品会留在磁盘上
                        raise _OverLimit(written)
                    f.write(chunk)
        except _OverLimit as e:
            _discard(tmp)
            return _too_large(e.size, max_bytes, ref)
        except Exception:
            _discard(tmp)
            raise
        if written == 0:
            _discard(tmp)
            return _fail('响应体为空', ref)
        return _finish(tmp, dest_dir, name, ref)
    finally:
        close = getattr(resp, 'close', None)
        if callable(close):
            try:
                close()
            except Exception as e:
                logger.debug('响应关闭失败（不影响结果）: %s', e)


def download(ref_or_segment, dest_dir=None, filename=None,
             max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = 30,
             fetcher=None) -> dict:
    """
    把一个媒体段（或裸引用字符串）落盘。

    :param ref_or_segment: 规范段 dict，或直接的引用字符串
    :param dest_dir: 目标目录，默认 `<状态目录>/media`
    :param filename: 指定文件名（仍会过 safe_filename）
    :param max_bytes: 单文件上限
    :param timeout: 网络读取超时（秒）
    :param fetcher: 测试注入用，签名为 `requests.get(url, **kw)`
    """
    ref = ''
    result_base = {'status': 'failed', 'path': '', 'size': 0, 'ref': '', 'msg': ''}
    if isinstance(ref_or_segment, dict):
        # 不直接用 media_ref：它对 file_id 不加前缀（只有 base64 才加），
        # 于是 file_id 的裸值会被当成"本地路径"去找文件——找不到是小事，
        # 可怕的是碰巧同名文件存在，就把无关文件当成了这条消息的图片。
        data = ref_or_segment.get('data')
        fields = data if isinstance(data, dict) else ref_or_segment
        for key in ('file', 'url', 'path', 'base64'):
            val = fields.get(key)
            if isinstance(val, str) and val.strip():
                ref = f'base64://{val.strip()}' if key == 'base64' else val.strip()
                break
        if not ref and (fields.get('file_id') or fields.get('file_info')):
            ref = 'file_id:' + segment_file_id(ref_or_segment)
    elif isinstance(ref_or_segment, str):
        ref = ref_or_segment.strip()
    elif ref_or_segment is not None:
        # int / list / bytes 之类：str() 出来的东西会被当成本地路径去 open()，
        # 结果是"文件不存在"这种误导性的错误。直接拒绝更诚实。
        result_base['msg'] = (f'引用类型不支持（{type(ref_or_segment).__name__}），'
                              '只接受规范段或字符串引用')
        return result_base
    result_base['ref'] = ref

    if not ref:
        result_base['msg'] = '段里没有可取的文件引用（url / file / base64 / path / file_id 都为空）'
        return result_base
    if max_bytes <= 0:
        result_base['msg'] = f'max_bytes 必须为正数，当前为 {max_bytes}'
        return result_base
    if ref.startswith('file_id:'):
        # 只有接入端知道 file_id 对应哪个文件，本模块不猜
        result_base['status'] = 'needs_adapter'
        result_base['msg'] = 'file_id 需由接入端换取真实地址'
        return result_base

    try:
        dest = os.path.abspath(dest_dir or default_media_dir())
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        result_base['msg'] = f'目标目录不可用: {e}'
        return result_base

    name = safe_filename(filename or _name_from_ref(ref, ref_or_segment))

    if ref.startswith('base64://'):
        return _write_base64(ref[len('base64://'):], dest, name, ref, max_bytes)
    if ref.startswith(('http://', 'https://')):
        return _download_http(ref, dest, name, max_bytes, timeout, fetcher)
    if ref.startswith('file://'):
        local = urllib.parse.unquote(ref[7:])
        # Windows 上 file:///C:/x 的 path 部分是 /C:/x，去掉前导斜杠
        if re.match(r'^/[A-Za-z]:', local):
            local = local[1:]
        return _read_local(local, dest, name, ref, max_bytes)
    return _read_local(ref, dest, name, ref, max_bytes)


__all__ = [
    'DEFAULT_MAX_BYTES',
    'default_media_dir',
    'download',
    'safe_filename',
    'segment_file_id',
]

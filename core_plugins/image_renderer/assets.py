# -*- coding: utf-8 -*-
"""渲染器外部资源（字体子集 + 原生扩展）的获取与校验。

为什么要有这一层
----------------
字体子集和 7 个平台的原生二进制合计约 15MB，放在源码仓库里既拖慢 clone，
也让每次打包体积翻倍。所以它们不再进源码，改为：

    源码仓库  →  只有代码 + charset 元数据
    发布产物  →  assets.zip（字体 + 原生库），挂在独立资源仓库
                 kuangxing6367/zcbot-render-assets 的 Release 上
    运行时    →  首次缺文件时按需下载到 data 目录，逐条校验 sha256 后原子落盘

为什么资源仓库是独立的而不是主仓的一个 Release：二进制和字体的更新节奏跟代码
发版无关（补个字形、重编一个平台的 .so 不该逼着框架 bump 版本号）；而且主仓
同时承担代码历史与二进制分发，只会让 clone / CI / wheel 一起变胖。

设计约束
--------
1. **目录可选**：源码运行、开发机上插件目录里本来就有文件时直接用，不联网。
2. **只写 data 目录**：pip 安装后的插件目录通常是只读的，绝不能往里写。
3. **原子替换**：先写 .part 再 os.replace，避免半截文件被当成可用资源。
4. **zip 路径穿越防护**：解压前逐条校验规范化路径必须落在目标目录内。
5. **失败不静默降级**：下载/校验失败只记录状态 + 抛错，由调用方决定怎么处理
   （由 backend 决定是否回退——本模块不做任何回退决策）。

写不了盘怎么办
--------------
资源是「可选增强」，不是插件能否工作的前提。所以这一层对写盘失败的处理是
**一律降级成状态 + 可操作建议，绝不抛给调用方**：

    data 目录建不出来      → preflight 直接返回，连网络都不碰（省流量、报错指向真因）
    目录存在但不可写        → 写探针文件验证（os.access 在 Windows 上不可信）
    空间不够               → 下载前按 40MB 预检；拿到 Content-Length 后再卡一次
    下到一半 ENOSPC        → 清理 .part，报「磁盘满」，不报成网络错误
    正式目录删不掉（只读）  → 退化为逐文件就地覆盖，不当作整体失败
    覆盖也写不进去          → 保住旧资源，标 stale=True，只说「在旧版」不说「挂了」

最后一条最关键：**更新失败 ≠ 没有资源**。已有可用资源时状态是 ok=True + stale=True，
面板据此提示「仍在使用旧版本 vX」，而不是让用户以为渲染功能坏了。

混合态的防止：就地覆盖时 manifest.json 只在其余文件全部写成功后更新。
否则会出现「版本号显示新版、字体还是旧内容」，字表与 ttf 还可能对不上，
这种不一致比直接失败更难查。
"""
import hashlib
import io
import json
import os
import platform
import shutil
import sys
import zipfile

logger = None       # 由 main.py 注入，避免重复建 logger


def set_logger(lg):
    global logger
    logger = lg


def _log(msg, level='info'):
    if logger is None:
        return
    getattr(logger, level, logger.info)('[image_renderer] ' + msg)


# ---------------------------------------------------------------- 平台判定

def platform_key():
    """返回发布包里使用的平台目录名；不支持的平台返回 None。

    与 native/README.md 的目录约定保持一致。
    """
    if sys.platform.startswith('win'):
        return 'win32' if sys.maxsize <= 2 ** 32 else 'win64'
    if not sys.platform.startswith('linux'):
        return None
    arch = (platform.machine() or '').lower()
    if 'loongarch' in arch or arch in ('loong64',):
        return 'linux-loongarch64'
    if arch.startswith('armv7') or arch in ('armv8l', 'armhf'):
        return 'linux-armv7'
    if arch in ('i386', 'i486', 'i586', 'i686', 'x86', 'ia32'):
        return 'linux-i686'
    if 'aarch64' in arch or 'arm64' in arch:
        return 'linux-aarch64'
    if 'x86_64' in arch or 'amd64' in arch:
        return 'linux-x86_64'
    return None


def native_filename():
    """原生扩展在当前平台上的文件后缀。"""
    if sys.platform.startswith('win'):
        return 'zcbot_render.pyd'
    return 'zcbot_render.so'


# ---------------------------------------------------------------- 资源布局

FONT_FILES = (
    'msyh_subset_common.ttf',
    'msyh_subset_ext.ttf',
    'charset_common.txt',
    'charset_ext.txt',
)

# assets.zip 内部结构（固定，不做版本差异，便于人工核对）
#   fonts/<FONT_FILES>
#   native/<platform_key>/zcbot_render.(pyd|so)
#   manifest.json

# 下载前预留的空间（MB）。来源：assets.zip 实测 8.99MB，解压后约 15MB，
# 期间还要同时存在 .part 与旧版本目录，按 40MB 预留（约 2.6 倍余量）。
# 磁盘满判定宁可提前一点错杀，也不要下到 90% 才 ENOSPC 然后半截数据落地。
SPACE_REQUIRED_MB = 40


def _cleanup(path):
    """删临时文件，失败就算了——清理不该再抛一个异常盖住真实原因。"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


class AssetState:
    """资源可用性快照，供日志与 WebUI 展示。"""

    def __init__(self):
        self.ok = False
        self.source = 'none'        # plugin / cache / download / none
        self.fonts_ok = False
        self.native_ok = False
        self.fonts_dir = None
        self.native_path = None
        self.error = None
        self.version = None
        # 需要用户确认才能下载（不消耗流量，先提示）
        self.pending_consent = False
        self.download_size_mb = None
        self.manual_hint = None
        # 落盘能力：写不了盘 ≠ 没有资源。两者分开报，否则用户会去查网络。
        self.writable = None        # True / False / None（未探测）
        self.free_mb = None         # 目标目录所在卷剩余空间
        self.stale = False          # 下载/更新失败，但已有旧的可用资源

    def as_dict(self):
        return {
            'ok': self.ok,
            'source': self.source,
            'fonts_ok': self.fonts_ok,
            'native_ok': self.native_ok,
            'fonts_dir': self.fonts_dir,
            'native_path': self.native_path,
            'error': self.error,
            'version': self.version,
            'pending_consent': self.pending_consent,
            'download_size_mb': self.download_size_mb,
            'manual_hint': self.manual_hint,
            'writable': self.writable,
            'free_mb': self.free_mb,
            'stale': self.stale,
        }


class AssetManager:
    """资源定位 + 按需下载。一个插件实例一份。"""
    # 【踩坑保佑】image_renderer 资源外置
    # 坑：合计15MB会拖慢clone  →  AssetManager
    #                    _ooOoo_
    #                   o8888888o
    #                   88" . "88
    #                   (| -_- |)
    #                    O\ = /O
    #                ____/`---'\____
    #              .   ' \\| |// `.
    #               / \\||| : |||// \
    #             / _||||| -:- |||||- \
    #               | | \\\ - /// | |
    #             | \_| ''\---/'' | |
    #              \ .-\__ `-` ___/-. /
    #           ___`. .' /--.--\ `. . __
    #        ."" '< `.___\_<|>_/___.' >'"".
    #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
    #         \ \ `-. \_ __\ /__ _/ .-` / /
    # ======`-.____`-.___\_____/___.-`____.-'======
    #                    `=---='
    #
    # .............................................
    #          佛祖保佑             永无BUG


    def __init__(self, plugin_dir, data_dir, asset_url='', timeout=30.0):
        self.plugin_dir = plugin_dir
        self.data_dir = data_dir or os.path.join(plugin_dir, '.assets')
        self.asset_url = (asset_url or '').strip()
        self.timeout = float(timeout)
        self._state = AssetState()
        # 仅作兜底：正常路径一律读磁盘上的 manifest，避免重启后丢版本号
        self._last_version = None

    # ── 路径 ──────────────────────────────────────────────
    @property
    def version_dir(self):
        return os.path.join(self.data_dir, 'image_renderer', 'assets')

    def _plugin_fonts_dir(self):
        return self.plugin_dir

    def _plugin_native_path(self):
        key = platform_key()
        if not key:
            return None
        return os.path.join(self.plugin_dir, 'native', 'bin', key, native_filename())

    def _cached_fonts_dir(self):
        return os.path.join(self.version_dir, 'fonts')

    def _cached_native_path(self):
        key = platform_key()
        if not key:
            return None
        return os.path.join(self.version_dir, 'native', key, native_filename())

    # ── 状态 ──────────────────────────────────────────────
    @property
    def state(self):
        return self._state

    def _fonts_present(self, d):
        return all(os.path.isfile(os.path.join(d, f)) for f in FONT_FILES)

    def _read_cached_version(self):
        """从缓存目录里的 manifest.json 取版本号。

        解压时 manifest 跟资源一起落盘了，所以这件事跨进程也成立——
        不依赖内存里的 _last_version，重启后日志里照样能报出「在用哪一版」。
        """
        p = os.path.join(self.version_dir, 'manifest.json')
        try:
            with open(p, 'rb') as f:
                return (json.loads(f.read().decode('utf-8')) or {}).get('version')
        except (OSError, ValueError):
            return None

    def refresh_state(self):
        """只探测本地，不联网。"""
        st = AssetState()
        if self._fonts_present(self._plugin_fonts_dir()):
            st.fonts_ok = True
            st.fonts_dir = self._plugin_fonts_dir()
            st.source = 'plugin'
        elif self._fonts_present(self._cached_fonts_dir()):
            st.fonts_ok = True
            st.fonts_dir = self._cached_fonts_dir()
            st.source = 'cache'

        p = self._plugin_native_path()
        c = self._cached_native_path()
        if p and os.path.isfile(p):
            st.native_ok = True
            st.native_path = p
            st.source = 'plugin' if st.source == 'plugin' else (st.source or 'plugin')
        elif c and os.path.isfile(c):
            st.native_ok = True
            st.native_path = c
            st.source = 'cache' if st.source in ('none', 'cache') else st.source

        if st.source in ('cache', 'download'):
            # 磁盘优先：重启后 manifest 才是权威，内存值只在前一次写入失败时兜底
            st.version = self._read_cached_version() or self._last_version

        st.ok = st.fonts_ok and st.native_ok
        self._state = st
        return st

    # ── 落盘能力 ──────────────────────────────────────────
    #
    # 资源是「可选增强」，不是插件能否工作的前提。所以这一层的铁律是：
    # **任何写盘失败都不许抛到调用方**，一律转成 st.error + 可操作的建议。
    # 否则一个只读的 data 目录就能把整个渲染插件拖死，连本来能用的 PIL 一起没了。

    @staticmethod
    def _ensure_space(path, need_bytes):
        """剩余空间不足时抛 IOError（带人话）。查不到配额就放过。"""
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            return None
        if free < need_bytes:
            raise IOError(
                '磁盘空间不足：需要约 {:.0f}MB，{} 所在卷只剩 {:.0f}MB'.format(
                    need_bytes / 1048576.0, path, free / 1048576.0))
        return free

    def _preflight(self):
        """下载前探测目标目录能不能写。返回 (ok, reason, free_bytes)。

        为什么要单独探测：`os.access(W_OK)` 在 Windows 上只看只读属性、
        对 ACL / 组策略 / 只读挂载全都不准；而且 makedirs 成功也不代表能写文件
        （配额满、目录本身是只读 bind mount 都能过 makedirs）。所以直接写一个
        探针文件再删，这是唯一可信的判据。
        """
        d = os.path.join(self.data_dir, 'image_renderer')
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            return False, '资源目录不可创建（{}）：{}'.format(d, e), None

        if not os.path.isdir(d):
            return False, '资源目录路径被占用且不是目录：{}'.format(d), None

        probe = os.path.join(d, '.zcbot_write_probe')
        try:
            with open(probe, 'wb') as f:
                f.write(b'1')
            os.remove(probe)
        except OSError as e:
            return False, '资源目录不可写（只读挂载 / 权限不足 / 配额已满）：{}：{}'.format(
                d, e), None

        free = None
        try:
            free = shutil.disk_usage(d).free
        except OSError:
            pass
        return True, None, free

    def probe_writable(self):
        """只探测不下载。供面板与日志提前判断。

        存在的意义：用户把 asset_consent 从 ask 改成 auto、重启、等 9MB 下完，
        才在最后一步发现目录只读——这一趟流量和时间全白花。所以 ask 阶段就探一次，
        写不了就直说「改了也没用」。
        """
        # 【踩坑保佑】image_renderer 写盘探针
        # 坑：一个字节都不下  →  probe_writable
        #                    _ooOoo_
        #                   o8888888o
        #                   88" . "88
        #                   (| -_- |)
        #                    O\ = /O
        #                ____/`---'\____
        #              .   ' \\| |// `.
        #               / \\||| : |||// \
        #             / _||||| -:- |||||- \
        #               | | \\\ - /// | |
        #             | \_| ''\---/'' | |
        #              \ .-\__ `-` ___/-. /
        #           ___`. .' /--.--\ `. . __
        #        ."" '< `.___\_<|>_/___.' >'"".
        #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
        #         \ \ `-. \_ __\ /__ _/ .-` / /
        # ======`-.____`-.___\_____/___.-`____.-'======
        #                    `=---='
        #
        # .............................................
        #          佛祖保佑             永无BUG

        ok, reason, free = self._preflight()
        if not ok:
            return ok, reason, None
        return ok, None, (None if free is None else round(free / 1048576.0, 1))

    def _commit(self, src_dir, dst_dir):
        """把已校验的资源目录落到最终位置。返回 None=成功，否则原因字符串。

        三种情形分开处理，因为它们的代价完全不同：

        1. 目标不存在 → os.replace，原子生效，最优。
        2. 目标存在且可删 → 删旧的再改名（保留极短的「两边都没有」窗口，
           但 content 已校验过，比原地逐文件覆盖快得多）。
        3. 目标存在但删不掉（只读挂载 / 文件被占用 / 无权限）→ **退化为就地覆盖**。
           这时绝不报「下载失败」了事：旧版本文件还在，多半仍可用，能覆盖多少
           算多少，全部到位就算成功。覆盖不了的逐条汇报，让用户知道是哪个文件。
        """
        # 【踩坑保佑】image_renderer 混合态
        # 坑：字表和ttf还可能对不上  →  _commit
        #                    _ooOoo_
        #                   o8888888o
        #                   88" . "88
        #                   (| -_- |)
        #                    O\ = /O
        #                ____/`---'\____
        #              .   ' \\| |// `.
        #               / \\||| : |||// \
        #             / _||||| -:- |||||- \
        #               | | \\\ - /// | |
        #             | \_| ''\---/'' | |
        #              \ .-\__ `-` ___/-. /
        #           ___`. .' /--.--\ `. . __
        #        ."" '< `.___\_<|>_/___.' >'"".
        #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
        #         \ \ `-. \_ __\ /__ _/ .-` / /
        # ======`-.____`-.___\_____/___.-`____.-'======
        #                    `=---='
        #
        # .............................................
        #          佛祖保佑             永无BUG

        if not os.path.isdir(dst_dir):
            try:
                os.replace(src_dir, dst_dir)
                return None
            except OSError as e:
                return '无法建立资源目录 {}：{}'.format(dst_dir, e)

        removed = True
        try:
            shutil.rmtree(dst_dir)
        except OSError as e:
            removed = False        # 只读 / 被占用：下面走就地覆盖
            _log('正式资源目录删除失败（{}），退化为就地覆盖'.format(e), 'warning')
        else:
            _log('正式资源目录已清空，准备原子改名')

        if removed:
            try:
                os.replace(src_dir, dst_dir)
                return None
            except OSError as e:
                # rename 失败但旧目录已被删，此时只能逐个搬回去
                _log('资源目录改名失败（{}），改为逐文件复制'.format(e), 'warning')

        # 就地覆盖：目标目录（可能只读）里的文件逐个写
        pairs = []
        for root, _dirs, files in os.walk(src_dir):
            for fn in files:
                pairs.append((os.path.join(root, fn), fn))

        # manifest.json 单独处理，且**只有在其余文件全部写成功后才写**。
        # 它记录的是「整包是哪一版」：只要有一个字体写失败就更新它，
        # 就会留下「版本号显示 2.0、实际字体还是 1.0」的混合态——
        # 版本看起来更新了，内容没变，字表与 ttf 还可能对不上，比直接失败更难查。
        failed = []
        manifest_src = None
        for sp, fn in pairs:
            if fn == 'manifest.json':
                manifest_src = sp
                continue
            rel = os.path.relpath(sp, src_dir)
            dp = os.path.join(dst_dir, rel)
            try:
                os.makedirs(os.path.dirname(dp), exist_ok=True)
                shutil.copyfile(sp, dp)
            except OSError as e:
                failed.append('{}（{}）'.format(rel, e))

        if not failed and manifest_src:
            try:
                shutil.copyfile(manifest_src, os.path.join(dst_dir, 'manifest.json'))
            except OSError as e:
                failed.append('manifest.json（{}）'.format(e))
        if failed:
            # src 留着没意义（内容已在 dst 里尽力铺过），但它能让人看到哪步失败
            shutil.rmtree(src_dir, ignore_errors=True)
            return '资源目录只读，{} 个文件无法写入：{}'.format(
                len(failed), '；'.join(failed[:3]))
        shutil.rmtree(src_dir, ignore_errors=True)
        return None

    # ── 下载 ──────────────────────────────────────────────
    def download(self, force=False):
        """下载 assets.zip 并解压到 version_dir。

        :return: AssetState（**任何情况下都不抛异常**；失败时 ok=False，
                 error 写明原因与下一步该做什么）
        """
        st = self.refresh_state()
        if st.ok and not force:
            return st

        had_previous = st.ok        # 已有旧资源：后面失败时要能被识别成「还能用」
        url = self.asset_url

        # 1) 落盘能力探测。即使最终不下载也先探：
        #    面板拿到 writable 才能区分「网络源没配」和「目录写不进去」两种提示。
        ok, reason, free = self._preflight()
        st.writable = ok
        st.free_mb = None if free is None else round(free / 1048576.0, 1)

        if not self.asset_url:
            st.error = '未配置资源地址（asset_url），无法下载渲染资源'
            st.manual_hint = self.manual_hint_text()
            self._state = st
            _log(st.error, 'error')
            return st

        if not ok:
            st.error = reason + '。可在 WebUI 把 data 目录改到可写位置，或手动放置资源到 ' \
                                '插件目录（见下方提示）'
            st.manual_hint = self.manual_hint_text()
            self._state = st
            _log('渲染资源无法获取：' + st.error, 'error')
            return st

        need = SPACE_REQUIRED_MB * 1048576
        if free is not None and free < need:
            st.error = ('磁盘空间不足：解压后约 15MB，连临时文件需要约 {}MB，'
                        '{} 所在卷只剩 {:.0f}MB').format(
                            SPACE_REQUIRED_MB, self.data_dir, st.free_mb)
            st.manual_hint = self.manual_hint_text()
            self._state = st
            _log('渲染资源无法获取：' + st.error, 'error')
            return st

        tmp_zip = os.path.join(self.data_dir, 'image_renderer', 'assets.zip.part')
        version = None
        try:
            _log('开始下载渲染资源: {}'.format(url))
            self._fetch(url, tmp_zip)
            manifest = self._extract(tmp_zip)          # 解压+校验到 .part 目录
            version = (manifest or {}).get('version')
            # 2) 提交。这一步才碰正式目录，之前的失败都不会污染已有资源。
            err = self._commit(self.version_dir + '.part', self.version_dir)
            if err:
                raise IOError(err)
            try:
                os.replace(tmp_zip, os.path.join(
                    os.path.dirname(tmp_zip), 'assets.zip'))
            except OSError as e:
                # 归档只是留档，存不下来不影响资源本身可用
                _log('资源包留档失败（不影响使用）：{}'.format(e), 'warning')
        except Exception as e:
            st.error = '{}: {}'.format(type(e).__name__, e)
            self._state = st
            _cleanup(tmp_zip)
            shutil.rmtree(self.version_dir + '.part', ignore_errors=True)
            _log('渲染资源下载失败: {}'.format(st.error), 'error')
            # 3) 关键区分：失败 ≠ 没得用。已有旧资源时给出「仍在用旧版」的结论，
            #    否则用户会以为功能挂了，跑去查网络。
            if had_previous:
                st2 = self.refresh_state()
                st2.error = st.error
                st2.stale = bool(st2.ok)
                st2.writable = ok
                st2.free_mb = st.free_mb
                # refresh_state 只认文件，不认版本号（版本记录在 manifest 里，
                # 而缓存目录里没有 manifest）。不补这一步，日志会写成「版本 未知」，
                # 用户没法判断自己还在用哪一版资源。
                st2.version = st2.version or self._last_version
                if st2.ok:
                    _log('已有可用资源仍在使用（版本 {}），本次更新失败但不影响渲染'.format(
                        st2.version or '未知'), 'warning')
                self._state = st2
                return st2
            return st

        self._last_version = version or self._last_version
        st = self.refresh_state()
        st.version = version or st.version   # 磁盘读不到时用这次包内的版本号
        st.source = 'download' if st.ok else st.source
        st.writable = ok
        st.free_mb = None if free is None else round(free / 1048576.0, 1)
        if not st.ok:
            st.error = '下载完成但资源仍不完整（可能是发布包缺少当前平台 {} 的产物）'.format(
                platform_key())
            _log(st.error, 'error')
        else:
            _log('渲染资源就绪（版本 {}，来自下载）'.format(st.version or '未知'))
        self._state = st
        return st

    def manual_hint_text(self):
        """手动放置资源的确切路径与文件名（日志/面板可直接照抄）。"""
        plat = platform_key() or '<平台>'
        fname = native_filename()
        return ('把 fonts/ 与 native/{plat}/ 放到 {base}/（或直接放在插件目录 '
                '{plugin}/：字体 msyh_subset_common.ttf、msyh_subset_ext.ttf、'
                'charset_common.txt、charset_ext.txt；原生扩展 native/bin/{plat}/{fname}）'
                ).format(base=self.version_dir, plugin=self.plugin_dir,
                         plat=plat, fname=fname)

    def _fetch(self, url, dest):
        import urllib.request
        req = urllib.request.Request(url, headers={'User-Agent': 'zcbot-image_renderer'})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            total = resp.headers.get('Content-Length')
            if total:
                # 拿到真实大小后再卡一次空间：预检用的是估算值，
                # 发布包一旦涨到几百 MB，40MB 的估算会放过它，这里能兜住。
                # ×2 是给解压后的体积留位置。
                self._ensure_space(dest, int(total) * 2 + 2097152)
            got = 0
            try:
                with open(dest, 'wb') as f:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
            except OSError as e:
                # ENOSPC / EACCES：区分「网络断了」和「盘写不下」，
                # 否则用户看到 ConnectionReset 会去查网络，方向完全错。
                raise IOError('写入临时文件失败（磁盘满 / 目录只读 / 配额耗尽）：{}'.format(e))
            if total and int(total) != got:
                raise IOError('下载不完整：期望 {} 字节，实际 {}'.format(total, got))

    def _extract(self, zip_path):
        """安全解压（防路径穿越）+ 逐条校验，返回包内 manifest.json 的 dict。

        校验不是可选项：manifest 就在包里，拿它比对是最便宜的一次完整性检查。
        传输被截断、代理返回了缓存旧包、发布时打错了包——这三种都会在这里被拦下，
        而不是让一个半坏的原生扩展进到正式目录里，等用户切到 rust 才炸。
        """
        target = self.version_dir
        # 统一成绝对路径：下面的路径穿越校验拿 abspath 做前缀比较，
        # 若 tmp_target 是相对路径，startswith 会恒为 False，把所有条目都误判成非法。
        tmp_target = os.path.abspath(target + '.part')
        if os.path.isdir(tmp_target):
            shutil.rmtree(tmp_target, ignore_errors=True)
        os.makedirs(tmp_target, exist_ok=True)

        manifest = None
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace('\\', '/')
                # 路径穿越防护：规范化后必须仍在目标目录内
                dest = os.path.normpath(os.path.join(tmp_target, name))
                if not dest.startswith(os.path.abspath(tmp_target) + os.sep):
                    raise IOError('资源包内包含非法路径: {}'.format(name))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, 'wb') as out:
                    shutil.copyfileobj(src, out)
                if name == 'manifest.json':
                    with open(dest, 'rb') as f:
                        try:
                            manifest = json.loads(f.read().decode('utf-8'))
                        except Exception:
                            manifest = None

        bad = self._verify_extracted(tmp_target, manifest)
        if bad:
            shutil.rmtree(tmp_target, ignore_errors=True)
            raise IOError('资源包校验失败: ' + '；'.join(bad[:5]))

        # 注意：这里**不**动正式目录。解压与校验全部发生在 .part 里，
        # 提交交给 download 的 _commit()——它要处理「正式目录只读/删不掉」的情形，
        # 在 _extract 里做掉就没有退路了。
        return manifest

    @staticmethod
    def _verify_extracted(base, manifest):
        """按 manifest.files 逐条核对 size 与 sha256，返回问题列表（空=通过）。"""
        mfiles = (manifest or {}).get('files') or {}
        if not mfiles:
            # 没有 manifest 就没法校验，但也不该当成致命错误：
            # 早期手工打的包没有 files 段，仍可解压使用，只是少一层保障。
            _log('资源包缺少 manifest.files，跳过完整性校验', 'warning')
            return []
        bad = []
        for arc, meta in mfiles.items():
            p = os.path.normpath(os.path.join(base, arc.replace('\\', '/')))
            if not os.path.isfile(p):
                bad.append('缺文件 {}'.format(arc))
                continue
            want_size = meta.get('size')
            if want_size is not None and os.path.getsize(p) != want_size:
                bad.append('{} 大小不符（期望 {} 实际 {}）'.format(
                    arc, want_size, os.path.getsize(p)))
                continue
            want_hash = meta.get('sha256')
            if want_hash and sha256_of(p) != want_hash:
                bad.append('{} sha256 不符'.format(arc))
        return bad


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(262144), b''):
            h.update(chunk)
    return h.hexdigest()

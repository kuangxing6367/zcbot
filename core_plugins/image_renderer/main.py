"""
图片渲染器插件 - 通用图片渲染引擎
提供可复用的图片绘制工具，供其他插件调用生成图片消息。
同时提供 /render_card 命令用于测试渲染效果。

渲染引擎：
  1. 原生扩展 zcbot_render（Rust + pyo3，放在 native/bin/<平台>/）
     平台目录：win64 / win32 / linux-x86_64 / linux-aarch64 /
              linux-i686(x32) / linux-armv7 / linux-loongarch64(龙芯)
     实测（win64 / Py3.13，300 次渲染）：常驻内存增量约 +20.6MB，
     低于 PIL 的 +25.2MB（约省 18%），且 300 次内无增长。
  2. 默认后端是 PIL（Pillow）。原生扩展需要用户在配置里显式切到 rust，
     不能默认启用：它要额外下载约 9MB 资源，且绘制比 PIL 慢（见 README）。

命令：
  /render_card [标题] [内容]  生成一张信息卡片图片
  /render_text [文字]         将文字渲染为图片

依赖：
  Pillow>=10.0.0（PIL 回退用）
"""
import importlib.util
import io
import logging
import os
import platform
import sys
import tempfile
from datetime import datetime

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "图片渲染器",
    "version": "1.1.0",
    "author": "ZGRIC",
    "desc": "通用图片渲染引擎（原生 Rust 扩展，自动回退 PIL），提供卡片/文本绘制工具",
    "priority": 200,
}

_FONT_DIR = os.path.dirname(os.path.abspath(__file__))
# 字体分层与完整字体候选的路径**不在这里写死**：字体可能来自插件目录，也可能是
# 首次启用时下载到 data 目录的 assets（见 assets.py）。实际路径由
# _font_tiers() / _FULL_FONT_CANDIDATES 在运行时按 _FONTS_DIR 解析。
# 完整字体候选：插件目录 / help 插件自带字体
_FULL_FONT_CANDIDATES = [
    os.path.join(_FONT_DIR, 'HarmonyOS_Sans_SC_Medium.ttf'),
    os.path.join(_FONT_DIR, 'HarmonyOS_Sans_SC_Regular.ttf'),
    os.path.join(_FONT_DIR, 'NotoSansCJK-Regular.ttc'),
    os.path.join(os.path.dirname(_FONT_DIR), 'help', 'DouyinSansBold.otf'),
]
# 子集字体字符集懒加载缓存（仅在选择层级时读取一次）
_FONT_CHARSET_CACHE = {}

# 模块级缓存，避免重复加载字体（LRU 上限，防无限增长导致内存泄漏）
_FONT_CACHE = {}
_FONT_CACHE_MAX = 16  # 最多缓存 16 个不同字号的字体对象（8MB/个，上限约 128MB）

def _font_cache_put(k, v):
    """写入字体缓存，超限时淘汰最旧条目"""
    # 【踩坑保佑】v1.0.1 图片渲染字体缓存
    # 坑：改LRU上限，修复无限增长的内存泄漏  →  _font_cache_put
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

    _FONT_CACHE[k] = v
    if len(_FONT_CACHE) > _FONT_CACHE_MAX:
        oldest = next(iter(_FONT_CACHE))
        del _FONT_CACHE[oldest]


def _linux_native_subdirs(arch: str) -> list:
    """按 platform.machine() 排出优先尝试的平台目录（精确架构在前）"""
    ordered = []
    # 龙芯 LoongArch（platform.machine() = loongarch64 / loong64）
    if 'loongarch' in arch or arch in ('loong64',):
        ordered.append('linux-loongarch64')
    # ARMv7 32 位（armv7l / armv8l 向下兼容，armhf 为软硬浮点别名）
    if arch.startswith('armv7') or arch in ('armv8l', 'armhf'):
        ordered.append('linux-armv7')
    # 32 位 x86（x32）
    if arch in ('i386', 'i486', 'i586', 'i686', 'x86', 'ia32'):
        ordered.append('linux-i686')
    # 64 位 ARM
    if 'aarch64' in arch or 'arm64' in arch:
        ordered.append('linux-aarch64')
    # 64 位 x86
    if 'x86_64' in arch or 'amd64' in arch:
        ordered.append('linux-x86_64')
    # 兜底：列出全部已支持目录（跨架构加载只在成功后缓存，失败仅记 DEBUG）
    for sub in ('linux-x86_64', 'linux64', 'linux-aarch64', 'linux-armv7',
                'linux-i686', 'linux-loongarch64'):
        if sub not in ordered:
            ordered.append(sub)
    return ordered


def _load_native_renderer(extra_candidates=None):
    """按平台加载原生渲染扩展；找不到返回 None。

    候选顺序：显式传入的路径（下载到 data 目录的产物）→ 插件目录 native/bin/。
    """
    candidates = list(extra_candidates or [])
    native_dir = os.path.join(_FONT_DIR, 'native', 'bin')
    if sys.platform.startswith('win'):
        # 32 位 Python（sys.maxsize 仅 2**31-1）走 win32，64 位走 win64
        subdirs = ['win32', 'win64'] if sys.maxsize <= 2 ** 32 else ['win64', 'win32', 'win-amd64']
        names = ['zcbot_render.pyd', 'zcbot_render.abi3.pyd']
    elif sys.platform.startswith('linux'):
        subdirs = _linux_native_subdirs((platform.machine() or '').lower())
        names = ['zcbot_render.so', 'zcbot_render.abi3.so']
    else:
        subdirs = []
        names = []
    for sub in subdirs:
        for name in names:
            candidates.append(os.path.join(native_dir, sub, name))

    tried = []
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            # PyO3 扩展每进程仅可初始化一次：先在 sys.modules 占位，
            # 自愈/热重载二次调用直接复用首载实例（重跑 exec_module 必炸）
            if 'zcbot_render' in sys.modules:
                return sys.modules['zcbot_render']
            spec = importlib.util.spec_from_file_location('zcbot_render', path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules['zcbot_render'] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop('zcbot_render', None)   # 允许尝试下一个候选
                raise
            logger.info(f"[image_renderer] 原生渲染扩展已加载: {path}")
            return mod
        except Exception as e:
            # 架构不匹配等单次尝试失败属正常，仅记录 DEBUG，避免误导性 WARNING
            tried.append(f"{path} ({e})")
            logger.debug(f"[image_renderer] 原生扩展尝试失败: {path} - {e}")
    logger.warning(f"[image_renderer] 原生扩展加载失败: {'; '.join(tried) if tried else '未找到任何候选扩展文件'}")
    return None


# ---------------------------------------------------------------- 后端选择
#
# 默认 PIL。用户在配置面板（core_plugins.yaml 的 image_renderer.backend）改成
# rust 才启用原生扩展。原生扩展与字体子集体积大，不进源码仓库，首次使用时按
# asset_url 下载到 data 目录（见 assets.py）。
#
# 关键语义：backend=rust 但资源不可用时**不回退 PIL**，而是明确报错。
# 理由：这是用户在面板里的显式选择，静默降级会让人以为"选了 rust 但其实是 PIL
# 在跑"，性能与内存表现都对不上，问题会藏到最后才暴露。

_CTX = None
_ASSETS = None
_NATIVE = None
_FONTS_DIR = _FONT_DIR
_BACKEND = 'pil'
_BACKEND_ERROR = None
_BACKEND_READY = False

_BACKEND_VALUES = ('pil', 'rust')
_CONSENT = 'ask'   # ask / auto / manual


def _assets_module():
    """按文件路径加载同目录的 assets.py。

    插件以文件形式被加载，同目录模块不一定在 sys.path 上，所以不能直接 import。
    """
    path = os.path.join(_FONT_DIR, 'assets.py')
    spec = importlib.util.spec_from_file_location('zcbot_image_renderer_assets', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.set_logger(logger)
    return mod


def _read_cfg(key, default=None):
    """读本插件配置项。

    配置写在 core_plugins.yaml 的 image_renderer 块，启动时由
    framework.config.load_config() 合并进主配置的同名段，这里读合并后的结果。

    **不能用 ctx.get_config()**：那个接口查的是 plugin_configs 表，只服务
    userspace 插件（它们的值由 WebUI 写表）。核心插件从未往表里写过任何行，
    用它只会永远拿到 default——配置写了等于没写，且不会有任何报错。
    """
    try:
        from framework.config import get_config
        sec = (get_config() or {}).get('image_renderer')
        if isinstance(sec, dict) and key in sec:
            v = sec[key]
            return default if v is None else v
    except Exception:
        pass
    return default


# 资源提示文案：必须把「干什么用」和「做不到什么」一起说清。
# 只报体积和路径，用户没法判断值不值得花这 9MB / 值不值得切后端。
_ASSET_PURPOSE = (
    'Pillow 之外的第二种渲染实现（Rust：fontdue 光栅化 + image 编码），'
    '对外是同一套 Canvas API。适用场景是【没装 Pillow、或该平台没有 Pillow wheel '
    '只能源码编译】的环境——这类环境下它是唯一可用的渲染后端。'
)

_ASSET_LIMITS = (
    '  · 绘制比 PIL 慢：同样 3 行中文文本，纯渲染 3.1ms vs PIL 1.3ms（约 2.4 倍）；\n'
    '    卡片端到端 8.7ms vs 6.9ms（约慢 25%）。PNG 编码更快，所以纯文本场景基本打平。\n'
    '  · 只有 12 个绘图原语：rect/line/circle/ellipse/gradient_rect/text/text_metrics/\n'
    '    paste/blur/alpha_overlay/to_png/get_size；没有 resize/crop/rotate/arc/polygon/save。\n'
    '  · 不减少依赖：Pillow 仍在 requirements 里（它是默认后端）。\n'
    '  · 生僻字要落到系统字体（如 msyh.ttc）才能渲染，子集字体覆盖不到。'
)


def _log_asset_notice(st, consent):
    """缺资源时的提示：目的 + 局限 + 怎么获取，一次说全。"""
    url = _read_cfg('asset_url', '') or '（未配置 asset_url）'
    plat = _assets_module().platform_key() or 'unsupported'
    head = ('[image_renderer] 渲染资源缺失（字体子集 + 原生扩展，下载包约 9MB，'
            '当前平台 {}）。').format(plat)

    # 落盘能力优先报：这类失败跟网络无关，混在「要不要下载」的文案里
    # 只会让用户跑去查网络、换镜像。
    if st.writable is False:
        logger.error(
            "%s\n  但是：**目标目录写不进去**，改 asset_consent 也没用。\n"
            "  原因：%s\n"
            "  可选的出路：\n"
            "    · 把 data 目录指向可写位置（容器 / systemd 下常见为挂载点只读）\n"
            "    · 手动放置：%s\n"
            "    · 直接用 PIL 后端：不需要这个包，缺字集时回退系统字体",
            head, st.error or '目录不可写', st.manual_hint or _ASSETS.version_dir)
        return

    if consent == 'auto':
        return      # auto 会直接下载，无需提示
    if consent == 'ask':
        logger.warning(
            "%s当前 asset_consent=ask，**不会自动下载**（避免消耗你的流量）。\n"
            "\n  这个后端是干什么的：%s\n"
            "\n  它做不到什么（实测，win64 / Python 3.13）：\n%s\n"
            "\n  想自动获取：把 asset_consent 改成 auto 后重启（来源 %s）\n"
            "  想手动放置：%s\n"
            "  想继续用 PIL：把 backend 改回 pil（默认，无需任何下载）",
            head, _ASSET_PURPOSE, _ASSET_LIMITS, url,
            st.manual_hint or _ASSETS.version_dir)
    else:       # manual
        logger.info(
            "%s当前 asset_consent=manual，跳过下载。\n"
            "  用途：%s\n"
            "  局限：见 README《适用与局限》一节（绘制比 PIL 慢约 2.4 倍、只有 12 个绘图原语）。\n"
            "  手动放置：%s",
            head, _ASSET_PURPOSE, st.manual_hint or _ASSETS.version_dir)


def _manual_placement_hint(assets_mgr):
    """手动放置资源时需要的确切目录与文件名（写进日志/报错，可直接照做）。"""
    # 文案统一由 assets 层给：它才知道自己实际用的 version_dir 与平台键，
    # main 里再拼一份只会两边慢慢漂移。
    try:
        return assets_mgr.manual_hint_text()
    except Exception:
        pass
    base = getattr(assets_mgr, 'version_dir', '<data>/image_renderer/assets')
    return '把 fonts/ 与 native/<平台>/ 放到 {base}/ 下'.format(base=base)


def _resolve_backend(force_download=False):
    """决定后端并准备资源。幂等，可重复调用。"""
    global _ASSETS, _NATIVE, _FONTS_DIR, _BACKEND, _BACKEND_ERROR, _BACKEND_READY, _CONSENT

    raw = str(_read_cfg('backend', 'pil') or 'pil').strip().lower()
    if raw not in _BACKEND_VALUES:
        logger.warning(
            f"[image_renderer] backend 配置值 {raw!r} 非法（可选 {'/'.join(_BACKEND_VALUES)}），"
            f"按 pil 处理")
        raw = 'pil'
    _BACKEND = raw

    # 资源管理（PIL 也要字体，所以一律初始化）
    if _ASSETS is None:
        try:
            data_dir = _CTX.get_data_dir() if _CTX is not None else None
        except Exception as e:
            data_dir = None
            logger.warning(
                f"[image_renderer] 获取 data 目录失败（{type(e).__name__}: {e}），"
                f"资源将尝试落在插件目录下（该目录通常只读，失败是预期结果）")
        if not data_dir:
            logger.warning(
                "[image_renderer] 未拿到可写的 data 目录，渲染资源只能手动放置；"
                "PIL 后端不受影响（缺字体字集会回退系统字体）")
        _ASSETS = _assets_module().AssetManager(
            _FONT_DIR, data_dir,
            asset_url=_read_cfg('asset_url', ''),
            timeout=_read_cfg('asset_timeout', 30))

    st = _ASSETS.refresh_state()

    # 资源获取策略。默认 ask：**不下载**，只把「需要多少流量 / 放哪里」讲清楚，
    # 由用户改 asset_consent 决定。切到 rust 要拉约 9MB，静默下载在按流量计费或
    # 内网环境会被当成事故。
    consent = str(_read_cfg('asset_consent', 'ask') or 'ask').strip().lower()
    if consent not in ('ask', 'auto', 'manual'):
        logger.warning(
            f"[image_renderer] asset_consent 配置值 {consent!r} 非法"
            f"（可选 ask/auto/manual），按 ask 处理（不会自动下载）")
        consent = 'ask'
    _CONSENT = consent

    if not st.ok:
        if consent == 'auto':
            try:
                st = _ASSETS.download(force=force_download)
            except Exception as e:
                # download() 自己承诺不抛异常，这里是纵深防御：资源层的任何意外
                # 都不该让插件加载失败——PIL 后端本来能跑，凭什么被一次下载拖死。
                logger.error(
                    f"[image_renderer] 资源获取出现未预期异常（已忽略，继续使用可用后端）: "
                    f"{type(e).__name__}: {e}")
                st.error = f'{type(e).__name__}: {e}'
            if st.writable is False:
                # 写不了盘：这跟「要不要下载」是两个问题，别让用户去查网络
                logger.error(
                    "[image_renderer] 渲染资源无法落盘：%s\n"
                    "  这不是网络问题，是目标目录不可写。三条出路：\n"
                    "    · 把 data 目录指到可写位置（容器里常见：挂载点只读）\n"
                    "    · 手动放置：%s\n"
                    "    · 只用 PIL 后端（不需要这个包）",
                    st.error, st.manual_hint or _manual_placement_hint(_ASSETS))
            elif st.stale:
                logger.warning(
                    "[image_renderer] 渲染资源更新失败，但**已有可用资源仍在使用**"
                    "（版本 %s）。本次失败原因：%s",
                    st.version or '未知', st.error)
        else:
            # 不下载：把待确认信息挂到状态上，供日志与面板读取
            st.pending_consent = (consent == 'ask')
            st.manual_hint = _manual_placement_hint(_ASSETS)
            st.download_size_mb = 9       # assets.zip 实测约 8.99MB
            # ask 阶段也探一次落盘能力（不发请求，只写一个探针文件）：
            # 免得用户改了 auto、等 9MB 下完、最后才发现目录只读。
            try:
                w_ok, w_reason, w_free = _ASSETS.probe_writable()
                st.writable, st.free_mb = w_ok, w_free
                if not w_ok:
                    st.error = w_reason
            except Exception as e:
                logger.debug(f"[image_renderer] 落盘探测失败（不影响后续）：{e}")
            _log_asset_notice(st, consent)

    if st.fonts_ok:
        _FONTS_DIR = st.fonts_dir

    if _BACKEND == 'rust':
        if not st.native_ok:
            _NATIVE = None
            if st.pending_consent:
                _BACKEND_ERROR = (
                    '原生渲染资源尚未获取（下载包约 9MB，当前 asset_consent=ask 不自动下载）。\n'
                    '  想自动获取：把 asset_consent 改成 auto 后重启\n'
                    '  想手动放置：{}').format(st.manual_hint or _ASSETS.version_dir)
            else:
                _BACKEND_ERROR = st.error or (
                    '原生渲染资源不可用（缺当前平台 {} 的产物），且未回退 PIL。\n'
                    '  手动放置：{}').format(
                        _assets_module().platform_key() or 'unsupported',
                        st.manual_hint or _ASSETS.version_dir)
            logger.error(
                "[image_renderer] backend=rust 但原生扩展不可用：%s。"
                "请在 WebUI 配置页把 backend 改回 pil，或按上面提示补齐资源。",
                _BACKEND_ERROR)
        else:
            _NATIVE = _load_native_renderer([st.native_path])
            if _NATIVE is None:
                _BACKEND_ERROR = '原生扩展文件存在但加载失败（见上方日志）'
                logger.error("[image_renderer] backend=rust 但原生扩展加载失败，未回退 PIL")
            else:
                _BACKEND_ERROR = None
    else:
        # PIL 后端：不使用原生扩展（即使文件存在）
        _NATIVE = None
        _BACKEND_ERROR = None

    _BACKEND_READY = True
    return _BACKEND


def _require_backend():
    """渲染入口统一前置检查。

    backend=rust 且资源不可用时抛错——不回退 PIL（见上面的设计说明）。
    """
    if not _BACKEND_READY:
        _resolve_backend()
    if _BACKEND == 'rust' and _NATIVE is None:
        raise RuntimeError(
            '图片渲染后端为 rust，但原生扩展不可用：{}。'
            '可在 WebUI 配置页将 image_renderer.backend 改回 pil。'.format(
                _BACKEND_ERROR or '原因未知'))


def backend_status():
    """供 /api 与日志查看的当前状态（不触发下载）。"""
    if _ASSETS is not None:
        st = _ASSETS.state.as_dict()
    else:
        # 字段必须与 AssetState.as_dict() 完全一致：面板按固定键取值，
        # 这里少一个键就是前端一次 TypeError。
        st = {'ok': False, 'source': 'none', 'fonts_ok': False,
              'native_ok': False, 'fonts_dir': None, 'native_path': None,
              'error': None, 'version': None, 'pending_consent': False,
              'download_size_mb': None, 'manual_hint': None,
              'writable': None, 'free_mb': None, 'stale': False}
    return {
        'backend': _BACKEND,
        'asset_consent': _CONSENT,
        'native_loaded': _NATIVE is not None,
        'error': _BACKEND_ERROR,
        'assets': st,
    }


def _font_tiers():
    """按当前字体目录生成子集层级（字体可能来自插件目录，也可能是下载的 assets）。"""
    return [
        ('common',
         os.path.join(_FONTS_DIR, 'msyh_subset_common.ttf'),
         os.path.join(_FONTS_DIR, 'charset_common.txt')),
        ('ext',
         os.path.join(_FONTS_DIR, 'msyh_subset_ext.ttf'),
         os.path.join(_FONTS_DIR, 'charset_ext.txt')),
    ]


def _iter_text_parts(*texts):
    """将任意嵌套文本/列表参数扁平化为字符串序列"""
    for t in texts:
        if isinstance(t, (list, tuple)):
            for it in t:
                if isinstance(it, (list, tuple)):
                    for s in it:
                        if isinstance(s, str):
                            yield s
                elif isinstance(it, str):
                    yield it
        elif isinstance(t, str):
            yield t


def _font_tier_charset(charset_path):
    """懒加载子集字体字符集；缺失或读取失败返回 None（不选用该层级）"""
    if charset_path in _FONT_CHARSET_CACHE:
        return _FONT_CHARSET_CACHE[charset_path]
    cs = None
    try:
        if os.path.isfile(charset_path):
            with io.open(charset_path, encoding='utf-8') as f:
                cs = set(f.read())
    except Exception:
        cs = None
    _FONT_CHARSET_CACHE[charset_path] = cs
    return cs


def _find_font_path(*texts):
    """按渲染文本字符集选择最小够用的字体层级（常见/更多/完全）

    先取最小子集层级，其字符集能覆盖全部文本则命中；否则逐级上升；
    子集均无法覆盖时回退完整字体（插件自带 → 系统字体兜底）。
    避免每次渲染都加载完整中文字体（fontdue 全量解析约 +170MB）。
    """
    need = set()
    for part in _iter_text_parts(*texts):
        need.update(part)
    # 常见/更多 子集层级（按需加载字符集判断覆盖）
    for _name, fp, cs_path in _font_tiers():
        if not os.path.isfile(fp):
            continue
        cs = _font_tier_charset(cs_path)
        if cs is None:
            continue  # 无字符集元数据，无法证明覆盖，保守跳过
        if need <= cs:
            return fp
    # 完整字体候选
    for p in _FULL_FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    # 系统字体回退（Windows / Linux），保证原生渲染可用
    if sys.platform.startswith('win'):
        windir = os.environ.get('WINDIR', r'C:\Windows')
        sys_fonts = [
            os.path.join(windir, 'Fonts', 'msyh.ttc'),
            os.path.join(windir, 'Fonts', 'msyhbd.ttc'),
            os.path.join(windir, 'Fonts', 'simhei.ttf'),
            os.path.join(windir, 'Fonts', 'simsun.ttc'),
            os.path.join(windir, 'Fonts', 'arial.ttf'),
        ]
    else:
        sys_fonts = [
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/arphic/uming.ttc',
        ]
    for p in sys_fonts:
        if os.path.isfile(p):
            return p
    return None


def _get_font(size, bold=False):
    """加载 PIL 字体（带缓存）。

    候选顺序：字体选择器（子集分层 → 插件完整字体 → 系统字体）优先，再退到插件目录
    里的具名字体。

    历史 bug：这里只试 4 个固定文件名（HarmonyOS/DouyinSansBold/NotoSansCJK），
    而它们**一个都不存在**，于是直接落到 `ImageFont.load_default()` —— 那是 Pillow
    内置的拉丁字体，所有汉字都被渲染成同一个豆腐块（□）。实测「中/文/测/试」四个字
    取到完全相同的 5×7 位图，整张卡片只有阿拉伯数字正常。
    改用 _find_font_path() 之后，PIL 回退与原生后端走同一套字体判定，不会再分叉。
    """
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    from PIL import ImageFont

    candidates = []
    picked = _find_font_path()
    if picked:
        candidates.append(picked)
    for fn in ("HarmonyOS_Sans_SC_Medium.ttf", "HarmonyOS_Sans_SC_Regular.ttf",
               "DouyinSansBold.otf", "NotoSansCJK-Regular.ttc"):
        candidates.append(os.path.join(_FONT_DIR, fn))

    for fp in candidates:
        if not fp or not os.path.isfile(fp):
            continue
        try:
            f = ImageFont.truetype(fp, size)
            _font_cache_put(key, f)
            return f
        except Exception:
            continue

    # 一个中文字体都没找到：明确告警（否则整站卡片静默变豆腐块，很难被发现）
    logger.error(
        "未找到任何可用的中文字体，PIL 回退将把汉字渲染成方框。"
        "请安装中文字体（如 Noto Sans CJK / 微软雅黑）或补充插件内置字体。")
    try:
        try:
            f = ImageFont.load_default(size=size)   # Pillow>=10.1
        except TypeError:
            f = ImageFont.load_default()
        _font_cache_put(key, f)
        return f
    except Exception:
        return None


def _status_view(params=None):
    """GET /api/plugins/image_renderer/status

    供 WebUI 打开时取一次状态，决定是否显示「资源缺失」横幅。
    params 有默认值：Flask 以无参视图调用，双进程宿主的远程派发则传 dict，
    两种调用方式都要能跑。
    """
    payload = {'code': 0, 'data': backend_status()}
    try:
        from flask import jsonify
        return jsonify(payload)
    except Exception:
        # 没有 Flask 时（纯 IPC 宿主）退回纯 dict，由调用方序列化
        return payload


def _init_context(ctx):
    """绑定上下文并解析后端（register 时调用一次）。"""
    global _CTX
    _CTX = ctx
    try:
        _resolve_backend()
    except Exception as e:
        logger.error(f"[image_renderer] 后端初始化失败: {e}")
    st = backend_status()
    if not st['assets']['fonts_ok']:
        logger.error(
            "[image_renderer] 未找到中文字体（插件目录与下载缓存都没有），"
            "PIL 渲染会退化为系统字体；若系统也无中文字体则汉字显示为方框。")
    if st['backend'] == 'rust' and not st['native_loaded']:
        logger.error(
            "[image_renderer] backend=rust 但原生扩展未加载（%s）。"
            "渲染会直接报错，不会回退 PIL。", st['error'])


def register(ctx):
    _init_context(ctx)
    # 状态查询：WebUI 打开时拉一次，资源缺失才显示横幅。
    # 只读、不触发下载——否则「打开管理后台」就等于偷偷花用户的流量。
    try:
        ctx.register_api('/api/plugins/image_renderer/status', _status_view,
                         methods=['GET'], description='图片渲染后端与资源状态')
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[image_renderer] 状态接口注册失败（不影响渲染）: {e}")
    ctx.command(
        "/render_card",
        handle_render_card,
        priority=200,
        description="生成信息卡片图片，用法: /render_card 标题 | 内容",
    )
    ctx.command(
        "/render_text",
        handle_render_text,
        priority=200,
        description="将文字渲染为图片，用法: /render_text 要显示的文字",
    )


def handle_render_card(event, match):
    """生成信息卡片"""
    text = (match.group(1) or event.message or "").strip()
    if not text:
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message="请提供内容，如: /render_card 标题 | 内容",
        )
        return
    parts = [p.strip() for p in text.split("|", 1)]
    title = parts[0] if len(parts) > 0 else "信息卡片"
    content = parts[1] if len(parts) > 1 else title

    result = _render_card_image(title, content)
    _send_image(ctx, event, result)


def handle_render_text(event, match):
    """将文字渲染为图片"""
    text = (match.group(1) or event.message or "").strip()
    if not text:
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message="请提供文字，如: /render_text 你好世界",
        )
        return
    result = _render_text_image(text)
    _send_image(ctx, event, result)


# ---------------------------------------------------------------- 渲染（原生优先，PIL 回退）

def _parse_color(c, default=None):
    """颜色归一化：'#RRGGBB' / '#RRGGBBAA' / [r,g,b] / [r,g,b,a] → RGBA 元组"""
    if c is None:
        return default
    if isinstance(c, str):
        h = c.lstrip('#')
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
        if len(h) == 8:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16))
        raise ValueError(f"颜色格式非法: {c}")
    seq = tuple(int(x) for x in c)
    if len(seq) == 3:
        return seq + (255,)
    if len(seq) == 4:
        return seq
    raise ValueError("颜色序列长度必须为 3 或 4")


def _gradient_row(top, bottom, y, height):
    """渐变第 y 行的颜色（与原生版 lerp 一致）"""
    t = y / max(1, height - 1)
    return tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)


def _render_card_image(title, content, width=600, padding=30, options=None):
    """渲染信息卡片。原生可用返回 PNG bytes，否则返回 PIL Image"""
    _require_backend()
    if _NATIVE is not None:
        font = _find_font_path(title, content)
        if font:
            try:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                return _NATIVE.render_card(title, content, font, ts, width, padding, options)
            except Exception as e:
                logger.warning(f"[image_renderer] 原生 render_card 失败，回退 PIL: {e}")
    return _render_card_image_pil(title, content, width, padding, options)


def _render_text_image(text, width=500, padding=20, options=None):
    """将文字渲染为图片。原生可用返回 PNG bytes，否则返回 PIL Image"""
    _require_backend()
    if _NATIVE is not None:
        font = _find_font_path(text)
        if font:
            try:
                return _NATIVE.render_text(text, font, width, 24, padding, options)
            except Exception as e:
                logger.warning(f"[image_renderer] 原生 render_text 失败，回退 PIL: {e}")
    return _render_text_image_pil(text, width, padding, options)


def _render_list_image(title, items, width=600, padding=30, options=None):
    """渲染榜单/列表图片。原生可用返回 PNG bytes，否则返回 PIL Image"""
    _require_backend()
    if _NATIVE is not None:
        font = _find_font_path()
        if font:
            try:
                return _NATIVE.render_list(title, items, font, width, padding, options)
            except Exception as e:
                logger.warning(f"[image_renderer] 原生 render_list 失败，回退 PIL: {e}")
    return _render_list_image_pil(title, items, width, padding, options)


# ---------------------------------------------------------------- PIL 回退实现

def _render_card_image_pil(title, content, width=600, padding=30, options=None):
    """PIL 版信息卡片渲染（与原生版布局一致，支持相同 options）"""
    from PIL import Image, ImageDraw

    options = options or {}
    padding = int(options.get('padding', padding))
    title_size = int(options.get('title_size', 28))
    content_size = int(options.get('content_size', 20))
    footer_size = int(options.get('footer_size', 14))
    line_h = int(options.get('line_height', 0)) or 30
    align = options.get('align', 'left')
    radius = int(options.get('radius', 0))
    border_color = _parse_color(options.get('border_color'))
    border_width = int(options.get('border_width', 2))

    title_color = _parse_color(options.get('title_color'), (20, 30, 60, 255))
    content_color = _parse_color(options.get('content_color'), (60, 60, 80, 255))
    footer_color = _parse_color(options.get('footer_color'), (160, 160, 170, 255))
    accent_color = _parse_color(options.get('accent_color'), (99, 102, 241, 255))
    bg_color = _parse_color(options.get('bg_color'))
    bg_gradient = options.get('bg_gradient')
    show_footer = bool(options.get('show_footer', True))
    footer_text = options.get('footer_text', 'ZGRIC')

    title_font = _get_font(title_size, bold=True)
    content_font = _get_font(content_size)
    footer_font = _get_font(footer_size)

    line_height = line_h
    content_lines = []
    for line in content.split("\n"):
        if content_font:
            avg_char_w = content_font.getlength("中")
            chars_per_line = max(1, int((width - padding * 2) / avg_char_w))
            for i in range(0, len(line), chars_per_line):
                content_lines.append(line[i:i + chars_per_line])
        else:
            content_lines.append(line)

    title_h = 50
    content_h = len(content_lines) * line_height + 20
    footer_h = 30 if show_footer else 0
    total_h = padding * 2 + title_h + content_h + footer_h

    img = Image.new("RGBA", (width, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 背景：渐变 / 纯色 / 默认
    gradient = None
    if bg_gradient:
        top = _parse_color(bg_gradient[0], (248, 250, 255, 255))
        bottom = _parse_color(bg_gradient[1], (255, 255, 245, 255))
        gradient = (top, bottom)
    elif bg_color is None:
        gradient = ((248, 250, 255, 255), (255, 255, 245, 255))
    if gradient:
        top, bottom = gradient
        for y in range(total_h):
            draw.line([(0, y), (width, y)], fill=_gradient_row(top, bottom, y, total_h))
    else:
        if radius > 0:
            draw.rounded_rectangle([0, 0, width - 1, total_h - 1], radius=radius, fill=bg_color)
        else:
            draw.rectangle([0, 0, width - 1, total_h - 1], fill=bg_color)

    # 边框
    if border_color:
        box = [0, 0, width - 1, total_h - 1]
        if radius > 0:
            draw.rounded_rectangle(box, radius=radius, outline=border_color, width=border_width)
        else:
            draw.rectangle(box, outline=border_color, width=border_width)

    # 标题栏左侧彩色条
    draw.rectangle([padding, padding, padding + 6, padding + title_h], fill=accent_color)

    # 标题
    if title_font:
        draw.text((padding + 18, padding + 4), title, fill=title_color, font=title_font)

    # 内容（支持对齐）
    y_off = padding + title_h + 10
    content_x0 = padding + 6
    content_x1 = width - padding - 6
    inner_w = content_x1 - content_x0
    if content_font:
        for line in content_lines:
            lw = content_font.getlength(line)
            if align == 'center':
                x = content_x0 + max(0, inner_w - lw) / 2
            elif align == 'right':
                x = content_x1 - min(lw, inner_w)
            else:
                x = content_x0
            draw.text((x, y_off), line, fill=content_color, font=content_font)
            y_off += line_height

    # 页脚：左时间戳，右 footer_text
    if show_footer and footer_font:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        foot_y = total_h - padding - footer_h + 8
        draw.text((padding, foot_y), ts, fill=footer_color, font=footer_font)
        if footer_text:
            footer_text = str(footer_text)
            tw = footer_font.getlength(footer_text)
            draw.text((width - padding - tw, foot_y), footer_text, fill=footer_color, font=footer_font)

    # 圆角裁剪（渐变背景时角落透明）
    if radius > 0 and gradient:
        mask = Image.new("L", (width, total_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, width - 1, total_h - 1], radius=radius, fill=255
        )
        img.putalpha(mask)
    return img


def _render_text_image_pil(text, width=500, padding=20, options=None):
    """PIL 版文字渲染为图片（与原生版布局一致，支持相同 options）"""
    from PIL import Image, ImageDraw

    options = options or {}
    padding = int(options.get('padding', padding))
    font_size = int(options.get('font_size', 24))
    line_h = int(options.get('line_height', 0)) or max(1, round(font_size * 1.35))
    align = options.get('align', 'left')
    radius = int(options.get('radius', 0))
    border_color = _parse_color(options.get('border_color'))
    border_width = int(options.get('border_width', 2))
    text_color = _parse_color(options.get('text_color'), (40, 40, 60, 255))
    bg_color = _parse_color(options.get('bg_color'), (248, 250, 255, 255))
    bg_gradient = options.get('bg_gradient')

    font = _get_font(font_size)
    line_height = line_h

    lines = []
    for para in text.split("\n"):
        if font:
            avg_char_w = font.getlength("中")
            chars_per_line = max(1, int((width - padding * 2) / avg_char_w))
            for i in range(0, len(para), chars_per_line):
                lines.append(para[i:i + chars_per_line])
        else:
            lines.append(para)

    total_h = padding * 2 + len(lines) * line_height + 20
    img = Image.new("RGBA", (width, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 背景：渐变 / 纯色
    gradient = False
    if bg_gradient:
        top = _parse_color(bg_gradient[0], bg_color)
        bottom = _parse_color(bg_gradient[1], bg_color)
        for y in range(total_h):
            draw.line([(0, y), (width, y)], fill=_gradient_row(top, bottom, y, total_h))
        gradient = True
    else:
        if radius > 0:
            draw.rounded_rectangle([0, 0, width - 1, total_h - 1], radius=radius, fill=bg_color)
        else:
            draw.rectangle([0, 0, width - 1, total_h - 1], fill=bg_color)

    # 边框
    if border_color:
        box = [0, 0, width - 1, total_h - 1]
        if radius > 0:
            draw.rounded_rectangle(box, radius=radius, outline=border_color, width=border_width)
        else:
            draw.rectangle(box, outline=border_color, width=border_width)

    # 文字（支持对齐）
    y_off = padding
    inner_w = width - padding * 2
    if font:
        for line in lines:
            lw = font.getlength(line)
            if align == 'center':
                x = padding + max(0, inner_w - lw) / 2
            elif align == 'right':
                x = padding + inner_w - min(lw, inner_w)
            else:
                x = padding
            draw.text((x, y_off), line, fill=text_color, font=font)
            y_off += line_height

    # 圆角裁剪
    if radius > 0 and gradient:
        mask = Image.new("L", (width, total_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, width - 1, total_h - 1], radius=radius, fill=255
        )
        img.putalpha(mask)
    return img


def _render_list_image_pil(title, items, width=600, padding=30, options=None):
    """PIL 版榜单/列表渲染（与原生版布局一致，支持相同 options）"""
    from PIL import Image, ImageDraw

    options = options or {}
    padding = int(options.get('padding', padding))
    title_size = int(options.get('title_size', 24))
    item_size = int(options.get('item_size', 18))
    line_h = int(options.get('line_height', 0)) or max(1, round(item_size * 1.6))
    radius = int(options.get('radius', 0))
    border_color = _parse_color(options.get('border_color'))
    border_width = int(options.get('border_width', 2))

    title_color = _parse_color(options.get('title_color'), (20, 30, 60, 255))
    accent_color = _parse_color(options.get('accent_color'), (99, 102, 241, 255))
    name_color = _parse_color(options.get('name_color'), (40, 40, 60, 255))
    value_color = _parse_color(options.get('value_color'), (120, 120, 140, 255))
    highlight_bg = _parse_color(options.get('highlight_bg'), (236, 239, 255, 255))
    highlight_color = _parse_color(options.get('highlight_color'), (99, 102, 241, 255))
    rank_color = _parse_color(options.get('rank_color'), (160, 160, 170, 255))
    bg_color = _parse_color(options.get('bg_color'))
    bg_gradient = options.get('bg_gradient')

    title_font = _get_font(title_size, bold=True)
    item_font = _get_font(item_size)

    title_h = 56
    row_h = line_h + 4
    total_h = padding * 2 + title_h + len(items) * row_h + 10

    img = Image.new("RGBA", (width, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 背景：渐变 / 纯色 / 默认渐变
    gradient = False
    if bg_gradient:
        top = _parse_color(bg_gradient[0], (248, 250, 255, 255))
        bottom = _parse_color(bg_gradient[1], (255, 255, 245, 255))
        for y in range(total_h):
            draw.line([(0, y), (width, y)], fill=_gradient_row(top, bottom, y, total_h))
        gradient = True
    elif bg_color is None:
        top, bottom = (248, 250, 255, 255), (255, 255, 245, 255)
        for y in range(total_h):
            draw.line([(0, y), (width, y)], fill=_gradient_row(top, bottom, y, total_h))
        gradient = True
    else:
        if radius > 0:
            draw.rounded_rectangle([0, 0, width - 1, total_h - 1], radius=radius, fill=bg_color)
        else:
            draw.rectangle([0, 0, width - 1, total_h - 1], fill=bg_color)

    # 边框
    if border_color:
        box = [0, 0, width - 1, total_h - 1]
        if radius > 0:
            draw.rounded_rectangle(box, radius=radius, outline=border_color, width=border_width)
        else:
            draw.rectangle(box, outline=border_color, width=border_width)

    # 标题区：左侧彩色条 + 标题
    draw.rectangle([padding, padding, padding + 6, padding + title_h], fill=accent_color)
    if title_font:
        draw.text((padding + 18, padding + 4), title, fill=title_color, font=title_font)

    # 行内容
    x0, x1 = padding, width - padding
    rank_w = 44
    y_off = padding + title_h + 8
    if item_font:
        for item in items:
            if isinstance(item, str):
                row = {'name': item, 'value': '', 'rank': None, 'highlight': False}
            else:
                row = {
                    'name': str(item.get('name', '')),
                    'value': str(item.get('value', '')) if item.get('value') is not None else '',
                    'rank': str(item.get('rank')) if item.get('rank') is not None else None,
                    'highlight': bool(item.get('highlight', False)),
                }
            # 高亮整行背景
            if row['highlight']:
                draw.rectangle([x0, y_off, x1, y_off + row_h], fill=highlight_bg)

            baseline = y_off + item_size  # 与原生版 baseline 近似
            # 序号（右对齐到序号区）
            if row['rank']:
                draw.text((x0 + rank_w - item_font.getlength(row['rank']), y_off),
                          row['rank'], fill=rank_color, font=item_font)
            # 名称
            name_color_cur = highlight_color if row['highlight'] else name_color
            draw.text((x0 + rank_w, y_off), row['name'], fill=name_color_cur, font=item_font)
            # 数值（右对齐）
            if row['value']:
                vw = item_font.getlength(row['value'])
                draw.text((x1 - min(vw, x1 - x0 - rank_w), y_off),
                          row['value'], fill=value_color, font=item_font)

            y_off += row_h

    # 圆角裁剪
    if radius > 0 and gradient:
        mask = Image.new("L", (width, total_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, width - 1, total_h - 1], radius=radius, fill=255
        )
        img.putalpha(mask)
    return img


def _send_image(ctx, event, img_or_bytes):
    """发送图片（支持 PIL Image 或 PNG bytes），发送后自动清理"""
    img_path = None
    try:
        is_bytes = isinstance(img_or_bytes, (bytes, bytearray))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img_path = tmp.name
            if is_bytes:
                tmp.write(bytes(img_or_bytes))
            else:
                img_or_bytes.save(tmp.name, "PNG")

        # Windows 临时路径含反斜杠, 需转为正斜杠才能被 OneBot 客户端解析
        path_str = img_path.replace("\\", "/")
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id,
            message=f"[CQ:image,file=file:///{path_str}]",
        )
    except Exception as e:
        ctx.log(f"发送图片失败: {e}", level="error")
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message=f"图片生成失败: {e}",
        )
    finally:
        if img_path:
            try:
                os.unlink(img_path)
            except Exception:
                pass


# ---------------------------------------------------------------- Canvas（原生优先，PIL 回退）


class _NativeCanvasProxy:
    """
    原生 Canvas 的 Python 包装：text() 直接透传原生实现（顶部语义，与 PIL 版一致）

    原生 zcbot_render.Canvas.text(x, y, ...) 的 y 为行顶语义（内部 baseline = y + font_size），
    draw_text 已按 fontdue 0.9 的 ymin 语义修正（by = baseline + ymin - height + py），
    文本顶部与 _CanvasPIL 一致；中英文混排不再下沉错位。

    此前 text() 每次创建 PIL 临时画布渲染文本再 PNG 编码、经 paste 解码合成，
    单条文本就有「字体加载 + 画布分配 + PNG 编码 + PNG 解码」四重开销，
    是"渲染不如 PIL 快、内存占用高"的直接原因。现直连原生 draw_text，
    零中间对象、零编解码。其他方法（rect/circle/paste 等）原样透传。
    """

    def __init__(self, native_canvas, font_path):
        self._c = native_canvas
        self._font_path = font_path

    def text(self, x, y, text, font_size=20, color=None, align="left", wrap_width=0):
        """文字直连原生 draw_text（顶部语义，与 _CanvasPIL.text 一致）"""
        self._c.text(
            int(x), int(y), str(text),
            font_size=int(font_size),
            color=color,
            align=align if align else "left",
            wrap_width=int(wrap_width or 0),
        )
        return self

    def __getattr__(self, name):
        return getattr(self._c, name)


class _CanvasPIL:
    """PIL 版链式 Canvas，接口与原生 Canvas 完全一致（回退用）"""

    def __init__(self, width, height, bg_color=None, font_path=None):
        from PIL import Image, ImageDraw
        self._width = int(width)
        self._height = int(height)
        self._bg = _parse_color(bg_color, (0, 0, 0, 0))
        self._img = Image.new("RGBA", (self._width, self._height), self._bg)
        self._draw = ImageDraw.Draw(self._img)
        self._font_path = font_path
        self._font_cache = {}  # 按 (size, bold) 缓存字体对象，避免逐行/逐元素 truetype 重载

    def _font(self, size, bold=False):
        """加载字体（带实例级缓存）。

        Canvas 链式 API 每画一个文本/元素都会调本方法；若不缓存，多行渲染会逐行
        重新 truetype 读盘（CJK 字体数 MB~数十 MB），性能骤降——即此前「每一行字体
        都要重新加载」的根因。font_path 存在时优先按该路径加载并缓存；否则回退模块级
        _get_font（自身亦有缓存）。
        """
        if not self._font_path:
            raise ValueError("未设置字体，请在 Canvas.new 传入 font_path")
        key = (int(size), bool(bold))
        cached = self._font_cache.get(key)
        if cached is not None:
            return cached
        from PIL import ImageFont
        f = ImageFont.truetype(self._font_path, int(size)) if os.path.isfile(self._font_path) else _get_font(int(size), bold)
        if f is not None:
            self._font_cache[key] = f
        return f

    def get_size(self):
        return (self._width, self._height)

    def rect(self, x0, y0, x1, y1, radius=0, fill=None, outline=None, width=1):
        # 归一化坐标：允许 x1<x0 / y1<y0（倒置坐标）当作同一矩形，与原生渲染行为一致
        # （rect(300,200,0,0) == rect(0,0,300,200)）；避免 Pillow 对倒置坐标抛 ValueError。
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        box = [x0, y0, x1, y1]
        fill_c = _parse_color(fill)
        outline_c = _parse_color(outline)
        if radius > 0:
            self._draw.rounded_rectangle(box, radius=radius, fill=fill_c, outline=outline_c, width=int(width))
        else:
            self._draw.rectangle(box, fill=fill_c, outline=outline_c, width=int(width))
        return self

    def line(self, points, color, width=1):
        c = _parse_color(color)
        self._draw.line([(int(p[0]), int(p[1])) for p in points], fill=c, width=int(width))
        return self

    def circle(self, cx, cy, r, fill=None, outline=None, width=1):
        box = [cx - r, cy - r, cx + r, cy + r]
        self._draw.ellipse(box, fill=_parse_color(fill), outline=_parse_color(outline), width=int(width))
        return self

    def ellipse(self, x0, y0, x1, y1, fill=None, outline=None, width=1):
        self._draw.ellipse([x0, y0, x1, y1], fill=_parse_color(fill), outline=_parse_color(outline), width=int(width))
        return self

    def gradient_rect(self, x0, y0, x1, y1, color_a, color_b, direction="vertical"):
        a = _parse_color(color_a, (0, 0, 0, 255))
        b = _parse_color(color_b, (0, 0, 0, 255))
        w, h = x1 - x0, y1 - y0
        if direction == "horizontal":
            for x in range(max(0, x0), min(self._width, x1)):
                t = (x - x0) / max(1, w - 1)
                c = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,)
                self._draw.line([(x, y0), (x, y1)], fill=c)
        else:
            for y in range(max(0, y0), min(self._height, y1)):
                t = (y - y0) / max(1, h - 1)
                c = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,)
                self._draw.line([(x0, y), (x1, y)], fill=c)
        return self

    def text(self, x, y, text, font_size=20, color=None, align="left", wrap_width=0):
        font = self._font(int(font_size))
        c = _parse_color(color, (40, 40, 60, 255))
        lines = []
        if wrap_width > 0:
            from PIL import ImageFont
            avg = font.getlength("中")
            chars = max(1, int(wrap_width / max(1, avg)))
            for para in str(text).split("\n"):
                for i in range(0, len(para), chars):
                    lines.append(para[i:i + chars])
        else:
            lines = str(text).split("\n")
        line_h = max(1, round(int(font_size) * 1.35))
        yy = int(y)
        for line in lines:
            lw = font.getlength(line)
            if align == "center":
                xx = x + max(0, wrap_width - lw) / 2 if wrap_width > 0 else x
            elif align == "right":
                xx = x + (wrap_width - lw) if wrap_width > 0 else x
            else:
                xx = x
            self._draw.text((xx, yy), line, fill=c, font=font)
            yy += line_h
        return self

    def text_metrics(self, text, font_size, wrap_width=0):
        font = self._font(int(font_size))
        if wrap_width > 0:
            avg = font.getlength("中")
            chars = max(1, int(wrap_width / max(1, avg)))
            lines = []
            for para in str(text).split("\n"):
                for i in range(0, len(para), chars):
                    lines.append(para[i:i + chars])
        else:
            lines = str(text).split("\n")
        line_h = max(1, round(int(font_size) * 1.35))
        w = max(font.getlength(ln) for ln in lines) if lines else 0
        return (int(w), int(len(lines) * line_h))

    def paste(self, image_bytes, x, y, width=None, height=None):
        # 修复前这条链路是唯一没有 _guard_image_bytes 前置就直插 Pillow 解码的入口，
        # 恶意图片可绕过 image_resize/crop 的预算闸门（实测 paste 接受同一批畸形包）。
        _guard_image_bytes(image_bytes)
        from PIL import Image as PILImage
        from io import BytesIO
        img = PILImage.open(BytesIO(bytes(image_bytes))).convert("RGBA")
        if width is not None and height is not None:
            img = img.resize((int(width), int(height)), PILImage.Resampling.LANCZOS)
        self._img.alpha_composite(img, (int(x), int(y)))
        return self

    def blur(self, radius):
        from PIL import ImageDraw, ImageFilter
        self._img = self._img.filter(ImageFilter.GaussianBlur(float(radius)))
        self._draw = ImageDraw.Draw(self._img)
        return self

    def alpha_overlay(self, x0, y0, x1, y1, color, alpha):
        from PIL import Image, ImageDraw
        c = list(_parse_color(color, (0, 0, 0, 255)))
        c[3] = int(alpha)
        ov = Image.new("RGBA", (self._width, self._height), tuple(c))
        self._img = Image.alpha_composite(self._img, ov)
        self._draw = ImageDraw.Draw(self._img)
        return self

    def to_png(self):
        from io import BytesIO
        buf = BytesIO()
        self._img.save(buf, format="PNG")
        return buf.getvalue()


def _get_native_or_pil_canvas(width, height, bg_color=None, font_path=None):
    """返回原生 Canvas（顶部语义包装）或 PIL 回退 Canvas"""
    _require_backend()
    if _NATIVE is not None and hasattr(_NATIVE, "Canvas"):
        try:
            nc = _NATIVE.Canvas(int(width), int(height), bg_color, font_path)
            return _NativeCanvasProxy(nc, font_path)
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 Canvas 创建失败，回退 PIL: {e}")
    return _CanvasPIL(width, height, bg_color, font_path)


# ---------------------------------------------------------------- 解码前的像素预算
#
# 为什么必须在 Python 层做，而不能只靠后端：
#   1. 原生后端（image crate）默认有 512MB 单次分配上限，实测 11000²(462MB) 通过、
#      12000²(549MB) 报 "Memory limit exceeded"。但代码在原生失败后会**回退 PIL**，
#      而 PIL 的阈值完全不是一回事 —— 默认 89478485 像素只 warn，要到 1.79 亿才 raise，
#      实测 12000²(1.44 亿像素) 只打一条 DecompressionBombWarning 就照常分配。
#      也就是说原生那道闸门被回退逻辑绕过了。
#   2. PIL 的 Image.MAX_IMAGE_PIXELS 是**模块级全局变量**，同进程任何插件都能改掉
#      （甚至设为 None 直接关死）。在多插件共进程的宿主里不能当作安全边界。
# 所以在解码之前、与后端无关地做一次尺寸预算检查，超限直接拒绝。
# 第 5 轮收口：原阈值 1.34 亿像素（512MB 位图）高于 Pillow 自己的
# DecompressionBombWarning 阈值（89478485 像素），实测 10000²=1 亿像素的
# 真实解压炸弹被旧 guard 放行、只打一条警告。Pillow 官方明确要求把
# DecompressionBombWarning 当错误处理，因此预算下调到 4000 万像素。
_MAX_IMAGE_FILE_BYTES = 20 * 1024 * 1024   # 压缩文件字节上限（解码前）
_MAX_PIXELS = 40_000_000                   # RGBA 约 160 MB 位图
_MAX_FRAMES = 200                          # 动画帧数上限（Pillow D-3）
_ALLOWED_IMAGE_FORMATS = frozenset({'PNG', 'JPEG', 'GIF', 'WEBP', 'BMP'})
_IMAGE_MAGIC = (b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff',
                b'GIF87a', b'GIF89a', b'BM')
# Pillow E-2：EPS 会经 subprocess 调用系统 Ghostscript；SVG/MVG/XXE 属于
# 活动内容，必须在入口按字节拒绝，不能交给任何后端猜格式。
_FORBIDDEN_HEAD_MARKERS = (
    b'<svg', b'<!doctype', b'<!entity', b'<script',
    b'push graphic-context', b'%!ps', b'xmlns=',
)
# 第 6 轮 polyglot 深校验：第 5 轮只扫前 4096 字节，实测仍有两张真实
# PoC 被放行（EXIF 里的 onerror=、GIF/JS 无 trailer）。依据 Pillow Security
# Handbook T-1/T-2（元数据传播与隐藏数据通道）、CWE-434 polyglot、
# 以及「解码后重新编码」之外的轻量替代：整包活动标记 + 格式结束标记校验。
# 刻意不含 `<%`（实测 8 张正常 fuzz 样本的二进制里会随机出现，会误伤）。
_FORBIDDEN_CONTENT_MARKERS = (
    b'<script', b'<svg', b'<?php', b'<!entity', b'onerror=', b'onload=',
    b'javascript:', b'eval(', b'<html', b'<body',
    b'push graphic-context', b'%!ps',
)


def _guard_image_bytes(blob):
    """解码前 fail-closed 校验：长度 / magic / 格式 / 尺寸 / 帧数 / 尾随数据。

    只读文件头（Pillow 的 open 是惰性的），不做像素解码。与旧实现的关键差别：
    · 旧版 `except Exception: return` 把「读不出尺寸」一律当放行，实测 111 张
      恶意图片（含 900 亿像素声明、EPS/PostScript、SVG 脚本、MVG、空文件）
      全部 guard=pass；现在任何失败都拒绝。
    · 只放行白名单格式；EPS 会触发 Ghostscript subprocess，SVG/MVG/XXE 是活动内容。
    · DecompressionBombWarning 当错误处理（Pillow Security Handbook D-1）。
    · 拒绝 PNG IEND 之后的尾随数据（CVE-2023-28303 aCropalypse / CWE-359）。
    """
    # 【踩坑保佑】image_renderer 像素预算
    # 坑：MAX_IMAGE_PIXELS不可作安全边界  →  _guard_image_bytes
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

    data = blob if isinstance(blob, bytes) else bytes(blob)
    if not data:
        raise ValueError('图片为空，已拒绝解码')
    if len(data) > _MAX_IMAGE_FILE_BYTES:
        raise ValueError(f'图片文件超过 {_MAX_IMAGE_FILE_BYTES // 1024 // 1024}MB 上限，已拒绝解码')
    is_webp = data[:4] == b'RIFF' and data[8:12] == b'WEBP'
    if not (data.startswith(_IMAGE_MAGIC) or is_webp):
        raise ValueError('不是允许的图片格式（仅 PNG/JPEG/GIF/WEBP/BMP），已拒绝解码')
    head = data[:4096].lower()
    for marker in _FORBIDDEN_HEAD_MARKERS:
        if marker in head:
            raise ValueError('图片内含脚本/外部实体/PostScript/MVG 内容，已拒绝解码')
    try:
        import warnings
        from PIL import Image as _PILImage
        from io import BytesIO as _BytesIO
        with warnings.catch_warnings():
            warnings.simplefilter('error', _PILImage.DecompressionBombWarning)
            with _PILImage.open(_BytesIO(data)) as probe:
                fmt = (probe.format or '').upper()
                w, h = probe.size
                frames = int(getattr(probe, 'n_frames', 1) or 1)
    except Exception as e:
        raise ValueError(
            f'图片头解析失败或触发解压炸弹警告，已拒绝解码: {type(e).__name__}') from e
    if fmt not in _ALLOWED_IMAGE_FORMATS:
        raise ValueError(f'不允许的图片格式 {fmt!r}，已拒绝解码')
    if w <= 0 or h <= 0:
        raise ValueError('图片尺寸非法，已拒绝解码')
    if w * h > _MAX_PIXELS:
        raise ValueError(
            '图片尺寸过大已拒绝解码：{}×{} = {:.1f} 百万像素，上限 {:.1f} 百万像素'
            '（约 {}MB 位图）。请先缩小后再处理。'.format(
                w, h, w * h / 1e6, _MAX_PIXELS / 1e6,
                _MAX_PIXELS * 4 // (1024 * 1024)))
    if frames > _MAX_FRAMES:
        raise ValueError(f'动画帧数 {frames} 超过上限 {_MAX_FRAMES}，已拒绝解码')
    # 全文活动内容扫描：EXIF/XMP/注释区或尾随数据里塞脚本的 polyglot，
    # 光看前 4096 字节和 magic 是看不出来的（本机两张真实 PoC 实测放行）。
    low_all = data.lower()
    for marker in _FORBIDDEN_CONTENT_MARKERS:
        if marker in low_all:
            raise ValueError(
                '图片内嵌脚本/外部实体/活动载荷标记 %r，已拒绝解码' % marker.decode('latin1'))
    if fmt == 'PNG':
        iend = data.rfind(b'IEND\xaeB`\x82')
        if iend < 0 or len(data) - (iend + 8) != 0:
            raise ValueError('PNG 缺 IEND 或 IEND 后有尾随数据（aCropalypse 残留），已拒绝解码')
    if fmt == 'JPEG':
        eoi = data.rfind(b'\xff\xd9')
        if eoi < 0 or len(data) - (eoi + 2) != 0:
            raise ValueError('JPEG 缺 EOI 或 EOI 后有尾随数据（polyglot 附加载荷），已拒绝解码')
    if fmt == 'GIF' and not data.endswith(b'\x3b'):
        raise ValueError('GIF 缺 trailer 或尾部附加数据（GIF/JS polyglot），已拒绝解码')


def image_resize(img_bytes, width, height, keep_ratio=True):
    """等比缩放图片，返回 PNG bytes（原生优先，PIL 回退）"""
    _require_backend()
    _guard_image_bytes(img_bytes)
    if _NATIVE is not None and hasattr(_NATIVE, "image_resize"):
        try:
            return _NATIVE.image_resize(bytes(img_bytes), int(width), int(height), bool(keep_ratio))
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 image_resize 失败，回退 PIL: {e}")
    from PIL import Image as PILImage
    from io import BytesIO
    img = PILImage.open(BytesIO(bytes(img_bytes))).convert("RGBA")
    if keep_ratio:
        ratio = min(width / img.width, height / img.height)
        nw, nh = max(1, round(img.width * ratio)), max(1, round(img.height * ratio))
    else:
        nw, nh = int(width), int(height)
    img = img.resize((nw, nh), PILImage.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def image_crop_16_9(img_bytes):
    """16:9 居中裁剪，返回 PNG bytes"""
    _require_backend()
    _guard_image_bytes(img_bytes)
    if _NATIVE is not None and hasattr(_NATIVE, "image_crop_16_9"):
        try:
            return _NATIVE.image_crop_16_9(bytes(img_bytes))
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 image_crop_16_9 失败，回退 PIL: {e}")
    from PIL import Image as PILImage
    from io import BytesIO
    img = PILImage.open(BytesIO(bytes(img_bytes))).convert("RGBA")
    w, h = img.size
    target_h = round(w * 9 / 16)
    if target_h <= h:
        crop = img.crop((0, (h - target_h) // 2, w, (h - target_h) // 2 + target_h))
    else:
        target_w = round(h * 16 / 9)
        crop = img.crop(((w - target_w) // 2, 0, (w - target_w) // 2 + target_w, h))
    buf = BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def image_circle_crop(img_bytes, size=256):
    """圆形裁剪（头像），返回 PNG bytes"""
    _require_backend()
    _guard_image_bytes(img_bytes)
    if _NATIVE is not None and hasattr(_NATIVE, "image_circle_crop"):
        try:
            return _NATIVE.image_circle_crop(bytes(img_bytes), int(size))
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 image_circle_crop 失败，回退 PIL: {e}")
    from PIL import Image as PILImage, ImageDraw
    from io import BytesIO
    img = PILImage.open(BytesIO(bytes(img_bytes))).convert("RGBA").resize(
        (int(size), int(size)), PILImage.Resampling.LANCZOS)
    mask = PILImage.new("L", (int(size), int(size)), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, int(size), int(size)), fill=255)
    out = PILImage.new("RGBA", (int(size), int(size)), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def image_round_corners(img_bytes, radius=16):
    """圆角裁剪，返回 PNG bytes"""
    _require_backend()
    _guard_image_bytes(img_bytes)
    if _NATIVE is not None and hasattr(_NATIVE, "image_round_corners"):
        try:
            return _NATIVE.image_round_corners(bytes(img_bytes), int(radius))
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 image_round_corners 失败，回退 PIL: {e}")
    from PIL import Image as PILImage, ImageDraw
    from io import BytesIO
    img = PILImage.open(BytesIO(bytes(img_bytes))).convert("RGBA")
    w, h = img.size
    mask = PILImage.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=int(radius), fill=255)
    out = PILImage.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def image_blur(img_bytes, radius=4.0):
    """高斯模糊，返回 PNG bytes"""
    _require_backend()
    _guard_image_bytes(img_bytes)
    if _NATIVE is not None and hasattr(_NATIVE, "image_blur"):
        try:
            return _NATIVE.image_blur(bytes(img_bytes), float(radius))
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 image_blur 失败，回退 PIL: {e}")
    from PIL import Image as PILImage, ImageFilter
    from io import BytesIO
    img = PILImage.open(BytesIO(bytes(img_bytes))).convert("RGBA").filter(
        ImageFilter.GaussianBlur(float(radius)))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def image_flip(img_bytes, direction="horizontal"):
    """翻转图片，返回 PNG bytes"""
    _require_backend()
    _guard_image_bytes(img_bytes)
    if _NATIVE is not None and hasattr(_NATIVE, "image_flip"):
        try:
            return _NATIVE.image_flip(bytes(img_bytes), direction)
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 image_flip 失败，回退 PIL: {e}")
    from PIL import Image as PILImage
    from io import BytesIO
    img = PILImage.open(BytesIO(bytes(img_bytes))).convert("RGBA")
    img = img.transpose(PILImage.Transpose.FLIP_LEFT_RIGHT if direction == "horizontal" else PILImage.Transpose.FLIP_TOP_BOTTOM)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def image_rotate(img_bytes, angle=90):
    """旋转图片，返回 PNG bytes"""
    _require_backend()
    _guard_image_bytes(img_bytes)
    if _NATIVE is not None and hasattr(_NATIVE, "image_rotate"):
        try:
            return _NATIVE.image_rotate(bytes(img_bytes), int(angle))
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 image_rotate 失败，回退 PIL: {e}")
    from PIL import Image as PILImage
    from io import BytesIO
    img = PILImage.open(BytesIO(bytes(img_bytes))).convert("RGBA")
    img = img.rotate(-int(angle), expand=True, resample=PILImage.Resampling.BICUBIC)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def image_gray(img_bytes):
    """灰度化，返回 PNG bytes"""
    _require_backend()
    _guard_image_bytes(img_bytes)
    if _NATIVE is not None and hasattr(_NATIVE, "image_gray"):
        try:
            return _NATIVE.image_gray(bytes(img_bytes))
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 image_gray 失败，回退 PIL: {e}")
    from PIL import Image as PILImage
    from io import BytesIO
    img = PILImage.open(BytesIO(bytes(img_bytes))).convert("L").convert("RGBA")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def image_contrast(img_bytes, factor=1.5):
    """对比度调整，返回 PNG bytes"""
    _require_backend()
    _guard_image_bytes(img_bytes)
    if _NATIVE is not None and hasattr(_NATIVE, "image_contrast"):
        try:
            return _NATIVE.image_contrast(bytes(img_bytes), float(factor))
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 image_contrast 失败，回退 PIL: {e}")
    from PIL import Image as PILImage, ImageEnhance
    from io import BytesIO
    img = PILImage.open(BytesIO(bytes(img_bytes))).convert("RGBA")
    img = ImageEnhance.Contrast(img).enhance(float(factor))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def image_overlay(bg_bytes, fg_bytes, x=0, y=0):
    """将前景图合成到背景图 (x,y)，返回 PNG bytes"""
    _require_backend()
    _guard_image_bytes(bg_bytes)
    _guard_image_bytes(fg_bytes)
    if _NATIVE is not None and hasattr(_NATIVE, "image_overlay"):
        try:
            return _NATIVE.image_overlay(bytes(bg_bytes), bytes(fg_bytes), int(x), int(y))
        except Exception as e:
            logger.warning(f"[image_renderer] 原生 image_overlay 失败，回退 PIL: {e}")
    from PIL import Image as PILImage
    from io import BytesIO
    bg = PILImage.open(BytesIO(bytes(bg_bytes))).convert("RGBA")
    fg = PILImage.open(BytesIO(bytes(fg_bytes))).convert("RGBA")
    bg.alpha_composite(fg, (int(x), int(y)))
    buf = BytesIO()
    bg.save(buf, format="PNG")
    return buf.getvalue()

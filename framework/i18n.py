"""多语言（i18n）：把面向用户的提示文案从代码里剥离出来。

三种语言：

========  ==========================================  ======================
代码       说明                                          基调
========  ==========================================  ======================
zh        简体中文（标准，默认）                          正式、简洁
ciallo    中文活泼版（Ciallo～(∠・ω< )⌒☆ 风格）          俏皮、带颜文字
en        English                                     正式、简洁
========  ==========================================  ======================

关于 Ciallo～
    「Ciallo～(∠・ω< )⌒☆」是柚子社 galgame《魔女的夜宴》女主角因幡巡
    的标志性问候语，由意大利语「Ciao」与英语「Hello」合成，配上配套的
    卖萌颜文字，是中文 ACG 社群里辨识度极高的活泼打招呼用语。
    这里把它作为「活泼版」的语气基调，只改语气、不改事实。

词条存放
    文案不再内嵌在源码字典里，而是按语言放在 ``framework/lang/`` 目录下：

        framework/lang/zh.yaml        # 简体中文（默认，也是回退源）
        framework/lang/en.yaml
        framework/lang/ciallo.yaml

    每个文件是嵌套结构，加载时自动展平为「模块.语义」点号 key，与 ``t()`` 调用一致。
    新增语言只需在 ``framework/lang/`` 下加一个 ``<code>.yaml``，无需改代码；
    新增文案在对应 yaml 里追加即可，不用碰 i18n.py。

用法::

    from framework.i18n import t, set_language, get_language

    set_language('ciallo')
    t('framework.starting')                 # → Ciallo～(∠・ω< )⌒☆ ...
    t('framework.started', elapsed='1.23')  # 支持 {} 占位符

设计约定
    * 纯数据文件 + 标准库路径加载（YAML 由 PyYAML 解析，框架运行环境已具备），
      不引入 gettext / .po / .mo；改完文案即生效，适合「文案随版本走」的场景。
    * 词条缺失时按「当前语言 → zh → key 本身」回退，绝不因翻译缺失抛异常，
      保证任何语言下框架都能正常启动。
    * 只本地化「给人看的提示文案」；协议标识符（如 console 的
      ``'ZCBOT console'`` banner）一律不翻译，否则会破坏进程探测与测试断言。
"""

from __future__ import annotations

import logging
from pathlib import Path

try:
    import yaml
    _HAS_YAML = True
except Exception:  # noqa: BLE001
    yaml = None
    _HAS_YAML = False

logger = logging.getLogger('zcbot.i18n')

# ── 语言代码 ────────────────────────────────────────────────
LANG_ZH = 'zh'
LANG_CIALLO = 'ciallo'
LANG_EN = 'en'

DEFAULT_LANG = LANG_ZH

# ── 加载 ────────────────────────────────────────────────────
_LANG_DIR = Path(__file__).parent / 'lang'
# 内置语言代码的优先顺序（其余 lang/ 下的文件按文件名追加）
_BUILTIN_ORDER = (LANG_ZH, LANG_CIALLO, LANG_EN)
_LANGS: dict[str, dict[str, str]] = {}
_current = DEFAULT_LANG


def _flatten(d: dict, prefix: str = '') -> dict:
    """把嵌套字典展平成「a.b.c」点号 key。"""
    out: dict = {}
    for k, v in d.items():
        key = f'{prefix}.{k}' if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def _load_lang(code: str) -> dict:
    """读取单个语言文件；不存在 / 解析失败都安全地返回空字典。"""
    path = _LANG_DIR / f'{code}.yaml'
    if not path.exists():
        return {}
    if not _HAS_YAML:
        logger.warning('[i18n] 未安装 PyYAML，无法加载语言文件: %s', path)
        return {}
    try:
        with open(path, encoding='utf-8') as fh:
            data = yaml.safe_load(fh) or {}
        return _flatten(data)
    except Exception as e:  # noqa: BLE001
        logger.warning('[i18n] 加载语言文件失败 %s: %s', path, e)
        return {}


def _load_all() -> None:
    if not _LANG_DIR.exists():
        logger.warning('[i18n] 语言目录不存在: %s', _LANG_DIR)
        return
    for code in _BUILTIN_ORDER:
        _LANGS[code] = _load_lang(code)
    # 兼容 lang/ 下其它自定义语言文件
    for f in sorted(_LANG_DIR.glob('*.yaml')):
        code = f.stem
        if code not in _LANGS:
            _LANGS[code] = _load_lang(code)


_load_all()


def available_languages() -> list:
    """返回所有支持的语言代码。"""
    return list(_LANGS.keys())


def normalize(lang) -> str:
    """把各种写法归一化成标准语言代码；无法识别时回退到默认语言。

    接受 ``zh`` / ``zh_CN`` / ``zh-CN`` / ``ZH``、``ciallo`` / ``CIALLO``、
    ``en`` / ``en_US`` / ``english`` 等常见写法。
    """
    s = str(lang or '').strip().lower().replace('_', '-')
    if not s:
        return DEFAULT_LANG
    if s in _LANGS:
        return s
    # zh_CN / en_US / english 等带区域或全称的写法 → 取主语言
    head = s.split('-')[0]
    if head in _LANGS:
        return head
    if head.startswith('english'):
        return LANG_EN
    return DEFAULT_LANG


def set_language(lang) -> str:
    """设置当前语言，返回实际生效的语言代码（无法识别时回退 zh）。"""
    global _current
    _current = normalize(lang)
    return _current


def get_language() -> str:
    """当前生效的语言代码。"""
    return _current


def t(key: str, **kwargs) -> str:
    """取一条本地化文案；支持 {} 占位符；缺失时按 当前语言 → zh → key 回退。

    占位符填充失败（模板与入参不匹配）时原样返回模板，不抛异常——
    启动日志绝不能因为文案问题把框架带崩。
    """
    table = _LANGS.get(_current) or {}
    text = table.get(key)
    if text is None:
        text = _LANGS.get(DEFAULT_LANG, {}).get(key)
    if text is None:
        return key
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

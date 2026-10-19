# -*- coding: utf-8 -*-
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

用法::

    from framework.i18n import t, set_language, get_language

    set_language('ciallo')
    t('framework.starting')                 # → Ciallo～(∠・ω< )⌒☆ ...
    t('framework.started', elapsed='1.23')  # 支持 {} 占位符

设计约定
    * 纯 Python 词条字典，不引入 gettext / .po / .mo 与任何新依赖，
      免编译、改完即生效，适合「文案随版本走、不随系统 locale 走」的场景。
    * 词条缺失时按「当前语言 → zh → key 本身」回退，绝不因翻译缺失抛异常，
      保证任何语言下框架都能正常启动。
    * 只本地化「给人看的提示文案」；协议标识符（如 console 的
      ``'ZCBOT console'`` banner）一律不翻译，否则会破坏进程探测与测试断言。
"""

from __future__ import annotations

# ── 语言代码 ────────────────────────────────────────────────
LANG_ZH = 'zh'
LANG_CIALLO = 'ciallo'
LANG_EN = 'en'

DEFAULT_LANG = LANG_ZH

# ── 词条表 ──────────────────────────────────────────────────
# 约定：key 用「模块.语义」命名；{xxx} 为运行时填充的占位符。
LANGS: dict[str, dict[str, str]] = {
    LANG_ZH: {
        'framework.starting': 'ZCBOT 框架 启动中...',
        'framework.started': '框架启动完成，等待消息...（总耗时 {elapsed}s）',
        'framework.stopping': '正在停止框架...',
        'framework.stopped': '框架已停止',
    },
    LANG_CIALLO: {
        'framework.starting': 'Ciallo～(∠・ω< )⌒☆ ZCBOT 框架启动中咯～ 稍等一小下，马上就好喵～',
        'framework.started': 'Ciallo～(∠・ω< )⌒☆ 启动完成啦～ 正竖起耳朵等你的消息哦～（总共花了 {elapsed}s）',
        'framework.stopping': 'Ciallo～(∠・ω< )⌒☆ 要收工啦～ 正在把框架停下来...',
        'framework.stopped': 'Ciallo～(∠・ω< )⌒☆ 框架已经乖乖躺好咯～ 下次见！',
    },
    LANG_EN: {
        'framework.starting': 'ZCBOT framework starting...',
        'framework.started': 'Framework started, waiting for messages... (took {elapsed}s)',
        'framework.stopping': 'Stopping framework...',
        'framework.stopped': 'Framework stopped',
    },
}

# ── 运行时状态 ──────────────────────────────────────────────
_current = DEFAULT_LANG


def available_languages() -> list[str]:
    """返回所有支持的语言代码。"""
    return list(LANGS.keys())


def normalize(lang) -> str:
    """把各种写法归一化成标准语言代码；无法识别时回退到默认语言。

    接受 ``zh`` / ``zh_CN`` / ``zh-CN`` / ``ZH``、``ciallo`` / ``CIALLO``、
    ``en`` / ``en_US`` / ``english`` 等常见写法。
    """
    s = str(lang or '').strip().lower().replace('_', '-')
    if not s:
        return DEFAULT_LANG
    if s in LANGS:
        return s
    # zh_CN / en_US / english 等带区域或全称的写法 → 取主语言
    head = s.split('-')[0]
    if head in LANGS:
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
    table = LANGS.get(_current) or {}
    text = table.get(key)
    if text is None:
        text = LANGS.get(DEFAULT_LANG, {}).get(key)
    if text is None:
        return key
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

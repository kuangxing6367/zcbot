# -*- coding: utf-8 -*-
"""验证 i18n 词条已从源码字典外置到 framework/lang/*.yaml，且 API 行为不变。"""
import os
import sys

ROOT = r'E:/工程/机器人/zgric_onebot11_35xLe'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import framework.i18n as i18n  # noqa: E402

_FAILED = False


def check(cond, msg):
    global _FAILED
    if cond:
        print('PASS', msg)
    else:
        print('FAIL', msg)
        _FAILED = True


# 1) 默认 zh：占位符 + 普通
i18n.set_language('zh')
check(i18n.t('framework.started', elapsed='1.23') ==
      '框架启动完成，等待消息...（总耗时 1.23s）', 'zh 占位符填充')
check(i18n.t('framework.initializing') == '正在初始化框架核心引擎...', 'zh 普通文案')

# 2) en
i18n.set_language('en')
check(i18n.t('framework.started', elapsed='1.23') ==
      'Framework started, waiting for messages... (took 1.23s)', 'en 占位符填充')
check(i18n.t('framework.initializing') == 'Initializing framework core engine...', 'en 普通文案')

# 3) ciallo 活泼版
i18n.set_language('ciallo')
check(i18n.t('framework.started', elapsed='1.23').startswith('Ciallo～'), 'ciallo 活泼版基调')

# 4) 缺失 key 按 key 本身回退（不抛异常）
i18n.set_language('en')
check(i18n.t('nonexistent.key') == 'nonexistent.key', '缺失 key 返回 key 本身')

# 5) 新增文案（验证 yaml 展平：嵌套 framework.xxx 正确解析）
i18n.set_language('zh')
check(i18n.t('plugin.loaded', name='demo', version='1.0') ==
      '[demo] 加载成功 v1.0', '嵌套 key 展平 + 双占位符')

# 6) 可用语言集合
langs = i18n.available_languages()
check({'zh', 'en', 'ciallo'}.issubset(set(langs)), f'可用语言含三内置: {langs}')

# 复位默认语言，避免影响其它测试
i18n.set_language('zh')

if _FAILED:
    print('RESULT: FAIL')
    sys.exit(1)
print('RESULT: ALL PASS')

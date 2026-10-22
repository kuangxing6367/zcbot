# -*- coding: utf-8 -*-
"""四类事件统一分类契约层（事件分发架构设计「最终版」C 项落地）。

理想架构把消息事件显式分为四类，并贯穿串行/并行/Hook 包装：
  - GENERAL 一般消息：命中静态/前缀命令（如「签到」）
  - REGEX  正则消息：命中正则命令（如「/使用优惠 xxx」）
  - ALL    全量消息：任意消息（message 事件总线统一监听通道）
  - RAW    原始消息：未经处理的底层原始事件（raw_message 处理器）

此前这四类由三套并存代码（命令引擎 / 事件总线 / raw handlers）各自隐式实现，
功能等价但无统一契约、插件无法直接判断「这条消息属于哪一类」。本模块把它们
收敛为单一真相源：每个 message 事件进入管线时一次性算出四类标记并挂在 Event 上，
下游与插件用同一套语义读取（ev.category / ev.categories），并在分发末端发射
message.classified 扩展点，使四类事件都能被 Hook 包装。
"""
from enum import Enum


class EventCategory(str, Enum):
    """消息事件的四类 + 非消息。字符串枚举，便于日志/序列化直接取字符串值。"""
    GENERAL = "general"   # 一般消息（命中静态/前缀命令）
    REGEX = "regex"       # 正则消息（命中正则命令）
    ALL = "all"           # 全量消息（message 事件总线兜底监听）
    RAW = "raw"           # 原始消息（raw_message 处理器横切维度）
    NONE = "none"         # 非消息事件（meta_event / notice / request）


# 命令命中后回填到 Event._category_command 的取值
COMMAND_GENERAL = "general"
COMMAND_REGEX = "regex"


def initial_classification(raw: dict) -> dict:
    """每个 message 事件进入管线时的初始分类标记（单一真相源）。

    返回 dict：
      is_message : 是否为消息事件（post_type == 'message'）
      is_raw     : 是否过 raw_message 处理器（每条消息恒为 True）
      is_all     : 是否进 message 事件总线兜底（每条消息恒为 True）
      command    : 命中命令类别（None / 'general' / 'regex'），由路由命中后回填

    RAW / ALL 是「横切维度」，对每条消息天然成立；GENERAL/REGEX 是「命令维度」，
    需路由真正命中命令后才定，故初始为 None，命中后再回填。
    """
    is_message = (raw.get('post_type') or raw.get('type')) == 'message'
    return {
        'is_message': is_message,
        'is_raw': is_message,   # 每条消息都过 raw_message 处理器
        'is_all': is_message,   # 每条文本消息都进 message 总线兜底
        'command': None,        # 路由命中后回填 'general' / 'regex'
    }

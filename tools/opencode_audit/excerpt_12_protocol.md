# 第 12 轮侦察材料：framework/messaging/protocol.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 """
   2 协议适配器接口 + 服务注册表
   3 框架核心通过此模块定义服务契约，官方/第三方插件实现并注册。
   4 
   5 设计目标：framework 核心不认识任何具体协议（OneBot/HTTP/自定义…），
   6 它只面向 ProtocolAdapter 抽象与 ServiceRegistry 编程。
   7 """
   8 import asyncio
   9 import logging
  10 import threading
  11 from abc import ABC, abstractmethod
  12 from typing import Any, Dict, Optional
  13 
  14 from framework.hooks import HookPoints
  15 from framework.messaging.contract import (
  16     CAP_TEXT,
  17     Capabilities,
  18     as_capabilities,
  19     contract_report,
  20     normalize_event,
  21     validate_adapter,
  22 )
  23 from framework.messaging.segments import (
  24     normalize_message,
  25     segments_to_text,
  26     to_outgoing,
  27 )
  28 
  29 logger = logging.getLogger('zcbot')
  30 
  31 
  32 class ProtocolAdapter(ABC):
  33     """协议适配器抽象基类（OneBot/HTTP/自定义协议等）"""
  34     # 【踩坑保佑】可靠性与限流 出站闸门
  35     # 坑：不挂在插件API上  →  ProtocolAdapter
  36     #                    _ooOoo_
  37     #                   o8888888o
  38     #                   88" . "88
  39     #                   (| -_- |)
  40     #                    O\ = /O
  41     #                ____/`---'\____
  42     #              .   ' \\| |// `.
  43     #               / \\||| : |||// \
  44     #             / _||||| -:- |||||- \
  45     #               | | \\\ - /// | |
  46     #             | \_| ''\---/'' | |
  47     #              \ .-\__ `-` ___/-. /
  48     #           ___`. .' /--.--\ `. . __
  49     #        ."" '< `.___\_<|>_/___.' >'"".
  50     #       | | : `- \`.;`\ _ /`;.`/ - ` : | |
  51     #         \ \ `-. \_ __\ /__ _/ .-` / /
  52     # ======`-.____`-.___\_____/___.-`____.-'======
  53     #                    `=---='
  54     #
  55     # .............................................
  56     #          佛祖保佑             永无BUG
  57 
  58 
  59     # 接入端唯一 id（连接页/多接入端场景按此引用；子类可覆写）
  60     adapter_id: str = ''
  61 
  62     @abstractmethod
  63     async def handle_event(self, raw_event: dict, bot_name: str) -> dict | None:
  64         """
  65         将原始协议事件转换为框架内部事件格式
  66         :return: 内部事件 dict，或 None 表示丢弃
  67         """
  68         ...
  69 
  70     @abstractmethod
  71     async def call_api(self, action: str, bot: str = None, **params) -> dict:
  72         """调用协议 API（异步，协议侧必须实现）"""
  73         ...
  74 
  75     @abstractmethod
  76     def get_connected_bots(self) -> list:
  77         """返回已连接的来源/实例列表"""
  78         ...
  79 
  80     @abstractmethod
  81     def start(self):
  82         """启动适配器"""
  83         ...
  84 
  85     @abstractmethod
  86     async def stop(self):
  87         """停止适配器"""
  88         ...
  89 
  90     # ─────────────────────────────────────────────────────────────
  91     # 消息契约（可选实现，基类给全套兜底）
  92     #
  93     # 内核对"一条消息"只有一种形状：规范消息段列表
  94     # [{'type': 'text'|'image'|'voice'|..., 'data': {...}}].
  95     # 接入端只需在两个方向各做一个翻译，其余由基类兜底：
  96     #
  97     #   原生协议 ──normalize_incoming──▶ 规范段 ──▶ ev.segments（插件）
  98     #   插件 message ──▶ 规范段 ──to_native──▶ 原生协议（发送）
  99     #
 100     # 不实现也能跑：基类默认把入站消息交给通用归一化器（能识别 OneBot 段
 101     # 数组与 CQ 码），出站原样透传规范段。但那样附件/语音多半会丢，
 102     # 所以接入端应当实现这两个方法——契约自检会点名没实现的。
 103     # ─────────────────────────────────────────────────────────────
 104 
 105     def capabilities(self) -> Capabilities:
 106         """
 107         能力自述：本接入端能收/能发什么。
 108 
 109         默认只声明"文本"；子类应当如实声明，插件据此决定要不要发图片、
 110         要不要挂按钮，而不是发出去才发现协议不支持。
 111         """
 112         return Capabilities(inbound=[CAP_TEXT], outbound=[CAP_TEXT])
 113 
 114     def supports(self, cap: str, direction: str = 'out') -> bool:
 115         """能力查询：`adapter.supports('voice', 'in')`"""
 116         try:
 117             return as_capabilities(self.capabilities()).supports(cap, direction)
 118         except Exception as _exc:
 119             logger.debug('[supports] 捕获异常后按降级路径继续: %s', _exc)
 120             return False
 121 
 122     def normalize_incoming(self, raw_message) -> list:
 123         """
 124         入站翻译：协议原生消息 → 规范消息段列表。
 125 
 126         默认实现交给通用归一化器（OneBot 段数组 / CQ 码 / 纯文本）。
 127         子类应覆写以保留本协议的附件、语音、贴纸等语义。
 128         """
 129         return normalize_message(raw_message)
 130 
```

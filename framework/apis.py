# -*- coding: utf-8 -*-
"""
framework.apis —— 兼容 shim

Web 应用层已按功能域拆分到 framework/api/ 包（auth / admins / apikeys /
dashboard / plugins / commands / users_groups / tasks / logs / config /
db_gateway / framework_ops / webui / files / stats / perm_api / static_routes）。

本模块仅作向后兼容入口，重导出 create_web_app / WebServer，
外部 `from framework.apis import create_web_app, WebServer` 的代码不受影响。
"""
from framework.api.webapp import create_web_app, WebServer

__all__ = ['create_web_app', 'WebServer']

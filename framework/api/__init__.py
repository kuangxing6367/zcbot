# -*- coding: utf-8 -*-
"""
framework.api —— Web 应用层包

负责 Web 管理后台 / REST API 的构建与路由注册。
原 framework/apis.py（巨型 create_web_app 上帝函数）将逐步按功能域迁入此包：

    registry.py  可插入路由注册表（ctx.register_api 的基础）
    context.py   ApiContext：集中持有 app / framework / db / config 等共享状态

向后兼容：framework/apis.py 仍导出 create_web_app / WebServer，
外部用 `from framework.apis import ...` 的代码不受影响。
"""

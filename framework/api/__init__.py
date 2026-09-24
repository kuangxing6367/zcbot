# -*- coding: utf-8 -*-
"""
framework.api —— Web 应用层包

负责 Web 管理后台 / REST API 的构建与路由注册。
原 framework/apis.py（巨型 create_web_app 上帝函数）将逐步按功能域迁入此包：

    registry.py  可插入路由注册表（ctx.register_api 的基础）
    context.py   ApiContext：集中持有 app / framework / db / config 等共享状态
    webapp.py    create_web_app 编排 + 鉴权/限速/审计 + ctx 分发
    webserver.py WebServer（独立线程 HTTP/HTTPS）
    app_helpers.py make_app_helpers()：版本 / yaml / 市场 / GitHub 下载辅助工厂
    plugins.py / plugin_market.py / plugin_meta.py  插件域按生命周期/市场/元数据拆分

向后兼容：framework/apis.py 仍导出 create_web_app / WebServer，
外部用 `from framework.apis import ...` 的代码不受影响。
"""

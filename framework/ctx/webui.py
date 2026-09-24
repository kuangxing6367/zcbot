# -*- coding: utf-8 -*-
"""PluginContext 仪表盘卡片 / WebUI 内嵌 / 管理页扩展 / 注册表 getter（自 ctx.py 剥离的 mixin）"""
from typing import Callable

class PluginWebuiMixin:
    """仪表盘卡片 / WebUI 内嵌 / 管理页扩展 / 注册表 getter"""

    # 以下方法依赖 PluginContext.__init__ 建立的实例字段

    # ---- 仪表盘卡片 ----

    def dashboard_card(self, title: str, handler: Callable, icon: str = None, priority: int = 50):
        """
        注册一个仪表盘卡片
        handler 返回 dict: {title, value, label, icon, color}
        """
        self._dashboard_cards.append({
            'plugin_name': self._plugin_name,
            'title': title,
            'handler': handler,
            'icon': icon,
            'priority': priority,
        })

    # ---- WebUI 内嵌 ----

    def webui(self, title: str, entry: str = 'index.html', icon: str = None, order: int = 50,
              sidebar: bool = False):
        """
        注册插件 WebUI 页面
        插件目录下的 web/ 子目录中的 HTML/JS/CSS 文件将被框架内嵌展示

        :param title: 页面标题（显示在导航栏 / 侧边栏）
        :param entry: 入口文件名（默认 index.html）
        :param icon: 图标（HTML 实体或 emoji，留空则用默认图标）
        :param order: 排序权重（越小越靠前）
        :param sidebar: 是否在侧边栏注册独立入口（True=独立菜单项跳转到 /plugin/<name>；
                        False=仍归入「插件页面」聚合页）。默认 False 以兼容旧插件。
        """
        self._framework.plugin_loader.register_webui(self._plugin_name, {
            'title': title,
            'entry': entry,
            'icon': icon,
            'order': order,
            'sidebar': sidebar,
        })

    def override_webui(self):
        """
        让本插件接管整个 Web 前端。

        调用后，框架的根路由 `/` 与静态资源（/css /js /img 与各 *.html 页面）
        全部改为从本插件的 web/ 目录服务，取代框架自带的默认前端。
        插件被禁用/卸载/删除时自动回退框架默认前端。
        """
        return self._framework.plugin_loader.override_webui(self._plugin_name)

    # ---- 内部方法 ----

    def register_group_extension(self, key: str, title: str, handler: Callable,
                                ext_type: str = 'column'):
        """
        注册 WebUI「群组管理」页插件扩展

        :param key: 扩展唯一键（如 'sign_count'）
        :param title: 列标题 / 面板标题
        :param handler: 数据回调 handler(group_id) -> 内容
                        column 类型：返回字符串（显示在表格列）
                        panel 类型：返回 {label: value, ...} 键值字典（显示在详情弹窗）
        :param ext_type: 'column'（表格列）或 'panel'（详情弹窗面板）
        handler 在独立线程中执行并限时 2 秒（超时显示占位，不卡 Web 线程）；
        异常时该格显示 '-'。
        """
        if not callable(handler):
            raise TypeError(f"handler '{getattr(handler, '__name__', handler)}' 不可调用")
        self._group_extensions.append({
            'key': key, 'title': title, 'handler': handler,
            'type': ext_type if ext_type in ('column', 'panel') else 'column',
        })

    def register_user_extension(self, key: str, title: str, handler: Callable,
                                ext_type: str = 'column'):
        """
        注册 WebUI「用户管理」页插件扩展（参数与 register_group_extension 相同，
        handler 签名：handler(user_id) -> 字符串 或 {label: value} 字典）
        """
        if not callable(handler):
            raise TypeError(f"handler '{getattr(handler, '__name__', handler)}' 不可调用")
        self._user_extensions.append({
            'key': key, 'title': title, 'handler': handler,
            'type': ext_type if ext_type in ('column', 'panel') else 'column',
        })

    def _get_group_extensions(self) -> list:
        """获取本次注册周期收集的群组页扩展"""
        return list(self._group_extensions)

    def _get_user_extensions(self) -> list:
        """获取本次注册周期收集的用户页扩展"""
        return list(self._user_extensions)

    def _get_raw_message_handlers(self) -> list:
        """获取本次注册周期收集的原始消息处理器"""
        return list(self._raw_message_handlers)

    def _get_commands(self) -> list:
        return self._commands

    def _get_tasks(self) -> list:
        return self._tasks

    def _get_dashboard_cards(self) -> list:
        return self._dashboard_cards

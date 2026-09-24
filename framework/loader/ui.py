# -*- coding: utf-8 -*-
"""
PluginLoader 的 UI 相关能力（自 framework/loader/ 剥离）

三个 mixin：
  PluginUiExtensionsMixin  仪表盘卡片 + 插件 UI 扩展点
  PluginWebuiMixin         插件 WebUI 注册与路径解析
  PluginGroupSettingsMixin 群级插件开关（带缓存）
"""

# 仪表盘卡片线程池（懒创建；global 指向本模块命名空间）
_cards_executor = None


class PluginUiExtensionsMixin:
    """仪表盘卡片同步 + 插件 UI 扩展点调用"""

    def _sync_dashboard_cards(self, plugin_name: str, cards: list):
        """存储仪表盘卡片信息到插件信息中"""
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            if info:
                info['dashboard_cards'] = cards

    def get_dashboard_cards(self) -> list:
        """
        获取所有仪表盘卡片（按优先级排序）

        每个卡片 handler 在独立线程池中执行并限时 2 秒：防止某个插件卡片的慢操作
        （如内存统计、网络请求）占满 Web 线程池导致仪表盘卡死；超时的卡片
        返回占位数据并记录告警（超时任务在后台继续跑，不阻塞 Web 线程）。
        """
        global _cards_executor
        result = []
        with self._lock:
            items = []
            for name, info in self._loaded_plugins.items():
                cards = info.get('dashboard_cards', [])
                for card in cards:
                    items.append((name, card))
        if not items:
            return result

        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
        if _cards_executor is None:
            _cards_executor = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix='zccards')
        futures = {}
        for name, card in items:
            handler = card['handler']
            fut = _cards_executor.submit(handler)
            futures[fut] = (name, card)
        for fut, (name, card) in futures.items():
            try:
                card_data = fut.result(timeout=2.0)
            except FutureTimeout:
                logger.warning(f"[{name}] 仪表盘卡片执行超时（>2s），已跳过: {card.get('title', '')}")
                card_data = {"value": "⏳ 加载超时", "label": card.get('title', ''), "timeout": True}
                fut.cancel()
            except Exception as e:
                logger.error(f"[{name}] 仪表盘卡片异常: {e}")
                card_data = None
            if card_data:
                result.append({
                    'plugin_name': name,
                    'title': card.get('title', ''),
                    'icon': card.get('icon'),
                    'priority': card.get('priority', 50),
                    'data': card_data,
                })
        result.sort(key=lambda x: x['priority'])
        return result

    # ==================================================================
    #  WebUI 群组/用户管理页插件扩展
    # ==================================================================

    def get_ui_extensions(self, scope: str) -> list:
        """
        获取群组(scope='groups')或用户(scope='users')管理页的全部插件扩展元信息
        返回: [{key, title, plugin, type}]
        """
        field = 'group_extensions' if scope == 'groups' else 'user_extensions'
        result = []
        with self._lock:
            for name, info in self._loaded_plugins.items():
                for ext in info.get(field, []):
                    result.append({
                        'key': ext['key'],
                        'title': ext['title'],
                        'plugin': name,
                        'type': ext['type'],
                    })
        result.sort(key=lambda x: (x['plugin'], x['title']))
        return result

    def _get_ext_handler(self, scope: str, key: str):
        """按 scope+key 找到扩展 handler（未找到返回 None）"""
        field = 'group_extensions' if scope == 'groups' else 'user_extensions'
        with self._lock:
            for info in self._loaded_plugins.values():
                for ext in info.get(field, []):
                    if ext['key'] == key:
                        return ext['handler']
        return None

    def call_ui_extensions(self, scope: str, target_id, keys=None) -> dict:
        """
        对单个群/用户调用扩展 handler（线程池 + 2 秒超时隔离，不卡 Web 线程）

        :param scope: 'groups' | 'users'
        :param target_id: group_id 或 user_id
        :param keys: 要调用的扩展 key 列表（None=全部）
        :return: {key: {type, data, plugin}}；超时/异常返回占位
        """
        global _cards_executor
        field = 'group_extensions' if scope == 'groups' else 'user_extensions'
        exts = []
        with self._lock:
            for name, info in self._loaded_plugins.items():
                for ext in info.get(field, []):
                    if keys is None or ext['key'] in keys:
                        exts.append((name, ext))
        if not exts:
            return {}

        from concurrent.futures import TimeoutError as FutureTimeout
        if _cards_executor is None:
            from concurrent.futures import ThreadPoolExecutor
            _cards_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='zccards')
        out = {}
        futures = {}
        for name, ext in exts:
            handler = ext['handler']
            fut = _cards_executor.submit(handler, target_id)
            futures[fut] = (name, ext)
        for fut, (name, ext) in futures.items():
            try:
                data = fut.result(timeout=2.0)
            except FutureTimeout:
                data = "⏳ 超时"
                fut.cancel()
            except Exception as e:
                logger.error(f"[{name}] UI 扩展[{ext['key']}] 异常: {e}")
                data = "-"
            out[ext['key']] = {
                'type': ext['type'],
                'plugin': name,
                'title': ext['title'],
                'data': data,
            }
        return out

    # ==================================================================
    #  插件 WebUI 内嵌
    # ==================================================================


class PluginWebuiMixin:
    """插件 WebUI 注册与路径解析"""

    def register_webui(self, plugin_name: str, webui_info: dict):
        """注册插件的 WebUI 页面"""
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            if info:
                # 避免重复注册
                webuis = info.setdefault('webuis', [])
                # 查找是否已存在同名 WebUI
                existing = next((w for w in webuis if w['title'] == webui_info['title']), None)
                if not existing:
                    webui_info['plugin_name'] = plugin_name
                    webuis.append(webui_info)

    def override_webui(self, plugin_name: str):
        """
        指定插件接管整个前端（根路由与静态资源从该插件 web/ 目录服务）。
        插件禁用/卸载时自动回退框架默认前端。
        """
        with self._lock:
            if plugin_name in self._loaded_plugins:
                self._override_webui = plugin_name
                return True
            return False

    def clear_override_webui(self, plugin_name: str = None):
        """清除前端接管。plugin_name 为 None 时清除全部；否则仅在匹配时清除。"""
        with self._lock:
            if plugin_name is None or self._override_webui == plugin_name:
                self._override_webui = None

    def get_override_webui(self) -> str:
        """获取当前接管前端的插件名（无则返回 None）"""
        with self._lock:
            return self._override_webui

    def get_override_webui_path(self) -> str:
        """获取接管前端的插件 web/ 目录（无接管或插件已卸载则返回 None）"""
        name = self.get_override_webui()
        if not name:
            return None
        path = self.get_plugin_webui_path(name)
        if not path:
            self.clear_override_webui(name)
            return None
        return path

    def get_plugin_webuis(self) -> list:
        """获取所有已注册的插件 WebUI 列表（按 order 排序）"""
        result = []
        with self._lock:
            for name, info in self._loaded_plugins.items():
                for w in info.get('webuis', []):
                    result.append({
                        'plugin_name': name,
                        'title': w.get('title', ''),
                        'entry': w.get('entry', 'index.html'),
                        'icon': w.get('icon'),
                        'order': w.get('order', 50),
                        'sidebar': bool(w.get('sidebar', False)),
                    })
        result.sort(key=lambda x: x['order'])
        return result

    def get_plugin_webui_path(self, plugin_name: str) -> str:
        """获取插件 web/ 目录的绝对路径"""
        plugin_path = os.path.join(self.plugins_dir, plugin_name, 'web')
        return plugin_path if os.path.isdir(plugin_path) else None

    def get_plugin_webui_entry(self, plugin_name: str, entry: str = 'index.html') -> str:
        """获取插件 WebUI 入口文件的完整路径"""
        web_dir = self.get_plugin_webui_path(plugin_name)
        if not web_dir:
            return None
        entry_path = os.path.join(web_dir, entry)
        return entry_path if os.path.isfile(entry_path) else None

    # ==================================================================
    #  群级插件开关（在路由前检查）
    # ==================================================================


class PluginGroupSettingsMixin:
    """群级插件开关（带进程内缓存）"""

    def is_plugin_enabled_for_group(self, plugin_name: str, group_id: int) -> bool:
        """
        检查插件在指定群是否启用
        默认启用（表中无记录时视为启用）
        优先从缓存读取，30 秒刷新
        """
        if not group_id:
            return True  # 私聊不做限制

        # 刷新缓存
        self._refresh_group_plugin_cache()

        group_settings = self._group_plugin_cache.get(group_id)
        if group_settings is not None and plugin_name in group_settings:
            return group_settings[plugin_name]
        return True  # 无记录 = 启用

    def is_plugin_enabled_for_group_cached(self, plugin_name: str, group_id: int) -> bool:
        """
        检查插件在指定群是否启用（纯内存，不刷新缓存、不查库）
        供消息路由热路径使用：群开关缓存由路由表的后台刷新任务周期性维护
        默认启用（表中无记录时视为启用）
        """
        if not group_id:
            return True  # 私聊不做限制
        group_settings = self._group_plugin_cache.get(group_id)
        if group_settings is not None and plugin_name in group_settings:
            return group_settings[plugin_name]
        return True  # 无记录 = 启用

    def set_group_plugin_enabled(self, plugin_name: str, group_id: int, enabled: bool):
        """设置插件在指定群的启用/禁用状态，并立即更新缓存"""
        try:
            self.db.execute(
                "INSERT INTO group_plugin_settings (group_id, plugin_name, enabled) "
                "VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE enabled = %s",
                (group_id, plugin_name, 1 if enabled else 0, 1 if enabled else 0)
            )
        except Exception as e:
            logger.error(f"设置群级插件状态失败 [{plugin_name}][{group_id}]: {e}")
            raise

        # 立即更新缓存
        self._group_plugin_cache.setdefault(group_id, {})[plugin_name] = enabled

    def remove_group_plugin_setting(self, plugin_name: str, group_id: int):
        """删除群级插件开关记录（恢复默认=启用）"""
        try:
            self.db.execute(
                "DELETE FROM group_plugin_settings WHERE group_id = %s AND plugin_name = %s",
                (group_id, plugin_name)
            )
        except Exception as e:
            logger.error(f"删除群级插件设置失败 [{plugin_name}][{group_id}]: {e}")

        # 更新缓存
        group_settings = self._group_plugin_cache.get(group_id)
        if group_settings and plugin_name in group_settings:
            del group_settings[plugin_name]

    def get_group_plugin_settings(self, group_id: int = None) -> list:
        """获取群级插件设置列表"""
        if group_id:
            try:
                rows = self.db.query(
                    "SELECT plugin_name, enabled, updated_at "
                    "FROM group_plugin_settings WHERE group_id = %s "
                    "ORDER BY plugin_name",
                    (group_id,)
                )
                return rows
            except Exception as e:
                logger.error(f"查询群级插件设置失败 [{group_id}]: {e}")
                return []
        else:
            try:
                rows = self.db.query(
                    "SELECT group_id, plugin_name, enabled, updated_at "
                    "FROM group_plugin_settings ORDER BY group_id, plugin_name"
                )
                return rows
            except Exception as e:
                logger.error(f"查询群级插件设置失败: {e}")
                return []

    def _refresh_group_plugin_cache(self):
        """刷新群级插件开关缓存（最多 30 秒一次）"""
        now = time.time()
        if self._group_plugin_cache and (now - self._group_plugin_cache_time) < self._group_cache_ttl:
            return

        try:
            rows = self.db.query(
                "SELECT group_id, plugin_name, enabled FROM group_plugin_settings"
            )
            cache = {}
            for r in rows:
                gid = r['group_id']
                cache.setdefault(gid, {})[r['plugin_name']] = bool(r['enabled'])
            self._group_plugin_cache = cache
            self._group_plugin_cache_time = now
        except Exception as e:
            logger.warning(f"刷新群级插件缓存失败: {e}")
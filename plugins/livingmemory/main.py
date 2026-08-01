"""
main.py - LivingMemory 插件主文件
负责插件注册、初始化和生命周期管理

适配目标框架: register(ctx) 函数式入口
"""

import asyncio
import os
from typing import Any

import logging
logger = logging.getLogger(__name__)

from plugins.livingmemory.core.base.config_manager import ConfigManager
from plugins.livingmemory.core.command_handler import CommandHandler
from plugins.livingmemory.core.event_handler import EventHandler
from plugins.livingmemory.core.i18n_backend import init as i18n_init
from plugins.livingmemory.core.i18n_backend import t
from plugins.livingmemory.core.managers.backup_manager import BackupManager
from plugins.livingmemory.core.passive_group_capture import get_active_plugin
from plugins.livingmemory.core.passive_group_capture import set_active_plugin
from plugins.livingmemory.core.plugin_initializer import PluginInitializer
from plugins.livingmemory.core.tools import MemoryMemorizeTool, MemorySearchTool

# 模块级变量
_plugin_instance = None  # type: LivingMemoryPlugin | None


def register(ctx):
    """插件注册入口 - 适配目标框架 register(ctx) 函数式"""
    global _plugin_instance
    # 获取插件数据目录
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)

    # 获取配置
    config = ctx.get_config("livingmemory", {})

    _plugin_instance = LivingMemoryPlugin(ctx, config, data_dir)
    logger.info("LivingMemory 插件已注册")

    # ========== 注册命令 ==========
    # /lmem status
    ctx.command("/lmem status", _plugin_instance.handle_status, priority=30,
                description="[Admin] 查看记忆系统状态")
    # /lmem search <query> [k]
    ctx.command("/lmem search", _plugin_instance.handle_search, priority=30,
                description="[Admin] 搜索记忆: /lmem search <query> [k]")
    # /lmem forget <doc_id>
    ctx.command("/lmem forget", _plugin_instance.handle_forget, priority=30,
                description="[Admin] 删除指定记忆: /lmem forget <doc_id>")
    # /lmem rebuild-index
    ctx.command("/lmem rebuild-index", _plugin_instance.handle_rebuild_index, priority=30,
                description="[Admin] 重建索引")
    # /lmem rebuild-graph
    ctx.command("/lmem rebuild-graph", _plugin_instance.handle_rebuild_graph, priority=30,
                description="[Admin] 重建图记忆索引")
    # /lmem webui
    ctx.command("/lmem webui", _plugin_instance.handle_webui, priority=30,
                description="[Admin] 显示 WebUI 访问信息")
    # /lmem summarize
    ctx.command("/lmem summarize", _plugin_instance.handle_summarize, priority=30,
                description="[Admin] 立即触发当前会话记忆总结")
    # /lmem reset
    ctx.command("/lmem reset", _plugin_instance.handle_reset, priority=30,
                description="[Admin] 重置当前会话的长期记忆上下文")
    # /lmem cleanup [mode]
    ctx.command("/lmem cleanup", _plugin_instance.handle_cleanup, priority=30,
                description="[Admin] 清理历史消息中的记忆注入片段")
    # /lmem help
    ctx.command("/lmem help", _plugin_instance.handle_help, priority=30,
                description="[Admin] 显示帮助信息")

    # ========== 注册事件处理 ==========
    ctx.on("message", _plugin_instance.handle_all_group_messages)
    ctx.on("llm_request", _plugin_instance.handle_memory_recall)
    # ctx.on("llm_response", _plugin_instance.handle_memory_reflection)  # 待适配
    # ctx.on("after_message_sent", _plugin_instance.handle_session_reset)  # 待适配

    # ========== 注册定时任务 ==========
    # 定期清理（每6小时）
    ctx.task("0 */6 * * *", _plugin_instance.task_cleanup, description="LivingMemory 定期清理")

    # 注册 LLM 工具
    _register_llm_tools(ctx)


def _register_llm_tools(ctx):
    """注册 LLM 工具到 llm_core"""
    plugin = _plugin_instance
    if not plugin:
        return
    if not plugin.initializer or not plugin.initializer.memory_engine:
        return

    try:
        # 适配目标框架：尝试多种方式获取 llm_core
        llm_core = None
        # 方式1: 通过 ctx.llm_core
        if hasattr(ctx, "llm_core"):
            llm_core = ctx.llm_core
        # 方式2: 通过 sys.modules
        if llm_core is None:
            import sys
            llm_core = sys.modules.get('plugin_llm_core')

        if llm_core is None:
            logger.warning("llm_core 未找到，跳过 LLM 工具注册")
            return

        # 检查 register_tool 方法是否存在
        register_tool = getattr(llm_core, "register_tool", None)
        if not register_tool:
            logger.warning("llm_core 没有 register_tool 方法，跳过 LLM 工具注册")
            return

        config = plugin.config_manager

        if config.get("agent_tools.enable_recall_tool", True):
            tool = MemorySearchTool(
                context=ctx,
                config_manager=config,
                memory_engine=plugin.initializer.memory_engine,
            )
            register_tool(
                plugin_name="livingmemory",
                tool_name="recall_long_term_memory",
                description=tool.description,
                parameters=tool.parameters,
                handler=tool.call,
            )
            logger.info("已注册 recall_long_term_memory 工具")

        if config.get("agent_tools.enable_memorize_tool", False):
            tool = MemoryMemorizeTool(
                context=ctx,
                memory_engine=plugin.initializer.memory_engine,
                memory_processor=plugin.initializer.memory_processor,
            )
            register_tool(
                plugin_name="livingmemory",
                tool_name="memorize_long_term_memory",
                description=tool.description,
                parameters=tool.parameters,
                handler=tool.call,
            )
            logger.info("已注册 memorize_long_term_memory 工具")
    except Exception as e:
        logger.error(f"注册 LLM 工具失败: {e}")


def on_unload():
    """插件卸载时清理"""
    global _plugin_instance
    if _plugin_instance:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_plugin_instance.terminate())
            else:
                loop.run_until_complete(_plugin_instance.terminate())
        except Exception as e:
            logger.error(f"LivingMemory 插件卸载失败: {e}")
    _plugin_instance = None
    logger.info("LivingMemory 插件已卸载")


# ====================================================================
#  LivingMemoryPlugin 主类
# ====================================================================

class LivingMemoryPlugin:
    """LivingMemory 插件主类"""

    def __init__(self, ctx, config: dict[str, Any], data_dir: str):
        """
        初始化插件

        Args:
            ctx: 目标框架上下文
            config: 配置字典
            data_dir: 数据目录路径
        """
        self.ctx = ctx
        self.data_dir = data_dir

        # 版本变更时自动备份数据
        self._backup_manager = BackupManager(data_dir)

        # 初始化配置管理器
        self.config_manager = ConfigManager(config)

        # 初始化后端 i18n
        i18n_init(config.get("bot_language", "zh"))

        # 初始化插件初始化器
        self.initializer = PluginInitializer(ctx, self.config_manager, data_dir)

        # 事件处理器和命令处理器（初始化后创建）
        self.event_handler: EventHandler | None = None
        self.command_handler: CommandHandler | None = None

        # 后台任务跟踪集合
        self._background_tasks: set[asyncio.Task] = set()
        self._component_init_lock = asyncio.Lock()
        self._terminating = False

        set_active_plugin(self)

        # 启动非阻塞的初始化任务
        self._create_tracked_task(self._initialize_plugin())

    def _create_tracked_task(self, coro) -> asyncio.Task:
        """创建并跟踪后台任务"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _initialize_plugin(self):
        """初始化插件"""
        try:
            # 版本变更时自动备份数据
            await self._backup_manager.backup_if_needed_async()

            # 执行初始化
            success = await self.initializer.initialize()

            if success:
                await self._ensure_runtime_components()

        except Exception as e:
            logger.error(f"插件初始化失败: {e}", exc_info=True)

    async def _ensure_runtime_components(self) -> bool:
        """确保运行期组件（事件/命令处理器）已就绪"""
        if self._terminating:
            return False
        if not self.initializer.is_initialized:
            return False

        async with self._component_init_lock:
            if self._terminating:
                return False
            # 检查必要组件是否初始化成功
            if not all(
                [
                    self.initializer.memory_engine,
                    self.initializer.memory_processor,
                    self.initializer.conversation_manager,
                ]
            ):
                logger.error("插件初始化不完整：部分核心组件未能初始化")
                return False

            # 创建事件处理器（幂等）
            if not self.event_handler:
                self.event_handler = EventHandler(
                    context=self.ctx,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,
                    memory_processor=self.initializer.memory_processor,
                    conversation_manager=self.initializer.conversation_manager,
                )

            # 创建命令处理器（幂等）
            if not self.command_handler:
                self.command_handler = CommandHandler(
                    context=self.ctx,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,
                    conversation_manager=self.initializer.conversation_manager,
                    index_validator=self.initializer.index_validator,
                    memory_processor=self.initializer.memory_processor,
                    initialization_status_callback=self._get_initialization_status_message,
                )

        return True

    async def _ensure_plugin_ready(self) -> tuple[bool, str]:
        """确保插件已完成初始化并且运行期组件可用"""
        if not await self.initializer.ensure_initialized():
            return False, self._get_initialization_status_message()

        if not await self._ensure_runtime_components():
            return (
                False,
                t("command.core_not_ready"),
            )

        return True, ""

    def _get_initialization_status_message(self) -> str:
        """获取初始化状态的用户友好消息"""
        if self.initializer.is_initialized:
            return t("init.ready")
        elif self.initializer.is_failed:
            return t(
                "init.failed",
                error=self.initializer.error_message or t("common.unknown_error"),
            )
        else:
            return t(
                "init.in_progress",
                attempts=self.initializer._provider_check_attempts,
            )

    @staticmethod
    def _command_handler_not_ready_message() -> str:
        """命令处理器未就绪时的提示"""
        return t("command.not_ready")

    # ==================== 命令处理器 ====================

    def handle_status(self, event, match):
        """处理 /lmem status 命令"""
        async def _inner():
            ready, message = await self._ensure_plugin_ready()
            if not ready:
                self._reply(event, message)
                return
            if not self.command_handler:
                self._reply(event, self._command_handler_not_ready_message())
                return
            result = await self.command_handler.handle_status(event)
            self._reply(event, result)
        self._create_tracked_task(_inner())

    def handle_search(self, event, match):
        """处理 /lmem search 命令"""
        args = match.group(1) or ""
        parts = args.strip().split(None, 1)
        query = parts[0] if parts else ""
        k = 5
        if len(parts) > 1:
            try:
                k = int(parts[1])
            except ValueError:
                pass
        async def _inner():
            ready, message = await self._ensure_plugin_ready()
            if not ready:
                self._reply(event, message)
                return
            if not self.command_handler:
                self._reply(event, self._command_handler_not_ready_message())
                return
            result = await self.command_handler.handle_search(event, query, k)
            self._reply(event, result)
        self._create_tracked_task(_inner())

    def handle_forget(self, event, match):
        """处理 /lmem forget 命令"""
        args = match.group(1) or ""
        try:
            doc_id = int(args.strip())
        except ValueError:
            self._reply(event, "用法: /lmem forget <记忆ID>")
            return
        async def _inner():
            ready, message = await self._ensure_plugin_ready()
            if not ready:
                self._reply(event, message)
                return
            if not self.command_handler:
                self._reply(event, self._command_handler_not_ready_message())
                return
            result = await self.command_handler.handle_forget(event, doc_id)
            self._reply(event, result)
        self._create_tracked_task(_inner())

    def handle_rebuild_index(self, event, match):
        """处理 /lmem rebuild-index 命令"""
        async def _inner():
            ready, message = await self._ensure_plugin_ready()
            if not ready:
                self._reply(event, message)
                return
            if not self.command_handler:
                self._reply(event, self._command_handler_not_ready_message())
                return
            result = await self.command_handler.handle_rebuild_index(event)
            self._reply(event, result)
        self._create_tracked_task(_inner())

    def handle_rebuild_graph(self, event, match):
        """处理 /lmem rebuild-graph 命令"""
        async def _inner():
            ready, message = await self._ensure_plugin_ready()
            if not ready:
                self._reply(event, message)
                return
            if not self.command_handler:
                self._reply(event, self._command_handler_not_ready_message())
                return
            result = await self.command_handler.handle_rebuild_graph(event)
            self._reply(event, result)
        self._create_tracked_task(_inner())

    def handle_webui(self, event, match):
        """处理 /lmem webui 命令"""
        async def _inner():
            ready, message = await self._ensure_plugin_ready()
            if not ready:
                self._reply(event, message)
                return
            if not self.command_handler:
                self._reply(event, self._command_handler_not_ready_message())
                return
            result = await self.command_handler.handle_webui(event)
            self._reply(event, result)
        self._create_tracked_task(_inner())

    def handle_summarize(self, event, match):
        """处理 /lmem summarize 命令"""
        async def _inner():
            ready, message = await self._ensure_plugin_ready()
            if not ready:
                self._reply(event, message)
                return
            if not self.command_handler:
                self._reply(event, self._command_handler_not_ready_message())
                return
            result = await self.command_handler.handle_summarize(event)
            self._reply(event, result)
        self._create_tracked_task(_inner())

    def handle_reset(self, event, match):
        """处理 /lmem reset 命令"""
        async def _inner():
            ready, message = await self._ensure_plugin_ready()
            if not ready:
                self._reply(event, message)
                return
            if not self.command_handler:
                self._reply(event, self._command_handler_not_ready_message())
                return
            result = await self.command_handler.handle_reset(event)
            self._reply(event, result)
        self._create_tracked_task(_inner())

    def handle_cleanup(self, event, match):
        """处理 /lmem cleanup 命令"""
        args = match.group(1) or ""
        mode = args.strip().lower() or "preview"
        dry_run = mode != "exec"
        async def _inner():
            ready, message = await self._ensure_plugin_ready()
            if not ready:
                self._reply(event, message)
                return
            if not self.command_handler:
                self._reply(event, self._command_handler_not_ready_message())
                return
            result = await self.command_handler.handle_cleanup(event, dry_run=dry_run)
            self._reply(event, result)
        self._create_tracked_task(_inner())

    def handle_help(self, event, match):
        """处理 /lmem help 命令"""
        async def _inner():
            ready, message = await self._ensure_plugin_ready()
            if not ready:
                self._reply(event, message)
                return
            if not self.command_handler:
                self._reply(event, self._command_handler_not_ready_message())
                return
            result = await self.command_handler.handle_help(event)
            self._reply(event, result)
        self._create_tracked_task(_inner())

    # ==================== 事件处理器 ====================

    def handle_all_group_messages(self, event):
        """处理群聊消息捕获"""
        if self._terminating or not self.initializer.is_initialized:
            return
        self._create_tracked_task(self._run_passive_group_capture(event))

    async def _run_passive_group_capture(self, event) -> None:
        try:
            if not await self._ensure_runtime_components():
                logger.debug("插件组件未就绪，跳过被动群聊消息捕获")
                return
            if not self.event_handler:
                return
            await self.event_handler.handle_all_group_messages(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"被动群聊消息捕获失败: {e}", exc_info=True)

    def handle_memory_recall(self, event):
        """处理 LLM 请求前的记忆召回"""
        async def _inner():
            ready, _ = await self._ensure_plugin_ready()
            if not ready:
                logger.debug("插件未完成初始化，跳过记忆召回")
                return
            if not self.event_handler:
                return
            await self.event_handler.handle_memory_recall(event, None)
        self._create_tracked_task(_inner())

    # ==================== 定时任务 ====================

    async def task_cleanup(self):
        """定期清理任务"""
        if not self.initializer.is_initialized:
            return
        try:
            if self.initializer.decay_scheduler:
                await self.initializer.decay_scheduler.run_maintenance()
            logger.info("LivingMemory 定期清理完成")
        except Exception as e:
            logger.error(f"定期清理失败: {e}")

    # ==================== 生命周期管理 ====================

    async def terminate(self):
        """Cleanup logic when plugin stops"""
        logger.info("LivingMemory 插件正在停止...")
        self._terminating = True
        if get_active_plugin() is self:
            set_active_plugin(None)

        # 取消所有后台任务
        if self._background_tasks:
            logger.info(f"正在取消 {len(self._background_tasks)} 个后台任务...")
            for task in self._background_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # 停止初始化后台任务
        await self.initializer.stop_background_tasks()

        # 通知EventHandler停止
        if self.event_handler:
            await self.event_handler.shutdown()

        # 停止衰减调度器
        await self.initializer.stop_scheduler()

        # 关闭 ConversationManager
        if (
            self.initializer.conversation_manager
            and self.initializer.conversation_manager.store
        ):
            await self.initializer.conversation_manager.store.close()
            logger.info("ConversationManager 已关闭")

        # 关闭 MemoryEngine
        if self.initializer.memory_engine:
            await self.initializer.memory_engine.close()
            logger.info("MemoryEngine 已关闭")

        # 关闭 FaissVecDB
        if self.initializer.db:
            await self.initializer.db.close()
            logger.info("FaissVecDB 已关闭")

        logger.info("LivingMemory 插件已成功停止。")

    # ==================== 工具函数 ====================

    def _reply(self, event, text):
        """统一回复封装"""
        if not text:
            return
        self.ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id if event.is_group else None,
            message=str(text),
        )
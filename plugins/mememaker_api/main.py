"""
Meme Maker API 插件 - 功能完善的表情包与图片工具插件
从 AstrBot 迁移至 zgric_onebot11 新语法

原插件架构使用 Mixin 多继承，新框架使用 register(ctx) 函数入口 + 模块级共享状态。
所有核心组件（api_client, meme_manager, recorder）通过 _shared.py 共享。

命令:
  -表情列表              生成动态表情包列表图
  -表情详情 <关键词>      查看表情详情
  -表情搜索 <关键词>      搜索表情
  -刷新表情              强制刷新表情包列表（管理员）
  -禁用表情 <关键词>      在当前群禁用表情（管理员）
  -启用表情 <关键词>      在当前群启用表情（管理员）
  -管理列表              查看表情管理规则列表（管理员）
  -全局禁用表情 <关键词>  全局禁用表情（超管）
  -全局启用表情 <关键词>  全局启用表情（超管）
  -群管理员 [添加/删除/查看] [@某人]  管理插件群管理员（管理员）
  -表情调用统计 [参数]    查看表情调用统计
  -随机表情 [参数]        随机生成一个表情
  关键词触发             直接输入表情关键词触发生成

配置项（_conf_schema.json）：
  meme_generator_base_url  meme-generator 服务地址
  command_prefix           命令前缀，默认 "-"
  timeout                  API 超时时间
  fuzzy_match              是否启用模糊匹配
  use_sender_when_no_image 无图片时使用发送者头像
  interactive_settings     交互式会话配置
  multi_image_options      多图发送策略配置
"""

import asyncio
import os
import re
import logging
from typing import Dict, List, Set, Optional, Any

from plugins.mememaker_api.api_client import APIClient
from plugins.mememaker_api.manager import MemeManager
from plugins.mememaker_api.recorder import StatsRecorder
from plugins.mememaker_api.core.permission import PermissionManager
from . import _shared
from plugins.mememaker_api._shared import _config

# 导入 handlers
from plugins.mememaker_api.handlers.help import handle_meme_list
from plugins.mememaker_api.handlers.info import handle_meme_info
from plugins.mememaker_api.handlers.search import handle_meme_search, handle_search_pagination, _search_sessions
from plugins.mememaker_api.handlers.management import (
    handle_group_admin_manager, handle_refresh_memes,
    handle_disable_meme, handle_enable_meme, handle_manager_list,
    handle_global_disable_meme, handle_global_enable_meme,
)
from plugins.mememaker_api.handlers.statistics import handle_meme_stats
from plugins.mememaker_api.handlers.tools import handle_image_tool
from plugins.mememaker_api.handlers.generation import (
    handle_random_meme, meme_generate_handler, handle_shortcut,
    active_sessions, UserInGroupSessionFilter,
)

logger = logging.getLogger(__name__)

__plugin_meta__ = {
    "name": "Meme Maker API",
    "version": "5.1.0",
    "author": "custom",
    "desc": "功能完善的表情包与图片工具插件",
    "priority": 30,
}

# 模块级变量
_processing_events: Set[str] = set()


def register(ctx):
    """插件注册入口"""
    # ========== 1. 加载配置 ==========
    api_url = ctx.get_config("meme_generator_base_url", "http://127.0.0.1:2233/")
    if not api_url.endswith('/'):
        api_url += '/'

    # 交互式配置
    interactive_settings = ctx.get_config("interactive_settings", {})
    if isinstance(interactive_settings, dict):
        recall_config = interactive_settings.get("recall", {})
        reprompt_config = interactive_settings.get("smart_reprompt", {})
    else:
        recall_config = {}
        reprompt_config = {}

    # 多图配置
    multi_image_options = ctx.get_config("multi_image_options", {})
    if not isinstance(multi_image_options, dict):
        multi_image_options = {}

    # 构建配置字典
    config = {
        "meme_generator_base_url": api_url,
        "command_prefix": ctx.get_config("command_prefix", "-"),
        "timeout": ctx.get_config("timeout", 20),
        "fuzzy_match": ctx.get_config("fuzzy_match", True),
        "use_sender_when_no_image": ctx.get_config("use_sender_when_no_image", True),
        "bot_display_name": ctx.get_config("bot_display_name", "Meme Bot"),
        "label_new_days": ctx.get_config("label_new_days", 7),
        "label_hot_days": ctx.get_config("label_hot_days", 30),
        "label_hot_threshold": ctx.get_config("label_hot_threshold", 20),
        "interactive_enabled": interactive_settings.get("enabled", True) if isinstance(interactive_settings, dict) else True,
        "session_timeout": interactive_settings.get("timeout", 60) if isinstance(interactive_settings, dict) else 60,
        "recall_enabled": recall_config.get("enabled", False) if isinstance(recall_config, dict) else False,
        "reprompt_enabled": reprompt_config.get("enabled", True) if isinstance(reprompt_config, dict) else True,
        "reprompt_threshold": reprompt_config.get("threshold", 2) if isinstance(reprompt_config, dict) else 2,
        "direct_send_threshold": multi_image_options.get("direct_send_threshold", 3),
        "send_forward_msg": multi_image_options.get("send_forward_msg", True),
        "send_as_zip_enabled": multi_image_options.get("send_as_zip_enabled", True),
        "zip_threshold": multi_image_options.get("zip_threshold", 20),
        "zip_use_base64": multi_image_options.get("zip_use_base64", False),
        "perms": ctx.get_config("perms", {}),
    }

    # 更新共享状态
    _config.clear()
    _config.update(config)
    _shared._ctx = ctx

    # ========== 2. 初始化核心组件 ==========
    api_client = APIClient(api_url, config["timeout"])
    _shared._api_client = api_client

    meme_manager = MemeManager()
    _shared._meme_manager = meme_manager

    # 数据库路径
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(plugin_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "usage_stats.db")
    recorder = StatsRecorder(db_path)
    _shared._recorder = recorder

    # 获取超管列表
    superusers = []
    try:
        rows = ctx.db_query("SELECT user_id FROM users WHERE role = 'super'")
        superusers = [str(r['user_id']) for r in rows]
    except Exception as e:
        ctx.log(f"获取超管列表失败: {e}", level="warning")

    # 初始化权限管理器
    PermissionManager.get_instance(
        superusers=superusers,
        perms=config.get("perms", {}),
        recorder_instance=recorder,
        ctx=ctx,
    )

    # ========== 3. 注册命令 ==========
    # 普通命令
    ctx.command(
        "-表情列表", handle_meme_list, priority=50,
        description="生成动态表情包列表图",
    )
    ctx.command(
        "-表情详情", handle_meme_info, priority=50,
        alias="-表情详细", description="查看表情详情：-表情详情 <关键词>",
    )
    ctx.command(
        "-表情搜索", handle_meme_search, priority=50,
        description="搜索表情：-表情搜索 <关键词>",
    )
    ctx.command(
        "-表情调用统计", handle_meme_stats, priority=50,
        description="查看表情调用统计：-表情调用统计 [我的] [全局] [时间] [表情名]",
    )
    ctx.command(
        "-随机表情", handle_random_meme, priority=50,
        description="随机生成一个表情：-随机表情 [文字]",
    )

    # 管理员命令
    ctx.command(
        "-刷新表情", handle_refresh_memes, priority=50,
        require_admin=True, description="强制刷新表情包列表",
    )
    ctx.command(
        "-禁用表情", handle_disable_meme, priority=50,
        require_admin=True, description="在当前群禁用表情：-禁用表情 <关键词>",
    )
    ctx.command(
        "-启用表情", handle_enable_meme, priority=50,
        require_admin=True, description="在当前群启用表情：-启用表情 <关键词>",
    )
    ctx.command(
        "-管理列表", handle_manager_list, priority=50,
        require_admin=True, description="查看表情管理规则列表",
    )
    ctx.command(
        "-群管理员", handle_group_admin_manager, priority=50,
        require_admin=True, description="管理插件群管理员：-群管理员 [添加/删除/查看] [@某人]",
    )

    # 超管命令
    ctx.command(
        "-全局禁用表情", handle_global_disable_meme, priority=50,
        require_superuser=True, description="全局禁用表情：-全局禁用表情 <关键词>",
    )
    ctx.command(
        "-全局启用表情", handle_global_enable_meme, priority=50,
        require_superuser=True, description="全局启用表情：-全局启用表情 <关键词>",
    )

    # Catch-all 通用处理器（优先级最低，最后匹配）
    ctx.command(
        "^", universal_handler, priority=999,
        description="表情包通用处理器（交互式会话、关键词匹配）",
    )

    # ========== 4. 启动后台任务 ==========
    asyncio.create_task(_background_refresh(ctx))

    ctx.log("MemeMaker API 插件已加载完成。")


async def _background_refresh(ctx):
    """后台刷新表情包数据"""
    await asyncio.sleep(1)  # 等待插件完全初始化
    try:
        api_client = _shared._api_client
        meme_manager = _shared._meme_manager
        if api_client and meme_manager:
            success, meme_count, shortcut_count = await meme_manager.refresh_memes(api_client)
            if success:
                ctx.log(f"表情包列表刷新成功！共加载 {meme_count} 个表情和 {shortcut_count} 个快捷指令。")
            else:
                ctx.log("表情包列表刷新失败。", level="warning")
        else:
            ctx.log("核心组件未初始化，跳过后台刷新。", level="warning")
    except Exception as e:
        ctx.log(f"后台刷新表情包失败: {e}", level="error")


def universal_handler(event, match):
    """
    通用消息处理器 - 处理所有消息
    优先级 999，在所有显式命令之后被调用。

    处理流程：
    1. 跳过机器人自己的消息
    2. 检查交互式会话状态（处理所有消息，不限于前缀命令）
    3. 检查搜索分页
    4. 检查是否以命令前缀开头
    5. 检查快捷指令匹配
    6. 检查关键词匹配
    7. 返回 False 继续传递
    """
    # 1. 跳过机器人自己的消息
    if str(event.user_id) == str(event.self_id):
        return False

    session_id = UserInGroupSessionFilter.filter(event)
    message = event.message.strip()

    # 2. 检查交互式会话（需要处理所有消息，不管是否以命令前缀开头）
    if session_id in active_sessions:
        session_state = active_sessions[session_id]
        future = session_state.get("future")
        if future and not future.done():
            future.set_result(event)
            return True  # 事件已被会话处理，停止传播
        # 会话已过期，清理
        active_sessions.pop(session_id, None)
        return False

    # 3. 检查搜索分页
    if handle_search_pagination(event, _shared._ctx):
        return True

    # 4. 检查是否以命令前缀开头
    prefix = _config.get("command_prefix", "-")
    if not message.startswith(prefix):
        return False  # 不是命令，不处理

    cleaned_text = message[len(prefix):].strip()
    if not cleaned_text:
        return False

    # 5. 检查快捷指令匹配（同步函数中无法 await，直接创建任务，禁用检查在任务内部处理）
    meme_manager = _shared._meme_manager
    if meme_manager and meme_manager.shortcuts:
        for sc_data in meme_manager.shortcuts:
            if match_obj := sc_data["pattern"].fullmatch(cleaned_text):
                asyncio.create_task(
                    handle_shortcut(event, sc_data["meme"], sc_data["shortcut"], match_obj)
                )
                return True

    # 6. 检查关键词匹配
    if meme_manager:
        keyword = meme_manager.find_keyword_in_text(
            cleaned_text, _config.get("fuzzy_match", True)
        )
        if keyword:
            meme_info = meme_manager.find_meme_by_keyword(keyword)
            if meme_info:
                asyncio.create_task(
                    meme_generate_handler(event, meme_info, cleaned_text)
                )
                return True

    return False


async def on_unload():
    """插件卸载时释放资源"""
    if _shared._api_client:
        await _shared._api_client.close()
    if _shared._recorder:
        await _shared._recorder.close()
    logger.info("MemeMakerApiPlugin 已卸载，所有连接已关闭。")
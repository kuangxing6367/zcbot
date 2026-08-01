"""
管理处理器 - 表情管理相关指令
从 AstrBot 迁移至 zgric_onebot11，适配框架 (event, match) 调用规范
"""
import logging
from typing import Dict, List

from plugins.mememaker_api._shared import _api_client, _meme_manager, _recorder, _config, _ctx
from plugins.mememaker_api.core.utils import get_ats
from plugins.mememaker_api.core.permission import perm_required

logger = logging.getLogger(__name__)


@perm_required()
async def handle_group_admin_manager(event, match):
    """处理群管理员管理"""
    ctx = _ctx
    arg_text = (match.group(1) or '').strip() if match else ''
    try:
        args = arg_text.split()
        if not args or args[0] not in ["添加", "删除", "查看"]:
            _send(event, ctx, "用法: -群管理员 [添加/删除/查看] [@某人或QQ号] [群号(可选)]")
            return

        sub_command = args[0]

        if sub_command == "查看":
            target_group_id = args[1] if len(args) > 1 else str(event.group_id) if event.group_id else None
            if not target_group_id:
                _send(event, ctx, "请指定群号或在群内使用此指令。")
                return
            admins = await _recorder.list_group_admins(target_group_id)
            if not admins:
                _send(event, ctx, f"群 {target_group_id} 尚无自定义插件管理员。")
            else:
                _send(event, ctx, f"群 {target_group_id} 的插件管理员有：\n" + "\n".join(admins))
            return

        target_user_id = None
        ats = get_ats(event.message or '')
        if ats:
            target_user_id = ats[0]
        if not target_user_id:
            for arg in args[1:]:
                if arg.isdigit():
                    target_user_id = arg
                    break
        if not target_user_id:
            _send(event, ctx, "请 @要操作的用户 或提供其 QQ 号。")
            return

        target_group_id = str(event.group_id) if event.group_id else None
        for arg in args[1:]:
            if arg.isdigit() and arg != target_user_id:
                target_group_id = arg
                break
        if not target_group_id:
            _send(event, ctx, "请在群内使用此指令，或在最后提供群号。")
            return

        if sub_command == "添加":
            await _recorder.add_group_admin(target_group_id, target_user_id)
            _send(event, ctx, f" 已将用户 {target_user_id} 添加为群 {target_group_id} 的插件管理员。")
        elif sub_command == "删除":
            await _recorder.remove_group_admin(target_group_id, target_user_id)
            _send(event, ctx, f" 已移除用户 {target_user_id} 在群 {target_group_id} 的插件管理员身份。")

    except Exception as e:
        logger.error(f"管理插件管理员时出错: {e}", exc_info=True)
        _send(event, ctx, "操作失败，请检查后台日志。")


@perm_required()
async def handle_refresh_memes(event, match):
    """处理刷新表情"""
    ctx = _ctx
    try:
        _send(event, ctx, "正在强制刷新表情包列表...")
        success, meme_count, shortcut_count = await _meme_manager.refresh_memes(_api_client)
        if success:
            _send(event, ctx, f"表情包列表刷新成功！共加载 {meme_count} 个表情和 {shortcut_count} 个快捷指令。")
        else:
            _send(event, ctx, "刷新失败，请查看后台日志。")
    except Exception as e:
        logger.error(f"刷新表情失败: {e}", exc_info=True)
        _send(event, ctx, "刷新失败，请查看后台日志。")


@perm_required()
async def handle_disable_meme(event, match):
    """处理禁用表情"""
    ctx = _ctx
    keyword = (match.group(1) or '').strip() if match else ''
    try:
        if not event.group_id:
            _send(event, ctx, " 此指令不能在私聊中使用，请使用 `-全局禁用表情`。")
            return
        if not keyword:
            _send(event, ctx, "请输入要禁用的表情关键词。")
            return
        meme_info = _meme_manager.find_meme_by_keyword(keyword)
        if not meme_info:
            _send(event, ctx, f"找不到表情"{keyword}"。")
            return

        await _recorder.set_meme_mode(meme_info.key, 'group', str(event.group_id), 'black')
        _send(event, ctx, f" 已在当前群禁用表情"{meme_info.key}"。")
    except Exception as e:
        logger.error(f"分群禁用失败: {e}", exc_info=True)
        _send(event, ctx, "操作失败...")


@perm_required()
async def handle_enable_meme(event, match):
    """处理启用表情"""
    ctx = _ctx
    keyword = (match.group(1) or '').strip() if match else ''
    try:
        if not event.group_id:
            _send(event, ctx, " 此指令不能在私聊中使用。")
            return
        if not keyword:
            _send(event, ctx, "请输入要启用的表情关键词。")
            return

        meme_info = _meme_manager.find_meme_by_keyword(keyword)
        key_to_enable = meme_info.key if meme_info else keyword

        is_white_mode = await _recorder.is_meme_whitelisted(key_to_enable)
        if is_white_mode:
            await _recorder.set_meme_mode(key_to_enable, 'group', str(event.group_id), 'white')
        else:
            await _recorder.remove_meme_rule(key_to_enable, 'group', str(event.group_id))

        _send(event, ctx, f" 已在当前群启用/解除限制表情"{key_to_enable}"。")
    except Exception as e:
        logger.error(f"分群启用失败: {e}", exc_info=True)
        _send(event, ctx, "操作失败...")


@perm_required()
async def handle_manager_list(event, match):
    """处理管理列表查看"""
    ctx = _ctx
    try:
        if not event.group_id:
            _send(event, ctx, "请在群内使用此指令。")
            return

        rules = await _recorder.get_manager_list(str(event.group_id))
        if not rules:
            _send(event, ctx, "当前没有任何全局或本群表情管理规则。")
            return

        rule_texts = [
            f"• {key} ({'全局' if scope == 'global' else '本群'} {'白名单(默认禁用)' if mode == 'white' else '黑名单(禁用)'})"
            for key, scope, mode in rules
        ]
        _send(event, ctx, "--- 表情管理规则 ---\n" + "\n".join(rule_texts))
    except Exception as e:
        logger.error(f"查看管理列表失败: {e}", exc_info=True)
        _send(event, ctx, "操作失败...")


@perm_required()
async def handle_global_disable_meme(event, match):
    """处理全局禁用表情"""
    ctx = _ctx
    arg_text = (match.group(1) or '').strip() if match else ''
    try:
        if not arg_text:
            _send(event, ctx, "请输入要设为白名单模式的表情关键词。")
            return
        meme_info = _meme_manager.find_meme_by_keyword(arg_text)
        if not meme_info:
            _send(event, ctx, f"找不到表情"{arg_text}"。")
            return

        await _recorder.set_meme_mode(meme_info.key, 'global', '*', 'white')
        _send(event, ctx, f" 已将表情"{meme_info.key}"设为全局白名单模式（默认禁用）。")
    except Exception as e:
        logger.error(f"全局禁用失败: {e}", exc_info=True)
        _send(event, ctx, "操作失败...")


@perm_required()
async def handle_global_enable_meme(event, match):
    """处理全局启用表情"""
    ctx = _ctx
    arg_text = (match.group(1) or '').strip() if match else ''
    try:
        if not arg_text:
            _send(event, ctx, "请输入要恢复为黑名单模式的表情关键词。")
            return
        meme_info = _meme_manager.find_meme_by_keyword(arg_text)
        key_to_manage = meme_info.key if meme_info else arg_text
        await _recorder.remove_meme_rule(key_to_manage, 'global', '*')
        _send(event, ctx, f" 已将表情"{key_to_manage}"恢复为全局黑名单模式（默认启用）。")
    except Exception as e:
        logger.error(f"全局启用失败: {e}", exc_info=True)
        _send(event, ctx, "操作失败...")


def _send(event, ctx, text: str):
    """统一发送消息"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )
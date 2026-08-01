"""
表情生成处理器 - 核心表情生成逻辑，包括交互式会话管理
从 AstrBot 迁移至 zgric_onebot11，适配新框架 API
"""
import re
import io
import time
import shlex
import base64
import random
import asyncio
import zipfile
import filetype
import tempfile
import os
import logging
from typing import Dict, Any, List, Optional, Union, AsyncGenerator
from datetime import datetime

from argparse import ArgumentError

from plugins.mememaker_api.models import MemeInfo, MemeParams
from plugins.mememaker_api.exceptions import ArgParseError, APIError, NoExitArgumentParser
from plugins.mememaker_api._shared import _api_client, _meme_manager, _recorder, _ctx, _config
from plugins.mememaker_api.core.utils import image_to_base64_cq

logger = logging.getLogger(__name__)

# ========== 会话存储（模块级字典）==========
active_sessions: Dict[str, Dict[str, Any]] = {}
recall_message_ids: Dict[str, List[str]] = {}


class UserInGroupSessionFilter:
    """
    用户会话隔离过滤器
    在群聊中使用 "群号-用户ID" 作为唯一标识，私聊中使用 "用户ID"。
    """
    @staticmethod
    def filter(event) -> str:
        if event.group_id:
            return f"{event.group_id}-{event.user_id}"
        return str(event.user_id)


# ========== 图片提取辅助函数 ==========

async def _get_images_from_message(event) -> List[bytes]:
    """从事件中提取所有图片的字节数据，使用 event.segments 解析"""
    image_bytes_list: List[bytes] = []

    for seg in event.segments:
        if seg.get('type') == 'image':
            data = seg.get('data', {})
            img_bytes = None

            # 1. 尝试 base64 编码
            img_file = data.get('file', '') or ''
            if img_file.startswith('base64://'):
                try:
                    img_bytes = base64.b64decode(img_file[len('base64://'):])
                except Exception:
                    pass

            # 2. 尝试 URL 下载
            if img_bytes is None:
                img_url = data.get('url', '')
                if img_url:
                    img_bytes = await _api_client._download_image(img_url)

            if img_bytes:
                image_bytes_list.append(img_bytes)

    # 处理回复消息中的图片（通过回复链解析）
    for seg in event.segments:
        if seg.get('type') == 'reply':
            # 回复消息本身不包含图片数据，跳过
            # 在 OneBot 实现中，回复消息的图片需要额外处理
            pass

    return image_bytes_list


async def _get_avatar(user_id: str) -> Optional[bytes]:
    """获取用户头像"""
    if not user_id.isdigit():
        return None
    return await _api_client._download_image(
        f"http://q4.qlogo.cn/g?b=qq&nk={user_id}&s=640"
    )


# ========== 发送结果辅助函数 ==========

async def _send_results(event, result_obj: Union[bytes, List[bytes]]):
    """
    发送图片结果，支持多图策略。
    简化版：移除转发消息、文件上传等依赖 bot 原生 API 的功能。
    """
    ctx = _ctx
    if not result_obj:
        _send(event, ctx, "图片处理失败，未收到结果。")
        return

    image_list = [result_obj] if isinstance(result_obj, bytes) else result_obj
    if not image_list:
        _send(event, ctx, "图片处理失败，未收到结果。")
        return

    direct_send_threshold = _config.get("direct_send_threshold", 3)
    send_as_zip_enabled = _config.get("send_as_zip_enabled", True)
    zip_threshold = _config.get("zip_threshold", 20)

    if len(image_list) <= direct_send_threshold:
        # 直接发送
        cq_parts = [image_to_base64_cq(img_bytes) for img_bytes in image_list]
        _send(event, ctx, "\n".join(cq_parts))
        return

    elif send_as_zip_enabled and len(image_list) > zip_threshold:
        # 打包为 ZIP 发送
        _send(event, ctx, f"图片过多（{len(image_list)}张），将打包为 .zip 文件发送...")
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, img_bytes in enumerate(image_list):
                ext = filetype.guess_extension(img_bytes) or "png"
                zf.writestr(f"image_{i+1}.{ext}", img_bytes)
        zip_buffer.seek(0)

        filename = f"meme_images_{int(time.time())}.zip"
        tmp_path = os.path.join(tempfile.gettempdir(), filename)
        try:
            with open(tmp_path, "wb") as f:
                f.write(zip_buffer.getvalue())
            path_str = tmp_path.replace("\\", "/")
            cq = f"[CQ:file,file=file:///{path_str}]"
            _send(event, ctx, cq)
        except Exception as e:
            logger.error(f"发送zip文件失败: {e}", exc_info=True)
            _send(event, ctx, "发送zip文件失败，请检查后台日志。")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return

    else:
        # 逐条发送
        _send(event, ctx, f"处理完成，共生成 {len(image_list)} 张图片：")
        for img_bytes in image_list:
            _send(event, ctx, image_to_base64_cq(img_bytes))
            await asyncio.sleep(0.5)


async def _send_and_record(event, text: str):
    """发送文本提示（简化版，移除撤回功能）"""
    ctx = _ctx
    try:
        _send(event, ctx, text)
    except Exception as e:
        logger.error(f"_send_and_record 失败: {e}", exc_info=True)


async def _cleanup_prompts(event):
    """清理会话提示（简化版，移除撤回功能）"""
    session_id = UserInGroupSessionFilter.filter(event)
    recall_message_ids.pop(session_id, None)


# ========== 核心生成逻辑 ==========

async def build_meme_payload(event, meme_info: MemeInfo, text: str) -> tuple:
    """
    构建表情生成所需的 payload。
    提取消息中的图片、解析命令行参数。
    """
    image_bytes_list: List[bytes] = []

    initial_images = await _get_images_from_message(event)
    image_bytes_list.extend(initial_images)

    # 如果设置了 use_sender_when_no_image 且图片不足，使用发送者头像
    if _config.get("use_sender_when_no_image", True) and len(image_bytes_list) < meme_info.params.min_images:
        if b := await _get_avatar(str(event.user_id)):
            image_bytes_list.insert(0, b)

    text_to_parse = text.strip()

    # 从文本中去除关键词
    keyword_in_text = _meme_manager.find_keyword_in_text(text_to_parse, _config.get("fuzzy_match", True))
    if keyword_in_text:
        text_to_parse = text_to_parse.replace(keyword_in_text, "", 1).strip()

    # 解析参数
    try:
        args = shlex.split(text_to_parse)
    except ValueError:
        args = text_to_parse.split()

    parser = NoExitArgumentParser(prog=f"{_config.get('command_prefix', '-')}{meme_info.key}", add_help=False)
    type_mapping = {"integer": int, "float": float, "string": str}
    for opt in meme_info.params.options:
        flags, pf = [], opt.parser_flags
        if pf.get("long", True):
            flags.append(f"--{opt.name}")
        if pf.get("short", False) and opt.name:
            flags.append(f"-{opt.name[0]}")
        for alias in pf.get("long_aliases", []):
            flags.append(f"--{alias}")
        for alias in pf.get("short_aliases", []):
            flags.append(f"--{alias}")
            if len(alias) == 1:
                flags.append(f"-{alias}")
        if not (unique_flags := list(dict.fromkeys(flags))):
            continue
        if opt.type == "boolean":
            parser.add_argument(*unique_flags, action="store_true", default=opt.default)
        else:
            parser.add_argument(*unique_flags, type=type_mapping.get(opt.type, str), default=opt.default)

    try:
        parsed_args, unknown_args = parser.parse_known_args(args)
        options_payload = {k: v for k, v in vars(parsed_args).items() if v is not None}
        texts = unknown_args
    except (ArgumentError, ValueError, ArgParseError) as e:
        raise ArgParseError(f"参数解析或类型转换错误: {e}")

    return texts, image_bytes_list, options_payload


async def _session_worker(event, session_id: str, meme_info: MemeInfo):
    """
    后台会话工人。
    负责交互式参数收集、API 调用、结果发送和会话清理。
    """
    try:
        session_state = active_sessions.get(session_id)
        if not session_state:
            logger.warning(f"后台工人启动，但未找到会话 {session_id} 的状态。")
            return

        p = session_state["params"]

        async def _final_generate_and_send():
            """最终的生成和发送步骤"""
            try:
                state = active_sessions.get(session_id, {})
                final_texts, final_images = state.get("texts", []), state.get("images", [])
                final_texts = final_texts[:p.max_texts]
                final_images = final_images[:p.max_images]

                tasks = [_api_client.upload_image(b) for b in final_images]
                image_ids = await asyncio.gather(*tasks)
                image_payload = [{"id": img_id, "name": f"img{i}"} for i, img_id in enumerate(image_ids)]
                final_payload = {"texts": final_texts, "images": image_payload, "options": state.get("options", {})}

                active_sessions[session_id]["status"] = "generating"

                image_data = await _api_client.generate_meme(meme_info.key, final_payload)
                await _recorder.record_usage(meme_info.key, str(event.user_id), str(event.group_id) if event.group_id else None)
                await _send_results(event, image_data)
            except Exception as e:
                logger.error(f"最终生成步骤出错: {e}", exc_info=True)
                await _send_and_record(event, "制作表情的最后一步失败了，呜呜...")

        # 交互式等待循环
        if not (len(session_state["texts"]) >= p.min_texts and len(session_state["images"]) >= p.min_images):
            if not _config.get("interactive_enabled", True):
                prompts = []
                if len(session_state["texts"]) < p.min_texts:
                    prompts.append(f"需要 {p.min_texts - len(session_state['texts'])} 段文字")
                if len(session_state["images"]) < p.min_images:
                    prompts.append(f"需要 {p.min_images - len(session_state['images'])} 张图片")
                await _send_and_record(event, f"参数不足：{'、'.join(prompts)}。（提示：可在后台配置中开启交互功能）")
                return

            # 发送初始提示
            prompts = []
            if len(session_state["texts"]) < p.min_texts:
                prompts.append(f"需要 {p.min_texts - len(session_state['texts'])} 段文字")
            if len(session_state["images"]) < p.min_images:
                prompts.append(f"需要 {p.min_images - len(session_state['images'])} 张图片")
            session_timeout = _config.get("session_timeout", 60)
            prompt_text = f"参数不足，请继续发送{'、'.join(prompts)}。{session_timeout}秒内无操作将自动取消。"
            cancel_hint = f"\n（可发送\"{_config.get('command_prefix', '-')}取消\"来随时终止）"
            await _send_and_record(event, prompt_text + cancel_hint)

            # 进入循环等待
            while not (len(session_state["texts"]) >= p.min_texts and len(session_state["images"]) >= p.min_images):
                future = asyncio.Future()
                active_sessions[session_id]["future"] = future
                try:
                    next_event = await asyncio.wait_for(future, timeout=session_timeout)
                except asyncio.TimeoutError:
                    _send(event, _ctx, "输入超时或交互时间过长，制作已取消")
                    return

                next_message = (next_event.message or "").strip()
                prefix = _config.get("command_prefix", "-")
                if next_message == f"{prefix}取消":
                    _send(event, _ctx, "操作已取消。")
                    return

                # 智能重提示和数据收集
                needs_text = len(session_state["texts"]) < p.min_texts
                needs_image = len(session_state["images"]) < p.min_images
                provided_text = next_message
                provided_images = await _get_images_from_message(next_event)
                is_valid_and_needed_input = (needs_text and provided_text) or (needs_image and provided_images)

                if is_valid_and_needed_input:
                    session_state["invalid_input_count"] = 0
                    if needs_text and provided_text:
                        session_state["texts"].extend(provided_text.split())
                    if needs_image and provided_images:
                        session_state["images"].extend(provided_images)
                    if len(session_state["texts"]) >= p.min_texts and len(session_state["images"]) >= p.min_images:
                        await _send_and_record(next_event, "参数已集齐，开始制作...")
                        break
                    else:
                        prompts = []
                        if len(session_state["texts"]) < p.min_texts:
                            prompts.append(f"还差 {p.min_texts - len(session_state['texts'])} 段文字")
                        if len(session_state["images"]) < p.min_images:
                            prompts.append(f"还差 {p.min_images - len(session_state['images'])} 张图片")
                        await _send_and_record(next_event, f"{'、'.join(prompts)}。")
                else:
                    session_state["invalid_input_count"] += 1
                    reprompt_enabled = _config.get("reprompt_enabled", True)
                    reprompt_threshold = _config.get("reprompt_threshold", 2)
                    if reprompt_enabled and session_state["invalid_input_count"] >= reprompt_threshold:
                        smart_prompt = ""
                        if not needs_text and provided_text:
                            smart_prompt = "文字已经够啦，请发送我需要的图片哦~"
                        elif not needs_image and provided_images:
                            smart_prompt = "图片已经够啦，我现在需要的是文字~"
                        if smart_prompt:
                            await _send_and_record(next_event, smart_prompt)
                            session_state["invalid_input_count"] = 0

        # 执行最终生成
        await _final_generate_and_send()

    except Exception as e:
        logger.error(f"会话工人任务 '{meme_info.key}' 失败: {e}", exc_info=True)
        await _send_and_record(event, "表情制作失败了，呜呜...")
    finally:
        await _cleanup_prompts(event)
        active_sessions.pop(session_id, None)
        logger.debug(f"后台工人任务结束，会话 {session_id} 已清理。")


async def meme_generate_handler(event, meme_info: MemeInfo, text: str,
                                 initial_options: Dict = None, initial_texts: List[str] = None):
    """
    表情生成处理器 - 启动器。
    检查状态锁 -> 创建会话状态 -> 启动后台工人 -> 立即返回。
    """
    if initial_options is None:
        initial_options = {}
    if initial_texts is None:
        initial_texts = []

    session_id = UserInGroupSessionFilter.filter(event)

    if session_id in active_sessions:
        await _send_and_record(event, "您上一个表情正在制作中，请稍等片刻~")
        return

    try:
        shortcut_texts = initial_texts
        shortcut_options = initial_options
        parsed_texts, initial_images, parsed_options = await build_meme_payload(event, meme_info, text)
        final_texts = shortcut_texts + parsed_texts
        final_options = shortcut_options.copy()
        final_options.update(parsed_options)

        p = meme_info.params
        if len(final_texts) == 0 and p.default_texts:
            final_texts = p.default_texts

        session_state = {
            "texts": final_texts,
            "images": initial_images,
            "options": final_options,
            "params": p,
            "invalid_input_count": 0,
            "status": "waiting_for_input",
        }
        active_sessions[session_id] = session_state

        asyncio.create_task(_session_worker(event, session_id, meme_info))

    except Exception as e:
        logger.error(f"启动会话 '{meme_info.key}' 失败: {e}", exc_info=True)
        active_sessions.pop(session_id, None)
        await _send_and_record(event, "开启表情制作任务失败了...")


async def handle_shortcut(event, meme: MemeInfo, shortcut: Dict, match: re.Match):
    """处理快捷指令"""
    try:
        logger.debug(f"快捷指令匹配成功: {meme.key}")
        match_dict = match.groupdict()
        texts = [t.format(**match_dict) for t in shortcut.get("texts", [])]
        options = {k: v.format(**match_dict) if isinstance(v, str) else v
                   for k, v in shortcut.get("options", {}).items()}
        await meme_generate_handler(event, meme, "", initial_options=options, initial_texts=texts)
    except Exception as e:
        logger.error(f"处理快捷指令失败: {e}", exc_info=True)


async def handle_random_meme(event, match):
    """处理随机表情命令"""
    ctx = _ctx
    arg_text = (match.group(1) or '').strip() if match else ''
    try:
        temp_meme_info = MemeInfo(
            key="", params=MemeParams(min_images=0, max_images=99, min_texts=0, max_texts=99),
            date_created=datetime.now(), keywords=[]
        )
        initial_texts, initial_images, _ = await build_meme_payload(event, temp_meme_info, arg_text)
        n_images_initial, n_texts_initial = len(initial_images), len(initial_texts)
        final_arg_text = arg_text
        n_images_filter, n_texts_filter = n_images_initial, n_texts_initial

        if n_images_initial == 0 and n_texts_initial == 0:
            logger.info("检测到无参数随机表情，启用默认文字模式")
            n_texts_filter = 1
            final_arg_text = "请输入文本"

        await _send_and_record(event, "正在寻找合适的表情...")

        available_memes = []
        for info in _meme_manager.meme_infos.values():
            if not await _recorder.is_meme_disabled(info.key, str(event.group_id) if event.group_id else None):
                if (info.params.min_images <= n_images_filter <= info.params.max_images and
                        info.params.min_texts <= n_texts_filter <= info.params.max_texts):
                    available_memes.append(info)

        if not available_memes:
            await _send_and_record(event, "找不到能制作这个素材的表情...换个试试？")
            return

        chosen_meme = random.choice(available_memes)
        await meme_generate_handler(event, chosen_meme, final_arg_text)

    except (ArgParseError, APIError, TimeoutError) as e:
        await _send_and_record(event, f"出错了：{e}")
    except Exception as e:
        logger.error(f"随机表情失败: {e}", exc_info=True)
        await _send_and_record(event, "随机表情失败了...")


def _send(event, ctx, text: str):
    """统一发送消息"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )
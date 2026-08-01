"""
图片工具处理器 - 图片处理工具功能
从 AstrBot 迁移至 zgric_onebot11，适配框架 (event, match) 调用规范
"""
import re
import asyncio
import io
import zipfile
import tempfile
import os
import time
import base64
import logging
from typing import Dict, Any, List, Optional

import filetype

from plugins.mememaker_api.exceptions import ArgParseError, APIError
from plugins.mememaker_api._shared import _api_client, _config, _ctx
from plugins.mememaker_api.core.utils import image_to_base64_cq, get_images

logger = logging.getLogger(__name__)


async def handle_image_tool(event, operation: str, arg_text: str):
    """
    处理图片工具操作
    注意：此函数不由路由框架直接调用，而是由 universal_handler 或路由辅助函数调用
    """
    ctx = _ctx
    try:
        op_config = {"merge_horizontal": 2, "merge_vertical": 2, "gif_merge": 2}
        min_images = op_config.get(operation, 1)
        image_ids = await _get_images_for_tool(event, min_images=min_images)
        if not image_ids:
            return

        result_obj = None
        if operation == "resize":
            width, height = _parse_resize_args(arg_text)
            result_obj = await _api_client.resize(image_ids[0], width, height)
        elif operation == "crop":
            image_info = await _api_client.inspect_image(image_ids[0])
            left, top, right, bottom = _parse_crop_args(arg_text, image_info)
            result_obj = await _api_client.crop(image_ids[0], left, top, right, bottom)
        elif operation == "gif_change_duration":
            image_info = await _api_client.inspect_image(image_ids[0])
            duration = _parse_gif_change_duration_args(arg_text, image_info)
            result_obj = await _api_client.gif_change_duration(image_ids[0], duration)
        elif operation in ["flip_horizontal", "flip_vertical", "grayscale", "invert", "gif_reverse"]:
            result_obj = await getattr(_api_client, operation)(image_ids[0])
        elif operation == "gif_split":
            result_obj = await _api_client.gif_split(image_ids[0])
        elif operation in ["merge_horizontal", "merge_vertical"]:
            result_obj = await getattr(_api_client, operation)(image_ids)
        elif operation == "rotate":
            degrees = float(arg_text or 90.0)
            result_obj = await _api_client.rotate(image_ids[0], degrees)
        elif operation == "gif_merge":
            duration = float(arg_text or 0.1)
            result_obj = await _api_client.gif_merge(image_ids, duration)

        if result_obj:
            await _send_results(event, result_obj)
        else:
            _send(event, ctx, "操作失败：未生成结果。")

    except (APIError, ValueError, ArgParseError) as e:
        _send(event, ctx, f"操作失败: {e}")
    except Exception as e:
        logger.error(f"图片操作 {operation} 失败: {e}", exc_info=True)
        _send(event, ctx, f"图片操作失败: {e}")


async def _get_images_for_tool(event, min_images: int = 1) -> List[str]:
    """从消息中提取所需数量的图片，上传并返回 image_id 列表"""
    image_bytes_list = await _get_images_from_message(event)

    if len(image_bytes_list) < min_images:
        if _config.get("use_sender_when_no_image", True):
            avatar_bytes = await _get_avatar(str(event.user_id))
            if avatar_bytes:
                image_bytes_list.insert(0, avatar_bytes)

    if len(image_bytes_list) < min_images:
        raise ArgParseError(f"图片数量不足，此操作需要 {min_images} 张图片。")

    images_to_upload = image_bytes_list[:min_images] if min_images > 0 else image_bytes_list
    tasks = [_api_client.upload_image(img_bytes) for img_bytes in images_to_upload]
    return await asyncio.gather(*tasks)


async def _get_images_from_message(event) -> List[bytes]:
    """从事件消息中提取图片字节数据"""
    image_bytes_list: List[bytes] = []

    for seg in event.segments:
        if seg.get('type') == 'image':
            data = seg.get('data', {})
            img_bytes = None

            img_file = data.get('file', '') or ''
            if img_file.startswith('base64://'):
                try:
                    img_bytes = base64.b64decode(img_file[len('base64://'):])
                except Exception:
                    pass

            if img_bytes is None:
                img_url = data.get('url', '')
                if img_url:
                    img_bytes = await _api_client._download_image(img_url)

            if img_bytes:
                image_bytes_list.append(img_bytes)

    return image_bytes_list


async def _get_avatar(user_id: str) -> Optional[bytes]:
    """获取用户头像"""
    if not user_id.isdigit():
        return None
    return await _api_client._download_image(f"http://q4.qlogo.cn/g?b=qq&nk={user_id}&s=640")


async def _send_results(event, result_obj):
    """发送图片结果，支持多图策略"""
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
        cq_parts = [image_to_base64_cq(img_bytes) for img_bytes in image_list]
        _send(event, ctx, "\n".join(cq_parts))
        return

    elif send_as_zip_enabled and len(image_list) > zip_threshold:
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
        _send(event, ctx, f"处理完成，共生成 {len(image_list)} 张图片：")
        for img_bytes in image_list:
            _send(event, ctx, image_to_base64_cq(img_bytes))
            await asyncio.sleep(0.5)


def _parse_resize_args(text: str):
    """解析缩放参数"""
    width, height = None, None
    if match := re.fullmatch(r"(\d{1,4})?[*xX, ](\d{1,4})?", text):
        w, h = match.groups()
        if w: width = int(w)
        if h: height = int(h)
        return width, height
    raise ArgParseError("缩放尺寸格式不正确，请使用如: 100x200, 100x, x200")


def _parse_crop_args(text: str, image_info: Dict):
    """解析裁剪参数"""
    if match := re.fullmatch(r"(\d{1,4})[, ](\d{1,4})[, ](\d{1,4})[, ](\d{1,4})", text):
        return tuple(map(int, match.groups()))
    img_w, img_h = image_info["width"], image_info["height"]
    if match := re.fullmatch(r"(\d{1,4})[*xX, ](\d{1,4})", text):
        width, height = map(int, match.groups())
    elif match := re.fullmatch(r"(\d{1,2})[:：比](\d{1,2})", text):
        wp, hp = map(int, match.groups())
        size = min(img_w / wp, img_h / hp)
        width, height = int(wp * size), int(hp * size)
    else:
        raise ArgParseError("裁剪格式不正确，请使用如: 0,0,100,100 或 100x100 或 16:9")
    left = (img_w - width) // 2
    top = (img_h - height) // 2
    return left, top, left + width, top + height


def _parse_gif_change_duration_args(text: str, image_info: Dict) -> float:
    """解析 Gif 变速参数"""
    p_float = r"\d{0,3}\.?\d{1,3}"
    if match := re.fullmatch(rf"({p_float})fps", text, re.I):
        duration = 1 / float(match.group(1))
    elif match := re.fullmatch(rf"({p_float})(m?)s", text, re.I):
        duration = float(match.group(1)) / 1000 if match.group(2) else float(match.group(1))
    else:
        duration = image_info.get("average_duration") or 0.1
        if match := re.fullmatch(rf"({p_float})(?:x|X|倍速?)", text):
            duration /= float(match.group(1))
        elif match := re.fullmatch(rf"({p_float})%", text):
            duration /= float(match.group(1)) / 100
        else:
            raise ArgParseError("变速格式不正确，请使用如: 0.5x, 50%, 20fps, 0.05s")
    if duration < 0.02:
        raise ArgParseError(f"帧间隔必须大于 0.02s (50fps)，当前为 {duration:.3f}s")
    return duration


def _send(event, ctx, text: str):
    """统一发送消息"""
    ctx.send_msg(
        user_id=event.user_id,
        group_id=event.group_id if event.is_group else None,
        message=text,
    )
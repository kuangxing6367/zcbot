"""
表情包管理器模块 - 管理内存中的表情包数据
从 AstrBot 迁移至 zgric_onebot11，仅替换日志模块
"""
import re
import logging
from typing import Dict, Any, List, Optional, Set, Tuple

from plugins.mememaker_api.api_client import APIClient
from plugins.mememaker_api.models import MemeInfo

logger = logging.getLogger(__name__)

class MemeManager:
    """负责管理内存中的表情包数据"""

    def __init__(self):
        self.meme_infos: Dict[str, MemeInfo] = {}
        self.keyword_map: Dict[str, MemeInfo] = {}
        self.shortcuts: List[Dict] = []
        self.sorted_keywords: List[str] = []

    async def refresh_memes(self, api_client: APIClient) -> Tuple[bool, int, int]:
        """从 API 刷新表情包数据和快捷指令"""
        logger.info("MemeManager: 正在刷新表情包数据...")
        try:
            infos = await api_client.get_meme_infos()

            meme_infos_temp: Dict[str, MemeInfo] = {info.key: info for info in infos}
            keyword_map_temp: Dict[str, MemeInfo] = {}
            shortcuts_temp: List[Dict] = []

            for info in infos:
                keyword_map_temp[info.key] = info
                for keyword in info.keywords:
                    keyword_map_temp[keyword] = info

                for sc in info.shortcuts:
                    try:
                        shortcuts_temp.append({
                            "pattern": re.compile(sc["pattern"]), "meme": info, "shortcut": sc
                        })
                    except re.error:
                        logger.warning(f"快捷指令 \"{sc['pattern']}\" 正则表达式无效，已跳过")

            self.meme_infos = meme_infos_temp
            self.keyword_map = keyword_map_temp
            self.shortcuts = shortcuts_temp
            self.sorted_keywords = sorted(self.keyword_map.keys(), key=len, reverse=True)

            meme_count = len(self.meme_infos)
            shortcut_count = len(self.shortcuts)
            logger.info(f"成功缓存 {meme_count} 个表情和 {shortcut_count} 个快捷指令。")
            return True, meme_count, shortcut_count

        except Exception as e:
            logger.error(f"MemeManager: 刷新表情列表失败: {e}")
            return False, 0, 0

    def find_keyword_in_text(self, text: str, fuzzy_match: bool) -> Optional[str]:
        """在文本中寻找第一个匹配的表情包关键词"""
        first_word = text.split(" ", 1)[0]
        if first_word in self.keyword_map:
            return first_word
        if fuzzy_match:
            for keyword in self.sorted_keywords:
                if text.startswith(keyword):
                    return keyword
        return None

    def find_meme_by_keyword(self, keyword: str) -> Optional[MemeInfo]:
        """通过关键词精确查找单个表情"""
        return self.keyword_map.get(keyword)

    def find_memes_by_keyword(self, keyword: str) -> List[MemeInfo]:
        """根据关键词寻找所有匹配的表情"""
        return [meme for meme in self.meme_infos.values() if keyword in meme.keywords or keyword == meme.key]
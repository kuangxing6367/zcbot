"""
数据库记录器模块 - 负责管理插件的 SQLite 数据库读写
从 AstrBot 迁移至 zgric_onebot11，仅替换日志模块
"""
import asyncio
import aiosqlite
import logging
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

class StatsRecorder:
    """负责管理插件的数据库读写（使用持久化连接和懒加载）"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def _get_connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
        return self._conn

    async def close(self):
        if self._conn:
            await self._conn.close()
            logger.info("数据库连接已成功关闭。")

    async def _initialize_db(self):
        """私有的初始化数据库方法，只负责建表"""
        try:
            db = await self._get_connection()
            await db.execute("""
                CREATE TABLE IF NOT EXISTS meme_usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, meme_key TEXT NOT NULL, user_id TEXT NOT NULL,
                    group_id TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS plugin_group_admins (
                    group_id TEXT NOT NULL, user_id TEXT NOT NULL, PRIMARY KEY (group_id, user_id));
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS meme_manager (
                    meme_key TEXT NOT NULL, scope TEXT NOT NULL, subject_id TEXT NOT NULL, mode TEXT NOT NULL,
                    PRIMARY KEY (meme_key, scope, subject_id));
            """)
            await db.commit()
            logger.info(f"数据库于 {self.db_path} 初始化完成。")
            self._initialized = True
        except Exception as e:
            logger.error(f"插件数据库自动建表失败: {e}")

    async def _ensure_initialized(self):
        """守护函数：确保在执行任何操作前，数据库已初始化"""
        if not self._initialized:
            async with self._lock:
                if not self._initialized:
                    await self._initialize_db()

    async def record_usage(self, meme_key: str, user_id: str, group_id: Optional[str]):
        await self._ensure_initialized()
        try:
            db = await self._get_connection()
            await db.execute(
                "INSERT INTO meme_usage_logs (meme_key, user_id, group_id) VALUES (?, ?, ?)",
                (meme_key, user_id, group_id or "private")
            )
            await db.commit()
        except Exception as e:
            logger.error(f"写入使用记录失败: {e}")

    async def get_stats_records(self, query: str, params: tuple) -> List[Tuple]:
        await self._ensure_initialized()
        db = await self._get_connection()
        cursor = await db.execute(query, params)
        return await cursor.fetchall()

    async def get_recent_meme_keys(self, start_time) -> List[str]:
        await self._ensure_initialized()
        db = await self._get_connection()
        cursor = await db.execute("SELECT meme_key FROM meme_usage_logs WHERE timestamp >= ?", (start_time.isoformat(),))
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def add_group_admin(self, group_id: str, user_id: str):
        await self._ensure_initialized()
        db = await self._get_connection()
        await db.execute("INSERT OR IGNORE INTO plugin_group_admins (group_id, user_id) VALUES (?, ?)", (group_id, user_id))
        await db.commit()

    async def remove_group_admin(self, group_id: str, user_id: str):
        await self._ensure_initialized()
        db = await self._get_connection()
        await db.execute("DELETE FROM plugin_group_admins WHERE group_id = ? AND user_id = ?", (group_id, user_id))
        await db.commit()

    async def list_group_admins(self, group_id: str) -> List[str]:
        await self._ensure_initialized()
        db = await self._get_connection()
        cursor = await db.execute("SELECT user_id FROM plugin_group_admins WHERE group_id = ?", (group_id,))
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def is_plugin_group_admin(self, group_id: str, user_id: str) -> bool:
        await self._ensure_initialized()
        db = await self._get_connection()
        cursor = await db.execute("SELECT 1 FROM plugin_group_admins WHERE group_id = ? AND user_id = ?", (group_id, user_id))
        return await cursor.fetchone() is not None

    async def is_meme_whitelisted(self, meme_key: str) -> bool:
        await self._ensure_initialized()
        db = await self._get_connection()
        cursor = await db.execute("SELECT 1 FROM meme_manager WHERE meme_key = ? AND scope = 'global' AND mode = 'white'", (meme_key,))
        return await cursor.fetchone() is not None

    async def set_meme_mode(self, meme_key: str, scope: str, subject_id: str, mode: str):
        await self._ensure_initialized()
        db = await self._get_connection()
        await db.execute("INSERT OR REPLACE INTO meme_manager (meme_key, scope, subject_id, mode) VALUES (?, ?, ?, ?)", (meme_key, scope, subject_id, mode))
        await db.commit()

    async def remove_meme_rule(self, meme_key: str, scope: str, subject_id: str):
        await self._ensure_initialized()
        db = await self._get_connection()
        await db.execute("DELETE FROM meme_manager WHERE meme_key = ? AND scope = ? AND subject_id = ?", (meme_key, scope, subject_id))
        await db.commit()

    async def get_manager_list(self, group_id: str) -> List[Tuple[str, str, str]]:
        await self._ensure_initialized()
        db = await self._get_connection()
        cursor = await db.execute("SELECT meme_key, scope, mode FROM meme_manager WHERE (scope = 'group' AND subject_id = ?) OR scope = 'global'", (group_id,))
        return await cursor.fetchall()

    async def is_meme_disabled(self, meme_key: str, group_id: Optional[str]) -> bool:
        await self._ensure_initialized()
        db = await self._get_connection()
        cursor = await db.execute("SELECT mode FROM meme_manager WHERE meme_key = ? AND scope = 'global'", (meme_key,))
        global_rule = await cursor.fetchone()
        group_rule = None
        if group_id:
            cursor = await db.execute("SELECT mode FROM meme_manager WHERE meme_key = ? AND scope = 'group' AND subject_id = ?", (meme_key, group_id))
            group_rule = await cursor.fetchone()
        if global_rule and global_rule[0] == 'white':
            return not (group_rule and group_rule[0] == 'white')
        else:
            return bool(group_rule and group_rule[0] == 'black')
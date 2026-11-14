"""令牌哈希工具：库内只存摘要，明文只返回客户端。

这里刻意不依赖 Flask / 数据库：数据库迁移（启动早期）与 Web 鉴权（运行期）
都要用同一套算法，放在 framework/api/ 下会让迁移反向依赖 Web 层。
"""
import hashlib
import secrets

# API Key 摘要前缀。带前缀有两个作用：
#   1) v11 迁移靠它区分「已迁移的摘要」与「历史明文」，重复执行不会二次哈希；
#   2) 校验侧一眼能认出老库里的明文，只做一次性即时收敛，不做长期兼容分支。
# 会话 token 的摘要不带前缀（admin_users.token_hash 是独立列，无混淆风险）。
API_KEY_HASH_PREFIX = 'sha256:'


def hash_session_token(token: str) -> str:
    """会话令牌的库内表示：sha256 十六进制摘要（64 字符，定长、可索引）。"""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def hash_api_key(token: str) -> str:
    """接口令牌（API Key）的库内表示：'sha256:' + sha256 十六进制摘要。"""
    return API_KEY_HASH_PREFIX + hashlib.sha256(token.encode('utf-8')).hexdigest()


def is_hashed_api_key(stored: str) -> bool:
    """判断库里的值是否已经是 v11 之后的摘要形式。"""
    return (isinstance(stored, str)
            and stored.startswith(API_KEY_HASH_PREFIX)
            and len(stored) == len(API_KEY_HASH_PREFIX) + 64)


def api_key_matches(stored: str, token: str) -> bool:
    """常量时间比对库内摘要与客户端提交的明文 API Key。

    DB 等值匹配受 collation 大小写 / 尾随空格影响，所以查出来之后再做一次
    compare_digest 二次确认，把「是否命中」收敛到一处。
    """
    if not is_hashed_api_key(stored) or not isinstance(token, str):
        return False
    return secrets.compare_digest(stored, hash_api_key(token))

# -*- coding: utf-8 -*-
"""
安全等级预设系统（Security Level Presets）
==========================================
把「蜜罐 / 诱饵 / 拉黑 / tarpit / 安全响应头 / 人机验证 / 认证静默」等一揽子开关
收敛成**一个档位**，让使用者只需在 config.yaml 里写一行 `security.level: medium`
即可，不必逐项调参；确有需要时，仍可对单个键显式覆盖（显式配置优先于预设）。

档位一览
--------
    off       关闭所有主动防御（仅保留最基础的登录限速），适合本地开发
    low       轻量：核心诱饵 + 安全响应头 + 人机验证，不自动拉黑
    medium    默认（推荐）：完整诱饵集 + 自动拉黑 1 小时 + 认证静默 + 人机验证
    high      严格：延迟 1.5s、拉黑 24 小时、认证失败 10 次即封
    paranoid  偏执：延迟 3s、永久封禁、认证失败 5 次即封、人机验证难度最高

解析规则
--------
生效配置 = 预设(level)  ←  用户显式配置（逐键覆盖，只认已知键）

即：`security.honeypot.tarpit_ms: 0` 这种显式写法会覆盖预设值；
未写到的键一律取预设值。这样既保证「一行档位」好用，又不失精细控制。

设计依据（来自公开案例）
------------------------
- 案例：12 天 502 次扫描中 `.env` 占一半以上 → 诱饵集以「配置文件/密钥」为第一优先
- 案例：攻击者会做路径变形（`/.//.env`、`%2eenv`、路径穿越）→ evasion_detect
- 案例：诱饵需可事后追溯泄露路径 → canary（唯一追踪串）
- 案例：Actuator / Swagger / Docker API 是高频失陷点 → 对应诱饵默认开启
- 案例：弱口令字典爆破成功 → 登录限速 + 失败阈值拉黑 + 人机验证
"""
import logging

logger = logging.getLogger('zcbot')

LEVELS = ('off', 'low', 'medium', 'high', 'paranoid')
DEFAULT_LEVEL = 'medium'

# 档位中文名（用于日志 / 状态接口展示）
LEVEL_LABELS = {
    'off': '关闭',
    'low': '宽松',
    'medium': '标准（推荐）',
    'high': '严格',
    'paranoid': '偏执',
}

# 每个档位的生效值。键必须与各模块读取的键一致。
_PRESETS = {
    'off': {
        'silent_auth': False,
        'headers': {
            'enabled': False,
            'server': '',
            'csp': '',
        },
        'human_check': {
            'enabled': False,
            'difficulty': 3,
            'apply_to': 'none',
            'bypass_whitelist': True,
            'ticket_ttl': 1800,
            'challenge_ttl': 120,
        },
        'honeypot': {
            'enabled': False,
            'decoy_set': 'off',          # off / basic / full
            'tarpit_ms': 0,
            'blacklist': False,
            'blacklist_ttl': 0,
            'log_body': False,
            'max_body_log': 1024,
            'unknown_api_decoy': False,
            'auth_fail_blacklist': 0,
            'auth_fail_window': 300,
            'tarpit_auth_ms': 0,
            'invalid_token_decoy': False,
            'canary': False,
            'evasion_detect': False,
            'fake_404': False,
        },
    },
    'low': {
        'silent_auth': False,
        'headers': {
            'enabled': True,
            'server': '',
            'csp': '',
        },
        'human_check': {
            'enabled': True,
            'difficulty': 3,
            'apply_to': 'all',
            'bypass_whitelist': True,     # 宽松档：白名单免检，便于排障
            'ticket_ttl': 3600,
            'challenge_ttl': 180,
        },
        'honeypot': {
            'enabled': True,
            'decoy_set': 'basic',
            'tarpit_ms': 200,
            'blacklist': False,           # 只记录不封禁，适合还在调参的阶段
            'blacklist_ttl': 0,
            'log_body': False,
            'max_body_log': 1024,
            'unknown_api_decoy': True,
            'auth_fail_blacklist': 50,
            'auth_fail_window': 600,
            'tarpit_auth_ms': 300,
            'invalid_token_decoy': False,
            'canary': False,
            'evasion_detect': True,
            'fake_404': True,
        },
    },
    'medium': {
        'silent_auth': True,              # 认证不对 → 静默丢弃（不返回 401 JSON）
        'headers': {
            'enabled': True,
            'server': '',
            'csp': '',
        },
        'human_check': {
            'enabled': True,
            'difficulty': 4,              # 约 6.5 万次哈希，纯 JS 约 0.1~0.4s
            'apply_to': 'all',
            'bypass_whitelist': False,
            'ticket_ttl': 1800,
            'challenge_ttl': 120,
        },
        'honeypot': {
            'enabled': True,
            'decoy_set': 'full',
            'tarpit_ms': 600,
            'blacklist': True,
            'blacklist_ttl': 3600,        # 1 小时后自动解封
            'log_body': True,
            'max_body_log': 2048,
            'unknown_api_decoy': True,
            'auth_fail_blacklist': 20,
            'auth_fail_window': 300,
            'tarpit_auth_ms': 800,
            'invalid_token_decoy': False,
            'canary': True,
            'evasion_detect': True,
            'fake_404': True,
        },
    },
    'high': {
        'silent_auth': True,
        'headers': {
            'enabled': True,
            'server': '',
            'csp': '',
        },
        'human_check': {
            'enabled': True,
            'difficulty': 4,
            'apply_to': 'all',
            'bypass_whitelist': False,
            'ticket_ttl': 900,
            'challenge_ttl': 90,
        },
        'honeypot': {
            'enabled': True,
            'decoy_set': 'full',
            'tarpit_ms': 1500,
            'blacklist': True,
            'blacklist_ttl': 86400,       # 24 小时
            'log_body': True,
            'max_body_log': 4096,
            'unknown_api_decoy': True,
            'auth_fail_blacklist': 10,
            'auth_fail_window': 300,
            'tarpit_auth_ms': 1500,
            'invalid_token_decoy': False,
            'canary': True,
            'evasion_detect': True,
            'fake_404': True,
        },
    },
    'paranoid': {
        'silent_auth': True,
        'headers': {
            'enabled': True,
            'server': '',
            'csp': '',
        },
        'human_check': {
            'enabled': True,
            'difficulty': 4,              # 约 0.3s（实测纯 JS）；5 约 8s，如需更严可手动调高
            'apply_to': 'all',
            'bypass_whitelist': False,
            'ticket_ttl': 600,
            'challenge_ttl': 60,
        },
        'honeypot': {
            'enabled': True,
            'decoy_set': 'full',
            'tarpit_ms': 3000,
            'blacklist': True,
            'blacklist_ttl': 0,           # 0 = 永久封禁
            'log_body': True,
            'max_body_log': 8192,
            'unknown_api_decoy': True,
            'auth_fail_blacklist': 5,
            'auth_fail_window': 300,
            'tarpit_auth_ms': 3000,
            'invalid_token_decoy': False,
            'canary': True,
            'evasion_detect': True,
            'fake_404': True,
        },
    },
}

_SECTIONS = ('headers', 'human_check', 'honeypot')


def normalize_level(value) -> str:
    """把任意输入规整为合法档位；非法值回落默认档并告警"""
    raw = str(value or '').strip().lower()
    if raw in LEVELS:
        return raw
    if raw:
        logger.warning(f"[安全等级] 未知档位 {value!r}，回落为 {DEFAULT_LEVEL}")
    return DEFAULT_LEVEL


def resolve(sec_cfg) -> dict:
    """
    解析 security 配置，返回生效值：
        {
          'level': 'medium',
          'level_label': '标准（推荐）',
          'level_raw': 用户原始输入（便于提示拼写错误）,
          'silent_auth': True,
          'headers': {...},
          'human_check': {...},
          'honeypot': {...},
        }
    预设为基线，用户显式配置逐键覆盖（只认预设中已存在的键）。
    """
    sec = sec_cfg if isinstance(sec_cfg, dict) else {}
    raw = sec.get('level')
    level = normalize_level(raw)
    preset = _PRESETS[level]

    out = {
        'level': level,
        'level_label': LEVEL_LABELS.get(level, level),
        'level_raw': raw,
        'silent_auth': preset['silent_auth'],
    }
    for s in _SECTIONS:
        out[s] = dict(preset[s])

    # 逐键叠加用户显式配置
    if sec.get('silent_auth') is not None:
        out['silent_auth'] = bool(sec['silent_auth'])
    for s in _SECTIONS:
        user = sec.get(s)
        if isinstance(user, dict):
            for k, v in user.items():
                if k in out[s] and v is not None:
                    out[s][k] = v
    return out


def describe(level: str) -> dict:
    """返回某档位的说明（供状态接口 / 文档展示）"""
    lv = normalize_level(level)
    p = _PRESETS[lv]
    return {
        'level': lv,
        'label': LEVEL_LABELS.get(lv, lv),
        'silent_auth': p['silent_auth'],
        'headers': dict(p['headers']),
        'human_check': dict(p['human_check']),
        'honeypot': dict(p['honeypot']),
    }


def all_levels() -> list:
    """返回全部档位及其说明，供前端/文档渲染"""
    return [describe(lv) for lv in LEVELS]

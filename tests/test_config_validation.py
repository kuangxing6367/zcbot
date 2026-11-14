"""配置 schema 校验（Gap 1）回归测试。

铁律：路径用 __file__ 推导、不碰真实 config.yaml、不依赖网络。
直接运行：PYTHONPATH=. python tests/test_config_validation.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from framework.config import _DEFAULT_CONFIG, validate_config


def _default_config():
    """解析内置默认配置模板（等价于一次完整 load_config 后的结构）。"""
    cfg = yaml.safe_load(_DEFAULT_CONFIG)
    return cfg


def check(name, cond, detail=''):
    print(('通过 ' if cond else '失败 ') + name + (('：' + detail) if detail else ''))
    if not cond:
        raise SystemExit('断言失败：' + name)


def test_default_config_clean():
    cfg = _default_config()
    issues = validate_config(cfg)
    check('默认配置零告警', issues == [], str(issues))


def test_bad_enum():
    cfg = _default_config()
    cfg['security']['level'] = 'ultra'
    issues = validate_config(cfg)
    check('非法枚举被检出', any('security.level' in i for i in issues), str(issues))


def test_wrong_type_int():
    cfg = _default_config()
    cfg['event_queue']['maxsize'] = 'not_an_int'
    issues = validate_config(cfg)
    check('整数字段类型错误被检出', any('event_queue.maxsize' in i for i in issues),
          str(issues))


def test_out_of_range():
    cfg = _default_config()
    cfg['web']['port'] = 70000
    issues = validate_config(cfg)
    check('端口超上限被检出', any('web.port' in i and '上限' in i for i in issues),
          str(issues))


def test_string_int_accepted():
    cfg = _default_config()
    cfg['web']['port'] = '8080'  # YAML 常见写法
    issues = validate_config(cfg)
    check('整数字符串被宽松接受', not any('web.port' in i for i in issues),
          str(issues))


def test_wrong_type_bool():
    cfg = _default_config()
    cfg['observability']['health'] = 'maybe'
    issues = validate_config(cfg)
    check('布尔字段类型错误被检出', any('observability.health' in i for i in issues),
          str(issues))


def test_bool_string_accepted():
    cfg = _default_config()
    cfg['log']['coalesce'] = 'false'
    issues = validate_config(cfg)
    check('布尔字符串被宽松接受', not any('log.coalesce' in i for i in issues),
          str(issues))


def test_nested_rate_limit():
    cfg = _default_config()
    cfg['reliability']['rate_limit']['enabled'] = 123
    issues = validate_config(cfg)
    check('嵌套 rate_limit 布尔错误被检出',
          any('reliability.rate_limit.enabled' in i for i in issues), str(issues))


def test_mysql_missing_conn():
    cfg = _default_config()
    cfg['database']['type'] = 'mysql'
    issues = validate_config(cfg)
    check('mysql 缺连接信息被检出',
          any('database.type=mysql' in i for i in issues), str(issues))


def test_section_not_dict():
    cfg = _default_config()
    cfg['web'] = '127.0.0.1:8080'
    issues = validate_config(cfg)
    check('整段非 dict 被检出', any('[web]' in i for i in issues), str(issues))


def run_all():
    test_default_config_clean()
    test_bad_enum()
    test_wrong_type_int()
    test_out_of_range()
    test_string_int_accepted()
    test_wrong_type_bool()
    test_bool_string_accepted()
    test_nested_rate_limit()
    test_mysql_missing_conn()
    test_section_not_dict()
    print('\n全部通过：配置 schema 校验 10 组')


if __name__ == '__main__':
    run_all()

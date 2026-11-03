# -*- coding: utf-8 -*-
"""配置缺段补齐：旧 config.yaml 必须能拿到新版本引入的 section。

铁律：路径用 __file__ 推导、文件写临时目录、不碰真实 config.yaml。
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.config import fill_missing_sections, load_config


def check(name, cond, detail=''):
    print(('通过 ' if cond else '失败 ') + name + (('：' + detail) if detail else ''))
    if not cond:
        raise SystemExit('断言失败：' + name)


def test_fill_adds_missing_section():
    cfg = {'database': {'type': 'sqlite'}}
    added = fill_missing_sections(cfg)
    check('补齐：缺的段被加进来', 'reliability' in cfg and 'reliability' in added,
          str(sorted(added)))
    check('补齐：新段带默认值（去重开、限流关）',
          cfg['reliability']['dedup_window'] == 300
          and cfg['reliability']['rate_limit']['enabled'] is False,
          str(cfg['reliability']))


def test_fill_keeps_user_values():
    cfg = {'database': {'type': 'sqlite'},
           'reliability': {'dedup_window': 5,
                           'rate_limit': {'enabled': True, 'user_per_min': 3}}}
    added = fill_missing_sections(cfg)
    check('补齐：已存在的段不被覆盖', cfg['reliability'] == {
        'dedup_window': 5, 'rate_limit': {'enabled': True, 'user_per_min': 3}},
        str(cfg['reliability']))
    check('补齐：已存在的段不算「新增」', 'reliability' not in added, str(sorted(added)))
    check('补齐：用户值不被默认值改写', cfg['database']['type'] == 'sqlite',
          cfg['database']['type'])


def test_fill_idempotent():
    cfg = {}
    first = fill_missing_sections(cfg)
    second = fill_missing_sections(cfg)
    check('补齐：二次调用无新增（幂等）', bool(first) and second == [], str(second))


def test_load_config_on_old_file():
    """真实读一份「旧版」config.yaml：必须能读到 reliability，且不改写文件。"""
    d = tempfile.mkdtemp(prefix='zcbot_cfg_')
    path = os.path.join(d, 'config.yaml')
    old = "database:\n  type: sqlite\n  path: data/zcbot.db\n"
    with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(old)
    before = io.open(path, encoding='utf-8').read()

    cfg = load_config(path)
    check('加载：旧配置文件也能拿到 reliability 段',
          isinstance(cfg.get('reliability'), dict), str(cfg.get('reliability')))
    check('加载：旧配置文件也能拿到 observability 段',
          isinstance(cfg.get('observability'), dict), str(cfg.get('observability')))
    check('加载：用户原有配置保持', cfg['database']['type'] == 'sqlite',
          str(cfg.get('database')))

    after = io.open(path, encoding='utf-8').read()
    check('加载：不改写用户配置文件（注释/格式不被破坏）', before == after,
          'file changed')

    # 清理（临时目录内，安全）
    for name in os.listdir(d):
        os.remove(os.path.join(d, name))
    os.rmdir(d)


def run_all():
    for fn in (test_fill_adds_missing_section, test_fill_keeps_user_values,
               test_fill_idempotent, test_load_config_on_old_file):
        fn()
    print('\n全部通过：配置缺段补齐 4 组')


if __name__ == '__main__':
    run_all()

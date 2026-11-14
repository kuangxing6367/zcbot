"""状态目录迁移子系统（内核内置，启动期自动执行，默认开启）。

目录职责：

- ``version.py``   当前内核版本检测 + SemVer 2.0.0 解析/比较、来源漂移检查；
- ``detector.py``  探测当前目录下**可能**是哪一代版本留下的布局（含证据与置信度）；
- ``policy.py``    自动迁移开关：默认开启，``config.yaml`` 的
                   ``framework.auto_migrate`` 可手动关闭，只读不改；
- ``migrator.py``  幂等迁移执行器 + 账本 + 并发锁 + 启动钩子；
- ``__main__.py``  命令行：``python -m framework.migration --inspect``。

方向是**向后兼容**：新内核接住旧版本留下的目录布局；自动迁移只是把历史
状态收敛到 ``data/``，不要求旧版本能读新布局，也不改动被搬文件的内容。
"""
from .detector import probe
from .migrator import (
                      ensure_auto_migrated,
                      inspect,
                      ledger_path,
                      migrate,
                      plan,
                      read_ledger,
)
from .policy import Decision, auto_migrate_enabled, read_config_switch
from .version import (
                      Version,
                      compare_versions,
                      current_version,
                      current_version_string,
                      parse_version,
                      version_sources,
)

__all__ = [
                      'Decision',
                      'Version',
                      'auto_migrate_enabled',
                      'compare_versions',
                      'current_version',
                      'current_version_string',
                      'ensure_auto_migrated',
                      'inspect',
                      'ledger_path',
                      'migrate',
                      'parse_version',
                      'plan',
                      'probe',
                      'read_config_switch',
                      'read_ledger',
                      'version_sources',
]

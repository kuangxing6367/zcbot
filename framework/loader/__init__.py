# -*- coding: utf-8 -*-
"""framework.loader —— 插件加载器包

旧路径兼容：
  from framework.loader import PluginLoader, pip_install_*  # 不变
  from framework.loader.lifecycle import PluginLifecycleMixin  # → framework.loader.lifecycle
"""
from framework.loader.base import (  # noqa: F401
    PluginLoader, _cards_executor, _CONFIG_FILE_EXTS, _CONFIG_FILE_NAMES, _CODE_FILE_NAMES,
)
from framework.loader.config import PluginConfigMixin  # noqa: F401
from framework.loader.lifecycle import PluginLifecycleMixin, _PluginSourceLoader  # noqa: F401
from framework.loader.runtime import PluginRuntimeMixin  # noqa: F401
from framework.loader.ui import (  # noqa: F401
    PluginUiExtensionsMixin, PluginWebuiMixin, PluginGroupSettingsMixin,
)
from framework.deps import (  # noqa: F401  兼容旧导入路径
    PluginDepsMixin,
    _check_version_compatible,
    _parse_requirements_file,
    _parse_ver,
    _parse_version_spec,
    pip_install_all,
    pip_install_requirements,
    pip_install_with_mirror,
)

__all__ = [
    'PluginLoader',
    'PluginConfigMixin', 'PluginLifecycleMixin', 'PluginRuntimeMixin',
    'PluginUiExtensionsMixin', 'PluginWebuiMixin', 'PluginGroupSettingsMixin',
    'PluginDepsMixin',
    'pip_install_all', 'pip_install_requirements', 'pip_install_with_mirror',
    '_check_version_compatible', '_parse_requirements_file', '_parse_ver', '_parse_version_spec',
    '_PluginSourceLoader', '_cards_executor',
    '_CONFIG_FILE_EXTS', '_CONFIG_FILE_NAMES', '_CODE_FILE_NAMES',
]

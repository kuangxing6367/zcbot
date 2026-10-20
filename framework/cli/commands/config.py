# -*- coding: utf-8 -*-
"""``zb config`` —— CLI 配置控制台（查看 / 修改 config.yaml）。

``set`` 采用按行原地替换，不动 YAML 里的注释（直接 yaml.dump 会把注释全冲掉）。
"""
import os
import re

from ..utils import PROG


def _print_config_item(key, value):
    print("  %-22s %s" % (key, value))


def do_config(args, working_dir):
    from framework import i18n
    from framework.config import get_home, load_config

    action = args.action or "list"
    path = os.path.join(working_dir, "config.yaml")

    try:
        cfg = load_config(path) or {}
    except Exception as e:  # noqa: BLE001
        print("读取配置失败: %s" % e)
        return 1

    if action in ("list", "show"):
        print("配置文件: %s" % path)
        print("%s 配置摘要" % PROG)
        print("-" * 46)
        _print_config_item("language", cfg.get("language", "zh"))
        db = cfg.get("database", {}) or {}
        if isinstance(db, str):
            _print_config_item("database", db)
        else:
            _print_config_item("database.type", db.get("type", "sqlite"))
        _print_config_item("onebot.listen_port",
                           (cfg.get("onebot", {}) or {}).get("listen_port"))
        _print_config_item("web.port", (cfg.get("web", {}) or {}).get("port"))
        print("-" * 46)
        print("可用语言: %s" % ", ".join(i18n.available_languages()))
        print("示例: %s config set language ciallo（重启后生效）" % PROG)
        return 0

    if action == "get":
        key = getattr(args, "key", None)
        if not key:
            print("用法: %s config get <key>" % PROG)
            return 2
        print("%s = %r" % (key, cfg.get(key)))
        return 0

    if action == "set":
        key = getattr(args, "key", None)
        value_parts = getattr(args, "value", None) or []
        if not key or not value_parts:
            print("用法: %s config set <key> <value>" % PROG)
            return 2
        value = " ".join(value_parts)
        if key == "language":
            norm = i18n.normalize(value)
            if norm != value.strip().lower():
                print("提示: 语言 %r 将按 %r 生效" % (value, norm))
        # 防路径穿越 / 注入：key 仅允许字母/数字/下划线/点，且不许以 . 开头。
        # 在校验文件存在性之前就拦截非法键，避免对可疑键做任何文件操作。
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", key):
            print("错误：非法配置键（仅允许字母/数字/下划线/点）: %r" % key)
            return 2
        if not os.path.isfile(path):
            print("配置文件不存在: %s（先启动一次让框架生成默认配置）" % path)
            return 1
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:  # noqa: BLE001
            print("读取失败: %s" % e)
            return 1
        # 只匹配「行首（可含空格）+ key:」，注释行以 # 开头不会被误伤
        pattern = re.compile(r"(?m)^([ \t]*" + re.escape(key) + r"[ \t]*:[ \t]*).*$")
        if pattern.search(text):
            new_text = pattern.sub(lambda m: m.group(1) + value, text, count=1)
        else:
            new_text = text.rstrip() + "\n%s: %s\n" % (key, value)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_text)
        except Exception as e:  # noqa: BLE001
            print("写入失败: %s" % e)
            return 1
        print("已设置 %s = %s（重启后生效）" % (key, value))
        return 0

    print("未知动作: %s" % action)
    print("可用: list / get <key> / set <key> <value>")
    return 2

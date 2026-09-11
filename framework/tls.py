# -*- coding: utf-8 -*-
"""
SSL/TLS 辅助。

把 config.yaml 顶层的 ssl 配置解析成服务端 SSLContext，供
Web 管理后台（HTTPS）与 OneBot 反向 WebSocket（WSS）共用。

配置形如::

    ssl:
      enabled: false
      cert: ""     # 证书链路径：绝对路径 或 相对项目根目录
      key: ""      # 私钥路径：绝对路径 或 相对项目根目录

证书路径支持**相对**与**绝对**两种写法：相对路径以 base_dir
（通常是 config.yaml 所在的项目根目录）为基准。
"""
import logging
import os
import ssl

logger = logging.getLogger('zcbot')


def resolve_ssl_path(path, base_dir):
    """把证书/私钥路径解析为绝对路径。

    绝对路径原样返回；相对路径相对 base_dir 拼接；空值返回 None。
    """
    if not path:
        return None
    path = str(path).strip()
    if not path:
        return None
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path))


def build_server_ssl_context(cfg, base_dir):
    """根据 ssl 配置构建服务端 SSLContext。

    :param cfg: ``config['ssl']``，形如 ``{enabled, cert, key}``
    :param base_dir: 相对路径的基准目录（一般为 config.yaml 所在目录）
    :return: ``ssl.SSLContext``；未启用时返回 ``None``
    :raises FileNotFoundError: 已启用但未配置证书/私钥，或文件不存在
    :raises ssl.SSLError: 证书/私钥无法加载
    """
    cfg = cfg or {}
    if not cfg.get('enabled'):
        return None

    cert = resolve_ssl_path(cfg.get('cert'), base_dir)
    key = resolve_ssl_path(cfg.get('key'), base_dir)
    if not cert or not key:
        raise FileNotFoundError("ssl.enabled=true 但未配置 ssl.cert / ssl.key")
    if not os.path.isfile(cert):
        raise FileNotFoundError(f"SSL 证书文件不存在: {cert}")
    if not os.path.isfile(key):
        raise FileNotFoundError(f"SSL 私钥文件不存在: {key}")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    return context

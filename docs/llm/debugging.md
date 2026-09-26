# 调试与测试（给 LLM）

## 跑测试

```bash
# pytest 用例(必须显式列文件)
python -m pytest tests/test_smoke.py tests/test_html_assembler.py -q
# 脚本式回归(直跑;模块级 sys.exit,被 pytest 收集会 INTERNALERROR)
python tests/test_plugin_imports.py   # 插件导入回归
python tests/test_dual_core.py        # 双进程
python tests/test_perm.py             # 权限引擎
python tests/test_smoke.py            # 冒烟(双模式兼容)
```

CI(`.github/workflows/tests.yml`):py3.10/3.11/3.12 矩阵 = 脚本回归 + pytest;
`build-zcbot-render.yml` 编译原生扩展(仅 native/** 变更);`deploy-docs.yml` 部署文档站。

## 症状 → 排查

| 症状 | 先查 |
| ---- | ---- |
| 插件没加载 | `core_plugins.yaml` 开关;日志「官方插件 [x] 已加载/加载失败」;目录里有 main.py |
| 命令不触发 | pattern 是否含正则元字符(决定前缀/正则);`is_dynamic=1` 不参与匹配;别名逗号分隔;插件 priority |
| 命令被吞 | 某插件 raw handler 返回 True 接管了(session 插件等待中的用户) |
| 回复发不出 | 无可用接入端;`adapter_for_source` 按事件来源选端,多端并存不串线 |
| 消息没日志 | `config.yaml → log.log_raw_message` |
| 内存堆积 | 事件缓冲 `EventBuffer.stats()`(l1/l3 字节、sqlite 积压、丢弃计数);workers 默认 1,慢 handler 会排队 |
| Web 500 | http_api 群管等走 `api.acall` 并查 `status`,失败不假报成功 |

## 日志

`data/logs/zcbot.log`;启动成功标志:`框架启动完成，等待事件...`;后台
`http://127.0.0.1:8080`(admin/admin123,首次必改密)看仪表盘/日志/在线改配置。

## 版本发布流程

提交 `release: vX.Y.Z`(同步 VERSION + pyproject.toml + README 版本行 + CHANGELOG)
→ 打同名 tag 推送 → `gh release create`。

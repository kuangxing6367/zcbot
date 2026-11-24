"""工作流编辑器（官方插件）：在 WebUI 侧边栏提供消息处理链的可视化配置页。

本身不保存数据，只注册侧边栏入口；所有读写走框架自带的 /api/workflows 系列
接口，复用同一套鉴权与审计。页面是完全自包含的 HTML/CSS/JS，不依赖闭源前端
产物的任何分包。
"""
import logging

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    'name': '工作流编辑器',
    'version': '1.0.0',
    'author': 'ZCBOT',
    'desc': '消息处理链：按 QQ 选工作流、节点卡片编排、插件顺序',
    'priority': 20,
    'official': True,
    'process': 'core',
}


def register(ctx):
    ctx.webui(title='工作流编辑器', entry='index.html', icon='◇',
              order=70, sidebar=True)
    ctx.log('[工作流编辑器] 已注册侧边栏入口')


def unregister():
    logger.info('工作流编辑器已卸载')

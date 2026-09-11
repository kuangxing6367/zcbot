import { defineConfig } from 'vitepress'

// ZCBOT 文档站配置
// 部署在 GitHub Pages，自定义域名：https://bot.zgric.top/（根路径，base 用 '/'）
// 若改回项目页托管（https://kuangxing6367.github.io/zcbot/），把 base 改成 '/zcbot/'
export default defineConfig({
  title: 'ZCBOT',
  description: '事件驱动的插件化服务宿主',
  lang: 'zh-CN',
  base: '/',
  cleanUrls: true,
  lastUpdated: true,

  // README.md 在 GitHub 上作为目录入口更好读，但站点需要 index.html
  rewrites: {
    'guide/README.md': 'guide/index.md'
  },

  head: [
    ['meta', { name: 'theme-color', content: '#3c8772' }]
  ],

  themeConfig: {
    nav: [
      { text: '指南', link: '/guide/', activeMatch: '/guide/' },
      { text: 'API', link: '/api/', activeMatch: '/api/' },
      { text: '进阶', link: '/advanced/architecture', activeMatch: '/advanced/' },
      { text: '更新日志', link: 'https://github.com/kuangxing6367/zcbot/blob/main/CHANGELOG.md' },
      { text: 'GitHub', link: 'https://github.com/kuangxing6367/zcbot' }
    ],

    sidebar: {
      '/guide/': [
        {
          text: '入门',
          items: [
            { text: '文档总入口', link: '/guide/' },
            { text: '安装', link: '/guide/installation' },
            { text: '开始使用', link: '/guide/getting-started' },
            { text: '对接 IM 平台', link: '/guide/connect-im' },
            { text: '配置系统', link: '/guide/configuration' }
          ]
        },
        {
          text: '开发插件',
          items: [
            { text: '编写插件', link: '/guide/writing-plugins' },
            { text: '多轮会话', link: '/guide/session' },
            { text: '最佳实践', link: '/guide/best-practices' }
          ]
        }
      ],
      '/api/': [
        {
          text: '基础参考',
          items: [
            { text: 'API 总览', link: '/api/' },
            { text: 'PluginContext (ctx)', link: '/api/basic/ctx' },
            { text: 'Event 事件对象', link: '/api/basic/event' },
            { text: 'Framework 核心', link: '/api/basic/framework' },
            { text: '服务注册表（DI）', link: '/api/basic/services' }
          ]
        },
        {
          text: '进阶扩展',
          items: [
            { text: '扩展点（Hook 系统）', link: '/api/advanced/hooks' },
            { text: '协议适配器', link: '/api/advanced/protocol_adapter' }
          ]
        }
      ],
      '/advanced/': [
        {
          text: '进阶主题',
          items: [
            { text: '架构总览', link: '/advanced/architecture' },
            { text: '插件加载与模块机制', link: '/advanced/loader' },
            { text: '数据库', link: '/advanced/database' },
            { text: '权限系统', link: '/advanced/permission' },
            { text: '定时任务', link: '/advanced/scheduler' },
            { text: '部署上线', link: '/advanced/deployment' },
            { text: '双核心架构（实验）', link: '/advanced/dual-core' }
          ]
        }
      ]
    },

    outline: { label: '本页目录', level: [2, 3] },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/kuangxing6367/zcbot' }
    ],

    search: {
      provider: 'local'
    },

    footer: {
      message: '基于 MIT + Apache 2.0 双协议发布',
      copyright: 'Copyright © 2026 ZCBOT'
    },

    docFooter: {
      prev: '上一篇',
      next: '下一篇'
    },

    lastUpdatedText: '最后更新于',
    darkModeSwitchLabel: '主题',
    sidebarMenuLabel: '菜单',
    returnToTopLabel: '回到顶部'
  }
})

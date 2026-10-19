import { defineConfig } from 'vitepress'

// ZCBOT 文档站配置
// 部署在 GitHub Pages，自定义域名：https://bot.zgric.top/（根路径，base 用 '/'）
// 若改回项目页托管（https://kuangxing6367.github.io/zcbot/），把 base 改成 '/zcbot/'
export default defineConfig({
  title: 'ZCBOT',
  description: '事件驱动的 IM 平台',
  lang: 'zh-CN',
  base: '/',
  cleanUrls: true,
  lastUpdated: true,

  // 目录即导航：文档按 指南/ 接口/ 进阶/ 品牌/ 速查/ 分目录存放，
  // 每节都有真实的 index.md，不再依赖 rewrites 把平铺文件"虚拟"成目录页。

  head: [
    ['link', { rel: 'icon', type: 'image/x-icon', href: '/favicon.ico' }],
    ['link', { rel: 'apple-touch-icon', href: '/apple-touch-icon.png' }],
    ['meta', { name: 'theme-color', content: '#6366F1' }]
  ],

  themeConfig: {
    // 导航栏标志：亮底 #6366F1 / 暗底 #818CF8，随站点主题自动切换
    logo: {
      light: '/brand/zcbot-icon-light.svg',
      dark: '/brand/zcbot-icon-dark.svg',
      alt: 'zcbot'
    },

    nav: [
      { text: '指南', link: '/指南/', activeMatch: '/指南/' },
      { text: '接口', link: '/接口/', activeMatch: '/接口/' },
      { text: '进阶', link: '/进阶/', activeMatch: '/进阶/' },
      { text: '速查', link: '/速查/', activeMatch: '/速查/' },
      { text: '品牌标识', link: '/品牌/', activeMatch: '/品牌/' },
      { text: '更新日志', link: 'https://gitee.com/liquanfeng123/zcbot/blob/main/CHANGELOG.md' },
      { text: 'Gitee', link: 'https://gitee.com/liquanfeng123/zcbot' }
    ],

    sidebar: {
      '/指南/': [
        {
          text: '入门',
          items: [
            { text: '指南总览', link: '/指南/' },
            { text: '安装', link: '/指南/安装' },
            { text: '快速开始', link: '/指南/快速开始' },
            { text: '对接 IM 平台', link: '/指南/对接IM平台' },
            { text: '配置系统', link: '/指南/配置系统' },
            { text: '多语言', link: '/指南/多语言' }
          ]
        },
        {
          text: '开发插件',
          items: [
            { text: '编写插件', link: '/指南/编写插件' },
            { text: '多轮会话', link: '/指南/多轮会话' },
            { text: '接入大模型（LLM）', link: '/指南/接入大模型' },
            { text: '接入端契约与规范消息', link: '/指南/接入端契约' },
            { text: '最佳实践', link: '/指南/最佳实践' }
          ]
        }
      ],
      '/接口/': [
        {
          text: '基础',
          items: [
            { text: '接口总览', link: '/接口/' },
            { text: '插件上下文（ctx）', link: '/接口/基础/插件上下文' },
            { text: '事件对象', link: '/接口/基础/事件对象' },
            { text: '核心框架', link: '/接口/基础/核心框架' },
            { text: '服务注册表（DI）', link: '/接口/基础/服务注册表' }
          ]
        },
        {
          text: '高级',
          items: [
            { text: '插件装饰器 API', link: '/接口/高级/插件装饰器' },
            { text: '扩展点（Hook 系统）', link: '/接口/高级/扩展点Hook' },
            { text: '协议适配器', link: '/接口/高级/协议适配器' }
          ]
        }
      ],
      '/进阶/': [
        {
          text: '进阶主题',
          items: [
            { text: '进阶总览', link: '/进阶/' },
            { text: '内核设计哲学', link: '/进阶/设计哲学' },
            { text: '架构总览', link: '/进阶/架构总览' },
            { text: '插件加载与模块机制', link: '/进阶/插件加载机制' },
            { text: '数据库', link: '/进阶/数据库' },
            { text: '定时任务', link: '/进阶/定时任务' },
            { text: '权限系统', link: '/进阶/权限系统' },
            { text: '接口令牌（API Key）', link: '/进阶/接口令牌' },
            { text: '部署上线', link: '/进阶/部署上线' },
            { text: '双核心架构（实验）', link: '/进阶/双核心架构' },
            { text: '项目结构', link: '/进阶/项目结构' },
            { text: '扩展清单', link: '/进阶/扩展清单' }
          ]
        }
      ],
      '/速查/': [
        {
          text: '给 AI 编码助手（LLM）',
          items: [
            { text: '速查总览', link: '/速查/' },
            { text: '框架结构', link: '/速查/框架结构' },
            { text: '插件开发', link: '/速查/插件开发' },
            { text: '调试与测试', link: '/速查/调试排错' },
            { text: 'IIPC 内核链路', link: '/速查/IIPC内核链路' },
            { text: '安全加固', link: '/速查/安全加固' }
          ]
        }
      ],
      '/品牌/': [
        {
          text: '品牌标识',
          items: [
            { text: '标志与规范', link: '/品牌/' }
          ]
        }
      ]
    },

    outline: { label: '本页目录', level: [2, 3] },

    socialLinks: [
      { icon: 'github', link: 'https://gitee.com/liquanfeng123/zcbot' }
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

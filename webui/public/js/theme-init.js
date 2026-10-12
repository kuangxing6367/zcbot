/*
 * 首屏主题初始化（外部脚本，避免 CSP 拦截内联脚本）
 * 在应用挂载前同步应用亮/暗主题与底色，消除首屏白闪。
 */
(function () {
  try {
    var t = localStorage.getItem('zcbot_theme')
    if (t !== 'light' && t !== 'dark') {
      t = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
        ? 'light'
        : 'dark'
    }
    var el = document.documentElement
    if (t === 'dark') {
      el.classList.add('dark')
    } else {
      el.classList.remove('dark')
    }
    el.style.colorScheme = t
    el.style.backgroundColor = t === 'dark' ? '#070707' : '#fafbfc'
  } catch (e) {
    /* 忽略：主题初始化失败不应阻断应用 */
  }
})()

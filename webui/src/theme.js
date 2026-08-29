import { ref, watch } from 'vue'

const THEME_KEY = 'zcbot_theme'
const theme = ref(localStorage.getItem(THEME_KEY) || (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'))

function apply(t) {
  const el = document.documentElement
  if (t === 'dark') {
    el.classList.add('dark')
  } else {
    el.classList.remove('dark')
  }
  el.style.colorScheme = t
  localStorage.setItem(THEME_KEY, t)
}

function toggleTheme() {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
  apply(theme.value)
}

function initTheme() {
  apply(theme.value)
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (e) => {
    if (!localStorage.getItem(THEME_KEY)) {
      theme.value = e.matches ? 'light' : 'dark'
      apply(theme.value)
    }
  })
}

initTheme()

export { theme, toggleTheme }
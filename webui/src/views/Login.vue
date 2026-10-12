<template>
  <div class="login-wrap">
    <!-- 品牌侧（宽屏显示） -->
    <div class="login-hero">
      <div class="hero-inner">
        <div class="hero-logo">
          <img :src="heroLogo" alt="ZCBOT" />
          <span>ZCBOT</span>
        </div>
        <h2 class="hero-title">事件驱动 IM 平台</h2>
        <p class="hero-desc">统一管理插件、命令、用户与接入端<br />一个后台，掌控全部消息链路</p>
        <ul class="hero-points">
          <li>插件热加载 · 多协议接入</li>
          <li>细粒度权限 · 全量审计</li>
          <li>蜜罐诱饵 · 安全加固</li>
        </ul>
      </div>
      <div class="hero-glow g1"></div>
      <div class="hero-glow g2"></div>
    </div>

    <!-- 表单侧 -->
    <div class="login-panel">
      <div class="login-card">
        <div class="login-logo">
          <img :src="logoUrl" alt="ZCBOT" class="login-logo-img" />
        </div>
        <h1 class="login-title">欢迎回来</h1>
        <div class="login-sub">登录 ZCBOT 管理面板</div>

        <el-form :model="form" @submit.prevent="onLogin" size="large">
          <el-form-item>
            <el-input v-model="form.username" placeholder="用户名" autocomplete="username" :prefix-icon="User" />
          </el-form-item>
          <el-form-item>
            <el-input v-model="form.password" type="password" placeholder="密码" show-password
                      autocomplete="current-password" :prefix-icon="Lock" @keyup.enter="onLogin" />
          </el-form-item>
          <el-button type="primary" class="login-btn" native-type="submit" :loading="loading">
            登 录
          </el-button>
        </el-form>

        <div class="login-ver">
          <span v-if="ver">ZCBOT <span class="mono">v{{ ver.version }}</span>
            <el-tag v-if="ver.alpha" type="warning" size="small" effect="plain" style="margin-left:6px">Alpha 预览版</el-tag>
          </span>
          <span v-else>ZCBOT</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { User, Lock } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed } from 'vue'
import { api, setToken, session, secureHeaders } from '../api'
import { API_BASE } from '../config'
import { theme } from '../theme'

const router = useRouter()
const route = useRoute()
const form = ref({ username: '', password: '' })
const loading = ref(false)
const ver = ref(null)
// 标志按主题切换明暗两版：亮底用 #5D87FF，暗底提亮
const logoUrl = computed(() => import.meta.env.BASE_URL + (theme.value === 'dark' ? 'img/zcbot-icon-dark.svg' : 'img/zcbot-icon-light.svg'))
const heroLogo = computed(() => import.meta.env.BASE_URL + 'img/zcbot-icon-mini-light.svg')

async function onLogin() {
  if (!form.value.username || !form.value.password) {
    ElMessage.warning('请填写用户名和密码')
    return
  }
  loading.value = true
  try {
    const res = await fetch(API_BASE + '/api/login', {
      method: 'POST',
      headers: await secureHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(form.value),
    })
    const data = await res.json().catch(() => ({}))
    if (res.ok && data.code === 0) {
      setToken(data.data.token)
      ElMessage.success('登录成功')
      const redirect = route.query.redirect
      router.push(redirect || '/dashboard')
    } else {
      ElMessage.error(data.msg || ('登录失败 (' + res.status + ')'))
    }
  } catch (err) {
    ElMessage.error('网络错误: ' + err.message)
  } finally {
    loading.value = false
  }
}

async function checkExisting() {
  if (!localStorage.getItem('zcbot_token')) return
  try {
    const me = await api('/api/me')
    if (me && me.code === 0) {
      session.admin = me.data
      router.push('/dashboard')
    }
  } catch (e) { /* ignore */ }
}

onMounted(async () => {
  checkExisting()
  try {
    const r = await fetch(API_BASE + '/api/version', { headers: await secureHeaders() }).then(r => r.json())
    if (r && r.code === 0) ver.value = r.data
  } catch (e) { /* ignore */ }
})
</script>

<style scoped>
.login-wrap {
  height: 100vh;
  display: flex;
  background: var(--default-bg-color);
}

/* ------------------------------ 品牌侧 ------------------------------ */
.login-hero {
  position: relative;
  flex: 1 1 46%;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background: linear-gradient(150deg, #4a6fe0 0%, #5d87ff 45%, #38bdf8 100%);
  color: #fff;
}
.hero-inner { position: relative; z-index: 2; padding: 48px; max-width: 440px; }
.hero-logo { display: flex; align-items: center; gap: 10px; font-size: 22px; font-weight: 800; letter-spacing: .02em; }
.hero-logo img { width: 34px; height: 34px; object-fit: contain; }
.hero-title { font-size: 30px; font-weight: 700; margin: 28px 0 14px; line-height: 1.25; }
.hero-desc { font-size: 15px; line-height: 1.8; opacity: .88; margin: 0 0 26px; }
.hero-points { list-style: none; padding: 0; margin: 0; }
.hero-points li {
  position: relative;
  padding-left: 20px;
  margin-bottom: 12px;
  font-size: 14px;
  opacity: .92;
}
.hero-points li::before {
  content: '';
  position: absolute;
  left: 0;
  top: 8px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: rgba(255, 255, 255, .9);
}
.hero-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(60px);
  opacity: .45;
}
.hero-glow.g1 { width: 320px; height: 320px; background: #a5beff; top: -80px; right: -60px; }
.hero-glow.g2 { width: 260px; height: 260px; background: #7dd3fc; bottom: -70px; left: -50px; }

/* ------------------------------ 表单侧 ------------------------------ */
.login-panel {
  flex: 1 1 54%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.login-card {
  width: 380px;
  background: var(--default-box-color);
  border: 1px solid var(--art-card-border);
  border-radius: calc(var(--custom-radius) + 6px);
  padding: 40px 36px 26px;
  box-shadow: 0 12px 40px -12px rgba(93, 135, 255, .18);
}
.login-logo { text-align: center; margin-bottom: 14px; }
.login-logo-img { width: 58px; height: 58px; object-fit: contain; }
.login-title { text-align: center; font-size: 22px; font-weight: 700; margin: 0 0 6px; }
.login-sub { text-align: center; color: var(--el-text-color-secondary); font-size: 13px; margin-bottom: 26px; }
.login-btn { width: 100%; height: 42px; font-size: 15px; }
.login-ver { text-align: center; margin-top: 22px; color: var(--el-text-color-secondary); font-size: 13px; }

@media (max-width: 860px) {
  .login-hero { display: none; }
}
</style>

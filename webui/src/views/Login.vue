<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="login-logo">
        <el-icon :size="40" color="#409eff"><Cpu /></el-icon>
      </div>
      <h1 class="login-title">ZCBOT 管理面板</h1>
      <div class="login-sub">OneBot QQ 机器人统一管理平台</div>

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
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { User, Lock, Cpu } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { api, setToken, session } from '../api'

const router = useRouter()
const route = useRoute()
const form = ref({ username: '', password: '' })
const loading = ref(false)
const ver = ref(null)

async function onLogin() {
  if (!form.value.username || !form.value.password) {
    ElMessage.warning('请填写用户名和密码')
    return
  }
  loading.value = true
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
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
    const r = await fetch('/api/version').then(r => r.json())
    if (r && r.code === 0) ver.value = r.data
  } catch (e) { /* ignore */ }
})
</script>

<style scoped>
.login-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1f2b46 0%, #243253 40%, #1a2338 100%);
}
.login-card {
  width: 380px;
  background: var(--el-bg-color);
  border-radius: 16px;
  padding: 40px 36px 28px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, .35);
}
.login-logo { text-align: center; margin-bottom: 8px; }
.login-title { text-align: center; font-size: 22px; margin: 0 0 4px; }
.login-sub { text-align: center; color: var(--el-text-color-secondary); font-size: 13px; margin-bottom: 28px; }
.login-btn { width: 100%; }
.login-ver { text-align: center; margin-top: 22px; color: var(--el-text-color-secondary); font-size: 13px; }
</style>
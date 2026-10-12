/*
 * 前端运行期配置
 * ==============
 * __ZC_API_BASE__ 由服务端 /webui-config.js 注入（端口隔离模式下为 API 端口地址）。
 * 未启用端口隔离时为空串，所有请求仍走当前站点（与旧行为完全一致）。
 *
 * 说明：这是外部同源脚本，不受 CSP 的 script-src 'self' 限制；
 * 若用内联 <script> 赋值会被 CSP 拦截，所以必须由 /webui-config.js 提供。
 */
export const API_BASE =
  (typeof window !== 'undefined' && window.__ZC_API_BASE__) || ''

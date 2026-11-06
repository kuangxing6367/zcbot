/*
 * 受控诱饵文件（honeytoken），内容全是假值，禁止填入任何真实凭据。
 *
 * 不参与前端构建、不被 index.html 加载；用途仅为：
 *   1) 蜜罐启用：/js/local.debug.js 被陷阱接管，返回内置假凭据并拉黑探测者；
 *   2) 蜜罐禁用：静态路由对该路径固定返回 404，不直出本文件；
 *   3) 文件浏览器把它列入敏感名，普通管理员读取一律 403。
 * 下方 IP / 账号 / 口令 / jwt_secret 均为占位示例，勿用于任何真实环境。
 */
window.__ZC_LOCAL__ = {
  api: 'http://10.20.30.15:8080/api',
  ws: 'ws://10.20.30.15:6830',
  mysql: 'mysql://zcbot:Zc0t_Pr0d_2024@10.20.30.15:3306/zcbot?charset=utf8mb4',
  redis: 'redis://:r3dis_pw_2024@10.20.30.16:6379/0',
  upload_dir: '/data/zcbot/uploads',
  jwt_secret: 'zc_HS256_2b7f9c4e1a8d5f3b6e0c9a7d4f2b8e1c',
  admin: { user: 'admin', pass: 'Zcbot@2024' },
  debug: true,
}

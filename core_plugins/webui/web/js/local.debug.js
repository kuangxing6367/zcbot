/*
 * 本地联调配置（临时）
 * TODO: 上线前删除 —— 迁移到 .env.local
 * 最后修改: 2024-11-xx
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

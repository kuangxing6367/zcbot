# 第 13 轮侦察材料：framework/api/users_groups.py 第 1~130 行

只把本文件当作线索来源。你可以用 Grep 在仓库里搜相关调用点，但**不要 Read 任何超过 150 行的文件**。

```
   1 """
   2 用户 / 群管理
   3 """
   4 import logging
   5 
   6 from flask import jsonify, request
   7 
   8 logger = logging.getLogger('zcbot')
   9 
  10 
  11 def register(ctx):
  12     app = ctx.app
  13     db = ctx.db
  14     require_auth = ctx.require_auth
  15     require_super = ctx.require_super
  16     audit_log = ctx.audit_log
  17 
  18     # ---- 用户管理 ----
  19 
  20     @app.route('/api/users', methods=['GET'])
  21     @require_auth
  22     def list_users():
  23         """获取用户列表（含角色信息）"""
  24         page = int(request.args.get('page', 1))
  25         size = min(int(request.args.get('size', 50)), 200)
  26         offset = (page - 1) * size
  27         keyword = request.args.get('keyword', '').strip()
  28 
  29         try:
  30             if keyword:
  31                 rows = db.query(
  32                     "SELECT id, user_id, nickname, is_friend, is_blacklist, remark, role, "
  33                     "first_seen_at, last_active_at FROM users "
  34                     "WHERE nickname LIKE %s OR user_id LIKE %s OR remark LIKE %s "
  35                     "ORDER BY last_active_at DESC LIMIT %s OFFSET %s",
  36                     (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%', size, offset)
  37                 )
  38                 total_row = db.query_one(
  39                     "SELECT COUNT(*) as cnt FROM users "
  40                     "WHERE nickname LIKE %s OR user_id LIKE %s OR remark LIKE %s",
  41                     (f'%{keyword}%', f'%{keyword}%', f'%{keyword}%')
  42                 )
  43             else:
  44                 rows = db.query(
  45                     "SELECT id, user_id, nickname, is_friend, is_blacklist, remark, role, "
  46                     "first_seen_at, last_active_at FROM users "
  47                     "ORDER BY last_active_at DESC LIMIT %s OFFSET %s",
  48                     (size, offset)
  49                 )
  50                 total_row = db.query_one("SELECT COUNT(*) as cnt FROM users")
  51 
  52             return jsonify({
  53                 'code': 0,
  54                 'data': rows,
  55                 'total': total_row['cnt'] if total_row else 0,
  56                 'page': page,
  57                 'size': size
  58             })
  59         except Exception as e:
  60             logger.error(f"list_users 内部错误: {e}")
  61             return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500
  62 
  63     @app.route('/api/users', methods=['POST'])
  64     @require_super
  65     def create_user():
  66         """手动新增机器人用户（仅超管）"""
  67         admin = request.admin
  68         data = request.get_json(silent=True) or {}
  69         user_id = str(data.get('user_id') or '').strip()
  70         if not user_id:
  71             return jsonify({'code': 400, 'msg': 'user_id 不能为空'}), 400
  72         try:
  73             if db.query_one("SELECT id FROM users WHERE user_id = %s", (user_id,)):
  74                 return jsonify({'code': 400, 'msg': '该用户已存在'}), 400
  75             db.execute(
  76                 "INSERT INTO users (user_id, nickname, remark, first_seen_at) "
  77                 "VALUES (%s, %s, %s, NOW())",
  78                 (user_id, str(data.get('nickname') or '')[:100],
  79                  str(data.get('remark') or '')[:200]))
  80             audit_log(admin['id'], admin['username'], 'create_user', 'user', user_id)
  81             return jsonify({'code': 0, 'msg': '用户已添加'})
  82         except Exception as e:
  83             logger.error(f"create_user 内部错误: {e}")
  84             return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500
  85 
  86     @app.route('/api/users/<int:user_id>', methods=['PUT'])
  87     @require_super
  88     def update_user(user_id):
  89         """修改机器人用户的昵称/备注（仅超管）"""
  90         admin = request.admin
  91         data = request.get_json(silent=True) or {}
  92         try:
  93             if not db.query_one("SELECT id FROM users WHERE user_id = %s", (user_id,)):
  94                 return jsonify({'code': 404, 'msg': '用户不存在'}), 404
  95             db.execute(
  96                 "UPDATE users SET nickname=%s, remark=%s WHERE user_id=%s",
  97                 (str(data.get('nickname') or '')[:100],
  98                  str(data.get('remark') or '')[:200], user_id))
  99             audit_log(admin['id'], admin['username'], 'update_user', 'user', str(user_id))
 100             return jsonify({'code': 0, 'msg': '用户已更新'})
 101         except Exception as e:
 102             logger.error(f"update_user 内部错误: {e}")
 103             return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500
 104 
 105     @app.route('/api/users/<int:user_id>', methods=['DELETE'])
 106     @require_super
 107     def delete_user(user_id):
 108         """删除机器人用户（仅超管）"""
 109         admin = request.admin
 110         try:
 111             db.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
 112             audit_log(admin['id'], admin['username'], 'delete_user', 'user', str(user_id))
 113             return jsonify({'code': 0, 'msg': '用户已删除'})
 114         except Exception as e:
 115             logger.error(f"delete_user 内部错误: {e}")
 116             return jsonify({'code': 500, 'msg': '服务器内部错误'}), 500
 117 
 118     @app.route('/api/users/<int:user_id>/role', methods=['PUT'])
 119     @require_super
 120     def set_user_role(user_id):
 121         """设置用户角色（仅超级管理员）"""
 122         admin = request.admin
 123         data = request.get_json(silent=True) or {}
 124         role = data.get('role', '').strip()
 125 
 126         if role not in ('super', ''):
 127             return jsonify({'code': 400, 'msg': '角色值无效，仅支持 super 或空字符串'}), 400
 128 
 129         try:
 130             row = db.query_one("SELECT id, nickname FROM users WHERE user_id = %s", (user_id,))
```

# 多轮会话

ZCBOT 内置多轮会话管理器，让插件可以轻松实现交互式对话。

## 基本用法

### `ctx.wait_for()` — 等待用户回复

```python
async def handle_survey(event, match):
    # 发送提示并等待回复
    name = await ctx.wait_for(event, prompt="你叫什么名字？", timeout=60)
    
    if name is None:
        await ctx.asend_msg(..., message="超时了，请重新发起")
        return
    
    age = await ctx.wait_for(event, prompt="你的年龄是？", timeout=60)
    
    await ctx.asend_msg(..., message=f"你好 {name}，{age} 岁")
```

### 参数说明

```python
await ctx.wait_for(
    event,           # 当前事件对象（必须）
    prompt="提示语",  # 可选，自动发送的提示消息
    timeout=60,      # 超时秒数，默认 60
    handler=None,    # 可选，过滤函数
)
```

**返回值**：
- 用户回复的原始 `dict`（含 `message`、`user_id` 等）
- 超时返回 `None`

## 高级用法

### `ctx.create_session()` — 会话对象

```python
async def handle_quiz(event, match):
    async with ctx.create_session(event, timeout=120) as sess:
        # sess.ask() 发送提示并等待回复
        q1 = await sess.ask("1+1=?")
        q1_text = q1.get('message', '') if q1 else ''
        sess.data['q1'] = q1_text
        
        q2 = await sess.ask("2+2=?")
        q2_text = q2.get('message', '') if q2 else ''
        sess.data['q2'] = q2_text
        
        # sess.data 持有整个会话的上下文
        await ctx.asend_msg(
            ...,
            message=f"你回答了: {sess.data['q1']}, {sess.data['q2']}"
        )
```

### Session 对象 API

| 方法/属性 | 说明 |
|-----------|------|
| `sess.ask(prompt, timeout=None)` | 发送提示并等待回复 |
| `sess.wait(timeout=None)` | 等待下一条消息（不发提示） |
| `sess.data` | 会话数据字典，可存储任意内容 |
| `sess.close()` | 关闭会话 |

## 自定义过滤器

通过 `handler` 参数自定义哪些消息被会话接受：

```python
async def handle_number(event, match):
    def is_number(raw):
        """只接受纯数字消息"""
        text = raw.get('message', '')
        return text.strip().isdigit()
    
    reply = await ctx.wait_for(
        event,
        prompt="请输入一个数字：",
        timeout=60,
        handler=is_number
    )
    
    if reply:
        num = int(reply['message'].strip())
        await ctx.asend_msg(..., message=f"你输入了 {num}")
```

## 会话超时处理

```python
async def handle_order(event, match):
    reply = await ctx.wait_for(event, prompt="请选择商品：", timeout=30)
    
    if reply is None:
        await ctx.asend_msg(..., message="选择超时，订单已取消")
        return
    
    # 处理用户选择...
```

## 完整示例：问卷调查

```python
__plugin_meta__ = {
    "name": "问卷调查",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "多轮对话问卷",
    "priority": 50,
}

def register(ctx):
    ctx.command("/问卷", handle_survey, description="参与问卷调查")

async def handle_survey(event, match):
    async with ctx.create_session(event, timeout=120) as sess:
        # 问题 1：姓名
        r = await sess.ask("1. 你的名字是？")
        sess.data['name'] = r.get('message', '') if r else '未知'
        
        # 问题 2：年龄
        r = await sess.ask("2. 你的年龄是？")
        sess.data['age'] = r.get('message', '') if r else '未知'
        
        # 问题 3：城市
        r = await sess.ask("3. 你在哪个城市？")
        sess.data['city'] = r.get('message', '') if r else '未知'
        
        # 问题 4：评价
        r = await sess.ask("4. 对 ZCBOT 的评价（1-5分）？")
        sess.data['score'] = r.get('message', '') if r else '未评分'
        
        # 保存到数据库
        await ctx.db_execute_async(
            "INSERT INTO survey (user_id, name, age, city, score) "
            "VALUES (%s, %s, %s, %s, %s)",
            (event.user_id, sess.data['name'], sess.data['age'],
             sess.data['city'], sess.data['score'])
        )
        
        # 发送总结
        summary = (
            f"问卷完成！\n"
            f"姓名: {sess.data['name']}\n"
            f"年龄: {sess.data['age']}\n"
            f"城市: {sess.data['city']}\n"
            f"评价: {sess.data['score']}"
        )
        await ctx.asend_msg(..., message=summary)
```

## 与旧插件兼容

旧的 `session_waiter` 插件仍可使用，但推荐迁移到内置会话管理器：

```python
# 旧方式（仍可用）
import sys
sw = sys.modules.get("plugin_session_waiter")
reply = await sw.wait_for_user(ctx, session_id=sw.make_session_id(event))

# 新方式（推荐）
reply = await ctx.wait_for(event, timeout=60)
```

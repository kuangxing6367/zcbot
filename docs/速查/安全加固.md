# ZCBOT 安全加固：API / 前端抗探测与蜜罐体系

> 适用版本 v1.8.9。本文是 LLM 按需加载文档之一，**只讲防御层**，不涉及框架内核语义。

## 定位说明（先读这段）

- **对象**：本文讲的是 ZCBOT 管理面板（`framework/api/`）的**抗自动化探测 / 抗抓包暴露 / 蜜罐反制**体系，属于「防御层」，不是框架内核语义（事件流、插件 API、权限模型那是另一套，见 `docs/llm-framework.md`）。
- **形态**：这是 v1.8.9 叠在原有 Web 鉴权之上的一层。与 IIPC（`docs/llm-iipc.md`）、插件 API **正交**——不改任何框架语义，只在 Web 入口与前端产物上做加固。
- **数字不可横向比**：蜜罐 / 限速 / 诱饵的度量属于「防御有效性」（攻击者被拦率、诱饵命中率），不是性能或功能指标。文中的阈值（限速容量、票据用量上限）全是**可调配置**，不是固定事实；改了配置数字就变，不要当成版本常量。
- **术语对齐**：
  - **诱饵（decoy）**：主动混入正常流量的假端点，目的是让抓包流量真假难分。
  - **蜜罐（honeypot）**：服务端陷阱路由，命中即取证 + 拉黑。
  - **PoW 票据**：人机验证（工作量证明）通过后签发的通行票据，此前是「买断制」，本文改为「计量制」。

## 背景：四个被实测坐实的问题

下面四个问题不是读代码推断，是用户在真浏览器抓包 + 黑盒压测中复现后指出的，已逐项源码核实：

| # | 问题 | 源码核实结论 |
|---|------|--------------|
| 1 | 高并发不拦，可秒刷 | 138 个 `/api/*` 端点仅 2 个有限速（`/api/login`、`/api/version`），其余零限速 |
| 2 | PoW 票据买断制 | `human_check.is_valid_ticket` 只查字典存在性，**不绑 IP、不计数、不消耗**，一次验证无限复用 |
| 3 | 诱饵命名 `/api/v1/` 但真实端点零 v1 | 一条 `/api/v1/` 正则就能把整张诱饵表筛掉，deception 归零 |
| 4 | 诱饵前端从不调用 | 诱饵全是静态路由，真实前端零调用 → 浏览器抓包只见真端点，诱饵等于零 |

## 加固后的三大地基

### 1. 全局限速闸门（`framework/api/ratelimit.py` + `webapp._global_rate_guard`）

- **三机制并行**：每 IP 滑动窗口 + 全局令牌桶（QPS 上限）+ 路由分档（认证/挑战接口更严）。
- **覆盖范围**：`before_request` 链里覆盖**全部** `/api/*`（此前仅 2 个端点有限速）。
- **超限行为**：返回 `429` + `Retry-After` 头。
- **配置键**：`security.rate_limit`（见下方配置段）。
- **默认 `whitelist_bypass=true`**：本机 / 内网 IP 豁免。后果：你本地 `127.0.0.1` 压测看不到拦截；要现场验证高并发拦截，要么从非内网 IP 打，要么临时把 `whitelist_bypass` 设为 `false`。

### 2. PoW 票据计量化（`framework/api/human_check.py`）

- **绑定签发 IP**：ticket 记录签发时客户端 IP，校验时比对 `remote_ip`，跨 IP 即作废（防票据轮转盗用）。
- **单次签发限速**：同一 IP 签发有速率上限，防农场批量刷票。
- **用量计数**：每次有效使用计数，超过上限（默认 200）作废，需重新解 PoW。
- `is_valid_ticket(ticket, remote_ip=...)` 增加 `remote_ip` 参数；不传或传错 IP 直接失效。

### 3. 诱饵真实化（`framework/api/honeypot.py`）

- **命名去 `/api/v1/` → `/api/`**：与真实端点同构（真实端点零 v1），避免一条正则废整张表。
- **陷阱 token 长度 `512 → 2048`**：对齐真实会话令牌 `len==2048`（实测 `webapp.py` 会话令牌为 `token_hex(1024)`），假令牌不再一眼假。
- **新增「前端主动调用型诱饵」** `_FRONTEND_DECOYS`：`/api/internal/{telemetry,bootstrap,feature_flags,debug-config}`——GET 良性返回、非 GET 视为利用即拉黑。这是让诱饵真正生效的关键（见下节）。

## 前端调用型诱饵（关键：让抓包流量混入真诱饵）

> 这是用户最在意的改动点：此前诱饵全是服务端静态路由，前端从不碰，所以浏览器一抓包，看到的只有真端点，deception 等于零。

- **新增 `webui/public/js/zc-decoy.js`**：前端主动 `GET` 上述四个伪端点。真实浏览器加载页面时就会发出这些请求，混入正常流量。
- **`webui/index.html` 引入**；Vite `build` 后落到 `core_plugins/webui/web/js/zc-decoy.js`（构建时 public 资源原样拷贝，且**不进 obfuscator**——故意保留明文路径字符串，供做源码审计的扫描器发现）。
- **受控 honeytoken**：`zc-decoy.js` 源码埋入 `/js/local.debug.js` 路径（用户仓库既有的一份明文假凭据文件，此前是死诱饵），扫描器 `fetch` 该路径即被蜜罐标记并拉黑。
- **设计要点**：真实前端会调用 → 抓包器看到的是「真端点 + 真诱饵」混合流，无法纯靠黑盒把诱饵筛掉。

## 配置项（`config.yaml`）

```yaml
security:
  rate_limit:
    enabled: true
    per_ip: 60              # 每 IP 滑动窗口容量
    window_sec: 10          # 滑动窗口时间窗口（秒）
    global_qps: 400         # 全局令牌桶：全站每秒允许请求数
    strict_paths:           # 更严的路由（认证 / 挑战接口）
      - /api/login
      - /api/human-check/verify
    strict_per_ip: 10
    strict_window_sec: 10
    whitelist_bypass: true   # 内网 / 回环 IP 豁免限速（默认开）
```

## 测试与验证（`tools/security_check.py`，15 断言全绿）

- 全局限速闸门：高并发必被拦（429）。
- 票据计量化：跨 IP 失效、用量耗尽失效、伪造票据无效、同 IP 有效。
- 诱饵真实化：命名无 `/api/v1/`、陷阱 token `len==2048`。
- 前端 decoy：端点可达且 GET 良性返回。

运行：

```bash
python tools/security_check.py
```

## 部署注意

- **限速默认内网豁免**：现场验证高并发拦截要从非内网 IP 打，或临时 `security.rate_limit.whitelist_bypass: false`。
- **蜜罐拉黑 TTL + 内网豁免**：本地 `127.0.0.1` 不会触发拉黑，这是预期。
- **前端 decoy 在 build 产物里**：改 decoy 端点清单必须**同步两处**——`honeypot._FRONTEND_DECOYS`（服务端）与 `webui/public/js/zc-decoy.js`（前端调用路径），否则前端会打到 404。

## 与既有蜜罐体系的关系

- 本文所有诱饵**复用 `framework/api/honeypot.py`**（用户既有 1500+ 行级蜜罐：tarpit、canary 注入、`_hit` 取证统计、拉黑 + TTL + 内网豁免、evasion 检测、噪声层、`/admin` 陷阱下发令牌）。
- **没有新开独立模块**：早期尝试的 `shadow.py` / `honeytoken.py`（另写一套诱饵，与既有蜜罐重复）已被废弃并删除。防御逻辑统一收敛到 `honeypot` + `ratelimit` + `human_check` 三件套。

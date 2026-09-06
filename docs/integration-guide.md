# 本地接口与可选共享接入

## 范围

本地 HTTP API 只监听 `127.0.0.1` 的动态端口，由 `friend_client.py` 启动并供内嵌页面使用。它不是公网 API，也不应绑定到外部网卡。单独调试页面时，`start.ps1` 使用固定端口 `4173`。

## 本地路由

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/matches` | 返回当前账号、玩法、筛选、汇总、时段和逐局数据 |
| GET | `/api/accounts` | 返回账号列表、当前账号和操作占用状态 |
| GET | `/api/auth/candidates` | 读取电脑版微信中可识别的小程序账号候选，不返回敏感凭证 |
| GET | `/api/status` | 返回当前缓存时间、局数和详情覆盖状态 |
| GET | `/api/sync/status?id=<job_id>` | 轮询后台同步任务 |
| GET/POST | `/api/preferences?account_id=<id>` | 读取或写入账号独立偏好 |
| POST | `/api/auth/import` | 读取并验证当前小程序账号，可携带 `candidate_id` 定向读取候选或恢复已保存账号 |
| POST | `/api/accounts/switch` | 切换本地激活账号 |
| POST | `/api/accounts/remove` | 移除登录凭证，保留该账号缓存 |
| POST | `/api/sync/start` | 启动后台同步，成功启动返回 `202` |
| POST | `/api/sync` | 兼容的同步阻塞入口 |
| POST | `/api/wechat/open` | 派发固定的官方小程序 URL Scheme，启动失败时回退普通微信 |

认证导入、账号切换/移除、偏好写入和微信拉起等读取 JSON 的路由要求请求体为对象，默认最大 64 KiB；同步入口不读取请求体。`/api/wechat/open` 只接受空对象或空请求体，任何自定义 URL、AppID 或页面路径都会返回 `400`，调用方不能把它当作通用链接启动器。

该接口只会派发代码中固定的“三角洲行动”正式版小程序地址 `weixin://dl/business/?appid=wx1c36464bbea2507a&path=pages/index/index&env_version=release`。直达派发成功时返回 `direct:true`；如果 `os.startfile` 抛出启动错误，则只回退固定的普通微信入口 `weixin://`，回退成功时返回 `direct:false`。`direct` 和 `ok` 只描述请求是否已交给 Windows，不证明微信已打开目标页面；调用方仍需检查 `ok`、`direct` 和 `message`，并以后续是否检测到目标账号的新凭据作为恢复结果。协议未注册或两次启动均失败时，接口也可能保持 HTTP `200` 并返回 `ok:false`。

## 认证恢复

`POST /api/auth/import` 不带 `candidate_id` 时尝试当前可识别候选；带 `candidate_id` 时只处理对应账号。自动恢复使用 `{"candidate_id":"<id>","recovery":true,"known_revision":"<opaque-sha256>"}`；`known_revision` 来自 `/api/accounts` 中该账号已保存凭据的安全 revision，只用于判断微信候选是否真的比保险箱更新，不包含凭证明文。不能把拉起前 `/api/auth/candidates` 的当前 revision 当作基线，否则用户已经提前刷新好的新凭据会被错误跳过。仅缓存账号或用户明确发起的新一轮验证可传 `null`，允许对当前同账号候选做一次新的有限验证。

首次没有账号时，普通导入若没有完整候选会返回 HTTP `400`、`status:waiting_for_miniapp`、`error_kind:auth_missing`、`auth_invalid:false`，且不会写入账号。用户点击“刷新登录态”后的固定顺序是：`POST /api/wechat/open` → 轮询 `GET /api/auth/candidates` → 取最新安全候选的 `id` 定向 `POST /api/auth/import` → 验证成功后激活并启动同步。首次发现不能传 `recovery:true`，也不能在最新候选繁忙、网络失败或未知异常时回退导入旧候选。

恢复已保存账号时，前端必须先固定目标账号 ID，再拉起官方小程序并等待本地候选变化；只允许携带该 `candidate_id` 做定向导入。检测到其它账号时不得退回无参数导入或覆盖当前账号。目标账号验证成功后才自动启动一次战绩同步；未检测到新凭据、腾讯繁忙或网络异常时显示明确结果并保留历史缓存。

- 验证成功：HTTP `200`，`ok:true`、`pending:false`、`status:recovered`、`auth_state:valid`，并返回目标账号安全摘要和本次 `credential_revision`。
- 小程序尚未写入候选、凭据 revision 未变化或新凭据仍被明确拒绝：HTTP `202`、`status:waiting_for_miniapp`、`error_kind:auth_waiting`；明确拒绝时另有 `probe_error_kind:expired`。前端等待新 revision，不重复探测同一份旧凭据。
- 当前最新候选是其它账号：HTTP `202`、`status:account_mismatch`，返回不含凭证的 `detected_account`；不得保存、激活或回退无参数导入。
- 同账号新凭据遇 `busy` 或 `network`：HTTP `202`、`status:pending_verification`、`auth_state:pending_verification` 和 `retry_after_seconds`。每一份 revision 只做有限验证，随后继续等待凭据变化。
- 账号管理或同步占用：HTTP `409`、`status:operation_busy`、`retryable:true`，前端按 `retry_after_seconds` 继续有限轮询。
- 请求字段无效、目标账号不存在或未知错误：HTTP `400`，结合统一错误字段处理。

认证恢复与同步失败的结构化字段为 `error`、`error_kind`、`retryable` 和 `auth_invalid`。其它路由仍可能只返回 `error`。`error_kind=expired` 表示当前凭据不可用，不代表账号保险箱一定已经写成 `auth_state=expired`；持久状态边界见 `architecture.md`。

## 同步任务

`POST /api/sync/start?account_id=<id>` 返回任务对象和 `job_id`。新任务返回 `202`；已有任务或跨进程操作锁占用返回 `409`；登录态缺失、不完整或其它启动前失败返回 `502`。前端使用 `/api/sync/status` 轮询列表、详情、保存和完成阶段。未知任务 ID 返回 `404`，当前只包含普通 `error` 字段。

后台任务顶层包含 `status`、`progress`、`message`、`result`、`error`、`error_kind`、`retryable`、`auth_invalid` 和 `auth_state`。完成后的统计位于 `result`：

```json
{
  "id": "<job-id>",
  "status": "completed",
  "progress": 100,
  "message": "同步完成",
  "auth_state": "valid",
  "error_kind": null,
  "retryable": false,
  "auth_invalid": false,
  "result": {
    "ok": true,
    "account_id": "<24-hex-id>",
    "matches": 0,
    "details_loaded": 0,
    "income_loaded": 0,
    "detail_failures": 0
  }
}
```

兼容入口 `POST /api/sync` 成功时直接返回上述 `result` 对象；操作锁冲突返回 `409`，执行失败返回 `502`。

## 战绩查询

`GET /api/matches` 的通用查询参数：

| 参数 | 形式 | 说明 |
|---|---|---|
| `account_id` | 单值 | 指定已保存账号；省略时使用当前账号 |
| `mode` | `sol` / `mp` / `all` | 页面应使用 `sol` 或 `mp`；`all` 仅兼容旧调用 |
| `from` / `to` | ISO 时间 | 手动日期范围 |
| `friend` | 可重复 | 选中的好友名称 |
| `friend_mode` | `any` / `all` | 任一好友在场或全部好友同时在场 |
| `session` | 可重复 | 游戏时段 ID；多个值按离散区间并集 |

烽火还使用 `difficulty`、`map`；全面还使用 `map`、`ruleset`、`result`、`side` 和 `operator`。未知 `friend_mode` 返回错误；已失效的时段 ID 会形成显式空筛选，而不是退回全部数据。

时段对象的主要字段：

```json
{
  "id": "<first-match-iso-time>",
  "from": "<iso-time>",
  "to": "<iso-time>",
  "matches": 0,
  "profit_matches": 0,
  "net_profit": null
}
```

选择好友后，时段对象才会额外包含 `friend_matches`。`profit_matches` 是烽火局数；纯全面时段的 `net_profit` 为 `null`。

## 可选远程共享

桌面端配置和上传是显式 CLI：

```powershell
$env:DELTA_REMOTE_URL = 'https://<private-share-root>'
$env:DELTA_REMOTE_TOKEN = '<token>'
python .\remote_sync.py configure --name '<display-name>'
python .\remote_sync.py status
python .\remote_sync.py upload
```

生产共享服务需要 `DELTA_UPLOAD_TOKEN` 和 `DELTA_DATA_DIR`，由 `/etc/delta-stats.env` 提供。上传使用 `Authorization: Bearer <token>`，单次请求最大 20 MiB，单玩家最多 5000 局。令牌只从受控凭证入口读取，不写入文档或命令记录。

共享服务的 `/api/matches`、`/api/preferences` 和页面依赖私密路径及反向代理边界；不要把 3012 直接开放公网。共享端目前没有本地时段对象的 `profit_matches` 和 `net_profit`，消费方不能假设两端响应完全一致。

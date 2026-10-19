# Host2Play 续期脚本 — 模块契约 (SPEC v1)

本文件是并行开发的唯一契约。所有模块必须严格按此实现，不得自行改名字/改 schema。

## 0. 目标

把"续期很不靠谱"变成"续期基本必成"。核心手段：
1. **少打扰**：成功续期后在冷却期内跳过，不再重复烧验证码额度（额度是稀缺资源）。
2. **多重试**：单个页面内验证码可重试 8 次；单 URL 可重开页面 6 次；IP 可轮换 5 次。当前代码 3 次验证码失败就整条 URL 判死，这是最大缺陷。
3. **识别失败可区分**：STT 网络错误（RequestError）→ 换 IP；识别不出（UnknownValueError）→ 换一道题。当前代码混为一谈。
4. **固定 sleep 全部换成轮询**，减少无谓等待与竞态。
5. **状态持久化**：`state.json` 提交回仓库，跨 run 记忆到期时间与上次成功时间。

## 1. 文件职责

| 文件 | 负责人 | 说明 |
|---|---|---|
| `main.py` | Agent A | 全部逻辑。必须可被 `import` 而不触发网络/浏览器副作用 |
| `.github/workflows/Host2Play_Renew.yml` | Agent B | CI 编排 |
| `README.md` | Agent B | 文档同步 |
| `selftest.py` | Agent C | 离线单测，只测纯函数，不联网不启浏览器 |
| `state.json` | 运行时生成 | 由 main.py 写入，workflow 提交回仓库 |

## 2. `main.py` 必须导出的纯函数（Agent C 依赖）

```python
def parse_expiry(text: str) -> datetime | None
    """把页面上的到期文案解析成 UTC aware datetime。
    必须支持：
      "Expires in: 2 days"                -> now + 2d
      "Expires in: 23 hours 30 minutes"   -> now + 23h30m
      "Deletes on: 2026-10-17 20:19:49"   -> 该时刻 (UTC)
      "2026-10-17T20:19:49Z"              -> 该时刻
      "2026-10-17"                        -> 该日 00:00 UTC
      "Expires in: 45 minutes"            -> now + 45m
      ""/"未知"/None/垃圾字符串            -> None
    不得抛异常。
    """

def server_key(url: str) -> str
    """从续期链接提取稳定 key。
    有 ?i=<id> 时返回 <id>；否则返回 url 的 sha1 前 12 位。
    不得抛异常。
    """

def should_skip(entry: dict | None, now: datetime, cooldown_hours: float) -> tuple[bool, str]
    """entry 是 state["servers"][key]，可能为 None。
    entry 为空 -> (False, "首次运行")
    last_success_at 距今 < cooldown_hours -> (True, "冷却中: 距上次成功 X.Xh")
    其余 -> (False, "需要续期")
    last_success_at 解析失败时按不跳过处理。
    不得抛异常。
    """

def build_caption(ok: bool, server_name: str, url: str,
                  old_expiry: str, new_expiry: str,
                  reason: str = "", skipped: bool = False) -> str
    """生成 Telegram 文案（纯文本，不用 HTML 标签，避免 parse_mode 转义问题）。
    成功:  ✅ 续订成功 / 服务器 / 到期: old -> new / URL
    跳过:  ⏭ 已跳过（冷却中） / 服务器 / 到期 / 原因 / URL
    失败:  ❌ 续订失败 / 服务器 / 原因 / URL
    """
```

## 3. `state.json` schema（版本 1）

```json
{
  "version": 1,
  "updated_at": "2026-10-14T20:19:49+00:00",
  "servers": {
    "<server_key>": {
      "url": "https://host2play.gratis/server/renew?i=...",
      "name": "My Server",
      "last_attempt_at": "2026-10-14T20:19:49+00:00",
      "last_success_at": "2026-10-14T20:19:49+00:00",
      "expiry_raw": "Expires in: 3 days",
      "expiry_iso": "2026-10-17T20:19:49+00:00",
      "consecutive_failures": 0,
      "last_status": "success",
      "last_reason": ""
    }
  },
  "last_run": {
    "started_at": "...",
    "finished_at": "...",
    "ok": 1,
    "failed": 0,
    "skipped": 0,
    "retryable": false,
    "retry_hint": ""
  }
}
```

规则：
- 时间一律 ISO8601 带 `+00:00` 时区。
- `last_success_at` 只在真正续期成功时更新，跳过/失败不动。
- `consecutive_failures` 成功后清零，失败 +1。
- `expiry_iso` 只在解析成功时写，解析失败保留旧值。
- 文件不存在 / JSON 损坏 / schema 不符 → 当作空 state 继续跑，不崩。
- 写入用 `tmp + os.replace` 原子替换。

## 4. 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部成功或跳过（`retryable=false`） |
| 1 | 部分失败（有成功/跳过，也有失败） |
| 2 | 全部失败（`retryable=true`） |

`last_run.retryable` = 存在失败项 **且** 失败不是"站点结构性变化"（找不到 Renew 按钮/选择器全挂）之外的临时原因。
简化：只要 `failed > 0` 且 `ok + skipped == 0` → `retryable=true`；否则 `retryable=false`。
（部分成功不再触发整轮重跑，避免重复烧验证码。）

## 5. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `TG_BOT_TOKEN` | 空 | Telegram |
| `TG_CHAT_ID` | 空 | Telegram |
| `RENEW_COOLDOWN_HOURS` | `6` | 成功后续期冷却，0 = 每次都续 |
| `RENEW_URLS` | 空 | 逗号分隔，覆盖代码里的 `RENEW_URLS` 列表 |
| `JOB_BUDGET_MINUTES` | `45` | 整体时间预算，超了就不再开新 URL |
| `DRY_RUN` | `0` | 1 = 只打开页面读状态，不点续期、不破验证码 |
| `PARALLEL_URLS` | `1` | 并发处理 URL 数（WARP 轮换有全局锁） |
| `CAPTCHA_MAX_PER_PAGE` | `8` | 单页面验证码尝试上限 |
| `PAGE_ATTEMPTS` | `6` | 单 URL 重开页面上限 |
| `IP_ROTATIONS` | `5` | 单 URL 换 IP 上限 |

## 6. 关键实现约束

1. 所有 DrissionPage 交互必须 try/except 包裹，单点失败不能中断整条流程。
2. 禁止裸 `time.sleep(10)` 这类长固定等待；用 `wait_for(pred, timeout)` 轮询，命中即返回。
3. Chrome 实例**按需重建**：验证码识别失败（UnknownValueError）时复用同一页面刷新题目；只有 IP 封锁 / 页面崩溃才重建。
4. 每次 `ChromiumPage` 的 `user_data_dir` 用 `tempfile.mkdtemp()`，`quit()` 后 `shutil.rmtree(ignore_errors=True)`，避免 Actions 磁盘被吃满。
5. WARP 轮换两级：先 `disconnect/connect`（快，~8s），连续 2 次 IP 没变才 `registration delete + new`（慢）。
6. 轮换后必须验证 IP 真的变了（对比 `api.ipify.org`），没变则重试。
7. 成功判定优先级：解析出到期时间且 `new > old + 1h` → 成功；否则页面出现 success 关键词 → 成功；否则失败。
8. 音频识别链路：下载 → 转 16k 单声道 → 归一化 → 去静音 → 生成 2 个变体（原样 / 高通滤波）→ 依次送 Google STT（`en-US`）。区分 `RequestError`（网络/被墙）与 `UnknownValueError`（听不懂）。
9. 站点选择器全部集中到文件顶部 `SELECTORS` dict，便于站点改版时一处修改。
10. 日志保留 `[INFO]/[WARN]/[ERROR]` 前缀与中文正文，新增 `[STEP]` 里程碑行。

## 7. Agent B 的 workflow 要求

1. 用 `actions/checkout@v4`（不要 v6，尚不存在）、`setup-python@v5`、`upload-artifact@v4`。
2. Chrome 安装加 `actions/cache` 缓存 deb 包。
3. `timeout-minutes: 70`，与 `JOB_BUDGET_MINUTES=45` 匹配。
4. 跑完提交 `state.json` + `time.txt`，`git pull --rebase` 后 push，失败重试 3 次。
5. `rerun-workflow` job：从仓库读 `state.json`（用 `github-script` + `repos.getContent`），只有 `last_run.retryable == true` 且 `retry < 3` 才 dispatch；否则打印原因退出。**不要**再用 `needs.result == 'failure'` 无脑重跑。
6. 保留 `delete-workflow-runs`，`keep_minimum_runs: 5`。
7. `concurrency` 组保持，但加 `cancel-in-progress: false`。
8. 输出 `GITHUB_STEP_SUMMARY`：每个 URL 的成功/失败/跳过表格。

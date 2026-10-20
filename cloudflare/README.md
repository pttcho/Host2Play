# Cloudflare 定时器（替代 GitHub 自带的 schedule）

## 为什么要换

GitHub Actions 的 `schedule` 在公开仓库上会被限流，实测数据：

```
配置：cron: '17 * * * *'   （每小时一次）
实际：
  2026-10-19 21:32 UTC  触发
  2026-10-20 01:05 UTC  触发   ← 间隔 3.6 小时，中间漏了 3 次
```

GitHub 官方文档也明确写了 schedule 不保证准时、高负载时事件会被丢弃。对这台服务器来说是致命的：**续期后只有 8 小时窗口，漏 3 次就过期了**。

Cloudflare Cron Trigger 的准点率高得多，且免费计划支持每分钟一次，额度绰绰有余。

## 职责划分

| 谁 | 做什么 | 成本 |
|---|---|---|
| **Cloudflare Worker** | 每分钟读一次 `getServerData`，判断剩余时间是否 ≤ 2h；到点才调 GitHub | 每次几毫秒 CPU，免费额度内 |
| **GitHub Actions** | 真正续期：起 Chrome、解 reCAPTCHA、提交 | 只在需要时才被触发 |

**关键点：Worker 不做真正的续期。** 它的 CPU 上限是 30 秒、跑不了浏览器、也解不了 reCAPTCHA。它只负责"准点敲门"。

这样正常情况下每天只有约 4 次 Actions 调用（每次续期一次），而不是 24 次。

## 部署步骤

### 1. 创建 GitHub PAT

访问 <https://github.com/settings/personal-access-tokens/new>

- **Token name**：`host2play-cf-cron`
- **Expiration**：建议 90 天（到期需换，可以设日历提醒）
- **Repository access**：Only select repositories → `pttcho/Host2Play`
- **Permissions** → Repository permissions：
  - **Actions**：`Read and write` ← 只需要这一个
  - 其余全部留 `No access`

生成后复制 `github_pat_...`。

> 用 Fine-grained PAT，不要用 classic token。Fine-grained 可以只授权这一个仓库的 Actions 权限，泄露了损失也有限。

### 2. 安装并登录 wrangler

```bash
npm install -g wrangler
wrangler login
```

### 3. 配置密钥

在 `cloudflare/` 目录下执行（每个命令会提示粘贴值）：

```bash
cd cloudflare

# GitHub PAT（上一步生成的）
wrangler secret put GITHUB_PAT

# 仓库全名
wrangler secret put GITHUB_REPO
# 输入：pttcho/Host2Play

# 续期链接，与 Actions 里的 RENEW_URLS 保持一致
wrangler secret put RENEW_URLS
# 输入：https://host2play.gratis/server/renew?i=你的服务器ID
```

### 4. 部署

```bash
wrangler deploy
```

输出会给出 Worker 的 URL，形如 `https://host2play-cron.<你的子域>.workers.dev`。

### 5. 验证

```bash
# 健康检查
curl https://host2play-cron.<你的子域>.workers.dev/health

# 看当前剩余时间（不触发 Actions）
curl https://host2play-cron.<你的子域>.workers.dev/probe

# 手动强推一次（绕过冷却，会真的跑 Actions）
curl https://host2play-cron.<你的子域>.workers.dev/trigger
```

`/probe` 正常输出示例：

```json
[
  {
    "id": "1b82…6855",
    "name": "fvf8042",
    "expiresAt": "2026-10-20T08:18:56.000Z",
    "leftHours": 4.85,
    "dead": false
  }
]
```

### 6. 看实时日志

```bash
wrangler tail
```

正常时每分钟输出一行 `[WAIT] ... 剩余 X.XXh，还不需要续期`；到点会输出 `[TRIGGER]` 和 `[OK] 已触发 GitHub Actions`。

## 参数

改 `wrangler.toml` 的 `[vars]` 后重新 `wrangler deploy`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `SKIP_HOURS` | `2` | 剩余低于这个小时数才触发。必须和 Actions 里的 `SKIP_IF_REMAINING_HOURS` 一致，否则两边判断会打架 |
| `COOLDOWN_HOURS` | `1` | 两次触发的最小间隔。Worker 每分钟都跑，靠这个去重 |
| `GITHUB_REF` | `main` | 触发哪个分支 |

## 常见问题

**Q：Worker 会不会重复触发，烧掉 Actions 额度？**

不会。有两层保护：Worker 侧有 `COOLDOWN_HOURS`（默认 1 小时），Actions 侧有 `concurrency: host2play-renew` + `cancel-in-progress: false` 排队机制，以及 `main.py` 里的 `RENEW_COOLDOWN_HOURS`（3 小时）。即使重复触发，第二次也会因为剩余时间还够而秒退（1.5 分钟，不烧验证码）。

**Q：Cloudflare 免费额度够吗？**

够。免费计划每天 10 万次请求，每分钟一次 = 1440 次/天，占 1.4%。CPU 时间每次几毫秒，远低于 10ms 的限制（实际上读一个 API 的等待时间不算 CPU）。

**Q：Worker 挂了怎么办？**

在 `wrangler.toml` 里恢复 GitHub 的 schedule 作为兜底（把注释掉的 `schedule:` 加回去）。两者可以共存，Actions 侧有 `concurrency` 组会串行化，不会冲突。

**Q：PAT 过期了会怎样？**

Worker 会打 `[ERROR] dispatch 401`，`wrangler tail` 能看到。建议设个 90 天后的日历提醒。也可以改用 GitHub App 的 installation token，但那需要额外维护一个签发服务，对单仓库场景不值得。

**Q：能不能让 Worker 直接续期，不经过 Actions？**

不行。续期需要过 reCAPTCHA（音频识别 + 浏览器），Worker 跑不了 Chrome，CPU 上限也只有 30 秒。这是架构上的硬限制，不是配置问题。

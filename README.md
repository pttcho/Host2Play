# Host2Play 自动续期脚本

# ⭐ **觉得有用？给个 Star 支持一下！**
> 注册地址：[https://host2play.gratis](https://host2play.gratis)

自动续期 Host2Play 免费游戏服务器的 GitHub Actions 脚本。基于 Chrome 自动化，自动处理 reCAPTCHA 音频验证，支持 IP 封锁自动切换 WARP，多服务器批量续期，并通过 Telegram 推送带截图的运行结果。

## ✨ 功能特性

- ✅ 多服务器支持 —— 一次配置多个续期链接，顺序执行
- ✅ 全自动 reCAPTCHA 破解 —— 音频识别 + 自动填写，无需人工干预
- ✅ 智能 IP 切换 —— 检测到 Google 封锁后自动通过 WARP 更换出口 IP
- ✅ Telegram 通知 —— 续期成功 / 失败均推送消息，附带页面截图
- ✅ 定时 + 手动运行 —— 默认每天多次自动执行，也支持手动触发
- ✅ 跨运行记忆 —— `state.json` 记住到期时间与上次成功时间，冷却期内直接跳过，不重复烧验证码
- ✅ 运行摘要 —— 每次跑完在 Actions 页面直接给出服务器 / 状态 / 到期时间表格，不用翻日志
- ✅ 自动清理运行记录 —— 只保留最近 5 条 Actions 记录，仓库保持清爽
- ✅ 虚拟显示无头运行 —— 使用 Xvfb，不依赖图形界面

## 📋 前置要求

### 1. 配置续期链接（必须，走 Secret）

**不要把续期链接写进代码。** 链接里的 `i=<uuid>` 是**免鉴权的续期凭据** —— 任何拿到它的人都能调 `/publicapis/renewServer` 给你的服务器续期，不需要登录、不需要密码。仓库是 public 时，写进代码等于公开凭据。

正确做法：进入仓库 `Settings` → `Secrets and variables` → `Actions` → **New repository secret**

| Secret 名称 | 值 |
|---|---|
| `RENEW_URLS` | `https://host2play.gratis/server/renew?i=你的服务器ID` |

多个服务器用**英文逗号**分隔：

```
https://host2play.gratis/server/renew?i=aaaa,https://host2play.gratis/server/renew?i=bbbb
```

**如何获取链接：**
打开 [Host2Play Minecraft 面板](https://host2play.gratis/panel/minecraft)，找到你的服务器，点击 **Copy public renew link** 即可复制。

**链接格式：**
```
https://host2play.gratis/server/renew?i=你的服务器ID
```

> 代码里的 `DEFAULT_RENEW_URLS` 默认为空列表。本地调试时可以用同名环境变量：
> ```bash
> RENEW_URLS="https://host2play.gratis/server/renew?i=xxx" python main.py --check
> ```
> 或临时改 `DEFAULT_RENEW_URLS`，但**提交前务必改回空**。

> ⚠️ 如果你的链接曾经明文提交过（写进过 main.py 并 push），它已经留在 git 历史里，删文件没用。建议去面板重新生成续期链接（或重建服务器拿新 ID），旧链接作废即可 —— 不必为此 force push 重写历史。

### 2. 配置 Telegram 通知（可选）

进入仓库 `Settings` → `Secrets and variables` → `Actions`，添加以下 Secrets：

| Secret 名称 | 必填 | 说明 | 示例 |
|------------|------|------|------|
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` |
| `TG_CHAT_ID` | ❌ | 接收消息的 Chat ID | `123456789` |

> 不配置这两个 Secret 时，脚本仍会正常运行，只是不会发送 Telegram 通知。

**获取方式：**
- **Bot Token**：向 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 创建机器人，获得 token。
- **Chat ID**：向 [@userinfobot](https://t.me/userinfobot) 发送任意消息即可获取。

### 3. 环境依赖（无需手动配置）

脚本运行在 GitHub Actions 的 `ubuntu-latest` 环境中，以下依赖会在运行时自动安装：
- Google Chrome（deb 包会被缓存，不用每次重新下载 110MB）
- Xvfb（虚拟显示）
- ffmpeg、unzip、curl
- WARP（网络代理，用于切换 IP）
- Python 库：`DrissionPage`, `xvfbwrapper`, `requests`, `SpeechRecognition`, `pydub`

## 🚀 使用方法

### 方法 1：定时自动运行（默认）

Fork 本仓库并完成上述配置后，工作流**每小时运行一次**：

- `17 * * * *` → 每小时的第 17 分钟（UTC），即北京时间每小时 17 分

**但每小时运行 ≠ 每小时续期。** 续不续期完全由「剩余时间」决定：

| 剩余时间 | 行为 | 开销 |
|---|---|---|
| > 2 小时 | 跳过，不启动浏览器 | 一次 HTTP 请求（约 1 秒） |
| ≤ 2 小时 | 续期 | 启动 Chrome + 解验证码，约 1–3 分钟 |
| 已过期（7 天内） | **立即续期**，不受冷却限制 | 同上 |
| 已过期超 7 天 | 判为终态，不再重试 | 一次 HTTP 请求 |

之所以敢每小时跑：跳过路径**连浏览器都不启动**，只调一次 `getServerData` 接口拿剩余时间，完全不消耗验证码额度。

之所以必须每小时跑：GitHub Actions 的 cron 在整点/半点高峰期会延迟 15–60 分钟，极端情况下整个事件被丢弃。原来一天只有 2–5 个调度点，某个点被跳过就没有替补 —— 这正是"中午那次定时器不工作"的原因。加密到每小时之后，单次被跳过最多影响一小时。

> 分钟数选 `17` 而不是 `0`：整点和半点是 Actions 排队最严重的时刻，避开能显著降低延迟概率。

如需修改频率，编辑 `.github/workflows/Host2Play_Renew.yml` 中的 `schedule` 部分。常用示例：
- `17 * * * *`  每小时一次（默认，推荐）
- `17 */2 * * *` 每 2 小时一次（更省 Actions 分钟数，但容错窗口变小）
- `17 3,11,19 * * *` 每天 3 次（北京 11:17 / 19:17 / 次日 03:17）

> ⚠️ 不建议把频率降到每天 1–2 次：调度一旦被 GitHub 延迟或丢弃，就正好赶上服务器过期。省下的那点 Actions 分钟数，不值得拿服务器去赌。

### 方法 2：手动触发（GitHub 网页）

1. 进入仓库的 `Actions` 页面
2. 选择 **Host2Play 续期** 工作流
3. 点击 **Run workflow** → 点击绿色的 **Run workflow** 按钮

> `retry` 输入框是给自动重试链路内部用的计数，手动触发保持默认 `0` 即可。

### 方法 3：API 调用

```bash
curl -X POST \
  -H "Authorization: Bearer ghp_你的Token" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/你的用户名/你的仓库名/actions/workflows/Host2Play_Renew.yml/dispatches \
  -d '{"ref":"main"}'
```

## 🧠 可靠性设计

续期失败意味着服务器被删，所以"能跑通"不够，得"跑不坏"。下面这几层是刻意堆出来的。

### 少打扰：不重复烧验证码额度

| 机制 | 行为 | 为什么这么做 |
|---|---|---|
| **剩余时间跳过** | API 读到的剩余时间超过 `SKIP_IF_REMAINING_HOURS`（默认 **2h**）直接跳过，连浏览器都不启动 | 这是主判据。续一次通常加 24–72 小时，太早续会把已有额度浪费掉。剩 2h 才动手，既不浪费也不冒险 |
| **冷却期去重** | 距上次成功不足 `RENEW_COOLDOWN_HOURS`（默认 6h）时，只有**剩余时间还够撑过冷却期**才跳过 | 防止同一窗口内反复重试烧额度。但一旦进入续期窗口，冷却不再阻拦 —— 每小时才来一次，被挡一次就可能直接过期 |
| **过期立即续** | `expires` 已过但未到 `deletes`（约 7 天保留期）时，**无视冷却和阈值，立刻续期** | 这是最紧急的状态。旧实现把页面上的 `Server expired` 当成"续期成功"，发假通知然后下轮再来一遍，永远续不上 |
| **兜底强制续** | 超过 `STALE_AFTER_HOURS`（默认 30h）没有成功续期，强制续一次 | GitHub 调度可能连续被丢弃。这是"调度漏了也能自愈"的最后一道保险 |

### 分级重试：三层预算彼此独立

旧实现的致命缺陷是"验证码 3 次失败 → 整条 URL 判死"。现在三层各管各的，互不牵连：

| 层级 | 上限 | 环境变量 | 什么时候消耗 |
|---|---|---|---|
| 单页面内验证码 | 8 次 | `CAPTCHA_MAX_PER_PAGE` | 同一道题听错了、填错了，刷新题目重来 |
| 单 URL 重开页面 | 6 次 | `PAGE_ATTEMPTS` | 页面崩了、验证码区域没加载出来，整页重开 |
| 单 URL 换出口 IP | 5 次 | `IP_ROTATIONS` | 确认是 IP 被 Google 封了，换 WARP 出口再来 |

三层叠加不是简单相乘 —— 换 IP 会把页面计数重置，所以最坏情况远比 8×6×5 小，实际由 `JOB_BUDGET_MINUTES`（默认 45 分钟）兜底：预算一超就不再开新 URL，保证在 job 的 70 分钟超时内收尾。

### 音频识别：错误必须分类

把两种完全不同的失败混为一谈，是最浪费额度的做法：

| 异常 | 真实含义 | 正确反应 | 错误反应（旧实现） |
|---|---|---|---|
| `RequestError` | 请求根本到不了 Google（网络不通 / 被墙） | 换出口 IP | 换一道题 —— 换了也还是到不了 |
| `UnknownValueError` | 到了 Google，但听不清这段音频 | 换一道题 | 换 IP —— 白烧一次 IP 轮换额度 |

现在两者分开处理，各自走各自的恢复路径，不会互相消耗对方的额度。

### 状态持久化：跨运行记忆

`state.json` 提交回仓库，是唯一跨 run 的记忆：

- 记住每个服务器的到期时间、上次成功时间、连续失败次数
- 冷却期和剩余时间判定全靠它 —— 不提交就等于每次都是"首次运行"，冷却永远失效
- 文件不存在 / JSON 损坏 / schema 不符时按空状态继续跑，**不崩**
- 写入用 `tmp + os.replace` 原子替换，不会写出半截文件

### 网络：两级 WARP 轮换

| 级别 | 操作 | 耗时 | 触发条件 |
|---|---|---|---|
| 一级 | `disconnect` + `connect` | ~8 秒 | 默认先试这个，够快 |
| 二级 | `registration delete` + `registration new` | ~30 秒 | 一级连续 2 次 IP 没变，说明这个注册被盯上了 |

轮换后**必须验证 IP 真的变了**（对比 `api.ipify.org`），没变就重试。不验证等于自欺欺人。

### 重试链路：只在该重跑时重跑

`rerun-workflow` 不再用 `needs.result == 'failure'` 无脑重跑整个 workflow，而是读仓库里的 `state.json`：

- `last_run.retryable === true` **且** 重试次数 < 3 → 才 dispatch 下一轮
- 部分成功（比如 2 个成功 1 个失败）→ `retryable = false` → 不重跑
- 全成功 → job 绿 → 这个 job 根本不会启动

这样避免了两种浪费：重复安装 Chrome/WARP 的 3–5 分钟，以及整轮重跑重复烧验证码。

## ⚙️ 配置项

所有配置都通过环境变量控制，在 `.github/workflows/Host2Play_Renew.yml` 里改，不用动代码。工作流顶部 `env:` 块集中放了最常调的三个。

| 变量 | 默认值 | 适用场景 |
|---|---|---|
| `TG_BOT_TOKEN` | 空 | Telegram 通知，留空则不发通知 |
| `TG_CHAT_ID` | 空 | 同上 |
| `SKIP_IF_REMAINING_HOURS` | `2` | **核心开关：剩多久才续期**。默认 2 = 只剩 1–2 小时才动手。续一次通常加 24–72 小时，太早续会把已有额度浪费掉。**不建议设到 1 以下**：cron 可能延迟 30–60 分钟，剩余不足 1 小时时一次延迟就可能真过期。设 `0` = 每次运行都尝试续期（会狂烧验证码，仅调试用） |
| `RENEW_COOLDOWN_HOURS` | `6` | **成功后多久内不重复续期**。防止同一窗口内反复重试。注意：进入续期窗口（剩余 ≤ 阈值）后冷却不再阻拦，只在剩余时间还充裕时生效 |
| `STALE_AFTER_HOURS` | `30` | **兜底强制续期**。超过这么久没成功续期，无视上面两个阈值强制续一次。用于 GitHub 调度连续被丢弃的极端情况。设 `0` = 关闭兜底 |
| `JOB_BUDGET_MINUTES` | `20` | **整体时间预算**。超预算后不再开新 URL。每小时运行一次，单次必须快；多服务器（5 个以上）建议调到 30–40 |
| `RENEW_URLS` | 空 | **续期链接，必须配成 Secret**。逗号分隔多个链接。链接里的 `i=<uuid>` 是免鉴权凭据，写进代码等于公开 |
| `DRY_RUN` | `0` | 设 `1` = 只调 API 读状态，不启动浏览器、不点续期、不碰验证码。**调试时必用**，详见[故障排查](#-故障排查) |
| `PARALLEL_URLS` | `1` | 并发处理的 URL 数。WARP 轮换有全局锁，调到 2–3 可以，再高收益递减且更容易触发风控 |
| `CAPTCHA_MAX_PER_PAGE` | `8` | 单页面验证码尝试上限。识别率低时可提到 10–12；额度紧张时降到 5 |
| `PAGE_ATTEMPTS` | `6` | 单 URL 重开页面上限。页面经常加载失败可提到 8 |
| `IP_ROTATIONS` | `5` | 单 URL 换 IP 上限。`0` = 完全不换 IP（用于排查是不是 WARP 本身的问题） |

> 为什么 `RENEW_COOLDOWN_HOURS` 默认 6 小时？
> 因为 reCAPTCHA 的"额度"并不是一个可以无限刷的计数器 —— 它背后是 Google 的风险评分。同一个 IP 短时间内反复解验证码，评分会持续恶化，表现出来就是验证码越来越难、音频越来越长、最后直接 `try again later` 封 IP。**省下来的额度，才是失败时能重试的本钱。**

> 为什么 `SKIP_IF_REMAINING_HOURS` 从 48 改成了 2？
> 48 小时是"刚续过就别再动"的保守值，但配合每小时调度就变成了"几乎永远不续"。真正该续的时机是**快到期前**：剩余 2 小时动手，既不会浪费已有时长，又留了 2 次调度机会应对延迟。而过期服务器走的是独立分支，**立即续期、不受这个阈值和冷却影响**。

## 🐛 故障排查

### 先别急着跑真的：`DRY_RUN=1`

怀疑站点改版、或者只是想确认链接还有效时，**不要**直接跑真续期 —— 那会消耗验证码额度。用空跑模式：

1. 仓库 `Settings` → `Secrets and variables` → `Actions` → `Variables` 标签页
2. 新建变量 `DRY_RUN`，值填 `1`
3. 手动触发一次 workflow

空跑会打开页面、读服务器名和到期时间、写 `state.json`，但**不点续期、不碰验证码**。日志里会看到 `[STEP] DRY_RUN=1：只读状态，不点续期、不碰验证码`，摘要表格里的状态是 `dry_run`。

确认没问题后，把 `DRY_RUN` 改成 `0` 或直接删掉这个变量。

### 选择器自检：`python main.py --check`

站点一改版，所有选择器都可能失效。`--check` 会一屏列出每个选择器当前的命中情况，按 DOM 阶段分组（页面 / 弹窗 / 验证码），不用翻日志猜是哪一步断了：

```bash
# 本地跑（需要装好 DrissionPage 和 Chrome）
python main.py --check

# 指定某条链接
python main.py --check "https://host2play.gratis/server/renew?i=你的服务器ID"
```

输出是每个选择器键的命中/未命中表格。**整组全部未命中**通常意味着站点改版，此时去 `main.py` 顶部的 `SELECTORS` 字典里改对应那一条即可 —— 所有选择器都集中在那一个 dict 里，改一处就够。

也可以临时加一个 workflow 步骤在 Actions 里跑，用 `DRY_RUN` 同理不会消耗额度。

### 手动重置 `state.json`

`state.json` 记着冷却期和连续失败次数。想强制让某台服务器立刻续期（比如刚扩容、想确认还活着），直接删掉或改掉它：

```bash
# 方式一：删掉整个文件（下次运行当作首次运行，冷却全部失效）
git rm state.json
git commit -m "重置续期状态"
git push

# 方式二：只重置某一台（推荐，其他服务器状态保留）
# 编辑 state.json，把目标服务器条目的 last_success_at 删掉或改成很早的时间，
# 同时把 consecutive_failures 改成 0
```

只重置单台时，记得 `last_success_at` 和 `consecutive_failures` 一起改 —— 只改前者的话，连续失败计数还在，重试链路的行为会和预期不符。

> 不要手工改 `version` 字段。版本不符时脚本会按空状态处理，等于把整份记录都丢了。

## ❓ 常见问题

### 1. 如何获取续期链接？
打开 [Host2Play Minecraft 面板](https://host2play.gratis/panel/minecraft)，找到你的服务器，点击 **Copy public renew link** 即可获得续期链接。然后到仓库 `Settings` → `Secrets and variables` → `Actions` 添加名为 `RENEW_URLS` 的 Secret。**不要写进代码**（见[前置要求](#1-配置续期链接必须走-secret)）。

### 2. 验证码识别失败怎么办？
脚本内置了音频识别流程，但如果识别率较低，可能是以下原因：
- **网络问题**：Google 语音识别 API 访问不稳定，脚本会区分"到不了 Google"和"听不清"两种失败，分别换 IP 和换题。
- **IP 被标记**：频繁操作会导致 Google 展示更难的验证码，脚本会通过 WARP 自动更换 IP。
- **环境噪音**：音频下载或转换问题，建议检查 Actions 日志中的识别结果。

先用 `DRY_RUN=1` 排除站点改版，再调 `CAPTCHA_MAX_PER_PAGE`。

### 3. IP 被封锁导致无法继续？
当检测到 `try again later` 或错误提示时，脚本会先做快速的 WARP 断开重连；连续两次 IP 没变，才升级到删除注册 + 重新注册。单条 URL 最多换 `IP_ROTATIONS`（默认 5）次 IP，单页面内验证码最多 `CAPTCHA_MAX_PER_PAGE`（默认 8）次，整条 URL 最多重开页面 `PAGE_ATTEMPTS`（默认 6）次 —— 三层预算独立，不会因为某一层用尽就把整条 URL 判死。

整体时间由 `JOB_BUDGET_MINUTES`（默认 45 分钟）兜底，超预算就不再开新 URL，保证在 job 的 70 分钟超时内收尾并提交状态。

### 4. 为什么需要 WARP？
Host2Play 的续期页面嵌入了 Google reCAPTCHA。当短时间内多次尝试验证时，Google 可能会封禁当前 IP。WARP 能快速更换 Cloudflare 出口 IP，绕过封锁继续验证。

### 5. 没有收到 Telegram 通知？
- 确认是否已正确设置 `TG_BOT_TOKEN` 和 `TG_CHAT_ID` 两个 Secret。
- 在 Telegram 中先给 Bot 发送 `/start` 激活对话。
- 检查 Actions 日志中是否有 `Telegram 图片通知发送成功` 的日志，或者错误信息。

### 6. 截图在哪里查看？
每次 Actions 运行结束后，在运行详情页底部 **Artifacts** 区域可以下载 `screenshots-运行编号` 的压缩包，里面包含每次续期的成功/失败截图。

### 7. 运行摘要在哪里看？
每次运行的详情页顶部有 **Summary** 区域，里面有服务器 / 状态 / 到期时间 / 原因的四列表格，以及成功、失败、跳过数量和"是否可重试"的结论。失败时先看这里，通常不用翻日志。

### 8. 能否同时续期多个服务器？
可以。在 `RENEW_URLS` Secret 里用英文逗号分隔多个链接即可。每个链接独立尝试，互不影响，最终 Telegram 通知也会分别推送。想并行处理可以调 `PARALLEL_URLS`，但注意 WARP 轮换有全局锁。

### 9. 运行时间很长是否正常？
正常。每次续期需要启动 Chrome、加载页面、处理验证码、可能还需要切换 IP 并重试，单个链接通常需要 2～5 分钟，遇到多次 IP 封锁会更久。但如果**每次都在冷却期内秒退**，那是设计如此 —— 说明服务器剩余时间还够，没必要打扰它。

### 10. `state.json` 和 `time.txt` 是什么？
- `state.json`：跨运行的记忆，记着每台服务器的到期时间、上次成功时间、连续失败次数。**不要删**，删了冷却期就失效了。
- `time.txt`：最后一次运行的 UTC 时间戳，纯人类可读的标记，格式为 `YYYY-MM-DD HH:MM:SS UTC`。脚本没有任何逻辑依赖它，删了也无所谓。

两个文件都由 workflow 自动提交回仓库，提交信息形如 `🤖 更新续期状态 2026-10-14 20:19 UTC`。如果仓库同时有人在改，workflow 会 `git pull --rebase` 后重试，最多 3 次。

## 🔒 安全建议

### 续期链接是凭据，不是配置

链接里的 `i=<uuid>` **没有鉴权**：任何拿到它的人都能调 `/publicapis/renewServer` 给你的服务器续期，不需要登录、不需要密码。所以它出现在任何会被提交或转发的地方都是泄露：

| 位置 | 处理方式 |
|---|---|
| `main.py` 源码 | `DEFAULT_RENEW_URLS` 默认为空，改用 `RENEW_URLS` Secret |
| `state.json` | key 用 sha1 哈希（`082dfb9b6753`），只存主机名，不存完整 URL |
| Actions 日志 | 只打印哈希 key，不打印链接 |
| Telegram 文案 | 脱敏为 `host2play.gratis/…?i=1b82…6855` |
| 截图文件名 | 用哈希 key，不含链接 |

> 注意：**完整链接仍然会出现在浏览器地址栏和页面请求里**，所以 Actions 的 artifact（截图）不包含链接文本，但如果站点把 URL 渲染在页面上，截图里可能有。公开仓库建议把 artifact 保留期设短一些。

### 如果链接曾经明文提交过

`git rm` 删文件没用 —— 它还在历史里。三个选项：

1. **换 ID（推荐）**：去面板重新生成续期链接，或重建服务器拿新 ID。旧链接立即失效，成本最低，不用动 git 历史。
2. **清历史**：`git filter-repo --replace-text` 或 BFG，会重写所有 commit hash，需要 force push，协作者要重新 clone。
3. **转 private**：仓库设为私有，但历史里的凭据对协作者仍然可见。

### 其他

- ✅ **敏感信息存放**：`RENEW_URLS`、Telegram Token 请严格存储在 GitHub Secrets 中。
- ✅ **定期维护**：如果 Host2Play 网站更新了页面结构，用 `python main.py --check` 一屏定位，改 `main.py` 顶部的 `SELECTORS` 字典即可。
- ✅ **Actions 权限**：默认的 `GITHUB_TOKEN` 权限已满足需要，无需额外设置。
- ✅ **Fork 安全**：建议保持 Fork 仓库同步，以便获取最新的脚本修复。

## 📄 许可证

MIT License

---

**⚠️ 免责声明**：本脚本仅供学习交流使用，使用者需遵守 Host2Play 的服务条款。因使用本脚本造成的任何问题，作者不承担任何责任。

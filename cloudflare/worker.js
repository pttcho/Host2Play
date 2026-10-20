/**
 * Host2Play 续期调度器 —— Cloudflare Worker + Cron Trigger
 *
 * 为什么需要它：
 *   GitHub Actions 自带的 schedule 在公开仓库上会被限流。实测 `17 * * * *`
 *   （每小时一次）实际间隔 3.6 小时，中间漏了 3 次。对"续期后只有 8 小时窗口"
 *   的服务器来说，漏 3 次就等于过期。Cloudflare 的 Cron Trigger 准点率高得多。
 *
 * 职责划分：
 *   - Cloudflare（本文件）：只负责"看时间、算要不要续、要续就敲一下 GitHub"。
 *     每分钟跑一次，CPU 耗时几毫秒，远低于免费额度。
 *   - GitHub Actions：真正干活（起 Chrome、解 reCAPTCHA、提交续期）。
 *
 * 关键设计：不在 Worker 里做真正的续期。
 *   Worker 的 CPU 时间上限是 30 秒、无法运行浏览器，也不可能解 reCAPTCHA。
 *   它只做两件事：调 getServerData 读剩余时间；到点调 workflow_dispatch。
 *   这样 99% 的分钟只花一次 HTTP 请求，不消耗任何 Actions 分钟数。
 *
 * 环境变量（wrangler secret / dashboard 配置）：
 *   GITHUB_PAT     必填。Fine-grained PAT，只需 Actions: read and write 权限。
 *   GITHUB_REPO    必填。格式 owner/repo，例如 pttcho/Host2Play
 *   GITHUB_REF     可选。默认 main
 *   RENEW_URLS     必填。逗号分隔的续期链接（与 Actions 里那个 Secret 同值）
 *   SKIP_HOURS     可选。剩余低于这个小时数才触发，默认 2
 *   COOLDOWN_HOURS 可选。两次触发的最小间隔，默认 1（防止重复触发）
 */

const API_BASE = "https://host2play.gratis";
const WORKFLOW_FILE = "Host2Play_Renew.yml";

export default {
  /**
   * Cron Trigger 入口。wrangler.toml 里配 `* * * * *`（每分钟）。
   * 每分钟只做一次轻量检查：读各服务器的 expires，判断是否进入续期窗口。
   */
  async scheduled(event, env, ctx) {
    const now = Date.now();
    const urls = parseUrls(env.RENEW_URLS);
    if (urls.length === 0) {
      console.log("[SKIP] RENEW_URLS 未配置");
      return;
    }

    const skipHours = num(env.SKIP_HOURS, 2);
    const cooldownHours = num(env.COOLDOWN_HOURS, 1);

    // 冷却：避免同一窗口内反复触发（Worker 每分钟都跑，必须自己去重）
    const last = await readLastDispatch(env);
    if (last && now - last < cooldownHours * 3600_000) {
      console.log(`[SKIP] 冷却中，距上次触发 ${((now - last) / 3600_000).toFixed(2)}h`);
      return;
    }

    const reasons = [];
    for (const url of urls) {
      const state = await probe(url);
      if (!state) {
        // 读不到状态时保守触发一次：宁可多跑一轮 Actions，也不能漏续
        reasons.push(`${shortId(url)}: 状态读取失败，保守触发`);
        continue;
      }
      const leftHours = (state.expiresAt - now) / 3600_000;
      if (state.dead) {
        console.log(`[DEAD] ${shortId(url)} 数据已删除，忽略`);
        continue;
      }
      if (leftHours <= skipHours) {
        reasons.push(
          `${shortId(url)}: 剩余 ${leftHours.toFixed(2)}h <= ${skipHours}h`
        );
      } else {
        console.log(
          `[WAIT] ${shortId(url)} 剩余 ${leftHours.toFixed(2)}h，还不需要续期`
        );
      }
    }

    if (reasons.length === 0) {
      console.log("[SKIP] 没有服务器进入续期窗口");
      return;
    }

    console.log(`[TRIGGER] ${reasons.join(" | ")}`);
    const ok = await dispatch(env, "cloudflare");
    if (ok) {
      await writeLastDispatch(env, now);
    }
  },

  /** 手动触发用：GET / 或 /trigger?force=1，方便部署后自测 */
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return json({ ok: true, now: new Date().toISOString() });
    }
    if (url.pathname === "/probe") {
      const urls = parseUrls(env.RENEW_URLS);
      const out = [];
      for (const u of urls) {
        const s = await probe(u);
        out.push({
          id: shortId(u),
          ...(s
            ? {
                name: s.name,
                expiresAt: new Date(s.expiresAt).toISOString(),
                leftHours: +((s.expiresAt - Date.now()) / 3600_000).toFixed(2),
                dead: s.dead,
              }
            : { error: "读取失败" }),
        });
      }
      return json(out);
    }
    if (url.pathname === "/trigger") {
      // 手动强推一次，绕过冷却（调试用）
      const ok = await dispatch(env, "manual-worker");
      return json({ triggered: ok });
    }
    return json({
      usage: ["/health", "/probe", "/trigger"],
      note: "cron 每分钟自动检查，到点才触发 GitHub Actions",
    });
  },
};

// ---------------------------------------------------------------------------
// GitHub 触发
// ---------------------------------------------------------------------------
async function dispatch(env, source) {
  const repo = (env.GITHUB_REPO || "").trim();
  const pat = (env.GITHUB_PAT || "").trim();
  const ref = (env.GITHUB_REF || "main").trim();
  if (!repo || !pat) {
    console.log("[ERROR] GITHUB_REPO / GITHUB_PAT 未配置");
    return false;
  }

  const api = `https://api.github.com/repos/${repo}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  try {
    const resp = await fetch(api, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${pat}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        // GitHub 要求带 UA，否则部分场景会被拒
        "User-Agent": "host2play-cf-cron",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref,
        inputs: { retry: "0", source: source || "cloudflare" },
      }),
    });
    if (resp.status === 204) {
      console.log("[OK] 已触发 GitHub Actions");
      return true;
    }
    const text = await resp.text();
    console.log(`[ERROR] dispatch ${resp.status}: ${text.slice(0, 300)}`);
    return false;
  } catch (e) {
    console.log(`[ERROR] dispatch 异常: ${e}`);
    return false;
  }
}

// ---------------------------------------------------------------------------
// 读取服务器状态
// ---------------------------------------------------------------------------
async function probe(url) {
  const id = extractId(url);
  if (!id) return null;
  try {
    const resp = await fetch(
      `${API_BASE}/publicapis/getServerData?i=${encodeURIComponent(id)}`,
      {
        headers: {
          Accept: "application/json",
          "User-Agent":
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        },
        // 用 cf 缓存不参与：该接口是 DYNAMIC，必须拿实时值
        cf: { cacheTtl: 0, cacheEverything: false },
      }
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    if (!data || Number(data.success) !== 1) {
      const msg = String(data && data.message ? data.message : "");
      if (msg.toLowerCase().includes("server not found")) {
        return { dead: true, expiresAt: 0, name: "" };
      }
      return null;
    }
    const body = data.response || {};
    const expiresAt = Number(body.expires) * 1000;
    if (!Number.isFinite(expiresAt) || expiresAt <= 0) return null;
    return { expiresAt, name: String(body.name || ""), dead: false };
  } catch (e) {
    console.log(`[WARN] probe 异常: ${e}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// 冷却状态（用 Cache API 存，不占 KV 配额，重启后自然失效也没关系）
// ---------------------------------------------------------------------------
async function readLastDispatch(env) {
  try {
    const hit = await caches.default.match("https://internal/last-dispatch");
    if (!hit) return 0;
    const t = Number(await hit.text());
    return Number.isFinite(t) ? t : 0;
  } catch {
    return 0;
  }
}

async function writeLastDispatch(env, ts) {
  try {
    await caches.default.put(
      "https://internal/last-dispatch",
      new Response(String(ts), {
        headers: { "Cache-Control": "max-age=86400" },
      })
    );
  } catch {
    /* 缓存失败不影响主流程，最坏是多触发一次 */
  }
}

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------
function parseUrls(raw) {
  return String(raw || "")
    .split(/[,\n\r]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function extractId(url) {
  try {
    return new URL(url).searchParams.get("i") || "";
  } catch {
    return "";
  }
}

function shortId(url) {
  const id = extractId(url);
  return id ? `${id.slice(0, 4)}…${id.slice(-4)}` : "unknown";
}

function num(v, dflt) {
  const n = Number(v);
  return Number.isFinite(n) ? n : dflt;
}

function json(obj) {
  return new Response(JSON.stringify(obj, null, 2), {
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

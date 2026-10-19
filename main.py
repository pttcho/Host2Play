#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host2Play 免费服务器自动续期脚本。

在 GitHub Actions（ubuntu-latest + Xvfb 虚拟显示）上工作：先用 HTTP API 读真实到期
时间戳，需要续期时才启动 Chrome 解 reCAPTCHA 音频验证，最后用同源 fetch 提交续期
请求，并把结果推送到 Telegram。

## 架构依据（实测证据，不是推测）

站点有三个关键事实，决定了本脚本的形状：

1. 存在免鉴权只读接口，返回精确 Unix 秒级时间戳，不需要 cookie / CSRF / captcha：

       GET /publicapis/getServerData?i=<id>
       200 {"success":1,"response":{"expires":"1792449295","deletes":"1793025295","name":"fvf8042"}}

2. 续期是纯 API POST，前端 JS 原文如此（CSRF 由服务端真校验，假 token 实测 403）：

       var body = JSON.stringify({captcha: grecaptcha.getResponse()})
       fetch("/publicapis/renewServer?i="+id,
             {method:"POST", headers:{'CSRF-Token': csrfToken,
              'Content-Type':'application/json'}, body})

   带真 CSRF + 真 `_csrf` cookie、captcha 传假值时实测返回：

       200 {"success":0,"message":"Failed to verify captcha"}

   即：CSRF 通过之后，服务端唯一剩下的门就是 reCAPTCHA。失败是 **HTTP 200 +
   success:0**，绝不能靠状态码判成败；`message` 是精确的人类可读原因。

3. 页面上 "Expires in:" 只是 `moment().startOf('day').seconds(剩余秒数)` 的倒计时，
   被 setInterval 每 500ms 刷新一次，**不是日期**；"Deletes on:" 才是
   `YYYY/MM/DD HH:mm:ss` 的绝对时间。旧实现拿倒计时做字符串比较判成功，等于在比较
   一个跳动中的时钟 —— 这是"续期很不靠谱"的直接原因之一。

## 可靠性设计要点

1. 成功判定改为时间戳比较（`new_expires > old_expires`），彻底摆脱字符串/倒计时。
2. 三层重试彼此独立：单页面验证码 CAPTCHA_MAX_PER_PAGE 次、单 URL 重开页面
   PAGE_ATTEMPTS 次、单 URL 换 IP IP_ROTATIONS 次。旧实现"验证码 3 次失败即整条
   URL 判死"是续期失败的第一大原因。
3. 识别失败必须分类：`RequestError`（到不了 Google）→ 换 IP；`UnknownValueError`
   （听不懂）→ 换一道题。混为一谈会同时浪费 IP 额度和验证码额度。
4. API 返回的 `message` 直接当失败原因：captcha 相关 → 换题重试；其它 → 最终失败，
   不再重试（重试也是同样结果，只会白烧验证码）。
5. 全部固定 sleep 换成 `wait_for(pred, timeout)` 轮询：命中即返回，不空等不竞态。
6. 跨 run 状态 `state.json` 记住上次成功时间与剩余时间，冷却/剩余充足时直接跳过，
   跳过路径从分钟级降到秒级。
7. Chrome 按需重建，user_data_dir 用完即删；API 连续不可用时自动降级到纯 DOM 流程。
8. 站点选择器集中在 `SELECTORS`，站点改版用 `--check` 一屏定位。

模块被 import 时不产生任何网络请求或浏览器进程；所有副作用都在 main() 里。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, unquote, urlparse

# ---------------------------------------------------------------------------
# 可选依赖：缺失时不能让 import 失败，否则 --check 和纯函数路径都会被拖死
# ---------------------------------------------------------------------------
try:
    import requests
except Exception:  # pragma: no cover - 环境差异
    requests = None

try:
    from DrissionPage import ChromiumOptions, ChromiumPage
except Exception:  # pragma: no cover - 环境差异
    ChromiumOptions = None
    ChromiumPage = None

try:
    from xvfbwrapper import Xvfb
except Exception:  # pragma: no cover - 环境差异
    Xvfb = None

try:
    import speech_recognition as sr
except Exception:  # pragma: no cover - 环境差异
    sr = None

try:
    from pydub import AudioSegment
    from pydub.silence import detect_leading_silence
except Exception:  # pragma: no cover - 环境差异
    AudioSegment = None
    detect_leading_silence = None


# ===========================================================================
# 站点常量与选择器（站点改版时只需要改这一段）
# ===========================================================================
API_BASE = "https://host2play.gratis"
API_GET_SERVER = "/publicapis/getServerData"
API_RENEW_SERVER = "/publicapis/renewServer"

# 模块级开关：API 连续不可用时自动置 False，退化为纯 DOM 流程
USE_API = True
API_FAILURE_BUDGET = 3

SELECTORS = {
    # ---- 页面主体 ----
    "server_name": [
        "#serverName",
        "css:.server-name",
        "css:[data-testid='server-name']",
        "xpath://h1[contains(@class,'server')]",
    ],
    # "Expires in:" 是倒计时，"Deletes on:" 才是绝对时间，两个都收
    "expiry": [
        "#expireDate",
        "text:Expires in:",
        "text:Deletes on:",
        "xpath://*[contains(text(),'Expires in:')]",
        "xpath://*[contains(text(),'Deletes on:')]",
    ],
    "delete_date": ["#deleteDate", "text:Deletes on:"],
    "csrf_meta": ['xpath://meta[@name="csrf-token"]', 'css:meta[name="csrf-token"]'],
    "captcha_container": ["#recaptchax"],
    "renew_open": [
        "xpath://button[contains(normalize-space(.),'Renew server')]",
        "xpath://button[contains(.,'Renew server')]",
        "xpath://a[contains(.,'Renew server')]",
    ],
    "renew_dialog_open": [
        "xpath://div[@role='dialog']//button[contains(.,'Renew server')]",
        "xpath://div[contains(@class,'modal')]//button[contains(.,'Renew server')]",
        "xpath://*[contains(@class,'swal2')]//button[contains(.,'Renew server')]",
    ],
    # 仅 DOM 降级路径使用：SweetAlert2 的确认按钮
    "renew_confirm": [
        "xpath://button[contains(@class,'swal2-confirm')]",
        "xpath://button[normalize-space(.)='Renew']",
        "xpath://button[starts-with(normalize-space(.),'Renew') and not(contains(.,'server'))]",
    ],
    "consent": [
        "xpath://button[contains(.,'Consent')]",
        "xpath://button[contains(.,'Accept')]",
    ],
    # ---- reCAPTCHA（在 bframe 内部查找）----
    "captcha_anchor": ["#recaptcha-anchor"],
    "captcha_audio_button": ["#recaptcha-audio-button"],
    "captcha_audio_input": ["#audio-response"],
    "captcha_audio_source": ["#audio-source"],
    "captcha_audio_link": [
        ".rc-audiochallenge-tdownload-link",
        ".rc-audiochallenge-ndownload-link",
    ],
    "captcha_reload": ["#recaptcha-reload-button"],
    "captcha_verify": ["#recaptcha-verify-button"],
    # 封锁判定用的 DOM 标记（在 bframe/anchor 内部）
    "captcha_blocked_header": [".rc-doscaptcha-header-text"],
    "captcha_blocked_error": [".rc-audiochallenge-error-message"],
    "captcha_status": ["#recaptcha-accessible-status"],
}

# 每个选择器键属于哪个 DOM 阶段，--check 用它分组显示
SELECTOR_STAGE = {
    "server_name": "页面",
    "expiry": "页面",
    "delete_date": "页面",
    "csrf_meta": "页面",
    "captcha_container": "页面",
    "renew_open": "页面",
    "consent": "页面",
    "renew_dialog_open": "弹窗",
    "renew_confirm": "弹窗",
    "captcha_anchor": "验证码",
    "captcha_audio_button": "验证码",
    "captcha_audio_input": "验证码",
    "captcha_audio_source": "验证码",
    "captcha_audio_link": "验证码",
    "captcha_reload": "验证码",
    "captcha_verify": "验证码",
    "captcha_blocked_header": "验证码",
    "captcha_blocked_error": "验证码",
    "captcha_status": "验证码",
}

CAPTCHA_SELECTOR_KEYS = tuple(k for k, v in SELECTOR_STAGE.items() if v == "验证码")

# 服务端 message 里出现这些字样 → captcha 被拒，换题重试即可（IP 没被封）
CAPTCHA_MESSAGE_HINTS = ("captcha", "recaptcha", "robot", "verification")

# 页面把"已过期但在 7 天保留期内"渲染成纯文本，不是时间。必须显式识别，
# 否则会被当成"解析失败"，进而误判成功或误判不可续期。
EXPIRED_MARKERS = ("server expired", "server has expired")

# 兜底成功关键词（仅当时间戳和到期时间都拿不到时使用）
SUCCESS_KEYWORDS = (
    "successfully renewed",
    "has been renewed",
    "renewed successfully",
    "renewal successful",
)

STEALTH_JS = r"""
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel(R) UHD Graphics 630';
    return getParameter.apply(this, [p]);
};
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

# 提交续期：在页面上下文里跑，浏览器自动带 cookie 与同源，不用手工搬 CSRF。
# 结果写 window.__h2p_result，Python 侧轮询读取，避免依赖 run_js 对 Promise 的支持。
RENEW_JS = r"""
(function () {
  window.__h2p_result = null;
  function collectTokens() {
    var out = [];
    try {
      var t = grecaptcha.getResponse();
      if (t && t.length > 30) out.push(t);
    } catch (e) {}
    try {
      var cfg = window.___grecaptcha_cfg;
      if (cfg && cfg.clients) {
        Object.keys(cfg.clients).forEach(function (k) {
          var client = cfg.clients[k];
          Object.keys(client).forEach(function (kk) {
            var v = client[kk];
            if (v && typeof v === 'object' && typeof v.getResponse === 'function') {
              try {
                var t2 = v.getResponse();
                if (t2 && t2.length > 30) out.push(t2);
              } catch (e) {}
            }
          });
        });
      }
    } catch (e) {}
    return out;
  }

  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? (meta.getAttribute('content') || '') : '';
  if (!csrf) {
    window.__h2p_result = {ok: false, stage: 'no_csrf', err: '页面缺少 csrf-token meta'};
    return 'done';
  }
  var tokens = collectTokens();
  if (!tokens.length) {
    window.__h2p_result = {ok: false, stage: 'no_token', err: 'grecaptcha.getResponse() 为空'};
    return 'done';
  }
  var serverId = '';
  try {
    serverId = new URLSearchParams(location.search).get('i') || '';
  } catch (e) {
    serverId = '';
  }
  fetch('/publicapis/renewServer?i=' + encodeURIComponent(serverId), {
    method: 'POST',
    headers: {'CSRF-Token': csrf, 'Content-Type': 'application/json'},
    body: JSON.stringify({captcha: tokens[0]})
  }).then(function (resp) {
    return resp.text().then(function (text) {
      var payload = null;
      try {
        payload = JSON.parse(text);
      } catch (e) {
        payload = null;
      }
      if (payload === null) {
        window.__h2p_result = {
          ok: false, stage: 'parse', status: resp.status,
          err: '响应不是 JSON: ' + String(text).slice(0, 200)
        };
        return;
      }
      window.__h2p_result = {
        ok: payload.success === 1, stage: 'api', status: resp.status,
        message: (payload && payload.message) || '', raw: payload
      };
    });
  }).catch(function (e) {
    window.__h2p_result = {ok: false, stage: 'network', err: String(e)};
  });
  return 'started';
})()
"""

# 续期链接从这里读，但**默认留空**。
#
# 为什么留空：续期链接里的 i=<uuid> 是免鉴权的续期凭据 —— 任何拿到它的人都能
# 调 /publicapis/renewServer 给你的服务器续期（不需要登录、不需要密码）。
# 把它写死在源码里再推到 public 仓库，等于把凭据公开。
#
# 正确用法：在仓库 Settings → Secrets and variables → Actions 里加一条
#   RENEW_URLS = https://host2play.gratis/server/renew?i=xxx,https://...?i=yyy
# workflow 会通过环境变量注入（见 .github/workflows/Host2Play_Renew.yml）。
# 本地调试则用同名环境变量，或临时改这里但**不要提交**。
DEFAULT_RENEW_URLS = []

STATE_VERSION = 1
DEFAULT_STATE_FILE = "state.json"
SCREENSHOT_DIR = "output/screenshots"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ===========================================================================
# 日志
# ===========================================================================
def log(msg: str, level: str = "INFO") -> None:
    prefix = {
        "INFO": "[INFO]",
        "WARN": "[WARN]",
        "ERROR": "[ERROR]",
        "STEP": "[STEP]",
        "DEBUG": "[DEBUG]",
    }.get(str(level).upper(), "[INFO]")
    print(f"{prefix} {msg}", flush=True)


# ===========================================================================
# 纯函数区（SPEC §2 + 本次架构新增）—— 不依赖第三方库，不产生副作用
# ===========================================================================
_EXPIRY_PREFIX_RE = re.compile(
    r"^\s*(?:expir\w*|delet\w*|renew\w*|valid\w*|remaining)\s*"
    r"(?:(?:in|on|at|until|from)\b)?\s*[:\-\u2013\u2014]?\s*",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "w": 604800.0,
    "wk": 604800.0,
    "wks": 604800.0,
    "week": 604800.0,
    "weeks": 604800.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
}

_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(weeks?|wks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)

_ABS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%d %b %Y %H:%M:%S",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)


_COUNTDOWN_RE = re.compile(r"^\d{1,3}:\d{2}:\d{2}$")


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _strip_expiry_prefix(text: str) -> str:
    stripped = _EXPIRY_PREFIX_RE.sub("", text, count=1).strip()
    return stripped or text.strip()


def _parse_absolute(text: str):
    cand = text.strip()
    if not cand:
        return None
    if cand[-1] in ("Z", "z") and len(cand) > 10:
        cand = cand[:-1] + "+00:00"
    try:
        return _to_utc(datetime.fromisoformat(cand))
    except Exception:
        pass
    for fmt in _ABS_FORMATS:
        try:
            return _to_utc(datetime.strptime(cand, fmt))
        except Exception:
            continue
    return None


def _parse_relative(text: str):
    lowered = text.lower()
    # "4 days ago" 是过去时间，绝不能当成未来的到期时间
    if " ago" in lowered or "前" in text:
        return None
    total = 0.0
    found = False
    for match in _UNIT_RE.finditer(text):
        unit = match.group(2).lower()
        seconds = _UNIT_SECONDS.get(unit)
        if seconds is None:
            continue
        found = True
        total += float(match.group(1)) * seconds
    if not found or total <= 0:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=total)


def parse_expiry(text):
    """把页面上的到期文案解析成 UTC aware datetime，无法解析返回 None。

    支持 "Expires in: 2 days" / "Expires in: 23 hours 30 minutes" /
    "Deletes on: 2026-10-17 20:19:49" / "2026-10-17T20:19:49Z" /
    "2026-10-17" / "Expires in: 45 minutes"，其余一律 None。绝不抛异常。

    注意 "Expires in: 06:39:55" 这类倒计时返回 None —— 它是时钟不是日期，把它当
    到期时间做比较正是旧实现误判的根源。绝对时间请用 parse_ts()。
    """
    try:
        if text is None:
            return None
        if isinstance(text, datetime):
            return _to_utc(text)
        if not isinstance(text, str):
            try:
                text = str(text)
            except Exception:
                return None
        raw = text.strip()
        if not raw:
            return None
        # 状态文案不是时间："Server expired" 是过期标记，解析成时间会污染状态判定
        if "expired" in raw.lower():
            return None
        # 倒计时形态 HH:MM:SS 也不是到期时间，是页面每 500ms 重刷的余量
        if _COUNTDOWN_RE.match(raw):
            return None
        candidate = _strip_expiry_prefix(raw)
        if not candidate:
            return None
        if _COUNTDOWN_RE.match(candidate):
            return None
        absolute = _parse_absolute(candidate)
        if absolute is not None:
            return absolute
        return _parse_relative(candidate)
    except Exception:
        return None


def parse_ts(value):
    """把秒级 Unix 时间戳（字符串或数字）转成 UTC aware datetime，非法返回 None。

    站点返回的是字符串形式的秒级时间戳，例如 "1792449295"。毫秒级（>1e11）自动
    降级处理；ISO 字符串顺带兼容，方便直接喂 state.json 里的值。绝不抛异常。
    """
    try:
        if value is None:
            return None
        if isinstance(value, datetime):
            return _to_utc(value)
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            text = str(value).strip()
            if not text:
                return None
            try:
                number = float(text)
            except Exception:
                # 不是数字就按 ISO/日期文案再试一次，比直接放弃更有用
                return parse_expiry(text)
        if number < 0:
            return None
        if number > 1e11:  # 秒级到 1e11 已是公元 5138 年，超出必然是毫秒
            number = number / 1000.0
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except Exception:
        return None


def extract_server_id(url):
    """从续期链接里取 ?i=<id>，没有则返回空串。"""
    try:
        raw = url if isinstance(url, str) else str(url or "")
        try:
            query = parse_qs(urlparse(raw).query, keep_blank_values=True)
        except Exception:
            query = {}
        values = query.get("i")
        if values and str(values[0]).strip():
            return str(values[0]).strip()
        match = re.search(r"[?&]i=([^&#]+)", raw)
        if match:
            return unquote(match.group(1)).strip()
        return ""
    except Exception:
        return ""


def server_key(url):
    """从续期链接算一个稳定、**不可逆**的 key，用作 state.json 的键。

    为什么不能直接用 ?i=<id>：那个 id 就是续期凭据本身。state.json 要提交回
    仓库（跨运行记忆），仓库又是 public，等于把凭据二次泄露一遍 —— 而且是以
    "看起来像内部状态"的形式躺在版本历史里，比写在源码里更难被发现。

    这里统一走 sha1 前 12 位：同一个链接永远得到同一个 key（冷却/历史记录才能
    对上），但无法从 key 反推出原始 id。12 位十六进制 = 48 bit，单仓库量级下
    碰撞概率可忽略。
    """
    try:
        raw = url.strip() if isinstance(url, str) else str(url or "").strip()
        if not raw:
            return "000000000000"
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]
    except Exception:
        return "000000000000"


def should_skip(entry, now, cooldown_hours):
    """判断当前 URL 是否可以因为冷却而跳过。返回 (skip, reason)。"""
    try:
        if not entry or not isinstance(entry, dict):
            return False, "首次运行"
        try:
            cooldown = float(cooldown_hours)
        except Exception:
            cooldown = 0.0
        if cooldown <= 0:
            return False, "需要续期"
        last_success = entry.get("last_success_at")
        if not isinstance(last_success, str) or not last_success.strip():
            return False, "需要续期"
        last_dt = parse_expiry(last_success)
        if last_dt is None:
            return False, "需要续期"
        if now is None:
            now_dt = datetime.now(timezone.utc)
        elif isinstance(now, datetime):
            now_dt = _to_utc(now)
        else:
            now_dt = parse_expiry(str(now))
            if now_dt is None:
                return False, "需要续期"
        elapsed_hours = (now_dt - last_dt).total_seconds() / 3600.0
        if elapsed_hours < cooldown:
            if elapsed_hours < 0:
                elapsed_hours = 0.0
            return True, f"冷却中: 距上次成功 {elapsed_hours:.1f}h"
        return False, "需要续期"
    except Exception:
        return False, "需要续期"


def _shape_ok(data) -> tuple:
    """把两种数据形状统一成 (ok_flag, message, body)。

    classify_server 会被两种调用方喂数据：fetch_server_data 的归一化形状
    {"ok": bool, "error": str, "expires_ts": int, "raw": {...}}，以及服务端原始
    JSON {"success": 0|1, "message": str, "response": {...}}。只认一种就会在另一种
    上误判 —— 例如把 "Server not found" 当成"未返回到期时间"，于是继续重试。
    """
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else data

    ok_flag = data.get("ok")
    if ok_flag is None:
        if "success" in raw:
            ok_flag = _success_is_one(raw)
        else:
            # 既无 ok 也无 success：精简形状（只给时间戳），按可用处理
            ok_flag = True
    ok_flag = bool(ok_flag)

    message = str(data.get("error") or raw.get("message") or "")
    body = raw.get("response") if isinstance(raw.get("response"), dict) else raw
    return ok_flag, message, body


def _expiry_dt(data):
    """从任一数据形状里取到期 datetime，取不到返回 None。"""
    if not isinstance(data, dict):
        return None
    _ok, _message, body = _shape_ok(data)
    return (
        parse_ts(data.get("expires_ts"))
        or parse_ts(data.get("expires"))
        or parse_ts(body.get("expires"))
        or parse_ts(body.get("expires_ts"))
    )


def classify_server(data, now):
    """把服务器状态分成四类，返回 (状态, 中文说明)。

    状态取值固定为 online / expired / dead / unknown。

    为什么必须分开：站点有"到期"和"删除"两个时间点，中间约 7 天是保留期。
    `now > expires` 但 `now < deletes` 时续期**依然有效**，这是最该续的时候；
    而超过 deletes 后服务器数据已被删除，任何重试都是白烧验证码。旧实现把三种
    状态混成"失败"，还把页面上的 "Server expired" 当成成功（因为字符串不相等），
    既发假通知又在下轮重复烧额度。
    """
    try:
        if now is None:
            now_dt = datetime.now(timezone.utc)
        elif isinstance(now, datetime):
            now_dt = _to_utc(now)
        else:
            now_dt = parse_expiry(str(now)) or datetime.now(timezone.utc)

        if not data or not isinstance(data, dict):
            return "unknown", "getServerData 不可用"

        ok_flag, message, body = _shape_ok(data)

        if not ok_flag:
            if "server not found" in message.lower():
                return "dead", "服务器数据已删除（超过 7 天保留期）"
            return "unknown", f"API 拒绝: {message or '未知原因'}"

        expires_dt = (
            parse_ts(data.get("expires_ts"))
            or parse_ts(data.get("expires"))
            or parse_ts(body.get("expires"))
            or parse_ts(body.get("expires_ts"))
        )
        if expires_dt is None:
            return "unknown", "API 未返回到期时间"
        deletes_dt = (
            parse_ts(data.get("deletes_ts"))
            or parse_ts(data.get("deletes"))
            or parse_ts(body.get("deletes"))
            or parse_ts(body.get("deletes_ts"))
        )

        if deletes_dt is not None and now_dt >= deletes_dt:
            return "dead", f"已过删除时间 {_fmt_dt(deletes_dt)}，数据已删除"

        if now_dt >= expires_dt:
            if deletes_dt is not None:
                left = (deletes_dt - now_dt).total_seconds() / 3600.0
                return (
                    "expired",
                    f"已过期，但仍在保留期内（还剩 {left:.1f}h 可续期）",
                )
            return "expired", "已过期，仍在保留期内可续期"

        remaining = (expires_dt - now_dt).total_seconds() / 3600.0
        return "online", f"剩余 {remaining:.1f}h"
    except Exception:
        return "unknown", "状态判定异常"


def decide_action(data, entry, now, cooldown_hours, skip_remaining_hours, stale_after_hours=0.0):
    """决定这条 URL 该做什么，返回 ("renew"|"skip"|"probe"|"dead", 中文原因)。

    决策表（自上而下，先命中先返回）：
      dead                          → dead（不续期、不重试、不烧额度）
      expired                       → renew（紧急，跳过所有冷却判断）
      超久没成功（stale）            → renew（兜底：调度可能连续被跳过）
      online 且剩余 > 阈值           → skip（已经续够了）
      online 且剩余 <= 阈值          → 冷却期内 skip，否则 renew
      unknown（API 不可用）          → probe（退回 DOM 探测）
    """
    try:
        if now is None:
            now_dt = datetime.now(timezone.utc)
        elif isinstance(now, datetime):
            now_dt = _to_utc(now)
        else:
            now_dt = parse_expiry(str(now)) or datetime.now(timezone.utc)

        state, detail = classify_server(data, now_dt)

        if state == "dead":
            return "dead", detail
        if state == "unknown":
            return "probe", f"{detail}，按原流程尝试"
        if state == "expired":
            return "renew", detail

        # 兜底保险：距离上次成功续期太久，说明调度很可能被连续跳过，
        # 此时即使剩余时间看着还够也要强制续一次。
        try:
            stale_hours = float(stale_after_hours)
        except Exception:
            stale_hours = 0.0
        if stale_hours > 0 and isinstance(entry, dict):
            last_success = parse_expiry(str(entry.get("last_success_at") or ""))
            if last_success is not None:
                idle_hours = (now_dt - last_success).total_seconds() / 3600.0
                if idle_hours >= stale_hours:
                    return "renew", f"已 {idle_hours:.1f}h 未成功续期，触发兜底强制续期"

        expires_dt = _expiry_dt(data)
        if expires_dt is None:
            return "probe", "API 未返回到期时间，按原流程尝试"
        remaining = (expires_dt - now_dt).total_seconds()

        try:
            skip_hours = float(skip_remaining_hours)
        except Exception:
            skip_hours = 0.0
        if skip_hours > 0 and remaining > skip_hours * 3600.0:
            return "skip", f"剩余 {remaining / 3600.0:.1f}h 已超过阈值 {skip_hours:g}h"

        # 进入续期窗口（剩余 <= 阈值）后，冷却只用来防止"同一窗口内反复重试"，
        # 不能把这一轮整个挡掉：调度每小时才来一次，被冷却挡掉就可能直接过期。
        # 因此只在"上次成功距现在不到 cooldown，且剩余时间还很充裕"时才让冷却生效，
        # 一旦剩余时间已经进入窗口，冷却只做一次性的短期去重。
        skip, reason = should_skip(entry, now_dt, cooldown_hours)
        if skip:
            # 剩余时间还够撑过冷却期才允许跳过，否则宁可多续一次也不能过期
            try:
                cooldown = float(cooldown_hours)
            except Exception:
                cooldown = 0.0
            if remaining > max(0.0, cooldown) * 3600.0:
                return "skip", reason
            return "renew", f"{reason}，但剩余仅 {remaining / 3600.0:.1f}h，优先续期"
        return "renew", f"剩余 {remaining / 3600.0:.1f}h，需要续期"
    except Exception:
        return "probe", "决策异常，按原流程尝试"


def _plain(text) -> str:
    """把可能被 Telegram 当成 HTML 实体的字符换成全角。

    服务器名是用户自建的，完全可能带 `<`；文案又同时用于 sendPhoto 的 caption 和
    sendMessage 的 text。换成全角后既可读，又不会被实体解析吃掉。不用
    html.escape()，那会产出 &lt; 在纯文本模式下显示成乱码。
    """
    return (
        str(text)
        .replace("&", "＆")
        .replace("<", "＜")
        .replace(">", "＞")
    )


def _mask_url(url: str) -> str:
    """把续期链接里的 i=<uuid> 打码，只留主机名和 id 的前后各 4 位。

    Telegram 消息、截图、Actions 日志都可能被截图转发或长期留存，完整链接等于
    把免鉴权凭据散播出去。这里保留可辨认性（同一个服务器前后一致）但不可用。
    """
    try:
        raw = str(url or "").strip()
        if not raw:
            return "未知"
        parsed = urlparse(raw)
        host = parsed.netloc or ""
        identifier = ""
        try:
            identifier = (parse_qs(parsed.query).get("i") or [""])[0]
        except Exception:
            identifier = ""
        if not identifier:
            return host or "未知"
        if len(identifier) > 10:
            shown = f"{identifier[:4]}…{identifier[-4:]}"
        else:
            shown = identifier[:2] + "…"
        return f"{host}/…?i={shown}"
    except Exception:
        return "未知"


def build_caption(ok, server_name, url, old_expiry, new_expiry, reason="", skipped=False):
    """生成 Telegram 文案。纯文本，不带任何 HTML 标签，避免 parse_mode 转义问题。"""
    name = _plain((server_name or "").strip() or "未知")
    old = _plain((old_expiry or "").strip() or "未知")
    new = _plain((new_expiry or "").strip() or "未知")
    link = _plain(_mask_url(url))
    why = _plain((reason or "").strip())

    lines = []
    if skipped:
        lines.append("⏭ 已跳过（冷却中）")
        lines.append(f"服务器: {name}")
        lines.append(f"到期: {old}")
        lines.append(f"原因: {why or '冷却中'}")
        lines.append(f"URL: {link}")
    elif ok:
        lines.append("✅ 续订成功")
        lines.append(f"服务器: {name}")
        lines.append(f"到期: {old} -> {new}")
        lines.append(f"URL: {link}")
    else:
        lines.append("❌ 续订失败")
        lines.append(f"服务器: {name}")
        lines.append(f"原因: {why or '未知'}")
        lines.append(f"URL: {link}")
    return "\n".join(lines)


# ===========================================================================
# 通用小工具
# ===========================================================================
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_dt(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return ""


def wait_for(pred, timeout=10.0, interval=0.4):
    """轮询直到 pred() 返回真值；超时返回 None。pred 抛异常视为未命中。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            value = pred()
        except Exception:
            value = None
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(str(raw).strip())
    except Exception:
        log(f"环境变量 {name}={raw!r} 不是数字，回退默认 {default}", "WARN")
        return float(default)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    value = int(_env_float(name, float(default)))
    return max(minimum, value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _disp_len(text) -> int:
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - _disp_len(text))


def print_table(headers, rows) -> None:
    """打印对齐的文本表格（按东亚字符显示宽度对齐，中文不会错位）。"""
    data = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = []
    for idx, head in enumerate(headers):
        width = _disp_len(head)
        for row in data:
            if idx < len(row):
                width = max(width, _disp_len(row[idx]))
        widths.append(width)

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(sep)
    print("| " + " | ".join(_pad(h, widths[i]) for i, h in enumerate(headers)) + " |")
    print(sep)
    for row in data:
        cells = [(row[i] if i < len(row) else "") for i in range(len(headers))]
        print("| " + " | ".join(_pad(c, widths[i]) for i, c in enumerate(cells)) + " |")
    print(sep)


# ===========================================================================
# HTTP API 层（不启浏览器）
# ===========================================================================
API_LAST_ERROR = ""


def _api_headers(referer: str = "") -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _success_is_one(payload) -> bool:
    """服务端现在给数字 1，但类型不稳定（可能是字符串 "1"），两边都认。"""
    try:
        return int(str(payload.get("success")).strip()) == 1
    except Exception:
        return False


def _content_type_is_json(response) -> bool:
    try:
        headers = getattr(response, "headers", None) or {}
        ctype = str(headers.get("Content-Type") or headers.get("content-type") or "")
    except Exception:
        return True  # 拿不到头就不要因此误杀正常响应
    if not ctype:
        return True
    return "json" in ctype.lower()


def fetch_server_data(url, timeout=20, retries=3):
    """读服务器真实状态。

    返回 dict：
      成功  {"ok": True, "name": str, "expires_ts": int, "deletes_ts": int,
             "expires_dt": datetime, "deletes_dt": datetime, "raw": dict}
      API 明确拒绝 {"ok": False, "error": str, "raw": dict}（如服务器不存在，别重试）
    传输层彻底失败返回 None（网络/Cloudflare 拦截，可以降级或换 IP）。
    不抛异常。

    注意：该接口返回 cf-cache-status: DYNAMIC，即 Cloudflare 不缓存、每次回源，
    所以"续期前后各调一次做对比"是可靠的；但也意味着会真实打到源站，只在预检和
    续期后确认各调一次，不要在重试循环里高频轮询。
    """
    global API_LAST_ERROR
    if requests is None:
        API_LAST_ERROR = "缺少 requests 库"
        return None

    identifier = extract_server_id(url)
    if not identifier:
        API_LAST_ERROR = "链接里没有 i 参数"
        return None

    endpoint = f"{API_BASE}{API_GET_SERVER}?i={quote(identifier)}"
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                endpoint, headers=_api_headers(url), timeout=timeout
            )
        except Exception as exc:
            API_LAST_ERROR = f"网络异常: {exc}"
            log(f"getServerData 网络异常 (第 {attempt}/{attempts} 次): {exc}", "WARN")
            time.sleep(min(8.0, 1.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.6))
            continue

        if response.status_code != 200:
            API_LAST_ERROR = f"HTTP {response.status_code}"
            log(
                f"getServerData HTTP {response.status_code} (第 {attempt}/{attempts} 次)",
                "WARN",
            )
            time.sleep(min(8.0, 1.5 * (2 ** (attempt - 1))))
            continue

        # Cloudflare 拦截时会回 HTML 挑战页，先看类型再解析，别拿 HTML 去 json.loads
        if not _content_type_is_json(response):
            API_LAST_ERROR = "响应 Content-Type 不是 JSON（疑似 Cloudflare 挑战页）"
            log(f"getServerData 非 JSON 响应 (第 {attempt}/{attempts} 次)", "WARN")
            time.sleep(min(8.0, 1.5 * (2 ** (attempt - 1))))
            continue

        try:
            payload = response.json()
        except Exception:
            API_LAST_ERROR = "响应不是 JSON（可能被 Cloudflare 挑战页拦截）"
            log(f"getServerData 返回非 JSON (第 {attempt}/{attempts} 次)", "WARN")
            time.sleep(min(8.0, 1.5 * (2 ** (attempt - 1))))
            continue

        if not isinstance(payload, dict) or "success" not in payload:
            API_LAST_ERROR = "响应格式异常（缺少 success 字段）"
            log(f"getServerData 响应缺 success 字段 (第 {attempt}/{attempts} 次)", "WARN")
            time.sleep(min(8.0, 1.5 * (2 ** (attempt - 1))))
            continue

        if not _success_is_one(payload):
            message = str(payload.get("message") or "API 返回 success != 1")
            API_LAST_ERROR = message
            return {"ok": False, "error": message, "raw": payload}

        body = payload.get("response")
        if not isinstance(body, dict):
            API_LAST_ERROR = "缺少 response 字段"
            return {"ok": False, "error": "API 缺少 response 字段", "raw": payload}

        # expires / deletes 是字符串形式的 Unix 秒（不是毫秒），parse_ts 两种都吃
        expires_dt = parse_ts(body.get("expires"))
        deletes_dt = parse_ts(body.get("deletes"))
        if expires_dt is None:
            API_LAST_ERROR = "expires 字段无法解析"
            return {"ok": False, "error": "API 未返回可解析的 expires", "raw": payload}

        API_LAST_ERROR = ""
        return {
            "ok": True,
            "name": str(body.get("name") or ""),
            "expires_ts": int(expires_dt.timestamp()),
            "deletes_ts": int(deletes_dt.timestamp()) if deletes_dt else 0,
            "expires_dt": expires_dt,
            "deletes_dt": deletes_dt,
            "raw": payload,
        }

    return None


def _is_captcha_message(message: str) -> bool:
    lowered = (message or "").lower()
    return any(hint in lowered for hint in CAPTCHA_MESSAGE_HINTS)


# ===========================================================================
# state.json（SPEC §3）
# ===========================================================================
def _empty_state() -> dict:
    return {"version": STATE_VERSION, "updated_at": "", "servers": {}, "last_run": {}}


# UUID 形态（8-4-4-4-12）或长度 >= 16 的十六进制串，都当作凭据形态
_CREDENTIAL_KEY_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_like_credential_key(key: str) -> bool:
    """判断 state 的 key 是否是把原始 ?i=<uuid> 直接当键的旧格式。

    新格式一律是 12 位 sha1 前缀，所以只要命中 UUID 形态就一定是旧数据。
    """
    try:
        return bool(_CREDENTIAL_KEY_RE.match(str(key or "").strip()))
    except Exception:
        return False


def _entry_leaks_credential(entry: dict) -> bool:
    """条目里是否存了含 ?i= 的完整链接（旧格式的 url 字段）。"""
    try:
        url = str(entry.get("url") or "")
        if not url:
            return False
        if "i=" not in url:
            return False
        return bool((parse_qs(urlparse(url).query).get("i") or [""])[0])
    except Exception:
        return False


def load_state(path: str = DEFAULT_STATE_FILE) -> dict:
    """读 state.json；不存在 / 损坏 / schema 不符一律降级为空 state，绝不抛异常。"""
    state = _empty_state()
    try:
        if not path or not os.path.exists(path):
            return state
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return state
    except Exception as exc:
        log(f"state.json 读取失败，按空状态继续: {exc}", "WARN")
        return state

    if not isinstance(data, dict):
        log("state.json 顶层不是对象，按空状态继续", "WARN")
        return state

    version = data.get("version")
    if version != STATE_VERSION:
        log(
            f"state.json 版本不符 (got={version!r}, want={STATE_VERSION})，按空状态继续",
            "WARN",
        )
        return state

    servers = data.get("servers")
    if not isinstance(servers, dict):
        log("state.json 缺少 servers 字典，按空状态继续", "WARN")
        return state

    clean = {}
    dropped = 0
    for key, value in servers.items():
        if not (isinstance(key, str) and isinstance(value, dict)):
            continue
        # 迁移清理：早期版本直接把 ?i=<uuid> 当 key，并把完整 URL 存进 url 字段。
        # 那种记录既泄露凭据，又与新的哈希 key 对不上（会成为孤儿条目）。这里
        # 一律剔除，让下次运行按新格式重建 —— 代价只是丢一次历史，远小于继续
        # 把凭据提交回 public 仓库。
        if _looks_like_credential_key(key) or _entry_leaks_credential(value):
            dropped += 1
            continue
        clean[key] = value
    if dropped:
        log(
            f"state.json 中剔除 {dropped} 条旧格式记录（含明文凭据），将按新格式重建",
            "WARN",
        )
    state["servers"] = clean

    last_run = data.get("last_run")
    state["last_run"] = last_run if isinstance(last_run, dict) else {}
    updated = data.get("updated_at")
    state["updated_at"] = updated if isinstance(updated, str) else ""
    return state


def save_state(state: dict, path: str = DEFAULT_STATE_FILE) -> bool:
    """原子写：同目录 tmp 文件 + fsync + os.replace，避免半截 JSON 被提交回仓库。

    关键设计：**`last_run` 不写进仓库文件**。
    调度是每小时一次，而 `last_run` 里全是每次必变的字段（started_at /
    finished_at / ok / skipped）。如果照写，就会变成每小时一条"只有时间戳变化"
    的提交，一天 24 条、一个月 720 条，把仓库历史彻底淹没。

    `last_run` 的唯一用途是给 workflow 的 rerun-gate 判断 retryable，那是**本次
    运行内**的事，写进同目录的旁路文件（默认 `state.last_run.json`，已在
    .gitignore 里）就够，不需要跨运行持久化。

    真正需要跨运行记忆的是 `servers`（到期时间、上次成功时间、连续失败数），
    它只在续期成功/失败/状态变化时才变，所以提交频率会降到"只在真有事时"。
    """
    try:
        state["version"] = STATE_VERSION
        state["updated_at"] = _now_iso()

        # 拆成两份：仓库里只放 servers（稳定），last_run 走旁路文件
        persisted = {k: v for k, v in state.items() if k != "last_run"}
        last_run = state.get("last_run")

        payload = json.dumps(persisted, ensure_ascii=False, indent=2, sort_keys=True)

        # 内容没变就整份不写，让 workflow 的 `git diff --cached --quiet` 能判定跳过
        unchanged = False
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    existing = json.load(handle)
                if isinstance(existing, dict):
                    existing.pop("updated_at", None)
                    compare = dict(persisted)
                    compare.pop("updated_at", None)
                    unchanged = existing == compare
            except Exception:
                unchanged = False

        if not unchanged:
            directory = os.path.dirname(os.path.abspath(path)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".state-", suffix=".json", dir=directory
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                raise

        # 旁路文件每次覆盖写（不进仓库，所以随便变）
        if isinstance(last_run, dict) and last_run:
            try:
                side_path = _last_run_path(path)
                side_dir = os.path.dirname(os.path.abspath(side_path)) or "."
                os.makedirs(side_dir, exist_ok=True)
                with open(side_path, "w", encoding="utf-8") as handle:
                    json.dump(last_run, handle, ensure_ascii=False, indent=2, sort_keys=True)
                    handle.write("\n")
            except Exception as exc:
                log(f"last_run 旁路文件写入失败（不影响主流程）: {exc}", "WARN")

        return True
    except Exception as exc:
        log(f"state.json 写入失败: {exc}", "ERROR")
        return False


def _last_run_path(state_path: str) -> str:
    """旁路文件路径：与 state.json 同目录，加 .last_run 后缀，不进仓库。"""
    directory = os.path.dirname(os.path.abspath(state_path)) or "."
    return os.path.join(directory, "state.last_run.json")


# ===========================================================================
# 配置（SPEC §5 + SKIP_IF_REMAINING_HOURS）
# ===========================================================================
@dataclass
class Config:
    urls: list = field(default_factory=list)
    tg_token: str = ""
    tg_chat_id: str = ""
    # 冷却：成功后多久内不重复续期。默认 3h。
    # 不能设成 6h —— 实测续期后剩 8h、阈值 2h 触发，实际间隔约 6.1h，6h 冷却
    # 会把放行窗口压到只剩 6 分钟，一次调度延迟就会被挡住导致漏续。
    cooldown_hours: float = 3.0
    # 剩余时间超过这个小时数就跳过。默认 2 表示"只剩 1~2 小时才动手"。
    #
    # 关键前提：续期是**把 expires 重置为 now + 8h**，不是累加。
    # 实测那次：续期前剩 6.58h，续期后剩 8.32h，净增只有 1.73h —— 因为是重置。
    # 所以"早续"等于把剩余时间直接扔掉：剩 7h 去续，那 7h 就白费了。
    # 配合 8h 窗口，阈值 2h 意味着实际约每 6.1h 续一次，每段额度都吃满。
    skip_remaining_hours: float = 2.0
    budget_minutes: float = 20.0
    dry_run: bool = False
    parallel: int = 1
    captcha_max: int = 8
    page_attempts: int = 6
    ip_rotations: int = 5
    state_path: str = DEFAULT_STATE_FILE
    deadline: float = 0.0
    # 超过这么久没成功续期就无视冷却与阈值，强制续一次。
    # 必须远小于"过期到删除"的 7 天保留期，但又得大于正常的 6h 续期间隔，
    # 否则每次正常运行都会误触发。默认 12h = 正常间隔的两倍，能容忍连续 1-2 次
    # 调度被丢弃，又能在真正卡死时及时兜底。
    stale_after_hours: float = 12.0


def load_config(state_path: str = None) -> Config:
    raw_urls = _env_str("RENEW_URLS")
    if raw_urls:
        urls = [item.strip() for item in re.split(r"[,\n\r]+", raw_urls) if item.strip()]
    else:
        urls = list(DEFAULT_RENEW_URLS)

    return Config(
        urls=urls,
        tg_token=_env_str("TG_BOT_TOKEN"),
        tg_chat_id=_env_str("TG_CHAT_ID"),
        cooldown_hours=_env_float("RENEW_COOLDOWN_HOURS", 3.0),
        skip_remaining_hours=_env_float("SKIP_IF_REMAINING_HOURS", 2.0),
        budget_minutes=_env_float("JOB_BUDGET_MINUTES", 20.0),
        dry_run=_env_bool("DRY_RUN", False),
        parallel=_env_int("PARALLEL_URLS", 1, minimum=1),
        captcha_max=_env_int("CAPTCHA_MAX_PER_PAGE", 8, minimum=1),
        page_attempts=_env_int("PAGE_ATTEMPTS", 6, minimum=1),
        ip_rotations=_env_int("IP_ROTATIONS", 5, minimum=0),
        state_path=state_path or _env_str("STATE_FILE", DEFAULT_STATE_FILE),
        stale_after_hours=_env_float("STALE_AFTER_HOURS", 12.0),
    )


# ===========================================================================
# 异常
# ===========================================================================
class CaptchaBlocked(Exception):
    """Google 明确表示当前 IP 被限制，只能换 IP。"""


class CaptchaUnavailable(Exception):
    """验证码结构异常（找不到 anchor / checkbox），重开页面才有意义。"""


# ===========================================================================
# 虚拟显示 / 浏览器生命周期
# ===========================================================================
_DISPLAY = None
_DISPLAY_LOCK = threading.Lock()


def ensure_display() -> bool:
    """按需启动 Xvfb；已有 DISPLAY 或本机有真实桌面时直接复用。"""
    global _DISPLAY
    with _DISPLAY_LOCK:
        if _DISPLAY is not None:
            return _DISPLAY != "failed"
        if os.getenv("DISPLAY"):
            _DISPLAY = "existing"
            return True
        if Xvfb is None:
            log("未安装 xvfbwrapper 且环境无 DISPLAY，浏览器可能无法启动", "WARN")
            _DISPLAY = "missing"
            return False
        try:
            display = Xvfb(width=1280, height=720, colordepth=24)
            display.start()
            _DISPLAY = display
            log("Xvfb 虚拟显示已启动")
            return True
        except Exception as exc:
            log(f"Xvfb 启动失败: {exc}", "ERROR")
            _DISPLAY = "failed"
            return False


def stop_display() -> None:
    global _DISPLAY
    with _DISPLAY_LOCK:
        if hasattr(_DISPLAY, "stop"):
            try:
                _DISPLAY.stop()
                log("Xvfb 虚拟显示已关闭")
            except Exception:
                pass
        _DISPLAY = None


_CHROME_CANDIDATES = (
    # 显式环境变量优先：自定义安装位置 / 容器镜像靠它兜底
    os.environ.get("CHROME_PATH", ""),
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "google-chrome",
    "chromium",
    # Windows：CI 走不到这几条，但本地用 --check / DRY_RUN 排查站点改版时，
    # 没有它们会先撞上"未找到浏览器"，把环境问题和站点问题混在一起。
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


def _find_chrome() -> str:
    for candidate in _CHROME_CANDIDATES:
        if not candidate:
            continue
        if os.sep in candidate or "/" in candidate:
            if os.path.exists(candidate):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return ""


def open_browser():
    """新建一个 Chrome 实例，返回 (page, user_data_dir)。失败抛异常由调用方兜底。"""
    if ChromiumPage is None or ChromiumOptions is None:
        raise RuntimeError(
            "缺少 DrissionPage，无法启动浏览器。请执行: pip install DrissionPage"
        )
    ensure_display()

    options = ChromiumOptions()
    chrome_path = _find_chrome()
    if chrome_path:
        try:
            options.set_browser_path(chrome_path)
        except Exception:
            pass
    for argument in (
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-setuid-sandbox",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--window-size=1280,720",
        "--log-level=3",
        "--silent",
    ):
        try:
            options.set_argument(argument)
        except Exception:
            pass
    try:
        options.set_user_agent(USER_AGENT)
    except Exception:
        pass

    user_data_dir = tempfile.mkdtemp(prefix="h2p-chrome-")
    try:
        options.set_user_data_path(user_data_dir)
    except Exception:
        pass
    try:
        options.auto_port()
    except Exception:
        pass
    try:
        options.headless(False)
    except Exception:
        pass

    page = ChromiumPage(options)
    # 反指纹脚本必须在导航前注入；两条路径都试，接口差异不该让流程挂掉
    try:
        page.add_init_js(STEALTH_JS)
    except Exception:
        try:
            page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=STEALTH_JS)
        except Exception:
            pass
    return page, user_data_dir


def close_browser(page, user_data_dir: str = None) -> None:
    if page is not None:
        try:
            page.quit()
        except Exception:
            pass
    if user_data_dir:
        # Actions 磁盘只有几十 G，用户数据目录不删很快就满
        shutil.rmtree(user_data_dir, ignore_errors=True)


# ===========================================================================
# 页面读取
# ===========================================================================
def _any_ele(page, selectors, timeout: float = 0.6):
    for selector in selectors:
        try:
            element = page.ele(selector, timeout=timeout)
        except Exception:
            element = None
        if element:
            return element
    return None


def ele_text(page, selectors, timeout: float = 1.0) -> str:
    element = _any_ele(page, selectors, timeout)
    if element is None:
        return ""
    try:
        return (element.text or "").strip()
    except Exception:
        return ""


def wait_text(page, selectors, timeout: float = 15.0) -> str:
    return wait_for(lambda: ele_text(page, selectors, 0.5), timeout) or ""


def extract_expiry_text(raw: str) -> str:
    """从可能夹带其他文案的元素文本里切出到期那一句。"""
    if not raw:
        return ""
    text = " ".join(str(raw).split())
    lowered = text.lower()
    for marker in EXPIRED_MARKERS:
        if marker in lowered:
            # 保留原文，让上层知道"过期了但还能续"，不要当成解析失败
            return text[:120].strip()
    for marker in ("expires in:", "deletes on:", "expires on:", "expire date:"):
        index = lowered.find(marker)
        if index >= 0:
            return text[index:index + 120].strip()
    return text[:120].strip()


_COUNTDOWN_RE = re.compile(r"^\d{1,3}:\d{2}:\d{2}$")


def is_countdown_text(text) -> bool:
    """判断是不是 "06:35:12" 这种实时倒计时。

    倒计时不是日期：它每 500ms 变一次，存进 state 或拿来做前后比较都会立刻失效，
    这正是旧实现误判成功的根源。识别出来就丢弃，改用绝对时间。
    """
    if not text:
        return False
    return bool(_COUNTDOWN_RE.match(" ".join(str(text).split())))


def is_expired_text(text) -> bool:
    if not text:
        return False
    lowered = str(text).lower()
    return any(marker in lowered for marker in EXPIRED_MARKERS)


def read_server_name(page) -> str:
    name = ele_text(page, SELECTORS["server_name"], 1.0)
    return name[:80] if name else ""


def read_expiry_raw(page) -> str:
    """读页面上的到期信息，只返回可存储的值。

    优先级：绝对时间（Deletes on / #deleteDate）> "Server expired" 标记。
    倒计时（06:35:12）直接丢弃返回空串 —— 它每 500ms 变一次，存进 state 或拿来做
    前后比较都会立刻失效，旧实现误判成功正是因为它把倒计时当成了到期时间。
    """
    absolute = extract_expiry_text(ele_text(page, SELECTORS["delete_date"], 1.0))
    if absolute and parse_expiry(absolute) is not None:
        return absolute

    raw = extract_expiry_text(ele_text(page, SELECTORS["expiry"], 1.0))
    if is_expired_text(raw):
        return raw
    if is_countdown_text(raw):
        return ""
    return raw


def read_csrf_token(page) -> str:
    element = _any_ele(page, SELECTORS["csrf_meta"], 3.0)
    if element is not None:
        try:
            token = element.attr("content")
            if token:
                return str(token).strip()
        except Exception:
            pass
    try:
        token = page.run_js(
            "var m=document.querySelector('meta[name=\"csrf-token\"]');"
            "return m ? (m.getAttribute('content')||'') : '';"
        )
        return str(token or "").strip()
    except Exception:
        return ""


def click_first(page, key: str, timeout: float = 5.0) -> bool:
    """点第一个能点到的候选元素；每次尝试都有兜底，单点失败不抛。

    只接受"可见"的元素：站点把整个 #renew 卡片先 display:none 隐藏，等异步
    getServerData 返回后才显示。隐藏状态下 DOM 里能查到按钮，但 click() 不会
    触发任何事，早期版本因此白白耗尽重试次数。
    """
    selectors = SELECTORS.get(key) or []
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for selector in selectors:
            try:
                element = page.ele(selector, timeout=0.6)
            except Exception:
                element = None
            if not element:
                continue
            if not _is_displayed(element):
                continue
            try:
                element.click()
                return True
            except Exception:
                pass
            try:
                element.click(by_js=True)
                return True
            except Exception:
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.4)


def dismiss_overlays(page) -> None:
    try:
        page.run_js(
            "(function(){['ins.adsbygoogle','iframe[src*=\"ads\"]','.modal-backdrop']"
            ".forEach(function(s){document.querySelectorAll(s).forEach(function(e){e.remove();});});})();"
        )
    except Exception:
        pass
    try:
        element = _any_ele(page, SELECTORS["consent"], 0.6)
        if element is not None:
            try:
                element.click()
            except Exception:
                try:
                    element.click(by_js=True)
                except Exception:
                    pass
    except Exception:
        pass


def humanize(page) -> None:
    """少量随机滚动与鼠标移动，降低自动化特征；时长随机，不做固定等待。"""
    try:
        for _ in range(random.randint(1, 3)):
            try:
                page.scroll.down(random.randint(150, 450))
            except Exception:
                pass
            try:
                page.actions.move(random.randint(80, 900), random.randint(80, 560))
            except Exception:
                pass
            time.sleep(random.uniform(0.15, 0.5))
    except Exception:
        pass


def capture_screenshot(page, name: str) -> str:
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    except Exception:
        return ""
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(name))[:80].strip("_") or "shot"
    filename = f"{safe}.png"
    full_path = os.path.join(SCREENSHOT_DIR, filename)
    # DrissionPage 的 path 参数是目录、name 是文件名；两条路径都试，兼容版本差异
    try:
        page.get_screenshot(path=SCREENSHOT_DIR, name=filename)
        if os.path.isfile(full_path):
            return full_path
    except Exception:
        pass
    try:
        page.get_screenshot(path=full_path)
        if os.path.isfile(full_path):
            return full_path
    except Exception as exc:
        log(f"截图失败: {exc}", "WARN")
    return ""


# ===========================================================================
# reCAPTCHA
# ===========================================================================
_SOLVED_JS = (
    "return (function(){var e=document.querySelector(\"textarea[name='g-recaptcha-response']\");"
    "return (e && e.value) ? e.value : '';})();"
)

def _blocked_js() -> str:
    """封锁判定脚本由 SELECTORS 拼装，站点改版时只改一处。"""

    def first(key):
        values = SELECTORS.get(key) or []
        return values[0] if values else ""

    return r"""
return (function(){
  var headerSel = %s;
  var errSel = %s;
  var statusSel = %s;
  var text = (document.body ? document.body.innerText : '') || '';
  text = text.toLowerCase();
  if (text.indexOf('try again later') >= 0) return true;
  var header = headerSel ? document.querySelector(headerSel) : null;
  if (header && (header.textContent || '').toLowerCase().indexOf('try again later') >= 0) return true;
  var err = errSel ? document.querySelector(errSel) : null;
  if (err && err.offsetParent !== null) {
    var msg = (err.textContent || '').toLowerCase();
    if (msg.indexOf('automated queries') >= 0) return true;
    if (msg.indexOf('try again later') >= 0) return true;
    if (msg.indexOf('your computer or network') >= 0) return true;
  }
  var status = statusSel ? document.querySelector(statusSel) : null;
  if (status && (status.textContent || '').toLowerCase().indexOf('try again later') >= 0) return true;
  return false;
})();
""" % (
        json.dumps(first("captcha_blocked_header")),
        json.dumps(first("captcha_blocked_error")),
        json.dumps(first("captcha_status")),
    )


def find_frame(page, kind: str):
    if page is None:
        return None
    try:
        frames = page.get_frames()
    except Exception:
        return None
    for frame in frames or []:
        try:
            frame_url = (frame.url or "").lower()
        except Exception:
            frame_url = ""
        if "recaptcha" in frame_url and kind in frame_url:
            return frame
    return None


def _frame_js(page, kind: str, script: str, default=None):
    frame = find_frame(page, kind)
    if frame is None:
        return default
    try:
        return frame.run_js(script)
    except Exception:
        return default


def is_solved(page) -> bool:
    if page is None:
        return False
    try:
        token = page.run_js(_SOLVED_JS)
        if token and len(str(token)) > 30:
            return True
    except Exception:
        pass
    for kind in ("anchor", "bframe"):
        token = _frame_js(page, kind, _SOLVED_JS)
        if token and len(str(token)) > 30:
            return True
    checked = _frame_js(
        page,
        "anchor",
        "return (function(){var e=document.querySelector('#recaptcha-anchor');"
        "return !!(e && e.getAttribute('aria-checked') === 'true');})();",
    )
    return bool(checked)


def is_blocked(page) -> bool:
    if page is None:
        return False
    script = _blocked_js()
    for kind in ("bframe", "anchor"):
        if _frame_js(page, kind, script, default=False):
            return True
    return False


def _is_displayed(element) -> bool:
    try:
        return bool(element.states.is_displayed)
    except Exception:
        return True


def _audio_input(page):
    frame = find_frame(page, "bframe")
    if frame is None:
        return None
    for selector in SELECTORS["captcha_audio_input"]:
        try:
            element = frame.ele(selector, timeout=1)
        except Exception:
            element = None
        if element and _is_displayed(element):
            return element
    return None


def click_checkbox(page) -> None:
    anchor = wait_for(lambda: find_frame(page, "anchor"), 25)
    if anchor is None:
        raise CaptchaUnavailable("未找到 reCAPTCHA anchor frame")
    checkbox = wait_for(
        lambda: anchor.ele(SELECTORS["captcha_anchor"][0], timeout=1), 10
    )
    if checkbox is None:
        raise CaptchaUnavailable("未找到 reCAPTCHA 复选框")
    try:
        page.actions.move_to(checkbox, duration=random.uniform(0.35, 0.9))
    except Exception:
        pass
    time.sleep(random.uniform(0.15, 0.4))
    try:
        checkbox.click()
    except Exception:
        try:
            checkbox.click(by_js=True)
        except Exception as exc:
            raise CaptchaUnavailable(f"复选框点击失败: {exc}")


def switch_to_audio(page, tries: int = 3) -> bool:
    if _audio_input(page):
        return True
    frame = find_frame(page, "bframe")
    if frame is None:
        return False
    for _ in range(max(1, tries)):
        if is_blocked(page):
            raise CaptchaBlocked("切换音频模式时检测到封锁提示")
        button = None
        for selector in SELECTORS["captcha_audio_button"]:
            try:
                button = frame.ele(selector, timeout=1)
            except Exception:
                button = None
            if button:
                break
        if button is not None:
            try:
                button.click()
            except Exception:
                try:
                    button.click(by_js=True)
                except Exception:
                    pass
        else:
            try:
                frame.run_js(
                    "var b=document.querySelector('#recaptcha-audio-button'); if(b){b.click();}"
                )
            except Exception:
                pass
        if wait_for(lambda: _audio_input(page), 8):
            return True
    return False


def get_audio_url(page) -> str:
    frame = find_frame(page, "bframe")
    if frame is None:
        return ""
    selectors = SELECTORS["captcha_audio_link"] + SELECTORS["captcha_audio_source"]
    for selector in selectors:
        try:
            element = frame.ele(selector, timeout=1)
        except Exception:
            element = None
        if not element:
            continue
        for attribute in ("href", "src"):
            try:
                value = element.attr(attribute)
            except Exception:
                value = None
            if value and len(str(value)) > 10:
                return html.unescape(str(value)).strip()
    return ""


def reload_challenge(page) -> bool:
    """点刷新按钮换一道题。识别失败（UnknownValueError）时走这里，不换 IP。"""
    frame = find_frame(page, "bframe")
    if frame is None:
        return False
    clicked = False
    for selector in SELECTORS["captcha_reload"]:
        try:
            button = frame.ele(selector, timeout=1)
        except Exception:
            button = None
        if button is not None:
            try:
                button.click()
                clicked = True
            except Exception:
                try:
                    button.click(by_js=True)
                    clicked = True
                except Exception:
                    pass
            if clicked:
                break
    if not clicked:
        try:
            frame.run_js(
                "var b=document.querySelector('#recaptcha-reload-button'); if(b){b.click();}"
            )
            clicked = True
        except Exception:
            pass
    if not clicked:
        return False
    return bool(wait_for(lambda: _audio_input(page), 12))


def fill_and_verify(page, text: str) -> bool:
    frame = find_frame(page, "bframe")
    if frame is None:
        return False
    box = None
    for selector in SELECTORS["captcha_audio_input"]:
        try:
            box = frame.ele(selector, timeout=2)
        except Exception:
            box = None
        if box:
            break
    filled = False
    if box is not None:
        try:
            box.click()
            box.clear()
            box.input(text)
            filled = True
        except Exception:
            filled = False
    if not filled:
        try:
            frame.run_js(
                "var e=document.querySelector('#audio-response');"
                "if(e){e.value=%s;e.dispatchEvent(new Event('input',{bubbles:true}));}"
                % json.dumps(text)
            )
            filled = True
        except Exception:
            return False
    if not filled:
        return False

    time.sleep(random.uniform(0.4, 1.2))
    for selector in SELECTORS["captcha_verify"]:
        try:
            button = frame.ele(selector, timeout=2)
        except Exception:
            button = None
        if button is not None:
            try:
                button.click()
            except Exception:
                try:
                    button.click(by_js=True)
                except Exception:
                    pass
            break
    return True


# ===========================================================================
# 音频下载与识别（SPEC §6.8）
# ===========================================================================
def download_audio(url: str, timeout: float = 25.0) -> str:
    """下载验证码音频，返回本地路径；失败返回空串。"""
    if requests is None or not url:
        return ""
    candidates = [url]
    if "recaptcha.net" in url:
        candidates.append(url.replace("recaptcha.net", "www.google.com"))
    elif "www.google.com" in url:
        candidates.append(url.replace("www.google.com", "recaptcha.net"))

    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.google.com/"}
    for candidate in candidates:
        try:
            response = requests.get(candidate, headers=headers, timeout=timeout)
        except Exception as exc:
            log(f"音频下载异常: {exc}", "WARN")
            continue
        if response.status_code != 200:
            log(f"音频下载 HTTP {response.status_code}", "WARN")
            continue
        body = response.content or b""
        if len(body) < 2000:
            log(f"音频体积过小 ({len(body)}B)，疑似错误页", "WARN")
            continue
        head = body[:16].lstrip().lower()
        if head.startswith(b"<html") or head.startswith(b"<!doctype") or head.startswith(b"{"):
            log("音频下载拿到的是 HTML/JSON，跳过", "WARN")
            continue
        directory = tempfile.mkdtemp(prefix="h2p-audio-")
        path = os.path.join(directory, "challenge.mp3")
        try:
            with open(path, "wb") as handle:
                handle.write(body)
        except Exception as exc:
            log(f"音频落盘失败: {exc}", "WARN")
            shutil.rmtree(directory, ignore_errors=True)
            continue
        return path
    return ""


def _normalize_audio(segment):
    """峰值归一化到 -0.1 dBFS。安静到几乎没有信号时不动，避免增益放大噪声。"""
    try:
        peak = float(segment.max_dBFS)
    except Exception:
        return segment
    if peak > -50.0 and peak <= -0.1:
        try:
            return segment.apply_gain(min(30.0, -0.1 - peak))
        except Exception:
            return segment
    return segment


def _trim_silence(segment):
    if detect_leading_silence is None:
        return segment
    try:
        head = detect_leading_silence(segment, silence_threshold=-40.0, chunk_size=10)
        tail = detect_leading_silence(segment.reverse(), silence_threshold=-40.0, chunk_size=10)
        total = len(segment)
        if head >= 0 and tail >= 0 and head + tail < total - 200:
            return segment[head: total - tail]
    except Exception:
        pass
    return segment


def _build_variants(segment):
    """生成 2 个识别变体：原样 / 高通滤波。不同噪声条件下总有一个更清楚。"""
    try:
        base = segment.set_channels(1).set_frame_rate(16000)
    except Exception:
        base = segment
    base = _normalize_audio(base)
    base = _trim_silence(base)

    variants = [("原样", base)]
    try:
        variants.append(("高通", base.high_pass_filter(300)))
    except Exception as exc:
        log(f"高通滤波变体生成失败: {exc}", "WARN")
    return variants


def _clean_stt_text(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", str(text).strip().lower())
    return " ".join(cleaned.split())


# reCAPTCHA 音频挑战的答案来自一个固定小词表（数字与少量常见词），
# 识别结果经常是 "remote engine stuff" 这类完全无关的句子 —— 那是 STT 在噪声上
# 编出来的幻觉。把不在词表里的结果直接判为"听不懂"，比拿去提交更划算：
# 提交错误答案会让 Google 判定为机器人，反而提高后续难度。
_CAPTCHA_WORDS = frozenset(
    """
    zero one two three four five six seven eight nine ten
    eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen
    twenty thirty forty fifty sixty seventy eighty ninety hundred thousand
    red green blue yellow black white orange purple brown gray grey pink
    apple banana cherry orange lemon grape melon peach
    cat dog bird fish horse mouse house car tree sun moon star
    """.split()
)


def _looks_like_captcha_answer(text: str) -> bool:
    """判断识别结果是否可能是验证码答案。

    真答案通常是 1~3 个词，且几乎都落在 _CAPTCHA_WORDS 里。长句子、含大量
    词表外单词的结果基本都是 STT 幻觉，提交上去只会浪费一次机会并加深风控。
    """
    if not text:
        return False
    words = text.split()
    if not words or len(words) > 6:
        return False
    known = sum(1 for word in words if word in _CAPTCHA_WORDS)
    # 数字串（"4821"）也是常见答案形态
    if re.fullmatch(r"\d{1,8}", text.replace(" ", "")):
        return True
    return known >= max(1, len(words) // 2)


def recognize_audio(mp3_path: str):
    """识别音频。返回 (文本, 错误类型)。

    错误类型："" 成功 / "request" 网络或墙导致 STT 不可用（该换 IP）/
    "unknown" 听不懂（该换一道题）/ "convert" 转码失败 / "unavailable" 缺依赖。
    这两种失败的处理方式完全不同，混在一起就是白白烧额度。
    """
    if sr is None or AudioSegment is None:
        return "", "unavailable"
    try:
        segment = AudioSegment.from_file(mp3_path)
    except Exception as exc:
        log(f"音频解码失败: {exc}", "WARN")
        return "", "convert"

    variants = _build_variants(segment)
    workdir = tempfile.mkdtemp(prefix="h2p-stt-")
    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 250
    recognizer.dynamic_energy_threshold = True
    try:
        for index, (label, variant) in enumerate(variants):
            wav_path = os.path.join(workdir, f"v{index}.wav")
            try:
                variant.export(wav_path, format="wav")
            except Exception as exc:
                log(f"变体 {label} 导出失败: {exc}", "WARN")
                continue
            try:
                with sr.AudioFile(wav_path) as source:
                    audio = recognizer.record(source)
                text = recognizer.recognize_google(audio, language="en-US")
            except sr.UnknownValueError:
                log(f"变体 {label}: 无法识别（换题）", "WARN")
                continue
            except sr.RequestError as exc:
                log(f"变体 {label}: Google STT 请求失败（换 IP）: {exc}", "WARN")
                return "", "request"
            except Exception as exc:
                log(f"变体 {label}: 识别异常: {exc}", "WARN")
                continue
            cleaned = _clean_stt_text(text)
            if cleaned and _looks_like_captcha_answer(cleaned):
                log(f"变体 {label} 识别成功: [{cleaned}]")
                return cleaned, ""
            if cleaned:
                # 词表外结果不提交：提交错误答案会被判定为机器人，抬高后续难度
                log(f"变体 {label} 识别结果不像答案，丢弃: [{cleaned}]", "WARN")
            else:
                log(f"变体 {label}: 识别结果为空", "WARN")
        return "", "unknown"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def solve_captcha(page, cfg: Config):
    """在单个页面内破解 reCAPTCHA，允许刷新题目重试。

    返回 (status, detail)，status ∈
    {"solved", "blocked", "stt_network", "exhausted", "unavailable", "no_captcha"}。
    """
    if sr is None or AudioSegment is None:
        return "unavailable", "复选框未直接放行，且缺少 speech_recognition / pydub 无法走音频兜底"

    if find_frame(page, "anchor") is None:
        if not wait_for(lambda: find_frame(page, "anchor"), 20):
            return "no_captcha", "页面未出现 reCAPTCHA"

    # 主路径：只点复选框，不主动切音频。
    # WARP 出口 IP 的评分足够高时 reCAPTCHA 直接放行，点一下就出 token；
    # 这种情况下切音频纯属自找麻烦（多一次挑战 = 多一次被判定为机器人的机会，
    # 也更容易触发 "try again later"）。多给几次机会是因为偶尔要等评分回填。
    for attempt in range(1, 4):
        try:
            click_checkbox(page)
        except CaptchaBlocked as exc:
            return "blocked", str(exc)
        except CaptchaUnavailable as exc:
            return "no_captcha", f"验证码结构异常: {exc}"
        except Exception as exc:
            return "no_captcha", f"点击验证码复选框失败: {exc}"

        if wait_for(lambda: is_solved(page), 8):
            return "solved", f"复选框第 {attempt} 次点击直接通过"

        if is_blocked(page):
            return "blocked", "复选框点击后检测到封锁提示"

        # 还没出 token 且没被封锁：可能评分还在回填，稍等再点一次
        log(f"复选框第 {attempt} 次未直接放行，准备重试", "WARN")
        time.sleep(random.uniform(1.2, 2.4))

    # 兜底：走音频挑战
    attempts = max(1, int(cfg.captcha_max))
    last_detail = ""
    for index in range(1, attempts + 1):
        if is_blocked(page):
            return "blocked", "检测到 Google 封锁提示"
        if is_solved(page):
            return "solved", "已通过"

        try:
            if not switch_to_audio(page):
                if is_blocked(page):
                    return "blocked", "切换音频模式时被封锁"
                last_detail = "无法进入音频模式"
                reload_challenge(page)
                continue
        except CaptchaBlocked as exc:
            return "blocked", str(exc)
        except Exception as exc:
            last_detail = f"切换音频模式异常: {exc}"
            reload_challenge(page)
            continue

        audio_url = get_audio_url(page)
        if not audio_url:
            last_detail = "未取到音频地址"
            reload_challenge(page)
            continue

        audio_path = download_audio(audio_url)
        if not audio_path:
            last_detail = "音频下载失败"
            reload_challenge(page)
            continue

        try:
            text, kind = recognize_audio(audio_path)
        finally:
            shutil.rmtree(os.path.dirname(audio_path), ignore_errors=True)

        if kind == "request":
            return "stt_network", "Google STT 网络错误，需要更换出口 IP"
        if kind == "unavailable":
            return "unavailable", "缺少音频识别依赖"
        if kind in ("convert", "unknown") or not text:
            last_detail = f"第 {index} 次识别不出内容"
            log(last_detail, "WARN")
            reload_challenge(page)
            continue

        if not fill_and_verify(page, text):
            last_detail = "填写验证码答案失败"
            reload_challenge(page)
            continue

        if wait_for(lambda: is_solved(page), 12):
            return "solved", f"第 {index} 次识别通过: {text}"

        last_detail = f"第 {index} 次答案 [{text}] 被判错"
        log(last_detail, "WARN")
        if is_blocked(page):
            return "blocked", "答案提交后被封锁"
        reload_challenge(page)

    return "exhausted", last_detail or "验证码尝试次数耗尽"


# ===========================================================================
# API 续期提交
# ===========================================================================
def _coerce_result(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
    return None


def renew_via_api(page, timeout: float = 30.0) -> dict:
    """在页面上下文里 POST /publicapis/renewServer。

    返回 {"ok": bool, "stage": "api|no_token|no_csrf|parse|network|timeout",
          "message": str, "status": int|None, "err": str}
    stage 的区分很关键：no_token 是没解出验证码（重试），api 且 message 含 captcha
    是被服务端拒了（换题），api 其它 message 是终局失败（别重试），network 是网络。
    """
    try:
        page.run_js(RENEW_JS)
    except Exception as exc:
        return {"ok": False, "stage": "network", "message": "", "err": f"注入失败: {exc}"}

    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        try:
            raw = page.run_js("return window.__h2p_result")
        except Exception:
            raw = None
        result = _coerce_result(raw)
        if result is not None:
            result.setdefault("ok", False)
            result.setdefault("stage", "api")
            result.setdefault("message", "")
            result.setdefault("err", "")
            return result
        time.sleep(0.5)

    return {"ok": False, "stage": "timeout", "message": "", "err": "等待续期响应超时"}


def verify_by_api(url, old_expires_dt) -> tuple:
    """续期后二次确认：重新读时间戳，返回 (confirmed, new_dt, note)。

    判据必须比"new > old"更严：过期服务器的 old 是过去时间，只要 new 比它大一点
    就成立，容易把"没续上但站点顺手刷新了时间戳"误判成成功。真正可靠的证据是
    **新的到期时间落在未来**——那才代表服务器还能用。
    """
    data = fetch_server_data(url, timeout=20, retries=2)
    if not isinstance(data, dict) or not data.get("ok"):
        return False, None, "续期后无法读取 API 二次确认"
    new_dt = parse_ts(data.get("expires_ts"))
    if new_dt is None:
        return False, None, "续期后 API 未返回到期时间"

    now_dt = datetime.now(timezone.utc)
    if new_dt <= now_dt:
        # 还在过去：无论比 old 大多少，服务器实际上已经过期，不算续上
        return False, new_dt, f"续期后到期时间仍在过去（{_fmt_dt(new_dt)}）"

    if old_expires_dt is None:
        return True, new_dt, "到期时间已回到未来"
    if new_dt > old_expires_dt:
        return True, new_dt, "到期时间已增长"
    return False, new_dt, "服务端返回成功但到期时间未增长"


# ===========================================================================
# WARP IP 轮换（两级 + 变更校验）
# ===========================================================================
_WARP_LOCK = threading.Lock()
_WARP_MISSING = False


def _warp(args, timeout: float = 40.0):
    """执行 warp-cli，返回 (ok, output)。找不到命令时置位全局标记避免反复重试。"""
    global _WARP_MISSING
    if _WARP_MISSING:
        return False, "warp-cli 不可用"
    executable = shutil.which("warp-cli")
    if executable is None:
        _WARP_MISSING = True
        log("未找到 warp-cli，无法轮换出口 IP", "WARN")
        return False, "warp-cli 未安装"

    base = [executable, "--accept-tos"] + [str(a) for a in args]
    commands = []
    if shutil.which("sudo"):
        # -n：绝不等待交互式密码输入，否则 Actions 里会直接挂到超时
        commands.append(["sudo", "-n"] + base)
    commands.append(base)

    last_output = ""
    for command in commands:
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError:
            continue
        except Exception as exc:
            last_output = str(exc)
            continue
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        last_output = output
        if completed.returncode == 0:
            return True, output
    return False, last_output


def get_public_ip(timeout: float = 8.0) -> str:
    if requests is None:
        return ""
    endpoints = (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    )
    for endpoint in endpoints:
        try:
            response = requests.get(
                endpoint,
                timeout=timeout,
                headers={"User-Agent": "curl/8.5.0", "Accept": "text/plain"},
            )
            if response.status_code == 200:
                ip = (response.text or "").strip()
                if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", ip):
                    return ip
        except Exception:
            continue
    return ""


def _wait_ip_changed(before: str, timeout: float = 40.0) -> bool:
    if not before:
        ip = wait_for(get_public_ip, timeout, interval=2.0)
        if ip:
            log(f"出口 IP: {ip}（无基线，按已轮换处理）")
            return True
        log("等待出口 IP 超时（且没有可用基线）", "WARN")
        return False

    def _changed():
        current = get_public_ip()
        if current and current != before:
            log(f"出口 IP 已变更: {before} -> {current}")
            return current
        return ""

    ip = wait_for(_changed, timeout, interval=2.0)
    if not ip:
        log(f"等待出口 IP 变更超时（仍是 {before}）", "WARN")
    return bool(ip)


def rotate_ip(wait_seconds: float = 40.0) -> bool:
    """两级轮换：先快速 disconnect/connect，连续两次 IP 没变才重建注册（慢）。"""
    with _WARP_LOCK:
        before = get_public_ip()
        log(f"当前出口 IP: {before or '未知'}")

        for attempt in (1, 2):
            ok_disconnect, out_disconnect = _warp(("disconnect",), timeout=25)
            time.sleep(random.uniform(0.5, 1.2))
            ok_connect, out_connect = _warp(("connect",), timeout=30)
            if not ok_disconnect and not ok_connect:
                log(f"WARP 断开/重连命令均失败: {out_disconnect or out_connect}", "WARN")
                if _WARP_MISSING:
                    return False
            if _wait_ip_changed(before, wait_seconds):
                return True
            log(f"第 {attempt} 次快速重连后 IP 未变化", "WARN")

        log("快速重连无效，改为重建 WARP 注册（较慢，约 30s）", "WARN")
        _warp(("disconnect",), timeout=25)
        ok_delete, out_delete = _warp(("registration", "delete"), timeout=40)
        if not ok_delete:
            log(f"删除 WARP 注册失败（可能本来未注册）: {out_delete}", "WARN")
        ok_new, out_new = _warp(("registration", "new"), timeout=60)
        if not ok_new:
            log(f"新建 WARP 注册失败: {out_new}", "ERROR")
            return False
        ok_connect, out_connect = _warp(("connect",), timeout=40)
        if not ok_connect:
            log(f"WARP 连接失败: {out_connect}", "ERROR")
        return _wait_ip_changed(before, wait_seconds)


# ===========================================================================
# 单页面续期流程：API 主路径 / DOM 降级路径
# ===========================================================================
def _blank_attempt(status: str, detail: str) -> dict:
    return {"status": status, "detail": detail, "name": "", "old_raw": "", "new_raw": "", "expires_dt": None}


def _page_ready_js() -> str:
    """页面是否已经可以安全点 Renew。

    三个条件缺一不可，全部由站点自身的异步初始化决定：
      1. #renew 卡片从 display:none 变成可见 —— 站点要等 /publicapis/getServerData
         返回后才显示，实测冷启动约 3.5s；
      2. window.grecaptcha 已就绪 —— 站点在 renew() 里直接调 grecaptcha.render()，
         这个脚本比主页面慢，实测晚约 0.4s。早点击会让 render 抛异常，Swal 弹窗
         照样打开但里面永远是空的 div，reCAPTCHA frame 永不出现；
      3. window.__cfRLUnblockHandlers 为真 —— 按钮的 onclick 是
         `if (!window.__cfRLUnblockHandlers) return false; renew()`，
         这是 Cloudflare Rocket Loader 的守卫，为假时点击被静默吞掉。
    """
    return (
        "return (function(){"
        "var c=document.getElementById('renew');"
        "var b=document.querySelector('button[onclick*=renew]');"
        "var visible = !!(b && (b.offsetWidth||b.offsetHeight||b.getClientRects().length));"
        "var card = !!(c && getComputedStyle(c).display!=='none');"
        "var gc = (typeof window.grecaptcha !== 'undefined') && !!window.grecaptcha;"
        "var cf = window.__cfRLUnblockHandlers === true;"
        "return JSON.stringify({visible:visible,card:card,gc:gc,cf:cf});"
        "})()"
    )


def wait_page_ready(page, timeout: float = 25.0) -> bool:
    """等页面自身的异步初始化完成（卡片可见 + grecaptcha 就绪 + CF 守卫放行）。"""

    def _ready():
        try:
            raw = page.run_js(_page_ready_js())
        except Exception:
            return False
        if not raw:
            return False
        try:
            state = json.loads(raw)
        except Exception:
            return False
        return state.get("visible") and state.get("card") and state.get("gc") and state.get("cf")

    if wait_for(_ready, timeout, interval=0.4):
        return True
    # 超时不直接判死：把最后状态打出来，方便区分"站点改版"和"就是慢"
    try:
        log(f"页面就绪等待超时，最后状态: {page.run_js(_page_ready_js())}", "WARN")
    except Exception:
        pass
    return False


def open_renew_dialog(page) -> bool:
    """点页面自己的 Renew server 按钮，让站点自己渲染 Swal + reCAPTCHA。

    顺序很关键：必须先等 grecaptcha 就绪再点。早点击会让 renew() 里的
    grecaptcha.render() 抛 ReferenceError，Swal 弹窗打开但内容为空，
    之后无论怎么等都不会出现 reCAPTCHA frame —— 这正是"验证码一直加载不出来"
    的根因。
    """
    wait_page_ready(page, 25.0)

    if click_first(page, "renew_open", timeout=6):
        if wait_for(lambda: find_frame(page, "anchor"), 12):
            return True

    # 弹窗开了但里面没有验证码：说明 render 失败过，重开一次（此时 grecaptcha 一定就绪）
    if _swal_open(page):
        _close_swal(page)
        wait_page_ready(page, 8.0)

    if click_first(page, "renew_open", timeout=5):
        if wait_for(lambda: find_frame(page, "anchor"), 14):
            return True

    # 最后兜底：直接调页面自己的 renew()，绕开按钮与 CF 守卫
    try:
        page.run_js("try{ if(typeof renew==='function'){ renew(); return 'ok'; } return 'missing'; }"
                    "catch(e){ return 'err:'+e; }")
        if wait_for(lambda: find_frame(page, "anchor"), 12):
            return True
    except Exception:
        pass

    return False


def _swal_open(page) -> bool:
    try:
        return bool(page.run_js("return !!document.querySelector('.swal2-container');"))
    except Exception:
        return False


def _close_swal(page) -> None:
    """关掉空弹窗，避免遮罩挡住后续点击。"""
    try:
        page.run_js(
            "(function(){var c=document.querySelector('.swal2-cancel');"
            "if(c){c.click();return;}var x=document.querySelector('.swal2-container');"
            "if(x){x.remove();}})();"
        )
    except Exception:
        pass
    time.sleep(0.6)


def run_renew_attempt_api(page, url: str, cfg: Config, precheck) -> dict:
    """API 主路径：读 CSRF → 开弹窗 → 解验证码 → 同源 fetch 提交 → 二次确认。"""
    try:
        page.get(url, retry=3)
    except Exception as exc:
        return _blank_attempt("error", f"打开页面失败: {exc}")

    old_dt = parse_ts(precheck.get("expires_ts")) if isinstance(precheck, dict) else None
    name = (precheck or {}).get("name") or read_server_name(page)
    old_raw = _fmt_dt(old_dt) or extract_expiry_text(wait_text(page, SELECTORS["expiry"], 15))
    result = {
        "status": "error",
        "detail": "",
        "name": name or "",
        "old_raw": old_raw,
        "new_raw": "",
        "expires_dt": old_dt,
    }
    log(f"服务器: {name or '未知'}，当前到期: {old_raw or '未知'}")

    csrf = wait_for(lambda: read_csrf_token(page), 10) or ""
    if not csrf:
        result["status"] = "structural"
        result["detail"] = "页面缺少 csrf-token meta（站点可能改版）"
        return result

    dismiss_overlays(page)
    humanize(page)

    if cfg.dry_run:
        result["status"] = "dry_run"
        result["detail"] = "DRY_RUN 只读取状态"
        return result

    if not open_renew_dialog(page):
        # 没弹窗就没验证码：要么站点改了，要么本来就已续期成功，用 API 判定
        confirmed, new_dt, note = verify_by_api(url, old_dt)
        result["new_raw"] = _fmt_dt(new_dt)
        if confirmed:
            result["status"] = "success"
            result["detail"] = f"未出现验证码，API 确认已续期（{note}）"
            result["expires_dt"] = new_dt
        else:
            result["status"] = "structural"
            result["detail"] = "找不到 Renew server 按钮且未确认续期（站点可能改版）"
        return result

    status, detail = solve_captcha(page, cfg)
    result["detail"] = detail
    if status == "blocked":
        result["status"] = "blocked"
        return result
    if status == "stt_network":
        result["status"] = "stt_network"
        return result
    if status == "unavailable":
        result["status"] = "unavailable"
        return result
    if status != "solved":
        result["status"] = "captcha_failed"
        return result

    api = renew_via_api(page)
    stage = api.get("stage")
    message = str(api.get("message") or "")
    result["detail"] = message or str(api.get("err") or stage or "")

    if api.get("ok"):
        confirmed, new_dt, note = verify_by_api(url, old_dt)
        result["expires_dt"] = new_dt
        result["new_raw"] = _fmt_dt(new_dt)
        if confirmed:
            result["status"] = "success"
            result["detail"] = f"服务端 success=1；{note}"
        else:
            # 服务端说成功了但读不回新时间：按成功处理，但把疑点写进备注
            result["status"] = "success"
            result["detail"] = f"服务端 success=1；二次确认存疑：{note}"
        return result

    if stage in ("no_token", "no_csrf", "timeout"):
        # 验证码 token 没拿到 → 回到破解流程重试，不是 API 失败
        result["status"] = "captcha_failed"
        result["detail"] = api.get("err") or f"未拿到 captcha token ({stage})"
        return result

    if stage == "api":
        if _is_captcha_message(message):
            result["status"] = "captcha_failed"
            result["detail"] = f"服务端拒绝验证码: {message}"
        else:
            # 明确的业务失败，重试也是同样结果，直接终局
            result["status"] = "api_rejected"
            result["detail"] = message or "服务端拒绝续期"
        return result

    # network / parse：临时性故障，可以重试
    result["status"] = "error"
    result["detail"] = api.get("err") or "续期请求失败"
    return result


def run_renew_attempt_dom(page, url: str, cfg: Config, precheck) -> dict:
    """DOM 降级路径：点 Swal 确认按钮 + 读页面时间，API 不可用时使用。"""
    try:
        page.get(url, retry=3)
    except Exception as exc:
        return _blank_attempt("error", f"打开页面失败: {exc}")

    # 等页面把到期区域渲染出来：绝对时间或 "Server expired" 都算渲染完成
    wait_for(
        lambda: read_expiry_raw(page)
        or is_countdown_text(extract_expiry_text(ele_text(page, SELECTORS["expiry"], 0.5))),
        25,
    )
    old_raw = read_expiry_raw(page)
    result = {
        "status": "error",
        "detail": "",
        "name": read_server_name(page),
        "old_raw": old_raw,
        "new_raw": "",
        "expires_dt": parse_expiry(old_raw),
    }
    if not result["old_raw"]:
        result["status"] = "structural"
        result["detail"] = "页面未出现可用的到期信息（选择器可能已失效）"
        return result

    log(f"服务器: {result['name'] or '未知'}，当前到期: {result['old_raw']}")

    dismiss_overlays(page)
    humanize(page)

    if cfg.dry_run:
        result["status"] = "dry_run"
        result["detail"] = "DRY_RUN 只读取状态"
        return result

    if not click_first(page, "renew_open", timeout=6):
        result["status"] = "structural"
        result["detail"] = "找不到 Renew server 按钮（站点可能改版）"
        return result
    wait_for(lambda: _any_ele(page, SELECTORS["renew_dialog_open"], 0.5), 8)
    if not click_first(page, "renew_dialog_open", timeout=4):
        click_first(page, "renew_open", timeout=4)

    if not wait_for(lambda: find_frame(page, "anchor"), 25):
        ok, new_raw = _detect_success_dom(page, result["expires_dt"], result["old_raw"])
        result["new_raw"] = new_raw
        result["expires_dt"] = parse_expiry(new_raw) or result["expires_dt"]
        if ok:
            result["status"] = "success"
            result["detail"] = "未出现验证码，页面已显示续期成功"
        else:
            result["status"] = "captcha_failed"
            result["detail"] = "未出现 reCAPTCHA，也未检测到成功标志"
        return result

    status, detail = solve_captcha(page, cfg)
    result["detail"] = detail
    if status != "solved":
        result["status"] = {
            "blocked": "blocked",
            "stt_network": "stt_network",
            "unavailable": "unavailable",
        }.get(status, "captcha_failed")
        return result

    if not click_first(page, "renew_confirm", timeout=8):
        result["status"] = "structural"
        result["detail"] = "验证码通过后找不到最终 Renew 按钮"
        return result

    ok, new_raw = _detect_success_dom(page, result["expires_dt"], result["old_raw"])
    result["new_raw"] = new_raw
    new_dt = parse_expiry(new_raw)
    if new_dt is not None:
        result["expires_dt"] = new_dt
    if ok:
        result["status"] = "success"
    else:
        result["status"] = "captcha_failed"
        result["detail"] = "点击 Renew 后到期时间未增长，也没有成功文案"
    return result


def _detect_success_dom(page, old_dt, old_raw=""):
    """DOM 降级路径的成功判定。

    只认两种证据：绝对时间增长 > 1h，或页面出现成功文案。
    "Server expired" 和倒计时 "06:35:12" 都不算成功 —— 旧实现用
    `new_expire != old_expire` 比较，遇到 "Server expired" 会因为字符串不相等而
    判定成功，于是发出假通知、下轮再烧一次额度。

    过期服务器的特殊处理：old_raw 是 "Server expired"，parse 不出 old_dt，
    此时"时间增长"无从比较。只要续期后页面出现了可解析的绝对时间，就说明
    expires 已经回到未来，这是过期续期成功的唯一可用证据。
    """
    try:
        wait_for(lambda: read_expiry_raw(page), 12)
    except Exception:
        pass
    new_raw = read_expiry_raw(page)

    new_dt = parse_expiry(new_raw) if new_raw else None

    # 过期起点：old_dt 为 None 但 old_raw 明确是过期标记。
    # 续期成功后页面会显示新的绝对时间，"Server expired" 消失即为成功。
    if old_dt is None and old_raw and is_expired_text(old_raw):
        if new_dt is not None:
            return True, new_raw
        return False, new_raw

    if is_expired_text(new_raw):
        return False, new_raw

    if new_dt is not None and old_dt is not None:
        if new_dt - old_dt > timedelta(hours=1):
            return True, new_raw

    page_text = ""
    try:
        page_text = (page.html or "").lower()
    except Exception:
        page_text = ""
    for keyword in SUCCESS_KEYWORDS:
        if keyword in page_text:
            return True, new_raw
    return False, new_raw


# ===========================================================================
# 单 URL 处理
# ===========================================================================
@dataclass
class UrlResult:
    url: str
    key: str
    name: str = ""
    old_raw: str = ""
    new_raw: str = ""
    ok: bool = False
    skipped: bool = False
    dry_run: bool = False
    failed: bool = False
    structural: bool = False
    dead: bool = False
    reason: str = ""
    screenshot: str = ""
    elapsed: float = 0.0
    expires_dt: object = None


def _apply_result(state: dict, lock, result: UrlResult, cfg: Config) -> None:
    """把单个 URL 的结果写回 state 并立刻落盘（进程被杀也不丢进度）。"""
    with lock:
        servers = state.setdefault("servers", {})
        entry = servers.get(result.key)
        if not isinstance(entry, dict):
            entry = {}

        # 不存完整 URL：它含 i=<uuid> 这个免鉴权凭据，而 state.json 会提交回
        # public 仓库。只保留脱敏后的主机名，够人工辨认，不足以被拿去续期。
        try:
            parsed = urlparse(result.url or "")
            entry["host"] = parsed.netloc or ""
        except Exception:
            pass
        if result.name:
            entry["name"] = result.name
        if result.new_raw:
            entry["expiry_raw"] = result.new_raw
        elif result.old_raw:
            entry["expiry_raw"] = result.old_raw

        if isinstance(result.expires_dt, datetime):
            entry["expiry_iso"] = _to_utc(result.expires_dt).isoformat(timespec="seconds")
        elif not entry.get("expiry_iso"):
            parsed_old = parse_expiry(result.old_raw) if result.old_raw else None
            if parsed_old is not None:
                entry["expiry_iso"] = parsed_old.isoformat(timespec="seconds")

        stamp = _now_iso()
        if result.skipped:
            entry["last_status"] = "skipped"
            entry["last_reason"] = result.reason
        elif result.dead:
            # 终态：数据已删除。不累加 consecutive_failures —— 这不是"续期失败"，
            # 而是服务器不存在，累加会让监控误以为重试有希望。
            entry["last_status"] = "dead"
            entry["last_reason"] = result.reason
        elif result.dry_run:
            entry["last_status"] = "dry_run"
            entry["last_reason"] = result.reason
        elif result.ok:
            entry["last_success_at"] = stamp
            entry["last_attempt_at"] = stamp
            entry["consecutive_failures"] = 0
            entry["last_status"] = "success"
            entry["last_reason"] = result.reason
        else:
            entry["last_attempt_at"] = stamp
            try:
                failures = int(entry.get("consecutive_failures") or 0)
            except Exception:
                failures = 0
            entry["consecutive_failures"] = failures + 1
            entry["last_status"] = "failed"
            entry["last_reason"] = result.reason

        servers[result.key] = entry
        save_state(state, cfg.state_path)


def _note_api_failure() -> None:
    """API 连续失败到阈值就自动降级到 DOM 流程，省掉后续无谓的重试等待。"""
    global USE_API
    with _API_STATE_LOCK:
        _API_STATE["failures"] += 1
        if USE_API and _API_STATE["failures"] >= API_FAILURE_BUDGET:
            USE_API = False
            log(
                f"getServerData 连续 {_API_STATE['failures']} 次不可用，"
                "本次运行降级为纯 DOM 流程",
                "WARN",
            )


def _note_api_success() -> None:
    with _API_STATE_LOCK:
        _API_STATE["failures"] = 0


_API_STATE_LOCK = threading.Lock()
_API_STATE = {"failures": 0}


def process_url(url: str, cfg: Config, state: dict, lock) -> UrlResult:
    started = time.monotonic()
    key = server_key(url)
    result = UrlResult(url=url, key=key)

    entry = state.get("servers", {}).get(key)
    if isinstance(entry, dict):
        result.name = str(entry.get("name") or "")
        result.old_raw = str(entry.get("expiry_raw") or "")
        stored = parse_ts(entry.get("expiry_iso"))
        if stored is not None:
            result.expires_dt = stored
            if not result.old_raw:
                result.old_raw = _fmt_dt(stored)

    # ---- 1. HTTP 预检：不启浏览器就知道要不要续 ----
    precheck = None
    if USE_API:
        precheck = fetch_server_data(url)
        if precheck is None:
            _note_api_failure()
            log(f"[WARN] {key} 预检失败（{API_LAST_ERROR or '未知'}），改走浏览器流程")
        else:
            _note_api_success()
            if not precheck.get("ok", True):
                # 服务器不存在之类的业务失败，重试没有意义，连浏览器都不启动
                result.failed = True
                result.reason = f"API 拒绝: {precheck.get('error') or '未知'}"
                log(f"[STEP] {key} 预检被拒：{result.reason}", "WARN")
                result.elapsed = time.monotonic() - started
                _apply_result(state, lock, result, cfg)
                return result
            if precheck.get("name"):
                result.name = precheck["name"]
            expires_dt = parse_ts(precheck.get("expires_ts"))
            if expires_dt is not None:
                result.expires_dt = expires_dt
                result.old_raw = _fmt_dt(expires_dt)
            deletes_dt = parse_ts(precheck.get("deletes_ts"))
            state_text, state_detail = classify_server(precheck, datetime.now(timezone.utc))
            log(
                f"[STEP] {key} 预检: {result.name or '未知'}，状态 {state_text}"
                f"（{state_detail}），到期 {result.old_raw or '未知'}"
                + (f"，删除 {_fmt_dt(deletes_dt)}" if deletes_dt else "")
            )

    now = datetime.now(timezone.utc)
    action, decision_reason = decide_action(
        precheck, entry, now, cfg.cooldown_hours, cfg.skip_remaining_hours,
        cfg.stale_after_hours,
    )

    # 数据已被删除：终态，不续期、不重试、不烧验证码
    if action == "dead":
        result.failed = True
        result.dead = True
        result.structural = False
        result.reason = decision_reason
        log(f"[STEP] {key} 终态：{decision_reason}", "WARN")
        result.elapsed = time.monotonic() - started
        _apply_result(state, lock, result, cfg)
        return result

    if action == "skip" and not cfg.dry_run:
        result.skipped = True
        result.reason = decision_reason
        log(f"[STEP] 跳过 {key}：{decision_reason}")
        result.elapsed = time.monotonic() - started
        _apply_result(state, lock, result, cfg)
        return result

    if cfg.dry_run:
        if precheck is not None and precheck.get("ok"):
            result.ok = True
            result.dry_run = True
            result.reason = f"DRY_RUN 预检: {decision_reason}"
            log(f"[STEP] DRY_RUN {key}: {result.reason}")
            result.elapsed = time.monotonic() - started
            _apply_result(state, lock, result, cfg)
            return result
        log("[WARN] DRY_RUN 拿不到 API 数据，退回浏览器只读页面")

    # ---- 2. 需要续期：起浏览器 ----
    log(f"[STEP] 开始续期 {key}: {decision_reason}")
    use_api_path = USE_API
    page_attempt = 0
    rotations = 0
    rotation_failures = 0
    last_detail = decision_reason

    while page_attempt < cfg.page_attempts:
        if time.monotonic() >= cfg.deadline:
            last_detail = "超出 JOB_BUDGET_MINUTES 时间预算，放弃本条"
            log(last_detail, "WARN")
            break

        page_attempt += 1
        log(f"[STEP] {key} 第 {page_attempt}/{cfg.page_attempts} 次打开页面")
        page = None
        user_data_dir = None
        attempt = _blank_attempt("error", "未执行")
        try:
            page, user_data_dir = open_browser()
            if use_api_path:
                attempt = run_renew_attempt_api(page, url, cfg, precheck)
            else:
                attempt = run_renew_attempt_dom(page, url, cfg, precheck)
        except Exception as exc:
            attempt = _blank_attempt("error", f"浏览器异常: {exc}")
            log(f"浏览器异常: {exc}", "ERROR")
        finally:
            if page is not None:
                shot = capture_screenshot(
                    page, f"{key}-a{page_attempt}-{attempt.get('status', 'unknown')}"
                )
                if shot:
                    result.screenshot = shot
            close_browser(page, user_data_dir)

        status = attempt.get("status", "error")
        if attempt.get("name"):
            result.name = attempt["name"]
        if attempt.get("old_raw"):
            result.old_raw = attempt["old_raw"]
        if attempt.get("new_raw"):
            result.new_raw = attempt["new_raw"]
        if isinstance(attempt.get("expires_dt"), datetime):
            result.expires_dt = attempt["expires_dt"]

        if status == "dry_run":
            result.ok = True
            result.dry_run = True
            result.reason = attempt.get("detail") or "DRY_RUN"
            break

        if status == "success":
            result.ok = True
            result.reason = attempt.get("detail") or "续期成功"
            log(
                f"[STEP] {key} 续期成功: {result.old_raw or '未知'} -> "
                f"{result.new_raw or '未知'}"
            )
            break

        last_detail = attempt.get("detail") or status

        if status == "api_rejected":
            # 服务端给了明确的业务失败原因，重试也是同样结果
            log(f"[STEP] {key} 服务端明确拒绝，停止重试：{last_detail}", "WARN")
            break

        if status in ("blocked", "stt_network"):
            if rotations >= cfg.ip_rotations:
                last_detail = f"{last_detail}；IP 轮换次数已用尽"
                log(last_detail, "WARN")
                break
            if rotation_failures >= 2:
                last_detail = f"{last_detail}；IP 轮换连续失败，停止继续轮换"
                log(last_detail, "WARN")
                break
            rotations += 1
            log(f"[STEP] 轮换出口 IP（第 {rotations}/{cfg.ip_rotations} 次）")
            if rotate_ip():
                rotation_failures = 0
            else:
                rotation_failures += 1
                log("IP 轮换未确认生效，仍继续尝试", "WARN")
            # 换 IP 不算一次页面重开，否则页面额度会被轮换吃光
            page_attempt = max(0, page_attempt - 1)
            continue

        if status == "structural":
            result.structural = True
            log(f"[WARN] {key} 疑似站点结构变化: {last_detail}")

        if status == "unavailable":
            # 依赖缺失属于环境问题，重开页面没有任何意义
            break

    result.failed = not (result.ok or result.skipped)
    if result.failed:
        result.reason = last_detail or "未知原因"
        log(f"[STEP] {key} 续期失败: {result.reason}", "WARN")

    result.elapsed = time.monotonic() - started
    _apply_result(state, lock, result, cfg)
    return result


# ===========================================================================
# Telegram
# ===========================================================================
def send_telegram(cfg: Config, caption: str, photo_path: str = "") -> bool:
    """先发图片；图片失败退回纯文本。未配置 token/chat 时静默跳过。"""
    if requests is None:
        return False
    if not cfg.tg_token or not cfg.tg_chat_id:
        return False

    base = f"https://api.telegram.org/bot{cfg.tg_token}"
    if photo_path and os.path.isfile(photo_path):
        try:
            with open(photo_path, "rb") as handle:
                response = requests.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": cfg.tg_chat_id, "caption": caption[:1024]},
                    files={"photo": handle},
                    timeout=30,
                )
            if response.ok and (response.json() or {}).get("ok"):
                log("Telegram 图片通知已发送")
                return True
            log(
                f"Telegram 图片通知失败: HTTP {response.status_code} {response.text[:120]}",
                "WARN",
            )
        except Exception as exc:
            log(f"Telegram 图片通知异常: {exc}", "WARN")

    try:
        response = requests.post(
            f"{base}/sendMessage",
            data={
                "chat_id": cfg.tg_chat_id,
                "text": caption[:4000],
                "disable_web_page_preview": "true",
            },
            timeout=30,
        )
        if response.ok and (response.json() or {}).get("ok"):
            log("Telegram 文本通知已发送")
            return True
        log(
            f"Telegram 文本通知失败: HTTP {response.status_code} {response.text[:120]}",
            "WARN",
        )
    except Exception as exc:
        log(f"Telegram 文本通知异常: {exc}", "WARN")
    return False


# ===========================================================================
# 汇总输出
# ===========================================================================
def write_step_summary(results, ok: int, failed: int, skipped: int) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Host2Play 续期结果",
        "",
        f"成功 **{ok}** / 失败 **{failed}** / 跳过 **{skipped}**",
        "",
        "| 服务器 | 状态 | 到期 | 备注 |",
        "|---|---|---|---|",
    ]
    for result in results:
        if result.skipped:
            status = "⏭ 跳过"
        elif result.dead:
            status = "🪦 已删除"
        elif result.ok:
            status = "✅ 成功"
        else:
            status = "❌ 失败"
        expiry = (result.new_raw or result.old_raw or "-").replace("|", "\\|")
        note = (result.reason or "-").replace("|", "\\|")
        name = (result.name or result.key or "-").replace("|", "\\|")
        lines.append(f"| {name} | {status} | {expiry} | {note} |")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except Exception as exc:
        log(f"写入 GITHUB_STEP_SUMMARY 失败: {exc}", "WARN")


# ===========================================================================
# 主流程
# ===========================================================================
def run_renew(cfg: Config) -> int:
    if not cfg.urls:
        log(
            "未配置任何续期链接。请到仓库 Settings → Secrets and variables → Actions "
            "添加名为 RENEW_URLS 的 Secret，值为续期链接（多个用英文逗号分隔）。",
            "ERROR",
        )
        return 1
    if requests is None:
        log("缺少 requests 库，无法继续。请执行: pip install requests", "ERROR")
        return 1
    if ChromiumPage is None and not cfg.dry_run:
        log("缺少 DrissionPage 库，无法继续。请执行: pip install DrissionPage", "ERROR")
        return 1

    cfg.deadline = time.monotonic() + cfg.budget_minutes * 60.0
    started_at = _now_iso()
    state = load_state(cfg.state_path)
    lock = threading.Lock()

    log(
        f"[STEP] 本次共 {len(cfg.urls)} 条链接，冷却 {cfg.cooldown_hours}h，"
        f"剩余 >{cfg.skip_remaining_hours:g}h 跳过，预算 {cfg.budget_minutes}min"
    )
    if cfg.dry_run:
        log("[STEP] DRY_RUN=1：只读状态，不点续期、不碰验证码")
    if not (cfg.tg_token and cfg.tg_chat_id):
        log("未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过 Telegram 通知")

    results = []
    try:
        if cfg.parallel > 1 and len(cfg.urls) > 1:
            log(f"[STEP] 并发处理 {min(cfg.parallel, len(cfg.urls))} 条链接")
            with ThreadPoolExecutor(max_workers=min(cfg.parallel, len(cfg.urls))) as pool:
                results = list(
                    pool.map(lambda item: process_url(item, cfg, state, lock), cfg.urls)
                )
        else:
            results = [process_url(item, cfg, state, lock) for item in cfg.urls]
    finally:
        stop_display()

    ok = sum(1 for item in results if item.ok and not item.skipped)
    skipped = sum(1 for item in results if item.skipped)
    failed = sum(1 for item in results if item.failed)
    # dead 是终态：数据已被删除，重跑不可能变好，所以不参与 retryable 判定
    dead = sum(1 for item in results if item.dead)
    retryable = bool(failed - dead > 0 and (ok + skipped) == 0)

    retry_hint = ""
    if retryable:
        structural = any(item.structural for item in results)
        retry_hint = (
            "疑似站点结构变化，重跑无效，请先跑 --check"
            if structural
            else "全部失败，可重跑一次"
        )
    elif dead and failed == dead:
        retry_hint = "全部服务器数据已删除（超过 7 天保留期），无需重跑"

    state["last_run"] = {
        "started_at": started_at,
        "finished_at": _now_iso(),
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "retryable": retryable,
        "retry_hint": retry_hint,
    }
    save_state(state, cfg.state_path)

    log(
        f"[STEP] 汇总：成功 {ok} / 跳过 {skipped} / 失败 {failed}"
        f"（其中已删除 {dead}）（retryable={retryable}）"
    )
    for result in results:
        if result.skipped:
            state_text = "跳过"
        elif result.dead:
            state_text = "已删除"
        elif result.ok:
            state_text = "成功"
        else:
            state_text = "失败"
        log(f"[STEP] {result.key} -> {state_text}，耗时 {result.elapsed:.0f}s，{result.reason}")

    write_step_summary(results, ok, failed, skipped)

    for result in results:
        caption = build_caption(
            ok=result.ok,
            server_name=result.name,
            url=result.url,
            old_expiry=result.old_raw,
            new_expiry=result.new_raw,
            reason=result.reason,
            skipped=result.skipped,
        )
        send_telegram(cfg, caption, result.screenshot)

    # 退出码：0 全成功/跳过/已删除，1 部分失败，2 全失败
    if failed == 0:
        return 0
    if ok + skipped == 0:
        return 2
    return 1


# ===========================================================================
# 选择器 / API 自检（--check）
# ===========================================================================
def _check_api(url) -> list:
    """API 自检：可达性 + 字段完整性 + 时间戳可解析性。"""
    rows = []
    identifier = extract_server_id(url)
    rows.append(["api:i 参数", "API", "✅" if identifier else "❌", "?i=<id>", identifier or "-"])

    data = fetch_server_data(url, timeout=20, retries=1)
    if data is None:
        rows.append(["api:getServerData", "API", "❌", "HTTP GET", API_LAST_ERROR or "不可达"])
        return rows

    if not data.get("ok", True):
        rows.append(
            ["api:getServerData", "API", "❌", "HTTP GET", f"success!=1: {data.get('error')}"]
        )
        return rows

    rows.append(["api:getServerData", "API", "✅", "HTTP GET", "success=1"])
    rows.append(
        [
            "api:字段完整",
            "API",
            "✅" if data.get("expires_ts") else "❌",
            "name/expires/deletes",
            f"name={data.get('name') or '-'}",
        ]
    )
    expires_dt = parse_ts(data.get("expires_ts"))
    deletes_dt = parse_ts(data.get("deletes_ts"))
    rows.append(
        [
            "api:expires 解析",
            "API",
            "✅" if expires_dt else "❌",
            "parse_ts(expires)",
            _fmt_dt(expires_dt) or "-",
        ]
    )
    rows.append(
        [
            "api:deletes 解析",
            "API",
            "✅" if deletes_dt else "⚠️",
            "parse_ts(deletes)",
            _fmt_dt(deletes_dt) or "-",
        ]
    )
    if expires_dt is not None:
        remaining = (expires_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
        rows.append(
            [
                "api:剩余时间",
                "API",
                "✅" if remaining > 0 else "⚠️",
                "expires - now",
                f"{remaining:.1f}h",
            ]
        )

    state, detail = classify_server(data, datetime.now(timezone.utc))
    rows.append(
        [
            "api:classify_server",
            "API",
            "✅" if state in ("online", "expired") else "⚠️",
            "classify_server()",
            f"{state}: {detail}",
        ]
    )
    action, action_reason = decide_action(data, None, datetime.now(timezone.utc), 6.0, 2.0)
    rows.append(
        ["api:decide_action", "API", "✅", "decide_action()", f"{action}: {action_reason}"]
    )
    return rows


def _check_page_selectors(page, keys) -> list:
    rows = []
    for key in keys:
        selectors = SELECTORS.get(key) or []
        hit = ""
        ok = False
        sample = ""
        for selector in selectors:
            try:
                element = page.ele(selector, timeout=0.8)
            except Exception:
                element = None
            if element:
                ok = True
                hit = selector
                try:
                    sample = (element.text or "").strip()[:40]
                except Exception:
                    sample = ""
                break
        rows.append(
            [key, SELECTOR_STAGE.get(key, "-"), "✅" if ok else "❌", hit or "-", sample or "-"]
        )
    return rows


def _check_captcha_selectors(page) -> list:
    rows = []
    frame = find_frame(page, "bframe")
    anchor = find_frame(page, "anchor")
    rows.append(
        [
            "recaptcha:anchor_frame",
            "验证码",
            "✅" if anchor else "❌",
            "frame.url 含 recaptcha/api2/anchor" if anchor else "-",
            (anchor.url if anchor else "-") or "-",
        ]
    )
    rows.append(
        [
            "recaptcha:bframe",
            "验证码",
            "✅" if frame else "❌",
            "frame.url 含 recaptcha/api2/bframe" if frame else "-",
            (frame.url if frame else "-") or "-",
        ]
    )
    for key in CAPTCHA_SELECTOR_KEYS:
        target = anchor if key == "captcha_anchor" else frame
        hit = ""
        ok = False
        sample = ""
        if target is not None:
            for selector in SELECTORS.get(key) or []:
                try:
                    element = target.ele(selector, timeout=0.8)
                except Exception:
                    element = None
                if element:
                    ok = True
                    hit = selector
                    try:
                        sample = (element.text or "").strip()[:40]
                    except Exception:
                        sample = ""
                    break
        rows.append([key, "验证码", "✅" if ok else "❌", hit or "-", sample or "-"])
    return rows


def run_selector_check(url: str) -> int:
    log(f"[STEP] 自检: {url}")

    rows = _check_api(url)

    if ChromiumPage is None:
        log("缺少 DrissionPage，跳过页面/验证码部分自检", "WARN")
    else:
        page = None
        user_data_dir = None
        try:
            page, user_data_dir = open_browser()
            try:
                page.get(url, retry=2)
            except Exception as exc:
                log(f"打开页面失败: {exc}", "ERROR")
                page = None
            if page is not None:
                wait_text(page, SELECTORS["expiry"], 25)
                page_keys = [k for k, v in SELECTOR_STAGE.items() if v == "页面"]
                dialog_keys = [k for k, v in SELECTOR_STAGE.items() if v == "弹窗"]
                rows.extend(_check_page_selectors(page, page_keys))

                csrf = read_csrf_token(page)
                rows.append(
                    [
                        "page:csrf-token",
                        "页面",
                        "✅" if csrf else "❌",
                        "meta[name=csrf-token]",
                        f"len={len(csrf)}" if csrf else "-",
                    ]
                )
                try:
                    has_grecaptcha = bool(
                        page.run_js("return typeof grecaptcha !== 'undefined'")
                    )
                except Exception:
                    has_grecaptcha = False
                rows.append(
                    [
                        "page:grecaptcha",
                        "页面",
                        "✅" if has_grecaptcha else "❌",
                        "window.grecaptcha",
                        "已加载" if has_grecaptcha else "未加载",
                    ]
                )

                log("[STEP] 尝试展开续期弹窗（只点一次 Renew server，不点最终 Renew、不碰验证码）")
                if open_renew_dialog(page):
                    rows.extend(_check_page_selectors(page, dialog_keys))
                    rows.extend(_check_captcha_selectors(page))
                else:
                    for key in dialog_keys:
                        rows.append([key, "弹窗", "⚠️", "-", "未展开弹窗"])
                    rows.append(
                        ["recaptcha:anchor_frame", "验证码", "⚠️", "-", "未展开弹窗，未触发验证码"]
                    )
        except Exception as exc:
            log(f"自检过程异常: {exc}", "ERROR")
        finally:
            close_browser(page, user_data_dir)
            stop_display()

    print()
    print_table(("检查项", "阶段", "结果", "命中表达式", "样例"), rows)
    print()
    missing = [row[0] for row in rows if row[2] == "❌"]
    if missing:
        log(f"[STEP] 未命中的检查项: {', '.join(missing)}", "WARN")
        log("[STEP] 去 SELECTORS / API 常量里更新对应项", "WARN")
        return 1
    log("[STEP] 全部关键检查项命中")
    return 0


# ===========================================================================
# 入口
# ===========================================================================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Host2Play 自动续期脚本")
    parser.add_argument(
        "--check",
        nargs="?",
        const="",
        default=None,
        metavar="URL",
        help="只做选择器/API 自检（不续期、不碰验证码）；省略 URL 时用 RENEW_URLS 第一条",
    )
    parser.add_argument(
        "--state", default=None, help=f"state.json 路径（默认 {DEFAULT_STATE_FILE}）"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.check is not None:
        url = (args.check or "").strip()
        if not url:
            configured = load_config(args.state).urls
            url = configured[0] if configured else ""
        if not url:
            log("--check 需要一个 URL，或先在 RENEW_URLS 里配置一条", "ERROR")
            return 1
        return run_selector_check(url)

    return run_renew(load_config(args.state))


if __name__ == "__main__":
    sys.exit(main())

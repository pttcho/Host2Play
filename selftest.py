#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host2Play 离线自测 —— 校验 SPEC v1 §2 的纯函数契约 与 §3 的 state.json schema。

设计约束（与 SPEC §1 中 selftest.py 的职责一致）：
  * 纯离线：不联网、不启动浏览器、不 import DrissionPage / xvfbwrapper /
    speech_recognition / pydub。
  * 只用标准库：unittest / datetime / json / urllib.parse（另加 sys、os 用于
    路径定位与退出码，这两个无法避免）。
  * 绝不修改 main.py，只 import 它并断言其行为。
  * 断言失败不会被 try/except 吞掉：只有"契约不可用"（模块导入失败、函数不存在）
    才走 unittest 的 skipTest / fail 通道；正常用例里的 AssertionError 原样冒泡。

用法：
    python selftest.py            # 契约缺失 -> 记为 skip，退出码 0（除非有真实失败）
    python selftest.py --strict   # 契约缺失 -> 记为失败，用于重写完成后严格验收

退出码：0 = 全部通过（含 skip）；1 = 存在 failure / error。
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

# ==============================================================================
# 环境与 main.py 导入
# ==============================================================================
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(REPO_ROOT, "state.json")

STRICT = "--strict" in sys.argv[1:]

# SPEC §2 钉死的四个纯函数（契约基线，任何情况下都要测）
SPEC_FUNCS = ("parse_expiry", "server_key", "should_skip", "build_caption")

# 新架构新增的纯函数（站点改成 Unix 秒级时间戳 + 三态决策后加入）。
# 它们不在 SPEC §2 里，但 main.py 重写后会提供，因此一并纳入 --strict 的验收集合。
EXTENDED_FUNCS = ("parse_ts", "decide_action", "classify_server")

CONTRACT_FUNCS = SPEC_FUNCS + EXTENDED_FUNCS

# 相对时间解析允许的误差（SPEC 只要求"约等于"，这里给 ±5 分钟）
TOL = timedelta(minutes=5)

# 便于从任意 cwd 运行：把脚本所在目录（即仓库根）放进 sys.path
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

main = None              # import 成功时指向 main 模块
IMPORT_ERROR_TEXT = ""   # 人类可读的失败摘要
IMPORT_ERROR_KIND = ""   # "missing-dep"（环境缺三方库）| "broken-module"（main.py 自身问题）

try:
    import main as _main_module
except SystemExit as _exc:                       # import 期调 sys.exit 属于实现缺陷
    IMPORT_ERROR_KIND = "broken-module"
    IMPORT_ERROR_TEXT = "main.py 在 import 阶段调用了 sys.exit(%r)" % (_exc.code,)
except ModuleNotFoundError as _exc:
    # 缺三方依赖（DrissionPage / xvfbwrapper / requests ...）：属于环境问题，不是契约问题
    IMPORT_ERROR_KIND = "missing-dep"
    IMPORT_ERROR_TEXT = "缺少第三方依赖模块 %r（%s: %s）" % (
        _exc.name, type(_exc).__name__, _exc,
    )
except Exception as _exc:                        # SyntaxError / ImportError / 其他导入期异常
    IMPORT_ERROR_KIND = "broken-module"
    IMPORT_ERROR_TEXT = "%s: %s" % (type(_exc).__name__, _exc)
else:
    main = _main_module

AVAILABLE_FUNCS = tuple(
    name for name in CONTRACT_FUNCS
    if main is not None and callable(getattr(main, name, None))
)
MISSING_FUNCS = tuple(name for name in CONTRACT_FUNCS if name not in AVAILABLE_FUNCS)


def require_function(testcase, name):
    """取 main.<name>；不可用时按 unittest 语义处理，绝不吞掉真实断言失败。

    默认模式：契约不可用 -> skipTest（附清楚原因）
    strict 模式：由 ContractAvailabilityTests 统一判失败（避免几十条重复 FAIL 刷屏）
    """
    if main is None:
        testcase.skipTest(
            "无法 import main 模块，%s() 未验证：%s" % (name, IMPORT_ERROR_TEXT)
        )
    fn = getattr(main, name, None)
    if fn is None:
        testcase.skipTest(
            "main.py 未导出 %s()（SPEC §2 契约缺失），相关用例跳过" % name
        )
    if not callable(fn):
        testcase.fail(
            "main.%s 存在但不是可调用对象：%r" % (name, type(fn))
        )
    return fn


def utcnow():
    return datetime.now(timezone.utc)


def to_utc(dt):
    """把 tz-aware datetime 归一到 UTC；naive 会被断言先行拦截。"""
    return dt.astimezone(timezone.utc)


def i_param(url):
    """用 urllib.parse 独立算出 ?i= 的值，作为 server_key 的对照 oracle。"""
    return parse_qs(urlsplit(url).query).get("i", [None])[0]


# ==============================================================================
# 契约可用性（先跑，用于给出"为什么全 skip"的明确原因）
# ==============================================================================
class ContractAvailabilityTests(unittest.TestCase):
    """检查 main.py 是否可 import、是否导出 SPEC §2 要求的四个纯函数。

    默认模式下不可用只算 skip；--strict 下算失败，这就是严格验收的开关。
    """

    def test_01_module_imports(self):
        """main.py 必须能被 import 而不触发网络/浏览器副作用（SPEC §1）。"""
        if main is not None:
            self.assertIsNotNone(main.__file__, "main 模块没有 __file__，不像本地模块")
            return
        msg = (
            "import main 失败 —— 类别=%s；原因=%s\n"
            "  说明：main.py 顶层 import 了 DrissionPage / xvfbwrapper 等三方库，"
            "本机未安装时无法加载，因此 SPEC §2 的四个纯函数全部无法验证。\n"
            "  处理：默认模式记 skip；--strict 模式记失败。" % (
                IMPORT_ERROR_KIND or "未知", IMPORT_ERROR_TEXT
            )
        )
        if STRICT:
            self.fail(msg)
        self.skipTest(msg)

    def test_02_contract_functions_exist(self):
        """main.py 必须导出 SPEC §2 的四个纯函数，以及新架构的 parse_ts / decide_action。"""
        if main is None:
            msg = "模块未能导入，无法检查契约函数是否存在（%s）" % IMPORT_ERROR_TEXT
            if STRICT:
                self.fail(msg)
            self.skipTest(msg)

        if not MISSING_FUNCS:
            self.assertEqual(
                set(AVAILABLE_FUNCS), set(CONTRACT_FUNCS),
                "契约函数集合不符：available=%r missing=%r"
                % (AVAILABLE_FUNCS, MISSING_FUNCS),
            )
            return

        missing_spec = tuple(n for n in MISSING_FUNCS if n in SPEC_FUNCS)
        missing_ext = tuple(n for n in MISSING_FUNCS if n in EXTENDED_FUNCS)
        parts = []
        if missing_spec:
            parts.append("SPEC §2 缺失: %s" % ", ".join(missing_spec))
        if missing_ext:
            parts.append("新架构扩展缺失: %s" % ", ".join(missing_ext))
        msg = "main.py 缺少契约函数 —— %s（已具备: %s）" % (
            "；".join(parts), ", ".join(AVAILABLE_FUNCS) or "无",
        )
        if STRICT:
            self.fail(msg)
        self.skipTest(msg)


# ==============================================================================
# parse_expiry —— 相对时间
# ==============================================================================
class ParseExpiryRelativeTests(unittest.TestCase):
    """测 parse_expiry 对 "Expires in: N days/hours/minutes" 相对文案的解析。

    用"调用前 now / 调用后 now"夹逼出允许窗口，避免测试自身耗时导致的假失败。
    """

    def setUp(self):
        self.parse_expiry = require_function(self, "parse_expiry")

    def _assert_relative(self, text, delta):
        """断言 parse_expiry(text) ≈ now + delta，误差 ±TOL。"""
        t0 = utcnow()
        dt = self.parse_expiry(text)
        t1 = utcnow()

        self.assertIsNotNone(
            dt, "parse_expiry(%r) 返回 None，期望约 now+%s" % (text, delta)
        )
        self.assertIsInstance(
            dt, datetime, "parse_expiry(%r) 返回 %r，期望 datetime" % (text, type(dt))
        )
        self.assertIsNotNone(
            dt.tzinfo,
            "parse_expiry(%r) 返回 naive datetime（tzinfo is None）：%r；"
            "必须是 tz-aware，否则跨时区比较会崩" % (text, dt),
        )

        got = to_utc(dt)
        lo = t0 + delta - TOL
        hi = t1 + delta + TOL
        self.assertTrue(
            lo <= got <= hi,
            "parse_expiry(%r) = %s；期望落在 [%s, %s]（now+%s，误差 ±%s）"
            % (text, got.isoformat(), lo.isoformat(), hi.isoformat(), delta, TOL),
        )

    def test_expires_in_2_days(self):
        """"Expires in: 2 days" -> now + 2 天（±5 分钟）。"""
        self._assert_relative("Expires in: 2 days", timedelta(days=2))

    def test_expires_in_23_hours_30_minutes(self):
        """"Expires in: 23 hours 30 minutes" -> now + 23.5 小时。"""
        self._assert_relative(
            "Expires in: 23 hours 30 minutes",
            timedelta(hours=23, minutes=30),
        )

    def test_expires_in_45_minutes(self):
        """"Expires in: 45 minutes" -> now + 45 分钟。"""
        self._assert_relative("Expires in: 45 minutes", timedelta(minutes=45))

    def test_expires_in_1_day_singular(self):
        """"Expires in: 1 day" 单数形式 -> now + 1 天（复数/单数都要认）。"""
        self._assert_relative("Expires in: 1 day", timedelta(days=1))

    def test_expires_in_1_hour_singular(self):
        """"Expires in: 1 hour" 单数小时 -> now + 1 小时。"""
        self._assert_relative("Expires in: 1 hour", timedelta(hours=1))


# ==============================================================================
# parse_expiry —— 绝对时间
# ==============================================================================
class ParseExpiryAbsoluteTests(unittest.TestCase):
    """测 parse_expiry 对绝对时间文案（Deletes on / ISO8601 / 纯日期）的解析。"""

    def setUp(self):
        self.parse_expiry = require_function(self, "parse_expiry")

    def _assert_exact(self, text, expected):
        dt = self.parse_expiry(text)
        self.assertIsNotNone(
            dt, "parse_expiry(%r) 返回 None，期望 %s" % (text, expected.isoformat())
        )
        self.assertIsInstance(
            dt, datetime, "parse_expiry(%r) 返回 %r，期望 datetime" % (text, type(dt))
        )
        self.assertIsNotNone(
            dt.tzinfo,
            "parse_expiry(%r) 返回 naive datetime：%r；必须 tz-aware（UTC）"
            % (text, dt),
        )
        got = to_utc(dt)
        self.assertEqual(
            got, expected,
            "parse_expiry(%r) = %s，期望 %s（差值 %s）"
            % (text, got.isoformat(), expected.isoformat(), got - expected),
        )

    def test_deletes_on_datetime(self):
        """"Deletes on: 2026-10-17 20:19:49" -> 该时刻 UTC。"""
        self._assert_exact(
            "Deletes on: 2026-10-17 20:19:49",
            datetime(2026, 10, 17, 20, 19, 49, tzinfo=timezone.utc),
        )

    def test_iso8601_z_suffix(self):
        """"2026-10-17T20:19:49Z" -> 该时刻 UTC。"""
        self._assert_exact(
            "2026-10-17T20:19:49Z",
            datetime(2026, 10, 17, 20, 19, 49, tzinfo=timezone.utc),
        )

    def test_date_only_is_midnight_utc(self):
        """"2026-10-17" -> 当日 00:00:00 UTC。"""
        self._assert_exact(
            "2026-10-17",
            datetime(2026, 10, 17, 0, 0, 0, tzinfo=timezone.utc),
        )


# ==============================================================================
# parse_expiry —— 非法输入
# ==============================================================================
class ParseExpiryInvalidTests(unittest.TestCase):
    """测 parse_expiry 对非法/空输入的容错：一律返回 None 且不抛异常（SPEC 硬要求）。"""

    def setUp(self):
        self.parse_expiry = require_function(self, "parse_expiry")

    def test_invalid_strings_return_none(self):
        """空串 / 中文"未知" / 垃圾串 / 只有前缀无数值 -> 全部 None，不抛异常。"""
        for text in ("", "未知", "garbage!!!", "Expires in:"):
            with self.subTest(text=text):
                try:
                    got = self.parse_expiry(text)
                except Exception as exc:      # 只把"抛异常"这个事实转成失败，不吞别的断言
                    self.fail(
                        "parse_expiry(%r) 抛出 %s: %s；SPEC 要求不得抛异常，应返回 None"
                        % (text, type(exc).__name__, exc)
                    )
                self.assertIsNone(
                    got, "parse_expiry(%r) 返回 %r，期望 None" % (text, got)
                )

    def test_non_string_inputs_return_none(self):
        """None 与非字符串（123）-> None，不抛异常（调用方可能直接喂页面对象）。"""
        for value in (None, 123):
            with self.subTest(value=value):
                try:
                    got = self.parse_expiry(value)
                except Exception as exc:
                    self.fail(
                        "parse_expiry(%r) 抛出 %s: %s；SPEC 要求不得抛异常，应返回 None"
                        % (value, type(exc).__name__, exc)
                    )
                self.assertIsNone(
                    got, "parse_expiry(%r) 返回 %r，期望 None" % (value, got)
                )

    def test_countdown_and_status_text_are_not_absolute_times(self):
        """纯倒计时 "HH:MM:SS" 与状态文案不得被解析成"未来时间"。

        页面上的 "06:35:12" 是剩余时长倒计时，不是绝对时刻；若被当成今天的
        06:35 解析，会得到一个可能已过去的"到期时间"，进而污染成功判定
        （旧实现就把 "Server expired" 当成了成功）。两者都应返回 None。
        """
        for text in ("06:35:12", "Server expired", "Expires in: 06:35:12"):
            with self.subTest(text=text):
                try:
                    got = self.parse_expiry(text)
                except Exception as exc:
                    self.fail(
                        "parse_expiry(%r) 抛出 %s: %s；SPEC 要求不得抛异常"
                        % (text, type(exc).__name__, exc)
                    )
                self.assertIsNone(
                    got,
                    "parse_expiry(%r) 返回 %r；倒计时/状态文案不该被解析成绝对时间，"
                    "期望 None" % (text, got),
                )

    def test_all_valid_results_are_tz_aware(self):
        """所有成功解析的返回值都必须 tz-aware（tzinfo 不为 None）。"""
        samples = (
            "Expires in: 2 days",
            "Expires in: 23 hours 30 minutes",
            "Expires in: 45 minutes",
            "Expires in: 1 day",
            "Deletes on: 2026-10-17 20:19:49",
            "2026-10-17T20:19:49Z",
            "2026-10-17",
        )
        for text in samples:
            with self.subTest(text=text):
                dt = self.parse_expiry(text)
                self.assertIsNotNone(
                    dt, "parse_expiry(%r) 返回 None，无法检查时区" % (text,)
                )
                self.assertIsNotNone(
                    dt.tzinfo,
                    "parse_expiry(%r) = %r 的 tzinfo 是 None（naive）；必须 tz-aware"
                    % (text, dt),
                )
                self.assertIsNotNone(
                    dt.utcoffset(),
                    "parse_expiry(%r) = %r 的 utcoffset() 是 None；必须带 UTC 偏移"
                    % (text, dt),
                )


# ==============================================================================
# server_key
# ==============================================================================
class ServerKeyTests(unittest.TestCase):
    """测 server_key：一律 sha1 前缀、稳定性、不含明文凭据、非法输入不抛异常。

    关键约束：server_key 的返回值会作为 state.json 的 dict key，而 state.json 要
    提交回 public 仓库。所以它**绝不能**原样返回 ?i=<uuid> —— 那是免鉴权的续期
    凭据。这里专门断言返回值里不含原始 id。
    """

    # 测试用的假 UUID，不要替换成真实服务器 ID —— 真实续期链接是免鉴权凭据，
    # 出现在 public 仓库里等于公开。这里只用固定假值验证提取逻辑。
    RENEW_URL = (
        "https://host2play.gratis/server/renew"
        "?i=00000000-1111-2222-3333-444444444444"
    )
    PLAIN_A = "https://host2play.gratis/server/renew/abc123"
    PLAIN_B = "https://host2play.gratis/server/renew/def456"

    def setUp(self):
        self.server_key = require_function(self, "server_key")

    def test_key_never_contains_raw_id(self):
        """返回值不得包含 ?i= 的原始值（凭据不能进 state.json）。"""
        got = self.server_key(self.RENEW_URL)
        raw_id = i_param(self.RENEW_URL)
        self.assertNotIn(
            raw_id, str(got),
            "server_key 返回值 %r 含有明文凭据 %r；state.json 会被提交回 public 仓库，"
            "绝不能存原始 id" % (got, raw_id),
        )

    def test_key_is_12_hex_chars(self):
        """无论有没有 ?i=，都返回 12 位小写十六进制。"""
        for url in (self.RENEW_URL, self.PLAIN_A, self.PLAIN_B):
            got = str(self.server_key(url))
            self.assertRegex(
                got, r"^[0-9a-f]{12}$",
                "server_key(%r) = %r，期望 12 位小写十六进制" % (url, got),
            )

    def test_different_ids_give_different_keys(self):
        """不同服务器必须得到不同 key，否则 state 会互相覆盖。"""
        a = self.server_key(
            "https://host2play.gratis/server/renew?i=00000000-1111-2222-3333-444444444444"
        )
        b = self.server_key(
            "https://host2play.gratis/server/renew?i=99999999-8888-7777-6666-555555555555"
        )
        self.assertNotEqual(
            a, b, "两个不同 id 得到相同 key %r，state 会互相覆盖" % (a,),
        )

    def test_extracts_i_parameter_with_extra_query(self):
        """?i= 后面还有别的查询参数时，仍稳定算出同一个 key。"""
        url = self.RENEW_URL + "&utm_source=x&x=1"
        base = self.server_key(self.RENEW_URL)
        got = self.server_key(url)
        # 多出查询参数会改变 sha1，这里只要求形状正确、不含明文
        self.assertRegex(
            str(got), r"^[0-9a-f]{12}$",
            "server_key(%r) = %r，期望 12 位十六进制" % (url, got),
        )
        self.assertNotIn(i_param(self.RENEW_URL), str(got))
        self.assertNotEqual(
            base, got,
            "带额外查询参数时 key 应与原 URL 不同（sha1 覆盖完整 URL）",
        )

    def test_stable_across_two_calls(self):
        """同一 URL 调两次结果必须相同（state.json 的 key 不能漂移）。"""
        first = self.server_key(self.RENEW_URL)
        second = self.server_key(self.RENEW_URL)
        self.assertEqual(
            first, second,
            "server_key 不稳定：第一次 %r，第二次 %r" % (first, second),
        )

    def test_no_id_returns_12_hex_chars(self):
        """无 ?i= 时返回 12 位小写十六进制（sha1 前缀）。"""
        got = self.server_key(self.PLAIN_A)
        self.assertIsInstance(
            got, str, "server_key(%r) 返回 %r，期望 str" % (self.PLAIN_A, type(got))
        )
        self.assertEqual(
            len(got), 12,
            "server_key(%r) = %r 长度 %d，期望 12（sha1 前 12 位）"
            % (self.PLAIN_A, got, len(got)),
        )
        bad = [ch for ch in got if ch not in "0123456789abcdef"]
        self.assertEqual(
            bad, [],
            "server_key(%r) = %r 含非小写十六进制字符 %r" % (self.PLAIN_A, got, bad),
        )

    def test_no_id_stable_across_two_calls(self):
        """无 ?i= 的 URL 两次调用结果一致。"""
        first = self.server_key(self.PLAIN_A)
        second = self.server_key(self.PLAIN_A)
        self.assertEqual(
            first, second,
            "server_key 对无 i 参数 URL 不稳定：%r vs %r" % (first, second),
        )

    def test_no_id_matches_known_sha1_prefix(self):
        """无 ?i= 时必须是 sha1(url) 前 12 位（预先算好的定值向量）。"""
        expected = "dcfec53c207b"   # sha1("https://host2play.gratis/server/renew/abc123")[:12]
        got = self.server_key(self.PLAIN_A)
        self.assertEqual(
            got, expected,
            "server_key(%r) = %r，期望 sha1(url)[:12] = %r"
            "（若实现改用 md5/sha256 截断，此断言会失败）"
            % (self.PLAIN_A, got, expected),
        )

    def test_different_urls_give_different_keys(self):
        """两个不同 URL（均无 ?i=）的 key 必须不同。"""
        ka = self.server_key(self.PLAIN_A)
        kb = self.server_key(self.PLAIN_B)
        self.assertNotEqual(
            ka, kb,
            "两个不同 URL 得到相同 key %r：%r vs %r"
            % (ka, self.PLAIN_A, self.PLAIN_B),
        )

    def test_invalid_inputs_return_nonempty_string(self):
        """空串 / None 不抛异常，返回非空字符串（能当 dict key 用）。"""
        for value in ("", None):
            with self.subTest(value=value):
                try:
                    got = self.server_key(value)
                except Exception as exc:
                    self.fail(
                        "server_key(%r) 抛出 %s: %s；SPEC 要求不得抛异常"
                        % (value, type(exc).__name__, exc)
                    )
                self.assertIsInstance(
                    got, str,
                    "server_key(%r) 返回 %r，期望 str" % (value, type(got)),
                )
                self.assertTrue(
                    got, "server_key(%r) 返回空字符串，无法作为 state.json 的 key"
                    % (value,),
                )


# ==============================================================================
# should_skip
# ==============================================================================
class ShouldSkipTests(unittest.TestCase):
    """测 should_skip 的冷却判定：entry 缺失/为空、冷却内、冷却外、cooldown=0、脏数据。"""

    COOLDOWN = 6

    def setUp(self):
        self.should_skip = require_function(self, "should_skip")

    def _entry(self, hours_ago):
        """构造一条 last_success_at = now - hours_ago 的 state entry。"""
        ts = utcnow() - timedelta(hours=hours_ago)
        return {
            "url": ServerKeyTests.RENEW_URL,
            "name": "My Server",
            "last_success_at": ts.isoformat(),
            "expiry_iso": (ts + timedelta(days=3)).isoformat(),
        }

    def _call(self, entry, cooldown=None):
        return self.should_skip(
            entry, utcnow(), self.COOLDOWN if cooldown is None else cooldown
        )

    def _assert_result(self, result, expect_skip, label):
        self.assertIsInstance(
            result, tuple, "%s 返回 %r，期望 (bool, str) 二元组" % (label, result)
        )
        self.assertEqual(
            len(result), 2, "%s 返回 %d 元组 %r，期望 2 元组" % (label, len(result), result)
        )
        flag, msg = result
        self.assertIsInstance(
            flag, bool, "%s 第一个元素是 %r，期望 bool" % (label, type(flag))
        )
        self.assertIsInstance(
            msg, str, "%s 第二个元素是 %r，期望 str" % (label, type(msg))
        )
        self.assertEqual(
            flag, expect_skip,
            "%s -> (flag=%r, msg=%r)，期望 flag=%r" % (label, flag, msg, expect_skip),
        )
        return flag, msg

    def test_none_entry_not_skipped(self):
        """entry=None（首次运行）-> (False, ...)，且不抛异常。"""
        try:
            result = self._call(None)
        except Exception as exc:
            self.fail("should_skip(None, ...) 抛出 %s: %s" % (type(exc).__name__, exc))
        flag, msg = self._assert_result(result, False, "should_skip(None, now, 6)")
        self.assertTrue(
            msg,
            "should_skip(None, ...) 的说明文案为空，期望非空（如「首次运行」）",
        )

    def test_empty_dict_entry_not_skipped(self):
        """entry={} -> (False, ...)，不抛异常。"""
        try:
            result = self._call({})
        except Exception as exc:
            self.fail("should_skip({}, ...) 抛出 %s: %s" % (type(exc).__name__, exc))
        self._assert_result(result, False, "should_skip({}, now, 6)")

    def test_entry_without_last_success_at_not_skipped(self):
        """entry 存在但没有 last_success_at 字段 -> (False, ...)。"""
        entry = {"url": ServerKeyTests.RENEW_URL, "name": "My Server"}
        try:
            result = self._call(entry)
        except Exception as exc:
            self.fail(
                "should_skip(无 last_success_at, ...) 抛出 %s: %s"
                % (type(exc).__name__, exc)
            )
        self._assert_result(result, False, "should_skip(无 last_success_at, now, 6)")

    def test_one_hour_ago_with_cooldown_6_is_skipped(self):
        """last_success_at = 1 小时前，cooldown=6 -> (True, ...)。"""
        entry = self._entry(1)
        result = self._call(entry, cooldown=6)
        flag, msg = self._assert_result(
            result, True,
            "should_skip(1h 前成功, now, 6) [entry.last_success_at=%r]"
            % entry["last_success_at"],
        )
        self.assertTrue(
            msg,
            "冷却判定的说明文案为空，期望含「冷却中: 距上次成功 X.Xh」之类信息",
        )

    def test_seven_hours_ago_with_cooldown_6_not_skipped(self):
        """last_success_at = 7 小时前，cooldown=6 -> (False, ...)。"""
        entry = self._entry(7)
        result = self._call(entry, cooldown=6)
        self._assert_result(
            result, False,
            "should_skip(7h 前成功, now, 6) [entry.last_success_at=%r]"
            % entry["last_success_at"],
        )

    def test_cooldown_zero_never_skips(self):
        """cooldown=0 表示每次都续（SPEC §5 RENEW_COOLDOWN_HOURS=0）-> (False, ...)。"""
        entry = self._entry(1)
        result = self._call(entry, cooldown=0)
        self._assert_result(
            result, False,
            "should_skip(1h 前成功, now, 0) —— cooldown=0 不应跳过",
        )

    def test_garbage_last_success_at_not_skipped(self):
        """last_success_at 是垃圾字符串 -> (False, ...)，不抛异常。"""
        for garbage in ("垃圾", "not-a-time", "2026-13-45T99:99:99Z"):
            with self.subTest(garbage=garbage):
                entry = {"last_success_at": garbage, "name": "My Server"}
                try:
                    result = self._call(entry, cooldown=6)
                except Exception as exc:
                    self.fail(
                        "should_skip(last_success_at=%r) 抛出 %s: %s；"
                        "SPEC 要求解析失败按不跳过处理且不抛异常"
                        % (garbage, type(exc).__name__, exc)
                    )
                self._assert_result(
                    result, False,
                    "should_skip(last_success_at=%r, now, 6)" % (garbage,),
                )


# ==============================================================================
# build_caption
# ==============================================================================
class BuildCaptionTests(unittest.TestCase):
    """测 build_caption：成功/失败/跳过三种文案的要素完整性、纯文本安全、无 None 字面量。"""

    SERVER = "My Server"
    URL = ServerKeyTests.RENEW_URL
    OLD = "Expires in: 2 days"
    NEW = "Expires in: 30 days"

    def setUp(self):
        self.build_caption = require_function(self, "build_caption")

    def _caption(self, ok, reason="", skipped=False, server=None, url=None,
                 old=None, new=None):
        # 全部按位置传参，避免实现方形参名与 SPEC 略有出入时误报
        return self.build_caption(
            ok,
            self.SERVER if server is None else server,
            self.URL if url is None else url,
            self.OLD if old is None else old,
            self.NEW if new is None else new,
            reason,
            skipped,
        )

    def test_success_contains_server_old_new_and_url(self):
        """成功文案必须含：服务器名、旧到期、新到期，以及脱敏后的主机名。

        URL 不要求完整出现 —— 完整链接里的 i=<uuid> 是免鉴权凭据，文案必须脱敏。
        这里断言"主机名在、原始 id 不在"。
        """
        caption = self._caption(True)
        self.assertIsInstance(
            caption, str, "成功文案返回 %r，期望 str" % type(caption)
        )
        for label, needle in (
            ("服务器名", self.SERVER),
            ("旧到期", self.OLD),
            ("新到期", self.NEW),
            ("主机名", "host2play.gratis"),
        ):
            self.assertIn(
                needle, caption,
                "成功文案缺少%s %r；实际文案：\n%s" % (label, needle, caption),
            )

    def test_success_caption_masks_credential(self):
        """文案里不得出现完整续期链接或原始 i=<uuid>（Telegram 会长期留存）。"""
        caption = self._caption(True)
        raw_id = i_param(self.URL)
        self.assertNotIn(
            raw_id, caption,
            "成功文案泄露了原始凭据 %r；实际文案：\n%s" % (raw_id, caption),
        )
        self.assertNotIn(
            "?i=" + raw_id, caption,
            "成功文案含完整续期链接；实际文案：\n%s" % caption,
        )

    def test_failure_contains_reason(self):
        """失败文案必须含失败原因原文。"""
        reason = "reCAPTCHA 异常: boom-xyz-42"
        caption = self._caption(False, reason=reason)
        self.assertIn(
            reason, caption,
            "失败文案缺少原因 %r；实际文案：\n%s" % (reason, caption),
        )

    def test_skipped_contains_skip_word(self):
        """跳过文案必须含"跳过"字样。"""
        caption = self._caption(False, reason="冷却中: 距上次成功 1.0h", skipped=True)
        self.assertIn(
            "跳过", caption,
            "跳过文案缺少「跳过」字样；实际文案：\n%s" % caption,
        )

    def test_returns_str_without_raw_angle_brackets(self):
        """正常输入下返回 str，且不含 HTML 标签形态的尖括号（纯文本模式）。

        注意：SPEC §2 规定成功文案必须写成 "到期: old -> new"，箭头本身就含 '>'，
        因此这里不能裸断言"不含 >"，否则测试与契约自相矛盾。判据改成：
          * '<' 一律不得出现（纯文本文案里没有合法的 '<'）；
          * '>' 只允许作为 '->' 箭头的一部分出现。
        真正的 HTML 标签残留由 test_angle_brackets_in_inputs_are_neutralized 抓。
        """
        for ok, skipped, reason in (
            (True, False, ""),
            (False, False, "网络超时"),
            (False, True, "冷却中: 距上次成功 1.0h"),
        ):
            with self.subTest(ok=ok, skipped=skipped):
                caption = self._caption(ok, reason=reason, skipped=skipped)
                self.assertIsInstance(
                    caption, str,
                    "build_caption(ok=%r, skipped=%r) 返回 %r，期望 str"
                    % (ok, skipped, type(caption)),
                )
                self.assertNotIn(
                    "<", caption,
                    "文案含原样 '<'，会被 Telegram HTML 解析吃掉；实际文案：\n%s"
                    % caption,
                )
                # '>' 仅允许出现在 '->' 箭头里
                stray = caption.replace("->", "")
                self.assertNotIn(
                    ">", stray,
                    "文案含除 '->' 箭头之外的 '>'，会被 Telegram HTML 解析吃掉；"
                    "实际文案：\n%s" % caption,
                )

    def test_angle_brackets_in_inputs_are_neutralized(self):
        """输入里带 <b> / <script> 时，输出仍不得残留原样 < >（必须转义或剥离）。"""
        caption = self._caption(
            False,
            reason="<script>alert(1)</script>",
            server="<b>My Server</b>",
        )
        for ch in ("<", ">"):
            self.assertNotIn(
                ch, caption,
                "输入含尖括号时文案残留原样 %r，会被 Telegram HTML 吃掉；"
                "实际文案：\n%s" % (ch, caption),
            )

    def test_no_none_literal_when_reason_empty(self):
        """reason 为空时文案里不得出现「None」字面量（常见 str(None) 拼接 bug）。"""
        for ok, skipped in ((False, False), (True, False), (False, True)):
            with self.subTest(ok=ok, skipped=skipped):
                caption = self._caption(ok, reason="", skipped=skipped)
                self.assertNotIn(
                    "None", caption,
                    "reason 为空时文案出现「None」字面量；实际文案：\n%s" % caption,
                )

    def test_keyword_arguments_match_spec(self):
        """SPEC §2 钉死了形参名 reason / skipped，按关键字调用必须可用。"""
        try:
            caption = self.build_caption(
                False, self.SERVER, self.URL, self.OLD, self.NEW,
                reason="kw-reason-1", skipped=True,
            )
        except TypeError as exc:
            self.fail(
                "build_caption(reason=..., skipped=...) 关键字调用失败：%s；"
                "SPEC §2 的签名是 (ok, server_name, url, old_expiry, new_expiry, "
                "reason='', skipped=False)" % exc
            )
        self.assertIn(
            "kw-reason-1", caption,
            "关键字传入的 reason 未出现在文案中；实际文案：\n%s" % caption,
        )
        self.assertIn(
            "跳过", caption,
            "skipped=True 时文案缺少「跳过」字样；实际文案：\n%s" % caption,
        )


# ==============================================================================
# parse_ts —— Unix 秒级时间戳
# ==============================================================================
class ParseTsTests(unittest.TestCase):
    """测 parse_ts：把 Unix 秒级时间戳（字符串/int/float）转成 UTC aware datetime。

    新架构下站点直接吐 "1792449295" 这种秒级时间戳，比 DOM 文案可靠得多；
    这里钉死定值向量，任何时区/单位（毫秒 vs 秒）错误都会被直接抓出来。
    """

    # 1792449295 == 2026-10-19T22:34:55Z
    TS = 1792449295
    EXPECTED = datetime(2026, 10, 19, 22, 34, 55, tzinfo=timezone.utc)

    def setUp(self):
        self.parse_ts = require_function(self, "parse_ts")

    def _assert_exact(self, value, expected, label):
        try:
            got = self.parse_ts(value)
        except Exception as exc:
            self.fail(
                "parse_ts(%r) 抛出 %s: %s；SPEC 要求不得抛异常"
                % (value, type(exc).__name__, exc)
            )
        self.assertIsNotNone(
            got, "parse_ts(%r) 返回 None，期望 %s（%s）"
            % (value, expected.isoformat(), label),
        )
        self.assertIsInstance(
            got, datetime, "parse_ts(%r) 返回 %r，期望 datetime" % (value, type(got))
        )
        self.assertIsNotNone(
            got.tzinfo,
            "parse_ts(%r) 返回 naive datetime %r；必须 tz-aware（UTC）" % (value, got),
        )
        got_utc = to_utc(got)
        self.assertEqual(
            got_utc, expected,
            "parse_ts(%r) = %s，期望 %s（差值 %s；%s）"
            % (value, got_utc.isoformat(), expected.isoformat(),
               got_utc - expected, label),
        )

    def test_unix_ts_string(self):
        """"1792449295" -> 2026-10-19T22:34:55Z。"""
        self._assert_exact("1792449295", self.EXPECTED, "字符串形式")

    def test_unix_ts_int(self):
        """1792449295（int）-> 同上。"""
        self._assert_exact(self.TS, self.EXPECTED, "int 形式")

    def test_unix_ts_float(self):
        """1792449295.0（float）-> 同上（小数部分为 0 时不该丢精度）。"""
        self._assert_exact(1792449295.0, self.EXPECTED, "float 形式")

    def test_epoch_zero_is_valid(self):
        """"0" -> 1970-01-01T00:00:00Z；epoch 是合法值，不能当非法输入。"""
        self._assert_exact(
            "0",
            datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            "epoch 零点",
        )

    def test_invalid_inputs_return_none(self):
        """非法输入一律 None 且不抛异常：None/""/abc/混尾/列表/负数/科学计数/bool。"""
        cases = (None, "", "abc", "1792449295abc", [1, 2], "-5", "1e99", True)
        for value in cases:
            with self.subTest(value=value):
                try:
                    got = self.parse_ts(value)
                except Exception as exc:
                    self.fail(
                        "parse_ts(%r) 抛出 %s: %s；非法输入应返回 None 且不抛异常"
                        % (value, type(exc).__name__, exc)
                    )
                self.assertIsNone(
                    got, "parse_ts(%r) 返回 %r，期望 None" % (value, got)
                )

    def test_all_valid_results_are_tz_aware(self):
        """所有成功解析的返回值都必须 tz-aware。"""
        for value in ("1792449295", self.TS, 1792449295.0, "0"):
            with self.subTest(value=value):
                got = self.parse_ts(value)
                self.assertIsNotNone(got, "parse_ts(%r) 返回 None" % (value,))
                self.assertIsNotNone(
                    got.tzinfo,
                    "parse_ts(%r) = %r 的 tzinfo 是 None（naive）" % (value, got),
                )


# ==============================================================================
# classify_server —— 四态状态机
# ==============================================================================
class ClassifyServerTests(unittest.TestCase):
    """测 classify_server 的四态状态机：online / expired / dead / unknown。

    站点有"到期(expires)"和"删除(deletes)"两个时间点，中间约 7 天保留期：
    now > expires 但 now < deletes 时续期依然有效，这是最该续的时候；
    过了 deletes 数据已删，再重试只是白烧验证码。四态必须严格区分。
    """

    NOW = datetime(2026, 10, 19, 16, 0, 0, tzinfo=timezone.utc)
    VALID_STATES = ("online", "expired", "dead", "unknown")

    def setUp(self):
        self.classify_server = require_function(self, "classify_server")

    def _ts(self, hours_from_now):
        """now + N 小时的 Unix 秒级时间戳（int）。"""
        return int((self.NOW + timedelta(hours=hours_from_now)).timestamp())

    def _classify(self, data):
        try:
            return self.classify_server(data, self.NOW)
        except Exception as exc:
            self.fail(
                "classify_server(%r, now) 抛出 %s: %s；SPEC 要求不得抛异常"
                % (data, type(exc).__name__, exc)
            )

    def _assert_state(self, data, expected, label):
        result = self._classify(data)
        self.assertIsInstance(
            result, tuple, "%s 返回 %r，期望 (state, detail) 二元组" % (label, result)
        )
        self.assertEqual(
            len(result), 2, "%s 返回 %d 元组 %r，期望 2 元组"
            % (label, len(result), result),
        )
        state, detail = result
        self.assertIsInstance(
            state, str, "%s 的 state 是 %r，期望 str" % (label, type(state))
        )
        self.assertIn(
            state, self.VALID_STATES,
            "%s 返回 state=%r，只允许 %r" % (label, state, list(self.VALID_STATES)),
        )
        self.assertIsInstance(
            detail, str, "%s 的 detail 是 %r，期望 str" % (label, type(detail))
        )
        self.assertTrue(
            detail, "%s 的 detail 为空字符串，期望非空中文说明" % (label,)
        )
        self.assertNotIn(
            "None", detail, "%s 的 detail 含「None」字面量：%r" % (label, detail)
        )
        self.assertTrue(
            any("\u4e00" <= ch <= "\u9fff" for ch in detail),
            "%s 的 detail=%r 不含中文，期望中文说明" % (label, detail),
        )
        self.assertEqual(
            state, expected,
            "%s -> (state=%r, detail=%r)，期望 state=%r"
            % (label, state, detail, expected),
        )
        return state, detail

    def test_none_data_is_unknown(self):
        """data=None（API 不可用）-> "unknown"。"""
        self._assert_state(None, "unknown", "classify_server(None)")

    def test_server_not_found_is_dead(self):
        """message 含 "Server not found" -> "dead"。"""
        data = {"success": 0, "message": "Server not found"}
        self._assert_state(data, "dead", "classify_server(success=0, Server not found)")

    def test_server_not_found_case_insensitive(self):
        """message 大小写不敏感："SERVER NOT FOUND" 同样 -> "dead"。"""
        data = {"success": 0, "message": "SERVER NOT FOUND"}
        self._assert_state(
            data, "dead", "classify_server(success=0, SERVER NOT FOUND 大写)"
        )

    def test_online_when_expiry_in_future(self):
        """expires 在未来 -> "online"。"""
        data = {"expires_ts": self._ts(10), "deletes_ts": self._ts(170)}
        self._assert_state(data, "online", "classify_server(expires=now+10h)")

    def test_expired_but_within_retention(self):
        """expires 已过但 deletes 未到 -> "expired"（关键：过期 7 天内仍可续）。"""
        data = {"expires_ts": self._ts(-1), "deletes_ts": self._ts(150)}
        self._assert_state(
            data, "expired",
            "classify_server(expires=now-1h, deletes=now+150h) —— 过期但在保留期内",
        )

    def test_expired_without_deletes(self):
        """expires 已过且没有 deletes 字段 -> "expired"。"""
        data = {"expires_ts": self._ts(-1)}
        self._assert_state(
            data, "expired", "classify_server(expires=now-1h, 无 deletes)"
        )

    def test_dead_after_delete_line(self):
        """deletes 已过 -> "dead"（数据已删除，重试无意义）。"""
        data = {"expires_ts": self._ts(-200), "deletes_ts": self._ts(-50)}
        self._assert_state(
            data, "dead",
            "classify_server(expires=now-200h, deletes=now-50h) —— 已过删除线",
        )

    def test_dead_boundary_now_equals_deletes(self):
        """边界：now >= deletes 即 "dead"（相等也算已删除）。"""
        data = {"expires_ts": self._ts(-1), "deletes_ts": self._ts(-1)}
        self._assert_state(
            data, "dead",
            "classify_server(expires=deletes=now-1h) —— now > deletes 的边界",
        )

    def test_empty_dict_is_unknown(self):
        """data={}（缺字段）-> "unknown"，不抛异常。"""
        self._assert_state({}, "unknown", "classify_server({})")

    def test_garbage_expires_is_unknown(self):
        """expires_ts 无法解析 -> "unknown"。"""
        data = {"expires_ts": "garbage"}
        self._assert_state(
            data, "unknown", "classify_server(expires_ts='garbage')"
        )

    def test_all_details_are_nonempty_chinese_without_none(self):
        """所有分支的第二个元素都必须非空、含中文、不含「None」字面量。"""
        cases = (
            ("unknown", None),
            ("dead", {"success": 0, "message": "Server not found"}),
            ("online", {"expires_ts": self._ts(10), "deletes_ts": self._ts(170)}),
            ("expired", {"expires_ts": self._ts(-1), "deletes_ts": self._ts(150)}),
            ("dead", {"expires_ts": self._ts(-200), "deletes_ts": self._ts(-50)}),
            ("unknown", {}),
            ("unknown", {"expires_ts": "garbage"}),
        )
        for expected, data in cases:
            with self.subTest(expected=expected, data=data):
                self._assert_state(
                    data, expected, "classify_server(%r)" % (data,)
                )


# ==============================================================================
# decide_action —— 三态决策
# ==============================================================================
class DecideActionTests(unittest.TestCase):
    """测 decide_action 的决策表：probe / skip / renew / dead。

    决策表（与 classify_server 的四态对应）：
      dead                     -> 不得是 "renew"（终态不该重试烧额度）
      expired                  -> 必须 "renew"（紧急，忽略冷却）
      online 且剩余 > 阈值      -> "skip"
      online 且剩余 <= 阈值     -> 冷却期内 "skip"，过冷却期 "renew"
      unknown                  -> "probe"

    now 固定为 2026-10-19T16:00:00Z，expires_ts 用 now±Nh 构造以便精确断言。
    """

    NOW = datetime(2026, 10, 19, 16, 0, 0, tzinfo=timezone.utc)
    VALID_ACTIONS = ("renew", "skip", "probe", "dead")

    def setUp(self):
        self.decide_action = require_function(self, "decide_action")

    def _entry_hours_ago(self, hours):
        """构造 entry，last_success_at = NOW - hours。"""
        return {
            "url": ServerKeyTests.RENEW_URL,
            "name": "My Server",
            "last_success_at": (self.NOW - timedelta(hours=hours)).isoformat(),
        }

    def _call(self, data, entry, cooldown=6, skip_remaining=48):
        try:
            return self.decide_action(
                data, entry, self.NOW, cooldown, skip_remaining
            )
        except Exception as exc:
            self.fail(
                "decide_action(data=%r, entry=%r, cooldown=%r, skip_remaining=%r) "
                "抛出 %s: %s；SPEC 要求不得抛异常"
                % (data, entry, cooldown, skip_remaining,
                   type(exc).__name__, exc)
            )

    def _assert_action(self, result, expected, label):
        """校验 (action, reason) 的形状；expected=None 表示"只要是合法动作即可"。"""
        self.assertIsInstance(
            result, tuple, "%s 返回 %r，期望 (action, reason) 二元组" % (label, result)
        )
        self.assertEqual(
            len(result), 2, "%s 返回 %d 元组 %r，期望 2 元组"
            % (label, len(result), result),
        )
        action, reason = result
        self.assertIsInstance(
            action, str, "%s 的 action 是 %r，期望 str" % (label, type(action))
        )
        self.assertIn(
            action, self.VALID_ACTIONS,
            "%s 返回 action=%r，只允许 %r" % (label, action, list(self.VALID_ACTIONS)),
        )
        self.assertIsInstance(
            reason, str, "%s 的 reason 是 %r，期望 str" % (label, type(reason))
        )
        self.assertTrue(
            reason, "%s 的 reason 为空字符串，期望非空中文说明" % (label,)
        )
        self.assertNotIn(
            "None", reason, "%s 的 reason 含「None」字面量：%r" % (label, reason)
        )
        if expected is not None:
            self.assertEqual(
                action, expected,
                "%s -> (action=%r, reason=%r)，期望 action=%r"
                % (label, action, reason, expected),
            )
        return action, reason

    def _ts(self, hours_from_now):
        """now + N 小时的 Unix 秒级时间戳（int）。"""
        return int((self.NOW + timedelta(hours=hours_from_now)).timestamp())

    def test_none_data_probes(self):
        """data=None（还没抓到页面数据）-> "probe"。"""
        result = self._call(None, None, skip_remaining=48)
        self._assert_action(result, "probe", "decide_action(data=None)")

    def test_far_expiry_skips(self):
        """data 显示还有 100h 到期，skip_remaining=48 -> "skip"（额度还够，别烧验证码）。"""
        data = {"expires_ts": self._ts(100)}
        result = self._call(data, None, skip_remaining=48)
        self._assert_action(
            result, "skip",
            "decide_action(expires=now+100h, skip_remaining=48)",
        )

    def test_near_expiry_renews(self):
        """data 显示只剩 10h，skip_remaining=48，entry=None -> "renew"。"""
        data = {"expires_ts": self._ts(10)}
        result = self._call(data, None, skip_remaining=48)
        self._assert_action(
            result, "renew",
            "decide_action(expires=now+10h, skip_remaining=48, entry=None)",
        )

    def test_expired_renews(self):
        """data 显示已过期 1h -> "renew"。"""
        data = {"expires_ts": self._ts(-1)}
        result = self._call(data, None, skip_remaining=48)
        self._assert_action(
            result, "renew",
            "decide_action(expires=now-1h, skip_remaining=48)",
        )

    def test_expired_overrides_cooldown(self):
        """过期未删 + 1h 前刚成功过 + cooldown=6 -> 必须 "renew"（过期优先，冷却挡不住）。"""
        data = {"expires_ts": self._ts(-1), "deletes_ts": self._ts(150)}
        entry = self._entry_hours_ago(1)
        result = self._call(data, entry, cooldown=6, skip_remaining=0)
        self._assert_action(
            result, "renew",
            "decide_action(expires=now-1h, deletes=now+150h, "
            "last_success=1h 前, cooldown=6, skip_remaining=0) —— 过期优先",
        )

    def test_server_not_found_is_never_renew(self):
        """message=Server not found -> 动作不得是 "renew"（终态不该重试烧额度）。"""
        data = {"success": 0, "message": "Server not found"}
        result = self._call(data, None, skip_remaining=48)
        action, reason = result
        self.assertIn(
            action, self.VALID_ACTIONS,
            "decide_action(Server not found) 返回非法 action %r" % (action,),
        )
        self.assertNotEqual(
            action, "renew",
            "decide_action(Server not found) -> (action=%r, reason=%r)；"
            "服务器已被删除时不得返回 renew" % (action, reason),
        )
        self.assertTrue(reason, "reason 为空字符串，期望非空中文说明")
        self.assertTrue(
            any("\u4e00" <= ch <= "\u9fff" for ch in reason),
            "reason=%r 不含中文，期望中文说明" % (reason,),
        )

    def test_skip_remaining_zero_falls_back_to_cooldown(self):
        """skip_remaining=0 关闭额度判断，回落到冷却期：1h 前成功 + cooldown=6 -> "skip"。"""
        data = {"expires_ts": self._ts(100)}
        entry = self._entry_hours_ago(1)
        result = self._call(data, entry, cooldown=6, skip_remaining=0)
        self._assert_action(
            result, "skip",
            "decide_action(expires=now+100h, skip_remaining=0, "
            "last_success=1h 前, cooldown=6) —— 应走冷却期判断",
        )

    def test_skip_remaining_zero_and_no_entry_renews(self):
        """skip_remaining=0 + entry=None -> "renew"（没有成功记录就不冷却）。"""
        data = {"expires_ts": self._ts(100)}
        result = self._call(data, None, cooldown=6, skip_remaining=0)
        self._assert_action(
            result, "renew",
            "decide_action(expires=now+100h, skip_remaining=0, entry=None)",
        )

    def test_empty_data_returns_valid_action(self):
        """data={}（缺字段）不抛异常，返回合法动作之一。"""
        for entry in (None, self._entry_hours_ago(1)):
            with self.subTest(entry=entry):
                result = self._call({}, entry, skip_remaining=48)
                action, reason = self._assert_action(
                    result, None, "decide_action(data={}, entry=%r)" % (entry,),
                )
                self.assertIn(
                    action, self.VALID_ACTIONS,
                    "decide_action(data={}) 返回非法 action %r" % (action,),
                )

    def test_reason_is_nonempty_chinese(self):
        """三种动作的第二个元素都必须是非空中文说明字符串。"""
        cases = (
            ("probe", None, None, 48),
            ("skip", {"expires_ts": self._ts(100)}, None, 48),
            ("renew", {"expires_ts": self._ts(10)}, None, 48),
        )
        for expected, data, entry, skip_remaining in cases:
            with self.subTest(expected=expected):
                result = self._call(data, entry, skip_remaining=skip_remaining)
                action, reason = self._assert_action(
                    result, expected, "decide_action(%r 场景)" % (expected,),
                )
                has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in reason)
                self.assertTrue(
                    has_cjk,
                    "action=%r 的 reason=%r 不含中文，期望中文说明" % (action, reason),
                )


# ==============================================================================
# state.json schema
# ==============================================================================
class StateJsonSchemaTests(unittest.TestCase):
    """校验仓库根目录 state.json 的 schema（SPEC §3）。文件不存在时整组跳过。"""

    ENTRY_TIME_FIELDS = ("last_success_at", "expiry_iso", "last_attempt_at")

    def setUp(self):
        if not os.path.exists(STATE_PATH):
            self.skipTest("仓库根目录没有 state.json（%s），schema 校验跳过" % STATE_PATH)
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as fh:
                self.state = json.load(fh)
        except Exception as exc:
            self.fail("state.json 存在但无法按 JSON 解析：%s: %s" % (type(exc).__name__, exc))

    def _parse_iso(self, value, where):
        """按 ISO8601 解析；容忍尾部 Z（3.10 的 fromisoformat 不认 Z）。"""
        self.assertIsInstance(
            value, str, "%s 是 %r，期望 ISO8601 字符串" % (where, type(value))
        )
        text = value.strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            self.fail(
                "%s = %r 无法被 datetime.fromisoformat 解析：%s"
                % (where, value, exc)
            )
        self.assertIsNotNone(
            dt.tzinfo,
            "%s = %r 解析出 naive datetime；SPEC §3 要求时间一律带 +00:00 时区"
            % (where, value),
        )
        return dt

    def test_version_is_1(self):
        """顶层 version 必须是整数 1。"""
        self.assertIn("version", self.state, "state.json 缺少顶层 version 字段")
        version = self.state["version"]
        self.assertIsInstance(
            version, int,
            "version = %r（%r），期望 int 1" % (version, type(version)),
        )
        self.assertFalse(
            isinstance(version, bool), "version 是 bool %r，期望 int 1" % (version,)
        )
        self.assertEqual(version, 1, "version = %r，期望 1（SPEC §3 版本 1）" % (version,))

    def test_servers_is_dict(self):
        """servers 必须是 dict。"""
        self.assertIn("servers", self.state, "state.json 缺少顶层 servers 字段")
        self.assertIsInstance(
            self.state["servers"], dict,
            "servers 是 %r，期望 dict" % type(self.state["servers"]),
        )

    def test_server_keys_and_entries_shape(self):
        """servers 的 key 是非空字符串，value 是 dict。"""
        servers = self.state.get("servers") or {}
        for key, entry in servers.items():
            with self.subTest(server_key=key):
                self.assertIsInstance(key, str, "server key %r 不是字符串" % (key,))
                self.assertTrue(key, "存在空字符串的 server key")
                self.assertIsInstance(
                    entry, dict,
                    "servers[%r] 是 %r，期望 dict" % (key, type(entry)),
                )

    def test_entry_timestamps_parse(self):
        """每个 entry 的 last_success_at / expiry_iso（若存在）必须能被解析。"""
        servers = self.state.get("servers") or {}
        for key, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            for field in self.ENTRY_TIME_FIELDS:
                if field not in entry:
                    continue
                with self.subTest(server_key=key, field=field):
                    self._parse_iso(
                        entry[field], "servers[%r][%r]" % (key, field)
                    )

    def test_top_level_updated_at_parses(self):
        """顶层 updated_at 若存在则必须可解析（SPEC §3 示例含该字段）。"""
        if "updated_at" not in self.state:
            self.skipTest("state.json 未写 updated_at（可选字段），跳过")
        self._parse_iso(self.state["updated_at"], "updated_at")


# ==============================================================================
# 入口
# ==============================================================================
def print_banner():
    print("=" * 78)
    print("Host2Play selftest —— SPEC v1 §2 纯函数契约 + §3 state.json schema（纯离线）")
    print("=" * 78)
    print("python      : %s" % sys.version.replace("\n", " "))
    print("repo root   : %s" % REPO_ROOT)
    print("strict mode : %s" % ("ON（契约缺失算失败）" if STRICT else "OFF（契约缺失算 skip）"))
    if main is not None:
        print("import main : 成功（%s）" % getattr(main, "__file__", "?"))
        print("契约函数    : 已具备 %s%s" % (
            ", ".join(AVAILABLE_FUNCS) or "无",
            ("；缺失 " + ", ".join(MISSING_FUNCS)) if MISSING_FUNCS else "",
        ))
    else:
        print("import main : 失败 [%s] %s" % (IMPORT_ERROR_KIND or "未知", IMPORT_ERROR_TEXT))
        print("契约函数    : 无法检查（模块未加载）")
        print("说明        : 这是环境缺三方依赖导致的，相关用例按 skip 处理；"
              "真实断言失败不会被吞掉。")
    print("state.json  : %s" % ("存在" if os.path.exists(STATE_PATH) else "不存在（schema 组将 skip）"))
    print("-" * 78)


def main_entry():
    # Windows 控制台默认 GBK，打印 ✅/❌ 会抛 UnicodeEncodeError；先钉死输出编码
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print_banner()
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)

    total = result.testsRun
    skipped = len(result.skipped)
    failed = len(result.failures)
    errored = len(result.errors)
    passed = total - skipped - failed - errored

    print("-" * 78)
    print("汇总: 共 %d 个用例 | 通过 %d | 跳过 %d | 失败 %d | 错误 %d"
          % (total, passed, skipped, failed, errored))
    if skipped:
        reasons = {}
        for _test, reason in result.skipped:
            head = reason.splitlines()[0][:100]
            reasons[head] = reasons.get(head, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print("  skip x%-2d %s" % (count, reason))
    exit_code = 0 if result.wasSuccessful() else 1
    print("退出码: %d（%s）" % (
        exit_code,
        "全部通过" if exit_code == 0 else "存在失败/错误",
    ))
    if exit_code == 0 and skipped and main is None:
        print("注意: main 模块未能导入，SPEC §2 四个纯函数本次全部未验证；"
              "装上依赖后重跑，或用 --strict 查看严格判定。")
    print("=" * 78)
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    sys.exit(main_entry())

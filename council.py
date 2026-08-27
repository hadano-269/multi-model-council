#!/usr/bin/env python3
"""council — 多模型研究议会编排器（stdlib-only，仅依赖 pyyaml）"""
import argparse
import concurrent.futures
import copy
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

from tools import TOOL_NAMES, ToolRunner, parse_tool_args, tool_detail

BASE = Path(__file__).resolve().parent
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}

def _enable_vt():
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        m = ctypes.c_ulong()
        if k.GetConsoleMode(h, ctypes.byref(m)):
            k.SetConsoleMode(h, m.value | 0x0004)
    except Exception:
        pass

_enable_vt()


class FatalSeatError(Exception):
    pass


class Cancelled(Exception):
    pass


class SeatSkipped(Exception):
    pass


class RunControl:
    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = False
        self._skip = set()
        self._extra = []

    def cancel(self):
        with self._lock:
            self._cancel = True

    def skip(self, seat_id):
        with self._lock:
            self._skip.add(seat_id)
            if seat_id == "moderator":
                self._skip.add("moderator_p5")
            elif seat_id == "moderator_p5":
                self._skip.add("moderator")

    def cancelled(self):
        with self._lock:
            return self._cancel

    def skipped(self, seat_id):
        with self._lock:
            return seat_id in self._skip

    def check(self, seat_id=None):
        with self._lock:
            if self._cancel:
                raise Cancelled("用户取消")
            if seat_id and seat_id in self._skip:
                raise SeatSkipped(seat_id)

    def inject(self, text):
        with self._lock:
            notes = getattr(self, "_extra", None)
            if notes is None:
                self._extra = []
                notes = self._extra
            notes.append(text)

    def drain_extra(self):
        with self._lock:
            notes = getattr(self, "_extra", None) or []
            self._extra = []
            return "\n\n".join(n.strip() for n in notes if n and str(n).strip())


class SeatMemory:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}

    def key(self, seat_id):
        return "moderator" if seat_id == "moderator_p5" else seat_id

    def has(self, seat_id):
        with self._lock:
            return bool(self._data.get(self.key(seat_id), {}).get("messages"))

    def snapshot(self, seat_id):
        with self._lock:
            rec = self._data.get(self.key(seat_id))
            if not rec:
                return []
            return copy.deepcopy(rec["messages"])

    def commit(self, seat_id, messages):
        with self._lock:
            self._data[self.key(seat_id)] = {"messages": copy.deepcopy(messages)}


def _flatten_history(messages):
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            parts.append(c)
        elif isinstance(c, list):
            texts = []
            for b in c:
                if isinstance(b, dict) and b.get("type") in ("text", "output_text"):
                    texts.append(b.get("text") or "")
            if texts:
                parts.append("".join(texts))
    return "\n\n".join(parts)


class RetryHub:
    def __init__(self, wait_sec=0):
        self.wait_sec = wait_sec
        self._cv = threading.Condition()
        self._tokens = {}

    @property
    def enabled(self):
        return self.wait_sec != 0

    def signal(self, seat_id):
        with self._cv:
            self._tokens[seat_id] = self._tokens.get(seat_id, 0) + 1
            self._cv.notify_all()

    def wait_token(self, seat_id, control=None):
        if self.wait_sec == 0:
            return False
        timeout = None if self.wait_sec < 0 else float(self.wait_sec)
        deadline = None if timeout is None else time.time() + timeout
        with self._cv:
            start = self._tokens.get(seat_id, 0)
            while self._tokens.get(seat_id, 0) <= start:
                if control is not None:
                    if control.cancelled():
                        raise Cancelled("用户取消")
                    if control.skipped(seat_id):
                        raise SeatSkipped(seat_id)
                remain = 0.4
                if deadline is not None:
                    remain = min(remain, deadline - time.time())
                    if remain <= 0:
                        return False
                self._cv.wait(remain)
            return True


# 预算超限不再单独设异常：以携带原因的 Cancelled 抛出，
# 复用三入口已有的取消级联（pack(incomplete)/GUI/MCP worker）全套处理路径。


def expand_env(val):
    if isinstance(val, str):
        def repl(m):
            var = m.group(1)
            default = m.group(2) or ""
            return os.environ.get(var, default)
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}", repl, val)
    if isinstance(val, dict):
        return {k: expand_env(v) for k, v in val.items()}
    if isinstance(val, list):
        return [expand_env(v) for v in val]
    return val


def load_config(path):
    return expand_env(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


# 疑似真实密钥的字面量（sk-/ark- 前缀的长串）；sk-ccswitch-local 这类短占位符不会命中
_SECRET_LIKE = re.compile(r"\b(?:sk|ark)-[A-Za-z0-9_-]{24,}")


def check_api_keys(cfg, config_path=None):
    """启动预检：返回问题列表（空列表=通过）。
    - 缺失：端点 api_key 展开后为空，需设置环境变量；
    - 明文：扫描 config_path 的原始文本（不是展开后的值），捕获误写进文件的密钥。"""
    problems = []
    for name, ep in (cfg.get("endpoints") or {}).items():
        if not str((ep or {}).get("api_key") or "").strip():
            problems.append(f"端点 {name} 的 api_key 为空：请 setx 对应环境变量后"
                            "重开终端（参见 README「首次配置」）。")
    if config_path:
        try:
            raw = Path(config_path).read_text(encoding="utf-8")
        except OSError:
            raw = ""
        for i, ln in enumerate(raw.splitlines(), 1):
            hit = _SECRET_LIKE.search(ln)
            if hit:
                problems.append(f"config 第 {i} 行疑似明文密钥（{hit.group(0)[:6]}***）："
                                "请改用 ${ENV:-} 环境变量写法，勿将真实 key 提交入库。")
    return problems


def guard_api_keys(args, cfg):
    """真实调用前的统一预检；--dry-run 等 mock 路径直接放行。"""
    if getattr(args, "dry_run", False):
        return
    problems = check_api_keys(cfg, getattr(args, "config", None))
    if problems:
        sys.exit("API Key 检查未通过：\n" + "\n".join(problems))


def build_toolkit(cfg, args):
    tools_cfg = cfg.get("tools") or {}
    if getattr(args, "no_tools", False) or not tools_cfg.get("enabled"):
        return None, {"enabled": False}
    raw = getattr(args, "workspace", None) or tools_cfg.get("workspace") or "."
    root = Path(raw)
    if not root.is_absolute():
        root = (BASE / root).resolve()
    else:
        root = root.resolve()
    allow = tools_cfg.get("allow") or list(TOOL_NAMES)
    runner = ToolRunner(root, allow=allow,
                        max_file_bytes=tools_cfg.get("max_file_bytes", 200000))
    if not runner.allow:
        return None, {"enabled": False}
    meta = {
        "enabled": True,
        "workspace": str(runner.root),
        "allow": sorted(runner.allow),
        "max_rounds": int(tools_cfg.get("max_rounds") or 8),
    }
    return runner, meta


def topic_title(topic, max_len=32):
    s = re.sub(r"\s+", " ", (topic or "").strip())
    for sep in ("\n", "。", "？", "！", "；", "，", ".", "?", "!", ";", ","):
        if sep in s:
            head = s.split(sep, 1)[0].strip()
            if len(head) >= 4:
                s = head
                break
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    s = s.strip(" ._-+")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" ._-+")
    return s or "议题"


def make_session_dir(out_root, topic, when=None):
    when = when or datetime.now()
    ts = when.strftime("%Y%m%d_%H%M%S")
    title = topic_title(topic)
    name = f"{ts}_{title}"
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / name
    n = 2
    while dest.exists():
        dest = root / f"{name}_{n}"
        n += 1
    dest.mkdir(parents=True)
    return dest, dest.name, ts, title


def write_text_retry(path, text, mode="w", retries=8):
    """落盘统一入口。

    mode="w" 走同目录临时文件 + os.replace 原子替换：同步盘（BaiduSyncdisk 等）
    只会看到完整的新旧版本，从源头消除「config_冲突文件_*」类半写副本；
    追加模式保持原语义直接 append（transcript 的高频小段追加场景）。
    """
    path = Path(path)
    if mode != "w":
        last = None
        for i in range(retries):
            try:
                with path.open(mode, encoding="utf-8") as f:
                    f.write(text)
                return
            except PermissionError as e:
                last = e
                time.sleep(0.12 * (i + 1))
        if last:
            raise last
        return
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    last = None
    for i in range(retries):
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)   # 同卷替换是原子的
            return
        except PermissionError as e:
            last = e
            try:
                tmp.unlink()
            except OSError:
                pass
            time.sleep(0.12 * (i + 1))
    if last:
        raise last


def extract_json(text):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("响应中未找到 JSON 对象")
    return json.loads(text[start:end + 1])


def parse_usage(data):
    if not isinstance(data, dict):
        return 0, 0
    u = data.get("usage") or {}
    if not isinstance(u, dict):
        return 0, 0
    inn = u.get("prompt_tokens", u.get("input_tokens", 0))
    out = u.get("completion_tokens", u.get("output_tokens", 0))
    if isinstance(inn, dict):
        inn = inn.get("total", inn.get("tokens", 0))
    if isinstance(out, dict):
        out = out.get("total", out.get("tokens", 0))
    try:
        return int(inn or 0), int(out or 0)
    except (TypeError, ValueError):
        return 0, 0


# 共享材料包限额：单项截断 / 总量封顶，防止主持人大量工具调用撑爆专家上下文
SHARED_ITEM_MAX = 6000
SHARED_TOTAL_MAX = 24000


def build_shared_block(pack):
    """共享材料包 → 注入文本；只转工具原文、不做加工，超限截断并标注。"""
    pack = pack or {}
    parts, used, dropped = [], 0, 0
    for item in pack.values():
        body = (item.get("body") or "").strip()
        cut = "…(后文截断)" if len(body) > SHARED_ITEM_MAX else ""
        chunk = f"[{item.get('label', '')}]\n{body[:SHARED_ITEM_MAX]}{cut}\n"
        if used + len(chunk) > SHARED_TOTAL_MAX:
            dropped += 1
            continue
        parts.append(chunk)
        used += len(chunk)
    if not parts:
        return ""
    tail = f"\n(另有 {dropped} 项未纳入；如需可自行调用工具查阅)\n" if dropped else ""
    return ("\n\n==== 主持调研共享材料（未经加工的工具原文，可直接引用） ====\n\n"
            + "\n".join(parts) + tail + "==== 共享材料结束 ====\n")


class Client:
    def __init__(self, endpoints, dry_run=False, toolkit=None, max_tool_rounds=8,
                 memory=None):
        self.endpoints = endpoints
        self.dry_run = dry_run
        self.toolkit = toolkit
        self.max_tool_rounds = max(1, int(max_tool_rounds or 8))
        self.memory = memory
        self.control = None
        # 主持人席工具读取的原文登记（target 去重、同目标留最新版本），供 P2 打包分发
        self._shared = {}
        self._shared_lock = threading.Lock()
        self._tls = threading.local()
        self._usage_lock = threading.Lock()
        self.usage = {}
        # 调用数护栏：None=不限制；由 run/run_review 从 --max-calls 注入
        self.max_calls = None
        self._total_calls = 0

    def enforce_budget(self):
        """会话级调用护栏：超限以携带原因的取消语义中止，零网络花费。"""
        limit = self.max_calls
        if not limit or self._total_calls < int(limit):
            return
        raise Cancelled(f"已达到 --max-calls={int(limit)} 调用上限，"
                        "会话中止以保护花费（可用 --experts 缩席或提高上限重跑）")

    def note_tool_output(self, name, args, result):
        """把工具输出原文登记进共享材料包；仅收主持人席，同一 target 覆盖旧版。"""
        sid = getattr(self._tls, "seat_id", None)
        if sid != "moderator" or not str(result or "").strip():
            return
        args = args if isinstance(args, dict) else {}
        if name == "read_file":
            key, label = f"文件:{args.get('path', '')}", f"read_file {args.get('path', '')}"
        elif name == "list_dir":
            key, label = f"目录:{args.get('path', '.')}", f"list_dir {args.get('path', '.')}"
        elif name == "grep":
            key = f"搜索:{args.get('pattern', '')}@{args.get('path', '.')}"
            label = f"grep \"{args.get('pattern', '')}\" ({args.get('path', '.')})"
        else:
            key = label = f"{name} {json.dumps(args, ensure_ascii=False)}"
        with self._shared_lock:
            self._shared[key] = {"label": label, "body": str(result)}

    def shared_pack(self):
        with self._shared_lock:
            return dict(self._shared)

    def usage_snapshot(self):
        with self._usage_lock:
            by = {k: dict(v) for k, v in self.usage.items()}
        tot = {"in": 0, "out": 0, "calls": 0}
        for v in by.values():
            tot["in"] += v.get("in", 0)
            tot["out"] += v.get("out", 0)
            tot["calls"] += v.get("calls", 0)
        return {"by_seat": by, "total": tot}

    def _record_usage(self, data):
        sid = getattr(self._tls, "seat_id", None)
        inn, out = parse_usage(data)
        with self._usage_lock:
            self._total_calls += 1   # 无条件计数：工具轮内的每次 HTTP 也占预算
        if not sid:
            return inn, out
        key = "moderator" if sid == "moderator_p5" else sid
        with self._usage_lock:
            rec = self.usage.setdefault(key, {"in": 0, "out": 0, "calls": 0})
            rec["in"] += inn
            rec["out"] += out
            rec["calls"] += 1
        return inn, out

    def _post(self, url, headers, payload, timeout):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "council/0.1")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            inn, out = self._record_usage(data)
            self._emit(getattr(self._tls, "on_event", None), "usage", inn, str(out))
            return data
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in RETRYABLE:
                raise ConnectionError(f"HTTP {e.code}: {detail}") from e
            raise FatalSeatError(f"HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ConnectionError(f"连接失败: {e}") from e

    def _endpoint_for(self, seat):
        ep = dict(self.endpoints.get(seat.get("endpoint")) or {})
        if seat.get("base_url"):
            ep["base_url"] = seat["base_url"]
        if seat.get("api_key"):
            ep["api_key"] = seat["api_key"]
        return ep

    def last_tool_trace(self):
        return list(getattr(self._tls, "trace", []) or [])

    def _emit(self, on_event, ev, att, detail):
        if not on_event:
            return
        try:
            on_event(ev, att, detail)
        except Exception:
            pass

    def _with_retry(self, fn, on_event=None):
        last_err = None
        for attempt in range(3):
            self._emit(on_event, "attempt", attempt + 1, None)
            try:
                return fn()
            except FatalSeatError:
                raise
            except Exception as e:
                last_err = e
                self._emit(on_event, "retry", attempt + 1, str(e)[:120])
                if attempt < 2:
                    time.sleep(2 * (2 ** attempt))
        raise ConnectionError(f"重试 3 次仍失败: {last_err}")

    def chat(self, seat_id, seat, system, user, on_event=None):
        ep = self._endpoint_for(seat)
        key = ep.get("api_key") or ""
        if not ep.get("base_url"):
            raise FatalSeatError(f"席位 {seat_id} 缺少 base_url（席位与端点均未配置）")
        if not key:
            raise FatalSeatError(
                f"席位 {seat_id} 缺少 api_key（检查环境变量或 config.yaml）"
            )
        proto = seat.get("protocol", "openai")
        timeout = seat.get("timeout", 300)
        max_tokens = seat.get("max_tokens", 16384)
        temperature = seat.get("temperature", 0.7)
        model = seat["model"]
        self._tls.trace = []
        self._tls.seat_id = seat_id
        self._tls.on_event = on_event
        if self.control:
            self.control.check(seat_id)
        self.enforce_budget()
        hist = self.memory.snapshot(seat_id) if self.memory else []
        use_tools = bool(self.toolkit and self.toolkit.allow
                         and proto in ("openai", "anthropic"))
        if use_tools:
            text, msgs = self._chat_with_tools(
                proto, ep, key, model, system, user, hist,
                max_tokens, temperature, timeout, on_event)
        else:
            text, msgs = self._with_retry(
                lambda: self._single(proto, ep, key, model, system, user, hist,
                                     max_tokens, temperature, timeout, on_event),
                on_event=on_event)
        if self.memory:
            self.memory.commit(seat_id, msgs)
        return text

    def _seed_messages(self, proto, system, user, hist):
        hist = list(hist or [])
        if proto == "anthropic":
            hist.append({"role": "user", "content": user})
            return hist
        if hist:
            if hist[0].get("role") == "system":
                hist[0] = {"role": "system", "content": system}
            else:
                hist = [{"role": "system", "content": system}] + hist
            hist.append({"role": "user", "content": user})
            return hist
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    def _single(self, proto, ep, key, model, system, user, hist,
                max_tokens, temperature, timeout, on_event):
        messages = self._seed_messages(proto, system, user, hist)
        if proto == "anthropic":
            text, _, _ = self._anthropic_turn(
                ep, key, model, system, messages,
                max_tokens, temperature, timeout, tools=None)
        elif proto == "responses":
            packed = _flatten_history(messages)
            text = self._responses(ep, key, model, system, packed,
                                   max_tokens, temperature, timeout)
        else:
            text, _, _ = self._openai_turn(
                ep, key, model, messages,
                max_tokens, temperature, timeout, tools=None)
        self._emit(on_event, "http", 1, None)
        messages.append({"role": "assistant", "content": text})
        return text, messages

    def _chat_with_tools(self, proto, ep, key, model, system, user, hist,
                         max_tokens, temperature, timeout, on_event):
        if proto == "anthropic":
            tools = self.toolkit.anthropic_schema()
        else:
            tools = self.toolkit.openai_schema()
        messages = self._seed_messages(proto, system, user, hist)
        for rnd in range(self.max_tool_rounds):
            def _turn():
                if proto == "anthropic":
                    return self._anthropic_turn(
                        ep, key, model, system, messages,
                        max_tokens, temperature, timeout, tools=tools)
                return self._openai_turn(
                    ep, key, model, messages,
                    max_tokens, temperature, timeout, tools=tools)
            text, calls, raw_msg = self._with_retry(_turn, on_event=on_event)
            self._emit(on_event, "http", rnd + 1, None)
            if not calls:
                if not (text or "").strip():
                    raise FatalSeatError("空响应且无工具调用")
                messages.append({"role": "assistant", "content": text})
                return text, messages
            if proto == "anthropic":
                messages.append({"role": "assistant", "content": raw_msg})
                results = []
                for c in calls:
                    args = c.get("input") if isinstance(c.get("input"), dict) else {}
                    result = self.toolkit.call(c.get("name"), args)
                    self.note_tool_output(c.get("name"), args, result)
                    self._tls.trace.append({
                        "name": c.get("name"), "args": args, "result": result[:800],
                    })
                    self._emit(on_event, "tool", rnd + 1,
                               tool_detail(c.get("name"), args))
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": c.get("id"),
                        "content": result,
                    })
                messages.append({"role": "user", "content": results})
            else:
                messages.append(raw_msg)
                for c in calls:
                    fn = (c.get("function") or {})
                    args = parse_tool_args(fn.get("arguments"))
                    name = fn.get("name") or c.get("name")
                    result = self.toolkit.call(name, args)
                    self.note_tool_output(name, args, result)
                    self._tls.trace.append({
                        "name": name, "args": args, "result": result[:800],
                    })
                    self._emit(on_event, "tool", rnd + 1, tool_detail(name, args))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": c.get("id") or f"call_{rnd}",
                        "content": result,
                    })
        raise FatalSeatError(
            f"工具轮次耗尽仍无最终文本（max_rounds={self.max_tool_rounds}）")

    def _openai_turn(self, ep, key, model, messages, max_tokens, temperature,
                     timeout, tools=None):
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        data = self._post(
            ep["base_url"].rstrip("/") + "/chat/completions",
            {"Authorization": f"Bearer {key}"},
            payload,
            timeout,
        )
        try:
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            calls = msg.get("tool_calls") or []
            if not str(content).strip() and not calls:
                reasoning = msg.get("reasoning_content") or ""
                raise FatalSeatError(
                    f"空响应 finish_reason={choice.get('finish_reason')} "
                    f"reasoning_len={len(reasoning)}（多为 max_tokens 被推理耗尽）"
                )
            return content, calls, msg
        except FatalSeatError:
            raise
        except (KeyError, IndexError, TypeError) as e:
            raise FatalSeatError(f"openai 响应结构异常: {json.dumps(data)[:400]}") from e

    def _responses(self, ep, key, model, system, user, max_tokens, temperature, timeout):
        data = self._post(
            ep["base_url"].rstrip("/") + "/responses",
            {"Authorization": f"Bearer {key}"},
            {
                "model": model,
                "instructions": system,
                "input": user,
                "max_output_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout,
        )
        try:
            text = data.get("output_text") or ""
            if not str(text).strip():
                chunks = []
                for item in data.get("output") or []:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "message":
                        continue
                    for c in item.get("content") or []:
                        if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                            chunks.append(c.get("text") or "")
                text = "".join(chunks)
            if not str(text).strip():
                kinds = [i.get("type") for i in (data.get("output") or [])
                         if isinstance(i, dict)]
                raise FatalSeatError(
                    f"空响应 status={data.get('status')} "
                    f"incomplete={data.get('incomplete_details')} "
                    f"output_types={kinds}（多为 max_tokens 被推理耗尽）"
                )
            return text
        except FatalSeatError:
            raise
        except (KeyError, TypeError) as e:
            raise FatalSeatError(f"responses 响应结构异常: {json.dumps(data)[:400]}") from e

    def _anthropic_turn(self, ep, key, model, system, messages, max_tokens,
                        temperature, timeout, tools=None):
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        data = self._post(
            ep["base_url"].rstrip("/") + "/messages",
            {"x-api-key": key, "Authorization": f"Bearer {key}",
             "anthropic-version": "2023-06-01"},
            payload,
            timeout,
        )
        try:
            blocks = [b for b in data.get("content", []) if isinstance(b, dict)]
            text = "".join(b.get("text", "") for b in blocks
                           if b.get("type") == "text")
            calls = [b for b in blocks if b.get("type") == "tool_use"]
            if not text.strip() and not calls:
                kinds = [b.get("type") for b in blocks]
                raise FatalSeatError(
                    f"空响应 stop_reason={data.get('stop_reason')} "
                    f"blocks={kinds}（多为 max_tokens 被推理耗尽）"
                )
            return text, calls, blocks
        except FatalSeatError:
            raise
        except (KeyError, TypeError) as e:
            raise FatalSeatError(f"anthropic 响应结构异常: {json.dumps(data)[:400]}") from e


MOCK_POSITION_STANCES = ["support", "oppose", "neutral"]


def mock_response(seat_id, phase, ctx):
    others = [s for s in ctx["experts"] if s != seat_id]
    if phase == "decompose":
        return json.dumps({"motions": [
            {"id": "M1", "title": "[mock] 核心路线选择",
             "description": "dry-run 占位论题：是否沿当前路线继续投入"},
            {"id": "M2", "title": "[mock] 资源分配",
             "description": "dry-run 占位论题：预算应投向哪个模块"},
        ]}, ensure_ascii=False)
    if phase == "position":
        stance = MOCK_POSITION_STANCES[hash(seat_id) % 3]
        return json.dumps({
            "positions": [
                {"motion_id": m["id"], "stance": stance,
                 "core_argument": f"[mock] {seat_id} 的论证要点", "confidence": 60}
                for m in ctx["motions"]
            ],
            "overall_take": f"[mock] {seat_id} 总体判断"},
            ensure_ascii=False)
    if phase == "cross_review":
        verdicts = ["agree", "disagree", "partial"]
        return json.dumps({
            "reviews": [
                {"target_seat": s, "verdict": verdicts[i % 3],
                 "reason": f"[mock] 对 {s} 的评审意见"}
                for i, s in enumerate(others)
            ],
            "revised_stance": None}, ensure_ascii=False)
    if phase == "disputes":
        pairs = [(ctx["experts"][i], ctx["experts"][i + 1])
                 for i in range(0, min(2, len(ctx["experts"]) - 1), 2)]
        return json.dumps({"disputes": [
            {"id": f"D{i + 1}", "motion_id": ctx["motions"][0]["id"],
             "between": list(p),
             "question": f"[mock] {p[0]} 与 {p[1]} 的分歧点是什么？"}
            for i, p in enumerate(pairs)
        ]}, ensure_ascii=False)
    if phase == "dispute_answer":
        return json.dumps({"answers": [
            {"dispute_id": d["id"], "answer": f"[mock] {seat_id} 的答辩",
             "final_stance": MOCK_POSITION_STANCES[hash(seat_id) % 3]}
            for d in ctx["disputes"]
        ]}, ensure_ascii=False)
    if phase == "verdict":
        return json.dumps({
            "consensus": ["[mock] 全员共识项"],
            "open_disputes": ["[mock] 未决分歧项"],
            "recommended_next_experiments": ["[mock] 推荐实验"],
            "rejected_routes": ["[mock] 否决路线"],
            "self_conflict_note": "[mock] 同源席位声明",
        }, ensure_ascii=False)
    if phase == "score":
        experts = ctx.get("experts") or []
        return json.dumps({
            "scores": {s: 8 for s in experts},
            "consensus": ["[mock] 共识"],
            "disputes": [],
            "insights": [f"[mock] {seat_id} 的创见"],
            "can_ship": False,
        }, ensure_ascii=False)
    raise ValueError(f"未知 phase: {phase}")


class Transcript:
    def __init__(self, path, header_lines, append=False):
        self.path = path
        self._lock = threading.Lock()
        if append and Path(path).exists():
            write_text_retry(self.path,
                             "\n## — 续跑 —\n\n"
                             + "\n".join(f"- {h}" for h in header_lines) + "\n",
                             mode="a")
        else:
            write_text_retry(self.path,
                             "# Council 会话记录\n\n"
                             + "\n".join(f"- {h}" for h in header_lines) + "\n")

    def section(self, title):
        with self._lock:
            write_text_retry(self.path, f"\n## {title}\n\n", mode="a")

    def entry(self, seat_id, status, raw):
        with self._lock:
            tag = status if status == "ok" else f"FAIL: {status}"
            write_text_retry(self.path, f"### {seat_id} [{tag}]\n\n```\n{raw}\n```\n\n",
                             mode="a")


class LiveProgress:
    def __init__(self, enabled=True, total_phases=5, on_update=None):
        force = os.environ.get("COUNCIL_FORCE_LIVE") == "1"
        is_tty = bool(sys.stdout and sys.stdout.isatty())
        self.on_update = on_update
        self.remote = on_update is not None
        if self.remote:
            enabled = True
            self.plain = False
        else:
            self.plain = not is_tty and force
        self.enabled = bool(enabled and ((is_tty or force) or self.remote)
                            and not os.environ.get("COUNCIL_NO_LIVE"))
        self.total_phases = total_phases
        self.lock = threading.Lock()
        self.phase = ""
        self.phase_idx = 0
        self.session_start = time.time()
        self.phase_start = time.time()
        self.seats = {}
        self.calls_done = 0
        self.max_calls = 80
        self.tokens = {}
        self.spinner_idx = 0
        self.render_lines = 0
        self.closed = False
        self.status_path = None
        self.status_extra = {}

    def set_phase(self, title, idx, total=None):
        if total is not None:
            self.total_phases = total
        with self.lock:
            if not self.remote and self.enabled and self.render_lines:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self.render_lines = 0
            self.phase = title
            self.phase_idx = idx
            self.phase_start = time.time()
            self.seats = {}
            self.spinner_idx = 0
            self._emit_locked(time.time())

    def start_seats(self, seat_ids):
        with self.lock:
            now = time.time()
            for sid in seat_ids:
                self.seats[sid] = {"status": "running", "start": now, "elapsed": 0,
                                   "attempt": 1, "detail": "等待响应",
                                   "tok_in": 0, "tok_out": 0}

    def start_seat(self, seat_id):
        self.start_seats([seat_id])

    def update_seat(self, seat_id, status=None, detail=None, attempt=None):
        with self.lock:
            if seat_id in self.seats:
                if status is not None:
                    self.seats[seat_id]["status"] = status
                if detail is not None:
                    self.seats[seat_id]["detail"] = detail
                if attempt is not None:
                    self.seats[seat_id]["attempt"] = attempt
                if self.remote:
                    self._emit_locked(time.time())

    def note_usage(self, seat_id, inn, out):
        key = "moderator" if seat_id == "moderator_p5" else seat_id
        with self.lock:
            rec = self.tokens.setdefault(key, {"in": 0, "out": 0, "calls": 0})
            rec["in"] += int(inn or 0)
            rec["out"] += int(out or 0)
            rec["calls"] += 1
            if seat_id in self.seats:
                self.seats[seat_id]["tok_in"] = rec["in"]
                self.seats[seat_id]["tok_out"] = rec["out"]
            if key != seat_id and key in self.seats:
                self.seats[key]["tok_in"] = rec["in"]
                self.seats[key]["tok_out"] = rec["out"]

    def note_call(self, seat_id=None):
        with self.lock:
            self.calls_done += 1
            if seat_id and seat_id in self.seats:
                self.seats[seat_id]["http_n"] = self.seats[seat_id].get("http_n", 0) + 1

    def finish_seat(self, seat_id, ok, detail=""):
        with self.lock:
            if seat_id in self.seats:
                info = self.seats[seat_id]
                info["status"] = "done" if ok else "fail"
                info["detail"] = detail[:70] if detail else ("完成" if ok else "失败")
                info["elapsed"] = time.time() - info["start"]
                if not info.get("http_n"):
                    self.calls_done += 1

    def _snapshot_locked(self, now):
        return {
            "phase_idx": self.phase_idx,
            "phase_total": self.total_phases,
            "phase": self.phase,
            "session_elapsed": round(now - self.session_start, 1),
            "calls_done": self.calls_done,
            "max_calls": self.max_calls,
            "tokens": {k: dict(v) for k, v in self.tokens.items()},
            "seats": {sid: dict(info) for sid, info in self.seats.items()},
        }

    def _write_status_locked(self, now):
        if not self.status_path:
            return
        snap = dict(self.status_extra or {})
        snap.update(self._snapshot_locked(now))
        try:
            path = Path(self.status_path)
            body = json.dumps(snap, ensure_ascii=False, default=str, indent=2)
            write_text_retry(path, body)
        except Exception:
            pass

    def write_status(self):
        with self.lock:
            self._write_status_locked(time.time())

    def _emit_locked(self, now):
        if self.remote:
            try:
                self.on_update(self._snapshot_locked(now))
            except Exception:
                pass
        self._write_status_locked(now)

    def tick(self):
        if self.closed:
            return
        if not self.enabled and not self.status_path:
            return
        with self.lock:
            now = time.time()
            for info in self.seats.values():
                if info["status"] in ("running", "retrying", "await_retry"):
                    info["elapsed"] = now - info["start"]
            self.spinner_idx = (self.spinner_idx + 1) % 4
            if self.enabled:
                if self.remote:
                    self._emit_locked(now)
                    return
                self._render_locked(now)
            self._write_status_locked(now)

    def _render_locked(self, now):
        spinner = ["|", "/", "-", "\\"][self.spinner_idx]
        elapsed_total = int(now - self.session_start)
        phase_elapsed = int(now - self.phase_start)
        done = sum(1 for s in self.seats.values() if s["status"] in ("done", "fail"))
        total = len(self.seats)
        if total == 0:
            est_str = "0s"
        elif done == total:
            est_str = "0s"
        elif done == 0:
            est_str = "?"
        else:
            avg = sum(s.get("elapsed", 0) for s in self.seats.values() if s["status"] in ("done", "fail")) / max(done, 1)
            est = int(avg * (total - done))
            max_running = max((s.get("elapsed", 0) for s in self.seats.values() if s["status"] in ("running", "retrying")), default=0)
            est = max(est, int(max_running))
            est_str = f"{est}s"
        header = f"[{self.phase_idx}/{self.total_phases}] {self.phase} {spinner} 已用 {phase_elapsed}s | 会话 {elapsed_total}s | 预计剩余 ~{est_str} | 调用 {self.calls_done}/{self.max_calls}  {done}/{total} 完成"
        lines = [header]
        for sid in sorted(self.seats):
            info = self.seats[sid]
            elapsed = int(info.get("elapsed", 0))
            detail = info.get("detail", "")
            status = info["status"]
            if status == "running":
                icon = spinner
                lines.append(f"  {sid:<16} {icon} {elapsed:>3}s  {detail}")
            elif status == "retrying":
                att = info.get("attempt", 1)
                lines.append(f"  {sid:<16} >> {elapsed:>3}s  重试 {att}/3 {detail[:50]}")
            elif status == "done":
                lines.append(f"  {sid:<16} OK {elapsed:>3}s  {detail[:60]}")
            elif status == "fail":
                lines.append(f"  {sid:<16} XX {elapsed:>3}s  {detail[:60]}")
            else:
                lines.append(f"  {sid:<16} .. {elapsed:>3}s  {detail}")
        if self.plain:
            for line in lines:
                sys.stdout.write(line + "\n")
            sys.stdout.flush()
            self.render_lines = 0
            return
        if self.render_lines:
            sys.stdout.write(f"\x1b[{self.render_lines}A")
            for i in range(self.render_lines):
                sys.stdout.write("\x1b[2K")
                if i < self.render_lines - 1:
                    sys.stdout.write("\n")
                else:
                    sys.stdout.write("\r")
        for i, line in enumerate(lines):
            sys.stdout.write("\x1b[2K" + line)
            if i < len(lines) - 1:
                sys.stdout.write("\n")
            else:
                sys.stdout.write("\r")
        sys.stdout.flush()
        self.render_lines = len(lines)

    def close_phase(self):
        if not self.enabled:
            return
        with self.lock:
            if not self.remote and self.render_lines:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self.render_lines = 0
            self._emit_locked(time.time())

    def close(self):
        with self.lock:
            if not self.remote and self.enabled and self.render_lines:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self.render_lines = 0
            self._emit_locked(time.time())
            self.closed = True


def _ask_json_once(client, seat_id, seat, phase, system, user, ctx, log, on_event=None):
    hint = ""
    if client.toolkit and client.toolkit.allow:
        known = bool((ctx or {}).get("shared_material")) or (
            client.memory and client.memory.has(seat_id))
        lead = ("如需核实细节或补充查阅其他资料，可调用工作区工具；"
                "查完后最终必须只输出一个合法 JSON 对象。\n"
                if known else
                "你可以先调用工具查阅工作区文件；"
                "查完后最终必须只输出一个合法 JSON 对象。\n")
        hint = "\n" + lead
        if client.memory and client.memory.has(seat_id):
            hint += "你已保留此前的对话与工具结果，请勿重复查阅已读过的同一文件。\n"
    first = user + hint
    last_raw, last_err = "", None
    for idx, prompt in enumerate((
        first,
        "上一轮输出不是合法 JSON。请只输出一个合法 JSON 对象，不要任何其他文字。",
    )):
        def _cb(ev, att, detail, _idx=idx):
            if not on_event:
                return
            if ev in ("retry", "tool", "http"):
                on_event(ev, att, detail)
            elif ev == "attempt" and _idx == 1 and att == 1:
                on_event("retry", 1, "JSON 重试")
        raw = client.chat(seat_id, seat, system, prompt, on_event=_cb)
        last_raw = raw
        trace = client.last_tool_trace()
        if trace and log:
            lines = []
            for t in trace:
                lines.append(
                    f"{t.get('name')} {json.dumps(t.get('args') or {}, ensure_ascii=False)}"
                    f"\n{(t.get('result') or '')[:400]}")
            log.entry(f"{seat_id}/tools", "ok", "\n\n".join(lines))
        try:
            return extract_json(raw), raw
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            if idx == 0:
                continue
    raise FatalSeatError(f"JSON 解析失败: {last_err}; 原文: {last_raw[:300]}")


def _with_extra(client, user):
    ctrl = getattr(client, "control", None)
    extra = ctrl.drain_extra() if ctrl else ""
    if extra:
        return user + "\n\n【追加需求/说明】\n" + extra
    return user


def ask_json(client, seat_id, seat, phase, system, user, ctx, log, on_event=None):
    ctrl = getattr(client, "control", None)
    if ctrl:
        ctrl.check(seat_id)
    user = _with_extra(client, user)
    if client.dry_run:
        raw = mock_response(seat_id, phase, ctx)
        if on_event:
            time.sleep(0.6)
        if ctrl:
            ctrl.check(seat_id)
        return extract_json(raw), raw
    while True:
        if ctrl:
            ctrl.check(seat_id)
        try:
            return _ask_json_once(
                client, seat_id, seat, phase, system, user, ctx, log, on_event)
        except (Cancelled, SeatSkipped):
            raise
        except Exception as e:
            hub = getattr(client, "retry_hub", None)
            if not hub or not hub.enabled:
                raise
            if on_event:
                try:
                    on_event("await_retry", 0, str(e)[:120])
                except Exception:
                    pass
            if not hub.wait_token(seat_id, control=ctrl):
                raise
            if on_event:
                try:
                    on_event("retry", 1, "手动重试")
                except Exception:
                    pass


def ask_text(client, seat_id, seat, phase, system, user, ctx, log, on_event=None):
    ctrl = getattr(client, "control", None)
    if ctrl:
        ctrl.check(seat_id)
    user = _with_extra(client, user)
    if client.dry_run:
        raw = (ctx or {}).get("mock_text") or f"[mock:{phase}] {seat_id} 占位正文\n"
        if on_event:
            time.sleep(0.4)
        if ctrl:
            ctrl.check(seat_id)
        if log:
            log.entry(seat_id, "ok", raw)
        return raw
    hint = ""
    if client.toolkit and client.toolkit.allow:
        known = bool((ctx or {}).get("shared_material")) or (
            client.memory and client.memory.has(seat_id))
        hint = ("\n如需核实细节或补充查阅其他资料，可调用工作区工具，然后输出 Markdown 正文。\n"
                if known else
                "\n你可以先调用工具查阅工作区文件，然后输出 Markdown 正文。\n")
        if client.memory and client.memory.has(seat_id):
            hint += "你已保留此前的对话与工具结果，请勿重复查阅已读过的同一文件。\n"
    while True:
        if ctrl:
            ctrl.check(seat_id)
        try:
            raw = client.chat(seat_id, seat, system, user + hint, on_event=on_event)
            if log:
                log.entry(seat_id, "ok", raw)
            return raw
        except (Cancelled, SeatSkipped):
            raise
        except Exception as e:
            hub = getattr(client, "retry_hub", None)
            if not hub or not hub.enabled:
                raise
            if on_event:
                try:
                    on_event("await_retry", 0, str(e)[:120])
                except Exception:
                    pass
            if not hub.wait_token(seat_id, control=ctrl):
                raise
            if on_event:
                try:
                    on_event("retry", 1, "手动重试")
                except Exception:
                    pass


def parallel_tasks(fn, items):
    results = {}
    if not items:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(items)) as ex:
        futs = {ex.submit(fn, item): item for item in items}
        for fut in concurrent.futures.as_completed(futs):
            try:
                results[futs[fut]] = ("ok", fut.result())
            except Exception as e:
                results[futs[fut]] = ("err", str(e))
    return results


def parallel_tasks_live(fn, items, progress):
    results = {}
    if not items:
        return results

    def _wrapped(item):
        def _on_event(ev, att, detail):
            if ev == "retry":
                progress.update_seat(item, status="retrying", detail=detail or "", attempt=att)
            elif ev == "attempt":
                if att > 1:
                    progress.update_seat(item, status="retrying", attempt=att)
            elif ev == "tool":
                progress.update_seat(item, status="running", detail=detail or "")
            elif ev == "http":
                progress.note_call(item)
            elif ev == "usage":
                try:
                    progress.note_usage(item, att, int(detail or 0))
                except (TypeError, ValueError):
                    pass
            elif ev == "await_retry":
                progress.update_seat(item, status="await_retry",
                                     detail=detail or "失败，可点重试")

        try:
            res = fn(item, _on_event)
            progress.finish_seat(item, True, "完成")
            return res
        except SeatSkipped:
            progress.finish_seat(item, False, "已跳过")
            raise
        except Cancelled:
            progress.finish_seat(item, False, "已取消")
            raise
        except Exception as e:
            msg = str(e)[:80]
            progress.finish_seat(item, False, msg)
            raise

    progress.start_seats(items)
    progress.tick()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(items)) as ex:
        futs = {ex.submit(_wrapped, it): it for it in items}
        pending = set(futs.keys())
        while pending:
            done, pending = concurrent.futures.wait(pending, timeout=0.5, return_when=concurrent.futures.FIRST_COMPLETED)
            for fut in done:
                sid = futs[fut]
                try:
                    results[sid] = ("ok", fut.result())
                except Exception as e:
                    results[sid] = ("err", str(e))
            progress.tick()
        progress.tick()
    progress.close_phase()
    return results


def run_single_live(seat_id, fn, progress, title):
    progress.start_seats([seat_id])

    def _on_event(ev, att, detail):
        if ev == "retry":
            progress.update_seat(seat_id, status="retrying", detail=detail or "", attempt=att)
        elif ev == "tool":
            progress.update_seat(seat_id, status="running", detail=detail or "")
        elif ev == "http":
            progress.note_call(seat_id)
        elif ev == "usage":
            try:
                progress.note_usage(seat_id, att, int(detail or 0))
            except (TypeError, ValueError):
                pass
        elif ev == "await_retry":
            progress.update_seat(seat_id, status="await_retry",
                                 detail=detail or "失败，可点重试")

    result_holder = {}
    err_holder = {}

    def _run():
        try:
            result_holder["res"] = fn(_on_event)
            progress.finish_seat(seat_id, True, "完成")
        except SeatSkipped as e:
            err_holder["err"] = e
            progress.finish_seat(seat_id, False, "已跳过")
        except Cancelled as e:
            err_holder["err"] = e
            progress.finish_seat(seat_id, False, "已取消")
        except Exception as e:
            err_holder["err"] = e
            progress.finish_seat(seat_id, False, str(e)[:80])

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    while t.is_alive():
        progress.tick()
        time.sleep(0.5)
    progress.tick()
    t.join()
    progress.close_phase()
    if "err" in err_holder:
        raise err_holder["err"]
    return result_holder["res"]


def write_checkpoint(session_dir, payload):
    Path(session_dir).mkdir(parents=True, exist_ok=True)
    write_text_retry(Path(session_dir) / "checkpoint.json",
                     json.dumps(payload, ensure_ascii=False, indent=2))


def load_checkpoint(path):
    p = Path(path)
    if p.is_dir():
        session_dir = p
        ck_file = p / "checkpoint.json"
    else:
        ck_file = p
        session_dir = p.parent
    if not ck_file.exists():
        raise FileNotFoundError(f"找不到 checkpoint: {ck_file}")
    return json.loads(ck_file.read_text(encoding="utf-8")), session_dir


def ping_seat(cfg, seat_id, timeout=45):
    if seat_id not in (cfg.get("seats") or {}):
        return False, f"席位不存在: {seat_id}", 0.0
    client = Client(cfg.get("endpoints") or {})
    seat = dict(cfg["seats"][seat_id])
    seat["timeout"] = min(int(seat.get("timeout") or timeout), timeout)
    t0 = time.time()
    try:
        reply = client.chat(seat_id, seat, "你是连通性测试器。",
                            "收到请只回复两个字符: OK")
        return True, (reply or "").strip()[:80], round(time.time() - t0, 2)
    except Exception as e:
        return False, str(e)[:160], round(time.time() - t0, 2)


def run(args, cfg=None, progress=None):
    if (getattr(args, "mode", None) or "debate") == "review":
        import review as review_mod
        return review_mod.run_review(args, cfg, progress)
    cfg = cfg if cfg is not None else load_config(args.config)
    guard_api_keys(args, cfg)
    seats_cfg = cfg["seats"]
    endpoints = cfg.get("endpoints") or {}

    selected = args.experts.split(",") if args.experts else []
    experts = []
    for sid, seat in seats_cfg.items():
        if sid == "moderator" or seat.get("role") == "moderator":
            continue
        if selected and sid not in selected:
            continue
        experts.append(sid)
    missing = [s for s in selected if s not in seats_cfg]
    if missing:
        sys.exit(f"config 中不存在席位: {missing}，可选: {[s for s in seats_cfg]}")

    resume_path = getattr(args, "resume", None)
    ck = {}
    start_phase = 1
    motions, positions, reviews = {}, {}, {}
    disputes, answers = [], {}
    if resume_path:
        ck, session_dir = load_checkpoint(resume_path)
        start_phase = int(ck.get("next_phase") or 1)
        topic = (getattr(args, "topic", None) or ck.get("topic") or "").strip()
        if not topic:
            sys.exit("续跑缺少议题（checkpoint 与命令行都没有 topic）")
        background = ck.get("background") or ""
        if args.file:
            background = Path(args.file).read_text(encoding="utf-8")
        if ck.get("experts"):
            experts = [s for s in ck["experts"] if s in seats_cfg]
        motions = ck.get("motions") or {}
        if isinstance(motions, list):
            motions = {"motions": motions}
        positions = ck.get("positions") or {}
        reviews = ck.get("reviews") or {}
        disputes = ck.get("disputes") or []
        answers = ck.get("answers") or {}
        session_id = session_dir.name
        ts = ck.get("time") or datetime.now().strftime("%Y%m%d_%H%M%S")
        session_title = ck.get("session_title") or topic_title(topic)
        mode = "DRY-RUN" if args.dry_run else "LIVE"
    else:
        topic = (args.topic or "").strip()
        if not topic:
            sys.exit("缺少议题。")
        background = ""
        if args.file:
            background = Path(args.file).read_text(encoding="utf-8")
        mode = "DRY-RUN" if args.dry_run else "LIVE"
        preset = getattr(args, "session_dir", None)
        if preset:
            session_dir = Path(preset)
            session_dir.mkdir(parents=True, exist_ok=True)
            session_id = session_dir.name
            matched = re.match(r"(\d{8}_\d{6})", session_id)
            ts = matched.group(1) if matched else datetime.now().strftime("%Y%m%d_%H%M%S")
            session_title = topic_title(topic)
        else:
            session_dir, session_id, ts, session_title = make_session_dir(BASE / "out", topic)

    toolkit, tools_meta = build_toolkit(cfg, args)
    client = Client(endpoints, dry_run=args.dry_run, toolkit=toolkit,
                    max_tool_rounds=(cfg.get("tools") or {}).get("max_rounds", 8),
                    memory=SeatMemory())
    client.retry_hub = getattr(args, "retry_hub", None)
    client.max_calls = getattr(args, "max_calls", None) or None
    control = getattr(args, "control", None) or RunControl()
    client.control = control
    if resume_path and ck.get("tokens", {}).get("by_seat"):
        client.usage = {k: dict(v) for k, v in ck["tokens"]["by_seat"].items()}

    def dump(name, obj):
        p = session_dir / name
        if isinstance(obj, str):
            write_text_retry(p, obj)
        else:
            write_text_retry(p, json.dumps(obj, ensure_ascii=False, indent=2))

    tr = Transcript(session_dir / "transcript.md", [
        f"时间: {ts}",
        f"模式: {mode}",
        f"议题: {topic}",
        f"背景材料: {args.file or '无'}",
        f"主持人: moderator ({seats_cfg['moderator'].get('model', '?')}"
        f"@{seats_cfg['moderator'].get('endpoint', 'custom')})",
        f"专家席: {', '.join(experts) or '无'}",
        f"工具: {tools_meta}",
    ], append=bool(resume_path))

    bg_block = f"\n背景材料:\n{background}\n" if background else "\n(无附加背景)\n"

    print(f"[council] 会话目录: {session_dir}")
    print(f"[council] 模式: {mode} | 专家: {experts}")
    print(f"[council] 工具: {tools_meta}")

    t0 = time.time()

    def mod_seat():
        return seats_cfg["moderator"]

    def expert_seat(sid):
        return seats_cfg[sid]

    force_live = os.environ.get("COUNCIL_FORCE_LIVE") == "1"
    if progress is None:
        live_enabled = not getattr(args, "quiet", False) and not getattr(args, "no_live", False)
        if live_enabled and not (sys.stdout and sys.stdout.isatty()) and not force_live:
            live_enabled = False
        progress = LiveProgress(enabled=live_enabled, total_phases=5)
    progress.max_calls = getattr(args, "max_calls", 80)
    progress.session_start = t0
    progress.status_extra = {
        "session": session_id,
        "topic": topic,
        "mode": mode,
        "state": "running",
        "experts": experts,
        "out_dir": str(session_dir),
    }
    progress.status_path = session_dir / "status.json"
    progress.write_status()
    live_enabled = progress.enabled

    sys_p = seats_cfg["moderator"]["persona"]
    current_phase = start_phase
    verdict = {}

    def save_ck(nxt):
        nonlocal current_phase
        current_phase = nxt
        write_checkpoint(session_dir, {
            "next_phase": nxt,
            "topic": topic,
            "background": background,
            "experts": experts,
            "motions": motions,
            "positions": positions,
            "reviews": reviews,
            "disputes": disputes,
            "answers": answers,
            "time": ts,
            "session_title": session_title,
            "tokens": client.usage_snapshot(),
        })

    def _pack_result(incomplete=False, err=""):
        result = {
            "meta": {
                "session": session_id,
                "session_title": session_title,
                "time": ts,
                "mode": mode,
                "topic": topic,
                "background_file": args.file,
                "seats": {"moderator": "moderator", "experts": experts},
                "elapsed_sec": round(time.time() - t0, 1),
                "tools": tools_meta,
                "incomplete": incomplete,
                "error": err,
                "tokens": client.usage_snapshot(),
            },
            "motions": motions.get("motions", []) if isinstance(motions, dict) else motions,
            "positions": positions,
            "reviews": reviews,
            "disputes": disputes,
            "answers": answers,
            "verdict": verdict if isinstance(verdict, dict) else {},
        }
        outp = session_dir / "verdict.json"
        write_text_retry(outp, json.dumps(result, ensure_ascii=False, indent=2))
        if progress is not None:
            extra = dict(progress.status_extra or {})
            extra["state"] = "incomplete" if incomplete else "done"
            extra["error"] = err
            extra["elapsed_sec"] = result["meta"]["elapsed_sec"]
            progress.status_extra = extra
            progress.write_status()
        print(f"\n[council] {'未完成' if incomplete else '完成'}，用时 {result['meta']['elapsed_sec']}s")
        print(f"[council] 裁决: {outp}")
        print(f"[council] 记录: {session_dir / 'transcript.md'}")
        return result

    if start_phase <= 1:
        control.check()
        tr.section("P1 论题拆解（moderator）")
        if live_enabled:
            progress.set_phase("P1 论题拆解", 1)
            print(f"[P1] 论题拆解 ...  (live)")
        else:
            print("\n[P1] 论题拆解 ...")
        usr_p = (f"[PHASE:decompose]\n议会主题:\n{topic}\n{bg_block}"
                 "\n请把主题拆解为 2-4 个值得激辩的关键论题。只输出 JSON：\n"
                 '{"motions": [{"id": "M1", "title": "...", "description": "..."}]}')
        ctx = {"experts": experts}
        try:
            if live_enabled:
                def _p1(cb):
                    return ask_json(client, "moderator", mod_seat(), "decompose", sys_p, usr_p, ctx, tr, on_event=cb)
                motions, raw = run_single_live("moderator", _p1, progress, "P1")
            else:
                motions, raw = ask_json(client, "moderator", mod_seat(), "decompose", sys_p, usr_p, ctx, tr)
        except (Cancelled, SeatSkipped):
            save_ck(1)
            if live_enabled:
                progress.close()
            return _pack_result(True, "P1 取消或跳过")
        except Exception:
            save_ck(1)
            if live_enabled:
                progress.close()
            raise
        dump("p1_motions.json", motions)
        tr.entry("moderator", "ok", raw)
        if not live_enabled:
            print(f"  -> {len(motions.get('motions', []))} 个论题")
        save_ck(2)
    else:
        print("[P1] 跳过（checkpoint）")

    def _make_position_job(sid, on_event=None):
        shared = build_shared_block(client.shared_pack())
        u = (f"[PHASE:position]\n主题:\n{topic}\n{bg_block}" + shared +
             f"\n论题列表:\n{json.dumps(motions, ensure_ascii=False)}\n"
             "\n请以你的独立视角对每个论题表态。只输出 JSON：\n"
             '{"positions": [{"motion_id": "M1", "stance": "support|oppose|neutral",'
             ' "core_argument": "...", "confidence": 0-100}],'
             ' "overall_take": "一句话总判断"}')
        ctx = {"experts": experts, "motions": motions.get("motions", [])}
        if shared:
            ctx["shared_material"] = True
        return ask_json(client, sid, expert_seat(sid), "position",
                        expert_seat(sid)["persona"], u,
                        ctx, tr, on_event=on_event)

    if start_phase <= 2:
        control.check()
        tr.section("P2 独立表态")
        need = [s for s in experts if s not in positions]
        if live_enabled:
            progress.set_phase("P2 独立表态", 2)
            print(f"[P2] 独立表态 ... {len(need)} 席并行")
            def _pos_live(sid, cb):
                return _make_position_job(sid, on_event=cb)
            pos_res = parallel_tasks_live(_pos_live, need, progress)
        else:
            print("\n[P2] 独立表态（并行）...")
            def position_job(sid):
                return _make_position_job(sid)
            pos_res = parallel_tasks(position_job, need)
        if control.cancelled():
            save_ck(2)
            if live_enabled:
                progress.close()
            return _pack_result(True, "用户取消")
        for sid, (status, payload) in pos_res.items():
            raw = payload[1] if status == "ok" else str(payload)
            tr.entry(sid, status, raw)
            if status == "ok":
                positions[sid] = payload[0]
                dump(f"p2_position_{sid}.json", payload[0])
                if not live_enabled:
                    print(f"  -> {sid} ok")
            else:
                if not live_enabled:
                    print(f"  -> {sid} 失败(缺席): {payload}")
        if live_enabled:
            print(f"[P2] 完成 {sum(1 for s in pos_res.values() if s[0]=='ok')}/{len(need)}")
        if not positions:
            save_ck(2)
            if live_enabled:
                progress.close()
            return _pack_result(True, "所有专家席均失败")
        save_ck(3)
    else:
        print("[P2] 跳过（checkpoint）")

    def _make_review_job(sid, on_event=None):
        peers = {k: v for k, v in positions.items() if k != sid}
        u = (f"[PHASE:cross_review]\n主题:\n{topic}\n{bg_block}"
             f"\n论题:\n{json.dumps(motions, ensure_ascii=False)}"
             f"\n\n其他议员立场:\n{json.dumps(peers, ensure_ascii=False)}\n"
             "\n请逐一点评每位议员的观点（agree/disagree/partial+具体理由），"
             "并说明是否修正自身立场。只输出 JSON：\n"
             '{"reviews": [{"target_seat": "...", "verdict": "agree|disagree|partial",'
             ' "reason": "..."}], "revised_stance": "若修正写新立场及理由，否则 null"}')
        return ask_json(client, sid, expert_seat(sid), "cross_review",
                        expert_seat(sid)["persona"], u,
                        {"experts": experts}, tr, on_event=on_event)

    if start_phase <= 3:
        control.check()
        tr.section("P3 交叉评审")
        need_r = [s for s in positions if s not in reviews]
        if live_enabled:
            progress.set_phase("P3 交叉评审", 3)
            print(f"[P3] 交叉评审 ... {len(need_r)} 席并行")
            def _rev_live(sid, cb):
                return _make_review_job(sid, on_event=cb)
            rev_res = parallel_tasks_live(_rev_live, need_r, progress)
        else:
            print("\n[P3] 交叉评审（并行）...")
            def review_job(sid):
                return _make_review_job(sid)
            rev_res = parallel_tasks(review_job, need_r)
        if control.cancelled():
            save_ck(3)
            if live_enabled:
                progress.close()
            return _pack_result(True, "用户取消")
        for sid, (status, payload) in rev_res.items():
            raw = payload[1] if status == "ok" else str(payload)
            tr.entry(sid, status, raw)
            if status == "ok":
                reviews[sid] = payload[0]
                dump(f"p3_review_{sid}.json", payload[0])
        if live_enabled:
            print(f"[P3] 完成 {sum(1 for s in rev_res.values() if s[0]=='ok')}/{len(rev_res)}")
        save_ck(4)
    else:
        print("[P3] 跳过（checkpoint）")

    mat = (f"论题:\n{json.dumps(motions, ensure_ascii=False)}"
           f"\n\n各议员立场:\n{json.dumps(positions, ensure_ascii=False)}"
           f"\n\n交叉评审:\n{json.dumps(reviews, ensure_ascii=False)}")
    if start_phase <= 4:
        control.check()
        tr.section("P4 分歧定向追问")
        if live_enabled:
            progress.set_phase("P4 分歧识别", 4)
            print("[P4] 分歧识别 ...")
        else:
            print("\n[P4] 分歧识别与定向追问 ...")
        disputes = []
        answers = {}
        u = ("[PHASE:disputes]\n以下是全部辩论材料。\n" + mat +
         "\n\n请找出真实存在且影响结论的分歧（最多 3 个，没有则空数组）。只输出 JSON：\n"
         '{"disputes": [{"id": "D1", "motion_id": "M1", '
         '"between": ["seat_a", "seat_b"], "question": "针对该分歧的追问"}]}')
        try:
            if live_enabled:
                def _dis(cb):
                    return ask_json(client, "moderator", mod_seat(), "disputes", sys_p, u,
                                    {"experts": experts, "motions": motions.get("motions", [])}, tr, on_event=cb)
                dj, raw = run_single_live("moderator", _dis, progress, "P4a")
            else:
                dj, raw = ask_json(client, "moderator", mod_seat(), "disputes", sys_p, u,
                                   {"experts": experts, "motions": motions.get("motions", [])}, tr)
            dump("p4_disputes.json", dj)
            tr.entry("moderator", "ok", raw)
            disputes = dj.get("disputes", [])
            print(f"[P4] 识别到 {len(disputes)} 个分歧")
        except (Cancelled, SeatSkipped) as e:
            save_ck(4)
            if live_enabled:
                progress.close()
            return _pack_result(True, str(e))
        except Exception as e:
            tr.entry("moderator", f"FAIL: {e}", "")
            if live_enabled:
                progress.close_phase()
            print(f"[P4] 分歧识别失败: {e}")

        involved = {}
        for d in disputes:
            for s in d.get("between", []):
                if s in positions:
                    involved.setdefault(s, []).append(d)

        if involved and live_enabled:
            progress.set_phase("P4b 定向追问", 4)
            print(f"[P4b] 定向追问 ... {len(involved)} 席并行")
            def _ans_live(sid, cb):
                my_disputes = involved[sid]
                u2 = ("[PHASE:dispute_answer]\n你是被追问方。以下是与您相关的分歧追问：\n"
                      + json.dumps(my_disputes, ensure_ascii=False)
                      + "\n\n完整辩论材料:\n" + mat
                      + "\n\n请正面回答每个追问并给出最终立场。只输出 JSON：\n"
                      '{"answers": [{"dispute_id": "D1", "answer": "...", '
                      '"final_stance": "support|oppose|neutral"}]}')
                return ask_json(client, sid, expert_seat(sid), "dispute_answer",
                                expert_seat(sid)["persona"], u2,
                                {"experts": experts, "disputes": my_disputes}, tr, on_event=cb)
            ans_res = parallel_tasks_live(_ans_live, list(involved), progress)
        elif involved:
            def answer_job(sid):
                my_disputes = involved[sid]
                u2 = ("[PHASE:dispute_answer]\n你是被追问方。以下是与您相关的分歧追问：\n"
                      + json.dumps(my_disputes, ensure_ascii=False)
                      + "\n\n完整辩论材料:\n" + mat
                      + "\n\n请正面回答每个追问并给出最终立场。只输出 JSON：\n"
                      '{"answers": [{"dispute_id": "D1", "answer": "...", '
                      '"final_stance": "support|oppose|neutral"}]}')
                return ask_json(client, sid, expert_seat(sid), "dispute_answer",
                                expert_seat(sid)["persona"], u2,
                                {"experts": experts, "disputes": my_disputes}, tr)
            ans_res = parallel_tasks(answer_job, list(involved))
        else:
            ans_res = {}
        for sid, (status, payload) in ans_res.items():
            raw = payload[1] if status == "ok" else str(payload)
            tr.entry(sid, status, raw)
            if status == "ok":
                answers[sid] = payload[0]
                dump(f"p4_answer_{sid}.json", payload[0])
        if control.cancelled():
            save_ck(4)
            if live_enabled:
                progress.close()
            return _pack_result(True, "用户取消")
        save_ck(5)
    else:
        print("[P4] 跳过（checkpoint）")

    if start_phase <= 5:
        control.check()
        tr.section("P5 终局裁决（moderator_p5）")
    p5_id = "moderator_p5" if "moderator_p5" in seats_cfg else "moderator"
    p5_seat = seats_cfg.get(p5_id, seats_cfg["moderator"])
    p5_persona = p5_seat.get("persona", sys_p)
    if live_enabled:
        progress.set_phase("P5 终局裁决", 5)
        print(f"[P5] 终局裁决 ... ({p5_id} {p5_seat['model']}@{p5_seat['endpoint']})")
    else:
        print(f"\n[P5] 终局裁决 ... ({p5_id})")
    uv = ("[PHASE:verdict]\n以下是本次议会全部材料。\n" + mat +
          (f"\n\n分歧与答辩:\n{json.dumps({'disputes': disputes, 'answers': answers}, ensure_ascii=False)}"
           if disputes else "") +
          "\n\n请输出终局裁决。只输出 JSON：\n"
          '{"consensus": ["..."], "open_disputes": ["..."], '
          '"recommended_next_experiments": ["..."], "rejected_routes": ["..."], '
          '"self_conflict_note": "同源席位冲突声明或空字符串"}')
    if live_enabled:
        def _ver(cb):
            return ask_json(client, p5_id, p5_seat, "verdict", p5_persona, uv, {"experts": experts}, tr, on_event=cb)
        try:
            verdict, raw = run_single_live(p5_id, _ver, progress, "P5")
        except (Cancelled, SeatSkipped) as e:
            save_ck(5)
            progress.close()
            return _pack_result(True, str(e))
        except Exception:
            save_ck(5)
            progress.close()
            raise
    else:
        try:
            verdict, raw = ask_json(client, p5_id, p5_seat, "verdict", p5_persona, uv, {"experts": experts}, tr)
        except (Cancelled, SeatSkipped) as e:
            save_ck(5)
            return _pack_result(True, str(e))
    dump("p5_verdict_raw.txt", raw)
    tr.entry(p5_id, "ok", raw)
    if live_enabled:
        progress.close()
    save_ck(6)
    return _pack_result(False, "")


def ping(args):
    cfg = load_config(args.config)
    guard_api_keys(args, cfg)
    ok, msg, elapsed = ping_seat(cfg, args.ping)
    tag = "ok" if ok else "FAIL"
    print(f"[ping:{args.ping}] {tag} {elapsed}s {msg}")
    if not ok:
        sys.exit(1)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="多模型研究议会编排器")
    ap.add_argument("topic", nargs="?", help="议会议题")
    ap.add_argument("--file", help="背景材料 markdown/txt 路径")
    ap.add_argument("--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--experts", help="逗号分隔的专家席位 id（缺省=全部）")
    ap.add_argument("--max-calls", type=int, default=80,
                    help="会话最大 LLM 调用数护栏（预留，当前内置重试已受限）")
    ap.add_argument("--workspace", help="工具工作区目录（覆盖 config.tools.workspace）")
    ap.add_argument("--no-tools", action="store_true", help="强制关闭工作区工具")
    ap.add_argument("--resume", help="从会话目录的 checkpoint.json 续跑")
    ap.add_argument("--mode", choices=("debate", "review"), default="debate",
                    help="debate=辩论议会, review=方案评审")
    ap.add_argument("--scheme", help="方案评审：方案文件路径")
    ap.add_argument("--discuss", help="方案评审：讨论区目录")
    ap.add_argument("--author", help="方案评审：主笔席位 id")
    ap.add_argument("--reviewers", help="方案评审：逗号分隔的评审席位")
    ap.add_argument("--scheme-existing", action="store_true",
                    help="方案评审：方案文件已存在，跳过主笔初稿直接进评审，不覆盖原方案")
    ap.add_argument("--inject", help="方案评审：会话开始即注入的补充需求文本"
                    "（等价于运行中插入）")
    ap.add_argument("--wizard", action="store_true",
                    help="交互式安装向导：主持人/端点/密钥/专家数量与职责预设问答")
    ap.add_argument("--wizard-profile", metavar="FILE",
                    help="无头应用安装档案（YAML/JSON，AI Agent 通道）")
    ap.add_argument("--dry-run", action="store_true",
                    help="mock 所有模型调用，零成本验证流程")
    ap.add_argument("--quiet", action="store_true", help="静默模式，不显示实时进度")
    ap.add_argument("--no-live", action="store_true", help="禁用覆盖式刷新，仅阶段汇总")
    ap.add_argument("--list", action="store_true", help="列出席位后退出")
    ap.add_argument("--ping", metavar="SEAT", help="对单席位做连通性测试后退出")
    args = ap.parse_args()

    if args.wizard or getattr(args, "wizard_profile", None):
        import wizard as wizard_mod
        if args.wizard:
            wizard_mod.run_interactive(args.config)
        else:
            wizard_mod.run_profile(args.wizard_profile, args.config)
        return
    if args.list:
        cfg = load_config(args.config)
        for sid, seat in cfg["seats"].items():
            role = seat.get("role", "expert")
            print(f"{sid:<16} {role:<10} {seat.get('model', '?'):<20} "
                  f"@{seat.get('endpoint', '-')} [{seat.get('protocol', 'openai')}]")
        return
    if args.ping:
        ping(args)
        return
    if args.resume:
        run(args)
        return
    if not args.topic:
        sys.exit("缺少议题。用法: python council.py \"议题\" [--file 背景.md]")
    run(args)


if __name__ == "__main__":
    main()

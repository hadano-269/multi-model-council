#!/usr/bin/env python3
"""council MCP — 供 OpenCode 异步发起议会。stdout 只走 JSON-RPC。"""
import argparse
import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# OpenCode 把 stderr 接到管道且握手前不读，一 import 打日志就会堵死。
# 必须在加载 council 之前把 stderr 拽到文件。
_LOG = BASE / "mcp_debug.log"
_logf = open(_LOG, "a", encoding="utf-8")
try:
    os.dup2(_logf.fileno(), sys.stderr.fileno())
except Exception:
    sys.stderr = _logf

CONFIG_PATH = BASE / "config.yaml"
OUT_ROOT = BASE / "out"
PROTOCOL_FALLBACK = "2024-11-05"
PROTOCOL_OK = {
    "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25",
}

_JOBS = {}
_JOBS_LOCK = threading.Lock()
_council_mod = None


def log(msg):
    line = f"[council-mcp] {msg}\n"
    try:
        _logf.write(line)
        _logf.flush()
    except Exception:
        pass


def _council():
    global _council_mod
    if _council_mod is None:
        log("loading council.py")
        import council as _c
        _council_mod = _c
        log("council.py ready")
    return _council_mod


def load_cfg():
    return _council().load_config(str(CONFIG_PATH))


def expert_ids(cfg):
    out = []
    for sid, seat in (cfg.get("seats") or {}).items():
        if sid in ("moderator", "moderator_p5") or seat.get("role") == "moderator":
            continue
        out.append(sid)
    return out


def format_status(snap):
    if not snap:
        return "无状态（会话不存在或尚未开始写 status.json）"
    state = snap.get("state") or "running"
    phase = snap.get("phase") or "待开始"
    idx = snap.get("phase_idx") or 0
    total = snap.get("phase_total") or 5
    elapsed = snap.get("session_elapsed")
    if elapsed is None:
        elapsed = snap.get("elapsed_sec") or 0
    calls = snap.get("calls_done") or 0
    max_calls = snap.get("max_calls") or 80
    toks = snap.get("tokens") or {}
    tot_in = tot_out = 0
    if isinstance(toks, dict) and "total" in toks:
        tot_in = int((toks.get("total") or {}).get("in") or 0)
        tot_out = int((toks.get("total") or {}).get("out") or 0)
    else:
        for v in (toks.values() if isinstance(toks, dict) else []):
            if isinstance(v, dict):
                tot_in += int(v.get("in") or 0)
                tot_out += int(v.get("out") or 0)
    lines = [
        f"session: {snap.get('session') or '?'}",
        f"state: {state}",
        f"topic: {snap.get('topic') or ''}",
        f"[{idx}/{total}] {phase}  会话 {elapsed}s  调用 {calls}/{max_calls}  "
        f"token in {tot_in} / out {tot_out}",
    ]
    if snap.get("error"):
        lines.append(f"error: {snap['error']}")
    seats = snap.get("seats") or {}
    for sid in sorted(seats):
        info = seats[sid] or {}
        st = info.get("status") or "?"
        el = int(info.get("elapsed") or 0)
        tin = int(info.get("tok_in") or 0)
        tout = int(info.get("tok_out") or 0)
        det = (info.get("detail") or "").replace("\n", " ")
        if len(det) > 80:
            det = det[:79] + "…"
        extra = ""
        if st == "retrying":
            extra = f"  重试 {info.get('attempt', 1)}/3"
        lines.append(f"  {sid:<16} {st:<12} {el:>4}s  in {tin}/out {tout}{extra}  {det}")
    if snap.get("out_dir"):
        lines.append(f"out: {snap['out_dir']}")
    return "\n".join(lines)


def format_verdict(result):
    meta = (result or {}).get("meta") or {}
    verdict = (result or {}).get("verdict") or {}
    sections = (
        ("consensus", "共识"),
        ("open_disputes", "未决分歧"),
        ("recommended_next_experiments", "推荐实验"),
        ("rejected_routes", "否决路线"),
        ("self_conflict_note", "同源声明"),
    )
    parts = [
        f"session: {meta.get('session')}",
        f"topic: {meta.get('topic')}",
        f"mode: {meta.get('mode')}  elapsed: {meta.get('elapsed_sec')}s",
        f"experts: {', '.join((meta.get('seats') or {}).get('experts') or [])}",
    ]
    toks = (meta.get("tokens") or {}).get("total") or {}
    if toks:
        parts.append(
            f"token: in {toks.get('in', 0)} / out {toks.get('out', 0)} / "
            f"calls {toks.get('calls', 0)}"
        )
    if meta.get("incomplete"):
        parts.append(f"（未完成）{meta.get('error') or ''}")
    parts.append("")
    known = {k for k, _ in sections}
    def emit(title, val):
        parts.append(f"【{title}】")
        if val is None or val == "" or val == []:
            parts.append("  （无）")
        elif isinstance(val, list):
            for i, item in enumerate(val, 1):
                text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                parts.append(f"  {i}. {text}")
        else:
            parts.append(f"  {val}")
        parts.append("")

    if isinstance(verdict, dict):
        for key, title in sections:
            if key in verdict:
                emit(title, verdict.get(key))
        for key, val in verdict.items():
            if key not in known:
                emit(key, val)
    else:
        parts.append(str(verdict))
    if meta.get("session"):
        parts.append(f"out: {OUT_ROOT / meta['session']}")
    return "\n".join(parts).rstrip() + "\n"


def resolve_session(session=None):
    if session:
        p = Path(session)
        if p.is_dir():
            return p
        raw = str(session).strip().rstrip("/\\")
        name = Path(raw).name
        direct = OUT_ROOT / name
        if direct.is_dir():
            return direct
        if OUT_ROOT.is_dir():
            hits = sorted(
                (d for d in OUT_ROOT.iterdir()
                 if d.is_dir() and (d.name == name or d.name.startswith(name))),
                key=lambda d: d.stat().st_mtime,
                reverse=True,
            )
            if hits:
                return hits[0]
        return direct
    with _JOBS_LOCK:
        running = [j for j in _JOBS.values() if j.get("state") == "running"]
        if running:
            running.sort(key=lambda j: j.get("started") or 0, reverse=True)
            return Path(running[0]["session_dir"])
    if not OUT_ROOT.is_dir():
        return None
    dirs = [d for d in OUT_ROOT.iterdir() if d.is_dir()]
    if not dirs:
        return None
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return dirs[0]


def read_status_file(session_dir):
    if not session_dir:
        return None
    p = Path(session_dir) / "status.json"
    if not p.exists():
        return {"session": Path(session_dir).name, "state": "unknown",
                "out_dir": str(session_dir)}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"session": Path(session_dir).name, "state": "error",
                "error": f"status.json 无法读取: {e}", "out_dir": str(session_dir)}


def tool_seats():
    cfg = load_cfg()
    lines = []
    for sid, seat in (cfg.get("seats") or {}).items():
        role = seat.get("role", "expert")
        model = seat.get("model", "?")
        ep = seat.get("endpoint") or "custom"
        proto = seat.get("protocol", "openai")
        lines.append(f"{sid:<16} {role:<10} {model:<28} @{ep} [{proto}]")
    if not lines:
        return "config.yaml 中没有席位"
    return "\n".join(lines)


def _resolve_local(rel):
    """相对路径解析：先试 OpenCode 工作目录，再退到 council 安装目录。"""
    p = Path(rel)
    if p.is_absolute():
        return p.resolve()
    cand = (Path.cwd() / p).resolve()
    return cand if cand.exists() else (BASE / rel).resolve()


def tool_start(topic, file=None, experts=None, dry_run=False, mode="debate",
               scheme=None, discuss=None, author=None, reviewers=None,
               scheme_existing=False, inject=None):
    topic = (topic or "").strip()
    if not topic:
        return "缺少 topic"
    is_review = str(mode or "debate").strip().lower() == "review"
    if not is_review and str(mode or "").strip() not in ("debate", "", None):
        return f"未知 mode: {mode}（可选 debate/review）"
    cfg = load_cfg()
    available = expert_ids(cfg)
    seats_cfg = cfg.get("seats") or {}
    selected = []
    if experts and not is_review:
        selected = [s.strip() for s in str(experts).split(",") if s.strip()]
        missing = [s for s in selected if s not in seats_cfg]
        if missing:
            return f"席位不存在: {missing}，可选专家: {available}"
    bg = None
    if file:
        bgp = _resolve_local(file)
        if not bgp.exists():
            return f"背景文件不存在: {bgp}"
        bg = str(bgp)
    scheme_path = discuss_path = None
    if is_review:
        if not scheme or not str(scheme).strip():
            return "方案评审模式需要提供 scheme（方案文件路径）"
        scheme_path = _resolve_local(scheme)
        if scheme_existing and not scheme_path.exists():
            return f"scheme_existing=true 但方案文件不存在: {scheme_path}"
        if discuss and str(discuss).strip():
            discuss_path = _resolve_local(discuss)
        if author and author not in seats_cfg:
            return f"主笔席位不存在: {author}，可选: {[s for s in seats_cfg]}"
        if reviewers:
            rlist = [s.strip() for s in str(reviewers).split(",") if s.strip()]
            miss_r = [s for s in rlist if s not in seats_cfg]
            if miss_r:
                return f"评审席位不存在: {miss_r}，可选专家: {available}"
    c = _council()
    session_dir, session_id, _ts, _title = c.make_session_dir(OUT_ROOT, topic)
    control = c.RunControl()
    job = {
        "session": session_id,
        "session_dir": str(session_dir),
        "state": "running",
        "error": None,
        "started": time.time(),
        "control": control,
        "topic": topic,
    }
    phase_total = 8 if is_review else 5
    init_status = {
        "session": session_id,
        "topic": topic,
        "pipeline": "review" if is_review else "debate",
        "mode": "DRY-RUN" if dry_run else "LIVE",
        "state": "running",
        "experts": selected or available,
        "out_dir": str(session_dir),
        "phase": "启动中",
        "phase_idx": 0,
        "phase_total": phase_total,
        "session_elapsed": 0,
        "calls_done": 0,
        "max_calls": 80,
        "tokens": {},
        "seats": {},
    }
    (session_dir / "status.json").write_text(
        json.dumps(init_status, ensure_ascii=False, indent=2), encoding="utf-8")

    def worker():
        args = argparse.Namespace(
            topic=topic, file=bg, config=str(CONFIG_PATH),
            experts=",".join(selected) if selected else None,
            max_calls=80, dry_run=bool(dry_run),
            quiet=True, no_live=False, workspace=None, no_tools=False,
            resume=None, session_dir=str(session_dir),
            retry_hub=None, control=control,
            mode="review" if is_review else "debate",
            scheme=str(scheme_path) if scheme_path else None,
            discuss=str(discuss_path) if discuss_path else None,
            author=author or None,
            reviewers=reviewers or None,
            scheme_existing=bool(scheme_existing),
            inject=(str(inject).strip() if inject else "") or None,
        )
        cmod = _council()
        progress = cmod.LiveProgress(enabled=True, total_phases=phase_total,
                                     on_update=lambda _s: None)
        try:
            with contextlib.redirect_stdout(_logf):
                cmod.run(args, cfg=cfg, progress=progress)
            with _JOBS_LOCK:
                job["state"] = "done"
        except Exception as e:
            log(f"{session_id} 失败: {e}")
            with _JOBS_LOCK:
                job["state"] = "error"
                job["error"] = str(e)
            try:
                progress.status_extra = dict(progress.status_extra or {})
                progress.status_extra["state"] = "error"
                progress.status_extra["error"] = str(e)
                progress.write_status()
            except Exception:
                pass
        finally:
            try:
                progress.close()
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True, name=f"council-{session_id}").start()
    with _JOBS_LOCK:
        _JOBS[session_id] = job
    who = ",".join(selected) if selected else "全部专家"
    run_mode = "DRY-RUN" if dry_run else "LIVE"
    label = "方案评审" if is_review else "议会"
    extra = f"\nscheme: {scheme_path}\n主笔: {author or '缺省首个专家'}" if is_review else ""
    return (
        f"已启动 {run_mode} {label}，立即返回。请用 council_status 轮询，结束后用 council_verdict 取结果。\n"
        f"session: {session_id}\n"
        + (f"scheme: {scheme_path}\n" if is_review else f"experts: {who}\n")
        + f"out: {session_dir}\n"
        + ("不要自己扮演评审或主笔。" if is_review else "不要自己扮演专家辩论。")
    )


def tool_status(session=None):
    session_dir = resolve_session(session)
    if not session_dir:
        return "没有可查询的会话。先调用 council_start，或指定 session。"
    snap = read_status_file(session_dir)
    with _JOBS_LOCK:
        job = _JOBS.get(session_dir.name)
        if job and job.get("error") and snap is not None:
            snap.setdefault("error", job["error"])
            if snap.get("state") == "running" and job["state"] != "running":
                snap["state"] = job["state"]
    return format_status(snap)


def tool_verdict(session=None):
    session_dir = resolve_session(session)
    if not session_dir:
        return "没有可读取的会话。"
    verdict_path = session_dir / "verdict.json"
    snap = read_status_file(session_dir)
    with _JOBS_LOCK:
        job = _JOBS.get(session_dir.name)
        if job and snap is not None:
            if job.get("error"):
                snap.setdefault("error", job["error"])
            if snap.get("state") == "running" and job["state"] != "running":
                snap["state"] = job["state"]
    if not verdict_path.exists():
        return (
            "裁决尚未生成。当前进度：\n"
            + format_status(snap)
            + "\n请稍后再调 council_status / council_verdict。"
        )
    try:
        result = json.loads(verdict_path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"verdict.json 无法读取: {e}"
    return format_verdict(result)


def tool_cancel(session):
    session_dir = resolve_session(session)
    if not session_dir:
        return "找不到会话。"
    sid = session_dir.name
    with _JOBS_LOCK:
        job = _JOBS.get(sid)
    if not job:
        return f"{sid} 不在本 MCP 进程内（可能已结束或由 CLI/GUI 启动），无法远程取消。"
    job["control"].cancel()
    return f"已请求取消 {sid}，当前请求结束后停止。用 council_status 查看。"


TOOLS = {
    "council_seats": {
        "description": (
            "列出 council 当前配置的主持人与专家席位（id/模型/端点/协议）。"
            "发起议会前可先看有哪些专家可传给 council_start.experts。"
        ),
        "schema": {"type": "object", "properties": {}},
        "fn": lambda _a: tool_seats(),
    },
    "council_start": {
        "description": (
            "在后台启动一场多模型会诊，立即返回 session id，不会等待结束。"
            "mode=debate（默认）为议会辩论（拆题→表态→交叉评审→分歧追问→裁决）；"
            "mode=review 为方案评审（主笔写方案→多轮评审打分→改稿→定稿），此时必须提供 scheme。"
            "之后用 council_status 查看进度/token，结束后用 council_verdict 取结果。"
            "不要自己扮演专家或评审。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "议会议题/需求描述（必填）"},
                "file": {"type": "string", "description": "可选背景材料路径（md/txt）"},
                "experts": {
                    "type": "string",
                    "description": "逗号分隔的专家席位 id；省略=全部专家（仅 debate 模式）",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "true 则 mock 全部模型调用，用于连通性验证",
                },
                "mode": {
                    "type": "string",
                    "enum": ["debate", "review"],
                    "description": "debate=议会辩论（默认）；review=方案评审",
                },
                "scheme": {
                    "type": "string",
                    "description": "review 模式必填：方案文件路径；相对路径先按当前目录再按安装目录解析",
                },
                "discuss": {
                    "type": "string",
                    "description": "review 可选：讨论区目录，缺省为方案同目录下的 讨论区/",
                },
                "author": {
                    "type": "string",
                    "description": "review 可选：主笔席位 id，缺省取第一个专家席",
                },
                "reviewers": {
                    "type": "string",
                    "description": "review 可选：逗号分隔的评审席位 id，缺省为主笔外全部专家",
                },
                "scheme_existing": {
                    "type": "boolean",
                    "description": "review 可选：方案文件已存在时置 true，跳过主笔初稿直接评审、不覆盖原文",
                },
                "inject": {
                    "type": "string",
                    "description": "review 可选：会话开始即注入的补充需求文本，从下一阶段起持续生效并由主笔写进方案",
                },
            },
            "required": ["topic"],
        },
        "fn": lambda a: tool_start(
            a.get("topic"), a.get("file"), a.get("experts"), bool(a.get("dry_run")),
            mode=a.get("mode") or "debate", scheme=a.get("scheme"),
            discuss=a.get("discuss"), author=a.get("author"),
            reviewers=a.get("reviewers"),
            scheme_existing=bool(a.get("scheme_existing")), inject=a.get("inject"),
        ),
    },
    "council_status": {
        "description": (
            "查询议会进度快照：阶段、每席推理/重试/完成/失败、耗时、token in/out。"
            "省略 session 则取本进程正在跑的一场，否则取最近一场。"
            "进行中请隔一段时间再查，不要连续狂轮询。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "session id 或 out 目录路径；可省略",
                },
            },
        },
        "fn": lambda a: tool_status(a.get("session")),
    },
    "council_verdict": {
        "description": (
            "读取已完成议会的结构化裁决（共识/未决分歧/推荐实验/否决路线/token）。"
            "若尚未结束，返回当前进度并提示继续等。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "session id 或 out 目录路径；可省略",
                },
            },
        },
        "fn": lambda a: tool_verdict(a.get("session")),
    },
    "council_cancel": {
        "description": "取消本 MCP 进程内正在跑的一场议会（当前请求结束后停止）。",
        "schema": {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "session id"},
            },
            "required": ["session"],
        },
        "fn": lambda a: tool_cancel(a.get("session")),
    },
}


def tools_list():
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["schema"],
        }
        for name, spec in TOOLS.items()
    ]


def call_tool(name, arguments):
    spec = TOOLS.get(name)
    if not spec:
        raise ValueError(f"未知工具: {name}")
    return spec["fn"](arguments or {})


def read_message(stdin):
    header_line = stdin.readline()
    if not header_line:
        return None
    if header_line.lower().startswith(b"content-length:"):
        headers = [header_line]
        while True:
            line = stdin.readline()
            if not line:
                return None
            headers.append(line)
            if line in (b"\r\n", b"\n"):
                break
        length = 0
        for line in headers:
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        body = stdin.read(length)
        if len(body) < length:
            return None
        return json.loads(body.decode("utf-8"))
    raw = header_line.strip()
    if not raw:
        return read_message(stdin)
    if raw.startswith(b"{"):
        return json.loads(raw.decode("utf-8"))
    log(f"无法解析的消息头: {header_line[:80]!r}")
    return read_message(stdin)


def write_message(payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def handle(msg):
    if not isinstance(msg, dict):
        return
    method = msg.get("method")
    msg_id = msg.get("id", None)
    log(f"recv method={method} id={msg_id}")
    if method is None and "result" in msg:
        return
    if method and method.startswith("notifications/"):
        return
    if method == "initialize":
        client_ver = ((msg.get("params") or {}).get("protocolVersion")
                      or PROTOCOL_FALLBACK)
        version = client_ver if client_ver in PROTOCOL_OK else PROTOCOL_FALLBACK
        write_message({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "council", "version": "0.1.0"},
            },
        })
        return
    if method == "ping":
        write_message({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        return
    if method == "tools/list":
        write_message({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": tools_list()},
        })
        return
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            text = call_tool(name, args)
            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": str(text)}],
                    "isError": False,
                },
            })
        except Exception as e:
            write_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": f"工具失败: {e}"}],
                    "isError": True,
                },
            })
        return
    if msg_id is None:
        return
    write_message({
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    })


def serve():
    stdin = sys.stdin.buffer
    log(f"stdio 已就绪 pid={os.getpid()} config={CONFIG_PATH}")
    while True:
        try:
            msg = read_message(stdin)
        except Exception as e:
            log(f"读消息失败: {e}")
            continue
        if msg is None:
            break
        try:
            handle(msg)
        except Exception as e:
            log(f"处理失败: {e}")
            if isinstance(msg, dict) and "id" in msg:
                write_message({
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {"code": -32603, "message": str(e)},
                })


if __name__ == "__main__":
    serve()

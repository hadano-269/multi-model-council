"""方案评审模式：主笔写方案 → 多轮评审/打分 → 改稿 → 定稿说明。"""
import json
import time
from pathlib import Path

import council


def _seat_short(sid):
    return (sid or "seat").replace("expert_", "").replace("moderator", "mod")


def _write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    council.write_text_retry(path, text if text.endswith("\n") else text + "\n")


def _list_discuss(discuss_dir):
    p = Path(discuss_dir)
    if not p.exists():
        return "(讨论区为空)"
    names = sorted(x.name for x in p.glob("*.md"))
    if not names:
        return "(讨论区为空)"
    return "\n".join(f"- {n}" for n in names)


def _read(path, fallback="(文件尚不存在)"):
    p = Path(path)
    if not p.exists():
        return fallback
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return fallback


def run_review(args, cfg=None, progress=None):
    cfg = cfg if cfg is not None else council.load_config(args.config)
    council.guard_api_keys(args, cfg)
    seats_cfg = cfg["seats"]
    endpoints = cfg.get("endpoints") or {}
    experts = [s for s, st in seats_cfg.items()
               if s not in ("moderator",) and st.get("role") != "moderator"]
    author_id = getattr(args, "author", None) or (experts[0] if experts else "moderator")
    if author_id not in seats_cfg:
        raise council.FatalSeatError(f"主笔席位不存在: {author_id}")
    raw_rev = getattr(args, "reviewers", None)
    if raw_rev:
        reviewers = [s.strip() for s in raw_rev.split(",") if s.strip()]
    else:
        reviewers = [s for s in experts if s != author_id]
    reviewers = [s for s in reviewers if s in seats_cfg]
    if not reviewers:
        raise council.FatalSeatError("没有可用的评审席位")

    scheme_path = Path(getattr(args, "scheme", None) or "方案.md")
    if not scheme_path.is_absolute():
        ws = (cfg.get("tools") or {}).get("workspace") or "."
        scheme_path = (Path(ws) / scheme_path).resolve()
    discuss_dir = Path(getattr(args, "discuss", None) or (scheme_path.parent / "讨论区"))
    if not discuss_dir.is_absolute():
        ws = (cfg.get("tools") or {}).get("workspace") or "."
        discuss_dir = (Path(ws) / discuss_dir).resolve()

    topic = (getattr(args, "topic", None) or "").strip() or "方案评审"
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    session_dir, session_id, ts, session_title = council.make_session_dir(
        council.BASE / "out", topic)
    toolkit, tools_meta = council.build_toolkit(cfg, args)
    client = council.Client(
        endpoints, dry_run=args.dry_run, toolkit=toolkit,
        max_tool_rounds=(cfg.get("tools") or {}).get("max_rounds", 8),
        memory=council.SeatMemory())
    client.retry_hub = getattr(args, "retry_hub", None)
    control = getattr(args, "control", None) or council.RunControl()
    client.control = control

    tr = council.Transcript(session_dir / "transcript.md", [
        f"时间: {ts}", f"模式: {mode} / 方案评审", f"议题: {topic}",
        f"主笔: {author_id}", f"评审: {', '.join(reviewers)}",
        f"方案: {scheme_path}", f"讨论区: {discuss_dir}",
        f"工具: {tools_meta}",
    ])
    t0 = time.time()
    if progress is None:
        progress = council.LiveProgress(enabled=True, total_phases=8)
    progress.max_calls = getattr(args, "max_calls", 80)
    progress.session_start = t0
    live = progress.enabled
    scores_hist = []
    author_seat = seats_cfg[author_id]

    def pack(incomplete=False, err="", verdict=None):
        result = {
            "meta": {
                "session": session_id, "session_title": session_title,
                "time": ts, "mode": mode, "topic": topic,
                "pipeline": "review", "incomplete": incomplete, "error": err,
                "author": author_id, "reviewers": reviewers,
                "scheme": str(scheme_path), "discuss": str(discuss_dir),
                "elapsed_sec": round(time.time() - t0, 1),
                "tokens": client.usage_snapshot(), "tools": tools_meta,
            },
            "scores": scores_hist,
            "verdict": verdict or {
                "consensus": [], "open_disputes": [],
                "recommended_next_experiments": [],
                "rejected_routes": [], "self_conflict_note": "",
                "scheme": str(scheme_path),
            },
        }
        council.write_text_retry(
            session_dir / "verdict.json",
            json.dumps(result, ensure_ascii=False, indent=2))
        print(f"\n[council] 方案评审{'未完成' if incomplete else '完成'} "
              f"{result['meta']['elapsed_sec']}s")
        print(f"[council] 方案: {scheme_path}")
        print(f"[council] 讨论区: {discuss_dir}")
        return result

    def phase(title, idx):
        control.check()
        tr.section(title)
        if live:
            progress.set_phase(title, idx, total=8)
        print(f"[{idx}/8] {title}")

    def run_author(phase_id, prompt, mock):
        ctx = {"mock_text": mock, "experts": reviewers}
        if live:
            def _fn(cb):
                return council.ask_text(
                    client, author_id, author_seat, phase_id,
                    author_seat.get("persona") or "你是方案主笔。",
                    prompt, ctx, tr, on_event=cb)
            return council.run_single_live(author_id, _fn, progress, phase_id)
        return council.ask_text(
            client, author_id, author_seat, phase_id,
            author_seat.get("persona") or "你是方案主笔。",
            prompt, ctx, tr)

    def run_reviewers(phase_id, prompt_builder, round_name, want_json=False):
        def job(sid, on_event=None):
            seat = seats_cfg[sid]
            u = prompt_builder(sid)
            ctx = {
                "experts": reviewers,
                "mock_text": f"[mock] {sid} {round_name} 评审意见。\n",
            }
            if want_json:
                ctx_j = {
                    "experts": reviewers,
                    "motions": [{"id": "M1", "title": round_name, "description": ""}],
                }
                data, raw = council.ask_json(
                    client, sid, seat, "score",
                    seat.get("persona") or "你是方案评审人。",
                    u, ctx_j, tr, on_event=on_event)
                return data, raw
            raw = council.ask_text(
                client, sid, seat, phase_id,
                seat.get("persona") or "你是方案评审人。",
                u, ctx, tr, on_event=on_event)
            return raw

        if live:
            def live_job(sid, cb):
                return job(sid, on_event=cb)
            res = council.parallel_tasks_live(live_job, reviewers, progress)
        else:
            res = council.parallel_tasks(job, reviewers)
        out = {}
        for sid, (st, payload) in res.items():
            if st != "ok":
                continue
            if want_json:
                data, raw = payload
                out[sid] = data
                text = raw if isinstance(raw, str) else json.dumps(data, ensure_ascii=False, indent=2)
            else:
                text = payload if isinstance(payload, str) else str(payload)
                out[sid] = text
            _write(discuss_dir / f"{_seat_short(sid)}-{round_name}.md", text)
        return out

    try:
        phase("A0 主笔写方案", 1)
        draft = run_author(
            "author_draft",
            f"需求/议题:\n{topic}\n\n请调研工作区后写出完整改进方案（Markdown）。"
            f"只输出方案正文，不要JSON。",
            f"# [mock] {topic} 方案草案\n\n- 目标\n- 步骤\n- 风险\n")
        _write(scheme_path, draft)
        council.write_checkpoint(session_dir, {"next_phase": 2, "pipeline": "review",
                                               "topic": topic, "scheme": str(scheme_path)})

        phase("R1 第一轮评审", 2)
        def p_r1(_sid):
            return (f"请评审这份改进方案:\n路径: {scheme_path}\n\n"
                    f"{_read(scheme_path)}\n\n"
                    f"把完整评审意见写成 Markdown。不要只输出 JSON。")
        run_reviewers("review1", p_r1, "第1轮评审")
        if control.cancelled():
            return pack(True, "用户取消")

        phase("R2 第二轮打分", 3)
        def p_r2(_sid):
            return (
                f"方案:\n{_read(scheme_path)}\n\n讨论区文件:\n{_list_discuss(discuss_dir)}\n\n"
                "请：1) 总结共识与分歧 2) 为每位评审上轮意见打分(1-10) "
                "3) 列出不超过3条独有创见。只输出 JSON：\n"
                '{"scores": {"seat_id": 8}, "consensus": ["..."], '
                '"disputes": ["..."], "insights": ["..."], "can_ship": false}'
            )
        r2 = run_reviewers("score2", p_r2, "第2轮打分", want_json=True)
        scores_hist.append({"round": "R2", "by_seat": r2})
        if control.cancelled():
            return pack(True, "用户取消")

        phase("A1 主笔改稿", 4)
        rev1 = run_author(
            "author_rev1",
            f"当前方案:\n{_read(scheme_path)}\n\n讨论区:\n{_list_discuss(discuss_dir)}\n"
            f"请根据评审意见改进方案，输出完整方案 Markdown（覆盖稿）。",
            f"# [mock] {topic} 改进稿 v2\n\n已吸收第1轮评审。\n")
        _write(scheme_path, rev1)

        phase("R3 第三轮评审", 5)
        def p_r3(_sid):
            return (f"改进后的方案:\n{_read(scheme_path)}\n\n"
                    f"讨论区:\n{_list_discuss(discuss_dir)}\n\n"
                    "再次评审是否可实施。输出 Markdown 意见。")
        run_reviewers("review3", p_r3, "第3轮评审")
        if control.cancelled():
            return pack(True, "用户取消")

        phase("A2 主笔再改", 6)
        rev2 = run_author(
            "author_rev2",
            f"当前方案:\n{_read(scheme_path)}\n\n第3轮意见在讨论区。请再改一版完整方案 Markdown。",
            f"# [mock] {topic} 改进稿 v3\n\n已吸收第3轮评审。\n")
        _write(scheme_path, rev2)

        phase("R4 定稿评审打分", 7)
        def p_r4(_sid):
            return (
                f"最新方案:\n{_read(scheme_path)}\n\n讨论区:\n{_list_discuss(discuss_dir)}\n"
                "请判断能否定稿实施，并为上轮意见打分。只输出 JSON：\n"
                '{"scores": {"seat_id": 8}, "consensus": ["..."], '
                '"disputes": ["..."], "insights": ["..."], "can_ship": true}'
            )
        r4 = run_reviewers("score4", p_r4, "第4轮打分", want_json=True)
        scores_hist.append({"round": "R4", "by_seat": r4})
        if control.cancelled():
            return pack(True, "用户取消")

        phase("F 定稿说明", 8)
        fin = run_author(
            "author_final",
            f"定稿方案:\n{_read(scheme_path)}\n\n请写一段实施说明（Markdown），"
            "包括范围、风险、不做什么。不要改方案结构除非必要。",
            f"# [mock] {topic} 定稿说明\n\n可以实施。\n")
        _write(discuss_dir / "定稿说明.md", fin)
        if live:
            progress.close()
        can = any(
            isinstance(v, dict) and v.get("can_ship")
            for round_ in scores_hist
            for v in (round_.get("by_seat") or {}).values()
        )
        verdict = {
            "consensus": ["方案已多轮评审并落盘"],
            "open_disputes": [],
            "recommended_next_experiments": ["按定稿说明实施"],
            "rejected_routes": [],
            "self_conflict_note": "",
            "can_ship": can,
            "scheme": str(scheme_path),
            "discuss": str(discuss_dir),
        }
        council.write_checkpoint(session_dir, {
            "next_phase": 9, "pipeline": "review", "topic": topic,
            "scheme": str(scheme_path), "discuss": str(discuss_dir),
        })
        return pack(False, "", verdict)
    except council.Cancelled as e:
        if live:
            progress.close()
        return pack(True, str(e))
    except council.SeatSkipped as e:
        if live:
            progress.close()
        return pack(True, str(e))
    except Exception:
        if live:
            progress.close()
        raise

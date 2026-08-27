#!/usr/bin/env python3
"""council GUI — 席位设置 + 多模型辩论实时进度（CustomTkinter）"""
import argparse
import copy
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.yaml"

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

try:
    import customtkinter as ctk
except ImportError:
    sys.exit("缺少依赖 customtkinter，请先执行: pip install customtkinter")

_THEME = BASE / "theme.json"
if _THEME.exists():
    ctk.set_default_color_theme(str(_THEME))

import yaml

import council

PROTOCOLS = ("openai", "anthropic")
SPIN_FRAMES = ["|", "/", "-", "\\"]
C_OK = ("#16803C", "#4ADE80")
C_FAIL = ("#DC2626", "#F87171")
C_RETRY = ("#B45309", "#FBBF24")
C_RUN = ("#18181B", "#ECECEC")
C_DIM = ("#71717A", "#A1A1AA")
C_BORDER = ("#E4E4E7", "#3F3F46")
C_DANGER = ("#B91C1C", "#7F1D1D")
THEME_LABELS = ("深色", "浅色", "系统")
THEME_MODES = {"深色": "dark", "浅色": "light", "系统": "system"}
MODE_LABELS = {"dark": "深色", "light": "浅色", "system": "系统"}
RESERVED_SIDS = {"moderator", "moderator_p5"}
VERDICT_SECTIONS = (
    ("consensus", "共识"),
    ("open_disputes", "未决分歧"),
    ("recommended_next_experiments", "推荐实验"),
    ("rejected_routes", "否决路线"),
    ("self_conflict_note", "同源声明"),
)
PRESET_PERSONA = (
    "你是独立研究员，不受议会已有框架约束。职责：补充替代假说、相邻领域可类比方法、被集体忽略的第三条路。\n"
    "鼓励低共识观点，但必须给出可查证的依据或完整推理链。严格只输出合法 JSON."
)


def format_verdict(verdict):
    if not isinstance(verdict, dict):
        return str(verdict or "（无裁决）")
    known = {k for k, _ in VERDICT_SECTIONS}
    parts = []

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

    for key, title in VERDICT_SECTIONS:
        if key in verdict:
            emit(title, verdict.get(key))
    for key, val in verdict.items():
        if key not in known:
            emit(key, val)
    return "\n".join(parts).rstrip() + "\n"


def sanitize_sid(name, taken):
    s = re.sub(r"[^0-9a-z_]+", "_", (name or "").strip().lower()).strip("_")
    if not s:
        return ""
    base, i = s, 2
    while s in taken or s in RESERVED_SIDS:
        s = f"{base}_{i}"
        i += 1
    return s


def load_raw():
    if CONFIG_PATH.exists():
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    else:
        cfg = {}
    cfg.setdefault("defaults", {"protocol": "openai", "temperature": 0.7,
                                "max_tokens": 16384, "timeout": 300})
    cfg.setdefault("endpoints", {})
    cfg.setdefault("seats", {})
    cfg.setdefault("tools", {
        "enabled": False,
        "workspace": ".",
        "allow": ["read_file", "list_dir", "grep"],
        "max_rounds": 8,
        "max_file_bytes": 200000,
    })
    return cfg


class SeatEditor(ctk.CTkFrame):
    def __init__(self, master, title, eff=None, model="", protocol="openai",
                 editable_name=False, on_delete=None, src_sid=None, orig=None,
                 persona=""):
        super().__init__(master, corner_radius=8, border_width=1,
                                 border_color=C_BORDER)
        self.src_sid = src_sid
        self.orig = orig or {"url": "", "key": ""}
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 0))
        bold = ctk.CTkFont(size=14, weight="bold")
        self.title_var = ctk.StringVar(value=title)
        if editable_name:
            ctk.CTkEntry(top, textvariable=self.title_var, width=180,
                         font=bold).pack(side="left")
        else:
            ctk.CTkLabel(top, textvariable=self.title_var, font=bold).pack(side="left")
        if on_delete is not None:
            ctk.CTkButton(top, text="删除", width=60, height=24,
                          fg_color=("#FEE2E2", "#7F1D1D"),
                          hover_color=("#FECACA", "#991B1B"),
                          text_color=("#B91C1C", "#FECACA"),
                          command=lambda: on_delete(self)).pack(side="right")
        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(fill="x", padx=10, pady=(2, 10))
        grid.columnconfigure(1, weight=1)

        def lbl(r, text, col=0):
            ctk.CTkLabel(grid, text=text, text_color=C_DIM, width=72,
                         anchor="e").grid(
                row=r, column=col, sticky="e", padx=(0, 8), pady=4)

        eff = eff or {}
        lbl(0, "Base URL")
        self.url_var = ctk.StringVar(value=eff.get("url") or "")
        ctk.CTkEntry(grid, textvariable=self.url_var,
                     placeholder_text="留空则沿用该席位的原端点").grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=3)
        lbl(1, "API Key")
        self.key_var = ctk.StringVar(value=eff.get("key") or "")
        self.key_entry = ctk.CTkEntry(grid, textvariable=self.key_var, show="•",
                                      placeholder_text="sk-… / ark-…（留空沿用原端点）")
        self.key_entry.grid(row=1, column=1, sticky="ew", pady=3)
        self.eye_btn = ctk.CTkButton(grid, text="显示", width=52, height=26,
                                     command=self.toggle_eye)
        self.eye_btn.grid(row=1, column=2, sticky="w", padx=6, pady=3)
        lbl(2, "模型名")
        self.model_var = ctk.StringVar(value=model)
        ctk.CTkEntry(grid, textvariable=self.model_var).grid(
            row=2, column=1, columnspan=3, sticky="ew", pady=3)
        lbl(3, "协议")
        self.proto_var = ctk.StringVar(value=protocol if protocol in PROTOCOLS else "openai")
        ctk.CTkOptionMenu(grid, values=list(PROTOCOLS), variable=self.proto_var,
                          width=140).grid(row=3, column=1, sticky="w", pady=3)
        ping_row = ctk.CTkFrame(grid, fg_color="transparent")
        ping_row.grid(row=4, column=1, columnspan=3, sticky="w", pady=(2, 4))
        self.ping_btn = ctk.CTkButton(ping_row, text="Ping", width=56, height=24,
                                      command=self._do_ping)
        self.ping_btn.pack(side="left")
        self.ping_lbl = ctk.CTkLabel(ping_row, text="", text_color=C_DIM)
        self.ping_lbl.pack(side="left", padx=8)
        self._persona_open = False
        self._persona_btn = ctk.CTkButton(grid, text="人设 ▸", width=72, height=24,
                                          command=self._toggle_persona)
        self._persona_btn.grid(row=5, column=0, sticky="e", padx=(0, 8), pady=(2, 6))
        self.persona_box = ctk.CTkTextbox(grid, height=90)
        self._persona_text = persona or ""
        if self._persona_text:
            self.persona_box.insert("1.0", self._persona_text)

    def _toggle_persona(self):
        self._persona_open = not self._persona_open
        if self._persona_open:
            self.persona_box.grid(row=5, column=1, columnspan=3, sticky="ew", pady=(2, 8))
            self._persona_btn.configure(text="人设 ▾")
        else:
            self.persona_box.grid_remove()
            self._persona_btn.configure(text="人设 ▸")

    def get_persona(self):
        return self.persona_box.get("1.0", "end").strip()

    def _do_ping(self):
        self.ping_btn.configure(state="disabled")
        self.ping_lbl.configure(text="测试中…", text_color=C_DIM)

        def work():
            problems, cfg = None, None
            try:
                app = self.winfo_toplevel()
                problems, cfg = app.collect()
            except Exception as e:
                self.after(0, lambda: self._ping_done(False, str(e), 0))
                return
            sid = self.src_sid or sanitize_sid(self.title_var.get(), set(RESERVED_SIDS))
            if problems and sid not in (cfg.get("seats") or {}):
                self.after(0, lambda: self._ping_done(False, "配置不完整", 0))
                return
            cfg = council.expand_env(copy.deepcopy(cfg))
            ok, msg, elapsed = council.ping_seat(cfg, sid)
            self.after(0, lambda: self._ping_done(ok, msg, elapsed))

        threading.Thread(target=work, daemon=True).start()

    def _ping_done(self, ok, msg, elapsed):
        self.ping_btn.configure(state="normal")
        if ok:
            self.ping_lbl.configure(text=f"OK {elapsed}s", text_color=C_OK)
        else:
            self.ping_lbl.configure(text=f"失败 {elapsed}s {msg[:48]}", text_color=C_FAIL)

    def toggle_eye(self):
        hidden = self.key_entry.cget("show")
        self.key_entry.configure(show="" if hidden else "•")
        self.eye_btn.configure(text="隐藏" if hidden else "显示")


class RunCard:
    def __init__(self, master, sid, model, is_mod=False, on_retry=None, on_skip=None):
        self.sid = sid
        self.on_retry = on_retry
        self.on_skip = on_skip
        self.last_info = None
        self.frame = ctk.CTkFrame(master, corner_radius=6, border_width=1,
                                  border_color=C_BORDER, height=58)
        self.frame.grid_propagate(False)
        self.frame.grid_columnconfigure(0, weight=1)
        name = sid.replace("expert_", "").replace("moderator", "主持")
        ctk.CTkLabel(self.frame, text=name,
                     font=ctk.CTkFont(size=12, weight="bold"),
                     anchor="w").grid(row=0, column=0, sticky="ew",
                                      padx=(8, 4), pady=(6, 0))
        self.status = ctk.CTkLabel(self.frame, text="待命", text_color=C_DIM,
                                   width=64, anchor="e",
                                   font=ctk.CTkFont(size=12))
        self.status.grid(row=0, column=1, sticky="e", padx=2, pady=(6, 0))
        self.retry_btn = ctk.CTkButton(
            self.frame, text="重试", width=36, height=20, state="disabled",
            command=self._click_retry, font=ctk.CTkFont(size=11))
        self.retry_btn.grid(row=0, column=2, sticky="e", padx=1, pady=(6, 0))
        self.skip_btn = ctk.CTkButton(
            self.frame, text="跳过", width=36, height=20, state="disabled",
            command=self._click_skip, font=ctk.CTkFont(size=11))
        self.skip_btn.grid(row=0, column=3, sticky="e", padx=1, pady=(6, 0))
        self.time_lbl = ctk.CTkLabel(self.frame, text="0s", text_color=C_DIM,
                                     width=36, anchor="e",
                                     font=ctk.CTkFont(size=11))
        self.time_lbl.grid(row=0, column=4, sticky="e", padx=(2, 8), pady=(6, 0))
        self.detail = ctk.CTkLabel(self.frame, text="", text_color=C_DIM,
                                   anchor="w", height=16,
                                   font=ctk.CTkFont(size=11))
        self.detail.grid(row=1, column=0, columnspan=5, sticky="ew",
                         padx=8, pady=(0, 6))

    def _click_retry(self):
        if self.on_retry:
            self.on_retry(self.sid)

    def _click_skip(self):
        if self.on_skip:
            self.on_skip(self.sid)

    def update(self, info, spin):
        if info:
            self.last_info = dict(info)
        info = self.last_info
        if not info:
            self.status.configure(text="待命", text_color=C_DIM)
            self.retry_btn.configure(state="disabled")
            self.skip_btn.configure(state="disabled")
            self.detail.configure(text="")
            return
        st = info.get("status")
        el = int(info.get("elapsed") or 0)
        det = (info.get("detail") or "").replace("\n", " ")
        if len(det) > 36:
            det = det[:35] + "…"
        self.time_lbl.configure(text=f"{el}s")
        retryable = st in ("fail", "await_retry")
        active = st in ("running", "retrying", "fail", "await_retry")
        self.retry_btn.configure(state="normal" if retryable else "disabled")
        self.skip_btn.configure(state="normal" if active else "disabled")
        tin, tout = int(info.get("tok_in") or 0), int(info.get("tok_out") or 0)
        tok = f"  in {tin}/out {tout}" if (tin or tout) else ""
        if tok and det:
            det = (det[:28] + "…") if len(det) > 28 else det
            det = det + tok
        elif tok:
            det = tok.strip()
        if st == "running":
            self.status.configure(text=f"{spin} 推理中", text_color=C_RUN)
            self.detail.configure(text=det, text_color=C_DIM)
        elif st == "retrying":
            self.status.configure(text=f"↻ 重试 {info.get('attempt', 1)}/3",
                                  text_color=C_RETRY)
            self.detail.configure(text=det, text_color=C_RETRY)
        elif st == "done":
            self.status.configure(text="✔ 完成", text_color=C_OK)
            self.detail.configure(text=det, text_color=C_DIM)
        elif st in ("fail", "await_retry"):
            self.status.configure(text="✘ 失败", text_color=C_FAIL)
            self.detail.configure(text=det or "失败，可点重试", text_color=C_FAIL)
        else:
            self.status.configure(text="…", text_color=C_DIM)
            self.detail.configure(text=det, text_color=C_DIM)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("council 控制台 — 多模型研究议会")
        self.geometry("1180x820")
        self.minsize(980, 660)
        self.raw_cfg = {}
        self.orig_seats = {}
        self.editors = []
        self.mod_editor = None
        self.cards = {}
        self.spin_i = 0
        self.running = False
        self.last_out_dir = None
        self.bg_file = None
        self.run_control = None
        self._resume_dir = None
        self.last_snap = None
        self.retry_hub = None
        self.seat_states = {}
        self.phase_info = None
        self._build_ui()
        self._reload_config()

    def _build_ui(self):
        shell = ctk.CTkFrame(self, corner_radius=12)
        shell.pack(fill="both", expand=True, padx=14, pady=14)
        bar = ctk.CTkFrame(shell, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(12, 8))
        self.tab_var = ctk.StringVar(value="运行")
        self.tab_switch = ctk.CTkSegmentedButton(
            bar, values=["运行", "设置"], variable=self.tab_var,
            command=self._switch_tab, width=180)
        self.tab_switch.pack(side="left")
        self.theme_var = ctk.StringVar(value="深色")
        self.theme_switch = ctk.CTkSegmentedButton(
            bar, values=list(THEME_LABELS), variable=self.theme_var,
            command=self._set_theme, width=200)
        self.theme_switch.pack(side="right")
        self.page_host = ctk.CTkFrame(shell, fg_color="transparent")
        self.page_host.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.tab_run = ctk.CTkFrame(self.page_host, fg_color="transparent")
        self.tab_set = ctk.CTkFrame(self.page_host, fg_color="transparent")
        self._build_run_tab()
        self._build_settings_tab()
        self._switch_tab("运行")

    def _switch_tab(self, name):
        if name == "设置":
            self.tab_run.place_forget()
            self.tab_set.place(relx=0, rely=0, relwidth=1, relheight=1)
        else:
            self.tab_set.place_forget()
            self.tab_run.place(relx=0, rely=0, relwidth=1, relheight=1)

    def _set_theme(self, label):
        mode = THEME_MODES.get(label, "dark")
        ctk.set_appearance_mode(mode)
        try:
            cfg = load_raw()
            cfg.setdefault("ui", {})["appearance"] = mode
            council.write_text_retry(
                CONFIG_PATH,
                yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False,
                               default_flow_style=False, width=120))
            if isinstance(self.raw_cfg, dict):
                self.raw_cfg.setdefault("ui", {})["appearance"] = mode
        except Exception:
            pass

    def _build_settings_tab(self):
        outer = ctk.CTkScrollableFrame(self.tab_set, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=4, pady=4)
        canvas = getattr(outer, "_parent_canvas", None)
        if canvas is not None:
            canvas.configure(yscrollincrement=28)
        ctk.CTkLabel(outer, text="工作区工具（全局，只读）",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", pady=(0, 6))
        tools_box = ctk.CTkFrame(outer, corner_radius=8, border_width=1,
                         border_color=C_BORDER)
        tools_box.pack(fill="x", pady=(0, 12))
        row1 = ctk.CTkFrame(tools_box, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=(8, 4))
        self.tool_enabled = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row1, text="启用工具", variable=self.tool_enabled).pack(side="left")
        self.tool_read = ctk.BooleanVar(value=True)
        self.tool_list = ctk.BooleanVar(value=True)
        self.tool_grep = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(row1, text="read_file", variable=self.tool_read).pack(side="left", padx=10)
        ctk.CTkCheckBox(row1, text="list_dir", variable=self.tool_list).pack(side="left")
        ctk.CTkCheckBox(row1, text="grep", variable=self.tool_grep).pack(side="left", padx=10)
        ctk.CTkLabel(row1, text="max_rounds", text_color=C_DIM).pack(side="left", padx=(16, 4))
        self.tool_rounds = ctk.StringVar(value="8")
        ctk.CTkEntry(row1, textvariable=self.tool_rounds, width=48).pack(side="left")
        row2 = ctk.CTkFrame(tools_box, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(row2, text="工作区", text_color=C_DIM).pack(side="left")
        self.tool_ws = ctk.StringVar(value=".")
        ctk.CTkEntry(row2, textvariable=self.tool_ws,
                     placeholder_text="相对 council 目录或绝对路径").pack(
            side="left", fill="x", expand=True, padx=8)
        ctk.CTkButton(row2, text="浏览…", width=72, command=self.pick_workspace).pack(side="left")
        ctk.CTkLabel(outer, text="主持人（拆题 / 分歧识别 / 裁决）",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", pady=(0, 6))
        self.mod_slot = ctk.CTkFrame(outer, fg_color="transparent")
        self.mod_slot.pack(fill="x")
        ctk.CTkLabel(outer, text="", height=10).pack()
        hdr = ctk.CTkFrame(outer, fg_color="transparent")
        hdr.pack(fill="x", pady=(4, 6))
        self.exp_hdr = ctk.CTkLabel(hdr, text="专家席（0）",
                                    font=ctk.CTkFont(size=16, weight="bold"))
        self.exp_hdr.pack(side="left")
        ctk.CTkButton(hdr, text="+ 添加专家", width=104, height=28,
                      command=self.add_blank_expert).pack(side="right")
        ctk.CTkButton(hdr, text="全部 Ping", width=88, height=28,
                      command=self.ping_all).pack(side="right", padx=(0, 8))
        self.exp_slot = ctk.CTkFrame(outer, fg_color="transparent")
        self.exp_slot.pack(fill="x")
        bar = ctk.CTkFrame(outer, fg_color="transparent")
        bar.pack(fill="x", pady=14)
        ctk.CTkButton(bar, text="保存到 config.yaml", width=180,
                      command=self.save_config).pack(side="left")
        self.save_status = ctk.CTkLabel(bar, text="", text_color=C_OK)
        self.save_status.pack(side="left", padx=12)

    def _eff_of(self, exp_cfg, seat):
        ep = (exp_cfg.get("endpoints") or {}).get(seat.get("endpoint")) or {}
        return {
            "url": seat.get("base_url") or ep.get("base_url") or "",
            "key": seat.get("api_key") or ep.get("api_key") or "",
        }

    def _reload_config(self):
        self.raw_cfg = load_raw()
        mode = ((self.raw_cfg.get("ui") or {}).get("appearance") or "dark").lower()
        if mode not in ("dark", "light", "system"):
            mode = "dark"
        if ctk.get_appearance_mode().lower() != mode:
            ctk.set_appearance_mode(mode)
        if hasattr(self, "theme_var"):
            self.theme_var.set(MODE_LABELS.get(mode, "深色"))
        self.orig_seats = copy.deepcopy(self.raw_cfg["seats"])
        tcfg = self.raw_cfg.get("tools") or {}
        self.tool_enabled.set(bool(tcfg.get("enabled")))
        allow = set(tcfg.get("allow") or ["read_file", "list_dir", "grep"])
        self.tool_read.set("read_file" in allow)
        self.tool_list.set("list_dir" in allow)
        self.tool_grep.set("grep" in allow)
        self.tool_rounds.set(str(tcfg.get("max_rounds") or 8))
        self.tool_ws.set(tcfg.get("workspace") or ".")
        exp_cfg = council.expand_env(copy.deepcopy(self.raw_cfg))
        for w in self.mod_slot.winfo_children():
            w.destroy()
        for w in self.exp_slot.winfo_children():
            w.destroy()
        self.editors.clear()
        mod_seat = self.orig_seats.get("moderator") or {}
        mod_eff = self._eff_of(exp_cfg, mod_seat)
        self.mod_editor = SeatEditor(
            self.mod_slot, "moderator", eff=mod_eff,
            model=mod_seat.get("model", ""),
            orig={"url": mod_eff["url"], "key": mod_eff["key"]},
            persona=mod_seat.get("persona") or "")
        self.mod_editor.pack(fill="x")
        for sid, seat in self.orig_seats.items():
            if sid in RESERVED_SIDS:
                continue
            self._add_expert_editor(sid, seat, exp_cfg)
        self._update_exp_count()

    def _add_expert_editor(self, sid, seat, exp_cfg):
        ed = SeatEditor(self.exp_slot, sid, eff=self._eff_of(exp_cfg, seat),
                        model=seat.get("model", ""),
                        protocol=seat.get("protocol", "openai"),
                        editable_name=True, on_delete=self.remove_expert,
                        src_sid=sid,
                        orig={"url": self._eff_of(exp_cfg, seat)["url"],
                              "key": self._eff_of(exp_cfg, seat)["key"]},
                        persona=seat.get("persona") or "")
        ed.pack(fill="x", pady=5)
        self.editors.append(ed)

    def remove_expert(self, ed):
        if ed in self.editors:
            self.editors.remove(ed)
        ed.destroy()
        self._update_exp_count()

    def add_blank_expert(self):
        n = len(self.editors) + 1
        while True:
            candidate = sanitize_sid(f"expert_{n}", set())
            if not any(e.title_var.get() == candidate for e in self.editors):
                break
            n += 1
        ed = SeatEditor(self.exp_slot, candidate, editable_name=True,
                        on_delete=self.remove_expert,
                        orig={"url": "", "key": ""})
        ed.pack(fill="x", pady=5)
        self.editors.append(ed)
        self._update_exp_count()

    def _update_exp_count(self):
        self.exp_hdr.configure(text=f"专家席（{len(self.editors)}）")
        self._refresh_author_menu()

    def _refresh_author_menu(self):
        if not hasattr(self, "author_menu"):
            return
        ids = []
        if self.mod_editor:
            ids.append("moderator")
        for ed in self.editors:
            sid = ed.src_sid or ed.title_var.get().strip()
            if sid:
                ids.append(sid)
        if not ids:
            ids = ["-"]
        cur = self.author_var.get()
        self.author_menu.configure(values=ids)
        self.author_var.set(cur if cur in ids else ids[0])

    def collect(self):
        problems = []
        exp_cfg = council.expand_env(copy.deepcopy(self.raw_cfg))

        def src_ep(src_sid, field):
            src = self.orig_seats.get(src_sid) or {}
            ep = (exp_cfg.get("endpoints") or {}).get(src.get("endpoint")) or {}
            return ep.get(field) or ""

        cfg = copy.deepcopy(self.raw_cfg)
        med = self.mod_editor
        m_model = med.model_var.get().strip()
        m_url = med.url_var.get().strip()
        m_key = med.key_var.get().strip()
        if not m_model:
            problems.append("主持人: 模型名不能为空")
        mod = dict(self.orig_seats.get("moderator") or {})
        mod["role"] = "moderator"
        if m_model:
            mod["model"] = m_model
        ptxt = med.get_persona()
        if ptxt:
            mod["persona"] = ptxt
        url_changed = m_url != med.orig["url"]
        key_changed = m_key != med.orig["key"]
        if url_changed:
            if m_url:
                mod["base_url"] = m_url
            else:
                problems.append("主持人: Base URL 不能为空（如需还原请填回原值）")
        if key_changed:
            if m_key:
                mod["api_key"] = m_key
            else:
                problems.append("主持人: API Key 不能为空")
        if not (mod.get("base_url") or src_ep("moderator", "base_url")):
            problems.append("主持人: 缺少 Base URL")
        if not (mod.get("api_key") or src_ep("moderator", "api_key")):
            problems.append("主持人: 缺少 API Key")

        new_seats = {"moderator": mod}
        p5_src = self.orig_seats.get("moderator_p5")
        if p5_src is not None:
            p5 = copy.deepcopy(p5_src)
            p5["model"] = mod.get("model", p5.get("model"))
            if url_changed and mod.get("base_url"):
                p5["base_url"] = mod["base_url"]
            if key_changed and mod.get("api_key"):
                p5["api_key"] = mod["api_key"]
            new_seats["moderator_p5"] = p5

        taken = set(new_seats)
        for i, ed in enumerate(self.editors, 1):
            sid = sanitize_sid(ed.title_var.get(), taken)
            if not sid:
                problems.append(f"专家 #{i}: 名称无效（仅限小写字母/数字/下划线）")
                continue
            taken.add(sid)
            model = ed.model_var.get().strip()
            if not model:
                problems.append(f"{sid}: 模型名不能为空")
            url = ed.url_var.get().strip()
            key = ed.key_var.get().strip()
            seat = {"role": "expert"}
            if model:
                seat["model"] = model
            proto = ed.proto_var.get().strip() or "openai"
            if proto != "openai":
                seat["protocol"] = proto
            if url != ed.orig["url"]:
                if url:
                    seat["base_url"] = url
                else:
                    problems.append(f"{sid}: Base URL 被清空，请填写或还原原值")
            if key != ed.orig["key"]:
                if key:
                    seat["api_key"] = key
                else:
                    problems.append(f"{sid}: API Key 被清空，请填写或还原原值")
            src = self.orig_seats.get(ed.src_sid) if ed.src_sid else None
            final_url = seat.get("base_url") or (src or {}).get("base_url") \
                or src_ep(ed.src_sid, "base_url")
            final_key = seat.get("api_key") or (src or {}).get("api_key") \
                or src_ep(ed.src_sid, "api_key")
            if not final_url:
                problems.append(f"{sid}: 缺少 Base URL（席位与来源端点均未提供）")
            if not final_key:
                problems.append(f"{sid}: 缺少 API Key（席位与来源端点均未提供）")
            if src:
                if src.get("endpoint"):
                    seat["endpoint"] = src["endpoint"]
                if src.get("persona"):
                    seat["persona"] = src["persona"]
                for k in ("temperature", "max_tokens", "timeout"):
                    if k in src:
                        seat[k] = src[k]
            ptxt = ed.get_persona()
            if ptxt:
                seat["persona"] = ptxt
            if "persona" not in seat:
                seat["persona"] = PRESET_PERSONA
            new_seats[sid] = seat

        cfg["seats"] = new_seats
        ui = dict(self.raw_cfg.get("ui") or {})
        ui["appearance"] = THEME_MODES.get(self.theme_var.get(), "dark")
        cfg["ui"] = ui
        allow = []
        if self.tool_read.get():
            allow.append("read_file")
        if self.tool_list.get():
            allow.append("list_dir")
        if self.tool_grep.get():
            allow.append("grep")
        try:
            rounds = max(1, int(self.tool_rounds.get().strip() or 8))
        except ValueError:
            problems.append("工具: max_rounds 必须是正整数")
            rounds = 8
        prev = self.raw_cfg.get("tools") or {}
        cfg["tools"] = {
            "enabled": bool(self.tool_enabled.get()),
            "workspace": self.tool_ws.get().strip() or ".",
            "allow": allow,
            "max_rounds": rounds,
            "max_file_bytes": prev.get("max_file_bytes", 200000),
        }
        if cfg["tools"]["enabled"] and not allow:
            problems.append("工具已启用但未勾选任何工具")
        return problems, cfg

    def save_config(self):
        problems, cfg = self.collect()
        if problems:
            from tkinter import messagebox
            messagebox.showerror("配置有误", "\n".join(problems), parent=self)
            return
        council.write_text_retry(
            CONFIG_PATH,
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False,
                           default_flow_style=False, width=120))
        self.raw_cfg = cfg
        self.orig_seats = copy.deepcopy(cfg["seats"])
        if self.mod_editor:
            self.mod_editor.orig = {
                "url": self.mod_editor.url_var.get().strip(),
                "key": self.mod_editor.key_var.get().strip(),
            }
        saved_ids = [s for s in cfg["seats"] if s not in RESERVED_SIDS]
        for ed, sid in zip(self.editors, saved_ids):
            ed.src_sid = sid
            ed.orig = {
                "url": ed.url_var.get().strip(),
                "key": ed.key_var.get().strip(),
            }
        self.save_status.configure(
            text=f"已保存 {datetime.now():%H:%M:%S}", text_color=C_OK)

    def _build_run_tab(self):
        tab = self.tab_run
        ctl = ctk.CTkFrame(tab, fg_color="transparent")
        ctl.pack(fill="x", padx=12, pady=(10, 4))
        ctl.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(ctl, text="议题", anchor="e").grid(
            row=0, column=0, padx=(0, 8), sticky="e")
        self.topic_var = ctk.StringVar()
        ctk.CTkEntry(ctl, textvariable=self.topic_var, height=32,
                     placeholder_text="要辩论的议题…").grid(
            row=0, column=1, sticky="ew", padx=(0, 8))
        ctk.CTkButton(ctl, text="背景文件", width=88, height=32,
                      command=self.pick_bg).grid(row=0, column=2, padx=(0, 8))
        self.bg_lbl = ctk.CTkLabel(ctl, text="无", text_color=C_DIM,
                                   width=90, anchor="w")
        self.bg_lbl.grid(row=0, column=3, padx=(0, 10), sticky="w")
        self.dry_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(ctl, text="测试", variable=self.dry_var).grid(
            row=0, column=4, padx=(0, 10))
        self.start_btn = ctk.CTkButton(ctl, text="开始", width=80, height=32,
                                       command=self.start_run)
        self.start_btn.grid(row=0, column=5, padx=(0, 8), sticky="e")
        self.resume_btn = ctk.CTkButton(ctl, text="续跑…", width=72, height=32,
                                        command=self.pick_resume)
        self.resume_btn.grid(row=0, column=6, sticky="e")

        self.mode_row = ctk.CTkFrame(tab, fg_color="transparent")
        self.mode_row.pack(fill="x", padx=12, pady=(4, 0))
        self.mode_var = ctk.StringVar(value="辩论议会")
        self.mode_switch = ctk.CTkSegmentedButton(
            self.mode_row, values=["辩论议会", "方案评审"], variable=self.mode_var,
            command=self._on_mode, width=220)
        self.mode_switch.pack(side="left")
        self.review_frame = ctk.CTkFrame(tab, fg_color="transparent")
        rf = self.review_frame
        rf.grid_columnconfigure(1, weight=1)
        hint = ctk.CTkFont(size=12)
        ctk.CTkLabel(rf, text="方案", width=48, anchor="e").grid(row=0, column=0, padx=(0, 8), pady=(3, 0))
        self.scheme_var = ctk.StringVar(value="方案.md")
        ctk.CTkEntry(rf, textvariable=self.scheme_var, height=28,
                     placeholder_text="主笔要写并每轮覆盖的那份 .md").grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=(3, 0))
        ctk.CTkButton(rf, text="选择", width=64, height=28,
                      command=self.pick_scheme).grid(row=0, column=2, pady=(3, 0))
        row1 = ctk.CTkFrame(rf, fg_color="transparent")
        row1.grid(row=1, column=1, columnspan=2, sticky="w", pady=(0, 4))
        self.scheme_existing_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row1, text="既有方案（跳过主笔初稿，不覆盖原文）",
                        variable=self.scheme_existing_var, height=24).pack(side="left")
        ctk.CTkLabel(row1, text="改稿前自动备份旧版到讨论区",
                     text_color=C_DIM, font=hint).pack(side="left", padx=(10, 0))
        ctk.CTkLabel(rf, text="讨论区", width=48, anchor="e").grid(row=2, column=0, padx=(0, 8), pady=(3, 0))
        self.discuss_var = ctk.StringVar(value="讨论区")
        ctk.CTkEntry(rf, textvariable=self.discuss_var, height=28,
                     placeholder_text="各专家评审/打分 md 的文件夹").grid(
            row=2, column=1, sticky="ew", padx=(0, 8), pady=(3, 0))
        ctk.CTkButton(rf, text="选择", width=64, height=28,
                      command=self.pick_discuss).grid(row=2, column=2, pady=(3, 0))
        ctk.CTkLabel(rf, text="每轮每人一份，如 qwen-第1轮评审.md",
                     text_color=C_DIM, font=hint, anchor="w").grid(
            row=3, column=1, columnspan=2, sticky="w", pady=(0, 4))
        ctk.CTkLabel(rf, text="主笔", width=48, anchor="e").grid(row=4, column=0, padx=(0, 8), pady=3)
        self.author_var = ctk.StringVar(value="")
        self.author_menu = ctk.CTkOptionMenu(rf, values=["-"], variable=self.author_var, width=180)
        self.author_menu.grid(row=4, column=1, sticky="w", pady=3)
        ctk.CTkLabel(rf, text="加需求", width=48, anchor="e").grid(row=5, column=0, padx=(0, 8), pady=(3, 0))
        self.extra_var = ctk.StringVar()
        ctk.CTkEntry(rf, textvariable=self.extra_var, height=28,
                     placeholder_text="开始后才有效：临时补的需求，点插入").grid(
            row=5, column=1, sticky="ew", padx=(0, 8), pady=(3, 0))
        ctk.CTkButton(rf, text="插入", width=64, height=28,
                      command=self.inject_extra).grid(row=5, column=2, pady=(3, 0))
        ctk.CTkLabel(
            rf,
            text="会诊进行中填写并点插入，文字进入下一阶段，不打断当前正在跑的席位",
            text_color=C_DIM, font=hint, anchor="w").grid(
            row=6, column=1, columnspan=2, sticky="w", pady=(0, 4))

        head = ctk.CTkFrame(tab, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(8, 2))
        self.phase_lbl = ctk.CTkLabel(head, text="[待开始]",
                                      font=ctk.CTkFont(size=17, weight="bold"))
        self.phase_lbl.pack(side="left")
        self.stat_lbl = ctk.CTkLabel(head, text="", text_color=C_DIM)
        self.stat_lbl.pack(side="right")

        self.seat_area = ctk.CTkScrollableFrame(tab, height=150,
                                                fg_color="transparent")
        self.seat_area.pack(fill="x", expand=False, padx=10, pady=4)
        self.seat_area.grid_columnconfigure(0, weight=1, uniform="seat")
        self.seat_area.grid_columnconfigure(1, weight=1, uniform="seat")
        self.seat_area.grid_columnconfigure(2, weight=1, uniform="seat")
        self.seat_area.bind("<Configure>", self._sync_card_widths)

        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        bb = ctk.CTkFrame(bottom, fg_color="transparent")
        bb.pack(fill="x", pady=(0, 6))
        self.open_btn = ctk.CTkButton(bb, text="打开输出目录", width=120, height=28,
                                      state="disabled", command=self.open_out)
        self.open_btn.pack(side="left")
        self.run_status = ctk.CTkLabel(bb, text="", text_color=C_DIM)
        self.run_status.pack(side="left", padx=12)
        self.verdict_box = ctk.CTkTextbox(bottom, height=200)
        self.verdict_box.pack(fill="both", expand=True)
        self.verdict_box.insert("1.0", "裁决结果将显示在这里。")
        self._on_mode(self.mode_var.get())

    def _on_mode(self, name):
        if name == "方案评审":
            self.review_frame.pack(fill="x", padx=12, pady=(6, 0),
                                   after=self.mode_row)
        else:
            self.review_frame.pack_forget()

    def pick_scheme(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            parent=self, title="方案文件", defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("所有文件", "*.*")])
        if path:
            self.scheme_var.set(path)

    def pick_discuss(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(parent=self, title="讨论区目录")
        if path:
            self.discuss_var.set(path)

    def inject_extra(self):
        text = self.extra_var.get().strip()
        if not text:
            return
        if self.run_control and self.running:
            self.run_control.inject(text)
            self.extra_var.set("")
            self.run_status.configure(
                text="已插入：当前席位跑完后，下一阶段会带上这段需求", text_color=C_OK)
        else:
            self.run_status.configure(
                text="请先点开始，跑起来后再插入（不会打断当前席位）", text_color=C_DIM)

    def pick_workspace(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(parent=self, title="选择工具工作区")
        if path:
            self.tool_ws.set(path)

    def pick_bg(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self, title="选择背景材料",
            filetypes=[("Markdown/Txt", "*.md *.txt"), ("所有文件", "*.*")])
        if path:
            self.bg_file = path
            self.bg_lbl.configure(text=Path(path).name, text_color=C_RUN)

    def start_run(self):
        if self.running:
            if self.run_control:
                self.run_control.cancel()
                self.run_status.configure(text="正在取消（当前请求结束后停止）…",
                                          text_color=C_RETRY)
            return
        from tkinter import messagebox
        problems, cfg = self.collect()
        if problems:
            messagebox.showerror("配置有误", "\n".join(problems), parent=self)
            return
        topic = self.topic_var.get().strip()
        if not topic and not self._resume_dir:
            messagebox.showerror("缺少议题", "请先填写议会议题，或选择续跑目录。", parent=self)
            return
        cfg = council.expand_env(copy.deepcopy(cfg))
        is_review = self.mode_var.get() == "方案评审"
        args = argparse.Namespace(
            topic=topic, file=self.bg_file, config=str(CONFIG_PATH),
            experts=None, max_calls=80, dry_run=bool(self.dry_var.get()),
            quiet=False, no_live=False, workspace=None, no_tools=False,
            resume=self._resume_dir,
            mode="review" if is_review else "debate",
            scheme=self.scheme_var.get().strip() or None,
            scheme_existing=bool(self.scheme_existing_var.get()),
            discuss=self.discuss_var.get().strip() or None,
            author=self.author_var.get().strip() or None,
            reviewers=None)
        q = queue.Queue()
        progress = council.LiveProgress(
            total_phases=8 if is_review else 5, on_update=q.put)
        self.retry_hub = council.RetryHub(wait_sec=-1)
        args.retry_hub = self.retry_hub
        self.run_control = council.RunControl()
        args.control = self.run_control
        self.running = True
        self.start_btn.configure(text="取消", state="normal")
        self.resume_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.run_status.configure(text="运行中…", text_color=C_DIM)
        self.verdict_box.delete("1.0", "end")
        self.verdict_box.insert("1.0", "辩论进行中…")
        self.last_snap = None
        self.seat_states = {}
        self.phase_info = None
        self._build_cards(cfg)
        threading.Thread(target=self._worker, args=(args, cfg, progress, q),
                         daemon=True).start()
        self.after(100, lambda: self._poll(q))

    def _worker(self, args, cfg, progress, q):
        try:
            result = council.run(args, cfg=cfg, progress=progress)
            q.put({"__event__": "done", "result": result})
        except BaseException as e:
            q.put({"__event__": "error", "error": str(e)})

    def _build_cards(self, cfg):
        for w in self.seat_area.winfo_children():
            w.destroy()
        self.cards.clear()
        order = ["moderator"] + [s for s in cfg["seats"] if s not in RESERVED_SIDS]
        for i, sid in enumerate(order):
            seat = cfg["seats"][sid]
            card = RunCard(self.seat_area, sid, seat.get("model", ""),
                           is_mod=(sid == "moderator"),
                           on_retry=self._retry_seat,
                           on_skip=self._skip_seat)
            self.seat_area.grid_rowconfigure(i // 3, minsize=64)
            card.frame.grid(row=i // 3, column=i % 3, sticky="nsew", padx=4, pady=3)
            self.cards[sid] = card
        self._sync_card_widths()

    def _sync_card_widths(self, _event=None):
        try:
            w = int(self.seat_area.winfo_width())
        except Exception:
            return
        if w < 80:
            return
        col_w = max(200, (w - 36) // 3)
        for card in self.cards.values():
            card.frame.configure(width=col_w)

    def _remember_seat(self, sid, info):
        copied = dict(info)
        self.seat_states[sid] = copied
        if sid == "moderator_p5":
            self.seat_states["moderator"] = dict(copied)

    def _retry_seat(self, sid):
        if not self.retry_hub or not self.running:
            return
        self.retry_hub.signal(sid)
        if sid == "moderator":
            self.retry_hub.signal("moderator_p5")
        info = dict(self.seat_states.get(sid) or {})
        info["status"] = "retrying"
        info["detail"] = "手动重试"
        info["attempt"] = 1
        self.seat_states[sid] = info
        if sid in self.cards:
            self.cards[sid].update(info, SPIN_FRAMES[self.spin_i])

    def _skip_seat(self, sid):
        if not self.run_control or not self.running:
            return
        self.run_control.skip(sid)
        if self.retry_hub:
            self.retry_hub.signal(sid)
        info = dict(self.seat_states.get(sid) or {})
        info["status"] = "fail"
        info["detail"] = "已跳过"
        self.seat_states[sid] = info
        if sid in self.cards:
            self.cards[sid].update(info, SPIN_FRAMES[self.spin_i])

    def pick_resume(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(parent=self, title="选择要续跑的会话目录")
        if path:
            self._resume_dir = path
            self.run_status.configure(text=f"续跑: {Path(path).name}", text_color=C_DIM)

    def ping_all(self):
        eds = ([self.mod_editor] if self.mod_editor else []) + list(self.editors)
        for ed in eds:
            ed._do_ping()

    def _poll(self, q):
        finished = False
        try:
            try:
                while True:
                    item = q.get_nowait()
                    if isinstance(item, dict) and "__event__" in item:
                        finished = True
                        if item["__event__"] == "done":
                            self._finish_ok(item["result"])
                        else:
                            self._finish_err(item["error"])
                    elif isinstance(item, dict):
                        self.phase_info = item
                        for sid, info in (item.get("seats") or {}).items():
                            self._remember_seat(sid, info)
            except queue.Empty:
                pass
            self.spin_i = (self.spin_i + 1) % len(SPIN_FRAMES)
            sp = SPIN_FRAMES[self.spin_i]
            now = time.time()
            info = self.phase_info
            if info is not None:
                self.phase_lbl.configure(
                    text=f"[{info['phase_idx']}/{info['phase_total']}] {info['phase']}")
                toks = info.get("tokens") or {}
                tot = {"in": 0, "out": 0}
                for v in toks.values():
                    tot["in"] += int(v.get("in") or 0)
                    tot["out"] += int(v.get("out") or 0)
                tok_s = f" · token {tot['in']}/{tot['out']}" if tot["in"] or tot["out"] else ""
                self.stat_lbl.configure(
                    text=f"会话 {int(info['session_elapsed'])}s · "
                         f"调用 {info['calls_done']}/{info['max_calls']}{tok_s}")
            for sid, card in self.cards.items():
                st = self.seat_states.get(sid)
                if st and st.get("status") in ("running", "retrying", "await_retry"):
                    st = dict(st)
                    if st.get("start"):
                        st["elapsed"] = now - st["start"]
                card.update(st, sp)
        except Exception:
            pass
        if not finished:
            self.after(100, lambda: self._poll(q))

    def _reset_run_buttons(self):
        self.running = False
        self.start_btn.configure(text="开始", state="normal")
        self.resume_btn.configure(state="normal")
        self._resume_dir = None

    def _finish_ok(self, result):
        self._reset_run_buttons()
        verdict = result.get("verdict") or {}
        meta = result.get("meta") or {}
        self.verdict_box.delete("1.0", "end")
        body = format_verdict(verdict)
        toks = (meta.get("tokens") or {}).get("total") or {}
        if toks:
            body += f"\n【Token】 in {toks.get('in', 0)} / out {toks.get('out', 0)} / 调用 {toks.get('calls', 0)}\n"
        if meta.get("incomplete"):
            body = "（本场未完成）" + (f" {meta.get('error')}" or "") + "\n\n" + body
        self.verdict_box.insert("1.0", body)
        session = meta.get("session")
        if session:
            self.last_out_dir = BASE / "out" / session
            elapsed = meta.get("elapsed_sec", "?")
            tag = "未完成" if meta.get("incomplete") else "完成"
            self.run_status.configure(
                text=f"{tag}，用时 {elapsed}s · 输出: {self.last_out_dir}",
                text_color=C_RETRY if meta.get("incomplete") else C_OK)
            self.open_btn.configure(state="normal")
        self.phase_lbl.configure(text="[未完成]" if meta.get("incomplete") else "[完成]")

    def _finish_err(self, err):
        self._reset_run_buttons()
        self.run_status.configure(text=f"失败: {err[:160]}", text_color=C_FAIL)
        self.verdict_box.delete("1.0", "end")
        self.verdict_box.insert("1.0", f"会话失败:\n{err}")
        from tkinter import messagebox
        messagebox.showerror("会话失败", str(err)[:800], parent=self)

    def open_out(self):
        if self.last_out_dir and Path(self.last_out_dir).exists():
            os.startfile(str(self.last_out_dir))


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

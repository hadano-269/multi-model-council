# -*- coding: utf-8 -*-
"""council 安装向导。

两条通道、同一套问答语义：
- python council.py --wizard                 人类在终端逐题作答；
- python council.py --wizard-profile p.yaml  AI Agent 按同一问题清单向用户收集后无头应用。

密钥安全约定（与 GUI 一致）：新密钥只进用户级环境变量（setx），
config.yaml 里仅落 ${VAR:-} 引用模板。profile 文件中禁止出现明文 key，
一律引用环境变量名（api_key_var），向导发现未设置时会引导补录。
"""
import copy
import getpass
import sys

import yaml

import council
import keybridge as _kb
import presets

MIN_EXPERTS, MAX_EXPERTS = 1, 8

# 各职责预设的顺手默认型号（仅交互菜单回车采纳，不影响 profile 通道）
SUGGEST_MODEL = {
    "designer_experiments": "kimi-k3",
    "analyst_boundary": "deepseek-v4-pro",
    "evaluator_engineering": "gpt-5",
    "researcher_independent": "claude-opus-5",
}


# ---------------------------------------------------------------- 校验与构建

def _endpoint_entry(endpoint_id, item):
    """解析 profile 条目的端点：返回 (生效端点id, 要写入endpoints的条目|None)"""
    if endpoint_id in ("custom", "") or not endpoint_id:
        url = (item.get("custom_base_url") or "").strip()
        if not url:
            raise ValueError(f"{item.get('name', '?')}: custom_base_url 缺失"
                             "（endpoint_id=custom 时必须提供）")
        base = _kb.sanitize_var_base(item.get("name") or "cust").lower()
        eid = f"custom_{base}"
        var = _kb.resolve_key_var("", item.get("name") or "cust")
        return eid, {"base_url": url, "api_key": "${" + var + ":-}"}
    if endpoint_id in ("opencode_go", "ccswitch", "ark_plan"):
        return endpoint_id, None
    raise ValueError(f"{item.get('name', '?')}: 未知 endpoint_id={endpoint_id}"
                     "（可选 opencode_go / ccswitch / ark_plan / custom）")


def _seat_item(profile_item, is_moderator):
    model = (profile_item.get("model") or "").strip()
    if not model:
        raise ValueError(f"{profile_item.get('name', 'moderator')}: model 不能为空")
    eid, ep_entry = _endpoint_entry(profile_item.get("endpoint_id"),
                                    {**profile_item, "name": profile_item.get(
                                        "name") or "moderator"})
    seat = {"role": "moderator" if is_moderator else "expert"}
    seat["model"] = model
    seat["endpoint"] = eid
    if profile_item.get("protocol"):
        seat["protocol"] = profile_item["protocol"]
    ptext = (profile_item.get("persona_text") or "").strip()
    if not ptext and not is_moderator:
        ptext = presets.persona_of(profile_item.get("role_preset"))
    if ptext:
        seat["persona"] = ptext
    return seat, ep_entry


def key_vars_in_profile(profile):
    """所有将被引用的环境变量名（供调用方逐一检查 os.environ 是否就绪）。"""
    items = [("MODERATOR", profile.get("moderator") or {})]
    items += [(e.get("name") or f"expert{i}", e)
              for i, e in enumerate((profile.get("experts") or []), 1)]
    out = []
    for hint, it in items:
        var = it.get("api_key_var") or (
            _kb.KNOWN_ENDPOINT_VARS.get(it.get("endpoint_id"))
            or _kb.resolve_key_var(it.get("endpoint_id"), hint))
        if var not in out:
            out.append(var)
    return out


def validate_profile(profile):
    errs = []
    if not isinstance(profile.get("moderator"), dict):
        errs.append("缺少 moderator 配置节")
    exps = profile.get("experts") or []
    if not (MIN_EXPERTS <= len(exps) <= MAX_EXPERTS):
        errs.append(f"专家数量须在 {MIN_EXPERTS}-{MAX_EXPERTS} 位之间，当前 {len(exps)}")
    try:
        n = int(profile.get("expert_count") or len(exps))
        if n != len(exps) and profile.get("expert_count"):
            errs.append("expert_count 与 experts 列表长度不一致")
    except (TypeError, ValueError):
        pass
    names = set()
    for e in exps:
        nm = (e.get("name") or "").strip()
        if not re_sid_ok(nm):
            errs.append(f"专家名称无效: {nm!r}（小写字母/数字/下划线）")
        elif nm in names or nm == "moderator":
            errs.append(f"专家名称重复或占用保留字: {nm}")
        names.add(nm)
        if not (e.get("model") or "").strip():
            errs.append(f"{nm}: model 为空")
        rp = e.get("role_preset")
        if rp and rp not in presets.preset_keys():
            errs.append(f"{nm}: 未知 role_preset={rp}")
    return errs


def re_sid_ok(name):
    import re
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name or ""))


def build_from_profile(profile, base_cfg):
    """把 profile 应用到既有配置骨架上：返回 (cfg, bridge_plan{var:val}, notes[])。
    bridge_plan 只包含『当前进程/系统尚未就绪』的变量——已设置的直接沿用。"""
    errs = validate_profile(profile)
    if errs:
        raise ValueError("\n".join(errs))
    cfg = copy.deepcopy(base_cfg)
    cfg.setdefault("endpoints", {})
    cfg["seats"] = {}
    bridges, notes = {}, []

    def handle(item, is_mod, fallback_hint):
        eid, ep_new = _endpoint_entry(
            item.get("endpoint_id"), {**item, "name": item.get("name") or fallback_hint})
        if ep_new:
            cfg["endpoints"][eid] = ep_new
        seat, _ = _seat_item({**item,
                              "name": item.get("name") or fallback_hint}, is_mod)
        seat["endpoint"] = eid
        var = item.get("api_key_var") or (
            _kb.KNOWN_ENDPOINT_VARS.get(eid) or _kb.resolve_key_var(eid, fallback_hint))
        seat["api_key"] = "${" + var + ":-}"
        return seat, var

    m_seat, m_var = handle(profile["moderator"], True, "MODERATOR")
    cfg["seats"]["moderator"] = m_seat
    if not m_seat.get("persona"):
        m_seat["persona"] = presets.persona_of("moderator")
    p5_src = (base_cfg.get("seats") or {}).get("moderator_p5")
    if p5_src is not None or profile.get("include_moderator_p5", True):
        cfg["seats"]["moderator_p5"] = {
            "endpoint": m_seat["endpoint"], "model": m_seat["model"],
            "role": "moderator", "api_key": m_seat["api_key"],
            "persona": presets.persona_of("moderator").replace(
                "的主持人。职责：把议题拆解为值得激辩的关键论题、交叉评审后精准定位分歧、\n\n"
                "仅对分歧点发起定向追问、最终输出结构化裁决。",
                "的终局裁决主持人。职责：综合全部辩论材料，输出结构化裁决。"),
        }

    seen = set()
    for i, e in enumerate(profile["experts"], 1):
        nm = e["name"].strip()
        seen.add(nm)
        seat, var = handle(e, False, nm)
        cfg["seats"][nm] = seat

    for var in key_vars_in_profile(profile):
        if os_environ_get(var):
            continue
        val = (profile.get("_interactive_keys") or {}).get(var)
        if val:
            bridges[var] = val
        else:
            notes.append(f"[todo] 用户环境变量 {var} 尚未设置。"
                         f"请在终端执行: setx {var} \"<密钥>\" 后重开终端；"
                         "或重新运行 --wizard 完成交互补录。")
    return cfg, bridges, notes


def os_environ_get(var):
    import os
    return os.environ.get(var)


def dump_yaml(cfg):
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=120)


def write_config(cfg, config_path):
    council.write_text_retry(config_path, dump_yaml(cfg))


def merge_preserve(base_loaded_cfg):
    """load_raw 骨架兜底，避免全新环境缺段。"""
    base_loaded_cfg.setdefault("defaults", {"protocol": "openai", "temperature": 0.7,
                                            "max_tokens": 16384, "timeout": 300})
    base_loaded_cfg.setdefault("tools", {
        "enabled": False, "workspace": ".",
        "allow": ["read_file", "list_dir", "grep"],
        "max_rounds": 8, "max_file_bytes": 200000})
    base_loaded_cfg.setdefault("ui", {})
    return base_loaded_cfg


# ---------------------------------------------------------------- 无头通道

def run_profile(profile_path, config_path):
    raw = open(profile_path, encoding="utf-8").read()
    profile = yaml.safe_load(raw)
    if not isinstance(profile, dict):
        sys.exit("profile 文件不是合法的映射结构")
    base = council.load_config(config_path) if council.Path(config_path).exists() \
        else merge_preserve({})
    base.setdefault("seats", {})
    cfg, bridges, notes = build_from_profile(profile, merge_preserve(copy.deepcopy(base)))
    for var, val in bridges.items():
        err = _kb.apply_bridge(var, val)
        if err:
            sys.exit(f"环境变量写入失败: {err}")
    write_config(cfg, config_path)
    print("[wizard] 配置已写入:", config_path)
    for n in notes:
        print("[wizard]", n)
    print("[wizard] 下一步自检: python council.py --list && "
          "python council.py \"冒烟\" --dry-run --quiet")


# ---------------------------------------------------------------- 交互通道

def _ask(prompt, default=""):
    suffix = f" [{default}]" if default != "" else ""
    v = input(f"{prompt}{suffix}: ").strip()
    return v or default


def _ask_secret(var):
    if os_environ_get(var):
        print(f"  · {var} 已存在于环境变量（{_kb.mask_value(os_environ_get(var))}），回车沿用")
        getpass.getpass("")
        return None
    while True:
        v = getpass.getpass(f"  粘贴 {var} 的 API Key（不回显，回车跳过稍后 setx）: ").strip()
        if not v:
            return None
        if len(v) < 12:
            print("  · 长度不足 12，疑似不完整，请重新输入")
            continue
        return v


def _choose_endpoint(default_ep):
    opts = [("opencode_go", "OpenCode Zen 聚合"),
            ("ccswitch", "本地 CC Switch 代理"),
            ("ark_plan", "火山方舟"),
            ("custom", "自定义 Base URL")]
    for i, (k, d) in enumerate(opts, 1):
        mark = "*" if k == default_ep else " "
        print(f"   {mark}{i}. {d} ({k})")
    pick = _ask("  选择编号", "1")
    try:
        return opts[int(pick) - 1][0]
    except (ValueError, IndexError):
        return default_ep


def _choose_role(prev):
    items = [(k, lbl) for k, lbl in presets.preset_labels()
             if ROLE_SCOPE_FILTER(k)]
    for i, (k, lbl) in enumerate(items, 1):
        mark = "*" if k == prev else " "
        print(f"   {mark}{i}. {lbl}")
    pick = _ask("  选择编号", "1")
    try:
        return items[int(pick) - 1][0]
    except (ValueError, IndexError):
        return prev or items[0][0]


def ROLE_SCOPE_FILTER(key):
    return True        # 两个流水线共用全部预设


def run_interactive(config_path):
    print("=" * 62)
    print("council 安装向导 —— 回车即采用 [] 中的默认值；Ctrl+C 随时退出")
    print("=" * 62)
    base = council.load_config(config_path) if council.Path(config_path).exists() \
        else merge_preserve({})
    base.setdefault("seats", {})
    old_seats = base["seats"]
    prev_mod = ((old_seats.get("moderator") or {}).get("model")) or "glm-5.3"

    print("\n【主持人】")
    mod_model = _ask("模型名", prev_mod)
    mod_ep = _choose_endpoint(((old_seats.get("moderator") or {}).get("endpoint"))
                              or "opencode_go")
    mod_var = (_kb.KNOWN_ENDPOINT_VARS.get(mod_ep)
               or _kb.resolve_key_var(mod_ep, "MODERATOR"))
    mod_secret = _ask_secret(mod_var)
    profile = {"version": 1,
               "moderator": {"model": mod_model, "endpoint_id": mod_ep},
               "include_moderator_p5": True, "experts": [],
               "_interactive_keys":
                   ({mod_var: mod_secret} if mod_secret else {})}

    n_raw = _ask("\n【专家】数量（1-8）",
                 str(max(1, len([s for s in old_seats
                                 if s not in ("moderator", "moderator_p5")])) or 4))
    try:
        n = max(MIN_EXPERTS, min(MAX_EXPERTS, int(n_raw)))
    except ValueError:
        n = 4
    prev_role, prev_ep, idx = None, mod_ep, 0
    while len(profile["experts"]) < n:
        idx += 1
        print(f"\n【专家 #{idx}】")
        name = _ask("席位名(英文)", f"expert_{idx}")
        if not council.re.match(r"[a-z][a-z0-9_]{0,31}", name) or \
                name in profile["seats"] or any(x["name"] == name for x in profile["experts"]):
            print("  · 名称非法或重复，请重命名")
            idx -= 1
            continue
        role = _choose_role(prev_role)
        model = _ask("模型名", SUGGEST_MODEL.get(role, ""))
        ep = _choose_endpoint(prev_ep)
        secret = _ask_secret(_kb.KNOWN_ENDPOINT_VARS.get(ep)
                             or _kb.resolve_key_var(ep, name))
        if secret:
            profile["_interactive_keys"][
                _kb.KNOWN_ENDPOINT_VARS.get(ep) or _kb.resolve_key_var(ep, name)] = secret
        item = {"name": name, "model": model, "endpoint_id": ep}
        if ep == "custom":
            item["custom_base_url"] = _ask("  自定义 Base URL", "")
        if role:
            item["role_preset"] = role
        profile["experts"].append(item)
        prev_role, prev_ep = role, ep

    print("\n──────── 生效预览 ────────")
    print(f"主持人: {mod_model} @ {profile['moderator']['endpoint_id']}")
    for e in profile["experts"]:
        print(f"· {e['name']:<14} {e['model']:<18} @ {e['endpoint_id']}"
              f"  角色={presets.ROLE_PRESETS.get(e.get('role_preset'), {}).get('label', '自定义')}")
    pending = [v for v in key_vars_in_profile(profile) if not os_environ_get(v)]
    todo_bridge = bool(profile["_interactive_keys"])
    if _ask("\n确认写入配置文件? (y/N)", "N").lower() != "y":
        print("已取消，未做任何更改。")
        return

    cfg, bridges, notes = build_from_profile(profile, merge_preserve(copy.deepcopy(base)))
    failed = [err for err in
              (_kb.apply_bridge(v, s) for v, s in bridges.items()) if err]
    write_config(cfg, config_path)
    print("[wizard] 配置已原子写入:", config_path)
    if failed:
        for f in failed:
            print("[wizard][失败]", f)
    elif bridges:
        print("[wizard] 环境变量已更新:", ", ".join(sorted(bridges)))
    for n in notes:
        print("[wizard]", n)
    print("[wizard] 自检: python council.py --list  |  单席: python council.py "
          "--ping <席位>  |  全流程: python council.py \"议题\" --dry-run --quiet")

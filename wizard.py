# -*- coding: utf-8 -*-
"""council 安装向导。

两条通道、同一套问答语义：
- python council.py --wizard                 人类在终端逐题作答；
- python council.py --wizard-profile p.yaml  AI Agent 按同一问题清单收集后无头应用。

密钥策略（2024-08 起）：config.yaml 已 gitignore，密钥直接明文写在文件里即可，
不再走环境变量桥接。profile 中允许 api_key 字段携带明文（写入本地文件不入库）；
留空则沿用现有配置中的同名席位 key，全新席位留空会在 notes 中提示 LIVE 会失败。
"""
import copy
import getpass
import re
import sys

import yaml

import council
import presets

MIN_EXPERTS, MAX_EXPERTS = 1, 8

SUGGEST_MODEL = {
    "designer_experiments": "kimi-k3",
    "analyst_boundary": "deepseek-v4-pro",
    "evaluator_engineering": "gpt-5",
    "researcher_independent": "claude-opus-5",
}

ENDPOINT_WHITELIST = ("opencode_go", "ccswitch", "ark_plan")


def re_sid_ok(name):
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name or ""))


def _endpoint_entry(item, fallback_hint):
    """解析条目端点：返回 (生效端点id, 需写入endpoints的新条目|None)"""
    eid = item.get("endpoint_id")
    if eid in ENDPOINT_WHITELIST:
        return eid, None
    if eid in (None, "", "custom"):
        url = (item.get("custom_base_url") or "").strip()
        if not url:
            raise ValueError(f"{item.get('name') or fallback_hint}: "
                             "endpoint_id=custom 时必须提供 custom_base_url")
        base = re.sub(r"[^a-z0-9]+", "_",
                      (item.get("name") or fallback_hint).lower()).strip("_")
        return f"custom_{base or 'node'}", None
    raise ValueError(f"{item.get('name') or fallback_hint}: 未知 endpoint_id={eid}"
                     "（可选 opencode_go / ccswitch / ark_plan / custom）")


def _seat_from(item, is_moderator, base_seats, fallback_hint):
    name = (item.get("name") or ("moderator" if is_moderator else fallback_hint))
    prev = (base_seats or {}).get(name) or {}
    model = (item.get("model") or "").strip()
    if not model:
        raise ValueError(f"{name}: model 不能为空")
    eid, ep_new = _endpoint_entry(item, fallback_hint)
    seat = {"role": "moderator" if is_moderator else "expert", "model": model}
    # 密钥：profile 显式给 → 用之；否则沿用既有配置同名席位的 key
    key = str(item.get("api_key") or "").strip() or prev.get("api_key", "")
    if key:
        seat["api_key"] = key
    if item.get("protocol"):
        seat["protocol"] = item["protocol"]
    seat["endpoint"] = eid
    ptext = (item.get("persona_text") or "").strip()
    if not ptext:
        ptext = presets.persona_of(item.get("role_preset")) if not is_moderator else ""
    if ptext:
        seat["persona"] = ptext
    return seat, ep_new


def validate_profile(profile):
    errs = []
    if not isinstance(profile.get("moderator"), dict):
        errs.append("缺少 moderator 配置节")
    exps = profile.get("experts") or []
    if not (MIN_EXPERTS <= len(exps) <= MAX_EXPERTS):
        errs.append(f"专家数量须在 {MIN_EXPERTS}-{MAX_EXPERTS} 位之间，当前 {len(exps)}")
    declared = profile.get("expert_count")
    if declared is not None:
        try:
            if int(declared) != len(exps):
                errs.append("expert_count 与 experts 列表长度不一致")
        except (TypeError, ValueError):
            errs.append("expert_count 必须是整数")
    names = set()
    for e in exps:
        nm = (e.get("name") or "").strip()
        if not re_sid_ok(nm):
            errs.append(f"专家名称无效: {nm!r}（小写字母开头，仅字母/数字/下划线）")
        elif nm in names or nm == "moderator":
            errs.append(f"专家名称重复或占用保留字: {nm}")
        names.add(nm)
        if not (e.get("model") or "").strip():
            errs.append(f"{nm}: model 为空")
        rp = e.get("role_preset")
        if rp and rp not in presets.ROLE_PRESETS:
            errs.append(f"{nm}: 未知 role_preset={rp}")
    return errs


def build_from_profile(profile, base_cfg):
    """应用 profile 到配置骨架：返回 (cfg, notes[])。
    明文密钥直接写入 seats；profile 未提及的席位 key 沿用既有配置。"""
    errs = validate_profile(profile)
    if errs:
        raise ValueError("\n".join(errs))
    cfg = copy.deepcopy(base_cfg)
    cfg.setdefault("endpoints", {})
    cfg["seats"] = {}
    base_seats = (base_cfg.get("seats") or {})
    notes = []

    mod_seat, _ = _seat_from(profile["moderator"], True, base_seats, "MODERATOR")
    if not mod_seat.get("persona"):
        mod_seat["persona"] = presets.persona_of("moderator")
    cfg["seats"]["moderator"] = mod_seat
    if profile.get("include_moderator_p5", True):
        cfg["seats"]["moderator_p5"] = {
            "endpoint": mod_seat["endpoint"],
            "model": mod_seat["model"],
            "role": "moderator",
            "api_key": mod_seat.get("api_key", ""),
            "persona": presets.persona_of("moderator").replace(
                "的主持人。职责：把议题拆解为值得激辩的关键论题、交叉评审后精准定位分歧、\n\n"
                "仅对分歧点发起定向追问、最终输出结构化裁决。",
                "的终局裁决主持人。职责：综合全部辩论材料，输出结构化裁决。"),
        }

    for e in profile["experts"]:
        nm = e["name"].strip()
        seat, _ = _seat_from(e, False, base_seats, nm)
        if not seat.get("persona"):
            seat["persona"] = presets.ROLE_PRESETS.get(
                e.get("role_preset"), {}).get("persona") or presets.persona_of(
                "researcher_independent")
        cfg["seats"][nm] = seat
        if not seat.get("api_key"):
            notes.append(f"[提示] 席位 {nm} 未配置 api_key，LIVE 调用会失败"
                         "（--dry-run 不受影响）。")

    # 端点表清理：未被任何席位引用且模板性的自定义端点不再保留
    used = {s.get("endpoint") for s in cfg["seats"].values()}
    for k in list(cfg["endpoints"]):
        if k not in used and k not in ("opencode_go", "ccswitch", "ark_plan"):
            del cfg["endpoints"][k]
    return cfg, notes


def merge_preserve(base_cfg):
    base_cfg.setdefault("defaults", {"protocol": "openai", "temperature": 0.7,
                                     "max_tokens": 16384, "timeout": 300})
    base_cfg.setdefault("tools", {
        "enabled": False, "workspace": ".",
        "allow": ["read_file", "list_dir", "grep"],
        "max_rounds": 8, "max_file_bytes": 200000})
    base_cfg.setdefault("ui", {})
    return base_cfg


def dump_yaml(cfg):
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=120)


def write_config(cfg, config_path):
    council.write_text_retry(config_path, dump_yaml(cfg))


def run_profile(profile_path, config_path):
    profile = yaml.safe_load(
        Path(profile_path).read_text(encoding="utf-8"))
    if not isinstance(profile, dict):
        sys.exit("profile 文件不是合法的映射结构")
    base = council.load_config(config_path) if council.Path(config_path).exists() \
        else merge_preserve({})
    base.setdefault("seats", {})
    cfg, notes = build_from_profile(profile, merge_preserve(copy.deepcopy(base)))
    write_config(cfg, config_path)
    print("[wizard] 配置已写入:", config_path)
    for n in notes:
        print("[wizard]", n)
    print("[wizard] 自检: python council.py --list && "
          "python council.py \"冒烟\" --dry-run --quiet")


# ---------------------------------------------------------------- 交互通道

def _ask(prompt, default=""):
    suffix = f" [{default}]" if default != "" else ""
    v = input(f"{prompt}{suffix}: ").strip()
    return v or default


def _ask_secret(var_hint):
    while True:
        v = getpass.getpass(f"  粘贴 {var_hint} 的 API Key（不回显，回车跳过）: ").strip()
        if not v:
            return ""
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
    items = presets.preset_labels()
    for i, (k, lbl) in enumerate(items, 1):
        mark = "*" if k == prev else " "
        print(f"   {mark}{i}. {lbl}")
    pick = _ask("  选择编号", "1")
    try:
        return items[int(pick) - 1][0]
    except (ValueError, IndexError):
        return prev or items[0][0]


def run_interactive(config_path):
    print("=" * 62)
    print("council 安装向导 —— 回车即采用 [] 中的默认值；Ctrl+C 随时退出")
    print("密钥将明文写入本机 config.yaml（该文件已被 gitignore，不会上传）")
    print("=" * 62)
    base = council.load_config(config_path) if council.Path(config_path).exists() \
        else merge_preserve({})
    base.setdefault("seats", {})
    old_seats = base["seats"]
    prev_mod = (old_seats.get("moderator") or {}).get("model") or "glm-5.3"

    print("\n【主持人】")
    mod_model = _ask("模型名", prev_mod)
    mod_ep = _choose_endpoint((old_seats.get("moderator") or {}).get("endpoint")
                              or "opencode_go")
    mod_key = ""
    if mod_ep != "ccswitch":
        mod_key = _ask_secret("主持人")
    profile = {"version": 1,
               "moderator": {"model": mod_model, "endpoint_id": mod_ep,
                             "api_key": mod_key},
               "include_moderator_p5": True, "experts": []}

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
        if not re_sid_ok(name) or name in profile["seats"] or \
                any(x["name"] == name for x in profile["experts"]):
            print("  · 名称非法或重复，请重命名")
            idx -= 1
            continue
        role = _choose_role(prev_role)
        model = _ask("模型名", SUGGEST_MODEL.get(role, ""))
        ep = _choose_endpoint(prev_ep)
        key = "" if ep == "ccswitch" else _ask_secret(name)
        item = {"name": name, "model": model, "endpoint_id": ep, "api_key": key}
        if ep == "custom":
            item["custom_base_url"] = _ask("  自定义 Base URL", "")
        item["role_preset"] = role
        profile["experts"].append(item)
        prev_role, prev_ep = role, ep

    print("\n──────── 生效预览 ────────")
    print(f"主持人: {mod_model} @ {profile['moderator']['endpoint_id']}")
    for e in profile["experts"]:
        print(f"· {e['name']:<16} {e['model']:<18} @ {e['endpoint_id']}"
              f"  角色={presets.ROLE_PRESETS.get(e.get('role_preset'), {}).get('label', '自定义')}")
    if _ask("\n确认写入 config.yaml? (y/N)", "N").lower() != "y":
        print("已取消，未做任何更改。")
        return

    cfg, notes = build_from_profile(profile, merge_preserve(copy.deepcopy(base)))
    write_config(cfg, config_path)
    print("[wizard] 配置已原子写入:", config_path)
    for n in notes:
        print("[wizard]", n)
    print("[wizard] 自检: python council.py --list  |  "
          "单席: python council.py --ping <席位>  |  "
          "全流程: python council.py \"议题\" --dry-run --quiet")

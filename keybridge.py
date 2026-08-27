"""密钥桥接共享层：GUI 设置页与 --wizard 向导共用。

约定：用户输入的新密钥一律写入用户级环境变量（setx），config 里只保留
`${VAR:-}` 引用模板。此模块只负责变量名解析与写入动作，不接触持久化。
"""
import os
import subprocess

# 已知端点 -> 约定环境变量名
KNOWN_ENDPOINT_VARS = {
    "ark_plan": "ARK_API_KEY",
    "opencode_go": "GO_API_KEY",
    "ccswitch": "CC_SWITCH_API_KEY",
}

# 与 council._SECRET_LIKE 同规则：识别既有配置里误写的明文密钥以便自动迁移
SECRET_LIKE_RE_RAW = r"\b(?:sk|ark)-[A-Za-z0-9_-]{24,}"


def sanitize_var_base(name):
    """席位名/标签 -> 大写下划线基础串（用于合成变量名）。"""
    import re
    s = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").upper()).strip("_")
    return s or "CUSTOM"


def resolve_key_var(endpoint_id, seat_hint=""):
    """席位生效端点 -> 约定环境变量名；未知端点回退为按席位生成的专属变量。"""
    v = KNOWN_ENDPOINT_VARS.get(endpoint_id or "")
    if v:
        return v
    return f"{sanitize_var_base(seat_hint)}_API_KEY"


def mask_value(value):
    return f"{value[:5]}***" if value else "(未设)"


def key_env_hint(var):
    """输入框占位文案：向用户说明当前凭据来源与覆盖方式。"""
    loaded = os.environ.get(var)
    if loaded:
        return f"环境变量 {var} 已设（{mask_value(loaded)}）；留空沿用，输入新值可覆盖"
    return f"粘贴密钥（保存至用户环境变量 {var}，不写入 config.yaml）"


def apply_bridge(var, value):
    """setx 写入用户环境变量并注入当前进程；失败返回错误串，成功返回 None。
    页内 Ping / 即时联动因此无需重启 GUI 或终端。"""
    r = subprocess.run(["setx", var, value], capture_output=True, text=True)
    if r.returncode != 0:
        return f"{var}: {r.stderr.strip()}"
    os.environ[var] = value
    return None


def migrate_literal_if_secret(raw_value):
    """旧配置里的明文字面量原样返回（供上游按新值桥接）；非明文返回 None。"""
    import re
    v = (raw_value or "").strip()
    return v if v and re.search(SECRET_LIKE_RE_RAW, v) else None

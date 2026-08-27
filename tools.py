"""工作区只读工具：read_file / list_dir / grep，路径限制在 workspace 内。"""
import json
import re
from pathlib import Path

TOOL_NAMES = ("read_file", "list_dir", "grep")
SKIP_DIRS = {".git", "__pycache__", ".omo", "out", "node_modules", ".venv", "venv", ".idea"}
MAX_LIST = 200
MAX_GREP = 80


def tool_detail(name, args):
    args = args or {}
    if name == "read_file":
        return f"查阅 {args.get('path', '')}"
    if name == "list_dir":
        return f"列出 {args.get('path', '.')}"
    if name == "grep":
        return f"搜索 {args.get('pattern', '')}"
    return name


class ToolRunner:
    def __init__(self, workspace, allow=None, max_file_bytes=200000):
        self.root = Path(workspace).resolve()
        allow = list(allow or TOOL_NAMES)
        self.allow = {n for n in allow if n in TOOL_NAMES}
        self.max_file_bytes = int(max_file_bytes or 200000)

    def resolve(self, rel):
        raw = (rel or "").strip() or "."
        p = Path(raw)
        cand = p.resolve() if p.is_absolute() else (self.root / p).resolve()
        try:
            cand.relative_to(self.root)
        except ValueError:
            raise ValueError(f"路径越界: {rel}")
        return cand

    def call(self, name, args):
        args = args if isinstance(args, dict) else {}
        if name not in TOOL_NAMES:
            return f"未知工具: {name}"
        if name not in self.allow:
            return f"工具未授权: {name}"
        try:
            if name == "read_file":
                return self.read_file(args.get("path", ""))
            if name == "list_dir":
                return self.list_dir(args.get("path", "."))
            return self.grep(args.get("pattern", ""), args.get("path", "."))
        except Exception as e:
            return f"工具错误: {e}"

    def read_file(self, path):
        p = self.resolve(path)
        if not p.is_file():
            return f"不是文件: {path}"
        size = p.stat().st_size
        if size > self.max_file_bytes:
            return f"文件过大 ({size} bytes > {self.max_file_bytes})"
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"非文本文件: {path}"
        lines = text.splitlines()
        return "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines, 1))

    def list_dir(self, path="."):
        p = self.resolve(path or ".")
        if not p.is_dir():
            return f"不是目录: {path}"
        entries = []
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if child.name in SKIP_DIRS:
                continue
            tag = "dir" if child.is_dir() else "file"
            rel = child.relative_to(self.root).as_posix()
            entries.append(f"{tag}\t{rel}")
            if len(entries) >= MAX_LIST:
                entries.append("...truncated")
                break
        return "\n".join(entries) if entries else "(空目录)"

    def grep(self, pattern, path="."):
        if not pattern:
            return "pattern 为空"
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"正则无效: {e}"
        root = self.resolve(path or ".")
        hits = []
        for f in self._walk_files(root):
            try:
                if f.stat().st_size > self.max_file_bytes:
                    continue
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = f.relative_to(self.root).as_posix()
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}:{line[:200]}")
                    if len(hits) >= MAX_GREP:
                        hits.append("...truncated")
                        return "\n".join(hits)
        return "\n".join(hits) if hits else "无匹配"

    def _walk_files(self, root):
        if root.is_file():
            yield root
            return
        stack = [root]
        while stack:
            cur = stack.pop()
            try:
                children = list(cur.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if child.name not in SKIP_DIRS:
                        stack.append(child)
                elif child.is_file():
                    yield child

    def openai_schema(self):
        specs = []
        for name in TOOL_NAMES:
            if name in self.allow:
                specs.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": _DESC[name],
                        "parameters": _PARAMS[name],
                    },
                })
        return specs

    def anthropic_schema(self):
        specs = []
        for name in TOOL_NAMES:
            if name in self.allow:
                specs.append({
                    "name": name,
                    "description": _DESC[name],
                    "input_schema": _PARAMS[name],
                })
        return specs


_DESC = {
    "read_file": "读取工作区内一个文本文件，返回带行号的内容。",
    "list_dir": "列出工作区内某一层目录的文件和子目录。",
    "grep": "在工作区内用正则搜索文本，返回 path:line:text。",
}

_PARAMS = {
    "read_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
        },
        "required": ["path"],
    },
    "list_dir": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的目录，默认 ."},
        },
    },
    "grep": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则"},
            "path": {"type": "string", "description": "搜索起点，默认整个工作区"},
        },
        "required": ["pattern"],
    },
}


def parse_tool_args(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}

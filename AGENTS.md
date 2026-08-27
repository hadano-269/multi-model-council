# council — 多模型研究议会

一个主持人模型 + 多个异构专家模型，对议题做多轮辩论（拆题 → 表态 → 交叉评审 → 分歧追问 → 终局裁决），全程落盘到 `out/`。独立小工具，git 仓库（main 分支），Windows（BaiduSyncdisk 同步盘）环境。

## 常用命令

```bash
python council.py --list                 # 列席位
python council.py --ping expert_glm      # 单席连通性测试
python council.py "议题" --dry-run       # 零成本 mock 全流程（改完代码先跑这个）
python council.py "议题" --file bg_c3g.md           # 正式会诊
python council.py --resume out/<session>            # 从 checkpoint.json 续跑
python council.py --mode review --scheme 方案.md --author expert_glm --reviewers expert_qwen "需求"  # 方案评审模式
python council.py --mode review --scheme 方案.md --scheme-existing --discuss 讨论区 --author expert_glm "需求"  # 从既有方案起步（跳过 A0，不覆盖原文）
python gui.py                            # 或双击 start_gui.bat（自动补装依赖）
```

无测试、无 lint 配置；验证方式是 `--dry-run` + `--list`。

## 架构与分层

- `council.py` — 核心编排器：Client（openai/anthropic 双协议，stdlib urllib）、RetryHub 重试、SeatMemory 会话记忆、RunControl 取消/跳过/插入、LiveProgress 终端覆盖式进度、checkpoint、`ask_json`/`ask_text`。其他模块都依赖它。
- `review.py` — 方案评审模式（主笔 → 评审打分 → 改稿），复用 council 的席位调用。
- `gui.py` — CustomTkinter GUI，`import council` 复用编排；设置页「保存」只写改动的字段、保留 config.yaml 其余内容（含 `${ENV:-default}` 模板与 persona）。
- `mcp_server.py` — 供 OpenCode 调用的 MCP server，包装 council 后台跑。
- `tools.py` — 工作区只读工具（read_file/list_dir/grep），路径严格限制在 `tools.workspace` 内。
- `config.yaml` — 席位/端点/人设；`${ENV:-default}` 语法支持环境变量覆盖内联 key。
- `out/<YYYYmmdd_HHMMSS_标题>/` — 每次会话独立目录：`transcript.md`、`verdict.json`、`p1..p5` 阶段文件、`status.json`（CLI/GUI/MCP 共用的进度）、`checkpoint.json`。

## 关键约定

- **stdlib-only**：HTTP 一律走 urllib，除 `pyyaml`（核心）和 `customtkinter`（仅 GUI）外不得新增依赖。
- 代码注释、docstring、日志与 UI 文案均为中文，保持一致。
- `moderator` 与 `moderator_p5` 是联动席位：跳过一个等于跳过两个，SeatMemory 中共用一个 key。
- 重试 3 次指数退避，仅 `RETRYABLE` 状态码重试，4xx 参数类错误立即失败；`--max-calls`（默认 80）是单会话护栏。
- 专家席失败 → 标记缺席继续；主持人失败 → 终止。
- 专家失败时数据中可能有缺席席位；`verdict` 强制要求 `self_conflict_note`（同源偏差缓解）。
- review 模式：注入需求（GUI「插入」/ `--inject`）在 phase 边界 drain 进 `extra_reqs`，此后持续拼进 A1/A2/R3/R4/F 的 prompt；`--scheme-existing` 跳过 A0 不覆盖既有方案；任何覆盖写方案前先备份到讨论区 `方案_v{N}.md`。- `transcript.md` 必须保持干净，只落原始发言，绝不写入进度/刷新字符。
- 共享材料包：仅登记主持人席的工具输出原文（`Client.note_tool_output`，同 target 覆盖旧版），P2 经 `build_shared_block` 打包注入专家首条消息；单项/总量限额见 `SHARED_ITEM_MAX`/`SHARED_TOTAL_MAX`。不得把主持人的解读混入材料包。

## 坑与注意事项

- **mcp_server.py**：stdout 只能走 JSON-RPC；stderr 必须在 `import council` 之前重定向到 `mcp_debug.log`（OpenCode 握手前不读 stderr，否则死锁）。改动时不可破坏该顺序。
- **BaiduSyncdisk 同步**：可能产生 `config_冲突文件_*.yaml` 之类的冲突副本——它们不是权威配置，勿编辑、勿当作依据；权威文件是 `config.yaml`。
- **config.yaml 不存任何真实 key**：一律 `${ENV:-默认值}` 引用环境变量（本机已 `setx ARK_API_KEY` / `GO_API_KEY`）。真实调用前 `guard_api_keys`（council.py，接入 run/ping/review 三入口）会拦截缺失 key 与文件中误写的明文 key；往 config.yaml 回填真实 key 属于回归，禁止提交。
- Windows 优先：`_enable_vt()`（ctypes 开 ANSI）、`start_gui.bat`、中文路径与编码（文件读写显式 `encoding="utf-8"`）。
- `out/`、`__pycache__`、`.omo/`、`bg_c3g.md`（私有研究材料）、冲突副本、`mcp_debug.log` 均已被 `.gitignore` 排除，是生成物/外部/私有内容，不要手工整理或纳入提交。

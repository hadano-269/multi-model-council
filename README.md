# council — 多模型研究议会

独立小工具：一个主持人模型 + 多个异构专家模型，对一个问题做多轮辩论（拆题→表态→交叉评审→分歧追问→裁决），或对一份方案做多轮评审改稿；全程落盘可审计，可通过 CLI、GUI 或任意 MCP 客户端调用。

> **你是谁？**
> **如果你是 LLM / AI Agent**：用户说"帮我装一下这个仓库 / 接入议会工具"时，请从下方 **第一部分** 开始逐步执行并自验，完成后跳过其余内容。
> **如果你是人类用户**：直接跳到 **第二部分** 阅读项目介绍与用法。
> 改本项目代码的约定另见 [AGENTS.md](AGENTS.md)。

---

## 第一部分 · 给 AI Agent 的自主安装手册

本手册假设你已把仓库克隆/下载到本地，记其绝对路径为 `<REPO_DIR>`。按顺序执行，每步都有验证判据；不要跳步，也不要向用户索要本文已给出的信息。

### Step 1 环境检查

```bash
python --version        # 需要 >= 3.10；不足则先安装 Python 再继续
```

### Step 2 安装依赖

核心仅一个第三方包：

```bash
pip install pyyaml
```

GUI 才需要 customtkinter，Agent 场景可跳过。

### Step 3 API Key → 环境变量（关键）

**仓库与 config.yaml 不含任何真实密钥**，缺 key 会在真实调用前被预检拦截并给出中文提示。向用户只需要问一件事："要用哪些模型通道？key 是什么？"——然后把 key 写进环境变量：

| 端点 id | 环境变量 | 说明 |
|---------|----------|------|
| `opencode_go` | `GO_API_KEY` | OpenCode Zen 控制台获取 |
| `ark_plan` | `ARK_API_KEY` | 火山方舟 |
| `ccswitch` | 可不设 | 本地 CC Switch 代理，默认占位 |

Windows：`setx GO_API_KEY "sk-xxxx"`（写 profile/export 同理）。**只对新进程生效——注册完 MCP 必须重启客户端**。绝不把 key 写进任何文件、日志或对话正文。

### Step 4 无成本自检（不需要任何 key）

```bash
cd <REPO_DIR>
python council.py --list                          # 应列出 moderator 与若干 expert 席位
python council.py "冒烟测试" --dry-run --quiet    # 全 mock 流程，退出码 0 即安装成功
```

任一失败：回到 Step 1-2 排查版本与依赖。

### Step 5 注册 MCP server

服务入口是仓库根目录的 `mcp_server.py`（stdio JSON-RPC），工具共五个：`council_seats` / `council_start` / `council_status` / `council_verdict` / `council_cancel`。将 `<REPO_DIR>` 与 `<PYTHON>` 替换为本机绝对路径。

OpenCode（`~/.config/opencode/opencode.json` 的 `mcp` 段）：

```json
{
  "mcp": {
    "council": {
      "type": "local",
      "command": ["<PYTHON>", "-u", "<REPO_DIR>\\mcp_server.py"],
      "enabled": true
    }
  }
}
```

Claude Desktop 及其他 stdio 客户端（如 `claude_desktop_config.json` 的 `mcpServers` 段）：

```json
{
  "mcpServers": {
    "council": {
      "command": "<PYTHON>",
      "args": ["-u", "<REPO_DIR>/mcp_server.py"]
    }
  }
}
```

JSON 内 Windows 路径反斜杠需转义（`\\`）；改完配置重启客户端。

### Step 6 安装 skill（可选但推荐）

仓库内 `skills/council/SKILL.md` 教 Agent 在对话中正确触发与轮询 council 工具，复制到客户端 skills 目录即可：

| 客户端 | 目标位置 |
|--------|----------|
| Claude Code 系 | `~/.claude/skills/council/SKILL.md` |
| ZCode 系 | 用户级或工作区 skills 目录下的 `council/SKILL.md` |

复制后重启客户端。Skill 只含指引文本，无代码，无需构建。

### Step 7 端到端验收

通过 MCP 依次执行：`council_seats`（席位正常）→ `council_start {topic:"接入验收", mode:"debate", dry_run:true}`（立即返回 session id）→ 每 30s 轮询一次 `council_status` 直到结束 → `council_verdict` 能读到结构化结果。全部通过即交付完成。

### 故障速查

| 现象 | 处置 |
|------|------|
| `端点 xxx 的 api_key 为空` | 对应环境变量没设，或客户端注册后未重启 |
| config 第 N 行疑似明文密钥 | 有人把真实 key 写回了 config.yaml——改为 `${ENV:-}` 引用；若已上云/入库视为泄漏，轮换之 |
| MCP 连不上/握手卡死 | stdout 只允许 JSON-RPC；确认没有往 mcp_server.py 加 print 到 stdout |
| 中文乱码 | Windows 终端启用 VT/UTF-8；GUI 用 start_gui.bat 启动 |
| 同步盘出现 `config_冲突文件_*.yaml` | 非权威配置，勿据此运行 |

### 行为红线（Agent 必读）

1. 不要自己扮演专家辩论；实质内容一律来自 council_* 工具产出。
2. LIVE 会话消耗真实 token，启动前征得用户同意；连通性测试用 `dry_run: true`。
3. 密钥只进出环境变量：不写文件、不打日志、不粘进对话正文。
4. `out/`、`mcp_debug.log`、`.omo/` 是生成物；同步盘冲突副本不是权威配置。

---

## 第二部分 · 项目介绍与人类使用指南

以上面向自动化安装；下面是给人类读者的完整说明。

## 席位

| 席位 | 模型 | 通道 | 人设定位 |
|------|------|------|----------|
| moderator / moderator_p5 | deepseek-v4-flash | OpenCode Go | 拆题/找分歧/终局裁决（联动席位） |
| expert_qwen | qwen3.7-plus | OpenCode Go (anthropic 协议) | 工程可行性 |
| expert_mimo | mimo-v2.5 | OpenCode Go | 工程可行性 |
| expert_glm | glm-5.3flash | OpenCode Go | 独立研究员 |

席位在 `config.yaml` 的 `seats` 段配置，可增删改名。

## 首次配置

所有 API Key 一律走环境变量，`config.yaml` 不存任何真实密钥（`${ENV:-默认值}` 语法支持内联兜底，默认值建议留空）：

1. **CC Switch**：把本地代理的实际地址填进 `config.yaml` 的 `endpoints.ccswitch.base_url`（占位 `http://127.0.0.1:3000/v1`）。若代理校验 key，设环境变量 `setx CC_SWITCH_API_KEY "..."`。
2. **OpenCode Go**：`setx GO_API_KEY "..."`（Zen 控制台复制），设置后重开终端生效。
3. **火山方舟**：`setx ARK_API_KEY "ark-..."`，设置后重开终端生效。

真实调用前会做统一预检（`guard_api_keys`）：缺失的 key 会按端点逐个报错提示；文件中若误写疑似明文密钥会直接拦截。`--dry-run` 不需要任何 key。

## 用法

```bash
cd council

# 列出席位
python council.py --list

# 单席位连通性测试
python council.py --ping expert_glm

# 零成本流程验证（mock 所有调用）
python council.py "议题" --dry-run

# 全量会诊（默认覆盖式实时进度）
python council.py "议题" --file 背景材料.md

# 只用部分专家
python council.py "议题" --experts expert_grok,expert_dsv4

# 实时进度控制
python council.py "议题" --quiet        # 静默，仅最终汇总
python council.py "议题" --no-live      # 禁用覆盖刷新，退化为阶段汇总
$env:COUNCIL_FORCE_LIVE="1"; python council.py "议题" --dry-run  # 管道/非 TTY 下强制刷新（调试用）

# 工作区只读工具（需 config.yaml tools.enabled: true）
python council.py "议题" --workspace D:\proj
python council.py "议题" --no-tools

# 从上次未完成的会话续跑
python council.py --resume out/20260826_172252_某标题

# 方案评审（主笔写方案 → 多轮评审/打分 → 改稿）
python council.py --mode review --scheme 方案.md --discuss 讨论区 --author expert_glm --reviewers expert_qwen,expert_mimo "需求描述"

# 从既有方案起步：跳过主笔初稿，直接评审，不覆盖原文；改稿前自动备份旧版到讨论区（方案_v1.md、…）
python council.py --mode review --scheme 方案.md --scheme-existing --discuss 讨论区 --author expert_glm "需求描述"

# 会话开始即注入补充需求（与运行中 GUI「插入」等效；注入后从下一阶段起持续可见，由主笔写进方案）
python council.py --mode review --scheme 方案.md --inject "客户新增：支持多主办方" "需求描述"
```

### 实时进度

- 覆盖式单块刷新：每 0.5s 原地刷新同一块区域，显示 `[阶段/总阶段] 标题 spinner 已用/会话/预计剩余/调用` + 每席 `席位 spinner 已用 重试/完成详情`
- 串行阶段（P1/P4/P5）单席计时，并行阶段（P2/P3/P4b）多席并发计时与 `完成/total` 计数
- 重试时实时显示 `重试 1/3 HTTP 502 ...`，便于区分卡死与正常推理
- 终端需支持 ANSI（Windows Terminal / VS Code 终端已支持，已自动启用 VT）；非 TTY 或 `--quiet` 自动回退
- `transcript.md` 保持干净，仅落盘原始发言，不含进度字符

## 产出物（每次会话独立目录）

```
out/<YYYYmmdd_HHMMSS_议题标题>/
├── transcript.md            # 全程原始发言记录
├── verdict.json             # 结构化裁决：共识/未决分歧/推荐实验/否决路线 + meta
├── p1_motions.json          # 论题拆解
├── p2_position_<seat>.json  # 各专家独立表态
├── p3_review_<seat>.json    # 各专家交叉评审
├── p4_disputes.json         # 分歧清单
├── p4_answer_<seat>.json    # 分歧答辩
└── p5_verdict_raw.txt       # 裁决原文
```

方案评审模式另有：讨论区各轮评审/打分 md、`定稿说明.md`、改稿前自动备份的 `方案_v{N}.md`。

## 行为说明

- 专家席失败 → 标记缺席继续；主持人失败 → 终止。
- 每次调用重试 3 次（指数退避），仅可重试状态码重试，4xx 参数类错误立即失败。
- `--max-calls N` 为单会话调用数护栏（默认 80）。
- 同源偏差缓解：moderator 与 expert_grok 同源时，verdict 强制输出 `self_conflict_note`。
- 工作区工具（默认关闭）：全局白名单 `read_file` / `list_dir` / `grep`，路径限制在 `tools.workspace` 内；openai / anthropic 席位可在给出 JSON 前先查文件。`responses` 协议不接工具。`--no-tools` 强制关。
- 共享材料包：主持人 P1 读取的工具原文按目标去重（同文件留最新版）、单项截断 6000 字符、总量封顶 24000 字符后，随 P2 注入每位专家的首条消息；只转未经加工的原文，不做主持人解读，避免锚定专家独立表态。后续阶段经各席历史自动可见；此时工具提示变为「如需核实再调用」，专家可按需补充查阅。
- GUI：运行中途可取消整场或跳过某席；失败可手动重试；设置页可编辑人设、单席/全部 Ping；顶栏与裁决记录 token 输入/输出计数（不换算金额）。`--resume` 或 GUI「续跑」从 `checkpoint.json` 下一阶段接着打。

## 依赖

Python >= 3.10，`pyyaml`。HTTP 走 stdlib urllib，无其他依赖。GUI 另需 customtkinter。

## MCP 接入

MCP server 为仓库根目录 `mcp_server.py`（stdio JSON-RPC），注册方式见**第一部分 Step 5**。工具一览：

| 工具 | 作用 |
|------|------|
| `council_seats` | 列出席位 |
| `council_start` | 后台开场（debate 议会 / review 方案评审），立刻返回 session；review 需给 scheme，可选 discuss/author/reviewers/scheme_existing/inject |
| `council_status` | 阶段 / 每席状态 / token |
| `council_verdict` | 读裁决；未结束则返回当前进度 |
| `council_cancel` | 取消本进程内正在跑的一场 |

对 OpenCode 说「用议会讨论 xxx」或「用评审模式评审 xxx 方案」即可发起。进度同时写在 `out/<session>/status.json`，CLI / GUI / MCP 共用。改完配置后重启客户端；LIVE 会话同样受 API Key 环境变量预检约束。

## GUI

双击 `start_gui.bat` 一键启动（自动检查并补装依赖、无黑窗口）。或手动：

```bash
pip install -r requirements.txt
python gui.py
```

- **设置页**：顶部「工作区工具」全局开关 + 工作区目录 + 三个只读工具勾选；主持人 + 专家席均可独立配置 Base URL / API Key / 模型名 / 协议（openai / anthropic）；专家可增删改名；「保存」写回 `config.yaml`（未改动的字段保留原样，含 `${ENV:-default}` 模板与各席 persona）。**API Key 特殊处理**：输入的新密钥写入用户环境变量（约定名 `GO_API_KEY`/`ARK_API_KEY` 等，未知端点按席位合成），config 只保留 `${VAR:-}` 引用模板，永不明文落盘；既有明文会在下次保存时自动迁移。
- **运行页**：可选「辩论议会」或「方案评审」；勾选「测试」为零费用 mock。方案评审需填方案路径、讨论区、主笔，可勾「既有方案」跳过主笔初稿（不覆盖原文）；运行中可「插入」额外需求（也可会前用 `--inject`），注入文字从下一阶段起持续生效，由主笔写进方案。
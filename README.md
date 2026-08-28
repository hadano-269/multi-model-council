# council — 多模型研究议会

独立小工具：一个主持人模型 + 多个异构专家模型，对一个问题做多轮辩论（拆题→表态→交叉评审→分歧追问→裁决），或对一份方案做多轮评审改稿；全程落盘可审计，可通过 CLI、GUI 或任意 MCP 客户端调用。

> **你是谁？**
> **如果你是 LLM / AI Agent**：用户说"帮我装一下这个仓库 / 接入议会工具"时，请从下方 **第一部分** 开始逐步执行并自验，完成后跳过其余内容。
> **如果你是人类用户**：直接跳到 **第二部分** 阅读项目介绍与用法。
> 改本项目代码的约定另见 [AGENTS.md](AGENTS.md)。

---

## 如果你是AI

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

### Step 3 配置密钥（直接写进本机 config.yaml）

`config.yaml` 已被 `.gitignore` 排除——**密钥明文写进该文件是设计内的，且永不进入 git**：

```bash
cp config.example.yaml config.yaml     # Windows: copy config.example.yaml config.yaml
```

然后打开 `config.yaml`，把各端点的 `<在此粘贴你的Key>` 占位换成真实密钥（或之后在 GUI 设置页里填，效果相同）。

**你是 AI Agent 时**：不要向用户索要密钥原文粘贴到对话里——引导用户自己编辑本机的 `config.yaml`（或用 GUI 设置页）填写即可。

### Step 4 无成本自检（不需要任何 key）

```bash
cd <REPO_DIR>
python council.py --list                          # 应列出 moderator 与若干 expert 席位
python council.py "冒烟测试" --dry-run --quiet    # 全 mock 流程，退出码 0 即安装成功
```

任一失败：回到 Step 1-2 排查版本与依赖。

### Step 4.5 定制席位（向导）

默认的六席阵容是展示组合。向你的用户逐条询问以下问题（问完二选一执行）：

> 1. 谁当主持人？模型名叫什么？
> 2. 主持人走哪个通道？OpenCode Zen / 本地 CC Switch / 自定义 URL？
> 3. 该通道的 API Key 是？（若没有，引导用户去控制台创建后再继续）
> 4. 想要几个专家？每位分别叫什么名字？
> 5. 每位专家的职责选一个预设：实验设计师 / 边界条件分析师 / 工程可行性评估人 / 独立研究员 / 方案主笔
> 6. 每位专家的模型名、通道与 API Key 同样确认一遍

执行方式二选一：

- 让用户自己在新终端跑 `python council.py --wizard`，交互回答同一组问题；
- 或由你把答案整理成 profile 文件后无头应用：

```yaml
version: 1
moderator: { model: glm-5.3, endpoint_id: opencode_go }
experts:
  - { name: kimi_expert, role_preset: designer_experiments,
      model: kimi-k3, endpoint_id: opencode_go }
```

```bash
python council.py --wizard-profile profile.yaml   # 读取→校验→原子写入 config.yaml
```

安全约定：密钥只落在本机的 `config.yaml`（已被 gitignore）——不要把 key 粘贴到对话正文或日志里；profile 文件在本机，写 key 也不会入库。
向导发现某变量未设置时会在 notes 中列出待办命令。

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
| `端点 xxx 的 api_key 为空` | `config.yaml` 里对应端点的 key 还没填（该文件不入库，放心写明文） |
| config 第 N 行疑似明文密钥 | 有人把真实 key 写回了 config.yaml——改为 `${ENV:-}` 引用；若已上云/入库视为泄漏，轮换之 |
| MCP 连不上/握手卡死 | stdout 只允许 JSON-RPC；确认没有往 mcp_server.py 加 print 到 stdout |
| 自写脚本调 MCP 时 `tools/call` 回包迟迟不到 | 先看 `mcp_debug.log`：若业务已完成（"完成，用时…"）而管道无响应，多为本机磁盘/同步盘对 `council.py` 冷加载的扫描拖慢——真客户端重试即可；自写验收器建议二进制管道 + 读线程 + 宽松超时 |
| 中文乱码 | Windows 终端启用 VT/UTF-8；GUI 用 start_gui.bat 启动 |
| 同步盘出现 `config_冲突文件_*.yaml` | 非权威配置，勿据此运行 |

### 行为红线（Agent 必读）

1. 不要自己扮演专家辩论；实质内容一律来自 council_* 工具产出。
2. LIVE 会话消耗真实 token，启动前征得用户同意；连通性测试用 `dry_run: true`。
3. 密钥只写进本机 `config.yaml`（已被 gitignore）：不打日志、不粘进对话正文、不提交任何含 key 的文件。
4. `out/`、`mcp_debug.log`、`.omo/` 是生成物；同步盘冲突副本不是权威配置。

---

## 如果你是人类

以上面向自动化安装；下面是给人类读者的完整说明。

## 席位

| 席位 | 模型 | 通道 | 人设定位 |
|------|------|------|----------|
| moderator / moderator_p5 | glm-5.3 | OpenCode Go | 拆题/找分歧/终局裁决（联动席位） |
| expert_kimi | kimi-k3 | OpenCode Go | 实验设计师 |
| expert_dsv4 | deepseek-v4-pro | OpenCode Go | 边界条件分析师 |
| expert_gpt | gpt-5 | OpenCode Go (anthropic 协议) | 工程可行性 |
| expert_claude | claude-opus-5 | OpenCode Go | 独立研究员 |

> 默认席位为**展示组合**：实际可用性取决于你的聚合通道（OpenCode Zen / 火山方舟等）提供的模型清单。
> 请按需替换 `config.yaml` 的 `seats` 段中的 model/endpoint——同一通道里填它支持的任意模型即可，
> `python council.py --list` 可随时核对你本机的有效阵容。

席位在 `config.yaml` 的 `seats` 段配置，可增删改名。

## 首次配置

最省事的方式是先跑安装向导（问答式生成席位配置）：

```bash
python council.py --wizard     # 也可用 --wizard-profile <file> 无头应用
```

手动配置两个常用通道：

1. **CC Switch**：把本地代理的实际地址填进 `config.yaml` 的 `endpoints.ccswitch.base_url`（占位 `http://127.0.0.1:3000/v1`）。若代理校验 key，同样直接写在文件里。
2. **OpenCode Go**：把 Zen 控制台的 key 写进 `endpoints.opencode_go.api_key`，保存即生效。

真实调用前会做统一预检（`guard_api_keys`）：哪个端点的 key 为空会按端点逐个报错提示。`--dry-run` 不需要任何 key。

## 用法

```bash
cd council

# 列出席位
python council.py --list

# 单席位连通性测试
python council.py --ping expert_kimi

# 零成本流程验证（mock 所有调用）
python council.py "议题" --dry-run

# 全量会诊（默认覆盖式实时进度）
python council.py "议题" --file 背景材料.md

# 只用部分专家
python council.py "议题" --experts expert_kimi,expert_dsv4

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
python council.py --mode review --scheme 方案.md --discuss 讨论区 --author expert_claude --reviewers expert_kimi,expert_gpt "需求描述"

# 从既有方案起步：跳过主笔初稿，直接评审，不覆盖原文；改稿前自动备份旧版到讨论区（方案_v1.md、…）
python council.py --mode review --scheme 方案.md --scheme-existing --discuss 讨论区 --author expert_claude "需求描述"

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
- 同源偏差缓解：议会中存在与主持人同源（同厂商/同基座模型）席位时，verdict 强制输出 `self_conflict_note`。
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

对 OpenCode 说「用议会讨论 xxx」或「用评审模式评审 xxx 方案」即可发起。进度同时写在 `out/<session>/status.json`，CLI / GUI / MCP 共用。改完配置后重启客户端；LIVE 会话前若 `config.yaml` 中某端点 key 为空会被预检拦截。

## GUI

双击 `start_gui.bat` 一键启动（自动检查并补装依赖、无黑窗口）。或手动：

```bash
pip install -r requirements.txt
python gui.py
```

- **设置页**：顶部「工作区工具」全局开关 + 工作区目录 + 三个只读工具勾选；主持人 + 专家席均可独立配置 Base URL / API Key / 模型名 / 协议（openai / anthropic）；专家可增删改名；「保存」写回 `config.yaml`（未改动的字段保留原样，含各席 persona）。**API Key**：输入框回显已保存的值（默认打码，眼睛按钮切换明文），保存后明文写入 `config.yaml`——该文件已被 gitignore，不会进入版本库。
- **运行页**：可选「辩论议会」或「方案评审」；勾选「测试」为零费用 mock。方案评审需填方案路径、讨论区、主笔，可勾「既有方案」跳过主笔初稿（不覆盖原文）；运行中可「插入」额外需求（也可会前用 `--inject`），注入文字从下一阶段起持续生效，由主笔写进方案。

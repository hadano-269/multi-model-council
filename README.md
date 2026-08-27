# council — 多模型研究议会

独立小工具：一个主持人模型 + 多个异构专家模型，对一个问题做多轮辩论（拆题 → 表态 → 交叉评审 → 分歧追问 → 终局裁决），全程落盘可审计。与本项目其他工程零耦合。

## 席位

| 席位 | 模型 | 通道 | 人设 |
|------|------|------|------|
| moderator | grok-4.6 | CC Switch 本地代理 | 拆题/找分歧/裁决（含同源自涉声明） |
| expert_grok | grok-4.6 | CC Switch | 专职证伪者 |
| expert_glm | glm-5.3 | 火山方舟 AgentPlan | 实验设计师 |
| expert_dsv4 | deepseek-v4-pro | OpenCode Go | 边界条件分析师 |
| expert_qwen | qwen3.7-plus | OpenCode Go (anthropic 协议) | 工程可行性 |
| expert_minimax | minimax-m2.7 | OpenCode Go (anthropic 协议) | 独立研究员 |

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
python council.py "C3g 阶段 CER 还剩 7.3pp，下一步从哪突破？" --file bg_c3g.md

# 只用部分专家
python council.py "议题" --experts expert_grok,expert_dsv4
python council.py "议题" --experts expert_qwen,expert_minimax  # 低成本快速验证

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

- 覆盖式单块刷新（A 方案）：每 0.5s 原地刷新同一块区域，显示 `[阶段/总阶段] 标题 spinner 已用/会话/预计剩余/调用` + 每席 `席位 spinner 已用 重试/完成详情`
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

## 行为说明

- 专家席失败 → 标记缺席继续；主持人失败 → 终止。
- 每次调用重试 3 次（指数退避），仅 4xx 参数类错误立即失败。
- `--max-calls N` 为单会话调用数护栏（默认 80）。
- 同源偏差缓解：moderator 与 expert_grok 同为 grok-4.6，verdict 强制输出 `self_conflict_note`。
- 工作区工具（默认关闭）：全局白名单 `read_file` / `list_dir` / `grep`，路径限制在 `tools.workspace` 内；openai / anthropic 席位可在给出 JSON 前先查文件。`responses` 协议不接工具。`--no-tools` 强制关。- GUI：运行中途可取消整场或跳过某席；失败可手动重试；设置页可编辑人设、单席/全部 Ping；顶栏与裁决记录 token 输入/输出计数（不换算金额）。`--resume` 或 GUI「续跑」从 `checkpoint.json` 下一阶段接着打。
- 共享材料包：主持人 P1 读取的工具原文按目标去重（同文件留最新版）、单项截断 6000 字符、总量封顶 24000 字符后，随 P2 注入每位专家的首条消息；只转未经加工的原文，不做主持人解读，避免锚定专家独立表态。后续阶段经各席历史自动可见；此时工具提示变为「如需核实再调用」，专家可按需补充查阅。

## 依赖

Python >= 3.10，`pyyaml`。HTTP 走 stdlib urllib，无其他依赖。

## OpenCode MCP

OpenCode 可直接发起议会（后台跑，不阻塞对话）。已可写入 `~/.config/opencode/opencode.json`：

```json
{
  "mcp": {
    "council": {
      "type": "local",
      "command": [
        "C:\\Python314\\python.exe",
        "-u",
        "C:\\BaiduSyncdisk\\council\\mcp_server.py"
      ],
      "enabled": true
    }
  }
}
```

对 OpenCode 说「用议会讨论 xxx」即可。工具：

| 工具 | 作用 |
|------|------|
| `council_seats` | 列出席位 |
| `council_start` | 后台开场，立刻返回 session |
| `council_status` | 阶段 / 每席状态 / token |
| `council_verdict` | 读裁决；未结束则返回当前进度 |
| `council_cancel` | 取消本进程内正在跑的一场 |

进度同时写在 `out/<session>/status.json`，CLI / GUI / MCP 共用。改完配置后重启 OpenCode。

## GUI

双击 `start_gui.bat` 一键启动（自动检查并补装依赖、无黑窗口）。或手动：

```bash
pip install -r requirements.txt
python gui.py
```

- **设置页**：顶部「工作区工具」全局开关 + 工作区目录 + 三个只读工具勾选；主持人 + 专家席均可独立配置 Base URL / API Key / 模型名 / 协议（openai / anthropic）；专家可增删改名；「保存」写回 `config.yaml`（未改动的字段保留原样，含 `${ENV:-default}` 模板与各席 persona）。
- **运行页**：可选「辩论议会」或「方案评审」；勾选「测试」为零费用 mock。方案评审需填方案路径、讨论区、主笔，可勾「既有方案」跳过主笔初稿（不覆盖原文）；运行中可「插入」额外需求（也可会前用 `--inject`），注入文字从下一阶段起持续生效，由主笔写进方案。
- 席位若自带 base_url/api_key 则优先生效，否则回退到 `endpoints` 共享配置。CLI 用法不变。

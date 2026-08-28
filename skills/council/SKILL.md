---
name: council
description: 多模型研究议会：一个主持人 + 多个异构专家对议题做五阶段辩论（拆题→表态→交叉评审→分歧追问→裁决），或对一份方案做多轮评审改稿。当用户要求"用议会讨论/会诊 xxx""让多个模型辩论""用评审模式评审某方案（多专家打分）"时使用。本 skill 通过 council MCP 工具发起后台任务，不自行扮演任何专家。
---

# council — 多模型议会调用指南

## 工具总览（来自 council MCP server）

| 工具 | 用途 |
|------|------|
| `council_seats` | 列出已配置的主持人与专家席位 |
| `council_start` | 后台启动一场，立即返回 session id，**不等待结束** |
| `council_status` | 查询进度快照（阶段、每席状态、token） |
| `council_verdict` | 读取最终结果；未结束则返回当前进度 |
| `council_cancel` | 取消本 MCP 进程内正在跑的一场 |

两种模式：
- **debate 议会辩论**（默认）：拆题 → 表态 → 交叉评审 → 分歧追问 → 裁决
- **review 方案评审**：主笔写方案 → 评审打分 → 改稿 → 再评审 → 定稿说明

## 铁律

1. **绝不自己扮演专家或主持人输出观点**——所有实质内容必须来自 council_* 工具的真实产出。
2. LIVE 模式消耗真实 token；LIVE 启动前向用户口头确认一次（用户明确说"正式跑/上真模型"时视为已确认）。连通性验证一律 `dry_run: true`。
3. 发起后立即把返回的 session id 与 out 目录告知用户，然后按下方节奏轮询。
4. 用户没指定专家席位时省略 experts/reviewers（=全部）；成本敏感时先 council_seats 看阵容，只挑两三个专家席组紧凑会。

## debate 流程

```
1) 可选：council_seats 确认席位存在
2) council_start { topic, file?, experts?, dry_run? }
3) 轮询 council_status { session } —— 每 30~60s 一次，勿连续狂轮询
   终态特征：不再处于 running（state 变为 done/error），或 [5/5]
4) council_verdict { session } → 把 JSON 的共识/未决分歧/推荐实验/否决路线
   整理成中文摘要讲给用户，并附 out 目录路径与 transcript.md / verdict.json 位置
5) 仅在用户要求停止时 council_cancel { session }
```

关键参数：`topic` 必填；`file` 为背景材料 md/txt 路径（相对路径先按当前目录、再按安装目录解析）；`experts` 逗号分隔席位 id。

## review 流程

```
1) 向用户拿到：方案文件路径 scheme（必填）、讨论区目录 discuss（缺省=scheme 同目录/讨论区）、
   主笔 author、评审 reviewers、是否从既有方案起步 scheme_existing、补充需求 inject
2) 若用户已有写好的方案文件 → scheme_existing: true（跳过主笔初稿且不覆盖原文；
   改稿前旧版自动备份为讨论区/方案_v{N}.md）
3) council_start {
     topic, mode: "review", scheme, dry_run?,
     scheme_existing?, discuss?, author?, reviewers?, inject?
   }
4) 轮询同上；终态为 [8/8]。注入需求属阶段性动作，发起后无法追加
   （CLI/GUI 运行中「插入」能力 MCP 场景暂无对应物）
5) council_verdict 输出含 can_ship/scores 字段：如实转述各席打分与定稿判断，
   不要替用户拍板"可以实施"
```

## 失败处理

- 返回文本以中文错误开头的（席位不存在/scheme 缺失等）：修正参数重试，必要时先 council_seats。
- status 显示 error 且 error 含 "api_key 为空"/"API Key 检查未通过"：引导用户编辑本机
  `config.yaml`，把对应端点的 api_key 填上（该文件已 gitignore），保存后**重启客户端**再试，
  MCP 进程不会热加载配置。
- 会话由 CLI/GUI 发起的（提示"不在本 MCP 进程内"）：council_cancel 无法远程取消，
  但 council_status/council_verdict 仍可只读查询。
- dry_run 全链路可零成本验证安装是否成功——新装环境先用它自检。

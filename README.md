# RiftLens

**英雄联盟对局复盘 Agent：每一局输赢，都有证据。**

RiftLens 从 Riot 官方接口拉取对局数据，用确定性的分析工具算出事实（转折点、团战、阵亡、资源、对线），再由大模型写成**每条结论都标注依据**的复盘。它也能分析近期状态、回答对位问题，并记住用户的长期特点。

它同时是一个**桌面客户端助手**（思路类似 League Akari）：直接读取本机英雄联盟客户端，国服、外服都能用，不需要 Riot key。功能包括：
- 自动接受、按位置选禁；
- 选人和游戏中的十人战绩、开黑检测、游戏内悬浮窗；
- 按对面阵容出装并写进游戏商店，生成赛前计划；
- OP.GG 数据、段位历史和 LP 曲线；
- 打完自动复盘。

Windows 用户可以直接下载安装包：**[Releases → RiftLens-Setup-1.0.0.exe](https://github.com/JaveyBae/riftlens/releases/latest)**。

> 这是一个个人学习项目，重点不在"又一个数据网站"，而在于：**在没有标准答案的分析任务上，怎样让 agent 的结论可验证、少幻觉，并且能量化成本。**

![首页](docs/screenshots/home.png)

![对话界面：左侧对话列表，中间回答（每条结论标注依据的工具），右侧 Agent 活动时间轴与本次 token 用量](docs/screenshots/chat.png)

---

## 功能

| 功能 | 说明 | 示例 |
|---|---|---|
| **单局复盘** | 经济转折、团战切分、每次阵亡的原因、资源争夺、对线期对比、规则检出的失误 | `复盘 Player#TAG 最近一局` |
| **近期状态** | 最近 N 局的胜率、赢局与输局对比、英雄池；只用终局统计，不拉时间线 | `我最近 10 局怎么样` |
| **对位知识** | Riot 官方英雄数据 + 联网查询数据网站和玩家讨论，标注版本、来源和分歧 | `亚索怎么打鳄鱼` |
| **长期记忆** | 用户资料（Riot ID、位置、段位、目标）+ agent 记下的长期特点，可查看、可删除 | `复盘我最近一局` |
| **段位基准** | 从同段位、同版本、同英雄的对局中采样，计算每分钟经济/经验/补刀分位数 | 李青打野 · 钻石 · 16.20 |
| **客户端助手** | 连接本机英雄联盟客户端（任何服务器，**包括国服**，不需要 Riot key）：自动接受对局、按位置自动选/禁英雄（可设延迟）、锁定英雄后自动套用国服或 OP.GG 符文、选人和游戏中查询每名玩家的段位/近期战绩/打法标签/侦察分/英雄熟练度/开黑检测、赛后自动复盘和自动点赞 | 侧栏“客户端助手” |
| **这把怎么打 + 出装** | 选人锁定后、进加载界面时各生成一次：对位胜率、对面最弱 / 最强的人、开黑、双方阵容（物理/魔法、坦克、回复、硬控）、按对面阵容调整的出装（自动写进游戏商店推荐页），以及 AI 写的 10 行赛前计划；只在开局前生成，游戏中不给实时指令 | 实时对局 · 悬浮窗“这把怎么打” |
| **游戏内悬浮窗** | 桌面版：游戏中按 Shift+Tab 显示十人面板（段位、侦察分、近 6 局、标签、你的备注、实时 KDA / 补刀 / 装备价值），点英雄头像看技能冷却 | 游戏中 Shift+Tab |
| **AI 教练** | 分析最近最多 100 局排位，可按位置筛选，输出强项、弱项和最该改进的一点；复盘和教练都能选“客观分析 / 毒舌喷子 / 专业夸夸” | 客户端助手 → AI 教练 |
| **战绩与玩家** | 个人打法画像、英雄熟练度、每局完整计分板（评分、MVP/ACE、伤害、装备）；按“名字#编号”查同大区玩家；给遇到的人写备注，下次排到时提醒 | 我的战绩 · 查玩家 · 玩家备注 |
| **段位历史与 LP 曲线** | 当前段位、本赛季最高、上赛季最高；外服账号显示 OP.GG 记录的往季段位，LP 曲线合并 OP.GG 和 RiftLens 自己记录的点（每局排位后自动记录，并让 OP.GG 刷新一次）；国服只能从开始使用 RiftLens 起记录 | 我的战绩 |
| **OP.GG 数据** | 英雄梯队（按位置、地区、段位，排位 / 大乱斗）、每个英雄的符文、召唤师技能、技能加点、出门装 / 鞋子 / 核心装备、难打和好打的对位；一键把符文和召唤师技能应用到客户端，也可以选定英雄后自动应用；对话里问出装、对位时 agent 会调用 `opgg_champion` | 客户端助手 → OP.GG |
| **桌面客户端** | 用 pywebview 把网页包成原生窗口，打包成 `RiftLens.exe`，并用 Inno Setup 做成中文安装包（按用户安装、不需要管理员、可卸载） | `python build_desktop.py --installer` |
| **可观测** | 每一步工具调用、返回、记忆写入、token 用量和成本实时显示，并写入轨迹文件 | — |

---

## 架构

```mermaid
flowchart LR
    U[浏览器<br/>首页 / 对话 / Agent 活动] -->|SSE| S[FastAPI<br/>web/app.py]
    S --> DB[(SQLite<br/>对话 · 资料 · 记忆)]
    S -->|收件箱| W[Headless worker<br/>每条消息一个隔离会话]
    S -->|llm 模式| L[OpenAI 兼容模型<br/>DeepSeek / Qwen …]
    W -->|只允许调用| T[工具层<br/>server.py · 22 个 MCP 工具]
    L --> T
    T --> A[分析函数<br/>lol_review/analysis.py]
    A --> R[Riot API 客户端<br/>缓存 · 限流 · 重试]
    R --> API[(Riot Match-v5 / League-v4<br/>Data Dragon)]
    W -.->|知识类问题| WEB[网页搜索 / 读取]
```

### 设计要点

1. **计算和解读分离。** 所有数字由确定性代码从 Riot 原始数据算出，模型不自己数数、不做算术。一局原始数据约 21.5 万 token，经工具层压缩后交给模型的约 4 千 token。
2. **结论必须有依据。** 每条结论标注来源工具（例如 `[jungle_stats]`）或网页链接；数据里没有的信息（技能释放、精确走位、插眼坐标）要明确说"无法判断"。
3. **轨迹可追溯。** 每次 API 请求、工具调用、决策都写进 `traces/*.jsonl`，可以从结论追到原始工具输出。
4. **隔离的执行环境。** 每条用户消息由一个全新的 headless 会话处理：不带任何开发对话历史，系统提示词完全替换，只允许调用项目工具和网页搜索。因此能精确统计单次问答的 token 与成本。
5. **记忆只记用户本人。** 分析别人的账号时不写入长期记忆；注入时提示"记忆可能过时，以新数据为准"。

### 三种运行模式

| 模式 | 谁来当模型 | 适合 |
|---|---|---|
| `bridge`（默认） | 本机 Claude Code，以 headless 方式运行，每条消息一个隔离会话 | 个人使用、开发调试 |
| `llm` | 任意 OpenAI 兼容接口（DeepSeek、通义千问等），自带工具调用循环 | 部署、多人使用、跨模型评估 |
| `b0` | 不用模型，固定骨架 + 模板 | 评估时的 baseline |

---

## 快速开始

### 1. 安装

```bash
git clone <this-repo> && cd lol-review-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # 可选：填入 Riot API key
```

需要 Python 3.11+。

**Riot API key 是可选的。** 不配置也能启动，对位知识问答（官方英雄数据 + 网页搜索）可以直接用；查询对局时才需要 key。两种配置方式：

- 写进 `.env` 的 `RIOT_API_KEY`；
- 或者在网页左下角的**设置**里填写：会先向 Riot 验证，再保存到本机的 `data/riot_api_key`（已被 git 忽略）。

开发者 key 在 [developer.riotgames.com](https://developer.riotgames.com) 免费申请，**每 24 小时过期**。

### 2. 启动网页

```bash
.venv/bin/python -m uvicorn web.app:app --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765>。

### 3. 启动模型端（按模式选一种）

```bash
# bridge 模式：需要本机已安装并登录 Claude Code
.venv/bin/python -m web.headless_worker              # 默认模型
.venv/bin/python -m web.headless_worker --model sonnet

# llm 模式：在 .env 中填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，无需 worker
```

### 3.5 客户端助手与桌面版（Windows）

客户端助手读取的是**本机正在运行的英雄联盟客户端**（LCU 接口），所以国服、外服都能用，也不需要 Riot API key：

- **自动接受对局**（可设延迟）；
- **自动选 / 禁英雄**：按你排好的顺序，跳过已被禁用、已被选走和队友想选的英雄，可选“只预选不锁定”；
- **按位置自动选 / 禁**：每个位置可以有自己的英雄列表，分到哪个位置就用哪个，没有就用通用列表；选和禁各自可设 0–5 秒延迟；
- **玩家战绩**：英雄选择时查询队友，进入游戏后查询全部 10 人：单双排段位、近 N 局胜率和 KDA、最近 10 局胜负、常用英雄，以及“连胜 / 连败 / 绝活 / 疑似小号 / 战绩隐藏”和约 30 种打法标签（输出核心、视野缺失、恐补症……）；
- **侦察分、熟练度、开黑**：侦察分由近期场次、胜场和 KDA 算出，只用于和同局其他人比较；游戏中显示每人所用英雄的熟练度；同队两人最近一起打过至少 2 局会标成“开黑”；
- **符文**：锁定英雄后自动应用，数据来源可选国服（lol.qq.com 的国服统计）或 OP.GG；也可以把客户端当前符文页存成某个英雄的默认页，之后优先用它；
- **游戏内悬浮窗**（桌面版）：游戏中按 Shift+Tab 显示 / 隐藏，只在游戏进行时占用这个快捷键。游戏需要设成“无边框”或“窗口”模式；
- **赛后**：可自动给一名队友点“友善”赞（默认关闭）；可以给这局遇到的人写备注和标签，之后在选人和游戏中再遇到会显示；
- **OP.GG**：英雄梯队和单英雄出装页；“应用到客户端”会新建一页名为“RiftLens 英雄名”的符文页（再次应用会替换它；符文页满了会替换当前页），召唤师技能只能在英雄选择阶段设置。可在“自动化”里打开选定英雄后自动应用，以及闪现放 D 还是 F；
- **赛后自动复盘**：对局结束后自动打开新对话复盘这一局；“我的战绩”里每一局都可以看计分板或一键复盘，复盘风格可选客观分析、毒舌喷子、专业夸夸。客户端对局的 id 形如 `LCU_<gameId>`，所有分析工具都能直接使用。

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-desktop.txt
.venv\Scripts\python desktop.py                 # 桌面窗口（直接打开客户端助手）
.venv\Scripts\python build_desktop.py           # 打包 → dist\RiftLens\RiftLens.exe
.venv\Scripts\python build_desktop.py --admin   # 打包成启动时请求管理员权限的版本
.venv\Scripts\python build_desktop.py --installer  # 再生成安装包 release\RiftLens-Setup-<版本>.exe（需要 Inno Setup 6：winget install -e --id JRSoftware.InnoSetup）
```

安装包按用户安装到 `%LOCALAPPDATA%\Programs\RiftLens`，不需要管理员权限；可选“总是以管理员身份运行”（国服需要）；卸载时保留 `%APPDATA%\RiftLens` 里的设置和记录。程序没有数字签名，第一次运行时 Windows SmartScreen 会提示，点“更多信息 → 仍要运行”。

- **国服客户端以管理员权限运行**，RiftLens 也要“以管理员身份运行”（或用 `--admin` 打包），否则读不到客户端的连接信息。
- 客户端装在不常见的位置时，在 `.env` 里设置 `LOL_INSTALL_DIR` 为包含 `lockfile` 的文件夹。
- 打包版的数据、设置和 `.env` 放在 `%APPDATA%\RiftLens`；没有配置 LLM 时使用无模型模式（B0），在那里的 `.env` 填写 `LLM_*` 后获得完整复盘。
- OP.GG 数据来自它网站使用的非官方 JSON 接口（`lol-api-champion.op.gg`），随时可能变动；接口失败时页面会提示，agent 的其他工具不受影响。结果缓存 1 小时。
- 客户端返回的对局数据没有 `challenges` 字段（如偷 buff 数、对线期优势），位置判断来自对局的 lane/role 和惩戒，可能不准。

### 4. 其他用法

```bash
# 在 Claude Code 中作为 MCP 工具使用
claude mcp add lol-review -- "$PWD/.venv/bin/python" "$PWD/server.py"

# 命令行：跑固定骨架并输出全部工具结果和轨迹
.venv/bin/python review.py "Player#TAG"

# 构建段位基准（例：钻石 · 16.20 · 李青打野 · 40 局）
.venv/bin/python -m lol_review.benchmark LeeSin JUNGLE DIAMOND 16.20 --n 40

# 运行一个隔离的实例（独立的数据库、轨迹和收件箱，例如用于演示）
RIFTLENS_STATE_DIR=/tmp/riftlens-demo .venv/bin/python -m uvicorn web.app:app --port 8766
RIFTLENS_STATE_DIR=/tmp/riftlens-demo .venv/bin/python -m web.headless_worker

# 轨迹 / 基准可视化
.venv/bin/python trace_view.py traces/<run_id>.jsonl
.venv/bin/python benchmark_view.py benchmarks/LeeSin_JUNGLE_DIAMOND_16.20.json <match_id> "<Riot ID>"
```

---

## 工具列表

| 类别 | 工具 | 作用 |
|---|---|---|
| 数据获取 | `find_player` | Riot ID → puuid、段位 |
| | `list_recent_matches` | 最近对局列表 |
| | `get_match_summary` | 10 名玩家的终局统计 |
| 全局分析 | `gold_xp_curve` | 每分钟经济差、经验差 |
| | `turning_points` | 经济波动最大的时段及其中的击杀和资源 |
| | `segment_fights` | 把击杀聚合成团战 |
| | `objective_control` | 龙、先锋、男爵、塔、水晶的归属 |
| 个人分析 | `analyze_deaths` | 每次阵亡：击杀者、敌方人数、是否在对方半场、附近队友、随后丢掉的资源 |
| | `lane_phase_stats` | 10 / 15 分钟与对位的差距 |
| | `item_timeline` | 成装时间 |
| | `jungle_stats` | 打野视角：参团、资源争夺、反野、前 10 分钟路线 |
| | `detect_mistakes` | 规则检出的失误 |
| | `kill_log` | 每次击杀的击杀者、被击杀者、助攻，用于核对"单杀"等说法 |
| 多局 | `recent_form` | 最近 N 局趋势，只用终局统计 |
| 本机客户端 | `client_recent_matches` · `client_player` · `client_recent_form` | 读取本机客户端的对局、玩家卡片和近期状态（国服可用） |
| | `client_game_plan` | 正在进行的对局：已知选角、对位胜率、双方阵容、十人战绩、按阵容调整的出装和规则建议 |
| | `client_coach_data` | 最近最多 100 局排位的汇总：打法标签、胜局和败局对比、各英雄和各位置战绩 |
| 英雄数据 | `opgg_champion` | OP.GG 统计：梯队、胜率、符文、出装、加点、对位 |
| 知识 | `champion_info` | 官方英雄数据（中文，当前版本），支持"鳄鱼""瞎子"等外号 |
| 规划 | `update_plan` | 记录和更新复盘计划 |

---

## 数据来源与局限

**数据来源**
- Riot Match-v5（对局详情、逐分钟时间线）、League-v4（段位）、Account-v1。
- [Data Dragon](https://developer.riotgames.com/docs/lol#data-dragon)：官方静态数据，包括英雄、装备。
- 知识类问题按需进行网页搜索，不批量抓取任何网站。

**已知局限**
- 时间线里的位置只有**每分钟一次**快照；**插眼事件没有坐标**；**没有技能释放数据**。所以团战走位、视野布置、技能使用都判断不了。
- Riot API **不提供历史 LP**，只能记录从开始追踪之后的段位变化。
- 对局数据实测可回溯约 17 个月；Riot 官方的保留上限约 2 年，尚未实测验证。
- 大区目前固定为配置中的服务器，自动识别大区还没有做。
- `turning_points` 用"经济差变化最大的时段"代替胜率模型，属于代理指标。
- 规则检测存在误报，例如 2 换 1 的阵亡也会被算作"孤身深入"。

---

## 评估

### 目前已有的结果（试点，样本很小）

| 实验 | 结果 | 说明 |
|---|---|---|
| 工具层的上下文压缩 | 约 21.5 万 → 约 4.2 千 token（约 1/50） | 1 局，基于轨迹统计 |
| Baseline：模型直接读原始 JSON | 约 45 条陈述中 5 条错误，包括编造技能释放、团战结论说反；子 agent 消耗约 55 万 token | **1 局，人工核对**，仅作方向性参考 |
| 单次问答的真实成本（隔离会话） | 近期状态约 2.5 万输入 / 3 千输出 token；联网知识问答约 9.8 万 / 5.3 千 | 3 次实测 |
| 段位基准采样 | 扫描 1,017 场钻石对局，得到 40 场 16.20 李青打野 | — |

### 计划中的正式评估

- **事实核对器：** 结构化结论 + **独立于分析工具**的代码核对，判定为支持 / 矛盾 / 无法验证。
- **指标：** 事实准确率、矛盾率、无出处陈述率、关键发现 F1、拒答正确率、单次成本与延迟。
- **对比：** B0（无模型）、B1（直接读原始数据）、工具层、工具层 + 证据、工具层 + 核对修正；以及 Claude 和 DeepSeek 的跨模型对比。
- **数据集：** 50 局分层抽样 + 20 局专家标注 + 扰动对局 + 无法回答的问题。

---

## 项目结构

```
lol_review/
  riot.py           Riot API 客户端：路由、缓存、限流、重试、Data Dragon
  lcu.py            本机客户端（LCU）接口：自动发现端口和密码、对局历史 → match-v5 格式转换
  assistant.py      客户端助手：自动接受、按位置选禁、玩家战绩与标签、开黑检测、自动应用符文、自动点赞、赛后复盘触发
  playstyle.py      打法标签、侦察分、开黑检测、计分板评分
  tencent.py        国服数据（lol.qq.com）：符文推荐、技能冷却
  notes.py          玩家备注
  gameplan.py       赛前计划：阵容分析、对位、按阵容出装、规则建议、一次模型调用写计划
  lp.py             LP 记录（配合 OP.GG 往季段位和 LP 点）
  opgg.py           OP.GG 英雄梯队与出装数据（Data Dragon 中文名称和图标）
  analysis.py       确定性分析函数（不调用模型）
  benchmark.py      段位基准采样
  trace.py          轨迹记录
server.py           MCP server：22 个工具（含 5 个读取本机客户端的工具和 1 个 OP.GG 工具）
review.py           命令行：固定骨架复盘
web/
  app.py            FastAPI：对话、资料、记忆、SSE 流式输出、三种模式
  store.py          SQLite：对话、消息、用户资料、长期记忆
  bridge.py         网页与模型端之间的收件箱 + 工具调用命令行
  headless_worker.py  每条消息一个隔离的 Claude Code 会话，记录 token 与成本
  static/index.html   前端：首页、对话列表、聊天、Agent 活动时间轴、资料与设置面板
  static/overlay.html 游戏内悬浮窗
docs/screenshots/   README 截图
desktop.py          桌面版：本进程内启动服务 + pywebview 原生窗口
build_desktop.py    PyInstaller 打包脚本
trace_view.py       轨迹 → 网页
benchmark_view.py   基准对比 → 网页
```

---

## 路线图

- [ ] 事实核对器 + 50 局评估集 + 对比实验
- [ ] 接入 DeepSeek，做跨模型准确率与成本对比
- [ ] 自动识别大区；按队列和位置拆分 `recent_form`
- [ ] 规则修正：阵亡前刚完成击杀的交换不计为失误
- [ ] LP 追踪：定时记录段位快照，生成真实 LP 曲线
- [ ] 胜率模型替代经济差代理，用于转折点和表现分
- [ ] 版本公告 / Wiki 机制的 RAG 知识库
- [ ] Skill 库：agent 提议新的分析能力，通过评估闸门后才写入
- [ ] 单元测试与 CI

---

## 声明

RiftLens 是个人学习项目，未获得 Riot Games 的认可，不代表 Riot Games 或任何参与《英雄联盟》制作与管理的官方人员的观点或意见。Riot Games 和《英雄联盟》是 Riot Games, Inc. 的商标或注册商标。使用本项目需遵守 [Riot Developer Policies](https://developer.riotgames.com/policies/general)。

开发过程使用了 Claude Code 辅助编码。

## 致谢

打法标签的规则和阈值、侦察分公式、开黑判定和自动点赞的轮换方式参考了
[LoL Desktop Assistant](https://github.com/Giggitycountless/LOL-AI-Intelligence)（AGPL-3.0），在本项目中用 Python 重新实现。

## License

[MIT](LICENSE)

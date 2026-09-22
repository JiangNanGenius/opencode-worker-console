# Worker Desk

**让 Codex / Claude Code 负责判断，让 OpenCode 多模型 Worker 持续完成执行。**

[English](README.md) · [安装指引](references/installation.zh-CN.md) · [让模型帮你安装](references/agent-install.zh-CN.md)

## 安装很简单：给模型发一条消息

**只需复制下面这段话，在 Codex 或 Claude Code 中发送，模型就会按指引帮你完成安装和设置。**

```text
帮我安装并配置 Worker Desk：
https://github.com/JiangNanGenius/opencode-worker-console
请先阅读仓库中的 references/agent-install.zh-CN.md，按指引完成安装、启动服务、打开网页控台并验证结果。
复用我已有的 OpenCode 配置，安装在常规用户目录，不要放在临时对话目录里。
只有缺少供应商或模型选择、需要我完成登录时再问我。
```

安装、启动和健康检查都交给模型；只有登录账号或选择模型时才需要你介入。

Worker Desk 是运行在本机的 Agent 桥接层。它把主 Agent 的判断能力与 OpenCode 的执行能力组合成一个可运营的工作池：任务有归属，Worker 有工作区，模型有路由，长任务能持续等待，结果和错误会回到主 Agent。

## 为什么需要它

| 现实问题 | Worker Desk 的处理方式 |
| --- | --- |
| 高端模型上下文昂贵，读取大型仓库消耗快 | 把完整调查、实现和测试交给长上下文 Worker，主 Agent 只接收结论与证据 |
| 多个套餐和模型各有优势，手工选择容易失衡 | 主 Agent 只选 Fast / Normal / Deep，桥接层按能力、额度和价格时段选择实际模型 |
| 任务可能运行几十分钟，中途还要补充指令或换模型 | 持久队列、事件驱动等待、同会话引导和安全边界换模持续到任务结束 |
| OpenCode 会话、工作区、Token 和历史证据分散 | 一个带登录的网页控台统一管理任务、会话、模型、额度、统计与清理 |

执行模型需要足够强，但不需要与最前沿模型完全相同。调查仓库、实现功能、修复测试、文档、SSH 运维和部署都可以作为完整任务下发。这样通常能降低高端模型额度依赖和总体金钱成本；**总 Token 未必减少，收益取决于任务、模型和套餐**。

## 工作架构

```text
你
 └─ 主 Agent：Codex / Claude Code / 其他能调用 CLI 的协调者
     └─ Worker Desk 桥接层
         ├─ 任务归属、过程引导、结果与错误回传
         ├─ 网页控台：会话、工作区、模型、额度
         └─ OpenCode 执行层
             └─ 自定义供应商与模型 profiles
```

仓库附带 **Codex skill**。Claude Code 等协调者可以调用相同 CLI 和 JSON 任务接口，但工具、权限和技能发现机制各自独立；桥接层不会自动把 Codex 的电脑操作或连接器复制给 OpenCode。

## 从哪里开始

| 需求 | 入口 |
| --- | --- |
| 自己安装 | [中文安装指引](references/installation.zh-CN.md) / [English](references/installation.md) |
| 让模型安装 | [模型安装指引](references/agent-install.zh-CN.md) / [Agent runbook](references/agent-install.md) |
| 首次配置模型 | 控台里的 **设置向导** |
| 下发任务、检查证据 | [Skill](SKILL.md) 和 [操作手册](references/operations.md) |
| 内网或外网反代 | [账号与远程访问](references/remote-access.md) |

### 向导安装

需要 **Python 3.9+、Git、macOS 或 Linux**，不支持原生 Windows。已有 OpenCode 会被复用，缺少时安装固定的官方版本；当前集成验证版本为 **1.18.30**。桥接层运行不需要 Node 构建、数据库服务或额外 Python 包。

在自己的终端执行：

```sh
mkdir -p ~/Developer
git clone https://github.com/JiangNanGenius/opencode-worker-console.git ~/Developer/opencode-worker-console
cd ~/Developer/opencode-worker-console
python3 scripts/install.py --wizard --lang zh-CN
~/.local/bin/delegate-opencode console --open
```

向导在安装前展示选择，已有安装保留模型配置、账号和任务证据。英文提示使用 `--lang en`。

全新安装的控台账号是 **admin / admin**，开放网络访问前请修改。供应商登录在 OpenCode 中完成，再通过 **设置向导** 检查模型、路由和 Worker 偏好；向导不收集供应商 API Key。

只接一个供应商也能完整使用。安装后可在控台建立每档单模型、顺序主备、固定比例池，或显式开启
双套餐额度自适应。仓库里的 Ark/Kimi 组合只是可选预设，不是使用前提。

### 非交互安装

已确定 OpenCode 的 `provider/model` 标识时：

```sh
python3 scripts/install.py --model YOUR_PROVIDER/YOUR_MODEL
```

或主动选择 DeepSeek/Kimi 预设：

```sh
python3 scripts/install.py --preset deepseek-kimi
```

也可以安装以套餐优先的 Ark/Kimi/DeepSeek 路由：

```sh
python3 scripts/install.py --preset ark-agent-plan
```

该预设让快速任务先用 Ark Auto；普通任务按 Kimi K2.8 : 方舟 Seed Evolving = 1:1；
深度任务按原厂 Kimi K3 : 方舟 K3 = 2:1；之后依次回退到 Ark Auto 与 DeepSeek 直连。
每个成对任务池都有独立的连续负载曲线：实时额度续航在配置的比例控制点之间插值，既不改写
保存的基准策略，也不会越过经济性边界。Fast、一级降载和二级降载也分别使用自己的曲线。

这些选项只初始化**全新安装**，不会覆盖已有 profiles。`--no-start` 表示安装后不启动。登录、托管 OpenCode 路径、密码、验证、升级和排障见[完整安装指引](references/installation.zh-CN.md)。

## Agent 只选层级，桥接层负责选模型

| 内部 Profile | 可选预设模型 | 桥接层用途 |
| --- | --- | --- |
| `senior-code` | Kimi K2.8 Preview | Normal 层套餐池 |
| `deep-research` | Kimi K3 | Deep 层套餐池 |
| `fallback` | DeepSeek V4.1 Flash | 前置阶段均不可用后的最终兜底 |

Codex 只选择 Fast、Normal 或 Deep，并以 `profile=auto` 提交；具体 profile、供应商、套餐比例和兜底均由桥接层决定。上表是管理员配置视图，不是 Agent 的角色清单。预设使用 `max`；新增模型要选择供应商实际支持的思考档位。只有用户明确要求指定模型或进行受控对比时才固定 profile。详见[路由与套餐利用率](references/routing.zh-CN.md)。

Fast 也能完整处理边界和验收明确的功能、已知修复、测试、文档和常规部署，并不是机械工作档。
Normal 用于目标明确，但执行过程仍需要开放调查或综合判断的任务。Deep 用于抽象或尚不明确的目标、
未定位的系统级根因、架构取舍和特别复杂的逻辑。文件数量、运行时间、上下文大小和任务重要性
不会自动把任务变成 Deep；真正符合条件的 Deep 任务不设额外总量限制。

## 产品能力

### 完整任务委派

Worker 可以承担调查、编码、测试修复、文档、SSH 操作和常规部署，而不是只接收碎片化代码请求。每个任务都有目标、验收条件、工作区、文件或远程资源范围；默认每个所属 Codex 会话可并发 4 个 Worker，不同会话互不占用名额。

### 三档能力路由

协调 Agent 只判断任务不确定性：**Fast** 处理方向和验收明确的完整工作，**Normal** 处理仍需开放调查的具体目标，**Deep** 处理抽象目标、系统级根因和复杂取舍。桥接层再按已配置的单模型、主备链、固定比例池或额度自适应池选择供应商。一个供应商也能使用，多套餐只是增强项。

额度保护使用两条连续曲线。一级随着套餐续航下降，逐步让后段按量模型承担 Fast/Normal；Fast 按自己的来源套餐续航计算，不会被刚恢复的 Normal/Deep 套餐掩盖。二级下降更快，并暂时让自动 Normal 使用 Fast 来源池。Deep 和显式指定模型保持不变。DeepSeek 余额高于保护线时，二级高压上限为 70%：闲时更快到达上限，峰时只有套餐续航严重不足才从一级上限逐步升到 70%。周末始终按闲时处理，工作日法定节假日通过可配置订阅识别并在本机缓存。详见[路由与套餐利用率](references/routing.zh-CN.md)。

### 长任务连续执行

任务没有人为的模型轮数、工具次数或总运行时长限制。`wait` 持续阻塞并在结果出现时立即返回；当前回合结束后可在同一 OpenCode 会话里引导或换模，保留上下文和部分工作。确认的额度耗尽、用量窗口限制与模型端 429 会沿下一路由继续，不重放原始任务。

### 实时控制台

- 任务在原位展开，可查看倒序实时活动、完整脱敏消息、输入/输出/推理/缓存 Token，并向运行中的模型发送引导。
- OpenCode 会话可查找、新建、改名、分叉、归档、绑定工作区、删除并直接跳转。
- 统计页每 3 秒复用同一份用量快照，更新近 1 小时、24 小时和 30 天图表；纵轴带真实 Token 刻度，并按模型、档位、状态和 Profile 汇总。
- 任务页显示 CPU、内存、磁盘、负载和 Worker 数。已清理任务的 Token、成本和路由摘要保留在可展开的**历史账本**。

### 安全清理与可恢复证据

清理已结束任务前，系统先写入精简用量账本，再删除关联 OpenCode 会话和可释放证据。已整合或无改动的隔离工作树会释放；未整合改动保留待复核。`needs_attention` 空闲满 24 小时，或同一主 Agent 会话已有更新任务时，也可回收。

### 凭据与通知

OpenCode 保存模型认证。方舟额度查询和 Bark 只保存环境变量名或仅当前用户可读的文件引用，页面与接口不返回密钥原文。Bark 默认只推送降载、耗尽和恢复等关键节点；重要任务可单独选择完成后提醒。[凭据说明](references/credentials.md) · [远程访问](references/remote-access.md)

网页控台支持英文、简体中文、繁体中文、日文和韩文。专用 Worker 默认开启 Auto Approve；Worker 使用当前用户权限，范围与资源锁是协作控制，并非操作系统沙箱。

## 第一个任务

下面会真正调用模型并消耗额度。使用允许 Worker 读取的现有目录：

```sh
~/.local/bin/delegate-opencode submit --directory /absolute/project \
  --profile auto --tier normal --group-title '了解项目' \
  --title '整理项目结构' \
  --acceptance '返回入口、测试命令与文件证据，不修改文件。' \
  '阅读项目约定，说明目录组织、关键入口和测试方法。'
~/.local/bin/delegate-opencode wait JOB_ID
~/.local/bin/delegate-opencode collect JOB_ID
```

替换返回的 `JOB_ID`。默认等待窗口按任务档位决定：Fast 5 分钟、Normal 30 分钟、Deep 60 分钟；一旦有结果会立即返回。命令工具返回仍在运行的会话 ID 时，应继续等待同一个命令进程，使用宿主支持的最长阻塞轮询，不要额外查询状态；只有完整窗口结束且 `continue_waiting` 为真时才再次调用 `wait`。观察窗口不是任务期限。结果仍需验收。修改任务使用 `--mode write`，并提供本地 `--scope`、操作 `--target` 或两者。[更多操作](references/operations.md)。

关键里程碑可加 `--notify-on-complete`。由 Codex 针对单个任务决定；桥接层只在该任务成功完成后
发送一次 Bark，普通任务保持安静。

## 安装独立于对话

| 项目 | 默认位置 |
| --- | --- |
| CLI | `~/.local/bin/delegate-opencode` |
| 运行时和 Codex skill | `~/.codex/skills/delegate-opencode` |
| 配置 | `~/.config/opencode/delegate-pool.json` |
| 私有状态、证据、worktree | `~/.local/state/delegate-opencode` |

归档对话不会删除安装。源码保留在常规开发目录用于更新，运行时会复制出去。自定义路径使用 `DELEGATE_INSTALL`、`DELEGATE_CONFIG`、`DELEGATE_STATE`、`DELEGATE_BIN_DIR`。

## 使用边界

专用 Worker 服务默认 Auto Approve，使用当前用户的系统权限。范围和资源锁是协作机制，**不是系统沙箱**；需要时可在控台选择受限执行。

供应商登录、控台登录、内部服务密码相互独立。真实密钥不要进入任务、引导或 Git；原生网关和任意进程/工具输出不保证全部脱敏。

服务在后台运行，重启后按需拉起，没有登录守护，不保证注销/休眠期间执行。模型变更需要 Worker 和原生会话空闲；兼容的纯代码更新可保留 OpenCode 进程，先阅读安装指引。

Kimi 隐藏月额度耗尽与接口明确零额度分别处理。确认的额度、窗口和模型端 429 终止会在同一 OpenCode 会话内切换下一路由，保留上下文与部分工作，不重放原始任务。[恢复说明](references/operations.md#quota-and-routing)。

## 开发

```sh
python3 -m unittest discover -s tests -v
node --check web/i18n.js
node --check web/app.js
node --check web/manage.js
node --check web/login.js
node --check web/setup.js
```

测试使用临时目录和模拟 API，不发付费请求。Node 仅用于前端检查。CI 覆盖 macOS/Linux、Python 3.9/3.12。[贡献](CONTRIBUTING.md) · [安全](SECURITY.md)。

MIT 协议。独立社区项目，与 OpenCode、OpenAI、Anthropic、DeepSeek、Moonshot、火山引擎无隶属关系。

# Worker Desk

**连接主 Agent 与执行模型的本地多模型协作桥接层。**

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

安装步骤和检查交给模型处理；需要登录账号或选择模型时，它会引导你完成。

让主 Agent 负责统筹、关键判断和最终验收，通过 OpenCode 把完整执行任务交给其他强模型。Worker Desk 提供持久任务队列、明确的工作区、模型配置，以及可以查看执行过程的网页控台。

协作的前提，是执行模型已经足够强。调查仓库、实现功能、测试修复、文档、SSH 运维和部署，都可以作为完整任务下发。长上下文模型可以消化大型代码库和长文档，再返回结论与证据。合理分工可以降低高端模型额度依赖和总体金钱成本；**总 Token 未必减少，具体收益取决于任务、模型及套餐**。

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
实时额度续航只允许把成对比例移动一档，不会改写保存的基准策略。

这些选项只初始化**全新安装**，不会覆盖已有 profiles。`--no-start` 表示安装后不启动。登录、托管 OpenCode 路径、密码、验证、升级和排障见[完整安装指引](references/installation.zh-CN.md)。

## Profile 是模型选择，不是能力限制

| Profile | 可选预设模型 | 常见场景 |
| --- | --- | --- |
| `senior-code` | Kimi K2.8 Preview | 套餐可用时的完整通用任务 |
| `deep-research` | Kimi K3 | 大仓库、长上下文、复杂根因与审查 |
| `fast-code` | DeepSeek V4.1 Flash | 显式指定模型，或 Agent Plan 预设的最终兜底 |

所有 profile 都能承担通用任务。名称是稳定路由标识，背后模型可在控台调整。预设使用 `max`；新增模型要选择供应商实际支持的思考档位。`auto` 按任务层级选择路由，可配置派发前顺序接替与加权分配；深度复杂度优先，否则快速紧急度进入 Fast 自动层，但不会固定某个模型。启用顺序路由时，显式 profile 固定模型且必须说明原因。详见[路由与套餐利用率](references/routing.zh-CN.md)。

## 主要功能

- **任务协作**：在任务下方原位展开详情，直接查看实时活动及输入、输出、推理、缓存 Token。按当前筛选汇总已记录的 Worker 用量，不含主 Agent 用量，也不代表费用。支持父子归属、运行中引导、结构化错误和完整会话。
- **会话管理**：查找、新建、改名、分叉、归档/恢复、绑定工作区、删除、跳转 OpenCode。分叉本身不发送提示词。
- **模型、额度与经济性**：可视化配置单供应商、顺序主备、固定比例池或主动开启额度自适应，并读取 DeepSeek 余额、Kimi 套餐窗口和方舟 AFP 窗口。控台显示当前动态比例和可配置的统一成本尺（默认 `1 AFP-equivalent = ¥0.002`）；AFP-equivalent 不是 Kimi 官方额度。
- **并发执行**：默认每个所属 Codex 会话同时运行 4 个 Worker，各会话独立计数；文件/资源冲突和供应商可用性仍影响调度。
- **工作区协调**：不重叠共享范围、远程操作目标、隔离 Git worktree；补丁检查后再合入。
- **长任务**：不人为限制模型轮数、工具次数或总运行时间；持续观察引导，未知状态不盲目重放。
- **凭据引用**：复用已登录工具，必要时以本地元数据引用注入。[凭据说明](references/credentials.md)。
- **网页控台**：密码登录、内网/反代，以及英文、简体中文、繁体中文、日文、韩文界面。

## 第一个任务

下面会真正调用模型并消耗额度。使用允许 Worker 读取的现有目录：

```sh
~/.local/bin/delegate-opencode submit --directory /absolute/project \
  --profile auto --group-title '了解项目' \
  --title '整理项目结构' \
  --acceptance '返回入口、测试命令与文件证据，不修改文件。' \
  '阅读项目约定，说明目录组织、关键入口和测试方法。'
~/.local/bin/delegate-opencode wait JOB_ID --seconds 20
~/.local/bin/delegate-opencode collect JOB_ID
```

替换返回的 `JOB_ID`。`continue_waiting` 为真时继续短时等待，观察窗口不是任务期限。结果仍需验收。修改任务使用 `--mode write`，并提供本地 `--scope`、操作 `--target` 或两者。[更多操作](references/operations.md)。

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

Kimi 隐藏月额度耗尽与接口明确零额度分别处理。主 Agent 检查部分结果后续接，桥接层不会盲目换模型重放。[恢复说明](references/operations.md#quota-and-routing)。

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

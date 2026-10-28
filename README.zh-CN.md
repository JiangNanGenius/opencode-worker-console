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
| 使用语音提醒播报 | [语音提醒设置](references/qwen-voice-settings.md) |
| 在 ESP32 小屏幕查看主机、任务和独立套餐额度 | [Mac 信息牌源码与协议](examples/mac-status-board/README.md) |
| 内网或外网反代 | [账号与远程访问](references/remote-access.md) |

### 向导安装

需要 **Python 3.9+、Git、macOS 或 Linux**，不支持原生 Windows。已有 OpenCode 会被复用，缺少时安装固定的官方版本；当前集成验证版本为 **1.18.32**。桥接层运行不需要 Node 构建、数据库服务或额外 Python 包。

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

或安装可选的三套餐组合（Kimi + 方舟 Agent Plan + 阿里云 Token Plan + 直连 DeepSeek）。
该预设只声明固定的 `qwen3.8-max` 与 `qwen3.8-flash`，不透明的阿里云 Auto 别名不会再进入
自动路由：

```sh
python3 scripts/install.py --preset three-plan
```

四档默认模型为 Fast = DeepSeek Flash、Normal = K2.8 Preview / Qwen3.8 Flash、
Deep = Qwen3.8 Max、Ultra = 原厂 Kimi K3.1 Pro（标准版、max）。旧原厂和 Ark K3 不再参与新任务路由，
历史模型记录保留。Kimi 为 K3.1 Pro 默认设置 30% 的基础／最高周保护。动态释放默认开启：按真实周刷新时间，后半周闲时逐步降至基础值与 5% 中的较小值，释放给 Normal；Ultra 有需求或信号未知时保持基础值。五小时和账户总额度分别限制，缺失总池显示未知，
不按四倍周额度估算。Ultra 确认额度耗尽后默认以检查点续接 Qwen3.8 Max；两者均不可用时等待，显式锁模或 Ultra 能力底线仍等待所需模型。详见[路由说明](references/routing.zh-CN.md)。

单 Key 接入时，把 API Key 放在仅本人可读的文件中：在私有
pool 配置里设置 `provider_api_key_files` 为 `{"bailian-token-plan": "/私有/路径/key"}`，运行时
只引用路径，不复制密钥。Credits 池会经官方只读账户接口（百炼 OpenAPI `GetSubscriptionStats`，
ACS3 签名）自动刷新：把与推理密钥相互独立的阿里云账号 AK/SK 分别放入仅本人可读的文件，并按固定
引用名 `aliyun-control-ak`、`aliyun-control-sk` 注册（绝不写入配置），随后 `delegate-opencode
quota --refresh` 或控制台的刷新按钮即可获取真实剩余 Credits，并明确标注 `openapi_live` 来源。
实时查询失败时绝不伪造刷新：额度行保留最近一次真实读数（若尚无实时读数，则保留导入的控制台
快照），并附上脱敏的 `live_error`。快照导入仍可作为该基线使用：

```sh
delegate-opencode quota --import-snapshot /私有/路径/snapshot.json
```

同一到期日的多个独立阿里坐席会合并为一个 Credits 总池，明细按需展开。可选的本地认证
转发入口按顺序使用 Key，确认单把 Key 被额度或认证拒绝后切换下一把；原有单 Key 用法保持兼容。
参见[合并额度规则](references/bailian-seat-ledger.md)与[多 Key 配置及空闲启用](references/bailian-pool.md)。
个人版用量包（增量包）与月度百分比分开显示：月度额度仍有剩余时，也会显示已观测的剩余/总量/已用 Credits、
生效包数量与服务端最近到期时间；已确认耗尽的用量包从当前卡片消失但保留已观测历史；查询失败显示未知而不是零。

快照会明确显示新鲜度，绝不冒充实时读数；已过期周期或导入时已失效的正数读数不能解除已记录的
耗尽状态或计费熔断。查看按响应时间戳统计的 Token 与估算 Credits（1 小时 / 24 小时 / 48 小时）：

```sh
delegate-opencode usage-report --windows 1,24,48
```

原生 OpenCode 自动升级默认开启、每 24 小时检查一次。桥接会在任务运行期间先下载并暂存官方稳定版，
仅在没有任何任务执行时进行短暂的串行重启；只有排队任务时会先保留准入屏障，绝不取消任务；健康或
版本校验失败会恢复旧二进制与配置。

这些选项只初始化**全新安装**，不会覆盖已有 profiles。`--no-start` 表示安装后不启动。登录、托管 OpenCode 路径、密码、验证、升级和排障见[完整安装指引](references/installation.zh-CN.md)。

## Agent 只选层级，桥接层负责选模型

| 内部 Profile | 可选预设模型 | 桥接层用途 |
| --- | --- | --- |
| `senior-code` | Kimi K2.8 Preview | Normal 层套餐池 |
| `aliyun-max` | Qwen3.8 Max | Deep 推理 |
| `ultra-reasoning` | Kimi K3.1 Pro | Ultra 推理；确认额度耗尽后可续接 Qwen3.8 Max |
| `fallback` | DeepSeek V4.1 Flash | 前置阶段均不可用后的最终兜底 |

上游 Harness 只选择 Fast、Normal、Deep 或 Ultra，并以 `profile=auto` 提交；具体 profile、供应商、套餐比例和兜底均由桥接层决定。上表是管理员配置视图，不是 Agent 的角色清单。预设使用 `max`；新增模型要选择供应商实际支持的思考档位。只有用户明确要求指定模型或进行受控对比时才固定 profile。详见[路由与套餐利用率](references/routing.zh-CN.md)。

Fast 也能完整处理边界和验收明确的功能、已知修复、测试、文档和常规部署，并不是机械工作档。
Normal 是日常具体目标的默认档，Deep 处理需要较强推理的复杂工作，Ultra 处理极复杂架构、疑难根因与长程推理。
连续两轮无实质进展时，协调者复核检查点后升级；认证、权限和环境故障先解决阻碍。问题明确后的常规实施交给 Normal。
文件数量、运行时间与重要性不会单独决定档位。

## 产品能力

### 完整任务委派

Worker 可以承担调查、编码、测试修复、文档、SSH 操作和常规部署，而不是只接收碎片化代码请求。每个任务都有目标、验收条件、工作区、文件或远程资源范围；默认每个所属上游 Harness 会话可并发 4 个 Worker，不同会话互不占用名额。

修改任务默认直接使用已绑定的共享/主工作区，大型和长时间任务也一样。只有用户明确要求隔离、
并行任务存在无法避免的写入冲突，或破坏性实验时才创建独立 worktree，避免重复依赖和编译产物。
安全整合后会释放受管 worktree；未整合修改继续保留并提示复核。

### 四档能力路由

协调 Agent 按不确定性与模型延迟对交付的具体作用选择 Fast、Normal、Deep 或 Ultra，默认 Normal。
桥接层选模型，Ultra 优先原厂 K3.1 Pro，确认额度耗尽后默认续接 Qwen3.8 Max；两者均不可用时等待。显式锁模和 `capability_floor=ultra` 保持所需模型。检查点交接与动态周保护规则见[路由说明](references/routing.zh-CN.md)。

额度保护同时计算总体曲线和各供应商自己的曲线。一个套餐紧张时可以从同能力池完全退出，由健康伙伴承担 100%，而总体池仍可保持健康。一级随着套餐续航下降，逐步让后段按量模型承担 Fast/Normal；Fast 按自己的来源套餐续航计算，不会被刚恢复的 Normal/Deep 套餐掩盖。二级下降更快，在 DeepSeek 余额高于保护线时可把只有单一套餐来源的 Fast 路由完整转给 DeepSeek。Deep 和显式指定模型不会掉到更低能力阶段。闲时更快到达高压上限，峰时只有套餐续航严重不足才逐步升高。周末始终按闲时处理，工作日法定节假日通过可配置订阅识别并在本机缓存。详见[路由与套餐利用率](references/routing.zh-CN.md)。

### 长任务连续执行

任务没有人为的模型轮数、工具次数或总运行时长限制。`wait` 持续阻塞并在结果出现时立即返回；可在安全回合边界引导；进入 Ultra 时保留桥接任务、授权和检查点，新建原生模型会话，不重放已完成操作。确认的额度耗尽、用量窗口限制与模型端 429 会沿下一路由继续，不重放原始任务。

### 实时控制台

- 任务在原位展开，可查看倒序实时活动、完整脱敏消息、输入/输出/推理/缓存 Token，并向运行中的模型发送引导。
- OpenCode 会话可查找、新建、改名、分叉、归档、绑定工作区、删除并直接跳转。
- 桥接层和网页控台可查询、新增、编辑、置顶和删除项目长期记忆。记忆后端可以是旧的 `opencode-mem` 服务，也可以是仅回环监听的 OpenViking 服务（官方预编译包，Ark 向量 + DeepSeek Flash 抽取），其数据、向量和迁移日志都保存在本机内部磁盘。滚动记忆只在超过容量上限后回收最旧且未置顶的记录，保留置顶项（桥接日志与原生 `pinned=true` 标签），并在下次服务启动时执行（即使关机一个月）；删除会话不会按天数淘汰记忆。OpenViking 仅监听回环地址，数据访问需要仅所有者可读的 API 密钥，模型凭据一律使用环境变量引用。控制台记忆页可按真实的项目/类别/目录树浏览，带分页与长度受限的预览，逐条查看按真实情况标注的 L0/L1/L2 层级，支持语义搜索，并提供不产生任何修改的容量预览与需要确认的维护执行。详见 [OpenViking 记忆](references/openviking-memory.md)。
- 大型编译、部署和 GitHub Actions 可上报真实阶段、百分比和预计完成时间；没有可靠总量的任务显示为不确定进度，不编造百分比。
- 统计页每 3 秒复用同一份用量快照，更新近 1 小时、24 小时和 30 天图表；纵轴带真实 Token 刻度，并按模型、档位、状态和 Profile 汇总。
- 任务页显示 CPU、内存、磁盘、负载和 Worker 数。已清理任务的 Token、成本和路由摘要保留在可展开的**历史账本**。
- **模型与路由**页新增独立的**语音提醒**卡片，对接已安装的 `qwen-voice-notify` 技能：默认通过现有火山方舟 Agent Plan 的 `seed-tts-2.0` 模型优先使用云端语音，云端在播放开始前失败时自动回退到本机离线 Qwen3-TTS，播放开始后不再回退；云端音色与本机备用音色分别选择，共用语速默认 1.10×（0.50–2.00×），设置原子保存并同时供控台和 `notify.py` 使用，缺少的新字段保持默认值，旧版保存不会重置它们。**试听**为显式固定的短句，停止只结束本控台发起的试听，GET 不合成也不访问网络。不会自动播放，不修改系统音量或默认输出设备，不下载模型，也不常驻推理服务。详见[语音提醒设置](references/qwen-voice-settings.md)。

### 主机概览与结构化控制

公网部署支持 **仅 Passkey 登录**，独立内网地址保留用户名、密码登录。
控台与原生 OpenCode 共用认证网关，覆盖实时事件与 WebSocket。
在 **Passkey / 安全登录** 中管理多设备凭据与撤销，全部遗失时可在 Mac 本机恢复；
部署步骤见[公网访问指引](references/remote-access.md#public-access-with-passkeys)。

首页与调度页的 Codex 任务可展开查看子代理和精确关联的 OpenCode Worker，
展示当前模型、思考强度及数据更新时间。此视图只读本机任务，不编造进度百分比。

登录后默认进入**主机概览**：CPU、GPU、内存、内核内存压力、芯片温度、风扇转速、网络速率、磁盘 I/O 与内置/外置磁盘容量图表（15 分钟 / 1 小时 / 24 小时），进行中的 Codex 与 OpenCode 任务（含真实模型与已运行时长）、各供应商额度和受管服务状态。同一采集器同时服务网页与实体信息牌；信息牌负载以仅属主可读的 `STATE/host-board.json` 按既有 v2 结构发布。缺失的传感器显示为不可用，而不是 0。

渐进展开的抽屉提供固定且经过校验的控制——没有终端，也没有文件浏览器：通过本机风扇守护进程切换档位或固定转速（绝不触碰其安全下限）、切换 macOS 支持的电源模式并回读确认、受管的保持唤醒（caffeinate）、结束本用户进程（先 TERM 再显式 KILL，校验 PID 与启动时间）、重启受管服务，以及睡眠/重启/关机（展示受影响任务、要求最近密码验证并持久化操作状态）。有破坏性的动作经由一个窄权限的 root 助手执行，须先安装并通过校验，否则一律报告不可用。安装、能力、配置路径与回滚见 [Host controls](references/host-controls.md)。

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

关键里程碑可加 `--notify-on-complete`。由上游 Harness 针对单个任务决定；桥接层只在该任务成功完成后
发送一次 Bark，普通任务保持安静。

## 安装独立于对话

| 项目 | 默认位置 |
| --- | --- |
| CLI | `~/.local/bin/delegate-opencode` |
| 运行时和 Codex skill | `~/.codex/skills/delegate-opencode` |
| 配置 | `~/.config/opencode/delegate-pool.json` |
| 私有状态、证据、worktree | `~/.local/state/delegate-opencode` |
| 项目记忆，旧存储（可配置） | `~/.local/state/delegate-opencode/opencode-mem` |
| 项目记忆，OpenViking 数据与日志 | `~/.local/state/delegate-opencode/openviking` |
| OpenViking 模型配置（仅所有者，环境变量引用） | `~/.openviking/ov.conf` |

归档对话不会删除安装。源码保留在常规开发目录用于更新，运行时会复制出去。自定义路径使用 `DELEGATE_INSTALL`、`DELEGATE_CONFIG`、`DELEGATE_STATE`、`DELEGATE_BIN_DIR`。

## 使用边界

专用 Worker 服务默认 Auto Approve，使用当前用户的系统权限。范围和资源锁是协作机制，**不是系统沙箱**；需要时可在控台选择受限执行。

供应商登录、控台登录、内部服务密码相互独立。真实密钥不要进入任务、引导或 Git；原生网关和任意进程/工具输出不保证全部脱敏。

服务在后台运行，重启后按需拉起，没有登录守护，不保证注销/休眠期间执行。模型变更需要 Worker 和原生会话空闲。Worker Desk 会按设置检查 OpenCode 稳定版本，只在任务与原生会话都空闲时升级；重启核对版本失败会恢复原可执行文件。兼容的纯代码更新可保留 OpenCode 进程，先阅读安装指引。

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

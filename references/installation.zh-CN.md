# 安装与设置

[English](installation.md) · [README](../README.zh-CN.md) · [模型安装指引](agent-install.zh-CN.md)

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

想自己动手安装，也可以按下面的步骤操作。

## 1. 检查环境

- macOS 或 Linux/POSIX、Python 3.9+、Git。
- OpenCode 支持的供应商账号。可以先安装桥接层，再登录；执行任务需要可用额度。
- 缺少 OpenCode 时需要下载网络。优先通过 npm 安装，否则下载并校验官方发布包；npm 不是桥接层运行依赖。
- 当前集成验证 OpenCode 1.18.30。已有可执行文件会复用，不会偷偷降级。

```sh
python3 --version
git --version
```

原生 Windows 不支持。WSL 等 Linux 环境需满足 Linux 条件，Windows 宿主集成未验证。

## 2. 放在常规开发目录

```sh
mkdir -p ~/Developer
git clone https://github.com/JiangNanGenius/opencode-worker-console.git ~/Developer/opencode-worker-console
cd ~/Developer/opencode-worker-console
```

目录已存在时先检查，保留修改，不要覆盖克隆。唯一源码副本不要放在随会话清理的目录或临时 worktree 中。

## 3. 选择安装方式

### 用户在终端运行向导

```sh
python3 scripts/install.py --wizard --lang zh-CN
# 英文：
python3 scripts/install.py --wizard --lang en
```

向导收集自定义 OpenCode 模型 ID 或主动选择的 DeepSeek/Kimi 预设、启动偏好，最后展示确认。已有安装保留配置。确认前取消不会安装或下载。向导需要交互终端；自动化使用下面的参数。

安装后可运行 `~/.local/bin/delegate-opencode setup --lang zh-CN` 再次进入终端向导，
英文使用 `--lang en`。已有模型保持不变，模型设置在网页向导调整。
终端向导支持通过隐藏输入设置初始管理员密码，跳过则保留公开的本地默认值，
不会重置已有账号。

### 模型或脚本非交互安装

全新安装二选一：

```sh
# 替换成真实 provider/model 标识。
python3 scripts/install.py --model YOUR_PROVIDER/YOUR_MODEL

# 或明确选择适合自己账号的预设。
python3 scripts/install.py --preset deepseek-kimi
python3 scripts/install.py --preset ark-agent-plan

# 可追加 --no-start，仅安装文件而不启动服务。
```

自定义模型初始映射到三个 profile。`deepseek-kimi` 配置 DeepSeek V4.1 Flash、Kimi K2.8
Preview、Kimi K3；`ark-agent-plan` 还加入方舟 Auto、Seed Evolving、方舟 K3 和
[路由说明](routing.zh-CN.md)中的套餐优先加权策略。两种预设均使用 `max`。预设不会创建账号
或赋予模型权限。已有安装即使传入模型/预设参数，也保留原配置。

安装后可在**模型与路由**中配置单套餐、一个主模型加一个备用、固定多模型池，或主动开启额度
自适应。用户只有一个供应商时从 `--model` 开始即可；预设只适用于已经拥有对应账号的用户。

安装器输出实际路径与本地地址；**端口自动选择，没有固定端口号**。运行时和 CLI 独立于源码目录。

## 4. 在 OpenCode 登录供应商

已经使用 OpenCode 时复用登录。否则通过控台提供的 OpenCode 入口登录，或在自己的终端执行：

```sh
opencode auth login
opencode models
```

自动托管安装的 OpenCode 不一定加入 PATH。可以读取配置中的可执行路径来调用，无需打开凭据文件：

```sh
python3 - <<'PY'
import json, os, pathlib, subprocess
path = pathlib.Path(os.environ.get('DELEGATE_CONFIG', '~/.config/opencode/delegate-pool.json')).expanduser()
binary = json.loads(path.read_text())['opencode_binary']
subprocess.run([binary, 'auth', 'login'], check=True)
PY
```

把调用列表改为 `[binary, 'models']` 可列出模型标识。登录、扫码和密钥输入由用户完成，不要粘贴到模型对话、任务 JSON 或仓库。

修改供应商登录后，先拉起缺失服务。运行中的目录仍未刷新时，等待任务和原生会话空闲后重启，再刷新向导，不要为完成安装而中断其他人的任务。

官方说明：[OpenCode CLI](https://opencode.ai/docs/cli/) / [供应商](https://opencode.ai/docs/providers/)。

## 5. 打开控台并设置账号

```sh
~/.local/bin/delegate-opencode console --open
```

命令会拉起缺失服务并打开实际控台地址。自定义 `DELEGATE_BIN_DIR` 时使用安装器输出的路径。若想直接输入 `delegate-opencode`，把 `~/.local/bin` 加入 shell PATH。

全新控台默认 **admin / admin**，升级不会重置已有密码。用隐藏输入修改账号：

```sh
python3 ~/.codex/skills/delegate-opencode/scripts/console_auth.py set-user --username admin
```

自定义安装时替换实际脚本路径，并沿用 `DELEGATE_STATE` 等覆盖变量。密码在终端输入，不作为参数；修改后所有现有网页登录失效。

先保持仅本机访问。内网监听、精确允许来源、HTTPS 反代和 WebSocket/SSE 配置见[远程访问](remote-access.md)。开放远程访问前修改默认密码。供应商凭据和控台密码不是同一个东西。

## 6. 完成网页设置向导

导航中打开 **设置向导 / Setup wizard**，以后可随时回来调整，不强制阻挡正常使用。

1. **供应商连接**：查看已连接供应商和模型；需要时打开 OpenCode 登录，然后刷新。
2. **模型配置**：保留已有 profiles、明确应用预设或自定义模型及路由；只选择模型支持的思考档位。
3. **Worker 偏好**：Auto Approve、每会话独立并发、可选 Kimi 月账期。使用自己的日期与时区，不套用别人的账期。
4. **检查并应用**：确认草稿后保存，需要时再创建首个任务。上下步切换不会保存服务端设置。

保存要求 Worker 和原生会话空闲。版本过期说明其他客户端修改过配置，需要重新加载再检查。返回/取消不修改服务端，未编辑的清理策略等设置会保留。

配置已保存、供应商已连接、真实请求成功是三件事。向导不会自动发起付费模型请求。

## 7. 验证安装

```sh
~/.local/bin/delegate-opencode service start
~/.local/bin/delegate-opencode doctor
~/.local/bin/delegate-opencode quota --refresh
```

检查 JSON 内容，不只看退出码：`doctor` 正常退出也可能包含 `server_error`。查看服务健康、注册 profiles、`daemon.healthy`。额度适配器覆盖 DeepSeek、Kimi 和方舟 Agent Plan；方舟 AFP 查询还要配置[凭据说明](credentials.md)中的控制面引用。

需要真实执行验证时，主动运行 [README 首个任务](../README.zh-CN.md#第一个任务)，收集报告并检查证据；这会消耗模型额度。仅服务健康不等于模型可用或任务验收通过。

## 升级与停止

在长期保留的源码目录执行：

```sh
git status --short
git pull --ff-only
python3 scripts/install.py
```

保留本地修改，不用重置覆盖分歧。普通升级拒绝活跃 Worker；等待任务完成，也检查原生会话。不要自动取消其他对话的任务。

运行中只更新兼容的**纯代码**：

```sh
python3 scripts/install.py --live
```

它重启观察器和控台，保留 OpenCode 会话，不能同时改变模型/可执行文件或使用 `--no-start`，也不是绕过需停机迁移的通用开关。配置、账号、任务证据保留，旧运行时备份到私有状态的 `releases/`。

停止服务但保留数据：

```sh
python3 scripts/install.py --stop
```

停止会影响活跃任务。重启电脑后，下次提交或 `console --open` 会拉起缺失服务，不安装开机登录守护。

## 路径与隔离安装

| 覆盖变量 | 默认值 |
| --- | --- |
| `DELEGATE_INSTALL` | `~/.codex/skills/delegate-opencode` |
| `DELEGATE_BIN_DIR` | `~/.local/bin` |
| `DELEGATE_CONFIG` | `~/.config/opencode/delegate-pool.json` |
| `DELEGATE_STATE` | `~/.local/state/delegate-opencode` |

隔离安装同时设置四个不同路径，并始终使用对应生成的 wrapper；wrapper 保存显式覆盖变量。配置/状态不要提交 Git，也不要为了重试安装而删除 worktree 和证据。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 找不到 CLI | 使用安装器输出的完整路径，检查 PATH |
| 全新安装提示缺少模型 | 使用向导、`--model provider/model` 或明确选择预设 |
| 向导拒绝非交互输入 | 使用非交互参数；模型按专用指引操作 |
| 供应商未连接 / 鉴权错误 | 在 OpenCode 登录，确认 provider ID，空闲后刷新 |
| 模型不支持 max | 从实际模型目录选择支持的思考档位 |
| 配置繁忙 / 版本过期 | 等待任务或重新读取最新配置，不强行覆盖 |
| 重启后服务没运行 | `service start` 后检查 `doctor` |
| 内网或反代返回 403 | 检查精确 origin、Host 和反代设置 |
| Kimi 窗口有余额但月额度报错 | 按错误恢复信息处理，窗口非零不能证明总额度恢复 |
| 下载失败 | 检查 npm/GitHub 网络，或按官方方式安装后传 `--opencode /absolute/executable` |

更多执行与恢复细节见[操作手册](operations.md)，模型自动安装见[专用指引](agent-install.zh-CN.md)。

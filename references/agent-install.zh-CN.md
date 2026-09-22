# 给模型执行的安装指引

[English](agent-install.md) · [人工安装指引](installation.zh-CN.md)

适用对象：受用户委托安装或更新 Worker Desk 的 Codex、Claude Code 等 Agent。把已授权安装落实到验证完成。使用确定的非交互 CLI 参数，终端交互向导留给用户。本文件本身不构成自动安装请求。

## 目标与必要输入

交付长期保留的用户级安装，保留已有状态，提供可用控台、模型配置和准确的就绪报告。全新安装需要稳定源码目录，以及用户允许使用的 provider/model 或明确预设。优先复用对话选择和已有配置，只在缺少实质模型/账号选择时询问。

不要索取、读取或输出原始 API Key、密码、SSH 私钥、浏览器 Cookie。复用 OpenCode 和已有工具的认证。登录、扫码、凭据输入和新增持久授权由用户完成。

## 修改前检查

1. 检查系统、Python 3.9+ 和 Git；原生 Windows 不支持，不宣称完成 Windows 集成验证。
2. 定位已有 wrapper 和显式 `DELEGATE_*` 覆盖。默认运行时 `~/.codex/skills/delegate-opencode`，状态 `~/.local/state/delegate-opencode`，配置 `~/.config/opencode/delegate-pool.json`。
3. 已安装时，在可能中断服务的更新前检查任务和原生会话。只输出需要的 ID/状态/归属，不把全部历史灌进上下文。配置只读取必要的非秘密字段，不打印整份配置或凭据文件。
4. 复用常规目录，通常 `~/Developer/opencode-worker-console`；检查 `git status --short` 和 remote。保留无关修改，不顺手 reset/clean/stash。

全新安装示例：

```sh
mkdir -p ~/Developer
git clone https://github.com/JiangNanGenius/opencode-worker-console.git ~/Developer/opencode-worker-console
cd ~/Developer/opencode-worker-console
```

目录已经存在时先检查复用，不覆盖克隆。

## 非交互安装

使用用户真实模型标识，**不能照抄占位符**：

```sh
python3 scripts/install.py --model YOUR_PROVIDER/YOUR_MODEL
```

只有用户选择或已有上下文明确时才用 `--preset deepseek-kimi` 或
`--preset ark-agent-plan`。方舟预设会加入套餐优先加权策略，需要原厂 Kimi、方舟 Agent Plan
和 DeepSeek 直连访问权；预设不创建账号，也不授予模型访问权。用户只要求安装文件时追加
`--no-start`。

不要推断用户必须有多个供应商。只有一个套餐时，`--model` 就是完整安装；以后可在网页控台增加
顺序备用、固定比例池或显式额度自适应，无需重装。

已有 OpenCode 复用，缺少时安装固定官方版本。安装器创建独立运行时/状态并输出实际地址，不能硬编码端口。覆盖变量为 `DELEGATE_INSTALL`、`DELEGATE_STATE`、`DELEGATE_CONFIG`、`DELEGATE_BIN_DIR`，后续统一使用生成的 wrapper。

已有安装用 `python3 scripts/install.py` 更新，保留配置、账号和证据；模型/预设参数不覆盖已有 profiles。非交互工具不要运行 `--wizard`，也不要伪造 TTY 回答。

## 供应商与控台

- 使用实际 wrapper 执行 `console --open`，默认 `~/.local/bin/delegate-opencode`。
- 全新本地控台默认 `admin / admin`，不重置已有账号，真实凭据不进入对话或日志。
- 供应商未登录时先完成不依赖它的安装，再引导用户在 OpenCode 登录。`opencode_binary` 是实际可执行路径，不一定在 PATH。
- 用户自行执行 `opencode auth login` 或对应托管可执行文件。不要自动处理新增授权或把密钥写进命令。
- 网页**设置向导**读取真实供应商状态和模型档位，明确应用前保持草稿。Auto Approve 只作用于专用 Worker 服务，不代表超出任务授权。
- 模型配置通过现有 API/控台在空闲时调整，不绕过版本检查或活跃任务检查直接改写运行配置。
- 普通本地安装不顺手开放网络；用户要求内网/反代时，按[远程访问说明](remote-access.md)完成已授权工作，使用用户自己的账号和精确来源。

自定义安装的直接维护命令需使用同样环境；生成 wrapper 会保留显式覆盖。

## 分层验证

```sh
~/.local/bin/delegate-opencode service start
~/.local/bin/delegate-opencode doctor
~/.local/bin/delegate-opencode quota --refresh
```

检查 JSON，而不只看退出码。`doctor` 可能返回 `server_error`；核对服务健康、profiles 注册和 daemon 心跳。无额度适配器不等于不可用；目录里存在模型、有额度样本，也不等于真实推理成功。

用户请求包含端到端验证且允许使用账号额度时，在明确安全的目录提交一个小型只读任务，选择合适 profile，等待完成，检查真实模型、结果和错误证据。用短时 `wait` 持续观察，不因耗时长取消，也不要让用户反复说“继续”。安装冒烟无需全仓库昂贵调查。

只展示向导或运行 fixture 测试不需要真实模型请求。未执行的模型检查写明“未验证”。如果仅剩用户登录，保留已完成安装并准确说明这一个阻塞项。

## 更新与恢复

- 检查源码和修改后使用 `git pull --ff-only`。
- 普通更新等待空闲，不取消其他会话任务。
- `python3 scripts/install.py --live` 只适用兼容纯代码更新，重启观察器/控台并保留 OpenCode，不更改模型/可执行文件；不把它当作所有迁移都能用的开关。
- 未知是否接受的任务先检查原任务/会话，不盲目重放有副作用操作。
- 确认的供应商额度/窗口错误和模型端 429 默认会在同一 OpenCode 会话内切换下一路由继续，需检查 `route_history` 与部分工作。若关闭自动转路由或已无可用路由，再按同一层级以 `profile=auto` 续接；接口明确零额度仍不可用，Kimi 隐藏月额度保留独立显式重试路径。
- 不删除账本、认证、worktree 或证据来“重装”。旧运行时备份在私有状态 `releases/`，恢复需保留当前配置和任务并考虑活跃执行。

## 交给主 Agent 使用

默认安装把 `delegate-opencode/SKILL.md` 放到 Codex 用户技能目录。发现机制取决于调用方，不宣称运行中的对话已经热加载；CLI 可以立即用绝对路径调用。

Claude Code 等协调者可阅读安装后的 [SKILL.md](../SKILL.md)，调用同一 CLI/JSON 协议。没有自动安装到所有 Agent 技能目录，也不会自动共享 Codex 专属工具。

调用方没有上游 Harness 会话身份时，为该对话传入稳定的 `--group-id` 和可读的
`--group-title`，续接继续用同一个 ID。它是调度归属的后备标识，不能用来绕过并发或资源协调。

全局提示只保留简短入口，细节链接到操作文档。给 Worker 完整目标，不把 profile 名称当作能力白名单。

## 交付报告

只报告必要的非秘密信息：

- 源码、运行时、CLI 的实际路径。
- 控台地址，以及是否真正打开验证。
- 选择的 profiles，或保留了已有配置。
- 安装、服务健康、供应商连接、真实执行分别通过与否。
- 尚需用户登录或选择模型时，指出具体下一步。
- 更新是否保留 OpenCode，是否影响运行任务。

不附真实余额、凭据、完整会话转储，也不凭安装成功宣称测得省钱或省 Token。

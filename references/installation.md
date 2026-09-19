# Installation and setup

[简体中文](installation.zh-CN.md) · [README](../README.md) · [Agent runbook](agent-install.md)

## 1. Check prerequisites

- macOS or Linux/POSIX, Python 3.9 or later, and Git.
- A provider account supported by OpenCode. You may install the bridge before signing in, but model work needs a usable account.
- Network access to obtain OpenCode if it is missing. The bootstrap uses npm when available; otherwise it downloads a checksum-verified official release. npm is optional, not a bridge runtime dependency.
- OpenCode 1.18.30 is integration-tested. An existing binary is reused, not silently downgraded.

```sh
python3 --version
git --version
```

Native Windows is unsupported. A Linux environment such as WSL must satisfy the Linux prerequisites; Windows host integration is not validated.

## 2. Keep the source in a stable directory

```sh
mkdir -p ~/Developer
git clone https://github.com/JiangNanGenius/opencode-worker-console.git ~/Developer/opencode-worker-console
cd ~/Developer/opencode-worker-console
```

If that directory already exists, inspect it instead of cloning over it. Keep local changes. Do not put the only checkout in a disposable agent worktree or conversation directory.

## 3. Choose an installation method

### Human-operated terminal wizard

```sh
python3 scripts/install.py --wizard --lang en
# Chinese:
python3 scripts/install.py --wizard --lang zh-CN
```

The wizard collects a custom OpenCode model ID or an explicit DeepSeek/Kimi preset, startup preference, and a final review. Existing installations retain their saved configuration. Cancelling before confirmation does not install or download anything. Run this in an interactive terminal; automation should use the flags below.

After installation, reopen the terminal wizard with
`~/.local/bin/delegate-opencode setup --lang en` (or `--lang zh-CN`).
It preserves existing models; change model settings in the web wizard.
The terminal wizard can set the initial admin password through hidden input;
skipping retains the documented local default. It never resets an existing account.

### Noninteractive / AI-operated

Choose **one** fresh-install method:

```sh
# Replace the placeholder with an actual provider/model identifier.
python3 scripts/install.py --model YOUR_PROVIDER/YOUR_MODEL

# Or opt into the preset if those accounts/models are appropriate.
python3 scripts/install.py --preset deepseek-kimi

# Append --no-start to provision files without starting services.
```

A custom model initially backs all three profiles. The preset configures DeepSeek V4.1 Flash, Kimi K2.8 Preview and Kimi K3 with `max` reasoning. A preset does not create provider accounts or grant model access. Existing configurations are preserved even when model/preset flags are supplied.

The installer prints its actual paths and loopback URLs; **ports are selected locally, not fixed**. It installs a CLI wrapper and copies runtime/skill files outside the source checkout.

## 4. Connect your provider in OpenCode

If you already use OpenCode, reuse that authentication. Otherwise, log in through OpenCode's provider UI (the console links to it), or in your own terminal:

```sh
opencode auth login
opencode models
```

When OpenCode was bootstrapped privately, `opencode` might not be on your shell PATH. Use the configured binary without printing any credential file:

```sh
python3 - <<'PY'
import json, os, pathlib, subprocess
path = pathlib.Path(os.environ.get('DELEGATE_CONFIG', '~/.config/opencode/delegate-pool.json')).expanduser()
binary = json.loads(path.read_text())['opencode_binary']
subprocess.run([binary, 'auth', 'login'], check=True)
PY
```

Use `[binary, 'models']` in the same pattern to list model identifiers. Login/QR/API-key entry is a user step. Do not paste secrets into agent chat, task JSON or repository files.

After changing provider auth, start services if needed. If the running server's catalog remains stale, restart only when existing work is idle; then reload the wizard. Never stop someone else's running task to finish onboarding.

Official references: [OpenCode CLI](https://opencode.ai/docs/cli/) and [providers](https://opencode.ai/docs/providers/).

## 5. Open the console and set the account

```sh
~/.local/bin/delegate-opencode console --open
```

This starts missing services and opens the printed console URL. With a custom `DELEGATE_BIN_DIR`, use the wrapper path printed by the installer. If you prefer `delegate-opencode` without its full path, add `~/.local/bin` to your shell PATH.

A fresh console uses **admin / admin**. Existing passwords are never reset by an update. Change the local account with a hidden prompt:

```sh
python3 ~/.codex/skills/delegate-opencode/scripts/console_auth.py set-user --username admin
```

For a custom runtime location, replace the path with the installed path. Run with the same `DELEGATE_STATE`/configuration overrides if you use them. The password is entered in the terminal, not as an argument. Changing it invalidates existing web login sessions.

Keep loopback-only access initially. LAN binding, exact public origins, HTTPS reverse proxy and WebSocket/SSE forwarding are described in [remote access](remote-access.md). Change the default password before enabling remote access. Provider credentials and the console password are different credentials.

## 6. Complete the web Setup wizard

Use **Setup wizard / 设置向导** in the navigation. You can return to it later; it does not interrupt normal console use.

1. **Provider readiness:** inspect connected providers and available models. Open OpenCode to authenticate if needed, then refresh.
2. **Models:** retain your current profiles, deliberately select the preset, or customize model choices and routing. Use only supported reasoning variants.
3. **Worker preferences:** review Auto Approve, independent per-conversation concurrency and the optional Kimi monthly billing reset. Use your actual billing date/time zone; there is no universal personal reset date.
4. **Review and apply:** inspect the draft, explicitly save, and proceed to a first task when ready. Moving between steps does not apply settings.

Saving requires idle workers and native sessions. A stale-revision error means another client changed settings; reload and review rather than overwriting it. Returning/cancelling keeps server settings intact. Existing cleanup settings are not silently reset.

A configured profile, connected provider and successful model request are three different checks. Setup does not automatically send a paid model request.

## 7. Verify the installation

```sh
~/.local/bin/delegate-opencode service start
~/.local/bin/delegate-opencode doctor
~/.local/bin/delegate-opencode quota --refresh
```

Inspect the JSON, not just the exit status: `doctor` can report a `server_error` in a successful CLI invocation. Check server health, registered profiles and `daemon.healthy`. Usage adapters currently cover DeepSeek and Kimi; another provider lacking quota telemetry is not necessarily unusable.

For actual execution verification, explicitly run the [README first task](../README.md#first-task), review `collect`, and inspect a transcript if necessary. This uses model allowance. A service-health check alone does not prove provider access or task quality.

## Updating and stopping

From the maintained source checkout:

```sh
git status --short
git pull --ff-only
python3 scripts/install.py
```

Preserve local changes; resolve divergence without resetting them. Ordinary updates refuse active workers. Wait for owned tasks to finish, and check native sessions too. Do not automatically cancel another conversation's work.

For a compatible **code-only** update while sessions run:

```sh
python3 scripts/install.py --live
```

This restarts the observer and console, retaining OpenCode sessions. It cannot change models/binaries or be combined with `--no-start`. It is not a generic bypass for migrations needing idle services. Existing configuration/account/evidence are retained, and the previous runtime is backed up under private state `releases/`.

To stop services while retaining all data:

```sh
python3 scripts/install.py --stop
```

Stop is disruptive to active execution. After a reboot, the next submission or `console --open` starts missing processes; no automatic login service is installed.

## Paths and alternate installations

| Environment override | Default |
| --- | --- |
| `DELEGATE_INSTALL` | `~/.codex/skills/delegate-opencode` |
| `DELEGATE_BIN_DIR` | `~/.local/bin` |
| `DELEGATE_CONFIG` | `~/.config/opencode/delegate-pool.json` |
| `DELEGATE_STATE` | `~/.local/state/delegate-opencode` |

Set all four to separate paths for an isolated installation, then consistently use its generated wrapper. The wrapper preserves explicitly supplied overrides. Private config/state must not be committed. Do not delete state/worktrees just to retry installation.

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| CLI not found | Use the full printed wrapper path; check PATH |
| Fresh install asks for a model | Use the wizard, `--model provider/model`, or an explicitly chosen preset |
| Wizard refuses noninteractive input | Use noninteractive flags; a model should follow the agent runbook |
| Missing provider / authentication error | Authenticate in OpenCode, confirm exact provider ID and refresh when idle |
| `max` not supported | Select a reasoning variant from that model's live catalog |
| Settings busy / revision changed | Let work finish or reload the latest settings; do not force overwrite |
| Services absent after restart | Run `service start`, then `doctor` |
| Browser 403 on LAN/proxy | Check exact allowed origin, Host and proxy configuration |
| Kimi window positive but monthly request fails | Read the returned billing recovery; positive windows alone do not prove total-quota recovery |
| Download failure | Check npm/GitHub network access, or install OpenCode through its official method and pass `--opencode /absolute/executable` |

For detailed delegation/recovery use [operations](operations.md); for agent-led installation use [the runbook](agent-install.md).

#!/usr/bin/env python3
"""Bilingual (English / Simplified Chinese) interactive setup wizard.

The wizard only collects and reviews choices. Every mutation (config, state,
package copy, service start, downloads) is performed by scripts/install.py
after the final review is confirmed, so cancelling or closing stdin at any
prompt aborts before the installer runs and nothing on disk changes.

Provider secrets are never requested or printed: provider login stays with
OpenCode. The optional console admin password is read with getpass (no echo,
never argv/logs) and applied through console_auth only after a successful
install; existing credentials are never reset.
"""
import getpass
import io
import json
import os
from pathlib import Path
import sys
from contextlib import redirect_stdout

import console_auth
import install

LANGS = ('en', 'zh-CN')

T = {
    'en': {
        'title': '=== Worker Desk setup wizard ===',
        'non_tty': ('The setup wizard needs an interactive terminal (TTY on stdin and stdout). '
                    'No changes were made.\n'
                    'For unattended installation run instead:\n'
                    '  python3 scripts/install.py --model provider/model [--no-start]\n'
                    '  python3 scripts/install.py --preset deepseek-kimi [--no-start]\n'
                    '  python3 scripts/install.py --preset ark-agent-plan [--no-start]'),
        'fresh_found': 'No existing configuration was found; this will be a fresh install.',
        'existing_found': 'An existing installation was found:',
        'config_unreadable': 'The existing configuration at {0} is not valid JSON; refusing to '
                             'continue. Fix or remove it first. No changes were made.',
        'existing_choice_title': 'Choose how to proceed:',
        'opt_keep': 'Keep the current models and configuration; upgrade the runtime only '
                    '(services restart only while idle)',
        'opt_cancel': 'Cancel (no changes)',
        'web_models_note': 'To change models later, use the web console (Models & routing) '
                           'while workers are idle; setup never replaces them silently.',
        'model_title': 'Model selection for the three default profiles:',
        'opt_preset': 'Use the deepseek-kimi preset (DeepSeek Flash + Kimi K2.8/K3, max reasoning)',
        'opt_ark_preset': 'Use the ark-agent-plan preset (Ark Auto/Evolving/K3 + native Kimi + DeepSeek)',
        'opt_custom': 'Use a custom provider/model',
        'choose_prompt': 'Enter a number',
        'choose_invalid': 'Please enter one of the listed numbers.',
        'ask_provider': 'Provider name (e.g. anthropic, deepseek)',
        'ask_model': 'Model name (e.g. claude-sonnet-4)',
        'invalid_model': 'Invalid model choice: {0}',
        'ask_opencode': 'Path to the opencode binary (leave empty to search PATH or install '
                        'the pinned release automatically)',
        'invalid_opencode': 'That path is not an executable file: {0}',
        'password_intro': 'Console login: you may set the admin password now (input hidden, '
                          'stored locally, never logged), or skip.',
        'ask_password': 'Set a console admin password now?',
        'password_prompt': 'Console admin password (empty to skip): ',
        'password_confirm': 'Repeat the password: ',
        'password_mismatch': 'Passwords do not match; try again or leave empty to skip.',
        'password_invalid': 'Password rejected: {0}',
        'password_skipped': 'Password setup skipped. A fresh install starts with the local '
                            'default admin/admin; change it right after signing in.',
        'password_existing_kept': 'Existing console credentials are kept; setup never resets them.',
        'ask_start': 'Start the services after installation?',
        'review_title': 'Review the installation plan (nothing changes until you confirm):',
        'review_install': 'Runtime installed to: {0}',
        'review_config': 'Configuration: {0}',
        'review_state': 'Private state: {0}',
        'review_keep': 'Models: keep the existing configuration (no model changes)',
        'review_preset': 'Models: {0} preset',
        'review_model': 'Models: all profiles use {0}',
        'review_opencode': 'OpenCode binary: {0}',
        'review_binary_kept': 'OpenCode binary: keep the configured one',
        'auto': 'auto (search PATH, else install the pinned release)',
        'review_start': 'Services after install: {0}',
        'start_yes': 'start now',
        'start_no': 'do not start (--no-start)',
        'review_password_set': 'Console admin password: set after installation',
        'review_credentials_kept': 'Console credentials: existing ones are kept',
        'review_confirm': 'Proceed with installation?',
        'cancelled': 'Setup cancelled; no changes were made.',
        'done': 'Installation finished.',
        'done_command': 'Command: {0}',
        'done_console': 'Console: {0}',
        'done_startup': 'Startup: {0}',
        'provider_note': 'Provider login is managed by OpenCode, not by this wizard: configure '
                         'your provider (for example `opencode auth login` or your OpenCode '
                         'config) before submitting tasks. Never paste API keys into prompts '
                         'or task text.',
        'password_applied': 'Console admin password updated; existing sessions were cleared.',
    },
    'zh-CN': {
        'title': '=== Worker Desk 安装向导 ===',
        'non_tty': ('安装向导需要交互式终端（stdin 和 stdout 均为 TTY）。未做任何更改。\n'
                    '无人值守安装请改用：\n'
                    '  python3 scripts/install.py --model 提供商/模型 [--no-start]\n'
                    '  python3 scripts/install.py --preset deepseek-kimi [--no-start]\n'
                    '  python3 scripts/install.py --preset ark-agent-plan [--no-start]'),
        'fresh_found': '未找到现有配置，将进行全新安装。',
        'existing_found': '发现现有安装：',
        'config_unreadable': '现有配置 {0} 不是有效的 JSON，拒绝继续。请先修复或删除该文件。'
                             '未做任何更改。',
        'existing_choice_title': '请选择操作方式：',
        'opt_keep': '保留当前模型和配置，仅升级运行时（仅在空闲时重启服务）',
        'opt_cancel': '取消（不做任何更改）',
        'web_models_note': '如需更换模型，请稍后使用网页控制台（模型与调度）并在空闲时应用；'
                           '安装向导绝不会静默替换模型。',
        'model_title': '为三个默认配置选择模型：',
        'opt_preset': '使用 deepseek-kimi 预设（DeepSeek Flash + Kimi K2.8/K3，max 推理）',
        'opt_ark_preset': '使用 ark-agent-plan 预设（方舟 Auto/Evolving/K3 + 原厂 Kimi + DeepSeek）',
        'opt_custom': '使用自定义 provider/model',
        'choose_prompt': '请输入数字',
        'choose_invalid': '请输入列出的数字之一。',
        'ask_provider': '提供商名称（如 anthropic、deepseek）',
        'ask_model': '模型名称（如 claude-sonnet-4）',
        'invalid_model': '模型选择无效：{0}',
        'ask_opencode': 'opencode 二进制文件路径（留空则在 PATH 中查找，或自动安装固定版本）',
        'invalid_opencode': '该路径不是可执行文件：{0}',
        'password_intro': '控制台登录：现在可以设置管理员密码（输入不回显、仅保存在本地、绝不记录），也可以跳过。',
        'ask_password': '现在设置控制台管理员密码？',
        'password_prompt': '控制台管理员密码（留空跳过）：',
        'password_confirm': '请再次输入密码：',
        'password_mismatch': '两次输入的密码不一致；请重试，或留空跳过。',
        'password_invalid': '密码被拒绝：{0}',
        'password_skipped': '已跳过密码设置。全新安装的初始本地登录为 admin/admin；登录后请立即修改。',
        'password_existing_kept': '保留现有控制台凭据；安装与升级绝不会重置它们。',
        'ask_start': '安装完成后启动服务？',
        'review_title': '请确认安装计划（确认之前不会做任何更改）：',
        'review_install': '运行时安装到：{0}',
        'review_config': '配置文件：{0}',
        'review_state': '私有状态目录：{0}',
        'review_keep': '模型：保留现有配置（不更改模型）',
        'review_preset': '模型：{0} 预设',
        'review_model': '模型：所有配置使用 {0}',
        'review_opencode': 'OpenCode 二进制：{0}',
        'review_binary_kept': 'OpenCode 二进制：保留已配置的二进制',
        'auto': '自动（查找 PATH，否则安装固定版本）',
        'review_start': '安装后服务：{0}',
        'start_yes': '立即启动',
        'start_no': '不启动（--no-start）',
        'review_password_set': '控制台管理员密码：安装完成后设置',
        'review_credentials_kept': '控制台凭据：保留现有设置',
        'review_confirm': '确认执行安装？',
        'cancelled': '已取消安装；未做任何更改。',
        'done': '安装完成。',
        'done_command': '命令：{0}',
        'done_console': '控制台：{0}',
        'done_startup': '启动状态：{0}',
        'provider_note': '提供商登录由 OpenCode 管理，与本向导无关：请在提交任务前配置提供商'
                         '（例如 `opencode auth login` 或编辑 OpenCode 配置）。'
                         '请勿在提示词或任务正文中粘贴 API 密钥。',
        'password_applied': '控制台管理员密码已更新；已有会话已清除。',
    },
}


class Cancelled(Exception):
    """The user cancelled or closed stdin; nothing has been modified."""


def _is_tty(stream):
    try:
        return bool(stream.isatty())
    except Exception:
        return False


class Prompt:
    """Line-oriented prompter over injected streams (testable, no input())."""

    def __init__(self, text, stdin, stdout):
        self.t = text
        self.stdin = stdin
        self.stdout = stdout

    def line(self, message=''):
        self.stdout.write(message + '\n')

    def ask(self, label, default=None):
        if default is None:
            self.stdout.write(label + ': ')
        else:
            self.stdout.write('{0} [{1}]: '.format(label, default))
        self.stdout.flush()
        raw = self.stdin.readline()
        if raw == '':
            raise Cancelled(self.t['cancelled'])
        value = raw.strip()
        if not value and default is not None:
            return default
        return value

    def ask_yes_no(self, label, default=True):
        hint = '[Y/n]' if default else '[y/N]'
        while True:
            value = self.ask('{0} {1}'.format(label, hint),
                             default='y' if default else 'n').lower()
            if value in ('y', 'yes'):
                return True
            if value in ('n', 'no'):
                return False
            self.line(self.t['choose_invalid'])

    def choose(self, title, options):
        """options: list of (key, label); returns the chosen key."""
        self.line(title)
        for index, (_, label) in enumerate(options, 1):
            self.line('  {0}) {1}'.format(index, label))
        valid = {str(i): key for i, (key, _) in enumerate(options, 1)}
        while True:
            value = self.ask(self.t['choose_prompt'])
            if value in valid:
                return valid[value]
            self.line(self.t['choose_invalid'])

    def hidden(self, label):
        # getpass never echoes and never appears in argv or logs.
        return getpass.getpass(label, stream=self.stdout)


def _model_error(spec):
    """Reuse the installer's model validation without its SystemExit failure."""
    try:
        install.validate_model(spec)
        return None
    except SystemExit as error:
        return str(error)


def _valid_binary(path):
    candidate = Path(path).expanduser()
    return candidate.is_file() and os.access(str(candidate), os.X_OK)


def _choose_models(prompt, plan):
    choice = prompt.choose(prompt.t['model_title'], [
        ('preset', prompt.t['opt_preset']),
        ('custom', prompt.t['opt_custom']),
        ('ark_preset', prompt.t['opt_ark_preset']),
        ('cancel', prompt.t['opt_cancel']),
    ])
    if choice == 'cancel':
        raise Cancelled(prompt.t['cancelled'])
    if choice == 'preset':
        plan['preset'] = 'deepseek-kimi'
        return
    if choice == 'ark_preset':
        plan['preset'] = 'ark-agent-plan'
        return
    while True:
        provider = prompt.ask(prompt.t['ask_provider'])
        model = prompt.ask(prompt.t['ask_model'])
        spec = provider + '/' + model
        error = _model_error(spec)
        if error is None:
            plan['model'] = spec
            return
        prompt.line(prompt.t['invalid_model'].format(error))


def _interview(prompt, no_start):
    t = prompt.t
    plan = {'mode': None, 'model': None, 'preset': None, 'opencode': None,
            'password': None, 'password_reminder': False,
            'no_start': bool(no_start), 'existing': False}
    if install.CONFIG.exists():
        # Existing installation: profiles, credentials, config and tasks are
        # preserved unless the user explicitly chooses to replace the models.
        plan['existing'] = True
        prompt.line(t['existing_found'])
        try:
            profiles = json.loads(install.CONFIG.read_text()).get('profiles') or {}
        except ValueError:
            raise SystemExit(t['config_unreadable'].format(install.CONFIG))
        for name in sorted(profiles):
            prompt.line('  - {0}: {1}'.format(name, profiles[name].get('model', '?')))
        choice = prompt.choose(t['existing_choice_title'], [
            ('keep', t['opt_keep']),
            ('cancel', t['opt_cancel']),
        ])
        if choice == 'cancel':
            raise Cancelled(t['cancelled'])
        # Keep-current upgrade only: profiles, credentials, config and tasks
        # stay untouched. Model changes belong to the web console.
        plan['mode'] = 'keep'
        prompt.line(t['web_models_note'])
        prompt.line(t['password_existing_kept'])
    else:
        plan['mode'] = 'fresh'
        prompt.line(t['fresh_found'])
        _choose_models(prompt, plan)
        while True:
            raw = prompt.ask(t['ask_opencode'], default='')
            if not raw:
                break
            if _valid_binary(raw):
                plan['opencode'] = str(Path(raw).expanduser())
                break
            prompt.line(t['invalid_opencode'].format(raw))
        prompt.line(t['password_intro'])
        if prompt.ask_yes_no(t['ask_password'], default=False):
            while True:
                first = prompt.hidden(t['password_prompt'])
                if not first:
                    plan['password_reminder'] = True
                    break
                try:
                    console_auth._validate_password(first)
                except ValueError as error:
                    prompt.line(t['password_invalid'].format(error))
                    continue
                if first != prompt.hidden(t['password_confirm']):
                    prompt.line(t['password_mismatch'])
                    continue
                plan['password'] = first
                break
        else:
            plan['password_reminder'] = True
    if not plan['no_start']:
        plan['no_start'] = not prompt.ask_yes_no(t['ask_start'], default=True)
    _review(prompt, plan)
    return plan


def _review(prompt, plan):
    t = prompt.t
    prompt.line('')
    prompt.line(t['review_title'])
    prompt.line('  ' + t['review_install'].format(install.INSTALL))
    prompt.line('  ' + t['review_config'].format(install.CONFIG))
    prompt.line('  ' + t['review_state'].format(install.STATE))
    if plan['mode'] == 'keep':
        prompt.line('  ' + t['review_keep'])
        prompt.line('  ' + t['review_binary_kept'])
        prompt.line('  ' + t['review_credentials_kept'])
    else:
        # Fresh install plan.
        if plan['preset']:
            prompt.line('  ' + t['review_preset'].format(plan['preset']))
        else:
            prompt.line('  ' + t['review_model'].format(plan['model']))
        prompt.line('  ' + t['review_opencode'].format(plan['opencode'] or t['auto']))
        if plan['password']:
            prompt.line('  ' + t['review_password_set'])
    prompt.line('  ' + t['review_start'].format(t['start_no'] if plan['no_start'] else t['start_yes']))
    if not prompt.ask_yes_no(t['review_confirm'], default=True):
        raise Cancelled(t['cancelled'])


def _execute(plan, prompt):
    t = prompt.t
    argv = []
    if plan['preset']:
        argv += ['--preset', plan['preset']]
    if plan['model']:
        argv += ['--model', plan['model']]
    if plan['opencode']:
        argv += ['--opencode', plan['opencode']]
    if plan['no_start']:
        argv.append('--no-start')
    # All prompting is done; only now may the installer touch disk or network.
    captured = io.StringIO()
    with redirect_stdout(captured):
        install.main(list(argv))
    try:
        summary = json.loads(captured.getvalue())
    except ValueError:
        summary = None
    if plan['password']:
        console_auth.set_user(console_auth.DEFAULT_USERNAME, plan['password'], install.STATE)
    prompt.line(t['done'])
    if summary:
        prompt.line(t['done_command'].format(summary.get('command')))
        if summary.get('console_url'):
            prompt.line(t['done_console'].format(summary['console_url'] + '/console'))
        prompt.line(t['done_startup'].format(summary.get('startup')))
    prompt.line(t['provider_note'])
    if plan['password']:
        prompt.line(t['password_applied'])
    elif plan['password_reminder']:
        # Shown once, after a successful install, on every password-skip path.
        prompt.line(t['password_skipped'])
    prompt.stdout.write(captured.getvalue())


def run(lang, no_start=False, stdin=None, stdout=None):
    """Run the wizard; raises Cancelled (or SystemExit) before any mutation."""
    if lang not in LANGS:
        raise SystemExit('Unsupported wizard language {0!r}; choose en or zh-CN'.format(lang))
    text = T[lang]
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    if not (_is_tty(stdin) and _is_tty(stdout)):
        raise SystemExit(text['non_tty'])
    prompt = Prompt(text, stdin, stdout)
    prompt.line(text['title'])
    try:
        plan = _interview(prompt, no_start)
    except KeyboardInterrupt:
        raise Cancelled(text['cancelled'])
    _execute(plan, prompt)

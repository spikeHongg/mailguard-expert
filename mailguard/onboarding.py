"""Stdlib-only installation choices and private workspace layout."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


class ChoiceRequired(Exception):
    def __init__(self, question, message, options):
        self.payload = {'status': 'needs_input', 'question': question,
                        'message': message, 'options': options}


def choose(value, question, message, options, *, interactive, input_fn=input):
    if value is not None:
        return value
    if not interactive:
        raise ChoiceRequired(question, message, options)
    print(message)
    for index, option in enumerate(options, 1):
        print(f'{index}. {option}')
    while True:
        answer = input_fn('选择编号或选项：').strip()
        if answer in options:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer)-1]


def choose_installation(args, *, interactive, input_fn=input):
    args.mode = choose(args.mode, 'mode', '选择安装方式：邮件专家Skill（推荐）或本地独立服务。',
                       ['skill', 'service'], interactive=interactive, input_fn=input_fn)
    workspace = Path(args.workspace).expanduser().resolve()
    existing = workspace_layout(workspace, args.env_file, args.state_dir)['env_file']
    options = ['reuse', 'edit', 'later'] if existing.is_file() else ['now', 'later']
    args.imap = choose(args.imap, 'imap', 'IMAP初始配置：现在填写，或稍后使用模拟邮件/本地EML；已有配置可沿用或修改。',
                       options, interactive=interactive, input_fn=input_fn)
    if args.imap == 'reuse' and not existing.is_file():
        raise RuntimeError('未找到可沿用的.env，请选择now或指定--env-file。')
    if args.mode == 'service':
        args.provider = choose(args.provider, 'provider', '独立服务使用哪个模型？Key稍后在本机.env填写。',
                               ['deepseek', 'glm', 'later'], interactive=interactive, input_fn=input_fn)
        if args.provider == 'glm':
            args.glm_channel = choose(args.glm_channel, 'glm_channel', '选择GLM Key的接口通道。',
                                      ['coding', 'standard'], interactive=interactive, input_fn=input_fn)
    return args


def private_directory(path):
    path = Path(path)
    if not path.is_dir():
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            if not path.is_dir():
                raise
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
    return path


def private_json(path, value):
    path = Path(path)
    private_directory(path.parent)
    # Files are local configuration, never mail or externally controlled paths.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    path.chmod(0o600)


def workspace_layout(workspace, env_file=None, state_dir=None):
    workspace = Path(workspace).expanduser().resolve()
    private = workspace/'.mailguard'
    if any(part in ('.codebuddy', '.agents', '.workbuddy') for part in workspace.parts):
        raise RuntimeError('请把--workspace指向用户工作目录，运行数据应位于Skill安装目录之外。')
    marker = private/'installation.json'
    saved = json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else {}
    return {'workspace': workspace, 'private': private, 'marker': marker,
            'env_file': Path(env_file or saved.get('env_file') or private/'.env').expanduser().resolve(),
            'state': Path(state_dir or saved.get('state_directory') or private/'state').expanduser().resolve(),
            'saved': saved}


def prepare_configuration(layout, *, imap, mode, provider=None, glm_channel=None):
    private_directory(layout['private'])
    ignored = layout['private']/'.gitignore'
    ignored.write_text('*\n', encoding='utf-8')
    env_file = layout['env_file']
    private_directory(env_file.parent) if env_file.parent == layout['private'] else None
    if not env_file.parent.is_dir():
        raise RuntimeError('指定.env的父目录不存在，请选择本机已有目录。')
    if not env_file.exists():
        fd = os.open(env_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write('# MailGuard local configuration. Fill credentials here, not in chat.\n')
    if env_file.is_symlink():
        raise RuntimeError('配置文件是符号链接，请显式选择实际文件。')
    env_file.chmod(0o600)
    existing = env_file.read_text(encoding='utf-8')
    keys = set(re.findall(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=', existing, re.M))
    defaults = {}
    if imap in ('now', 'edit'):
        defaults.update(IMAP_HOST='', IMAP_PORT='993', IMAP_USERNAME='', IMAP_PASSWORD='', IMAP_FOLDER='')
    if mode == 'service' and provider in ('deepseek', 'glm'):
        defaults.update(EVAL_PROGRESS_TIMEOUT_SECONDS='300')
        if provider == 'deepseek':
            defaults.update(DEEPSEEK_API_KEY='', DEEPSEEK_MODEL='deepseek-flash', DEEPSEEK_CONCURRENCY='20')
        else:
            url = 'https://open.bigmodel.cn/api/coding/paas/v4' if glm_channel == 'coding' else 'https://open.bigmodel.cn/api/paas/v4/'
            defaults.update(GLM_API_KEY='', GLM_BASE_URL=url, GLM_MODEL='glm-5.3-flash', GLM_CONCURRENCY='5',
                            GLM_THINKING='enabled', GLM_REASONING_EFFORT='low', GLM_MAX_TOKENS='4500')
    additions = [f'{key}={value}' for key, value in defaults.items() if key not in keys]
    if additions:
        with env_file.open('a', encoding='utf-8') as stream:
            stream.write('\n# IMAP_USERNAME: full mailbox; IMAP_PASSWORD: app password. Confirm folder (e.g. INBOX).\n')
            stream.write('\n'.join(additions)+'\n')
    private_directory(layout['state'])
    return ['IMAP_HOST', 'IMAP_PORT', 'IMAP_USERNAME', 'IMAP_PASSWORD', 'IMAP_FOLDER'] if imap in ('now', 'edit') else []


def command_text(argv):
    args = [str(x) for x in argv]
    return subprocess.list2cmdline(args) if os.name == 'nt' else shlex.join(args)


def add_setup_arguments(parser):
    parser.add_argument('--imap', choices=('now', 'later', 'reuse', 'edit'))
    parser.add_argument('--env-file', type=Path, help='显式沿用本机.env；默认工作区.mailguard/.env')
    parser.add_argument('--state-dir', type=Path, help='显式沿用旧状态目录')
    parser.add_argument('--provider', choices=('deepseek', 'glm', 'later'))
    parser.add_argument('--glm-channel', choices=('coding', 'standard'))

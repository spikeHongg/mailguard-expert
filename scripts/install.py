#!/usr/bin/env python3
"""Clone-first WorkBuddy installer. Run with Python 3.12+ from any directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

SKILL_NAME = 'mailguard-expert'


def python_in(directory):
    return directory/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def install(args):
    root = Path(__file__).resolve().parent.parent
    if sys.version_info < (3, 12):
        raise RuntimeError('需要Python 3.12或更新版本，请先安装Python后重试。')
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else root
    target = (Path.home() if args.scope == 'user' else workspace)/'.codebuddy/skills'/SKILL_NAME
    source = root/'skills'/SKILL_NAME
    files = {p.relative_to(source): p for p in source.rglob('*')
             if p.is_file() and p.name != 'runtime.json' and '__pycache__' not in p.parts}
    marker = target/'.mailguard-install.json'
    if target.is_symlink():
        raise RuntimeError('目标技能是符号链接。请使用独立复制的Skill目录（npx skills add时添加--copy）。')
    managed = marker.is_file() and json.loads(marker.read_text()).get('owner') == 'mailguard-expert'
    for relative, original in files.items():
        current = target/relative
        if current.exists() and (not current.is_file() or current.read_bytes() != original.read_bytes()):
            if not (managed and args.update):
                raise RuntimeError(f'已有不同内容的Skill文件：{relative}；保留原文件，确认升级时使用--update。')
    runtime = Path(args.venv).expanduser().resolve() if args.venv else root/'.venv'
    if not python_in(runtime).is_file():
        if runtime.exists() and any(runtime.iterdir()):
            raise RuntimeError('虚拟环境路径已存在且不是有效venv，请选择空目录。')
        venv.EnvBuilder(with_pip=True).create(runtime)
    interpreter = python_in(runtime)
    if not args.skip_dependencies:
        spec = str(root)+('[ui]' if args.with_ui else '')
        subprocess.run([str(interpreter), '-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-e', spec],
                       check=True, cwd=root, stdout=sys.stderr)
    env_file = root/'.env'
    if not env_file.exists():
        shutil.copyfile(root/'.env.example', env_file)
    env_file.chmod(0o600)
    state = root/'data/private/expert'
    doctor = subprocess.run([str(interpreter), '-m', 'mailguard.expert_cli', '--state-dir', str(state),
                             '--env-file', str(env_file), 'doctor'], check=True, cwd=root,
                            text=True, encoding='utf-8', capture_output=True)
    check = json.loads(doctor.stdout)
    if check.get('status') != 'ok' or check.get('skills') != 9:
        raise RuntimeError('离线自检失败，未标记安装完成。')
    target.mkdir(parents=True, exist_ok=True)
    for relative, original in files.items():
        path = target/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, path)
    locator = {'python': str(interpreter), 'source': str(root), 'state_dir': str(state), 'env_file': str(env_file)}
    (target/'runtime.json').write_text(json.dumps(locator, ensure_ascii=False, indent=2), encoding='utf-8')
    (target/'runtime.json').chmod(0o600)
    marker.write_text(json.dumps({'owner': 'mailguard-expert', 'version': '0.1.0',
        'files': {str(p): hashlib.sha256(s.read_bytes()).hexdigest() for p, s in files.items()}}, indent=2), encoding='utf-8')
    return {'status': 'ok', 'target': 'workbuddy', 'skill_directory': str(target),
            'python': str(interpreter), 'env_file': str(env_file), 'state_directory': str(state),
            'selftest': check, 'next': '在WorkBuddy打开此工作区或重新开启会话，调用mailguard-expert分析内置模拟邮件。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='安装MailGuard专家到WorkBuddy兼容的.codebuddy/skills')
    parser.add_argument('--workspace', help='项目Skill安装位置；默认仓库根目录')
    parser.add_argument('--scope', choices=('project', 'user'), default='project')
    parser.add_argument('--venv', help='Python虚拟环境目录；默认仓库.venv')
    parser.add_argument('--with-ui', action='store_true', help='同时安装Streamlit依赖')
    parser.add_argument('--update', action='store_true', help='升级本安装器管理的Skill文件，保留.env和数据')
    parser.add_argument('--skip-dependencies', action='store_true', help='已安装依赖时只安装Skill并自检')
    args = parser.parse_args(argv)
    try:
        value = install(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc) if isinstance(exc, RuntimeError)
                          else '安装步骤失败；请检查Python、网络、目录权限和依赖安装输出。'}, ensure_ascii=False))
        return 1
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())

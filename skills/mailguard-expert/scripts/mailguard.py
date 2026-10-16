#!/usr/bin/env python3
"""Portable Skill launcher; runtime-specific paths stay in ignored runtime.json."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    skill = Path(__file__).resolve().parent.parent
    path = skill/'runtime.json'
    if not path.is_file():
        # Agents may explicitly read the repository's canonical Skill after install.
        root = skill.parent.parent
        installed = root/'.codebuddy/skills/mailguard-expert/runtime.json'
        if (root/'scripts/install.py').is_file() and (root/'pyproject.toml').is_file() and installed.is_file():
            path = installed
    if not path.is_file():
        print(json.dumps({'status': 'error', 'error_code': 'runtime_missing',
                          'error': '请按references/operations.md从仓库执行安装器；仅复制Skill尚未安装Python后端。'}, ensure_ascii=False))
        return 1
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
        if not Path(config['python']).is_file() or not Path(config['source']).is_dir():
            raise ValueError()
        command = [config['python'], '-m', 'mailguard.expert_cli', '--state-dir', config['state_dir'],
                   '--env-file', config['env_file'], *sys.argv[1:]]
        return subprocess.run(command, cwd=config['source']).returncode
    except (OSError, KeyError, ValueError):
        print(json.dumps({'status': 'error', 'error_code': 'runtime_invalid',
                          'error': '本机运行环境定位失效，请保留.env和私有数据并重新运行仓库安装器。'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())

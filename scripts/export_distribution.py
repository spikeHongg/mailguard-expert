#!/usr/bin/env python3
"""Export a clean source ZIP from an allowlist, without Git history or local state."""
import argparse
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = ('README.md', 'AGENTS.md', 'INSTALL.md', 'PUBLISH.md', 'CONTEXT.md', 'GLOSSARY.md',
              '.gitignore', '.gitattributes', '.streamlit/config.toml', '.env.example', 'pyproject.toml', 'setup.py', 'MANIFEST.in',
              'requirements.txt', 'requirements.lock.txt', 'app.py', '启动邮件研判.command')
PUBLIC_SCRIPTS = ('audit_public_overlap.py', 'build_testbench.py', 'expand_testbench.py',
                  'prepare_form_controls.py', 'prepare_html_corpus.py', 'prepare_html_holdout.py',
                  'restore_public_corpus.py', 'install.py', 'export_distribution.py')


def distribution_files(root=ROOT):
    root = Path(root)
    files = {root/p for p in ROOT_FILES}
    files.add(root/'docs/local-api.md')
    files.add(root/'policies/expert-guidance.txt')
    files.update((root/'mailguard').glob('*.py'))
    files.update((root/'tests').glob('*.py'))
    files.add(root/'tests/fixtures/contract-request-hashes.json')
    files.update(root/'scripts'/name for name in PUBLIC_SCRIPTS)
    for skill in (root/'skills').iterdir():
        if skill.is_dir():
            files.add(skill/'SKILL.md')
    for folder, pattern in [('scripts', '*.py'), ('references', '*.md')]:
        files.update((root/'skills/mailguard-expert'/folder).glob(pattern))
    files.add(root/'skills/mailguard-expert/.gitignore')
    # Entire corpus is synthetic and owned by this project. No replay responses ship.
    files.update(root/'data/testbench'/f'm{i:03}.eml' for i in range(1, 101))
    files.update(root/'data/testbench'/name for name in
                 ('context.json', 'context-v2.json', 'labels.json', 'manifest.json', 'metadata-v2.json', 'versions/v1.json'))
    for path in files:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('发布文件缺失、是符号链接或越界：'+str(path.relative_to(root)))
    return sorted(files)


def export(destination, root=ROOT):
    root, destination = Path(root), Path(destination)
    if destination.exists():
        raise ValueError('发布包已存在，请使用新文件名，保留旧包。')
    files = distribution_files(root)
    # Match only this machine's actual secrets; never print their values.
    secrets = []
    env = root/'.env'
    if env.is_file():
        for line in env.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                if re.search(r'(?:_API_KEY|_PASSWORD|_USERNAME|_TOKEN|_SECRET)$', key.strip()):
                    value = value.strip().strip('"\'')
                    if value:
                        secrets.append(value.encode())
    personal = [str(Path.home()).encode(), Path.home().name.encode()]
    for path in files:
        content = path.read_bytes()
        if any(value in content for value in secrets+personal):
            raise ValueError('文件包含本机敏感值或个人路径，发布已停止：'+str(path.relative_to(root)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, 'mailguard/'+str(path.relative_to(root)))
    return len(files)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='生成不含内部研发记录和Git历史的源码发布包')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        count = export(args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc)+'\n')
    print(f'已生成发布包：{args.output}（{count}个文件；无Git历史、内部记录或本机状态）')

#!/usr/bin/env python3
"""Build/check the Skill's explicit, readable runtime from canonical source."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
CORE = ('__init__', 'models', 'config', 'parser', 'html_static', 'contracts', 'evidence',
        'skills', 'expert', 'expert_cli', 'progress', 'imap_source', 'bootstrap', 'onboarding', 'runtime_support', 'failures')
POLICIES = ('mail-triage-core', 'mail-identity-access', 'mail-payment-bec', 'mail-link-attachment',
            'mail-data-exfil', 'mail-remote-extortion', 'mail-prompt-injection', 'mail-false-positive-review', 'mail-html-forms')
SAMPLES = ('m002', 'm026', 'm031')
STATIC = ('SKILL.md', '.gitignore', 'references/operations.md', 'references/installation.md')


def mapping(root=ROOT):
    root = Path(root)
    files = {f'scripts/_runtime/mailguard/{name}.py': root/f'mailguard/{name}.py' for name in CORE}
    files['scripts/mailguard.py'] = root/'scripts/skill_entry.py'
    files['scripts/_runtime/requirements-core.txt'] = root/'requirements-core.txt'
    files['scripts/_runtime/mailguard/_assets/guidance.txt'] = root/'policies/expert-guidance.txt'
    for name in POLICIES:
        files[f'scripts/_runtime/mailguard/_assets/skills/{name}/SKILL.md'] = root/f'policies/detection/{name}/SKILL.md'
    for name in SAMPLES:
        files[f'scripts/_runtime/mailguard/_assets/samples/{name}.eml'] = root/f'data/testbench/{name}.eml'
    return files


def build(root=ROOT, check=False):
    root = Path(root); skill = root/'skills/mailguard-expert'
    sources = mapping(root)
    if not check:
        for name, source in sources.items():
            target = skill/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    expected = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in sources.items()}
    expected.update({name: hashlib.sha256((skill/name).read_bytes()).hexdigest() for name in STATIC})
    manifest = {'format': 'mailguard-skill-bundle-v1', 'version': '1.0.0', 'files': dict(sorted(expected.items()))}
    path = skill/'bundle-manifest.json'
    if not check:
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    if json.loads(path.read_text()) != manifest:
        raise ValueError('Skill清单与源码不一致，请重新构建。')
    for name, sha in expected.items():
        target = skill/name
        if target.is_symlink() or not target.resolve().is_relative_to(skill.resolve()):
            raise ValueError('Skill分发路径无效。')
        if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
            raise ValueError('Skill与源码不同：'+name)
    actual = {str(p.relative_to(skill)) for p in (skill/'scripts/_runtime').rglob('*')
              if p.is_file() and '__pycache__' not in p.parts}
    if actual != {name for name in expected if name.startswith('scripts/_runtime/')}:
        raise ValueError('Skill运行包含清单之外的文件。')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    result = build(check=args.check)
    print(json.dumps({'status': 'ok', 'check_only': args.check, 'files': len(result['files'])}))

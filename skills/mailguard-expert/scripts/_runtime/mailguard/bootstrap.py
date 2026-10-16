"""Provision either distribution, then delegate to the shared expert CLI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import __version__
from .onboarding import (ChoiceRequired, add_setup_arguments, choose_installation, command_text,
                         prepare_configuration, private_directory, private_json, workspace_layout)
from .runtime_support import core_command, ensure_runtime, probe, python_in, run_checked
from .failures import failure

SKILL_NAME = 'mailguard-expert'
HOST_COMMANDS = {'doctor', 'samples', 'prepare', 'submit', 'get', 'review', 'check-imap', 'fetch-imap'}


def bundle_inventory(skill):
    skill = Path(skill).resolve()
    manifest = json.loads((skill/'bundle-manifest.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        path = skill/name
        if path.is_symlink() or not path.resolve().is_relative_to(skill) or not path.is_file():
            raise RuntimeError('Skill分发文件缺失或越界，请重新安装。')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Skill分发文件校验失败，请重新安装完整版本。')
    actual = {str(p.relative_to(skill)) for p in (skill/'scripts/_runtime').rglob('*')
              if p.is_file() and '__pycache__' not in p.parts}
    if actual != {name for name in manifest['files'] if name.startswith('scripts/_runtime/')}:
        raise RuntimeError('Skill运行目录包含清单之外的文件，请重新安装完整版本。')
    return manifest


def copy_skill(source, target, update=False):
    source, target = Path(source).resolve(), Path(target)
    manifest = bundle_inventory(source)
    if target.resolve() == source:
        return source
    if target.is_symlink():
        raise RuntimeError('请使用npx的--copy模式安装Skill，或选择独立目录。')
    managed = (target/'bundle-manifest.json').is_file() or (target/'.mailguard-install.json').is_file()
    names = {*manifest['files'], 'bundle-manifest.json'}
    for name in names:
        old = target/name
        if any(p.is_symlink() for p in (old, *old.parents) if p.is_relative_to(target)):
            raise RuntimeError('目标Skill包含符号链接，请选择独立目录。')
        if old.exists() and (not old.is_file() or old.read_bytes() != (source/name).read_bytes()):
            if not (managed and update):
                raise RuntimeError('目标Skill已有不同内容，请检查后使用--update。')
    for name in sorted(names):
        path = target/name
        if not path.parent.is_dir():
            path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/name, path)
    return target.resolve()


def install_runtime(args, *, source, requirements, skill=None):
    layout = workspace_layout(args.workspace, args.env_file, args.state_dir)
    prepare_configuration(layout, imap=args.imap, mode=args.mode,
                          provider=args.provider, glm_channel=args.glm_channel)
    runtime = Path(args.venv).expanduser().resolve() if getattr(args, 'venv', None) else layout['private']/f'runtime/{args.mode}'
    if args.mode == 'service':
        extras = 'service,ui' if getattr(args, 'with_ui', False) else 'service'
        install_spec = str(Path(source).resolve())+f'[{extras}]'
        imports = ('openai', 'fastapi', 'uvicorn')
    else:
        install_spec, imports = None, ()
    if getattr(args, 'skip_dependencies', False):
        interpreter = python_in(runtime)
        ready = probe(interpreter, 'import imap_tools,pydantic,dotenv'+(',openai,fastapi,uvicorn' if imports else ''))
        if ready is None or ready.returncode:
            raise RuntimeError('已选跳过依赖，但该运行环境缺少必需组件。')
        repaired = False
    else:
        interpreter, repaired = ensure_runtime(runtime, requirements, install_spec=install_spec, extra_imports=imports)
    command = core_command(interpreter, source, layout['env_file'], layout['state'], [])
    doctor = json.loads(run_checked([*command, 'doctor'], '离线自检'))
    if doctor.get('status') != 'ok' or doctor.get('skills') != 9:
        raise RuntimeError('离线自检未通过，安装未完成。')
    saved = layout['saved']
    modes = saved.get('modes', {})
    modes[args.mode] = {'python': str(interpreter), 'source': str(Path(source).resolve()),
                        'version': __version__}
    record = {**saved, 'version': __version__, 'modes': modes, 'imap_choice': args.imap,
              'env_file': str(layout['env_file']), 'state_directory': str(layout['state'])}
    if skill is not None:
        record['skill_manifest_hash'] = hashlib.sha256((Path(skill)/'bundle-manifest.json').read_bytes()).hexdigest()
    private_json(layout['marker'], record)
    commands = {'doctor': command_text([*command, 'doctor']),
                'check_imap': command_text([*command, 'check-imap'])}
    if args.mode == 'service':
        commands.update(start=command_text([*command, 'serve', '--port', '8765']),
                        status=command_text([*command, 'service-status', '--port', '8765']),
                        stop='在启动服务的终端按Ctrl+C（前台运行）。')
    return {'status': 'ok', 'installation_status': 'installed', 'mode': args.mode,
            'workspace': str(layout['workspace']), 'env_file': str(layout['env_file']),
            'state_directory': str(layout['state']), 'python': str(interpreter),
            'local_library_repaired': repaired, 'selftest': doctor,
            'imap': {**doctor['imap_configuration'], 'choice': args.imap},
            'model_api': {'required': args.mode == 'service', 'selected': args.provider,
                          'configured': doctor['providers_configured'], 'verified': False},
            'commands': commands,
            'next': '配置值仅在本机.env填写；确认填写后check-imap只验证连接；用三封模拟邮件完成宿主新研判验收。'}


def skill_main(skill, argv=None):
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description='MailGuard自包含Skill。始终指定用户工作区。')
    parser.add_argument('--workspace', type=Path)
    known, rest = parser.parse_known_args(argv)
    if not known.workspace:
        print(json.dumps({'status': 'needs_input', 'question': 'workspace',
                          'message': '请用--workspace指定用户工作目录，Skill目录仅用于分发。'}, ensure_ascii=False))
        return 2
    try:
        skill = Path(skill).resolve()
        bundle_inventory(skill)
        source = skill/'scripts/_runtime'
        if rest and rest[0] == 'setup':
            setup = argparse.ArgumentParser(description='首次安装先确认IMAP现在配置、稍后配置或沿用。')
            add_setup_arguments(setup)
            args = setup.parse_args(rest[1:])
            args.workspace, args.mode = known.workspace, 'skill'
            args = choose_installation(args, interactive=sys.stdin.isatty())
            result = install_runtime(args, source=source, requirements=source/'requirements-core.txt', skill=skill)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        layout = workspace_layout(known.workspace)
        saved = layout['saved']
        if 'skill' not in saved.get('modes', {}):
            raise ChoiceRequired('imap', '首次使用先询问IMAP配置，再执行setup --imap now|later|reuse|edit。', ['now', 'later'])
        actual_hash = hashlib.sha256((skill/'bundle-manifest.json').read_bytes()).hexdigest()
        if saved.get('skill_manifest_hash') != actual_hash:
            raise RuntimeError('Skill已更新，请按SOP重新setup以复用配置和数据。')
        if not rest or rest[0] not in HOST_COMMANDS:
            raise RuntimeError('Skill入口支持doctor/samples/prepare/submit/get/review/check-imap/fetch-imap；独立服务请按安装SOP选择service。')
        interpreter = saved['modes']['skill']['python']
        command = core_command(interpreter, source, layout['env_file'], layout['state'], rest)
        return subprocess.run(command).returncode
    except ChoiceRequired as exc:
        print(json.dumps(exc.payload, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        print(json.dumps(failure('operation_cancelled', '用户已中止当前步骤。'), ensure_ascii=False))
        return 130
    except PermissionError:
        print(json.dumps(failure('file_access_denied', '宿主或目录权限拒绝当前步骤。'), ensure_ascii=False))
        return 1
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else '本机安装文件无法读取，请检查工作区和Skill分发文件。'
        print(json.dumps(failure(getattr(exc, 'code', 'installation_error'), message), ensure_ascii=False))
        return 1

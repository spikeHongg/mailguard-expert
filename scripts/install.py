#!/usr/bin/env python3
"""Select installation mode and IMAP intent before changing the workspace."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from mailguard.bootstrap import copy_skill, install_runtime
from mailguard.onboarding import ChoiceRequired, add_setup_arguments, choose_installation
from mailguard.failures import failure


def main(argv=None):
    parser = argparse.ArgumentParser(description='MailGuard双模式安装；先选择模式和IMAP初始配置。')
    parser.add_argument('--mode', choices=('skill', 'service'))
    parser.add_argument('--workspace', type=Path, default=ROOT)
    parser.add_argument('--scope', choices=('project', 'user'), default='project')
    parser.add_argument('--venv', type=Path, help='显式复用已知运行环境；默认工作区.mailguard/runtime/<mode>')
    parser.add_argument('--with-ui', action='store_true', help='service模式另装可选网页依赖')
    parser.add_argument('--update', action='store_true', help='更新已由MailGuard管理的Skill文件')
    parser.add_argument('--skip-dependencies', action='store_true')
    add_setup_arguments(parser)
    args = parser.parse_args(argv)
    try:
        args = choose_installation(args, interactive=sys.stdin.isatty())
        if args.with_ui and args.mode != 'service':
            raise RuntimeError('--with-ui用于service模式，Skill使用宿主界面。')
        if args.mode == 'skill':
            base = Path.home() if args.scope == 'user' else args.workspace.expanduser().resolve()
            skill = copy_skill(ROOT/'skills/mailguard-expert', base/'.codebuddy/skills/mailguard-expert', args.update)
            result = install_runtime(args, source=skill/'scripts/_runtime',
                                     requirements=skill/'scripts/_runtime/requirements-core.txt', skill=skill)
            result['skill_directory'] = str(skill)
        else:
            result = install_runtime(args, source=ROOT, requirements=ROOT/'pyproject.toml')
    except ChoiceRequired as exc:
        print(json.dumps(exc.payload, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        print(json.dumps(failure('operation_cancelled', '用户已中止安装步骤。'), ensure_ascii=False))
        return 130
    except PermissionError:
        print(json.dumps(failure('file_access_denied', '宿主或目录权限拒绝安装步骤。'), ensure_ascii=False))
        return 1
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps(failure(getattr(exc, 'code', 'installation_error'),
                                str(exc) if isinstance(exc, RuntimeError) else '安装文件读取失败，请检查工作区和完整源码。'), ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

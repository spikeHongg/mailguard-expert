"""Agent-friendly CLI. Commands emit JSON; failures have nonzero exit status."""
import argparse
import json
from pathlib import Path
import sys
import sqlite3
import os

from .models import AnalysisError
from .failures import failure


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    # Select the environment before importing the core/config module. An explicit
    # file must not silently inherit missing values from the checkout's .env.
    preflight = argparse.ArgumentParser(add_help=False)
    preflight.add_argument('--env-file', type=Path)
    early, _ = preflight.parse_known_args(argv)
    if early.env_file:
        if not early.env_file.is_file():
            emit(failure('invalid_config', '指定配置文件不存在。'))
            return 1
        os.environ['MAILGUARD_ENV_FILE'] = str(early.env_file.expanduser().resolve())
        from dotenv import load_dotenv
        load_dotenv(early.env_file, override=True)
    from .expert import Expert, PROFILES, SAMPLES, private_directory
    parser = argparse.ArgumentParser(description='MailGuard 邮件安全专家')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--env-file', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='离线安装自检；不调用模型或IMAP')
    commands.add_parser('samples', help='列出内置无害邮件')
    commands.add_parser('check-imap', help='验证TLS、认证和只读文件夹；不获取邮件正文')
    prepare = commands.add_parser('prepare', help='提取静态事实并准备宿主研判请求')
    source = prepare.add_mutually_exclusive_group(required=True)
    source.add_argument('--sample', choices=SAMPLES)
    source.add_argument('--eml', type=Path)
    prepare.add_argument('--context', type=Path, help='公开业务背景JSON；省略时为空')
    prepare.add_argument('--profile', choices=PROFILES, default='v6-c1')
    submit = commands.add_parser('submit', help='校验并保存WorkBuddy/其他宿主研判')
    submit.add_argument('--job', required=True)
    submit.add_argument('--response', type=Path, required=True)
    submit.add_argument('--request-hash', required=True)
    submit.add_argument('--host', default='workbuddy')
    submit.add_argument('--model', default='not_reported')
    analyze = commands.add_parser('analyze', help='明确调用已配置的外部模型API')
    analyze.add_argument('--job', required=True)
    analyze.add_argument('--provider', choices=('deepseek', 'glm', 'gpt'), required=True)
    get = commands.add_parser('get')
    get.add_argument('--job', required=True)
    review = commands.add_parser('review')
    review.add_argument('--job', required=True)
    review.add_argument('--verdict', choices=('risk', 'clear', 'review'), required=True)
    review.add_argument('--note', required=True)
    imap = commands.add_parser('fetch-imap', help='显式只读读取配置文件夹；不会调用模型')
    imap.add_argument('--limit', type=int, default=3)
    imap.add_argument('--context', type=Path)
    service = commands.add_parser('serve', help='在127.0.0.1启动独立HTTP服务')
    service.add_argument('--port', type=int, default=8765)
    status = commands.add_parser('service-status', help='检查本机服务健康和当前状态目录的鉴权')
    status.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == 'check-imap':
            from .imap_source import check_connection
            emit(check_connection())
            return 0
        if args.command == 'service-status':
            from .expert import state_directory
            from http.client import HTTPConnection
            if not 1 <= args.port <= 65535:
                raise AnalysisError('端口须为1至65535。', code='invalid_config')
            state = Path(args.state_dir or state_directory())
            token_file = state/'service.token'
            if not token_file.is_file():
                raise AnalysisError('本机服务尚未启动。', code='service_not_started')
            connection = HTTPConnection('127.0.0.1', args.port, timeout=5)
            try:
                # Connect directly to loopback; never forward the local token
                # through a proxy or follow a redirect to another endpoint.
                connection.request('GET', '/v1/capabilities',
                                   headers={'Authorization': 'Bearer '+token_file.read_text().strip()})
                response = connection.getresponse()
                body = response.read(65537)
                if response.status != 200 or len(body) > 65536:
                    raise ValueError()
                health = json.loads(body)
                if not isinstance(health, dict) or health.get('status') != 'ok':
                    raise ValueError()
            except Exception:
                raise AnalysisError('本机服务不可达或当前实例鉴权失败。', code='service_unreachable') from None
            finally:
                connection.close()
            emit({'status': 'ok', 'running': True, 'authenticated': True,
                  'providers_configured': health.get('providers_configured', {})})
            return 0
        expert = Expert(args.state_dir)
        context = json.loads(args.context.read_text(encoding='utf-8')) if getattr(args, 'context', None) else {}
        if not isinstance(context, dict):
            raise AnalysisError('业务背景须为JSON对象。', code='invalid_context')
        if args.command == 'doctor':
            value = expert.doctor()
        elif args.command == 'samples':
            value = {'samples': list(SAMPLES), 'source': 'synthetic'}
        elif args.command == 'prepare':
            value = expert.prepare(raw=args.eml.read_bytes() if args.eml else None, sample=args.sample,
                                   context=context, profile=args.profile)
            path = expert.state/'requests'/f'{value["id"]}.json'
            private_directory(path.parent)
            # Each fresh opaque job gets its own private request; no shared output filename.
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
                json.dump(value.pop('request'), stream, ensure_ascii=False, indent=2)
            path.chmod(0o600)
            value['request_file'] = str(path)
            value['response_file'] = str(expert.state/'requests'/f'{value["id"]}.response.json')
        elif args.command == 'submit':
            value = expert.submit(args.job, json.loads(args.response.read_text(encoding='utf-8')),
                                  host=args.host, model=args.model, request_hash=args.request_hash)
        elif args.command == 'analyze':
            value = expert.analyze(args.job, args.provider)
        elif args.command == 'get':
            value = expert.get(args.job)
        elif args.command == 'review':
            value = expert.review(args.job, args.verdict, args.note)
        elif args.command == 'fetch-imap':
            value = expert.read_imap(args.limit, context=context)
            # Save requests for native host analysis using the same private convention.
            for job in value['jobs']:
                path = expert.state/'requests'/f'{job["id"]}.json'
                private_directory(path.parent)
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
                    json.dump(job.pop('request'), stream, ensure_ascii=False, indent=2)
                path.chmod(0o600)
                job['request_file'] = str(path)
                job['response_file'] = str(path.with_suffix('.response.json'))
        elif args.command == 'serve':
            if not 1 <= args.port <= 65535:
                raise AnalysisError('端口须为1至65535。', code='invalid_config')
            try:
                from .service import serve
            except ImportError:
                raise AnalysisError('请安装service模式依赖后启动独立服务。', code='service_not_installed') from None
            serve(expert.state, args.port)
            return 0
        emit(value)
        if (value.get('result') or {}).get('error_code') == 'operation_cancelled':
            return 130
        return 1 if value.get('status') == 'error' else 0
    except KeyboardInterrupt:
        emit(failure('operation_cancelled', '用户已中止当前操作。'))
        return 130
    except PermissionError:
        emit(failure('file_access_denied', '宿主拒绝文件操作；请检查本机目录权限并保留沙箱设置。未产生有效研判。'))
        return 1
    except (OSError, sqlite3.Error):
        emit(failure('filesystem_error', '本机文件或私有状态库无法读写。请检查目录、磁盘与权限；未产生有效研判。'))
        return 1
    except (AnalysisError, ValueError) as exc:
        emit(failure(getattr(exc, 'code', 'input_error'),
                     str(exc) if isinstance(exc, AnalysisError) else '输入文件无法读取或格式无效。'))
        return 1


if __name__ == '__main__':
    sys.exit(main())

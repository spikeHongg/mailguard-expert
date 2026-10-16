"""Explicit TLS IMAP operations. Initial connection checks never fetch messages."""
from contextlib import contextmanager
import os
import ssl
from imap_tools import MailBox, AND
from .models import AnalysisError
from .parser import parse_mail

FIELDS = ('IMAP_HOST', 'IMAP_PORT', 'IMAP_USERNAME', 'IMAP_PASSWORD', 'IMAP_FOLDER')


def configuration_status():
    missing = [key for key in FIELDS if not os.getenv(key, '993' if key == 'IMAP_PORT' else '').strip()]
    if os.getenv('IMAP_HOST') == 'imap.example.com' and 'IMAP_HOST' not in missing:
        missing.insert(0, 'IMAP_HOST')
    return {'configured': not missing, 'missing': missing, 'verified': False}


def _configuration():
    status = configuration_status()
    if not status['configured']:
        raise AnalysisError('IMAP待配置，请在本机填写：'+', '.join(status['missing']), code='imap_not_configured')
    try:
        port = int(os.getenv('IMAP_PORT', '993'))
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise AnalysisError('IMAP_PORT须为1至65535的TLS端口。', code='invalid_config') from None
    return {key: os.environ.get(key, '') for key in FIELDS} | {'IMAP_PORT': port}


@contextmanager
def _readonly_session(mailbox_factory):
    config = _configuration()
    stage = 'connection'
    try:
        box = mailbox_factory(config['IMAP_HOST'], port=config['IMAP_PORT'], timeout=20,
                              ssl_context=ssl.create_default_context())
        stage = 'authentication'
        with box.login(config['IMAP_USERNAME'], config['IMAP_PASSWORD'], initial_folder=None):
            stage = 'folder'
            box.folder.set(config['IMAP_FOLDER'], readonly=True)
            stage = 'read'
            yield box
    except AnalysisError:
        raise
    except ssl.SSLError:
        raise AnalysisError('IMAP TLS验证失败，请检查服务器、TLS端口和证书。', code='imap_tls_failed') from None
    except Exception:
        codes = {'connection': ('imap_connection_failed', 'IMAP连接失败，请检查服务器、端口和网络。'),
                 'authentication': ('imap_auth_failed', 'IMAP认证失败，请在本机检查用户名和客户端专用密码。'),
                 'folder': ('imap_folder_failed', 'IMAP只读文件夹选择失败，请检查文件夹名称和访问权限。'),
                 'read': ('imap_read_failed', 'IMAP读取或关闭连接失败，请在本机检查邮箱连接。')}
        code, message = codes[stage]
        raise AnalysisError(message, code=code) from None


def check_connection(mailbox_factory=MailBox):
    with _readonly_session(mailbox_factory):
        pass
    return {'status': 'ok', 'imap_verified': True, 'readonly': True,
            'mails_fetched': 0, 'sent_to_model': False,
            'checks': {'tls': True, 'authentication': True, 'folder': True}}


def fetch_test_folder(limit=20, mailbox_factory=MailBox, inspect_html=False):
    result = fetch_test_folder_batch(limit, mailbox_factory, inspect_html)
    if result['errors']:
        raise AnalysisError(f'{len(result["errors"])}封邮件解析失败；请用分项读取结果查看可用邮件。', code='mail_parse_error')
    return result['mails']


def fetch_test_folder_batch(limit=20, mailbox_factory=MailBox, inspect_html=False):
    with _readonly_session(mailbox_factory) as box:
        mails, errors = [], []
        for message in box.fetch(AND(all=True), limit=min(max(int(limit), 1), 50), mark_seen=False, reverse=True):
            try:
                mails.append(parse_mail(message.obj.as_bytes(), f'imap-{message.uid}', inspect_html=inspect_html))
            except AnalysisError as exc:
                errors.append({'id': f'imap-{message.uid}', 'stage': 'mime_parse', 'error': str(exc)})
        return {'mails': mails, 'errors': errors}

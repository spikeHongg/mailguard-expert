import os
import ssl
from imap_tools import MailBox, AND
from .models import AnalysisError
from .parser import parse_mail

def fetch_test_folder(limit=20, mailbox_factory=MailBox, inspect_html=False):
    result=fetch_test_folder_batch(limit,mailbox_factory,inspect_html)
    if result['errors']:
        raise AnalysisError(f'{len(result["errors"])}封邮件解析失败；请用分项读取结果查看可用邮件。',code='mail_parse_error')
    return result['mails']

def fetch_test_folder_batch(limit=20, mailbox_factory=MailBox, inspect_html=False):
    username, password, folder = (os.getenv(k, '') for k in ('IMAP_USERNAME', 'IMAP_PASSWORD', 'IMAP_FOLDER'))
    if not all((username, password, folder)):
        raise AnalysisError('IMAP 待配置：请填写用户名、客户端专用密码和测试文件夹。')
    try:
        box = mailbox_factory(os.getenv('IMAP_HOST', 'imap.example.com'),
                              port=int(os.getenv('IMAP_PORT', '993')),
                              ssl_context=ssl.create_default_context())
        with box.login(username, password, initial_folder=None):
            box.folder.set(folder, readonly=True)
            mails, errors = [], []
            for m in box.fetch(AND(all=True),limit=min(max(int(limit),1),50),mark_seen=False,reverse=True):
                try:
                    mails.append(parse_mail(m.obj.as_bytes(),f'imap-{m.uid}',inspect_html=inspect_html))
                except AnalysisError as exc:
                    errors.append({'id':f'imap-{m.uid}','stage':'mime_parse','error':str(exc)})
            return {'mails':mails,'errors':errors}
    except Exception as e:
        raise AnalysisError('IMAP 读取失败，请在本机检查配置、认证、文件夹及网络。') from e

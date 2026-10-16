import hashlib
import re
from html.parser import HTMLParser
from email.header import decode_header, make_header
from imap_tools import MailMessage
from .models import Mail, InspectedMail, AnalysisError

MAX_BYTES = 5 * 1024 * 1024
MAX_HTML_DEPTH = 256
MAX_HTML_EVENTS = 100_000
MAX_LINKS = 10_000
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
             'link', 'meta', 'param', 'source', 'track', 'wbr'}

class HTMLFacts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.visible, self.hidden, self.links = [], [], []
        self.anchor = None
        self.anchor_parts = []
        self.events = 0

    def budget(self):
        self.events += 1
        if self.events > MAX_HTML_EVENTS:
            raise AnalysisError('HTML处理预算已耗尽，未完成邮件解析。', code='mail_parse_limit')

    def finish_anchor(self):
        if self.anchor is not None:
            self.anchor['text'] = ''.join(self.anchor_parts)
        self.anchor, self.anchor_parts = None, []

    def handle_starttag(self, tag, attrs):
        self.budget()
        if tag not in VOID_TAGS and len(self.stack) >= MAX_HTML_DEPTH:
            raise AnalysisError('HTML嵌套超过解析限制，未完成邮件解析。', code='mail_parse_limit')
        a = dict(attrs)
        style = (a.get('style') or '').replace(' ', '').lower()
        hidden = ((self.stack[-1][1] if self.stack else False) or tag in ('script', 'style', 'template')
                  or 'hidden' in a or 'display:none' in style or 'visibility:hidden' in style)
        if tag not in VOID_TAGS:
            self.stack.append((tag, hidden))
        if tag == 'a':
            if len(self.links) >= MAX_LINKS:
                raise AnalysisError('邮件链接数量超过解析限制。', code='mail_parse_limit')
            self.finish_anchor()
            self.anchor = {'text': '', 'url': a.get('href') or ''}
            self.links.append(self.anchor)
        if tag == 'img' and a.get('alt'):
            (self.hidden if hidden else self.visible).append(a['alt'])

    def handle_endtag(self, tag):
        self.budget()
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag == 'a':
            self.finish_anchor()

    def handle_data(self, data):
        self.budget()
        if data.strip():
            (self.hidden if self.stack and self.stack[-1][1] else self.visible).append(data.strip())
            if self.anchor is not None:
                self.anchor_parts.append(data)

def parse_mail(raw: bytes, mail_id: str = 'imported', inspect_html=False) -> Mail:
    if not raw or len(raw) > MAX_BYTES:
        raise AnalysisError('邮件为空或超过 5 MiB 限制。')
    try:
        msg = MailMessage.from_bytes(raw)
        html = HTMLFacts()
        html.feed(msg.html or '')
        html.finish_anchor()
        urls = {link['url'] for link in html.links}
        for match in re.finditer(r'https?://[^\s<>"\u3002\uff0c\uff1b\uff09]+', msg.text or ''):
            url = match.group()
            if url not in urls:
                if len(html.links) >= MAX_LINKS:
                    raise AnalysisError('邮件链接数量超过解析限制。', code='mail_parse_limit')
                html.links.append({'text':url, 'url':url})
                urls.add(url)
        attachments = [{'filename': a.filename, 'content_type': a.content_type, 'size': len(a.payload)}
                       for a in msg.attachments]
        warnings = ['附件内容未检测；未访问链接；未验证发件人真实性。']
        if not msg.subject or not msg.from_:
            warnings.append('邮件缺少主题或发件人。')
        if '\ufffd' in (msg.text or '') + (msg.html or ''):
            warnings.append('邮件存在无法解码的字符。')
        mail=Mail(id=mail_id, content_hash=hashlib.sha256(raw).hexdigest(),
                    sender=str(make_header(decode_header(msg.headers.get('from', ('',))[0]))),
                    reply_to=', '.join(msg.reply_to), recipients=list(msg.to),
                    subject=msg.subject, text=msg.text or '',
                    html_text='\n'.join(html.visible), hidden_text='\n'.join(html.hidden),
                    links=html.links, attachments=attachments, warnings=warnings)
        if inspect_html:
            from .html_static import inspect_message
            mail.warnings[0]='HTML已尝试静态检查，覆盖状态见各来源；其他附件内容未检测；未访问链接或验证发件人。'
            return InspectedMail(**mail.model_dump(),html_inspections=inspect_message(raw))
        return mail
    except AnalysisError:
        raise
    except Exception as e:
        raise AnalysisError('邮件解析失败，请检查 EML 格式。') from e

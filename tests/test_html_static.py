from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest

from mailguard.engine import request_for, validate_assessment
from mailguard.html_static import inspect_html, inspect_message, MAX_HTML_BYTES
from mailguard.models import AnalysisError
from mailguard.parser import parse_mail


def message(body='<p>请查看附件。</p>', attachment=None):
    msg=EmailMessage()
    msg['From']='notice@company.test'
    msg['To']='reader@company.test'
    msg['Subject']='测试邮件'
    msg.set_content('请查看说明。')
    msg.add_alternative(body,subtype='html')
    if attachment is not None:
        msg.add_attachment(attachment.encode(),maintype='application',subtype='octet-stream',filename='账单.HTML')
    return msg.as_bytes()


def test_every_control_and_external_form_owner_and_override():
    facts=inspect_html(b'''<base href="https://legit.test/base/">
        <form id="f" action="search"><label for="a">OTP</label><input id="a">
        <label>PIN<input type="text" name="code"></label><textarea name="feedback"></textarea>
        <select name="department"></select><div contenteditable="true"></div></form>
        <button form="f" formaction="https://collector.test/submit">Send</button>
        <input form="missing" type="text" formaction="https://ignored.test/">
        ''','body:1')
    assert len(facts['controls'])==7
    assert facts['forms'][0]['destination']['resolved']=='https://legit.test/base/search'
    assert facts['controls'][0]['sensitive_hint']
    assert facts['controls'][1]['sensitive_hint']
    button=facts['controls'][5]
    assert button['form_owner']=='F001' and button['submission_override_applies']
    assert button['submission_override']['hostname']=='collector.test'
    assert not facts['controls'][6]['submission_override_applies']
    assert facts['status']=='partial' and 'unresolved_form_owner' in facts['notes']


def test_static_facts_are_not_risk_decisions_and_prefilled_values_omitted():
    facts=inspect_html(b'''<form><input value="SECRET_VALUE"><textarea>SECRET_TEXTAREA</textarea>
        <label>Search<input name="query"></label></form>
        <template><form><input type="password"></form></template>''','attachment:2')
    assert 'verdict' not in facts and 'risk' not in facts
    assert 'SECRET_VALUE' not in str(facts) and 'SECRET_TEXTAREA' not in str(facts)
    assert facts['controls'][-1]['disabled']
    assert facts['forms'][0]['destination']['status']=='unknown_current_document'


def test_html_body_and_mislabeled_html_attachment_checked_without_execution(monkeypatch):
    import socket
    monkeypatch.setattr(socket,'create_connection',MagicMock(side_effect=AssertionError('network is forbidden')))
    raw=message('<form><input name="query"></form>',
                '<form action="https://collector.test/post"><input type="password"></form>'
                '<script src="https://external.test/code.js"></script>'
                '<script>document.write("<input>");fetch("https://external.test/")</script>')
    records=inspect_message(raw)
    assert len(records)==2 and records[0]['source_id'].startswith('body:')
    assert records[1]['source_id'].startswith('attachment:')
    assert records[1]['filename']=='账单.HTML'
    assert records[1]['external_scripts'][0]['destination']['hostname']=='external.test'
    assert records[1]['counts']['controls']==1  # Script-generated input was not executed.
    assert records[1]['status']=='partial'
    socket.create_connection.assert_not_called()


@pytest.mark.parametrize('html,status,note',[
    (b'x'*(MAX_HTML_BYTES+1),'skipped_size',None),
    (b'<div>'*300,'partial','nesting_limit'),
    (b'<input>'*250,'partial','record_limit'),
    (b'<form action="http://[invalid]/"><input type></form>','inspected',None),
    (b'\xff<form><input></form>','partial','decoding_replacement'),
])
def test_limits_and_bad_markup_are_explicit_not_risk_or_safe(html,status,note):
    facts=inspect_html(html,'body:1')
    assert facts['status']==status and 'verdict' not in facts
    if note:assert note in facts['notes']


def test_v6_requires_new_inspection_and_validates_anchored_html_evidence():
    raw=message(attachment='<form action="https://collector.test/post"><input type="password" name="bankPIN"></form>')
    with pytest.raises(AnalysisError,match='静态检查'):
        request_for(parse_mail(raw),{},'v6')
    request=request_for(parse_mail(raw,inspect_html=True),{},'v6')
    anchor=next(e for e in request['evidence_catalog'] if e['field']=='html_inspections' and 'bankPIN' in e['quote'])
    answer={'verdict':'risk','risk_types':['credential_phishing'],
        'skill_checks':[{'skill_id':s['id'],'outcome':'finding' if s['id']=='mail-html-forms' else 'no_finding',
                         'evidence_ids':[anchor['id']] if s['id']=='mail-html-forms' else []} for s in request['skills']]}
    assessment=validate_assessment(answer,request)
    assert len(assessment.skill_checks)==9 and assessment.evidence[0].field=='html_inspections'
    assert not {'expected','source_label','labels'} & request.keys()
    answer['skill_checks'][-1]['evidence_ids']=['not-an-anchor']
    with pytest.raises(ValueError):validate_assessment(answer,request)


def test_imap_static_check_preserves_read_only_not_seen(monkeypatch):
    from types import SimpleNamespace
    from mailguard.imap_source import fetch_test_folder_batch
    for key in ('IMAP_USERNAME','IMAP_PASSWORD','IMAP_FOLDER'):monkeypatch.setenv(key,'test-placeholder')
    box=MagicMock()
    box.fetch.return_value=[SimpleNamespace(uid='1',obj=SimpleNamespace(as_bytes=lambda:message('<input name="otp">')))]
    result=fetch_test_folder_batch(mailbox_factory=MagicMock(return_value=box),inspect_html=True)
    assert result['mails'][0].html_inspections[0]['counts']['controls']==1
    assert box.fetch.call_args.kwargs['mark_seen'] is False
    box.folder.set.assert_called_once_with('test-placeholder',readonly=True)


def test_old_versions_exclude_new_field_and_all_synthetic_mail_parse():
    from mailguard.evaluation import dataset
    for _,mail,context in dataset('all',inspect_html=True):
        assert 'html_inspections' in request_for(mail,context,'v6')['email']
        assert 'html_inspections' not in request_for(mail,context,'v5')['email']


def test_non_html_attachment_not_misrepresented_and_chinese_charset_kept():
    msg=EmailMessage();msg['From']='notice@company.test';msg['Subject']='中文表单'
    msg.set_content('仅检查HTML附件的能力边界。')
    msg.add_attachment(b'%PDF-test fixture only',maintype='application',subtype='pdf',filename='普通报表.pdf')
    assert inspect_message(msg.as_bytes())==[]
    facts=inspect_html('<label>银行验证码<input name="code"></label>'.encode('gb18030'),
                       'attachment:2',charset='gb18030')
    assert facts['controls'][0]['label']=='银行验证码' and facts['controls'][0]['sensitive_hint']


def test_frozen_request_change_stops_before_api(monkeypatch,tmp_path):
    from mailguard import public_benchmark as benchmark
    from mailguard.frozen_corpus import identify_mail,publish_corpus
    mail=identify_mail(parse_mail(message(),inspect_html=True))
    monkeypatch.setattr(benchmark,'RUNS',tmp_path/'runs')
    monkeypatch.setattr(benchmark,'provider_config',lambda _: {'model':'glm-5.3-flash','api_key':'test-placeholder'})
    call=MagicMock(side_effect=AssertionError('must stop before the API'))
    monkeypatch.setattr(benchmark,'api_assess',call)
    base=tmp_path/'frozen'
    publish_corpus(base,manifest=[{'id':mail.id,'split':'dev','content_hash':mail.content_hash}],mails=[mail],
                   labels={mail.id:{'id':mail.id,'expected':'clear'}},context={},
                   freeze_metadata={'request_hashes':{mail.id:'0'*64}})
    with pytest.raises(ValueError,match='Frozen evaluation requests changed'):
        benchmark.run('frozen-request-check',base=base,version='v6')
    call.assert_not_called()
    assert not (tmp_path/'runs').exists()

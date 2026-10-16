from email.message import EmailMessage
import pytest
from mailguard.engine import request_for, validate_assessment
from mailguard.parser import parse_mail


def example():
    message=EmailMessage()
    message['From']='Notice <notice@sender.test>'
    message['To']='reader@company.test'
    message['Subject']='Verify account'
    message.set_content('Verify your account.')
    message.add_alternative('<a href="https://collector.test/login">Click&nbsp;here</a>',subtype='html')
    mail=parse_mail(message.as_bytes())
    request=request_for(mail,{},'v5')
    anchor=next(x for x in request['evidence_catalog'] if x['field']=='html_text' and 'Click' in x['quote'])
    answer={'verdict':'risk','risk_types':['credential_phishing'],
        'skill_checks':[{'skill_id':s['id'],'outcome':'finding' if s['id']=='mail-identity-access' else 'no_finding',
                         'evidence_ids':[anchor['id']] if s['id']=='mail-identity-access' else []}
                        for s in request['skills']]}
    return request,answer


def test_ids_remove_quote_copying_without_losing_exact_source():
    request,answer=example()
    assessment=validate_assessment(answer,request)
    assert assessment.verdict=='risk'
    assert assessment.evidence[0].quote=='Click\xa0here'
    assert assessment.evidence[0].quote in request['email']['html_text']


def test_unknown_or_tampered_anchor_cannot_be_used_as_evidence():
    request,answer=example()
    check=next(c for c in answer['skill_checks']if c['outcome']=='finding')
    check['evidence_ids']=['invented-id']
    with pytest.raises(ValueError): validate_assessment(answer,request)
    request,answer=example()
    request['evidence_catalog'][0]['quote']='fabricated'
    with pytest.raises(ValueError): validate_assessment(answer,request)


def test_presentation_defect_does_not_discard_valid_decision():
    request,answer=example()
    answer['explanation']={'unexpected':'object'}
    answer['recommendation']=[]
    assert validate_assessment(answer,request).verdict=='risk'


def test_malformed_url_is_a_parse_warning_not_a_risk_verdict():
    request,_=example()
    from mailguard.models import Mail
    mail=Mail.model_validate(request['email'])
    mail.links=[{'text':'http://[not-an-ipv6]/','url':'http://[not-an-ipv6]/'}]
    req=request_for(mail,{},'v5')
    assert any('无法解析' in s for s in req['observations'])
    assert 'verdict' not in req


def test_core_decision_and_skill_coverage_remain_strict():
    request,answer=example()
    answer['verdict']='clear'
    with pytest.raises(ValueError): validate_assessment(answer,request)
    request,answer=example()
    answer['skill_checks'].pop()
    with pytest.raises(ValueError): validate_assessment(answer,request)


def test_imap_parse_error_isolated_from_other_messages(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from mailguard.imap_source import fetch_test_folder_batch
    from tests.test_core import raw_mail
    for key in ('IMAP_USERNAME','IMAP_PASSWORD','IMAP_FOLDER'):
        monkeypatch.setenv(key,'unit-test-placeholder')
    box=MagicMock()
    bad=SimpleNamespace(uid='1',obj=SimpleNamespace(as_bytes=lambda:b''))
    good=SimpleNamespace(uid='2',obj=SimpleNamespace(as_bytes=raw_mail))
    box.fetch.return_value=[bad,good]
    result=fetch_test_folder_batch(mailbox_factory=MagicMock(return_value=box))
    assert len(result['mails'])==1 and result['mails'][0].id=='imap-2'
    assert result['errors'][0]['id']=='imap-1'
    assert box.fetch.call_args.kwargs['mark_seen'] is False
    box.folder.set.assert_called_once_with('unit-test-placeholder',readonly=True)

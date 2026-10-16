import copy
import json
import pytest
from mailguard.engine import request_for, validate_assessment, digest
from mailguard.parser import parse_mail
from mailguard.public_corpus import derived, normalize, shingles, template
from mailguard.public_benchmark import candidate_request
from tests.test_core import raw_mail
from tests.test_skills import skill_answer


def indexed_answer():
    request = request_for(parse_mail(raw_mail()), {}, 'v4')
    answer = skill_answer(request)
    for check in answer['skill_checks']:
        evidence = check.pop('evidence')
        check['evidence_indices'] = [0] if evidence else []
    return request, answer


def test_v4_references_still_validate_original_quotes_and_all_skills():
    request, answer = indexed_answer()
    validated = validate_assessment(answer, request)
    assert validated.verdict == 'risk'
    assert len(validated.skill_checks) == 8
    bad = copy.deepcopy(answer)
    bad['evidence'][0]['quote'] = '捏造的证据'
    with pytest.raises(ValueError, match='原文'):
        validate_assessment(bad, request)
    bad = copy.deepcopy(answer)
    bad['skill_checks'].pop()
    with pytest.raises(ValueError, match='技能检查'):
        validate_assessment(bad, request)
    bad = copy.deepcopy(answer)
    bad['verdict'] = 'clear'
    with pytest.raises(ValueError, match='汇总'):
        validate_assessment(bad, request)


@pytest.mark.parametrize('index', [-1, 99, True, '0'])
def test_v4_rejects_invalid_evidence_references(index):
    request, answer = indexed_answer()
    next(c for c in answer['skill_checks'] if c['outcome']=='finding')['evidence_indices'] = [index]
    with pytest.raises(ValueError):
        validate_assessment(answer, request)


def test_public_input_preserves_domain_relationships_and_changes_hash():
    mail = parse_mail(raw_mail())
    clean = derived(mail)
    assert clean.sender.endswith('@xinghe.test')
    assert 'support@' not in clean.sender
    assert clean.links == mail.links
    data = clean.model_dump()
    data.update(id='pending',content_hash='')
    assert digest(data) == clean.content_hash
    request = candidate_request(clean, {}, '检查请求动作。')
    assert not {'label','expected','source_label','provenance'} & request['email'].keys()
    assert 'public_prompt_guidance_hash' in request['analysis_config']


def test_template_grouping_catches_changed_recipient_and_tracking_url():
    a = 'Hello a@example.test please sign this document using https://a.example/123 now 1234'
    b = 'Hello b@example.test please sign this document using https://b.example/456 now 9999'
    assert template(a) == template(b)
    assert shingles(a) == shingles(b)


def test_batch_missing_results_remain_in_denominator():
    from mailguard.evaluation import score
    rows=[{'mail_id':'a','status':'ok','assessment':{'verdict':'risk'}},
          {'mail_id':'b','status':'error','source':'not_attempted'},
          {'mail_id':'c','status':'ok','assessment':{'verdict':'clear'}}]
    truth={'a':{'expected':'risk'},'b':{'expected':'risk'},'c':{'expected':'clear'}}
    result=score(rows,truth)
    assert result['risk_recall']==.5 and result['risk_precision']==1
    assert result['errors']==1

import json
import hashlib
import shutil
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from mailguard.config import ROOT
from mailguard.parser import parse_mail
from mailguard.engine import request_for, digest, validate_assessment, analyze, api_assess
from mailguard.evaluation import dataset
from mailguard.models import AnalysisError
from mailguard.skills import REGISTRY, select_skills
from tests.test_core import raw_mail, assessment, response

def skill_answer(request):
    result = assessment()
    result['skill_checks'] = [{'skill_id':s['id'],
        'outcome':'finding' if s['id']=='mail-identity-access' else 'no_finding',
        'evidence':result['evidence'] if s['id']=='mail-identity-access' else [],
        'note':'索取验证码。' if s['id']=='mail-identity-access' else '测试中无该类证据。'} for s in request['skills']]
    return result

def test_skill_sources_are_local_and_selected_checks_required():
    request = request_for(parse_mail(raw_mail()), {}, 'v3')
    answer = skill_answer(request)
    validate_assessment(answer, request)
    assert {s['id'] for s in request['skills']} <= {x[0] for x in REGISTRY}
    assert {'mail-triage-core','mail-prompt-injection','mail-false-positive-review'} <= {s['id'] for s in request['skills']}
    answer['skill_checks'].pop()
    with pytest.raises(ValueError,match='技能检查'):
        validate_assessment(answer,request)

def test_skill_aggregation_cannot_hide_finding():
    request = request_for(parse_mail(raw_mail()), {}, 'v3')
    answer = skill_answer(request)
    answer['verdict'] = 'clear'
    with pytest.raises(ValueError,match='汇总结论'):
        validate_assessment(answer,request)

def test_skill_evidence_must_be_real_and_reported():
    request = request_for(parse_mail(raw_mail()), {}, 'v3')
    answer = skill_answer(request)
    answer['skill_checks'][0].update(outcome='finding', evidence=[{'field':'text','quote':'伪造证据'}])
    with pytest.raises(ValueError):
        validate_assessment(answer,request)

def test_skill_edit_invalidates_replay(tmp_path, monkeypatch):
    import mailguard.skills as skills
    shutil.copytree(ROOT/'skills', tmp_path/'skills')
    monkeypatch.setattr(skills, 'SKILLS_DIR', tmp_path/'skills')
    mail=parse_mail(raw_mail())
    before=request_for(mail, {}, 'v3')
    key=digest(before)
    folder=tmp_path/'replay';folder.mkdir()
    (folder/f'{key}.json').write_text(json.dumps({'request_hash':key,'request':before,
        'assessment':skill_answer(before),'source':'assistant_session','recorded_at':'test'}))
    assert analyze(mail,{},version='v3',replay_dir=folder)['status']=='ok'
    path=tmp_path/'skills/mail-triage-core/SKILL.md'
    path.write_text(path.read_text()+'\n新增专项证据检查。\n')
    assert digest(request_for(mail,{},'v3')) != key
    with pytest.raises(AnalysisError,match='无匹配回放'):
        analyze(mail,{},version='v3',replay_dir=folder)

def test_live_request_actually_contains_skills(monkeypatch):
    monkeypatch.setenv('GPT_API_KEY','test-only')
    monkeypatch.setenv('GPT_MODEL','test-model')
    request=request_for(parse_mail(raw_mail()), {}, 'v3')
    client=MagicMock()
    client.chat.completions.create.return_value=response(json.dumps(skill_answer(request)))
    api_assess(request,'gpt',client=client)
    messages=client.chat.completions.create.call_args.kwargs['messages']
    for s in request['skills']:
        assert s['instructions'] in messages[0]['content']
    assert 'skills' not in json.loads(messages[1]['content'])

def test_original_corpus_unchanged():
    base=ROOT/'data/testbench'
    snapshot=json.loads((base/'versions/v1.json').read_text())
    for name,sha in snapshot['hashes'].items():
        assert hashlib.sha256((base/name).read_bytes()).hexdigest()==sha
    assert json.loads((base/'labels.json').read_text())[:50] == snapshot['labels.json']
    assert len(list(dataset('all','legacy'))) == 50
    assert len(list(dataset('all','extension'))) == 50

def test_missing_skill_does_not_silently_bypass(tmp_path):
    with pytest.raises(AnalysisError,match='缺失'):
        select_skills(parse_mail(raw_mail()),tmp_path)

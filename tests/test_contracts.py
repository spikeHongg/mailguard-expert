import copy
import json
import sys
from pathlib import Path

import pytest

from mailguard import cli
from mailguard.contracts import PROMPTS, request_for, digest, make_replay_record, read_replay_record
from mailguard.engine import analyze
from mailguard.evaluation import dataset
from mailguard.parser import parse_mail
from tests.test_core import raw_mail,assessment


def response_for(request):
    answer=assessment()
    version=request['prompt_version']
    if version in ('v1','v2'):return answer
    checks=[]
    for skill in request['skills']:
        finding=skill['id']=='mail-identity-access'
        check={'skill_id':skill['id'],'outcome':'finding'if finding else'no_finding','note':'合成测试。'}
        if version=='v3':check['evidence']=answer['evidence']if finding else[]
        elif version=='v4':check['evidence_indices']=[0]if finding else[]
        else:
            eid=next(e['id']for e in request['evidence_catalog']if e['field']=='text'and'验证码'in e['quote'])
            check['evidence_ids']=[eid]if finding else[]
        checks.append(check)
    answer['skill_checks']=checks
    if version in ('v5','v6'):
        answer.pop('evidence');answer.pop('limitations')
    return answer


@pytest.mark.parametrize('version',list(PROMPTS))
def test_cli_import_then_replay_uses_archive_contract(tmp_path,monkeypatch,version):
    mail=parse_mail(raw_mail(),inspect_html=version=='v6')
    request=request_for(mail,{},version)
    requests=tmp_path/'requests.json';responses=tmp_path/'responses.json'
    requests.write_text(json.dumps([{'id':mail.id,'request':request}]))
    responses.write_text(json.dumps([{'id':mail.id,'assessment':response_for(request)}]))
    monkeypatch.setattr(cli,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['mailguard','import','--requests',str(requests),'--responses',str(responses)])
    cli.main()
    replay_dir=tmp_path/'data/replays'
    before=requests.read_text()
    result=analyze(mail,{},version=version,replay_dir=replay_dir)
    assert result['source']=='assistant_replay'and result['assessment']['verdict']=='risk'
    assert result['request_hash']==digest(request)and requests.read_text()==before
    archive=json.loads((replay_dir/f'{digest(request)}.json').read_text())
    assert archive['assessment_format']=='normalized-assessment-v1'
    # Old importers already saved normalized content, but had no format tag.
    archive.pop('assessment_format')
    assert read_replay_record(archive,request)[0].verdict=='risk'


@pytest.mark.parametrize('version',['v3','v4','v5','v6'])
def test_normalized_archives_still_validate_evidence_and_skill_coverage(version):
    mail=parse_mail(raw_mail(),inspect_html=version=='v6')
    request=request_for(mail,{},version)
    archive=make_replay_record(response_for(request),request,recorded_at='fixture',provenance='fixture')
    for mutation in ('evidence','skill','verdict','format','request'):
        bad=copy.deepcopy(archive)
        if mutation=='evidence':bad['assessment']['evidence'][0]['quote']='伪造原文'
        elif mutation=='skill':bad['assessment']['skill_checks'].pop()
        elif mutation=='verdict':bad['assessment']['verdict']='clear'
        elif mutation=='format':bad['assessment_format']='wire-response'
        else:bad['request']['context']={'changed':True}
        with pytest.raises(ValueError):read_replay_record(bad,request)


def test_all_historical_request_hashes_remain_identical():
    frozen=json.loads((Path(__file__).parent/'fixtures/contract-request-hashes.json').read_text())
    for version,expected in frozen['versions'].items():
        actual=digest([(m.id,digest(request_for(m,c,version)))for _,m,c in dataset('all',inspect_html=version=='v6')])
        assert actual==expected,version


def test_replay_preserves_partial_report_status_without_erasing_valid_decision():
    request=request_for(parse_mail(raw_mail(),inspect_html=True),{},'v6')
    response=response_for(request);response.pop('explanation')
    record=make_replay_record(response,request,recorded_at='fixture',provenance='fixture')
    assessment,meta=read_replay_record(record,request)
    assert assessment.verdict=='risk'and meta['report_status']=='partial'

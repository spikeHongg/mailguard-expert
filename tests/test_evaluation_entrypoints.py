"""Entry-point adapters retain their scoring and immutable-resume behavior."""
import json
from types import SimpleNamespace

import pytest

from mailguard import evaluation,public_benchmark
from mailguard.contracts import request_for
from mailguard.frozen_corpus import identify_mail,publish_corpus
from mailguard.models import AnalysisError
from mailguard.parser import parse_mail
from tests.test_core import raw_mail
from tests.helpers import replay_fixture


def test_page_batch_replays_all_cases_with_same_score(monkeypatch,tmp_path):
    data=list(evaluation.dataset('all'))
    truth=evaluation.labels()
    rows=replay_fixture(tmp_path/'replays')
    baseline={'results':list(rows.values()),'metrics':evaluation.score(list(rows.values()),truth)}
    monkeypatch.setattr(evaluation,'dataset',lambda *args,**kwargs:data)
    monkeypatch.setattr(evaluation,'ROOT',tmp_path)
    (tmp_path/'reports').mkdir()
    calls=[]
    real=evaluation.analyze
    def analyzed(mail,*args,**kwargs):
        calls.append(mail.id)
        return real(mail,*args,replay_dir=tmp_path/'replays',**kwargs)
    monkeypatch.setattr(evaluation,'analyze',analyzed)
    def labels():
        assert len(calls)==100,'answers were read before inference finished'
        return truth
    monkeypatch.setattr(evaluation,'labels',labels)
    path,result=evaluation.run_batch('replay','v3','all',workers=20)
    assert result['metrics']==baseline['metrics']
    assert {r['mail_id']:r['assessment']['verdict']for r in result['results']}=={
        r['mail_id']:r['assessment']['verdict']for r in baseline['results']}
    assert len({r['mail_id']for r in result['results']})==100
    assert all(r['source']=='assistant_replay'for r in result['results'])
    assert json.loads(path.read_text())['metrics']==baseline['metrics']


def test_public_entry_resumes_without_replacing_auth_error_or_raw_response(monkeypatch,tmp_path):
    # Use the actual publisher/reader, lifecycle, and scorer; replace only the external model.
    mail=parse_mail(raw_mail(),inspect_html=True)
    mails=[identify_mail({**mail.model_dump(),'subject':f'模拟样例 {i}'})for i in range(3)]
    context={}
    manifest=[{'id':m.id,'split':'dev','content_hash':m.content_hash,'group_id':f'g{i}'}for i,m in enumerate(mails)]
    requests={m.id:request_for(m,context,'v6')for m in mails}
    base=tmp_path/'corpus'
    publish_corpus(base,manifest=manifest,mails=mails,context=context,
                   labels={m.id:{'id':m.id,'expected':'clear'}for m in mails})
    monkeypatch.setattr(public_benchmark,'RUNS',tmp_path/'runs')
    monkeypatch.setattr(public_benchmark,'provider_config',lambda _: {'api_key':'never-record-this-key',
        'model':'glm-5.3-flash','base_url':'https://example.invalid'})
    calls=[]
    def model(request,provider,**kwargs):
        calls.append(request['email']['id'])
        assert request==requests[request['email']['id']]
        assert not {'expected','label','source_label','provenance'}&request['email'].keys()
        kwargs['response_sink']({'fixture':'raw response'})
        if len(calls)==1:
            raise AnalysisError('鉴权失败',code='http_401',attempts=[{'status':'http_error','http_status':401}])
        return SimpleNamespace(model_dump=lambda:{'verdict':'clear'}),{'model':'glm-5.3-flash','attempts':[]}
    monkeypatch.setattr(public_benchmark,'api_assess',model)
    first=public_benchmark.run('resume-public',base=base,version='v6',workers=2)
    assert not first['finished']and first['metrics']['errors']==3
    folder=tmp_path/'runs/resume-public'
    checkpoint=folder/'cases'/f'{mails[0].id}.json'
    raw_response=folder/'responses'/f'{mails[0].id}.json'
    original=(checkpoint.read_bytes(),raw_response.read_bytes())
    second=public_benchmark.run('resume-public',base=base,version='v6',workers=2)
    assert second['finished']and second['metrics']['completed']==2 and second['metrics']['errors']==1
    assert len(calls)==3 and len(set(calls))==3
    assert (checkpoint.read_bytes(),raw_response.read_bytes())==original
    complete=(folder/'report.json').read_bytes()
    with pytest.raises(AnalysisError)as exc:
        public_benchmark.run('resume-public',base=base,version='v6',workers=20)
    assert exc.value.code=='run_complete'
    assert (folder/'report.json').read_bytes()==complete
    for path in folder.rglob('*.json'):
        assert 'never-record-this-key'not in path.read_text()

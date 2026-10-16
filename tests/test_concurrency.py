import pytest
from mailguard.concurrency import InFlightMeter
from mailguard.config import provider_concurrency
from mailguard.models import AnalysisError


@pytest.mark.parametrize('value',[0,21,True,1.5,'bad'])
def test_concurrency_limits_reject_invalid_values(value):
    with pytest.raises(AnalysisError):provider_concurrency('glm',value)


def test_provider_configurable_defaults_and_override(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_CONCURRENCY',raising=False)
    monkeypatch.delenv('GLM_CONCURRENCY',raising=False)
    assert provider_concurrency('glm')==5
    monkeypatch.setenv('GLM_CONCURRENCY','7')
    assert provider_concurrency('deepseek')==20
    assert provider_concurrency('glm')==7
    assert provider_concurrency('glm',20)==20


def test_probe_does_not_retry_a_rate_limit(monkeypatch):
    import httpx
    from unittest.mock import MagicMock
    from openai import RateLimitError
    from mailguard.engine import api_assess,request_for
    from mailguard.parser import parse_mail
    from tests.test_core import raw_mail
    monkeypatch.setenv('GLM_API_KEY','test-placeholder')
    client=MagicMock()
    client.chat.completions.create.side_effect=RateLimitError('limited',
        response=httpx.Response(429,request=httpx.Request('POST','https://example.test')),body={'error':{'code':'1302'}})
    meter=InFlightMeter()
    with pytest.raises(AnalysisError)as exc:
        api_assess(request_for(parse_mail(raw_mail()),{}),'glm',client=client,observer=meter,max_transport_retries=0)
    assert client.chat.completions.create.call_count==1 and meter.peak==1 and meter.active==0
    assert exc.value.attempts[0]['provider_error_code']=='1302'


def test_profile_uses_fixed_label_free_requests_and_keeps_all_cases(tmp_path,monkeypatch):
    import json,time
    from types import SimpleNamespace
    from mailguard import concurrency_benchmark as bench
    from mailguard.parser import parse_mail
    from tests.test_core import raw_mail
    mails=[parse_mail(raw_mail(),str(i),inspect_html=True)for i in range(4)]
    monkeypatch.setattr(bench,'RUNS',tmp_path)
    monkeypatch.setattr(bench,'dataset',lambda *a,**kw:[({},m,{})for m in mails])
    monkeypatch.setattr(bench,'labels',lambda:{m.id:{'expected':'clear'}for m in mails})
    monkeypatch.setattr(bench,'provider_config',lambda _: {'api_key':'private-placeholder','model':'test','base_url':'https://example.test'})
    def fake(request,provider,**kwargs):
        assert 'expected'not in json.dumps(request)
        assert kwargs['max_transport_retries']==0
        with kwargs['observer'].track():time.sleep(.005)
        return SimpleNamespace(model_dump=lambda:{'verdict':'clear'}),{'model':'test','attempts':[{'status':'response','validation':'passed'}]}
    monkeypatch.setattr(bench,'api_assess',fake)
    result=bench.run('deepseek','fixture',[1,2],samples=4)
    assert result['all_levels_attempted']and all(r['metrics']['completed']==4 for r in result['stages'])
    assert result['stages'][0]['request_count']==4
    for path in tmp_path.rglob('*.json'):assert 'private-placeholder'not in path.read_text()


def test_public_runner_rejects_cross_corpus_split_before_model_config():
    from mailguard.public_benchmark import run
    for corpus,split in [('public','all'),('synthetic','test')]:
        with pytest.raises(ValueError,match='Split does not belong'):run('bad-split',corpus=corpus,split=split)

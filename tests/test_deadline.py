import asyncio
import json
import httpx
import pytest
from threading import Event
from openai import AsyncOpenAI
import mailguard.engine as engine
from mailguard.config import progress_timeout_seconds
from mailguard.models import AnalysisError
from mailguard.parser import parse_mail
from tests.test_core import raw_mail, assessment


def test_batch_cancellation_closes_keepalive_without_retry(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','deadline-test-only')
    cancel_event=Event()
    closed=[]
    class KeepAlive(httpx.AsyncByteStream):
        async def __aiter__(self):
            count=0
            while True:
                yield b'\n'
                await asyncio.sleep(0.005)
                count+=1
                if count==6: cancel_event.set()
        async def aclose(self):
            closed.append(True)
    def factory(**kwargs):
        return AsyncOpenAI(**kwargs,http_client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request:httpx.Response(200,stream=KeepAlive(),headers={'content-type':'application/json'}))))
    monkeypatch.setattr(engine,'AsyncOpenAI',factory)
    with pytest.raises(AnalysisError) as exc:
        engine.api_assess(engine.request_for(parse_mail(raw_mail()),{}),'deepseek',sleep=lambda _:None,cancel_event=cancel_event)
    assert exc.value.code=='progress_timeout'
    assert len(exc.value.attempts)==len(closed)==1
    assert exc.value.attempts[0]['status']=='cancelled'


def test_async_live_path_keeps_coding_protocol(monkeypatch):
    monkeypatch.setenv('GLM_API_KEY','deadline-test-only')
    monkeypatch.setenv('GLM_MODEL','glm-5.3')
    monkeypatch.setenv('GLM_BASE_URL','https://open.bigmodel.cn/api/coding/paas/v4')
    monkeypatch.setenv('GLM_THINKING','enabled')
    monkeypatch.setenv('EVAL_PROGRESS_TIMEOUT_SECONDS','300')
    def respond(request):
        assert request.url.path=='/api/coding/paas/v4/chat/completions'
        body=json.loads(request.content)
        assert body['thinking']=={'type':'enabled'}
        assert body['response_format']=={'type':'json_object'}
        return httpx.Response(200,json={'id':'test','created':1,'model':'glm-5.3',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(assessment())}}]})
    monkeypatch.setattr(engine,'AsyncOpenAI',lambda **kw:AsyncOpenAI(**kw,http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))))
    result=engine.analyze(parse_mail(raw_mail()),{},'glm','v2')
    assert result['transport_config']=={'read_timeout_seconds':600,'batch_cancellation':False}
    assert result['access_mode']=='coding_plan'
    monkeypatch.setenv('EVAL_PROGRESS_TIMEOUT_SECONDS','invalid')
    with pytest.raises(AnalysisError,match='EVAL_PROGRESS_TIMEOUT_SECONDS'):
        progress_timeout_seconds()

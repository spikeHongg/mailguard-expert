import json
import httpx
import pytest
from openai import OpenAI
from mailguard.config import provider_config, configuration_status
from mailguard.engine import api_assess, api_options, request_for, analyze
from mailguard.models import AnalysisError
from mailguard.parser import parse_mail
from tests.test_core import raw_mail, assessment

@pytest.mark.parametrize('base,host,path', [
    ('https://open.bigmodel.cn/api/paas/v4/','open.bigmodel.cn','/api/paas/v4/chat/completions'),
    ('https://api.z.ai/api/paas/v4/','api.z.ai','/api/paas/v4/chat/completions'),
    ('https://open.bigmodel.cn/api/coding/paas/v4','open.bigmodel.cn','/api/coding/paas/v4/chat/completions'),
])
def test_glm_official_sdk_contract(monkeypatch,base,host,path):
    monkeypatch.setenv('GLM_API_KEY','glm-test-only-key')
    monkeypatch.setenv('GPT_API_KEY','different-gpt-key')
    monkeypatch.setenv('GLM_MODEL','glm-5.2')
    monkeypatch.setenv('GLM_BASE_URL',base)
    monkeypatch.setenv('GLM_THINKING','disabled')
    monkeypatch.setenv('GLM_MAX_TOKENS','4500')
    seen=[]
    def respond(request):
        seen.append(request)
        assert request.url.host==host
        assert request.url.path==path
        assert request.headers['authorization']=='Bearer glm-test-only-key'
        body=json.loads(request.content)
        assert body['model']=='glm-5.2'
        assert body['thinking']=={'type':'disabled'}
        assert body['max_tokens']==4500 and 'max_completion_tokens' not in body
        assert body['response_format']=={'type':'json_object'}
        return httpx.Response(200,json={'id':'glm-response','request_id':'glm-request',
            'created':1,'model':'glm-5.2','choices':[{'index':0,'finish_reason':'stop',
            'message':{'role':'assistant','content':json.dumps(assessment())}}],
            'usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}})
    cfg=provider_config('glm')
    with OpenAI(api_key=cfg['api_key'],base_url=cfg['base_url'],max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        result=analyze(parse_mail(raw_mail()),{},'glm','v2',client=client)
    assert len(seen)==1 and result['source']=='live_api' and result['provider']=='glm'
    assert result['attempts'][0]['request_id']=='glm-request'
    assert result['usage']['total_tokens']==150
    assert result['access_mode']==('coding_plan' if '/coding/' in path else 'standard_api')
    assert configuration_status()['glm']
    assert 'glm-test-only-key' not in json.dumps(result)

def test_missing_glm_key_never_uses_other_key(monkeypatch):
    monkeypatch.delenv('GLM_API_KEY',raising=False)
    monkeypatch.setenv('GPT_API_KEY','gpt-present')
    monkeypatch.setenv('DEEPSEEK_API_KEY','ds-present')
    with pytest.raises(AnalysisError,match='glm 待配置'):
        api_assess(request_for(parse_mail(raw_mail()),{}),'glm')

def test_glm_thinking_and_output_budget_validation(monkeypatch):
    monkeypatch.setenv('GLM_MODEL','glm-5.3')
    monkeypatch.setenv('GLM_THINKING','disabled')
    with pytest.raises(AnalysisError,match='需要思考模式'):
        api_options('glm')
    monkeypatch.setenv('GLM_THINKING','enabled')
    monkeypatch.setenv('GLM_MAX_TOKENS','8000')
    assert api_options('glm')['max_tokens']==8000
    assert api_options('glm')['extra_body']['reasoning_effort']=='low'
    monkeypatch.setenv('GLM_MAX_TOKENS','invalid')
    with pytest.raises(AnalysisError,match='GLM_MAX_TOKENS'):
        api_options('glm')

def test_unknown_provider_fails_explicitly():
    with pytest.raises(AnalysisError,match='未知模型提供方'):
        provider_config('typo')

def test_glm_quota_1113_is_not_retried(monkeypatch):
    monkeypatch.setenv('GLM_API_KEY','unit-test-key')
    monkeypatch.setenv('GLM_MODEL','glm-5.2')
    monkeypatch.setenv('GLM_THINKING','disabled')
    calls=[]
    def respond(request):
        calls.append(1)
        return httpx.Response(429,json={'error':{'code':'1113','message':'private error text unit-test-key'}})
    with OpenAI(api_key='unit-test-key',base_url='https://open.bigmodel.cn/api/paas/v4/',max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        with pytest.raises(AnalysisError) as exc:
            api_assess(request_for(parse_mail(raw_mail()),{}),'glm',client=client,sleep=lambda _:None)
    assert len(calls)==1 and exc.value.code=='provider_1113'
    assert exc.value.attempts[0]['provider_error_code']=='1113'
    assert 'unit-test-key' not in str(exc.value)

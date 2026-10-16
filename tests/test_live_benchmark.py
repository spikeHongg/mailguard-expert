import json
from pathlib import Path
import pytest
import mailguard.live_benchmark as bench
from mailguard.models import AnalysisError
from tests.helpers import replay_fixture

@pytest.fixture
def setup(monkeypatch,tmp_path):
    monkeypatch.setattr(bench,'ROOT',tmp_path)
    monkeypatch.setenv('GPT_API_KEY','test-secret-not-for-artifacts')
    monkeypatch.setenv('GPT_MODEL','test-model')
    rows=replay_fixture(tmp_path/'replays')
    baseline={'id':'contract-fixture','label':'工作流夹具','provider':'fixture','results':list(rows.values()),
              'metrics':bench.score(list(rows.values()),bench.labels()),
              'label_hash':bench.digest(bench.labels()),
              'dataset_hash':bench.digest({mid:r['request_hash'] for mid,r in rows.items()})}
    path=tmp_path/'baseline.json';path.write_text(json.dumps(baseline))
    monkeypatch.setattr(bench,'BASELINE',path)
    calls=[]
    def analyzer(mail,context,provider,version):
        calls.append(mail.id)
        return {**rows[mail.id],'provider':provider,'source':'live_api','model':'test-model',
            'elapsed_seconds':0.01,'attempts':[{'status':'response','validation':'passed',
            'usage':{'prompt_tokens':10,'completion_tokens':5}}]}
    analyzer.expected_accuracy=baseline['metrics']['accuracy']
    return tmp_path,analyzer,calls

def test_resume_does_not_repeat_and_no_secret_saved(setup):
    tmp,analyzer,calls=setup
    folder,r=bench.run_live('gpt',tmp/'run',analyzer,limit=3)
    assert r['attempted']==3 and not r['finished']
    assert r['metrics']['completed']==3 and r['metrics']['errors']==97
    _,r=bench.run_live('gpt',folder,analyzer,workers=2)
    assert len(calls)==len(set(calls))==100
    assert r['finished'] and r['runtime']['prompt_tokens']==1000
    assert r['metrics']['accuracy']==analyzer.expected_accuracy
    for p in tmp.rglob('*.json'):
        assert 'test-secret-not-for-artifacts' not in p.read_text()
    output=tmp/'comparison.md'
    bench.write_comparison([folder/'report.json'],output)
    assert f'{analyzer.expected_accuracy:.1%}' in output.read_text()

def test_auth_failure_stops_after_smoke_and_keeps_all_denominators(setup):
    tmp,_,calls=setup
    def fail(*args):
        calls.append(1)
        raise AnalysisError('认证失败',code='http_401',attempts=[{'status':'http_error','http_status':401}])
    _,r=bench.run_live('gpt',tmp/'failed',fail)
    assert len(calls)==1 and not r['finished']
    assert r['metrics']['errors']==100 and r['attempted']==1
    assert sum(x['source']=='not_attempted' for x in r['results'])==99
    assert r['vs_replay']['regressed']==['m001']
    assert len(r['verdict_disagreements'])==1

def test_resume_rejects_model_change(setup,monkeypatch):
    tmp,analyzer,_=setup
    folder,_=bench.run_live('gpt',tmp/'run',analyzer,limit=1)
    monkeypatch.setenv('GPT_MODEL','other-model')
    with pytest.raises(AnalysisError,match='配置或测试输入已改变'):
        bench.run_live('gpt',folder,analyzer)

def test_deepseek_glm_comparison_labels(setup,monkeypatch):
    tmp,analyzer,_=setup
    for name in ('DEEPSEEK_API_KEY','GLM_API_KEY'):
        monkeypatch.setenv(name,'test-only-key')
    monkeypatch.setenv('GLM_MODEL','glm-5.2')
    monkeypatch.setenv('GLM_THINKING','disabled')
    monkeypatch.setenv('GLM_MAX_TOKENS','4500')
    a,_=bench.run_live('deepseek',tmp/'ds',analyzer,limit=1)
    b,r=bench.run_live('glm',tmp/'glm',analyzer,limit=1)
    assert r['provider']=='glm'
    out=tmp/'pair.md'
    bench.write_comparison([a/'report.json',b/'report.json'],out)
    text=out.read_text()
    assert '|deepseek|' in text and '|glm|' in text
    assert '本轮尚未完成100封' in text

def test_invalid_outputs_after_smoke_do_not_abort_coverage(setup):
    tmp,analyzer,calls=setup
    def failing_after_smoke(mail,*args):
        if mail.id=='m001': return analyzer(mail,*args)
        raise AnalysisError('格式错误',code='invalid_output')
    _,r=bench.run_live('gpt',tmp/'invalid',failing_after_smoke,limit=5)
    assert r['attempted']==5
    assert r['metrics']['completed']==1


def test_progress_watchdog_cancels_active_and_stops_new_requests(setup,monkeypatch):
    import time
    tmp,good,_=setup
    monkeypatch.setattr(bench,'progress_timeout_seconds',lambda:0.04)
    def blocking(mail,context,provider,version,cancel_event=None):
        if mail.id=='m001': return good(mail,context,provider,version)
        assert cancel_event.wait(2)
        raise AnalysisError('进度超时',code='progress_timeout')
    monkeypatch.setattr(bench,'analyze',blocking)
    start=time.monotonic()
    _,r=bench.run_live('gpt',tmp/'stalled',blocking,limit=4,workers=2)
    assert r['stop_reason']=='progress_timeout' and r['attempted']==3
    assert r['metrics']['completed']==1 and time.monotonic()-start<1
    assert r['results'][3]['source']=='not_attempted'


def test_progress_resets_on_each_saved_mail_not_each_request(setup,monkeypatch):
    import time
    tmp,good,_=setup
    monkeypatch.setattr(bench,'progress_timeout_seconds',lambda:0.12)
    def varying(mail,*args):
        time.sleep(0.22 if mail.id=='m002' else 0.03)
        return good(mail,*args)
    _,r=bench.run_live('gpt',tmp/'progressing',varying,limit=10,workers=2)
    assert r['attempted']==10 and r['metrics']['completed']==10
    assert r['stop_reason'] is None  # m002 alone exceeded the watchdog interval.

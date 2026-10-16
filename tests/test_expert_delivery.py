import base64
import json
from pathlib import Path
from threading import Event, Thread
import time
import os
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from mailguard.contracts import decode_response, digest
from mailguard.expert import Expert, asset, private_directory
from mailguard.models import AnalysisError
from mailguard.service import create_app, service_token


def wire(job, verdict='clear'):
    checks = [{'skill_id': s['id'], 'outcome': 'no_finding', 'evidence_ids': []}
              for s in job['request']['skills']]
    risks = []
    if verdict == 'risk':
        evidence = next(e for e in job['request']['evidence_catalog'] if '客户端专用密码' in e['quote'])
        checks[1].update(outcome='finding', evidence_ids=[evidence['id']])
        risks = ['credential_phishing']
    return {'verdict': verdict, 'risk_types': risks, 'skill_checks': checks,
            'explanation': '仅用于验证接口与证据契约的固定夹具。', 'recommendation': '人工核实。'}


def fake_assessor(request, provider, **kwargs):
    response = wire({'request': request}, 'risk')
    kwargs['response_sink']({'content': json.dumps(response), 'model': 'fixture-model'})
    return decode_response(response, request), {'model': 'fixture-model', 'report_status': 'complete'}


def test_prepare_submit_review_provenance_and_immutable_result(tmp_path):
    expert = Expert(tmp_path)
    job = expert.prepare(sample='m002')
    assert job['origin'] == 'synthetic_sample'
    assert job['request']['context'] == {}
    assert len(job['request']['skills']) == 9
    assert 'public_prompt_guidance_hash' in job['request']['analysis_config']
    assert not {'expected', 'rationale', 'labels'} & job['request'].keys()
    response = wire(job, 'risk')
    result = expert.submit(job['id'], response, request_hash=job['request_hash'])
    assert result['status'] == 'ok'
    assert result['result']['source'] == 'host_agent'
    assert result['result']['model_verified'] is False
    assert result['result']['assessment']['evidence']
    assert '没有可检查的HTML来源' in result['result']['assessment']['limitations'][0]
    assert expert.submit(job['id'], response, request_hash=job['request_hash']) == result
    review = expert.review(job['id'], 'risk', '用户独立确认索要客户端专用密码。')
    assert review['reviews'][0]['source'] == 'human_review'
    assert review['result'] == result['result']
    with pytest.raises(AnalysisError) as error:
        expert.submit(job['id'], wire(job), request_hash=job['request_hash'])
    assert error.value.code == 'job_conflict'


def test_invalid_evidence_and_hash_are_never_clear(tmp_path):
    expert = Expert(tmp_path)
    job = expert.prepare(sample='m002')
    response = wire(job, 'risk')
    response['skill_checks'][1]['evidence_ids'] = ['E9999']
    with pytest.raises(AnalysisError) as error:
        expert.submit(job['id'], response, request_hash=job['request_hash'])
    assert error.value.code == 'invalid_output'
    assert expert.get(job['id'])['status'] == 'prepared'
    assert expert.get(job['id'])['result'] is None
    assert expert.get(job['id'])['attempt_records'] == 1
    with pytest.raises(AnalysisError) as error:
        expert.submit(job['id'], wire(job), request_hash='0'*64)
    assert error.value.code == 'request_mismatch'
    with expert.connect() as db:
        db.execute('UPDATE jobs SET request=? WHERE id=?', ('{}', job['id']))
    with pytest.raises(AnalysisError) as error:
        expert.get(job['id'])
    assert error.value.code == 'request_changed'


def test_failure_timeout_and_actual_source(tmp_path):
    expert = Expert(tmp_path)
    job = expert.prepare(sample='m002')
    live = expert.analyze(job['id'], 'deepseek', assessor=fake_assessor)
    assert live['result']['source'] == 'live_api'
    assert live['result']['model'] == 'fixture-model'
    assert live['attempt_records'] == 1
    def fail(*args, **kwargs):
        raise AnalysisError('测试鉴权失败', code='http_401')
    failed = expert.analyze(expert.prepare(sample='m002')['id'], 'glm', assessor=fail)
    assert failed['status'] == 'error'
    assert 'assessment' not in failed['result']
    def stall(*args, cancel_event, **kwargs):
        cancel_event.wait(2)
        assert cancel_event.is_set()
        raise AnalysisError('测试取消', code='progress_timeout')
    timed = expert.analyze(expert.prepare(sample='m002')['id'], 'glm', assessor=stall, idle_seconds=.03)
    assert timed['result']['error_code'] == 'progress_timeout'


def test_doctor_offline_imap_explicit_and_private_permissions(tmp_path):
    expert = Expert(tmp_path)
    doctor = expert.doctor()
    assert doctor['skills'] == 9 and doctor['api_verified'] is False
    assert doctor['imap_verified'] is False
    calls = []
    def reader(**kwargs):
        from mailguard.parser import parse_mail
        calls.append(kwargs)
        return {'mails': [parse_mail(asset('sample', 'm002').read_bytes(), 'imap-test', inspect_html=True)], 'errors': []}
    batch = expert.read_imap(3, reader=reader)
    assert calls == [{'limit': 3, 'inspect_html': True}]
    assert batch['sent_to_model'] is False
    assert batch['jobs'][0]['origin'] == 'imap_readonly'
    assert not (expert.db.stat().st_mode & 0o077)
    token = service_token(expert.state)
    assert token == service_token(expert.state)
    assert not ((expert.state/'service.token').stat().st_mode & 0o077)


def test_existing_directory_needs_no_mkdir_permission(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError('host sandbox refuses a redundant mkdir')
    monkeypatch.setattr(Path, 'mkdir', denied)
    assert private_directory(tmp_path) == tmp_path
    with pytest.raises(PermissionError):
        private_directory(tmp_path/'missing')


def test_explicit_env_file_does_not_fall_back_to_checkout_secrets(tmp_path):
    env_file = tmp_path/'empty.env'; env_file.write_text('# isolated host fixture\n')
    clean = {k:v for k,v in os.environ.items() if not k.startswith(('DEEPSEEK_', 'GLM_', 'GPT_', 'MAILGUARD_'))}
    proc = subprocess.run([sys.executable, '-m', 'mailguard.expert_cli', '--env-file', str(env_file),
        '--state-dir', str(tmp_path/'state'), 'doctor'], capture_output=True, text=True, env=clean)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)['providers_configured'] == {'deepseek': False, 'glm': False}


def test_http_auth_parsing_roundtrip_and_live(tmp_path):
    expert = Expert(tmp_path)
    client = TestClient(create_app(expert=expert, token='test-secret', assessor=fake_assessor))
    headers = {'Authorization': 'Bearer test-secret'}
    assert client.get('/health').status_code == 200
    assert client.get('/v1/capabilities').status_code == 401
    assert client.get('/v1/capabilities', headers={'Authorization': 'test-secret'}).status_code == 401
    assert client.get('/v1/capabilities', headers=headers).json()['skills'] == 9
    assert client.post('/v1/prepare', json={'eml_base64': 'bad'}, headers=headers).status_code == 400
    assert client.post('/v1/prepare', json={'path': '/etc/passwd'}, headers=headers).status_code == 422
    assert client.post('/v1/prepare', json={'sample': 'm002', 'eml_base64': 'AA=='}, headers=headers).status_code == 400
    job = client.post('/v1/prepare', json={'sample': 'm002'}, headers=headers).json()
    result = client.post('/v1/jobs/'+job['id']+'/analyze', json={'provider': 'deepseek'}, headers=headers)
    assert result.status_code == 200 and result.json()['status'] == 'ok'
    assert client.post('/v1/jobs/'+job['id']+'/analyze', json={'provider': 'deepseek'}, headers=headers).status_code == 409
    native = client.post('/v1/prepare', json={'sample': 'm026'}, headers=headers).json()
    saved = client.post('/v1/jobs/'+native['id']+'/submit', json={
        'request_hash': native['request_hash'], 'response': wire(native)}, headers=headers)
    assert saved.json()['result']['source'] == 'host_agent'
    assert client.get('/v1/jobs/'+native['id'], headers=headers).json()['status'] == 'ok'
    assert client.post('/v1/prepare', content=b'x'*(8*1024*1024+1), headers=headers).status_code == 413


def test_http_provider_limit_is_shared_and_does_not_consume_waiting_job(tmp_path):
    expert = Expert(tmp_path)
    entered, release = Event(), Event()
    def blocked(request, provider, **kwargs):
        entered.set()
        release.wait(5)
        return fake_assessor(request, provider, **kwargs)
    app = create_app(expert=expert, token='test-secret', assessor=blocked,
                     limits={'deepseek': 1, 'glm': 1, 'gpt': 1})
    headers = {'Authorization': 'Bearer test-secret'}
    client = TestClient(app)
    jobs = [expert.prepare(sample='m002') for _ in range(2)]
    results = []
    thread = Thread(target=lambda: results.append(client.post('/v1/jobs/'+jobs[0]['id']+'/analyze',
        json={'provider': 'deepseek'}, headers=headers)))
    thread.start()
    assert entered.wait(5)
    try:
        busy = client.post('/v1/jobs/'+jobs[1]['id']+'/analyze', json={'provider': 'deepseek'}, headers=headers)
        assert busy.status_code == 429
        assert expert.get(jobs[1]['id'])['status'] == 'prepared'
    finally:
        release.set()
        thread.join(5)
    assert results[0].json()['status'] == 'ok'

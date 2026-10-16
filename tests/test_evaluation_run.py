"""Lifecycle guarantees exercised through the same interface as all entry points."""
import json
from threading import Barrier
import time

import pytest

from mailguard import evaluation_run as lifecycle
from mailguard.concurrency import InFlightMeter
from mailguard.evaluation_run import EvaluationJob, EvaluationRun, Produced, StopPolicy
from mailguard.models import AnalysisError


def jobs(count=4):
    return [EvaluationJob(f'm{i}', {'prompt_version':'v2','email':{'id':f'm{i}','text':'模拟输入'}},
                          metadata={'provider':'glm'}) for i in range(count)]


def good(job, cancel):
    return {'status':'ok','source':'live_api','assessment':{'verdict':'clear'}}


def report(outcome):
    return {'id':outcome.manifest['id'],'finished':outcome.finished,'results':outcome.results}


def test_resume_preserves_success_and_failure_and_only_calls_pending(tmp_path):
    tasks=jobs();calls=[]
    def worker(job,cancel):
        calls.append(job.key)
        if job.key=='m1':
            return Produced(error=AnalysisError('无效JSON',code='invalid_output'),artifacts={'responses':['not JSON']})
        return Produced(good(job,cancel),{'responses':[{'verdict':'clear'}]})
    def completed(key,row,count):
        assert (tmp_path/'responses'/f'{key}.json').exists()
        assert json.loads((tmp_path/'cases'/f'{key}.json').read_text())==row
    with EvaluationRun(tasks,folder=tmp_path) as run:
        first=run.execute(worker,workers=2,idle_seconds=10,limit=2,on_completed=completed)
        assert first.attempted==2 and not first.finished
        assert [r['source']for r in first.results[2:]]==['not_attempted']*2
        run.save_report(report(first))
    original={p.name:p.read_bytes()for p in (tmp_path/'cases').glob('*.json')}
    with EvaluationRun(tasks,folder=tmp_path) as run:
        resumed=run.execute(worker,workers=2,idle_seconds=10)
        assert resumed.finished and resumed.newly_attempted==2
        assert resumed.records['m1']['status']=='error'
        run.save_report(report(resumed))
    assert sorted(calls)==['m0','m1','m2','m3']
    assert all((tmp_path/'cases'/name).read_bytes()==raw for name,raw in original.items())
    complete=(tmp_path/'report.json').read_bytes()
    with EvaluationRun(tasks,folder=tmp_path) as run:
        assert run.complete_report['finished']
        nothing=run.execute(lambda *args:pytest.fail('completed run must not call worker'),workers=20,idle_seconds=10)
        assert nothing.newly_attempted==0
        run.save_report({**report(nothing),'changed':'must not replace a completed report'})
    assert (tmp_path/'report.json').read_bytes()==complete


def test_changed_identity_unknown_checkpoint_and_wrong_mail_are_rejected(tmp_path):
    tasks=jobs(1)
    with EvaluationRun(tasks,folder=tmp_path) as run:
        run.execute(good,workers=1,idle_seconds=10)
    changed=jobs(1);changed[0].request['email']['text']='新的输入'
    with pytest.raises(AnalysisError)as exc:
        with EvaluationRun(changed,folder=tmp_path):pass
    assert exc.value.code=='run_identity_changed'
    path=tmp_path/'cases/m0.json';row=json.loads(path.read_text());row['mail_id']='other'
    path.write_text(json.dumps(row))
    with pytest.raises(AnalysisError,match='检查点'):
        with EvaluationRun(tasks,folder=tmp_path):pass
    path.unlink();(tmp_path/'cases/unknown.json').write_text('{}')
    with pytest.raises(AnalysisError,match='未知任务'):
        with EvaluationRun(tasks,folder=tmp_path):pass


def test_directory_lock_blocks_duplicate_run_and_releases(tmp_path):
    with EvaluationRun(jobs(),folder=tmp_path):
        for _ in range(2):
            with pytest.raises(AnalysisError)as exc:
                with EvaluationRun(jobs(),folder=tmp_path):pass
            assert exc.value.code=='run_busy'
    with EvaluationRun(jobs(),folder=tmp_path):pass


def test_twenty_tasks_overlap_and_preserve_input_order():
    barrier=Barrier(20);meter=InFlightMeter()
    def worker(job,cancel):
        with meter.track():barrier.wait(timeout=5)
        return good(job,cancel)
    with EvaluationRun(jobs(40)) as run:
        outcome=run.execute(worker,workers=20,idle_seconds=10)
    assert outcome.finished and meter.peak==20 and meter.active==0
    assert [r['mail_id']for r in outcome.results]==[f'm{i}'for i in range(40)]


def test_idle_timeout_cancels_inflight_and_keeps_not_attempted():
    def worker(job,cancel):
        assert cancel.wait(2)
        raise AnalysisError('进度超时',code='progress_timeout')
    with EvaluationRun(jobs(8)) as run:
        outcome=run.execute(worker,workers=2,idle_seconds=.03)
    assert outcome.stop_reason=='progress_timeout'and outcome.attempted==2
    assert [r['source']for r in outcome.results[2:]]==['not_attempted']*6


def test_first_smoke_failure_stops_but_later_format_failures_continue():
    def auth(*args):raise AnalysisError('认证失败',code='http_401')
    policy=StopPolicy(smoke_check=True,stop_on_smoke_error=True,consecutive_errors=3)
    with EvaluationRun(jobs()) as run:
        outcome=run.execute(auth,workers=20,idle_seconds=10,policy=policy)
    assert outcome.attempted==1 and outcome.stop_reason=='http_401'
    def invalid(job,cancel):
        if job.key=='m0':return good(job,cancel)
        raise AnalysisError('输出错误',code='invalid_output')
    with EvaluationRun(jobs(8)) as run:
        outcome=run.execute(invalid,workers=2,idle_seconds=10,policy=policy)
    assert outcome.finished and outcome.stop_reason is None


def test_unexpected_worker_error_cancels_peers_before_executor_shutdown(tmp_path):
    barrier=Barrier(2)
    def worker(job,cancel):
        barrier.wait(timeout=2)
        if job.key=='m0':raise RuntimeError('implementation bug')
        assert cancel.wait(1),'peer was not cancelled before executor shutdown'
        raise AnalysisError('cancelled',code='progress_timeout')
    with EvaluationRun(jobs(2),folder=tmp_path) as run:
        with pytest.raises(RuntimeError,match='implementation bug'):
            run.execute(worker,workers=2,idle_seconds=5)
    with EvaluationRun(jobs(2),folder=tmp_path):pass  # Lock is released on failure too.


def test_cross_job_result_and_snapshot_tampering_are_rejected(tmp_path):
    def wrong(job,cancel):return {**good(job,cancel),'request_hash':'wrong'}
    with EvaluationRun(jobs(1),folder=tmp_path) as run:
        with pytest.raises(AnalysisError,match='任务不匹配'):
            run.execute(wrong,workers=1,idle_seconds=10)
    assert not list((tmp_path/'cases').glob('*.json'))
    path=tmp_path/'requests/m0.json';value=json.loads(path.read_text());value['email']['text']='篡改'
    path.write_text(json.dumps(value))
    with pytest.raises(AnalysisError,match='快照'):
        with EvaluationRun(jobs(1),folder=tmp_path):pass


def test_legacy_manifest_and_checkpoints_remain_compatible(tmp_path):
    tasks=jobs(2);identity={'request_hashes':{j.key:j.request_hash for j in tasks}}
    legacy={'id':'historical-id','created_at':'2026-10-15T12:00:00+08:00','identity':identity,'extra':'preserved'}
    (tmp_path/'cases').mkdir()
    row={**good(tasks[0],None),'mail_id':'m0','request_hash':tasks[0].request_hash}
    (tmp_path/'cases/m0.json').write_text(json.dumps(row))
    original=(tmp_path/'cases/m0.json').read_bytes()
    (tmp_path/'manifest.json').write_text(json.dumps(legacy))
    with EvaluationRun(tasks,folder=tmp_path,identity=identity) as run:
        outcome=run.execute(good,workers=1,idle_seconds=10)
        assert outcome.newly_attempted==1 and outcome.manifest['extra']=='preserved'
        assert outcome.manifest['id']=='historical-id'
    assert (tmp_path/'cases/m0.json').read_bytes()==original


def test_interrupted_manifest_creation_reuses_verified_snapshots(tmp_path):
    tasks=jobs(2)
    (tmp_path/'requests').mkdir()
    path=tmp_path/'requests/m0.json';path.write_text(json.dumps(tasks[0].request))
    original=path.read_bytes()
    with EvaluationRun(tasks,folder=tmp_path)as run:
        outcome=run.execute(good,workers=1,idle_seconds=10)
        assert outcome.finished
    assert path.read_bytes()==original
    assert len(list((tmp_path/'requests').glob('*.json')))==2


def test_resume_archives_orphan_response_before_requesting_again(tmp_path):
    tasks=jobs(1)
    with EvaluationRun(tasks,folder=tmp_path):pass
    (tmp_path/'responses').mkdir()
    path=tmp_path/'responses/m0.json';path.write_text('[{"original":"interrupted response"}]')
    original=path.read_bytes()
    def worker(job,cancel):
        archives=list((tmp_path/'orphaned').glob('*/responses/m0.json'))
        assert len(archives)==1 and archives[0].read_bytes()==original
        assert not path.exists(),'orphan response must be preserved before a new request'
        return Produced(good(job,cancel),{'responses':[{'new':'completed response'}]})
    with EvaluationRun(tasks,folder=tmp_path)as run:
        outcome=run.execute(worker,workers=1,idle_seconds=10)
        assert outcome.finished
    assert json.loads(path.read_text())==[{'new':'completed response'}]
    assert next((tmp_path/'orphaned').glob('*/responses/m0.json')).read_bytes()==original


@pytest.mark.parametrize('wall_gap,mono_gap,stops',[(601,0,True),(601,601,True),(0,601,True),(200,200,False),(-600,200,False)])
def test_scheduler_resume_checks_clock_before_new_dispatch(monkeypatch,tmp_path,wall_gap,mono_gap,stops):
    clocks={'wall':1000.,'mono':10.}
    monkeypatch.setattr(time,'time',lambda:clocks['wall'])
    monkeypatch.setattr(time,'monotonic',lambda:clocks['mono'])
    original_wait=lifecycle.wait
    def paused_wait(active,**kwargs):
        result=original_wait(active,**kwargs)
        clocks['wall']+=wall_gap;clocks['mono']+=mono_gap
        return result
    monkeypatch.setattr(lifecycle,'wait',paused_wait)
    calls=[]
    def worker(job,cancel):calls.append(job.key);return good(job,cancel)
    with EvaluationRun(jobs(2),folder=tmp_path) as run:
        outcome=run.execute(worker,workers=1,idle_seconds=300)
    assert outcome.stop_reason==('progress_timeout'if stops else None)
    assert calls==(['m0']if stops else ['m0','m1'])

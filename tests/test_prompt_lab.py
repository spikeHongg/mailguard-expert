import copy
import json
import time
import pytest
from mailguard import prompt_lab as lab
from mailguard.engine import request_for, digest, analyze
from mailguard.evaluation import dataset
from mailguard.models import AnalysisError
from tests.helpers import replay_fixture


def test_private_feedback_permissions_under_permissive_umask(tmp_path):
    import os
    import stat
    if os.name != 'posix':
        pytest.skip('POSIX permission check')
    previous = os.umask(0o022)
    try:
        path = tmp_path/'reviews'/'record.json'
        lab.write(path, {'note':'synthetic fixture'}, immutable=True)
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.parent.chmod(0o755)
        lab.write(path, {'note':'updated fixture'})
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(AnalysisError):
            lab.write(path, {'note':'cannot overwrite'}, immutable=True)
        assert json.loads(path.read_text())['note'] == 'updated fixture'
    finally:
        os.umask(previous)


@pytest.fixture
def work(monkeypatch,tmp_path):
    monkeypatch.setattr(lab,'LAB',tmp_path/'lab')
    monkeypatch.setattr(lab,'REVIEWS',tmp_path/'reviews')
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-private-key')
    monkeypatch.setenv('GLM_API_KEY','test-private-key')
    monkeypatch.setenv('GLM_MODEL','glm-5.3-flash')
    monkeypatch.setenv('GLM_THINKING','enabled')
    rows=replay_fixture(tmp_path/'replays')
    from mailguard import prompt_lab_ui
    monkeypatch.setattr(prompt_lab_ui,'ROOT',tmp_path)
    (tmp_path/'reports').mkdir()
    (tmp_path/'reports/fixture.json').write_text(json.dumps({'id':'contract-fixture','label':'工作流夹具','results':list(rows.values())}))
    _,mail,context=next(iter(dataset('dev')))
    return tmp_path,rows,mail,context


def candidate(parent=None):
    return lab.save_candidate({'instructions':'先逐项完成技能检查，再把每个finding的原文证据完整复制到顶层evidence，最后检查字段和引用完全一致。',
        'rationale':'通过明确输出顺序减少证据汇总遗漏。','possible_regressions':['输出可能变长']},parent,[],{'source':'test_fixture'})


def test_feedback_is_explicit_dev_only_and_latest_opt_out(work,monkeypatch):
    tmp,rows,mail,context=work
    lab.store_feedback(mail,context,rows[mail.id],'risk','需完整同步证据',consent=False)
    assert lab.eligible_feedback()==[]
    accepted=lab.store_feedback(mail,context,rows[mail.id],'risk','需完整同步证据',consent=True)
    _,held,ctx=next(iter(dataset('holdout')))
    lab.store_feedback(held,ctx,rows[held.id],'risk','保留集评论不得送优化器',consent=True)
    monkeypatch.setattr(lab,'labels',lambda:pytest.fail('optimizer must not read labels'))
    req,feedback=lab.optimization_request()
    assert [r['id'] for r in feedback]==[accepted['id']]
    assert '保留集评论不得送优化器' not in json.dumps(req,ensure_ascii=False)
    assert 'expected' not in req['payload']
    lab.store_feedback(mail,context,rows[mail.id],'risk','取消后续优化使用',consent=False)
    assert lab.eligible_feedback()==[]
    bad={**rows[mail.id],'request_hash':'wrong'}
    with pytest.raises(AnalysisError,match='不匹配'):
        lab.store_feedback(mail,context,bad,'risk','输入错误',consent=True)


def test_candidate_hash_isolated_from_replay_and_tampering_rejected(work):
    _,rows,mail,ctx=work
    old=digest(request_for(mail,ctx,'v3'))
    c=candidate()
    assert digest(request_for(mail,ctx,'v3'))==old==rows[mail.id]['request_hash']
    assert digest(request_for(mail,ctx,'v3',c['id']))!=old
    with pytest.raises(AnalysisError,match='无匹配回放'):
        analyze(mail,ctx,'replay','v3',candidate_id=c['id'])
    p=lab.LAB/'candidates'/f'{c["id"]}.json'
    c['payload']['instructions']='tampered';p.write_text(json.dumps(c))
    with pytest.raises(AnalysisError,match='哈希'):
        lab.load_candidate(c['id'])


def test_proposal_preserves_human_source_and_no_auto_activation(work):
    _,rows,mail,ctx=work
    lab.store_feedback(mail,ctx,rows[mail.id],'risk','证据需要同步',consent=True)
    def generator(req,provider):
        assert req['payload']['review_feedback'][0]['review_comment']=='证据需要同步'
        return {'instructions':'每次输出前核对各技能原文引用与顶层字段的引用完全一致，不允许遗漏或重写。',
            'rationale':'解决人工反馈的证据同步问题。','possible_regressions':[]},{'source':'test_fixture','model':'fake'}
    c=lab.propose('glm',generator=generator)
    assert c['payload']['feedback_refs'] and lab.active_candidate() is None
    assert not lab.adoption_status(c['id'])['eligible']


def test_paired_evaluation_resume_and_model_gated_adoption(work,monkeypatch):
    tmp,rows,_,_=work;c=candidate();calls=[]
    def fake(mail,context,provider,version,*,candidate_id,cancel_event):
        calls.append((provider,candidate_id,mail.id))
        row=copy.deepcopy(rows[mail.id]);row.update(source='live_api',provider=provider,model=lab.provider_config(provider)['model'],
            request_hash=digest(request_for(mail,context,version,candidate_id)))
        if not candidate_id and mail.id=='m001':
            raise AnalysisError('test invalid output',code='invalid_output')
        return row
    folder,smoke=lab.evaluate_pair(c['id'],'deepseek',5,analyzer=fake)
    assert not smoke['gate_passed'] and smoke['changes']['fixed']==['m001']
    lab.evaluate_pair(c['id'],'deepseek',5,resume=folder,analyzer=fake)
    assert len(calls)==10
    for provider in lab.PROVIDERS:
        full_folder,r=lab.evaluate_pair(c['id'],provider,100,analyzer=fake)
        assert r['gate_passed'] and r['finished']
    from mailguard.prompt_report import summarize
    summary=summarize(full_folder/'report.json',tmp/'comparison.md')
    assert summary['fixed']==['m001'] and summary['regressed']==[]
    assert 'AI研发反馈' in (tmp/'comparison.md').read_text()
    assert lab.adoption_status(c['id'])['eligible']
    monkeypatch.setenv('GLM_MODEL','glm-5.3')
    assert not lab.adoption_status(c['id'])['eligible']
    monkeypatch.setenv('GLM_MODEL','glm-5.3-flash')
    lab.activate(c['id'],'人工检查新增退化为0，启用候选')
    assert lab.active_candidate()==c['id']
    lab.rollback('恢复历史版本')
    assert lab.active_candidate() is None
    for p in tmp.rglob('*.json'): assert 'test-private-key' not in p.read_text()


def test_progress_failure_preserved_and_not_eligible(work,monkeypatch):
    c=candidate()
    monkeypatch.setattr(lab,'progress_timeout_seconds',lambda:0.03)
    def stalled(*args,cancel_event,**kwargs):
        assert cancel_event.wait(1)
        raise AnalysisError('进度超时',code='progress_timeout')
    _,r=lab.evaluate_pair(c['id'],'glm',5,analyzer=stalled)
    assert not r['finished'] and not r['gate_passed'] and r['stop_reason']=='progress_timeout'
    assert r['attempted']==5  # Current GLM default is five concurrent tasks.
    assert r['reports']['candidate']['metrics']['errors']==5


def test_cycle_stops_when_no_gain_and_does_not_activate(work,monkeypatch):
    c=candidate();count=[]
    monkeypatch.setattr(lab,'propose',lambda *args,**kwargs:c)
    def evaluated(*args):
        count.append(1)
        return lab.LAB,{'scores':{'baseline':0.8,'candidate':0.8},'finished':True,'stop_reason':None}
    monkeypatch.setattr(lab,'evaluate_pair',evaluated)
    r=lab.cycle('glm',rounds=3,limit=5)
    assert r['status']=='no_measured_improvement' and len(count)==2
    assert lab.active_candidate() is None


def test_prompt_lab_page_feedback_and_candidate_inspection(work):
    from streamlit.testing.v1 import AppTest
    at=AppTest.from_file(str(lab.ROOT/'app.py'),default_timeout=30).run()
    next(r for r in at.radio if r.label=='页面').set_value('提示词自迭代').run()
    assert not at.exception
    assert next(b for b in at.button if b.label=='生成一个候选提示词').disabled
    next(s for s in at.selectbox if s.label=='你的复核判断').set_value('risk')
    next(t for t in at.text_area if t.label=='具体问题和期望行为').set_value('界面测试：同步每条分项风险证据')
    next(c for c in at.checkbox if c.label.startswith('允许将这封')).check()
    next(b for b in at.button if b.label=='保存优化反馈').click().run()
    assert not at.exception and len(lab.eligible_feedback())==1
    c=candidate()
    _,rows,_,_=work
    def fake(mail,context,provider,version,**kwargs):
        return {**rows[mail.id],'source':'live_api','request_hash':digest(request_for(mail,context,version,kwargs.get('candidate_id')))}
    lab.evaluate_pair(c['id'],'glm',5,analyzer=fake)
    at.run()
    assert not at.exception
    assert next(b for b in at.button if b.label=='启用这版提示词').disabled
    assert len(at.dataframe)>0
    assert any('候选' in option for s in at.selectbox if s.label=='选择待复核运行' for option in s.options)


def test_ai_development_feedback_never_masquerades_as_human(work):
    _,rows,mail,ctx=work
    with pytest.raises(AnalysisError,match='授权'):
        lab.store_feedback(mail,ctx,rows[mail.id],'risk','AI意见',consent=True,source='ai_review')
    r=lab.store_feedback(mail,ctx,rows[mail.id],'risk','AI建议同步证据',consent=True,
        source='ai_review',reviewer='test_assistant',authorization='user requested AI development review')
    assert lab.eligible_feedback()==[]
    assert lab.eligible_feedback('ai_review')[0]['id']==r['id']
    request,feedback=lab.optimization_request(feedback_source='ai_review')
    assert request['payload']['feedback_source']=='ai_review'
    assert request['payload']['review_feedback'][0]['review_source']=='ai_review'
    c=lab.save_candidate({'instructions':'每次输出都需要将分项风险引用逐字复制到顶层证据数组，并逐条确认来源字段。',
        'rationale':'AI研发反馈指出格式错误。','possible_regressions':[]},None,feedback,{'source':'assistant_session'})
    assert c['payload']['feedback_sources']==['ai_review']

"""Streamlit controls for the bounded human-feedback prompt optimization workflow."""
import json
import streamlit as st
from .config import ROOT, provider_config
from .evaluation import dataset
from .models import AnalysisError
from . import prompt_lab as lab

NAMES={'risk':'发现风险','clear':'未发现明显风险','review':'信息不足，需核实'}


def render():
    st.subheader('提示词自迭代')
    st.caption('人工反馈 → 生成候选 → 双模型同条件对照 → 查看新增退化 → 人工启用／回退')
    st.info('优化提示词及其表述，保留模型权重、8项技能文件、原始标签和证据校验。已有100封只作已见回归；小样本通过不能启用。')
    for provider in lab.PROVIDERS:
        st.text(f'{provider}：{provider_config(provider)["model"]}')
    st.text('当前启用：'+(lab.active_candidate() or '原始 v3'))
    st.text(f'可用于优化的人工反馈：{len(lab.eligible_feedback())} 封开发集邮件')
    st.caption(f'另有AI研发反馈 {len(lab.eligible_feedback("ai_review"))} 封；来源独立记录，不计作人工反馈。')
    feedback_tab, candidate_tab, evaluation_tab=st.tabs(['1 · 人工反馈','2 · 生成与迭代','3 · 对照与启用'])
    with feedback_tab:
        st.caption('可复核历史批量中的失败邮件；明确勾选后，仅模拟开发集反馈会发送给优化模型。不会发送IMAP、导入邮件或保留集反馈。')
        runs=[]
        for path in (ROOT/'reports').glob('*.json'):
            r=json.loads(path.read_text());runs.append((path.stat().st_mtime_ns,r))
        for path in (lab.LAB/'evaluations').glob('*/report.json'):
            r=json.loads(path.read_text())
            for side,label in [('baseline','基线'),('candidate','候选')]:
                runs.append((path.stat().st_mtime_ns,{'id':r['id']+'-'+side,
                    'label':f'{r["provider"]} {label} · {r["candidate_id"]}',
                    'results':r['reports'][side]['results']}))
        runs=[r for _,r in sorted(runs,key=lambda item:item[0],reverse=True)]
        if runs:
            run=st.selectbox('选择待复核运行',runs,format_func=lambda r:r['label']+' · '+r['id'])
            dev={m.id:(m,c) for _,m,c in dataset('dev')}
            rows=[r for r in run['results'] if r['mail_id'] in dev and r.get('request_hash') and r.get('source')!='not_attempted']
            if rows:
                row=st.selectbox('选择复核样例',rows,format_func=lambda r:f'{r["mail_id"]} · {r["status"]}')
                mail,context=dev[row['mail_id']]
                st.text(mail.subject);st.text(mail.text or mail.html_text)
                st.json(row.get('assessment') or {'error_code':row.get('error_code'),'attempts':row.get('attempts',[])})
                with st.form('lab_feedback'):
                    verdict=st.selectbox('你的复核判断',list(NAMES),format_func=NAMES.get,index=None)
                    note=st.text_area('具体问题和期望行为',placeholder='例如：风险方向正确，但各技能引用的证据必须完整同步到汇总字段。')
                    rating=st.slider('对本次结果的评分',1,5,3)
                    consent=st.checkbox('允许将这封模拟开发集邮件和我的反馈用于提示词优化')
                    if st.form_submit_button('保存优化反馈'):
                        try:
                            lab.store_feedback(mail,context,row,verdict,note,rating,consent)
                            st.success('已保存人工反馈。');st.rerun()
                        except AnalysisError as e: st.error(str(e))
            else: st.info('该运行没有可复核的开发集请求。')
    with candidate_tab:
        feedback_source=st.selectbox('候选使用的反馈来源',['human_review','ai_review'],
            format_func=lambda value:'人工复核' if value=='human_review' else 'AI研发复核（不是人工奖励）')
        optimizer=st.selectbox('提示词优化模型',lab.PROVIDERS,index=1)
        st.caption('候选只读取所选来源中已授权的开发集反馈，以及既往候选在开发集的输出校验问题；不读取参考标签。两类反馈不混记。')
        if st.button('生成一个候选提示词',disabled=not lab.eligible_feedback(feedback_source)):
            try:
                with st.spinner('根据人工反馈生成候选…'):
                    candidate=lab.propose(optimizer,parent=lab.active_candidate(),feedback_source=feedback_source)
                st.success('候选已保存：'+candidate['id']);st.rerun()
            except AnalysisError as e: st.error(str(e))
        rounds=st.selectbox('自动迭代轮数',[1,2,3])
        cycle_limit=st.selectbox('每轮对照邮件数',[5,20,100])
        st.caption(f'每轮包含1次候选生成和最多{cycle_limit*4}次基础研判请求；重试/格式修复会增加调用。无改善或评测中断时停止，当前启用版保持不变。')
        if st.button('生成候选并自动运行双模型对照',disabled=not lab.eligible_feedback(feedback_source)):
            try:
                with st.spinner('运行有界提示词迭代；连续5分钟无新邮件结果会报错…'):
                    result=lab.cycle(optimizer,rounds,cycle_limit,feedback_source)
                st.json(result)
                if result['status'] in ('evaluation_incomplete','error'):
                    st.error('本次迭代未完成，已保存检查点和失败原因。')
            except AnalysisError as e: st.error(str(e))
        candidates=lab.list_candidates()
        if candidates:
            cid=st.selectbox('查看候选版本',[c['id'] for c in candidates])
            c=lab.load_candidate(cid)
            st.caption('候选反馈来源：'+', '.join(c['payload'].get('feedback_sources',['历史记录未标注'])))
            st.text_area('候选优化说明',c['payload']['instructions'],height=220,disabled=True)
            st.text(c['rationale']);st.write('可能的退化：',c['possible_regressions'])
            st.caption('父版：'+(c['payload']['parent'] or '原始 v3')+' · SHA256：'+c['sha256'])
    with evaluation_tab:
        candidates=lab.list_candidates()
        if candidates:
            cid=st.selectbox('待对照候选',[c['id'] for c in candidates])
            limit=st.selectbox('本次评测邮件数',[5,20,100],key='pair_limit')
            st.caption(f'基线与候选分别由两家模型分析相同邮件，最多{limit*4}次基础研判请求。5/20封用于检查运行情况，100封用于启用门槛。')
            if st.button('运行双模型A/B对照'):
                try:
                    for provider in lab.PROVIDERS:
                        with st.spinner(f'{provider} 同条件对照…'):
                            folder,report=lab.evaluate_pair(cid,provider,limit)
                        st.write(provider,report['changes'],report['stop_reason'] or '已完成')
                except AnalysisError as e: st.error(str(e))
            paths=sorted((lab.LAB/'evaluations').glob('*/report.json'),key=lambda p:p.stat().st_mtime,reverse=True)
            for path in paths:
                r=json.loads(path.read_text())
                if r['candidate_id']!=cid: continue
                with st.expander(f'{r["provider"]} · {r["attempted"]}/{r["expected"]} · {r["id"]}'):
                    keys=['accuracy','risk_recall','benign_false_positive_rate','coverage']
                    st.dataframe([{'提示词':side,**{k:r['reports'][side]['metrics'][k] for k in keys}} for side in ('baseline','candidate')])
                    st.json(r['changes']);st.caption(r['caveat'])
                    if r['stop_reason']: st.error(r['stop_reason'])
            status=lab.adoption_status(cid)
            if status['eligible']: st.success('两家均通过完整回归，至少一家的综合分提高，可人工选择启用。')
            else:
                for reason in status['reasons']: st.info(reason)
            with st.form('activate_prompt'):
                note=st.text_input('人工启用理由')
                if st.form_submit_button('启用这版提示词',disabled=not status['eligible']):
                    try: lab.activate(cid,note);st.rerun()
                    except AnalysisError as e: st.error(str(e))
        if lab.active_candidate():
            with st.form('rollback_prompt'):
                note=st.text_input('回退理由')
                if st.form_submit_button('回退到上一启用版本'):
                    try: lab.rollback(note);st.rerun()
                    except AnalysisError as e: st.error(str(e))

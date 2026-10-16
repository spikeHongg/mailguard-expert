import json
import streamlit as st
from mailguard.config import ROOT, configuration_status, provider_config, access_mode,provider_concurrency
from mailguard.parser import parse_mail
from mailguard.engine import analyze, request_for, digest
from mailguard.skills import skill_trace
from mailguard.evaluation import dataset, run_batch
from mailguard.imap_source import fetch_test_folder_batch
from mailguard.models import AnalysisError
from mailguard.prompt_lab import active_candidate, store_feedback

st.set_page_config(page_title='清邮 · 邮件风险研判', page_icon='✉️', layout='wide')
st.title('清邮 · 邮件风险研判')
st.caption('邮件解析 → 静态检查 → Skills 研判 → 证据核验 → 人工复核')
names = {'risk':'发现风险', 'clear':'未发现明显风险', 'review':'信息不足，需核实'}
categories = {'credential_phishing':'凭据钓鱼','payment_fraud':'付款诈骗','attachment_lure':'附件执行诱导',
              'impersonation_remote_control':'身份仿冒／远控诱导','link_qr_lure':'链接／扫码诱导',
              'data_exfiltration':'资料套取','extortion_callback':'勒索／回拨骗局','prompt_injection':'提示注入'}
with st.sidebar:
    st.header('运行方式')
    page = st.radio('页面', ['邮件研判','评测结果','提示词自迭代','迭代记录'])
    provider = st.selectbox('分析来源', ['replay','rules','deepseek','glm'],
        format_func=lambda p: {'replay':'助手研判回放','rules':'固定规则基线','deepseek':'DeepSeek 官方 API',
            'glm':'GLM Coding Plan 实测' if access_mode('glm',provider_config('glm'))=='coding_plan' else 'GLM 官方 API'}[p])
    version = st.selectbox('提示词版本', ['v6','v3','v5','v4','v2','v1'] if provider in ('deepseek','glm') else ['v3','v2','v1'],
        format_func=lambda v: {'v6':'v6 · HTML 表单与附件静态检查','v3':'v3 · Skills 分项检测','v5':'v5 · 原文编号引用（实验）',
                              'v4':'v4 · 输出证据编号（实验）'}.get(v,v+' · 历史提示词'))
    for p, ready in configuration_status().items():
        st.caption(f'{p.upper()}：'+('已配置' if ready else '待配置'))
    st.caption('配置保存在本机 .env；页面不显示密钥。')
    candidate_id=active_candidate() if version=='v3' and provider in ('deepseek','glm') else None
    if candidate_id: st.caption('当前优化提示词：'+candidate_id)

if page == '邮件研判':
    source = st.radio('邮件来源', ['模拟测试集','导入 EML','IMAP 测试文件夹'], horizontal=True)
    context = json.loads((ROOT/'data/testbench/context.json').read_text())
    mail = None
    if source == '模拟测试集':
        items = list(dataset('all',inspect_html=version=='v6'))
        choice = st.selectbox('选择邮件', range(len(items)), format_func=lambda i:f'{items[i][1].id} · {items[i][1].subject}')
        _, mail, context = items[choice]
        st.caption('合成样例 · 默认不显示参考标签')
    else:
        st.warning('外部邮件需配置业务背景；默认模拟企业规则不适用于你的真实邮件。')
        context_text = st.text_area('业务背景 JSON（内部域名、已知流程等）', value='{}')
        try:
            context = json.loads(context_text)
            if not isinstance(context, dict):
                raise ValueError()
        except (ValueError, TypeError):
            st.error('业务背景必须为 JSON 对象。')
            st.stop()
        if source == '导入 EML':
            uploaded = st.file_uploader('选择 .eml 文件（最多 5 MiB）', type=['eml'])
            if uploaded:
                try:
                    mail = parse_mail(uploaded.getvalue(),inspect_html=version=='v6')
                except AnalysisError as e:
                    st.error(str(e))
        else:
            if st.button('只读获取测试文件夹'):
                st.session_state.pop('imap_mails',None)
                st.session_state.pop('imap_errors',None)
                try:
                    batch=fetch_test_folder_batch(inspect_html=version=='v6')
                    st.session_state['imap_mails'] = batch['mails']
                    st.session_state['imap_errors'] = batch['errors']
                except AnalysisError as e:
                    st.error(str(e))
            mails = st.session_state.get('imap_mails', [])
            for error in st.session_state.get('imap_errors',[]):
                st.warning(f'{error["id"]}：解析失败，未进行风险判断。{error["error"]}')
            if mails:
                idx = st.selectbox('选择已获取邮件', range(len(mails)), format_func=lambda i:mails[i].subject)
                mail = mails[idx]
    if mail:
        left, right = st.columns([1.05, 1])
        with left:
            st.text(mail.subject or '无主题')
            st.text(f'发件人：{mail.sender}\n回复地址：{mail.reply_to or "未指定"}')
            st.text_area('邮件正文（纯文本）', mail.text or mail.html_text, height=270, disabled=True)
            with st.expander('链接、附件与隐藏文本'):
                st.json({'链接':mail.links,'附件':mail.attachments,'隐藏文本':mail.hidden_text})
            if version=='v6' and hasattr(mail,'html_inspections'):
                inspected=mail.html_inspections
                forms=sum(item.get('counts',{}).get('forms',0) for item in inspected)
                controls=sum(item.get('counts',{}).get('controls',0) for item in inspected)
                gaps=sum(item['status']!='inspected' for item in inspected)
                st.caption(f'静态检查范围：{len(inspected)} 个 HTML 来源，{forms} 个表单，{controls} 个输入控件。')
                if gaps:
                    st.info(f'{gaps} 个来源存在检查缺口，详见下方明细；检查缺口本身不是风险结论。')
                with st.expander('HTML 静态检查：表单、控件与外部资源'):
                    st.json(mail.html_inspections)
                    st.caption('状态表示检查覆盖；有输入框不等于有风险。空列表表示未发现可检查的 HTML MIME 部件。')
                st.caption('未执行脚本或访问链接；动态生成内容、其他附件类型和图片未检测。')
            else:
                st.caption('附件未扫描，图片未识别，链接未访问，身份未经认证。')
        try:
            request = request_for(mail, context, version,candidate_id)
        except AnalysisError as e:
            st.error(str(e))
            st.stop()
        identity = (digest(request), provider, source)
        with right:
            if version in ('v3','v4','v5','v6') and provider != 'rules':
                with st.expander('本次加载的风险检测 Skills'):
                    for skill in skill_trace(request):
                        st.text(f'{skill["title"]} · {skill["version"]} · {skill["sha256"][:8]}\n{skill["reason"]}')
            if provider == 'replay':
                st.info('展示当前助手已保存的研判；不会实时调用模型 API。')
            if provider in ('deepseek','glm'):
                st.caption('点击分析会把当前邮件文本、元数据及启用后的 HTML 静态检查摘要发送到所选模型服务。')
            if st.button('分析这封邮件', type='primary'):
                st.session_state.pop('analysis', None)
                try:
                    with st.spinner('正在处理…'):
                        result = analyze(mail, context, provider, version,candidate_id=candidate_id)
                    st.session_state['analysis'] = (identity, result)
                except AnalysisError as e:
                    st.error(str(e))
                    proposed=next((a['proposed_verdict'] for a in reversed(e.attempts) if a.get('proposed_verdict')),None)
                    if proposed in names:
                        st.warning(f'模型初步意见：{names[proposed]}；输出或证据未通过校验，需要复核，不能视为有效研判。')
            stored = st.session_state.get('analysis')
            if stored and stored[0] == identity:
                result = stored[1]
                a = result['assessment']
                {'risk':st.error, 'clear':st.success, 'review':st.warning}[a['verdict']](names[a['verdict']])
                if a['risk_types']:
                    st.text('风险类型：'+'、'.join(categories.get(t,t) for t in a['risk_types']))
                st.text(a['explanation'])
                if result.get('report_status')=='partial':
                    st.warning('核心判断与证据已校验；说明文字未完整生成，需人工补充。')
                for evidence in a['evidence']:
                    st.text(f'证据 [{evidence["field"]}]：{evidence["quote"]}')
                st.text('建议：'+a['recommendation'])
                st.text('；'.join(a['limitations']))
                st.caption(f'来源：{result["source"]} · 提示词：{version}')
                if result.get('access_mode'):
                    st.caption('接口通道：'+result['access_mode'])
                if a.get('skill_checks'):
                    with st.expander('Skills 分项检查结果', expanded=True):
                        title_by_id = {s['id']:s['title'] for s in result.get('skills_used',[])}
                        for check in a['skill_checks']:
                            status = {'finding':'发现风险','no_finding':'未发现明确风险','insufficient':'信息不足'}[check['outcome']]
                            st.text(f'{title_by_id.get(check["skill_id"],check["skill_id"])}：{status}\n{check["note"]}')
                review_key=digest(identity)[:16]
                with st.form('review-'+review_key):
                    decision = st.selectbox('人工复核结论', list(names), format_func=names.get,
                                            index=None,placeholder='请选择你的复核判断')
                    note = st.text_input('复核说明')
                    consent=st.checkbox('允许用于提示词优化（目前仅 v3 模拟开发集反馈会被采用）')
                    if st.form_submit_button('保存人工复核'):
                        try:
                            store_feedback(mail,context,result,decision,note,consent=consent)
                            st.success('人工复核已保存到本机。')
                        except AnalysisError as e: st.error(str(e))
                st.download_button('下载研判记录', json.dumps(result,ensure_ascii=False,indent=2), 'assessment.json')

elif page == '评测结果':
    st.subheader('固定样例评测')
    st.info('合成样例结果用于工程回归，不代表真实企业效果。回放耗时不代表模型推理速度。')
    cohort = st.selectbox('样本批次', ['all','legacy','extension'],
        format_func=lambda c:{'all':'全部样例','legacy':'原始 50 封（历史回归）','extension':'新增 50 封'}[c])
    counts = {s:len(list(dataset(s,cohort))) for s in ['dev','holdout','all']}
    st.caption('原始保留集已用于首版验证，现用于历史回归；新增保留集在 v3 技能冻结后评测。')
    split = st.selectbox('样本集合', ['dev','holdout','all'],
        format_func=lambda s:f'{dict(dev="开发集",holdout="保留集",all="全部")[s]} {counts[s]} 封')
    workers=st.number_input('批量并发数',min_value=1,max_value=20,value=provider_concurrency(provider),step=1,key='workers-'+provider)
    if st.button('运行批量评测'):
        with st.spinner('逐封运行并生成报告…'):
            path, run = run_batch(provider, version, split, 'UI', cohort,workers=workers)
        st.success(f'报告已保存：{path.name}')
    reports = sorted((ROOT/'reports').glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)
    if reports:
        path = st.selectbox('历史运行', reports, format_func=lambda p:json.loads(p.read_text())['label']+' · '+p.stem)
        run = json.loads(path.read_text())
        st.caption(f'{run["created_at"]} · {run["provider"]} · {run["prompt_version"]} · {run["split"]}')
        metrics = run['metrics']
        cols = st.columns(5)
        for col,key,title in zip(cols,['accuracy','risk_recall','risk_precision','benign_false_positive_rate','coverage'],['判断一致率','风险召回','风险精确率','正常误报率','有效完成率']):
            value=metrics[key]
            col.metric(title, '不适用' if value is None else f'{value:.1%}')
        st.write('混淆矩阵（行＝参考判断，列＝实际判断；error＝运行失败）')
        st.dataframe([{'参考判断':truth, **counts} for truth,counts in metrics['confusion_matrix'].items()], hide_index=True)
        st.write('错误与待改进样例')
        st.dataframe(metrics['issues'])
        st.download_button('下载完整评测报告', path.read_text(), path.name)
elif page == '提示词自迭代':
    from mailguard.prompt_lab_ui import render
    render()
else:
    st.subheader('每轮改动与新增问题')
    path = ROOT/'docs/iterations.md'
    if path.exists():
        st.markdown(path.read_text())
    else:
        st.info('尚未生成迭代记录。')

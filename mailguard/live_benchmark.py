"""Resumable real API evaluation of the fixed synthetic corpus; no credentials in artifacts."""
import argparse
import json
import os
import statistics
from collections import Counter
from pathlib import Path
from uuid import uuid4
from .config import ROOT, provider_config, LIVE_PROVIDERS, access_mode, progress_timeout_seconds,provider_concurrency
from .engine import analyze, digest, request_for, api_options
from .evaluation import dataset, labels, now, score, compare
from .models import AnalysisError
from .evaluation_run import EvaluationJob, EvaluationRun, StopPolicy, write_json

BASELINE = Path(os.getenv('MAILGUARD_BASELINE_FILE', str(ROOT/'data/private/baseline.json'))).expanduser()

def run_live(provider, run_dir=None, analyzer=analyze, limit=100, workers=None):
    workers=provider_concurrency(provider,workers)
    cfg=provider_config(provider)
    if not cfg['api_key'] or not cfg['model']:
        raise AnalysisError(f'{provider} 尚未填写 API Key 或模型名。',code='not_configured')
    baseline=json.loads(BASELINE.read_text())
    cases=list(dataset('all'))
    requests={m.id:request_for(m,c,'v3') for _,m,c in cases}
    baseline_rows={r['mail_id']:r for r in baseline['results']}
    if set(requests)!=set(baseline_rows) or any(digest(q)!=baseline_rows[mid]['request_hash'] for mid,q in requests.items()):
        raise AnalysisError('当前邮件/skills与指定v3回放基线不一致，不能作为同输入对照。')
    run_dir=Path(run_dir or ROOT/'reports/live'/f'{provider}-{uuid4().hex[:12]}')
    identity={'provider':provider,'model':cfg['model'],'base_url':cfg['base_url'],'generation_config':api_options(provider),
              'request_hashes':{mid:digest(q) for mid,q in requests.items()},'baseline_id':baseline['id']}
    jobs=[EvaluationJob(mail.id,requests[mail.id],(mail,context),{'provider':provider}) for _,mail,context in cases]
    def perform(job,cancel):
        mail,context=job.payload
        return analyzer(mail,context,provider,'v3',**({'cancel_event':cancel} if analyzer is analyze else {}))
    def completed(key,row,count):
        print(f'{provider}: {count}/100 {row["mail_id"]} {row["status"]}',flush=True)
    with EvaluationRun(jobs,folder=run_dir,identity=identity,run_id=uuid4().hex[:12]) as execution:
        if execution.complete_report is not None:
            return run_dir,execution.complete_report
        batch=execution.execute(perform,workers=workers,idle_seconds=progress_timeout_seconds(),limit=limit,
            policy=StopPolicy(smoke_check=True,stop_on_smoke_error=True,consecutive_errors=3),
            on_completed=completed,execution={'limit':limit,'progress_timeout_seconds':progress_timeout_seconds()})
        manifest=batch.manifest
        saved=list(batch.records.values())
        results=batch.results
        stop_reason=batch.stop_reason
        truth=labels()  # Never passed to analyzer/provider.
        indexed={r["mail_id"]:r for r in saved}
        mode=access_mode(provider,cfg)
        report={'id':manifest['id'],'label':f'{provider}-'+('Coding Plan' if mode=='coding_plan' else '正式API')+'实测100封','created_at':manifest['created_at'],
                'updated_at':now(),'provider':provider,'model_requested':cfg['model'],'endpoint':cfg['base_url'],
                'access_mode':mode,
                'executions':manifest['executions'],
                'prompt_version':'v3','split':'all','cohort':'all','generation_config':api_options(provider),'dataset_hash':baseline['dataset_hash'],
                'label_hash':digest({r['mail_id']:truth[r['mail_id']] for r in results}),
                'metrics':score(results,truth),'results':results,'attempted':len(saved),'finished':len(saved)==100,
                'stop_reason':stop_reason,
                'caveat':'真实API调用；失败和未调用分别记录且均保留在100封分母中。合成数据不能外推生产效果。'}
        report['vs_replay']=compare(baseline,report)
        attempted_ids=set(indexed)
        report['vs_replay']={k:[mid for mid in v if mid in attempted_ids] for k,v in report['vs_replay'].items()}
        report['not_attempted_ids']=[r['mail_id'] for r in results if r['source']=='not_attempted']
        report['verdict_disagreements']=[{'id':r['mail_id'],'replay':baseline_rows[r['mail_id']]['assessment']['verdict'],
            'live':r['assessment']['verdict'] if r['status']=='ok' else r.get('error_code','error')}
            for r in results if r['source']!='not_attempted' and (r['status']!='ok' or r['assessment']['verdict']!=baseline_rows[r['mail_id']]['assessment']['verdict'])]
        attempts=[a for r in saved for a in r.get('attempts',[])]
        elapsed=[r['elapsed_seconds'] for r in saved if r['status']=='ok']
        report['runtime']={'http_attempts':len(attempts),'successful_models':sorted({r['model'] for r in saved if r['status']=='ok'}),
            'prompt_tokens':sum((a.get('usage') or {}).get('prompt_tokens',0) or 0 for a in attempts),
            'completion_tokens':sum((a.get('usage') or {}).get('completion_tokens',0) or 0 for a in attempts),
            'repaired_messages':sum(any(a.get('validation') not in (None,'passed') for a in r.get('attempts',[])) for r in saved),
            'successful_mean_seconds':statistics.mean(elapsed) if elapsed else None}
        report=execution.save_report(report)
        # A stable id for this run, updated during resume. Case evidence is never overwritten.
        write_json(ROOT/'reports'/f'{manifest["id"]}.json',report)
        return run_dir,report

def write_comparison(report_paths, output):
    baseline=json.loads(BASELINE.read_text())
    runs=[json.loads(Path(p).read_text()) for p in report_paths]
    for run in runs: compare(baseline,run)
    all_runs=[baseline,*runs]
    lines=['# 真实API与回放对照','',f'生成时间：{now()}','',
        '固定100封合成邮件、v3提示和技能内容。回放仅为基线；未调用样例和失败均计入100封分母。','',
        '|来源|已调用/处理|有效完成|判断一致率|风险召回|正常误报|','|---|---:|---:|---:|---:|---:|']
    for r in all_runs:
        m=r['metrics']
        fmt=lambda v:'不适用' if v is None else f'{v:.1%}'
        lines.append(f'|{r["provider"]}|{r.get("attempted",100)}/100|{m["completed"]}/100|{fmt(m["accuracy"])}|{fmt(m["risk_recall"])}|{fmt(m["benign_false_positive_rate"])}|')
    for r in runs:
        errors=Counter(row.get('error_code','error') for row in r['results'] if row['status']!='ok' and row['source']!='not_attempted')
        validations=Counter(a['validation'] for row in r['results'] for a in row.get('attempts',[]) if a.get('validation') not in (None,'passed'))
        semantic=[d for d in r['verdict_disagreements'] if d['live'] in ('risk','clear','review')]
        review=r['metrics']['confusion_matrix']['review']
        lines += ['',f'## {r["provider"]}：实际模型与问题','',
                  f'请求模型：{r["model_requested"]}；实际成功模型：{", ".join(r["runtime"]["successful_models"]) or "无成功调用"}。',
                  f'调用来源：{r.get("access_mode","standard_api")}；端点：{r["endpoint"]}。',
                  f'运行记录：`reports/{r["id"]}.json`；已记录调用尝试：{r["runtime"]["http_attempts"]}。',
                  f'输入/输出tokens：{r["runtime"]["prompt_tokens"]}/{r["runtime"]["completion_tokens"]}；出现输出修复的邮件：{r["runtime"]["repaired_messages"]}。',
                  '上述用量只统计收到usage的响应；超时及中断请求的服务端用量未知。耗时包含重试和修复，不等于单次模型推理耗时。',
                  f'运行失败分类：{dict(errors)}；校验失败原因（按响应次数）：{dict(validations)}。',
                  f'有效结果与回放的判断分歧：{len(semantic)}封；输出被拒绝或网络失败不视为已获得模型分类结论。',
                  f'信息不足样例：正确复核 {review["review"]}/10，判风险 {review["risk"]}，判未发现明显风险 {review["clear"]}，失败或未调用 {review["error"]}。',
                  f'相对回放：修复 {r["vs_replay"]["fixed"]}；新增不一致 {r["vs_replay"]["regressed"]}；仍未解决 {r["vs_replay"]["remaining"]}。','']
        lines += ['混淆矩阵（行为参考标签，列为输出判断；error包含失败及未调用）：','',
                  '|参考标签|risk|clear|review|error|','|---|---:|---:|---:|---:|']
        for state,counts in r['metrics']['confusion_matrix'].items():
            lines.append(f'|{state}|{counts["risk"]}|{counts["clear"]}|{counts["review"]}|{counts["error"]}|')
        lines.append('')
        for d in r['verdict_disagreements']:
            lines.append(f'- {d["id"]}：回放 {d["replay"]} → 实时 {d["live"]}。')
        if not r['finished']: lines.append('- 本轮尚未完成100封，不能视为完整实测。')
        if r.get('stop_reason')=='progress_timeout': lines.append('- 停止原因：达到所设无进展时限仍没有新邮件结果落盘，按用户要求报错并取消在途请求；不是单请求总时限。')
        if r.get('not_attempted_ids'): lines.append(f'- 尚未调用 {len(r["not_attempted_ids"])} 封，不把这些样例记作模型新增退化。')
    if len(runs)==2:
        by=[{r['mail_id']:r for r in run['results']} for run in runs]
        common=[mid for mid in by[0] if by[0][mid]['status']=='ok' and by[1][mid]['status']=='ok']
        diffs=[mid for mid in common if by[0][mid]['assessment']['verdict']!=by[1][mid]['assessment']['verdict']]
        lines += ['', '## 两家实时判断差异','',f'仅比较两家均有有效输出的 {len(common)} 封；分歧 {len(diffs)} 封。',', '.join(diffs) or '共同有效输出中没有判断差异。']
    lines += ['', '## 解释边界','',
              '- 判断一致率以100封全部样例为分母；风险召回以60封风险样例为分母。失败没有从分母剔除。',
              '- 正常误报率为30封正常样例中有效输出为risk的比例；失败并不代表正确识别为正常，需同时阅读完成率和混淆矩阵。',
              '- 助手回放来自参与研发的助手；这是合成样例上的工程对照，不能当作独立生产盲测或模型能力排行榜。',
              '- 本轮保持原提示、技能、标签和证据校验不变；后续修正应建立新轮次。']
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text('\n'.join(lines)+'\n')

def main():
    p=argparse.ArgumentParser(description='真实API评测：首封验证、逐封检查点、同输入回放对照')
    sub=p.add_subparsers(dest='command',required=True)
    r=sub.add_parser('run');r.add_argument('--provider',choices=LIVE_PROVIDERS,required=True)
    r.add_argument('--resume');r.add_argument('--limit',type=int,default=100,choices=range(1,101),metavar='1..100')
    r.add_argument('--workers',type=int,choices=range(1,21))
    c=sub.add_parser('compare');c.add_argument('reports',nargs='+');c.add_argument('--output',default='docs/live-comparison.md')
    a=p.parse_args()
    try:
        if a.command=='run':
            folder,report=run_live(a.provider,a.resume,limit=a.limit,workers=a.workers)
            print(json.dumps({'run_dir':str(folder),'attempted':report['attempted'],'finished':report['finished']},ensure_ascii=False))
            if report.get('stop_reason')=='progress_timeout':
                p.exit(2,'评测进度超时：已保存检查点和失败报告。\n')
        else: write_comparison(a.reports,a.output)
    except AnalysisError as e:
        p.exit(2,str(e)+'\n')

if __name__=='__main__': main()

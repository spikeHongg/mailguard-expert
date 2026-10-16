"""Small, fixed synthetic cohort; measure real request overlap without hiding limits."""
import argparse
import json
import math
import time
from collections import Counter

from .config import ROOT,provider_config,provider_concurrency,progress_timeout_seconds
from .contracts import request_for,digest
from .engine import api_assess,api_options
from .evaluation import dataset,labels,score,now
from .concurrency import InFlightMeter
from .evaluation_run import EvaluationJob, EvaluationRun, Produced, StopPolicy, write_json
from .models import AnalysisError

RUNS=ROOT/'data/private/concurrency'


def percentile(values,fraction):
    return round(sorted(values)[max(0,math.ceil(len(values)*fraction)-1)],3)if values else None


def run(provider,name,levels,samples=20):
    cfg=provider_config(provider)
    if not cfg['api_key']or not cfg['model']:raise AnalysisError('模型与密钥待配置。',code='not_configured')
    levels=[provider_concurrency(provider,x)for x in levels]
    if len(set(levels))!=len(levels)or levels!=sorted(levels):raise ValueError('Levels must be unique and ascending')
    data=list(dataset('dev',inspect_html=True))[:samples]
    if len(data)!=samples or samples<max(levels):raise ValueError('Cohort must cover the largest concurrency level')
    folder=RUNS/name
    requests={mail.id:request_for(mail,context,'v6')for _,mail,context in data}
    identity={'provider':provider,'model_requested':cfg['model'],'endpoint':cfg['base_url'],'levels':levels,
        'generation_config':api_options(provider,'v6'),'samples':samples,'prompt_version':'v6',
        'request_hashes':{key:digest(req)for key,req in requests.items()},'transport_retries':0,
        'progress_timeout_seconds':progress_timeout_seconds()}
    manifest_path=folder/'manifest.json'
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text())
        if manifest['identity']!=identity:raise ValueError('Run identity changed; choose a new name')
        if (folder/'summary.json').exists():raise ValueError('Completed profile is immutable')
    else:
        manifest={'id':name,'created_at':now(),'identity':identity}
        write_json(manifest_path,manifest)
        for mid,request in requests.items():write_json(folder/'requests'/f'{mid}.json',request)
    reports=[];fatal=None
    for workers in levels:
        stage=folder/f'c{workers}'
        if (stage/'report.json').exists():
            report=json.loads((stage/'report.json').read_text());reports.append(report)
            if report.get('stop_reason'):fatal=report['stop_reason'];break
            continue
        meter=InFlightMeter();started_at=now();start=time.perf_counter()
        jobs=[EvaluationJob(mid,request,metadata={'provider':provider})for mid,request in requests.items()]
        def execute(job,cancel):
            responses=[]
            try:
                assessment,meta=api_assess(job.request,provider,cancel_event=cancel,response_sink=responses.append,
                                           observer=meter,max_transport_retries=0)
                return Produced({'status':'ok','source':'live_api','assessment':assessment.model_dump(),**meta},
                                {'responses':responses})
            except AnalysisError as exc:
                return Produced(error=exc,artifacts={'responses':responses})
        def completed(key,row,count):
            if count%5==0 or count==samples:
                print(f'{provider} c{workers}: {count}/{samples} terminal results',flush=True)
        with EvaluationRun(jobs,folder=stage,identity=identity,run_id=f'{name}-c{workers}',legacy_manifest=manifest) as execution:
            initial_keys=set(execution.records)
            resumed=bool(initial_keys)
            print(f'{provider}: starting {workers} concurrent tasks, {samples-len(execution.records)} pending synthetic mails',flush=True)
            outcome=execution.execute(execute,workers=workers,idle_seconds=progress_timeout_seconds(),on_completed=completed,
                                      policy=StopPolicy(nonfatal_provider_codes=frozenset({'1302','1305'})))
            elapsed=time.perf_counter()-start
            rows=outcome.results
            truth=labels()  # Never supplied to request construction or model execution.
            metrics=score(rows,truth)
            attempts=[a for row in rows for a in row.get('attempts',[])]
            latency=[r['elapsed_seconds']for r in rows if r.get('elapsed_seconds')is not None]
            rate=[a for a in attempts if a.get('http_status')==429 or a.get('provider_error_code')=='1302']
            overload=[a for a in attempts if a.get('provider_error_code')=='1305']
            report={'provider':provider,'workers':workers,'samples':samples,'started_at':started_at,'finished_at':now(),
                'finished':outcome.finished,'stop_reason':outcome.stop_reason,'models_returned':sorted({r['model']for r in rows if r['status']=='ok'}),
                'client_peak_inflight':meter.peak,'measured_this_process':outcome.newly_attempted,'resumed':resumed,
                'target_overlap_observed':meter.peak>=workers,'elapsed_seconds':round(elapsed,3),
                'mails_per_minute':round(outcome.newly_attempted*60/elapsed,2)if elapsed else None,
                'valid_mails_per_minute':round(sum(r['status']=='ok'for key,r in outcome.records.items()if key not in initial_keys)*60/elapsed,2)if elapsed else None,
                'mail_p50_seconds':percentile(latency,.5),'mail_p95_seconds':percentile(latency,.95),
                'request_p50_seconds':percentile([x['seconds']for x in meter.calls],.5),
                'request_p95_seconds':percentile([x['seconds']for x in meter.calls],.95),
                'request_count':len(meter.calls),'rate_limit_attempts':len(rate),'overload_attempts':len(overload),
                'transport_errors':sum(a.get('status')=='transport_error'for a in attempts),
                'http_errors':sum(a.get('status')=='http_error'for a in attempts),
                'http_status_counts':dict(Counter(str(a['http_status'])for a in attempts if a.get('http_status'))),
                'provider_error_codes':dict(Counter(a['provider_error_code']for a in attempts if a.get('provider_error_code'))),
                'format_repairs':sum(any(a.get('validation')not in(None,'passed')for a in r.get('attempts',[]))for r in rows),
                'first_attempt_valid':sum(r['status']=='ok' and len(r.get('attempts',[]))==1 for r in rows),
                'prompt_tokens':sum((a.get('usage')or{}).get('prompt_tokens',0)or 0 for a in attempts),
                'completion_tokens':sum((a.get('usage')or{}).get('completion_tokens',0)or 0 for a in attempts),
                'metrics':metrics,'results':rows,
                'caveat':'固定合成开发样例的有限负载测量；并发为客户端在途调用，不能据此推定服务器容量或持续稳定性；非流式，未测首token时延。'}
            write_json(stage/'http-timings.json',meter.calls);report=execution.save_report(report)
        reports.append(report)
        print(json.dumps({k:report[k]for k in ('provider','workers','client_peak_inflight','request_count','rate_limit_attempts','overload_attempts','http_errors','transport_errors','format_repairs','elapsed_seconds')},ensure_ascii=False),flush=True)
        if outcome.stop_reason:
            fatal=outcome.stop_reason;break
    eligible=[r for r in reports if r['finished']and r['metrics']['completed']==samples and r['target_overlap_observed']
              and not r['resumed']and not(r['rate_limit_attempts']or r['http_errors']or r['transport_errors'])]
    summary={'id':name,'provider':provider,'model_requested':cfg['model'],'endpoint':cfg['base_url'],
             'created_at':manifest['created_at'],'finished_at':now(),'stages':reports,
             'highest_clean_observed':max((r['workers']for r in eligible),default=None),
             'all_levels_attempted':len(reports)==len(levels),'stop_reason':fatal}
    write_json(folder/'summary.json',summary)
    print(json.dumps({k:v for k,v in summary.items()if k!='stages'},ensure_ascii=False),flush=True)
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--provider',choices=['deepseek','glm'],required=True)
    p.add_argument('--name',required=True)
    p.add_argument('--levels',type=int,nargs='+',default=[1,2,5,10,20])
    p.add_argument('--samples',type=int,default=20)
    a=p.parse_args();result=run(a.provider,a.name,a.levels,a.samples)
    if result['stop_reason']:raise SystemExit(2)

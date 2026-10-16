"""Frozen public corpus real API runs with append-only cases and a progress watchdog."""
import argparse
import json
from collections import Counter
from pathlib import Path

from .config import ROOT, provider_config, progress_timeout_seconds,provider_concurrency
from .engine import request_for, digest, api_assess, api_options
from .evaluation import now, score, compare, dataset, labels as synthetic_labels
from .evaluation_run import EvaluationJob, EvaluationRun, Produced, StopPolicy
from .models import AnalysisError
from .public_corpus import DEST
from .frozen_corpus import FrozenCorpus
from .skills import skill_trace
from .concurrency import InFlightMeter
from .contracts import guided_request

RUNS = ROOT/'data/private/public_benchmark/runs'


def candidate_request(mail, context, guidance=None, version='v4'):
    return guided_request(mail, context, guidance, version)


def run(name, split='dev', guidance_file=None, corpus='public', workers=None, previous=None, base=DEST, version='v4',provider='glm'):
    allowed_splits={'public':('dev','test'),'synthetic':('dev','holdout','all')}
    if corpus not in allowed_splits or split not in allowed_splits[corpus]:
        raise ValueError('Split does not belong to this corpus')
    folder = RUNS/name
    config = provider_config(provider)
    workers=provider_concurrency(provider,workers)
    if not config['model'] or not config['api_key']:
        raise AnalysisError('请先配置本轮模型及密钥。')
    guidance = Path(guidance_file).read_text() if guidance_file else None
    frozen=FrozenCorpus(base) if corpus=='public' else None
    data = list(frozen.cases(split)) if frozen else list(dataset(split,inspect_html=version=='v6'))
    if not data:raise ValueError('Evaluation corpus is empty')
    requests = {m.id:candidate_request(m,c,guidance,version) for _,m,c in data}
    freeze=frozen.metadata if frozen else {}
    if frozen:
        frozen.validate_requests(requests)
    identity = {'model':config['model'], 'endpoint':config['base_url'], 'corpus':corpus, 'split':split,
                'generation':api_options(provider,version), 'request_hashes':{k:digest(v) for k,v in requests.items()},
                'guidance':guidance, 'progress_timeout_seconds':progress_timeout_seconds()}
    jobs=[EvaluationJob(mid,request,metadata={'provider':provider,'skills_used':skill_trace(request)}) for mid,request in requests.items()]
    meter=InFlightMeter()
    def perform(job,cancel):
        responses=[]
        try:
            assessment,meta=api_assess(job.request,provider,cancel_event=cancel,response_sink=responses.append,observer=meter)
            result={'status':'ok','source':'live_api','assessment':assessment.model_dump(),**meta}
            return Produced(result,{'responses':responses})
        except AnalysisError as exc:
            return Produced(error=exc,artifacts={'responses':responses})
    def completed(key,row,count):
        print(f'{name}: {count}/{len(requests)} {row["mail_id"]} {row["status"]}',flush=True)
    with EvaluationRun(jobs,folder=folder,identity=identity,run_id=name) as execution:
        batch=execution.execute(perform,workers=workers,idle_seconds=progress_timeout_seconds(),
            policy=StopPolicy(smoke_check=True,consecutive_errors=3,reject_finished=True),
            on_completed=completed,execution={'provider':provider})
        manifest=batch.manifest
        saved=batch.records
        results=batch.results
        stop_reason=batch.stop_reason
        # Truth is loaded only after inference. It never enters request construction.
        truth=frozen.scoring_labels() if frozen else synthetic_labels()
        metrics=score(results,truth)
        attempts=[a for row in results for a in row.get('attempts',[])]
        report={'id':name,'created_at':manifest['created_at'],'updated_at':now(),'corpus':corpus,'split':split,
                'corpus_version':Path(base).name if corpus=='public' else 'synthetic-v2',
                'evaluation_role':freeze.get('evaluation_roles',{}).get(split,split) if corpus=='public' else 'synthetic_development',
                'model_requested':config['model'],'models_returned':sorted({r['model'] for r in results if r['status']=='ok'}),
                'prompt_version':version,'guidance_hash':digest(guidance), 'provider':provider,'workers':workers,
                'executions':manifest['executions'],
                'dataset_hash':digest([(m.id,m.content_hash) for _,m,_ in data]),
                'label_hash':digest({mid:truth[mid] for mid in requests}),
                'metrics':metrics,'results':results,'finished':len(saved)==len(requests), 'stop_reason':stop_reason,
                'meets_point_targets':stop_reason is None and len(saved)==len(requests) and metrics['risk_recall'] is not None and
                   metrics['risk_recall']>=.90 and metrics['risk_precision'] is not None and metrics['risk_precision']>=.90,
                'runtime':{'client_peak_inflight':meter.peak,'responses':sum(a.get('status')=='response' for a in attempts),
                           'prompt_tokens':sum((a.get('usage') or {}).get('prompt_tokens',0) or 0 for a in attempts),
                           'completion_tokens':sum((a.get('usage') or {}).get('completion_tokens',0) or 0 for a in attempts),
                           'validation_failures':dict(Counter(a['validation'] for a in attempts if a.get('validation') not in (None,'passed')))},
                'caveat':freeze.get('caveat','公开来源标签对照，非逐封人工鉴定；源间年代偏差、基础模型训练污染未知；review与错误均不算风险召回。') if corpus=='public' else '合成开发样例，非独立盲测。'}
        if previous:
            report['comparison']=compare(json.loads(Path(previous).read_text()),report)
        report=execution.save_report(report)
        print(json.dumps({k:v for k,v in report.items() if k not in ('results',)},ensure_ascii=False),flush=True)
        return report


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--name',required=True)
    p.add_argument('--split',choices=['dev','test','holdout','all'],default='dev')
    p.add_argument('--provider',choices=['glm','deepseek'],default='glm')
    p.add_argument('--guidance')
    p.add_argument('--corpus',choices=['public','synthetic'],default='public')
    p.add_argument('--previous')
    p.add_argument('--workers',type=int,choices=range(1,21))
    p.add_argument('--base',type=Path,default=DEST)
    p.add_argument('--version',choices=['v4','v5','v6'],default='v4')
    a=p.parse_args()
    report=run(a.name,a.split,a.guidance,a.corpus,a.workers,a.previous,a.base,a.version,a.provider)
    if report['stop_reason']:
        raise SystemExit(2)

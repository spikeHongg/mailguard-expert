import json
from uuid import uuid4
from .config import ROOT,provider_concurrency,progress_timeout_seconds,LIVE_PROVIDERS
from .evaluation_run import EvaluationJob, EvaluationRun, write_json, timestamp as now
from .parser import parse_mail
from .engine import analyze, digest

def dataset(split='all', cohort='all', inspect_html=False):
    base = ROOT / 'data/testbench'
    manifest = json.loads((base / 'manifest.json').read_text())
    for item in manifest:
        is_legacy = int(item['id'][1:]) <= 50
        if (cohort == 'legacy' and not is_legacy) or (cohort == 'extension' and is_legacy):
            continue
        if split == 'all' or item['split'] == split:
            context = json.loads((base / item.get('context_file', 'context.json')).read_text())
            yield item, parse_mail((base / item['filename']).read_bytes(), item['id'],inspect_html=inspect_html), context

def labels():
    return {r['id']: r for r in json.loads((ROOT/'data/testbench/labels.json').read_text())}

def score(rows, truth):
    states = ['risk', 'clear', 'review']
    matrix = {t: {p: 0 for p in states + ['error']} for t in states}
    by_id = {r['mail_id']: r for r in rows}
    if len(by_id) != len(rows):
        raise ValueError('重复评测ID')
    errors, correct, tp, fp, fn, benign_fp, benign_n, risk_n = 0, 0, 0, 0, 0, 0, 0, 0
    cases = []
    for mid, r in by_id.items():
        t = truth[mid]['expected']
        p = r['assessment']['verdict'] if r['status'] == 'ok' else 'error'
        matrix[t][p] += 1
        errors += p == 'error'
        correct += p == t
        tp += t == 'risk' and p == 'risk'
        fp += t != 'risk' and p == 'risk'
        fn += t == 'risk' and p != 'risk'
        benign_n += t == 'clear'
        risk_n += t == 'risk'
        benign_fp += t == 'clear' and p == 'risk'
        if t != p:
            cases.append({'id': mid, 'expected': t, 'predicted': p})
    div = lambda n, d: round(n/d, 4) if d else None
    return {'count': len(rows), 'completed': len(rows)-errors, 'errors': errors,
            'coverage': div(len(rows)-errors, len(rows)), 'accuracy': div(correct, len(rows)),
            'risk_recall': div(tp, tp+fn), 'risk_precision': div(tp, tp+fp),
            'benign_false_positive_rate': div(benign_fp, benign_n),
            'tp':tp, 'fp':fp, 'fn':fn, 'risk_count':risk_n, 'benign_count':benign_n,
            'confusion_matrix': matrix, 'issues': cases}

def run_batch(provider='replay', version='v2', split='dev', label='run', cohort='all',workers=None):
    workers=provider_concurrency(provider,workers)
    cases=list(dataset(split,cohort,inspect_html=version=='v6'))
    inputs=[{'item':item,'hash':mail.content_hash}for item,mail,_ in cases]
    from .contracts import request_for
    jobs=[EvaluationJob(mail.id,request_for(mail,context,version),(mail,context),{'provider':provider})
          for _,mail,context in cases]
    def perform(job,cancel):
        mail,context=job.payload
        return analyze(mail,context,provider,version,**({'cancel_event':cancel}if provider in LIVE_PROVIDERS else{}))
    with EvaluationRun(jobs) as execution:
        batch=execution.execute(perform,workers=workers,idle_seconds=progress_timeout_seconds())
    rows=batch.results
    truth = labels()  # Loaded only after inference, never passed to analyze.
    run = {'id': uuid4().hex[:12], 'label': label, 'created_at':now(), 'provider':provider,
           'prompt_version':version, 'split':split, 'cohort':cohort, 'dataset_hash':digest(inputs),
           'label_hash':digest({r['mail_id']:truth[r['mail_id']] for r in rows}),
           'metrics':score(rows, truth), 'results':rows,
           'workers':workers,'peak_tasks':batch.peak_tasks,'stop_reason':batch.stop_reason,
           'caveat':'自编合成样例工程回归；不能外推企业效果。' +
                    ('助手会话研判回放，非 API 实测；助手参与研发存在偏差。' if provider == 'replay' else
                     '固定规则基线，未调用模型。' if provider == 'rules' else
                     '实时 API 评测；成功数与失败数分别记录，仅成功条目证明该次调用完成。')}
    path = ROOT / 'reports' / f'{run["id"]}.json'
    write_json(path,run)
    return path, run

def compare(before, after):
    if before['dataset_hash'] != after['dataset_hash'] or before['label_hash'] != after['label_hash']:
        raise ValueError('数据集或标签不一致，不能直接比较')
    b = {c['id'] for c in before['metrics']['issues']}
    a = {c['id'] for c in after['metrics']['issues']}
    return {'fixed':sorted(b-a), 'regressed':sorted(a-b), 'remaining':sorted(a & b)}

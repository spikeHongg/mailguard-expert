import argparse
import json
from pathlib import Path
from .config import ROOT, LIVE_PROVIDERS
from .contracts import request_for, PROMPTS, make_replay_record
from .evaluation import dataset, run_batch, now, compare

def main():
    p = argparse.ArgumentParser(description='邮件研判与可复现评测')
    sub = p.add_subparsers(dest='command', required=True)
    e = sub.add_parser('export', help='导出不含标签的研判请求')
    e.add_argument('--version', choices=list(PROMPTS), default='v3')
    e.add_argument('--split', choices=['dev','holdout','all'], default='dev')
    e.add_argument('--output', required=True)
    e.add_argument('--cohort', choices=['all','legacy','extension'], default='all')
    i = sub.add_parser('import', help='导入当前助手的真实研判结果')
    i.add_argument('--requests', required=True)
    i.add_argument('--responses', required=True)
    i.add_argument('--reviewer-note', default='当前会话研发研判，不是外部API调用或独立盲测')
    r = sub.add_parser('evaluate')
    r.add_argument('--provider', choices=['rules','replay',*LIVE_PROVIDERS], default='replay')
    r.add_argument('--version', choices=list(PROMPTS), default='v3')
    r.add_argument('--split', choices=['dev','holdout','all'], default='dev')
    r.add_argument('--label', default='run')
    r.add_argument('--cohort', choices=['all','legacy','extension'], default='all')
    r.add_argument('--workers',type=int,choices=range(1,21),help='并发1–20；省略则读取模型的CONCURRENCY配置')
    c = sub.add_parser('compare')
    c.add_argument('before')
    c.add_argument('after')
    a = p.parse_args()
    if a.command == 'export':
        records = [{'id':mail.id, 'request':request_for(mail, context, a.version)}
                   for _, mail, context in dataset(a.split, a.cohort,inspect_html=a.version=='v6')]
        Path(a.output).write_text(json.dumps(records, ensure_ascii=False, indent=2))
        print(f'已导出 {len(records)} 条无标签请求：{a.output}')
    elif a.command == 'import':
        requests = json.loads(Path(a.requests).read_text())
        responses = json.loads(Path(a.responses).read_text())
        by_id = {r['id']:r for r in responses}
        if len(by_id) != len(responses) or set(by_id) != {r['id'] for r in requests}:
            raise ValueError('响应ID必须与请求完整且唯一对应')
        records = []
        for req in requests:
            response = by_id[req['id']]
            request = req['request']
            record=make_replay_record(response['assessment'],request,recorded_at=now(),provenance=a.reviewer_note)
            records.append((record['request_hash'],record))
        dest = ROOT / 'data/replays'
        dest.mkdir(parents=True, exist_ok=True)
        for key, record in records:
            path = dest / f'{key}.json'
            if path.exists():
                old = json.loads(path.read_text())
                if old['assessment'] != record['assessment']:
                    raise ValueError('既有回放不可覆盖；请升级提示词版本后重新导出')
        for key, record in records:
            path = dest / f'{key}.json'
            if not path.exists():
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        print(f'已校验并导入 {len(records)} 条助手研判回放。')
    elif a.command == 'evaluate':
        path, run = run_batch(a.provider, a.version, a.split, a.label, a.cohort,workers=a.workers)
        print(json.dumps({'report':str(path), 'metrics':run['metrics']}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(compare(json.loads(Path(a.before).read_text()),json.loads(Path(a.after).read_text())),ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()

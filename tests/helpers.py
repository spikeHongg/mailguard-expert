"""Fresh contract fixtures; no internal experiment records are test dependencies."""
import json
from mailguard.contracts import make_replay_record, request_for, digest
from mailguard.engine import analyze
from mailguard.evaluation import dataset


def replay_fixture(folder):
    folder.mkdir(parents=True, exist_ok=True)
    rows = {}
    for _, mail, context in dataset('all'):
        request = request_for(mail, context, 'v3')
        response = {'verdict': 'clear', 'risk_types': [], 'evidence': [],
                    'explanation': '工作流契约测试夹具，不是邮件安全判断。',
                    'recommendation': '人工核实。', 'limitations': ['固定测试夹具。'],
                    'skill_checks': [{'skill_id': s['id'], 'outcome': 'no_finding',
                                      'evidence': [], 'note': '固定测试夹具。'} for s in request['skills']]}
        record = make_replay_record(response, request, recorded_at='test_fixture', provenance='test_fixture')
        (folder/f'{digest(request)}.json').write_text(json.dumps(record), encoding='utf-8')
        rows[mail.id] = analyze(mail, context, 'replay', 'v3', replay_dir=folder)
    return rows

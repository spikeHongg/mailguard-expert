"""Host-agent and live-API entry points sharing parsing, prompts and validation."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Event, Thread
import time
from uuid import uuid4

from .config import ROOT, configuration_status, progress_timeout_seconds
from .contracts import canonical, decode_response, digest, guided_request, response_report_status
from .engine import api_assess
from .models import AnalysisError
from .parser import parse_mail
from .progress import ProgressDeadline
from .skills import skill_trace

PROFILES = ('v6-c1', 'v6')
SAMPLES = ('m002', 'm026')
MAX_MAIL_BYTES = 5 * 1024 * 1024


def asset(kind, name):
    """Checkout source is authoritative; wheels carry the same allowlisted bytes."""
    if kind == 'sample' and name in SAMPLES:
        source, packaged = ROOT/'data/testbench'/f'{name}.eml', f'samples/{name}.eml'
    elif kind == 'guidance' and name == 'v6-c1':
        source, packaged = ROOT/'policies/expert-guidance.txt', 'guidance.txt'
    else:
        raise AnalysisError('未知内置资源。', code='invalid_source')
    return source if source.is_file() else Path(__file__).parent/'_assets'/packaged


def state_directory():
    configured = os.getenv('MAILGUARD_STATE_DIR')
    if configured:
        return Path(configured).expanduser().resolve()
    if (ROOT/'AGENTS.md').is_file():
        return ROOT/'data/private/expert'
    return Path.home()/'.local/share/mailguard'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def private_directory(path):
    """Avoid redundant mkdir calls in host sandboxes; tolerate a creation race."""
    path = Path(path)
    if not path.is_dir():
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            if not path.is_dir():
                raise
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
    return path


def _job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise AnalysisError('研判任务标识无效。', code='invalid_job')
    return value


class Expert:
    def __init__(self, state=None):
        self.state = Path(state or state_directory()).expanduser().resolve()
        private_directory(self.state)
        self.db = self.state/'expert.sqlite3'
        # Pre-create with private permissions before SQLite opens the file.
        fd = os.open(self.db, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        if self.db.stat().st_mode & 0o077:
            self.db.chmod(0o600)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, request_hash TEXT NOT NULL,
                    origin TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'prepared', result TEXT, response_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    job_id TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    job_id TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL
                );
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def prepare(self, *, raw=None, sample=None, context=None, profile='v6-c1', mail=None, origin='eml'):
        if profile not in PROFILES:
            raise AnalysisError('未知专家提示词配置。', code='invalid_profile')
        if sum(x is not None for x in (raw, sample, mail)) != 1:
            raise AnalysisError('每次选择一个邮件来源。', code='invalid_source')
        if sample is not None:
            raw, origin = asset('sample', sample).read_bytes(), 'synthetic_sample'
        if mail is None:
            mail = parse_mail(raw, 'mail-'+hashlib.sha256(raw).hexdigest()[:16], inspect_html=True)
        guidance = asset('guidance', profile).read_text(encoding='utf-8') if profile == 'v6-c1' else None
        request = guided_request(mail, context or {}, guidance, 'v6')
        job_id = uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO jobs(id,request,request_hash,origin,created_at) VALUES(?,?,?,?,?)',
                       (job_id, canonical(request), digest(request), origin, now()))
        return self.get(job_id, include_request=True)

    def get(self, job_id, *, include_request=False):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (_job_id(job_id),)).fetchone()
            if row is None:
                raise AnalysisError('研判任务不存在。', code='job_not_found')
            reviews = db.execute('SELECT recorded_at,payload FROM reviews WHERE job_id=? ORDER BY rowid', (job_id,)).fetchall()
            attempts = db.execute('SELECT COUNT(*) FROM attempts WHERE job_id=?', (job_id,)).fetchone()[0]
        request = json.loads(row['request'])
        if digest(request) != row['request_hash']:
            raise AnalysisError('研判请求快照已改变。', code='request_changed')
        value = {k: row[k] for k in ('id', 'request_hash', 'origin', 'created_at', 'status')}
        value['attempt_records'] = attempts
        value['result'] = json.loads(row['result']) if row['result'] else None
        value['reviews'] = [{'recorded_at': r['recorded_at'], **json.loads(r['payload'])} for r in reviews]
        if include_request:
            value['request'] = request
        return value

    def record_attempt(self, job_id, payload):
        with self.connect() as db:
            db.execute('INSERT INTO attempts VALUES(?,?,?)', (_job_id(job_id), now(), canonical(payload)))

    def submit(self, job_id, response, *, host='workbuddy', model='not_reported', request_hash=None):
        job = self.get(job_id, include_request=True)
        if request_hash != job['request_hash']:
            raise AnalysisError('提交结果必须引用准备阶段返回的完整request_hash。', code='request_mismatch')
        if not all(isinstance(v, str) and 0 < len(v) <= 100 for v in (host, model)):
            raise AnalysisError('宿主与模型声明须为1至100字文本。', code='invalid_source')
        response_hash = digest({'response': response, 'host': host, 'model': model})
        try:
            assessment = decode_response(response, job['request'])
        except (ValueError, TypeError, KeyError):
            self.record_attempt(job_id, {'status': 'invalid_output', 'source': 'host_agent',
                                        'response': response})
            raise AnalysisError('宿主输出未通过结构、证据或结论一致性校验；请修正后重新提交。', code='invalid_output') from None
        result = {'status': 'ok', 'source': 'host_agent', 'host': host,
                  'model_declared': model, 'model_verified': False,
                  'request_hash': job['request_hash'], 'assessment': assessment.model_dump(),
                  'decision_status': 'validated', 'evidence_status': 'verified',
                  'report_status': response_report_status(response, job['request']),
                  'skills_used': skill_trace(job['request']), 'recorded_at': now()}
        with self.connect() as db:
            updated = db.execute("UPDATE jobs SET status='ok',result=?,response_hash=? WHERE id=? AND status='prepared'",
                                 (canonical(result), response_hash, job_id)).rowcount
            if not updated:
                row = db.execute('SELECT response_hash FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row[0] != response_hash:
                    raise AnalysisError('任务已运行或已有结果；重新研判请准备新任务。', code='job_conflict')
        return self.get(job_id)

    def analyze(self, job_id, provider, *, assessor=api_assess, idle_seconds=None):
        if provider not in ('deepseek', 'glm', 'gpt'):
            raise AnalysisError('请选择已支持的实时模型。', code='invalid_provider')
        job = self.get(job_id, include_request=True)
        seconds = progress_timeout_seconds() if idle_seconds is None else idle_seconds
        with self.connect() as db:
            if not db.execute("UPDATE jobs SET status='running' WHERE id=? AND status='prepared'", (job_id,)).rowcount:
                raise AnalysisError('任务已运行或已有结果；重新研判请准备新任务。', code='job_conflict')
        cancel, finished = Event(), Event()
        deadline = ProgressDeadline(seconds)
        def watchdog():
            while not finished.wait(deadline.wait_seconds()):
                if deadline.expired():
                    cancel.set()
                    return
        watcher = Thread(target=watchdog, daemon=True)
        watcher.start()
        started = time.perf_counter()
        try:
            assessment, meta = assessor(job['request'], provider, cancel_event=cancel,
                response_sink=lambda response: self.record_attempt(job_id, {'source': 'live_api', **response}))
            if cancel.is_set() or deadline.expired():
                raise AnalysisError('本邮件研判超过无完成结果等待时间。', code='progress_timeout')
            # Defense at the seam: only a contract-validated assessment may be stored.
            from .contracts import validate_normalized_assessment
            validate_normalized_assessment(assessment.model_dump(), job['request'])
            result = {'status': 'ok', 'source': 'live_api', 'provider': provider,
                      'assessment': assessment.model_dump(), **meta, 'skills_used': skill_trace(job['request'])}
        except AnalysisError as exc:
            result = {'status': 'error', 'source': 'live_api_attempt', 'provider': provider,
                      'error': str(exc), 'error_code': exc.code, 'attempts': exc.attempts,
                      'decision_status': 'unvalidated', 'evidence_status': 'unverified', 'report_status': 'failed'}
        except Exception:
            result = {'status': 'error', 'source': 'live_api_attempt', 'provider': provider,
                      'error': '研判内部错误，未产生有效结果。', 'error_code': 'internal_error'}
        finally:
            finished.set()
            watcher.join(timeout=1)
        result.update(request_hash=job['request_hash'], recorded_at=now(), elapsed_seconds=round(time.perf_counter()-started, 4))
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=?,result=? WHERE id=? AND status=\'running\'',
                       (result['status'], canonical(result), job_id))
        return self.get(job_id)

    def review(self, job_id, verdict, note):
        job = self.get(job_id)
        if job['status'] != 'ok' or verdict not in ('risk', 'clear', 'review') or not note.strip() or len(note) > 4000:
            raise AnalysisError('请针对有效结果填写人工复核判断和说明（1至4000字）。', code='invalid_review')
        with self.connect() as db:
            db.execute('INSERT INTO reviews VALUES(?,?,?)', (job_id, now(), canonical({'source': 'human_review', 'verdict': verdict, 'note': note})))
        return self.get(job_id)

    def read_imap(self, limit=3, *, context=None, reader=None):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise AnalysisError('IMAP读取数量须为1至50。', code='invalid_source')
        if reader is None:
            from .imap_source import fetch_test_folder_batch
            reader = fetch_test_folder_batch
        batch = reader(limit=limit, inspect_html=True)
        return {'jobs': [self.prepare(mail=m, context=context, origin='imap_readonly') for m in batch['mails']],
                'errors': batch['errors'], 'source': 'imap_readonly', 'sent_to_model': False}

    def doctor(self):
        request = guided_request(parse_mail(asset('sample', 'm026').read_bytes(), 'doctor', inspect_html=True),
                                 {}, asset('guidance', 'v6-c1').read_text(encoding='utf-8'), 'v6')
        fixture = {'verdict': 'clear', 'risk_types': [], 'skill_checks': [
            {'skill_id': s['id'], 'outcome': 'no_finding', 'evidence_ids': []} for s in request['skills']]}
        decode_response(fixture, request)
        with self.connect() as db:
            db.execute('SAVEPOINT installation_probe')
            db.execute('INSERT INTO attempts VALUES(?,?,?)', ('installation_probe', now(), '{}'))
            db.execute('ROLLBACK TO installation_probe')
            db.execute('RELEASE installation_probe')
        return {'status': 'ok', 'mode': 'offline_contract_selftest', 'skills': len(request['skills']),
                'html_static': True, 'samples': list(SAMPLES), 'contract_roundtrip': True,
                'state_writable': self.db.is_file(), 'providers_configured': configuration_status(),
                'api_verified': False, 'imap_verified': False,
                'note': '固定结构夹具通过解析与校验；不是AI研判或真实连通性测试。'}

"""Evaluation lifecycle: request identity, checkpoints, scheduling and termination.

Callers select inputs and score the outcome. Workers only analyze a job; this
module owns the order of artifact persistence, completion and progress updates.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import re
import sys
from threading import Event
import tempfile
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import MAX_CONCURRENCY, LIVE_PROVIDERS
from .contracts import canonical, digest
from .models import AnalysisError
from .progress import ProgressDeadline

_RUN_DIRECTORIES = {'cases', 'requests', 'orphaned'}


def _run_lock(stream, *, release=False):
    """Lock the same run across processes using the platform's native file lock."""
    if sys.platform == 'win32':
        import msvcrt
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if not release and exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError('run directory locked') from None
            raise
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


def timestamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def write_json(path, value, *, immutable=False):
    """Atomically publish one JSON artifact; no shared temporary filenames."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.'+path.name+'.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, ensure_ascii=False, indent=2))
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise AnalysisError('已有运行记录不可覆盖。', code='checkpoint_conflict') from None
        else:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _safe_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', key):
        raise AnalysisError('运行记录标识无效。', code='invalid_run')
    return key


@dataclass(frozen=True)
class EvaluationJob:
    key: str
    request: dict
    payload: object = None
    metadata: dict = field(default_factory=dict)

    @property
    def mail_id(self):
        return self.request['email']['id']

    @property
    def request_hash(self):
        return digest(self.request)


@dataclass
class Produced:
    row: dict | None = None
    artifacts: dict = field(default_factory=dict)
    error: AnalysisError | None = None


@dataclass(frozen=True)
class StopPolicy:
    smoke_check: bool = False
    stop_on_smoke_error: bool = False
    consecutive_errors: int | None = None
    fatal_codes: frozenset = frozenset({'http_400', 'http_401', 'http_402', 'http_403',
                                      'http_404', 'provider_1113', 'invalid_config', 'not_configured'})
    nonfatal_provider_codes: frozenset = frozenset()
    reject_finished: bool = False


@dataclass
class RunOutcome:
    manifest: dict
    records: dict
    results: list
    stop_reason: str | None
    peak_tasks: int
    newly_attempted: int

    @property
    def attempted(self):
        return len(self.records)

    @property
    def finished(self):
        return self.attempted == len(self.results)


class EvaluationRun(AbstractContextManager):
    """One locked run, backed by a directory or kept in memory for page batches."""
    def __init__(self, jobs, *, folder=None, identity=None, run_id=None, legacy_manifest=None):
        self.jobs = [EvaluationJob(j.key, json.loads(canonical(j.request)), j.payload, dict(j.metadata))
                     for j in jobs]
        self.by_key = {_safe_key(j.key): j for j in self.jobs}
        if len(self.by_key) != len(self.jobs) or len({j.key.casefold() for j in self.jobs}) != len(self.jobs):
            raise AnalysisError('评测任务标识重复。', code='invalid_run')
        for job in self.jobs:
            if not isinstance(job.request.get('email'),dict) or not isinstance(job.request['email'].get('id'),str) or not job.mail_id:
                raise AnalysisError('研判请求缺少邮件标识。', code='invalid_run')
            if {'mail_id','request_hash','status','source','assessment'} & job.metadata.keys():
                raise AnalysisError('任务元数据不能覆盖研判身份或结果。',code='invalid_run')
        hashes = {j.key: j.request_hash for j in self.jobs}
        self.identity = json.loads(canonical(identity if identity is not None else {'request_hashes': hashes}))
        if self.identity.get('request_hashes') != hashes:
            raise AnalysisError('运行身份与研判请求不一致。', code='invalid_run')
        self.folder = Path(folder) if folder is not None else None
        self.manifest = {'id': run_id or uuid4().hex, 'created_at': timestamp(), 'identity': self.identity}
        self.records = {}
        self.complete_report = None
        self._lock = None
        self._lock_acquired = False
        self._entered = False
        self._outcome = None
        self._legacy_manifest = legacy_manifest

    def __enter__(self):
        try:
            if self.folder is not None:
                self.folder.mkdir(parents=True, exist_ok=True)
                self._lock = (self.folder/'.run.lock').open('a+b')
                try:
                    _run_lock(self._lock)
                    self._lock_acquired = True
                except BlockingIOError:
                    raise AnalysisError('同一运行目录正在使用，请稍后续跑。', code='run_busy') from None
                manifest_path = self.folder/'manifest.json'
                fresh = not manifest_path.exists()
                if manifest_path.exists():
                    self.manifest = json.loads(manifest_path.read_text())
                    if self.manifest['identity'] != self.identity:
                        raise AnalysisError('续跑配置或测试输入已改变，请创建新的运行目录。', code='run_identity_changed')
                else:
                    if any((self.folder/'cases').glob('*.json')):
                        if not self._legacy_manifest or self._legacy_manifest.get('identity') != self.identity:
                            raise AnalysisError('检查点缺少对应运行身份。', code='checkpoint_invalid')
                        self.manifest['created_at'] = self._legacy_manifest['created_at']
                for path in sorted((self.folder/'cases').glob('*.json')):
                    key = path.stem
                    if key not in self.by_key:
                        raise AnalysisError('检查点包含未知任务。', code='checkpoint_invalid')
                    row = json.loads(path.read_text())
                    self._validate_row(row, self.by_key[key], checkpoint=True)
                    self.records[key] = row
                for path in (self.folder/'requests').glob('*.json'):
                    key = path.stem
                    if key not in self.by_key or digest(json.loads(path.read_text())) != self.by_key[key].request_hash:
                        raise AnalysisError('请求快照校验失败。', code='checkpoint_invalid')
                report_path = self.folder/'report.json'
                if report_path.exists():
                    report = json.loads(report_path.read_text())
                    if report.get('finished'):
                        if len(self.records) != len(self.jobs):
                            raise AnalysisError('完整报告与检查点数量不一致。', code='checkpoint_invalid')
                        self.complete_report = report
                if fresh:
                    for job in self.jobs:
                        path=self.folder/'requests'/f'{job.key}.json'
                        if not path.exists():
                            write_json(path, job.request, immutable=True)
                    write_json(manifest_path, self.manifest, immutable=True)
            self.manifest.setdefault('executions',[])
            self._entered = True
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        if self._lock is not None:
            try:
                if self._lock_acquired:
                    _run_lock(self._lock, release=True)
            finally:
                self._lock.close()
                self._lock = None
                self._lock_acquired = False
        self._entered = False

    @staticmethod
    def _validate_row(row, job, *, checkpoint=False):
        assessment=row.get('assessment')if isinstance(row,dict)else None
        if (not isinstance(row,dict) or row.get('mail_id') != job.mail_id or row.get('request_hash') != job.request_hash or
                row.get('status') not in ('ok', 'error') or row.get('source') == 'not_attempted' or
                row.get('status')=='ok' and (not isinstance(assessment,dict)or assessment.get('verdict')not in ('risk','clear','review')) or
                row.get('status')=='ok' and job.metadata.get('provider')in LIVE_PROVIDERS and row.get('source')!='live_api'):
            raise AnalysisError('检查点输入校验失败。' if checkpoint else '研判结果与任务不匹配。',
                                code='checkpoint_invalid' if checkpoint else 'result_mismatch')

    def _perform(self, job, worker, cancel):
        started = time.perf_counter()
        try:
            produced = worker(job, cancel)
            if isinstance(produced, dict):
                produced = Produced(produced)
        except AnalysisError as exc:
            produced = Produced(error=exc)
        if produced.error is not None:
            exc = produced.error
            proposed = next((a['proposed_verdict'] for a in reversed(exc.attempts) if a.get('proposed_verdict')), None)
            provider = job.metadata.get('provider')
            produced.row = {'status': 'error', 'source': 'live_api_attempt' if provider in LIVE_PROVIDERS else provider,
                            'error': str(exc), 'error_code': exc.code, 'attempts': exc.attempts,
                            'decision_status': 'unvalidated', 'evidence_status': 'unverified',
                            'report_status': 'failed', 'diagnostic_proposed_verdict': proposed}
        row = dict(produced.row)
        row.setdefault('mail_id', job.mail_id)
        row.setdefault('request_hash', job.request_hash)
        row.update(job.metadata)
        row.setdefault('prompt_version', job.request.get('prompt_version'))
        row.setdefault('recorded_at', timestamp())
        row.setdefault('elapsed_seconds', round(time.perf_counter()-started, 4))
        self._validate_row(row, job)
        if job.metadata.get('provider') in LIVE_PROVIDERS and row['status'] == 'ok' and row.get('source') != 'live_api':
            raise AnalysisError('实时评测出现非API来源。', code='result_mismatch')
        if self.folder is not None:
            for name, value in produced.artifacts.items():
                _safe_key(name)
                if name in _RUN_DIRECTORIES:
                    raise AnalysisError('运行产物目录无效。', code='invalid_run')
                write_json(self.folder/name/f'{job.key}.json', value, immutable=True)
            write_json(self.folder/'cases'/f'{job.key}.json', row, immutable=True)
        return row

    def _preserve_orphaned_artifacts(self, pending):
        if self.folder is None:
            return
        keys={job.key for job in pending}
        orphans=[path for directory in self.folder.iterdir()
                 if directory.is_dir() and directory.name not in _RUN_DIRECTORIES
                 for path in directory.glob('*.json') if path.stem in keys]
        if not orphans:
            return
        archive=self.folder/'orphaned'/uuid4().hex
        write_json(archive/'index.json',{'recorded_at':timestamp(),
            'request_hashes':{key:self.by_key[key].request_hash for key in sorted({p.stem for p in orphans})}},immutable=True)
        for path in orphans:
            target=archive/path.parent.name/path.name
            target.parent.mkdir(parents=True,exist_ok=True)
            path.rename(target)

    def execute(self, worker, *, workers, idle_seconds, policy=StopPolicy(), limit=None,
                on_completed=None, execution=None):
        if not self._entered or self._outcome is not None:
            raise AnalysisError('评测运行生命周期无效。', code='invalid_run')
        if type(workers) is not int or not 1 <= workers <= MAX_CONCURRENCY:
            raise AnalysisError('并发数必须是1至20的整数。', code='invalid_config')
        if limit is not None and (type(limit) is not int or not 0 <= limit <= len(self.jobs)):
            raise AnalysisError('评测处理数量无效。', code='invalid_config')
        if policy.reject_finished and self.complete_report is not None:
            raise AnalysisError('Run already complete; do not overwrite original results.', code='run_complete')
        initial = len(self.records)
        pending = [j for j in self.jobs if j.key not in self.records]
        if limit is not None:
            pending = pending[:max(0, limit-initial)]
        if pending:
            self._preserve_orphaned_artifacts(pending)
            entry = {'started_at': timestamp(), 'workers': workers, **(execution or {})}
            self.manifest.setdefault('executions', []).append(entry)
            if self.folder is not None:
                write_json(self.folder/'manifest.json', self.manifest)
        cancel = Event()
        progress = ProgressDeadline(idle_seconds)
        stop_reason = None
        consecutive = 0
        peak = 0
        iterator = iter(pending)
        active = {}

        def check_idle():
            nonlocal stop_reason
            if not stop_reason and progress.expired():
                stop_reason = 'progress_timeout'
                cancel.set()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                while True:
                    check_idle()
                    capacity = 1 if policy.smoke_check and not self.records else workers
                    while not stop_reason and len(active) < capacity:
                        job = next(iterator, None)
                        if job is None:
                            break
                        active[pool.submit(self._perform, job, worker, cancel)] = job
                        peak = max(peak, len(active))
                    if not active:
                        break
                    done, _ = wait(active, timeout=None if cancel.is_set() else progress.wait_seconds(),
                                   return_when=FIRST_COMPLETED)
                    # A paused process must check elapsed time before ready futures reset progress.
                    check_idle()
                    for future in done:
                        job = active.pop(future)
                        row = future.result()  # Artifacts and case checkpoint have already been saved.
                        self.records[job.key] = row
                        if on_completed:
                            on_completed(job.key, row, len(self.records))
                        progress.completed()
                        error = row['status'] == 'error'
                        consecutive = consecutive+1 if error and row.get('error_code') != 'invalid_output' else 0
                        codes = {a.get('provider_error_code') for a in row.get('attempts', [])}
                        fatal = error and row.get('error_code') in policy.fatal_codes and not codes & policy.nonfatal_provider_codes
                        failed_smoke = error and policy.stop_on_smoke_error and not initial and len(self.records) == 1
                        threshold = policy.consecutive_errors and consecutive >= policy.consecutive_errors
                        if not stop_reason and (fatal or failed_smoke or threshold):
                            stop_reason = row.get('error_code', 'analysis_error')
                            cancel.set()
            finally:
                cancel.set()
        results = [self.records.get(j.key, {'mail_id': j.mail_id, 'request_hash': j.request_hash,
                    **j.metadata, 'status': 'error', 'source': 'not_attempted', 'error_code': 'not_attempted'})
                   for j in self.jobs]
        self._outcome = RunOutcome(self.manifest, dict(self.records), results, stop_reason, peak, len(self.records)-initial)
        return self._outcome

    def save_report(self, report):
        if not self._entered or self._outcome is None:
            raise AnalysisError('报告不能先于评测运行保存。', code='invalid_run')
        if self.complete_report is not None:
            return self.complete_report
        if bool(report.get('finished')) != self._outcome.finished:
            raise AnalysisError('报告完成状态与运行记录不一致。', code='invalid_run')
        if self.folder is not None:
            write_json(self.folder/'report.json', report)
        return report

"""Publish immutable mail cohorts and verify them before inference or scoring.

The on-disk JSON contract remains compatible with the original public corpora.
Source annotations stay in the manifest and labels; inference receives only Mail
and context. Labels are deliberately read through a separate scoring operation.
"""
import copy
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from .contracts import digest
from .models import Mail, InspectedMail, load_mail

_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')


def _json(path):
    return json.loads(path.read_text())


def _safe_id(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError('Invalid corpus member id')
    return value


def _mail(value):
    data = value.model_dump() if isinstance(value, Mail) else value
    if not isinstance(data, dict):
        raise ValueError('Corpus input must contain mail fields')
    allowed = (InspectedMail if 'html_inspections' in data else Mail).model_fields
    if set(data) != set(allowed):
        raise ValueError('Corpus input contains missing or non-mail fields')
    return load_mail(data)


def _content_hash(mail):
    data = mail.model_dump()
    data.update(id='pending', content_hash='')
    return digest(data)


def identify_mail(value, prefix='pub'):
    """Bind a derived Mail to the legacy normalized-content hash and id."""
    _safe_id(prefix)
    mail = _mail(value)
    key = _content_hash(mail)
    data = mail.model_dump()
    data.update(id=prefix + '-' + key[:16], content_hash=key)
    return load_mail(data)


def _manifest(value):
    if not isinstance(value, list) or not value:
        raise ValueError('Corpus manifest must be a nonempty list')
    ids, group_splits = set(), {}
    for item in value:
        if not isinstance(item, dict) or not {'id', 'split', 'content_hash'} <= item.keys():
            raise ValueError('Invalid corpus manifest member')
        mid = _safe_id(item['id'])
        if mid in ids:
            raise ValueError('Duplicate corpus member id')
        ids.add(mid)
        if not isinstance(item['split'], str) or not item['split']:
            raise ValueError('Invalid corpus split')
        if not isinstance(item['content_hash'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['content_hash']):
            raise ValueError('Invalid corpus content hash')
        if 'group_id' in item:
            group = item['group_id']
            if not isinstance(group, str) or not group:
                raise ValueError('Invalid corpus group id')
            if group in group_splits and group_splits[group] != item['split']:
                raise ValueError('Corpus group crosses splits')
            group_splits[group] = item['split']
        if 'provenance' in item and not isinstance(item['provenance'], dict):
            raise ValueError('Invalid corpus provenance')
    return value


def _labels(value, manifest):
    expected_ids = {item['id'] for item in manifest}
    if not isinstance(value, dict) or set(value) != expected_ids:
        raise ValueError('Corpus label membership differs from manifest')
    for mid, row in value.items():
        if not isinstance(row, dict) or row.get('id') != mid or row.get('expected') not in ('risk', 'clear', 'review'):
            raise ValueError('Invalid corpus scoring label')
    return value


def _verified_mail(item, value):
    mail = _mail(value)
    if (mail.id != item['id'] or mail.content_hash != item['content_hash']
            or _content_hash(mail) != item['content_hash']):
        raise ValueError('Corpus input changed after freeze')
    return mail


def _request_hashes(metadata, manifest):
    hashes = metadata.get('request_hashes')
    if hashes is not None:
        if (not isinstance(hashes, dict) or set(hashes) != {item['id'] for item in manifest}
                or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in hashes.values())):
            raise ValueError('Frozen request hash membership differs from manifest')


class FrozenCorpus:
    """Verified metadata plus separate inference, request and scoring operations."""

    def __init__(self, base):
        self.base = Path(base)
        self._metadata = _json(self.base / 'freeze.json')
        self._manifest = _manifest(_json(self.base / 'manifest.json'))
        self._context = _json(self.base / 'context.json')
        if not isinstance(self._context, dict) or not isinstance(self._metadata, dict):
            raise ValueError('Invalid frozen corpus context/metadata')
        if (digest(self._manifest) != self._metadata.get('manifest_hash')
                or digest(self._context) != self._metadata.get('context_hash')):
            raise ValueError('Corpus manifest/context changed after freeze')
        _request_hashes(self._metadata, self._manifest)

    @property
    def metadata(self):
        """Preserve original role/caveat/request hashes and future metadata."""
        return copy.deepcopy(self._metadata)

    def cases(self, split=None):
        """Return verified inference inputs in their original manifest order.

        Verify all selected inputs before returning the first one. This operation
        never opens labels.json, including when it is missing or unreadable.
        """
        if split is not None and split not in {item['split'] for item in self._manifest}:
            raise ValueError('Split does not belong to this frozen corpus')
        selected = [item for item in self._manifest if split is None or item['split'] == split]
        mails = [(item, _verified_mail(item, _json(self.base / 'inputs' / (item['id'] + '.json'))))
                 for item in selected]
        for item, mail in mails:
            yield copy.deepcopy(item), mail, copy.deepcopy(self._context)

    def scoring_labels(self):
        """Read answers only for scoring, verifying hash and member identities."""
        labels = _json(self.base / 'labels.json')
        if digest(labels) != self._metadata.get('labels_hash'):
            raise ValueError('Labels changed after freeze')
        return _labels(labels, self._manifest)

    def validate_requests(self, requests):
        """Reject changed fixed requests before a model call; allow a split subset."""
        ids = {item['id'] for item in self._manifest}
        if not isinstance(requests, dict) or not set(requests) <= ids:
            raise ValueError('Request membership differs from frozen corpus')
        for mid, request in requests.items():
            if (not isinstance(request, dict) or not isinstance(request.get('email'), dict)
                    or request['email'].get('id') != mid):
                raise ValueError('Request email id differs from corpus member key')
        hashes = self._metadata.get('request_hashes')
        if hashes is not None and any(digest(request) != hashes[mid] for mid, request in requests.items()):
            raise ValueError('Frozen evaluation requests changed; do not call the model')

    def remap_labels(self, child_manifest):
        """Inherit an entire parent cohort through a one-to-one previous_id map.

        Input enrichment may change mail ids and append provenance, but may not
        select different members, move splits/groups, or rewrite source ancestry.
        """
        child_manifest = _manifest(child_manifest)
        parents = {item['id']: item for item in self._manifest}
        previous = [_safe_id(item.get('previous_id')) for item in child_manifest]
        if len(set(previous)) != len(previous) or set(previous) != set(parents):
            raise ValueError('Corpus lineage must preserve every parent member once')
        labels = self.scoring_labels()
        remapped = {}
        for child in child_manifest:
            old_id = child['previous_id']
            parent = parents[old_id]
            if child['split'] != parent['split'] or child.get('group_id') != parent.get('group_id'):
                raise ValueError('Corpus lineage changed split/group assignment')
            inherited = parent.get('provenance', {})
            provenance = child.get('provenance', {})
            if any(provenance.get(key) != value for key, value in inherited.items()):
                raise ValueError('Corpus lineage changed source provenance')
            remapped[child['id']] = {**labels[old_id], 'id': child['id']}
        return remapped


def scoring_labels(base):
    """Scorer adapter for callers that only need validated answers."""
    return FrozenCorpus(base).scoring_labels()


def _relative_file(name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError('Invalid corpus artifact path')
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or any(part in ('.', '..') for part in path.parts):
        raise ValueError('Invalid corpus artifact path')
    return path


def publish_corpus(target, *, manifest, mails, labels, context, freeze_metadata=None, artifacts=None, files=None):
    """Validate and publish a new cohort once; existing destinations are immutable.

    ``artifacts`` holds audit/mapping JSON by stem. ``files`` holds auxiliary
    bytes (for example self-authored EMLs). Neither becomes inference input.
    Hash calculation and JSON layouts retain the original corpus contract.
    """
    target = Path(target)
    if target.exists() or target.is_symlink():
        raise ValueError('Frozen corpus exists; use a new destination/version')
    manifest = _manifest(copy.deepcopy(manifest))
    labels = _labels(copy.deepcopy(labels), manifest)
    if not isinstance(context, dict):
        raise ValueError('Invalid corpus context')
    inputs = {}
    for value in mails:
        mail = _mail(value)
        if mail.id in inputs:
            raise ValueError('Duplicate corpus input id')
        inputs[mail.id] = mail
    if set(inputs) != {item['id'] for item in manifest}:
        raise ValueError('Corpus input membership differs from manifest')
    for item in manifest:
        _verified_mail(item, inputs[item['id']])
    metadata = copy.deepcopy({} if freeze_metadata is None else freeze_metadata)
    if not isinstance(metadata, dict):
        raise ValueError('Invalid freeze metadata')
    hashes = {'manifest_hash': digest(manifest), 'labels_hash': digest(labels), 'context_hash': digest(context)}
    if any(key in metadata and metadata[key] != value for key, value in hashes.items()):
        raise ValueError('Provided freeze hashes differ from corpus contents')
    metadata.setdefault('created_at', datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'))
    metadata.update(hashes)
    _request_hashes(metadata, manifest)
    payloads = {'manifest.json': manifest, 'labels.json': labels, 'context.json': context, 'freeze.json': metadata}
    for stem, value in (artifacts or {}).items():
        _safe_id(stem)
        name = stem + '.json'
        if name in payloads:
            raise ValueError('Corpus artifact overrides a core file')
        payloads[name] = value
    auxiliary = {}
    for name, value in (files or {}).items():
        relative = _relative_file(name)
        if relative.parts[0] == 'inputs' or str(relative) in payloads or not isinstance(value, bytes):
            raise ValueError('Invalid auxiliary corpus file')
        auxiliary[str(relative)] = value
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / ('.' + target.name + '.publishing')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError('Frozen corpus publication is already in progress') from exc
    os.close(descriptor)
    stage = None
    try:
        if target.exists() or target.is_symlink():
            raise ValueError('Frozen corpus exists; use a new destination/version')
        stage = Path(tempfile.mkdtemp(prefix='.' + target.name + '-', dir=target.parent))
        (stage / 'inputs').mkdir()
        for mail in inputs.values():
            (stage / 'inputs' / (mail.id + '.json')).write_text(mail.model_dump_json(indent=2))
        for name, value in payloads.items():
            (stage / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))
        for name, value in auxiliary.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        # Verify the same public interface before the staged directory is visible.
        prepared = FrozenCorpus(stage)
        list(prepared.cases())
        prepared.scoring_labels()
        if target.exists() or target.is_symlink():
            raise ValueError('Frozen corpus exists; use a new destination/version')
        stage.rename(target)
        stage = None
    finally:
        if stage is not None:
            shutil.rmtree(stage)
        lock.unlink()
    return FrozenCorpus(target)

"""The corpus interface guards inference inputs, scorer answers and lineage."""
import copy
import importlib.util
import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from mailguard.contracts import digest, request_for
from mailguard.frozen_corpus import FrozenCorpus, identify_mail, publish_corpus, scoring_labels
from mailguard.models import Mail
from mailguard.public_corpus import cases


def inputs():
    context = {'scope': '评估可见邮件操作风险，fixture.test是模拟域名。'}
    mails = [identify_mail(Mail(id='imported', content_hash='raw-hash', sender='sender@fixture.test',
                  reply_to='', recipients=['reader@fixture.test'], subject=f'模拟通知{index}',
                  text=f'第{index}封合成邮件，请阅读公开说明。', html_text='', hidden_text='',
                  links=[], attachments=[], warnings=['未访问链接。'])) for index in (1, 2)]
    manifest = [{'id': mail.id, 'split': split, 'content_hash': mail.content_hash, 'group_id': f'group-{index}',
                 'provenance': {'source': 'synthetic-fixture', 'source_row': str(index),
                                'raw_hash': digest(['source', index]), 'source_label': expected}}
                for index, (mail, split, expected) in enumerate(zip(mails, ('dev', 'test'), ('clear', 'review')), 1)]
    labels = {item['id']: {'id': item['id'], 'expected': item['provenance']['source_label'],
                           'label_source': 'fixture_only', 'rationale': '仅供评分的答案说明。'} for item in manifest}
    return manifest, mails, labels, context


def publish(base, **kwargs):
    manifest, mails, labels, context = inputs()
    return publish_corpus(base, manifest=manifest, mails=mails, labels=labels, context=context, **kwargs)


def edit(path, change):
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value, ensure_ascii=False))


def script(name):
    path = Path(__file__).resolve().parents[1] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location('fixture_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publish_read_and_scoring_roundtrip_preserves_metadata_and_old_layout(tmp_path):
    metadata = {'created_at': '2026-10-16T10:00:00+08:00', 'evaluation_roles': {'test': 'seen_regression'},
                'future_field': {'version': 7}, 'caveat': '合成样例。'}
    corpus = publish(tmp_path / 'cohort', freeze_metadata=metadata,
                     artifacts={'mapping': [{'previous_id': 'old', 'id': 'new'}]},
                     files={'eml/example.eml': b'Subject: fixture\n\nOnly simulated data.\n'})
    manifest, mails, labels, context = inputs()
    assert list(corpus.cases()) == list(zip(manifest, mails, [context] * len(mails)))
    assert corpus.scoring_labels() == labels == scoring_labels(corpus.base)
    assert list(cases('dev', corpus.base)) == [(manifest[0], mails[0], context)]
    assert corpus.metadata == {**metadata, 'manifest_hash': digest(manifest),
                               'labels_hash': digest(labels), 'context_hash': digest(context)}
    returned = corpus.metadata
    returned['future_field']['version'] = 0
    assert corpus.metadata['future_field']['version'] == 7
    assert json.loads((corpus.base / 'mapping.json').read_text())[0]['previous_id'] == 'old'
    assert (corpus.base / 'eml/example.eml').read_bytes().startswith(b'Subject: fixture')


def test_inference_does_not_open_labels_or_include_source_answers(tmp_path):
    corpus = publish(tmp_path / 'cohort')
    (corpus.base / 'labels.json').unlink()
    records = list(FrozenCorpus(corpus.base).cases())
    assert len(records) == 2
    for _, mail, context in records:
        request = request_for(mail, context, 'v4')
        serialized = json.dumps(request, ensure_ascii=False)
        assert all(key not in serialized for key in ('source_label', 'fixture_only', '仅供评分的答案', 'provenance'))
    with pytest.raises(FileNotFoundError):
        corpus.scoring_labels()


@pytest.mark.parametrize('name', ['manifest', 'context', 'labels'])
def test_changed_frozen_metadata_or_answers_are_rejected(tmp_path, name):
    corpus = publish(tmp_path / 'cohort')
    def change(value):
        if name == 'manifest':
            value[0]['provenance']['source_row'] = 'changed'
        elif name == 'context':
            value['scope'] = '改变了背景'
        else:
            next(iter(value.values()))['expected'] = 'risk'
    edit(corpus.base / (name + '.json'), change)
    with pytest.raises(ValueError, match='changed after freeze'):
        if name == 'labels':
            scoring_labels(corpus.base)
        else:
            FrozenCorpus(corpus.base)


@pytest.mark.parametrize('field,value', [
    ('id', 'different-id'), ('content_hash', '0' * 64), ('text', '内容发生改变。'),
    ('expected', 'risk'),
])
def test_all_selected_inputs_are_verified_before_first_inference(tmp_path, field, value):
    corpus = publish(tmp_path / 'cohort')
    manifest, _, _, _ = inputs()
    edit(corpus.base / 'inputs' / (manifest[1]['id'] + '.json'), lambda item: item.update({field: value}))
    with pytest.raises(ValueError, match='Corpus input'):
        next(corpus.cases())


def test_fixed_requests_are_checked_before_a_call_and_split_subsets_are_supported(tmp_path):
    manifest, mails, labels, context = inputs()
    requests = {mail.id: request_for(mail, context, 'v4') for mail in mails}
    frozen = {mid: digest(request) for mid, request in requests.items()}
    corpus = publish_corpus(tmp_path / 'cohort', manifest=manifest, mails=mails, labels=labels,
                             context=context, freeze_metadata={'request_hashes': frozen})
    corpus.validate_requests(requests)
    corpus.validate_requests({mails[0].id: requests[mails[0].id]})
    changed = copy.deepcopy(requests)
    changed[mails[0].id]['prompt'] += '\n新的提示'
    with pytest.raises(ValueError, match='Frozen evaluation requests changed'):
        corpus.validate_requests(changed)
    with pytest.raises(ValueError, match='membership'):
        corpus.validate_requests({'outside-member': {}})


def test_legacy_corpora_without_request_hashes_keep_request_freedom(tmp_path):
    corpus = publish(tmp_path / 'cohort')
    mail = next(corpus.cases())[1]
    corpus.validate_requests({mail.id: {'email': {'id': mail.id}, 'prompt': '没有冻结模型请求的旧格式可另开新实验。'}})


@pytest.mark.parametrize('email', [{'id': 'wrong-member'}, {}, None])
def test_even_legacy_requests_must_match_their_corpus_member_key(tmp_path, email):
    corpus = publish(tmp_path / 'cohort')
    mail = next(corpus.cases())[1]
    with pytest.raises(ValueError, match='email id'):
        corpus.validate_requests({mail.id: {'email': email, 'prompt': '合成检查请求。'}})


def test_publish_never_overwrites_and_failed_publication_leaves_no_partial_cohort(tmp_path):
    corpus = publish(tmp_path / 'cohort')
    original = {str(path.relative_to(corpus.base)): path.read_bytes() for path in corpus.base.rglob('*') if path.is_file()}
    with pytest.raises(ValueError, match='Frozen corpus exists'):
        publish(corpus.base)
    assert original == {str(path.relative_to(corpus.base)): path.read_bytes() for path in corpus.base.rglob('*') if path.is_file()}
    with pytest.raises(TypeError):
        publish(tmp_path / 'failed', artifacts={'audit': {'not_json': {1, 2}}})
    assert not (tmp_path / 'failed').exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ['cohort']


@pytest.mark.parametrize('change', [
    lambda manifest, mails, labels: manifest.append(copy.deepcopy(manifest[0])),
    lambda manifest, mails, labels: labels.pop(manifest[0]['id']),
    lambda manifest, mails, labels: mails.pop(),
    lambda manifest, mails, labels: manifest[1].update(group_id=manifest[0]['group_id']),
])
def test_publish_rejects_duplicate_missing_or_cross_split_members(tmp_path, change):
    manifest, mails, labels, context = inputs()
    change(manifest, mails, labels)
    with pytest.raises(ValueError):
        publish_corpus(tmp_path / 'cohort', manifest=manifest, mails=mails, labels=labels, context=context)
    assert not (tmp_path / 'cohort').exists()


@pytest.mark.parametrize('kwargs', [
    {'files': {'../outside': b'fixture'}},
    {'files': {'inputs/replacement.json': b'fixture'}},
    {'artifacts': {'labels': {}}},
    {'freeze_metadata': {'request_hashes': {}}},
    {'freeze_metadata': {'labels_hash': 'changed'}},
])
def test_auxiliary_files_and_metadata_cannot_replace_frozen_contract(tmp_path, kwargs):
    with pytest.raises(ValueError):
        publish(tmp_path / 'cohort', **kwargs)
    assert not (tmp_path / 'cohort').exists()


def child_rows(parent):
    rows, mails = [], []
    for item, old, _ in parent.cases():
        data = old.model_dump()
        data['warnings'].append('补充来源检查范围。')
        mail = identify_mail(data)
        mails.append(mail)
        rows.append({**item, 'id': mail.id, 'content_hash': mail.content_hash, 'previous_id': old.id,
                      'provenance': {**item['provenance'], 'restored_original': {'raw_hash': 'fixture'}}})
    return rows, mails


def test_enrichment_maps_ids_labels_splits_groups_and_source_ancestry_once(tmp_path):
    parent = publish(tmp_path / 'parent')
    rows, mails = child_rows(parent)
    labels = parent.remap_labels(rows)
    assert set(labels) == {mail.id for mail in mails}
    original = parent.scoring_labels()
    for row in rows:
        assert labels[row['id']] == {**original[row['previous_id']], 'id': row['id']}
    child = publish_corpus(tmp_path / 'child', manifest=rows, mails=mails, labels=labels,
                           context=next(parent.cases())[2])
    assert len(list(child.cases())) == len(list(parent.cases()))


@pytest.mark.parametrize('field,value', [('previous_id', 'outside'), ('split', 'other'),
                                        ('group_id', 'different'), ('provenance', {'source': 'different'})])
def test_enrichment_rejects_changed_membership_split_group_or_source(tmp_path, field, value):
    parent = publish(tmp_path / 'parent')
    rows, _ = child_rows(parent)
    rows[0][field] = value
    with pytest.raises(ValueError, match='lineage'):
        parent.remap_labels(rows)


def test_self_authored_form_builder_uses_the_same_publish_read_and_score_interface(tmp_path, monkeypatch):
    module = script('prepare_form_controls')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    module.prepare()
    corpus = FrozenCorpus(tmp_path / 'data/private/form_controls/v1')
    records = list(corpus.cases('dev'))
    labels = corpus.scoring_labels()
    assert len(records) == 16
    assert [row['expected'] for row in labels.values()].count('risk') == 4
    assert [row['expected'] for row in labels.values()].count('clear') == 8
    assert [row['expected'] for row in labels.values()].count('review') == 4
    assert all('html_inspections' in mail.model_dump() for _, mail, _ in records)
    assert len(list((corpus.base / 'eml').glob('*.eml'))) == 16
    with pytest.raises(ValueError, match='already exist'):
        module.prepare()


def test_html_enrichment_builder_keeps_parent_members_and_answers(tmp_path, monkeypatch):
    module = script('prepare_html_corpus')
    parent = publish(tmp_path / 'parent')
    raw = EmailMessage()
    raw['From'] = 'notice@fixture.test'
    raw['Subject'] = '合成静态表单'
    raw.set_content('用于静态解析验证的无害页面。')
    raw.add_alternative('<form action="https://fixture.test/search"><input name="q"></form>', subtype='html')
    class Originals:
        def raw(self, provenance):
            return raw.as_bytes()
        def close(self):
            pass
    monkeypatch.setattr(module, 'Originals', Originals)
    module.prepare(parent.base, tmp_path / 'child')
    child = FrozenCorpus(tmp_path / 'child')
    records = list(child.cases())
    rows = [item for item, _, _ in records]
    assert child.scoring_labels() == parent.remap_labels(rows)
    before = {item['id']: (item, mail) for item, mail, _ in parent.cases()}
    for item, mail, _ in records:
        original_item, original_mail = before[item['previous_id']]
        assert item['provenance'] == original_item['provenance']
        assert item['split'] == original_item['split'] and item['group_id'] == original_item['group_id']
        assert mail.text == original_mail.text and mail.links == original_mail.links
        assert mail.attachments == original_mail.attachments
        assert (child.base / 'inputs' / (mail.id + '.json')).read_text() == mail.model_dump_json(indent=2)
    assert all(mail.html_inspections for _, mail, _ in records)
    assert child.metadata['evaluation_roles']['test'] == 'seen_regression'
    assert len(json.loads((child.base / 'mapping.json').read_text())) == 2

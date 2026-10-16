"""Reproducible, offline public corpus preparation; provenance never enters inference."""
import argparse
import csv
import hashlib
import html
import json
import mailbox
import re
import tarfile
import unicodedata
from collections import Counter, defaultdict
from email.utils import parseaddr
from pathlib import Path

from .config import ROOT
from .engine import digest
from .evaluation import now
from .frozen_corpus import FrozenCorpus, identify_mail, publish_corpus
from .models import Mail
from .parser import parse_mail

SOURCES = ROOT / 'data/private/public_email_sources'
DEST = ROOT / 'data/private/public_benchmark/v1'
CONTEXT = {'scope': '独立公共邮件研究。根据邮件可见事实判断社工、钓鱼和执行等安全风险。',
           'business_context': '未知发件企业，没有内部域名白名单或额外审批记录。历史邮件按当时文本语境理解；不能访问链接、执行附件或使用实时域名信誉。',
           'privacy': '输入保留原语言，邮箱本地部分和长号码等已替换；不把脱敏占位符本身当作风险证据。'}
EMAIL = re.compile(r'([\w.+%\-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')
URL = re.compile(r'https?://[^\s<>"\]]+', re.I)


def normalize(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', html.unescape(text))).strip().casefold()


def template(text):
    text = EMAIL.sub(' mailbox ', normalize(text))
    text = URL.sub(' link ', text)
    return re.sub(r'\d+', ' num ', text)


def shingles(text):
    words = re.findall(r'\w+', template(text))
    return {hashlib.blake2b(' '.join(words[i:i+5]).encode(), digest_size=8).digest()
            for i in range(max(0, len(words)-4))}


def redact(text):
    text = EMAIL.sub(lambda m: 'user-' + hashlib.sha256(m[1].casefold().encode()).hexdigest()[:8] + '@' + m[2], text)
    text = re.sub(r'(?<![A-Za-z0-9])\+?\d[\d ()-]{8,}\d(?![A-Za-z0-9])', '[REDACTED_NUMBER]', text)
    # Keep the destination host/path, redact query values/fragments that can identify recipients.
    text = URL.sub(lambda m: re.sub(r'([?&][^=&\s]+)=([^&\s]*)', r'\1=REDACTED', m[0]).split('#')[0], text)
    return text


def derived(mail):
    data = mail.model_dump()
    data['sender'] = parseaddr(data['sender'])[1] or data['sender']
    data['reply_to'] = parseaddr(data['reply_to'])[1] or data['reply_to']
    data['recipients'] = ['recipient@redacted.invalid']
    def visit(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [visit(x) for x in value]
        if isinstance(value, dict):
            return {k: visit(v) for k, v in value.items()}
        return value
    data = visit(data)
    data['warnings'] = ['公开历史文本；地址和长号码等已做规则化脱敏，非完整匿名化。'+
                        ('HTML静态检查覆盖见各来源；未访问链接或检查其他附件内容。' if 'html_inspections' in data else '未访问链接或检测附件。')]
    return identify_mail(data)


def csv_mail(row, source, number):
    content = json.loads(row['email_content']) if source == 'phishnchips' else row
    body = content.get('body', '')
    links = [{'text': m[0], 'url': m[0]} for m in URL.finditer(body)]
    if source == 'phishnchips' and content.get('link_url'):
        links.append({'text': content.get('link_display_text') or content['link_url'], 'url': content['link_url']})
    raw = json.dumps(row, ensure_ascii=False).encode()
    return Mail(id='import', content_hash=hashlib.sha256(raw).hexdigest(),
                sender=content.get('from') or content.get('sender', ''), reply_to='', recipients=[],
                subject=content.get('subject', ''), text=body, html_text='', hidden_text='', links=links,
                attachments=[], warnings=[]), hashlib.sha256(raw).hexdigest()


def raw_cases():
    for year in (2024, 2025):
        box = mailbox.mbox(SOURCES / f'nazario-{year}.mbox', create=False)
        for i, key in enumerate(box.iterkeys()):
            yield f'nazario{year}', str(i+1), 'risk', box.get_bytes(key)
        box.close()
    csv.field_size_limit(20_000_000)
    for source, name in [('phishnchips', 'hf-phishnchips-real.csv'), ('aura', 'hf-aura-nazario.csv')]:
        with (SOURCES / name).open() as f:
            for i, row in enumerate(csv.DictReader(f)):
                yield source, str(i+1), 'risk', row
    for category in ('easy', 'hard'):
        with tarfile.open(SOURCES / f'spamassassin-{category}-ham.tar.bz2', 'r:bz2') as archive:
            for member in archive:
                # Read bounded regular files directly; never extract archive paths.
                if not member.isfile() or not re.match(r'^\d+\.', Path(member.name).name):
                    continue
                if member.size > 5*1024*1024:
                    yield f'spamassassin_{category}', member.name, 'clear', b''
                else:
                    yield f'spamassassin_{category}', member.name, 'clear', archive.extractfile(member).read()


def prepare(dest=DEST, dev_each=30, test_each=50):
    dest = Path(dest)
    if dest.exists() or dest.is_symlink():
        raise ValueError('Frozen corpus exists; use a new destination/version.')
    records, excluded, counts = [], [], Counter()
    for source, index, expected, raw in raw_cases():
        counts[source] += 1
        provenance = {'source': source, 'source_row': index, 'source_label': expected}
        try:
            if isinstance(raw, dict):
                mail, raw_hash = csv_mail(raw, source, index)
            else:
                raw_hash = hashlib.sha256(raw).hexdigest()
                mail = parse_mail(raw)
            provenance['raw_hash'] = raw_hash
            body = mail.text.strip() or mail.html_text.strip()
            full = mail.text + mail.html_text + mail.hidden_text
            if 'FOLDER INTERNAL DATA' in mail.subject or body.startswith('This text is part of the internal format of your mail folder'):
                reason = 'folder_metadata_or_concatenated_record'
            elif full.lower().count('return-path:') >= 5 and full.lower().count('message-id:') >= 5:
                reason = 'concatenated_messages'
            elif len(full) > 24000:
                reason = 'text_over_24000_characters'
            elif len(normalize(body)) < 80:
                reason = 'no_or_short_visible_text'
            elif full.count('\ufffd') / max(1, len(full)) > .03:
                reason = 'decoding_damage'
            else:
                reason = None
            if reason:
                excluded.append({**provenance, 'reason': reason})
                continue
            clean = derived(mail)
            records.append({'mail': clean, 'body': body, 'provenance': provenance, 'expected': expected})
        except Exception as exc:
            excluded.append({**provenance, 'reason': 'parse_error', 'error_type': type(exc).__name__})
    # Union duplicate/template groups BEFORE selecting samples or splitting.
    parent = list(range(len(records)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[max(a,b)] = min(a,b)
    hashes, buckets = {}, defaultdict(list)
    tokens = [shingles(r['body']) for r in records]
    near_edges = 0
    for i, record in enumerate(records):
        key = ''.join(c for c in template(record['body']) if c.isalnum())
        if key in hashes:
            union(i, hashes[key])
        else:
            hashes[key] = i
        # Shared 5-word shingles generate candidates; Jaccard >= .8 merges them.
        candidates = set()
        for token in tokens[i]:
            candidates.update(buckets[token])
        for j in candidates:
            if find(i) == find(j) or not tokens[i] or not tokens[j]:
                continue
            if min(len(tokens[i]), len(tokens[j])) / max(len(tokens[i]), len(tokens[j])) < .8:
                continue
            common = len(tokens[i] & tokens[j])
            if common / (len(tokens[i]) + len(tokens[j]) - common) >= .8:
                union(i, j)
                near_edges += 1
        for token in tokens[i]:
            buckets[token].append(i)
    grouped = defaultdict(list)
    for i, record in enumerate(records):
        grouped[find(i)].append(record)
    rank = {'nazario2025':0, 'nazario2024':1, 'phishnchips':2, 'aura':3,
            'spamassassin_easy':0, 'spamassassin_hard':0}
    eligible, groups = [], []
    for members in grouped.values():
        group_id = digest(sorted(r['provenance']['raw_hash'] for r in members))
        labels = {r['expected'] for r in members}
        group = {'group_id':group_id, 'members':[r['provenance'] for r in members]}
        if len(labels) != 1:
            group['excluded'] = 'conflicting_source_labels'
            groups.append(group)
            continue
        chosen = min(members, key=lambda r: (rank[r['provenance']['source']], r['mail'].content_hash))
        group['representative'] = chosen['mail'].id
        groups.append(group)
        eligible.append({**chosen, 'group_id':group_id, 'members':len(members)})
    # Deterministic group-level assignment independent of model predictions.
    selected = []
    for expected in ('risk', 'clear'):
        pool = sorted((r for r in eligible if r['expected']==expected), key=lambda r: digest(['public-v1', r['group_id']]))
        if len(pool) < dev_each + test_each:
            raise ValueError('Not enough deduplicated groups')
        for split, rows in [('dev', pool[:dev_each]), ('test', pool[dev_each:dev_each+test_each])]:
            selected.extend({**r, 'split':split} for r in rows)
    selected.sort(key=lambda r:r['mail'].id)
    manifest, truth = [], {}
    for r in selected:
        mail = r['mail']
        if mail.id in truth:
            raise ValueError('Duplicate derived id after grouping')
        manifest.append({'id':mail.id, 'split':r['split'], 'group_id':r['group_id'],
                         'content_hash':mail.content_hash, 'provenance':r['provenance'], 'group_size':r['members']})
        truth[mail.id] = {'id':mail.id, 'expected':r['expected'], 'label_source':'published_source_annotation',
                          'review_status':'not_independently_adjudicated'}
    files = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCES.iterdir()
             if p.suffix in ('.mbox','.csv') or p.name.endswith('.tar.bz2')}
    audit = {'created_at':now(), 'source_counts':dict(counts), 'source_files':files,
             'excluded_counts':dict(Counter(r['reason'] for r in excluded)), 'excluded':excluded,
             'eligible_records':len(records), 'groups_total':len(groups), 'eligible_groups':len(eligible),
             'duplicate_records_removed':len(records)-len(groups), 'near_duplicate_edges':near_edges,
             'split_counts':dict(Counter(r['split']+'_'+r['expected'] for r in selected)),
             'selected_sources':dict(Counter(r['provenance']['source'] for r in selected)),
             'dedup_method':'casefold/NFKC/entities; replace email/URL/digits; alnum exact and 5-word-shingle Jaccard>=0.8; transitive union',
             'limitations':['模板聚类并非完整语义去重保证。','标签来自原数据，非逐封独立安全鉴定；spam/ham标签不能保证安全。',
                            '风险与正常来源/年代不同；公开语料可能存在基础模型训练污染。','保留原语言；规则脱敏不保证删除所有人名或个人信息。']}
    publish_corpus(dest, manifest=manifest, mails=[r['mail'] for r in selected], labels=truth,
                   context=CONTEXT, artifacts={'audit':audit, 'groups':groups})
    return {k:v for k,v in audit.items() if k!='excluded'}


def cases(split, base=DEST):
    yield from FrozenCorpus(base).cases(split)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dest', type=Path, default=DEST)
    args = parser.parse_args()
    print(json.dumps(prepare(args.dest),ensure_ascii=False,indent=2))

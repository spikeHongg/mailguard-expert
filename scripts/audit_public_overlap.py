"""Offline corpus overlap audit. Never visits message links or executes HTML."""
import csv
import hashlib
import html
import json
import mailbox
import re
import unicodedata
from collections import Counter, defaultdict
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data/private/public_email_sources'
csv.field_size_limit(20_000_000)


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, value):
        self.parts.append(value)


def visible(value):
    parser = Text()
    parser.feed(value)
    return ' '.join(parser.parts)


def normalized(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', html.unescape(value))).strip().casefold()


def alnum(value):
    return ''.join(c for c in normalized(value) if c.isalnum())


def load():
    a = list(csv.DictReader((DATA / 'hf-phishnchips-real.csv').open()))
    b = list(csv.DictReader((DATA / 'hf-aura-nazario.csv').open()))
    corpora = {
        'phishnchips': [{'id': r['id'], 'bodies': [json.loads(r['email_content'])['body']]} for r in a],
        'aura': [{'id': f'aura-row-{i+1}', 'bodies': [r['body']]} for i, r in enumerate(b)],
    }
    for year in (2024, 2025):
        box = mailbox.mbox(DATA / f'nazario-{year}.mbox', create=False)
        rows = []
        for i, key in enumerate(box.iterkeys()):
            msg = BytesParser(policy=policy.default).parsebytes(box.get_bytes(key))
            bodies = []
            for part in msg.walk():
                if part.get_content_type() not in ('text/plain', 'text/html') or part.get_content_disposition() == 'attachment':
                    continue
                try:
                    body = part.get_content()
                except (LookupError, UnicodeError):
                    body = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
                bodies.append(body)
                if part.get_content_type() == 'text/html':
                    bodies.append(visible(body))
            rows.append({'id': f'nazario-{year}-row-{i+1}', 'bodies': bodies})
        box.close()
        corpora[f'nazario{year}'] = rows
    return corpora, dict(Counter(r['datasource'] for r in a))


def compare(left, right, transform):
    index = defaultdict(set)
    for row in right:
        for body in row['bodies']:
            value = transform(body)
            if len(value) >= 80:
                index[value].add(row['id'])
    pairs = set()
    for row in left:
        for body in row['bodies']:
            for match in index.get(transform(body), ()):
                pairs.add((row['id'], match))
    return {'left_rows': len({a for a, b in pairs}), 'right_rows': len({b for a, b in pairs}),
            'pairs_count': len(pairs), 'pairs': sorted(pairs)}


def audit():
    corpora, years = load()
    result = {'method': {'version': 1, 'minimum_transformed_characters': 80,
              'normalized': 'HTML entity unescape + Unicode NFKC + whitespace collapse + casefold',
              'alnum': 'normalized then retain Unicode letters/numbers only; candidate duplicates, not raw equality',
              'limits': 'No semantic/template near-duplicate guarantee; multipart bodies compared individually; no attachment extraction.'},
              'counts': {k: len(v) for k, v in corpora.items()}, 'phishnchips_source_years': years,
              'files': {}, 'comparisons': {}, 'anomalies': []}
    for path in DATA.glob('*'):
        if path.suffix in ('.csv', '.mbox'):
            raw = path.read_bytes()
            result['files'][path.name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    for x, y in [('phishnchips', 'aura'), ('phishnchips', 'nazario2024'),
                 ('phishnchips', 'nazario2025'), ('aura', 'nazario2024'),
                 ('aura', 'nazario2025'), ('nazario2024', 'nazario2025')]:
        result['comparisons'][f'{x}__{y}'] = {
            name: compare(corpora[x], corpora[y], fn)
            for name, fn in [('raw', lambda s: s), ('normalized', normalized), ('alnum', alnum)]}
    for row in corpora['aura']:
        body = row['bodies'][0]
        if len(body) > 100_000:
            value = alnum(body)
            contained = [r['id'] for r in corpora['phishnchips']
                         if len(alnum(r['bodies'][0])) >= 80 and alnum(r['bodies'][0]) in value]
            result['anomalies'].append({'id': row['id'], 'characters': len(body),
                'return_path_markers': body.lower().count('return-path:'),
                'message_id_markers': body.lower().count('message-id:'),
                'contained_phishnchips_ids_alnum': contained,
                'note': 'Containment in oversized aggregate is separate from one-mail equality.'})
    result['internal_normalized_duplicates'] = {}
    for name, rows in corpora.items():
        groups = defaultdict(set)
        for row in rows:
            for body in row['bodies']:
                value = normalized(body)
                if len(value) >= 80:
                    groups[value].add(row['id'])
        duplicates = [sorted(g) for g in groups.values() if len(g) > 1]
        result['internal_normalized_duplicates'][name] = duplicates
    dest = DATA / 'overlap-audit.json'
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    brief = {k: v for k, v in result.items() if k not in ('comparisons', 'internal_normalized_duplicates', 'anomalies')}
    brief['comparisons'] = {key: {method: {k: v for k, v in val.items() if k != 'pairs'}
                            for method, val in methods.items()} for key, methods in result['comparisons'].items()}
    brief['anomalies'] = [{k: (len(v) if k == 'contained_phishnchips_ids_alnum' else v)
                           for k, v in row.items()} for row in result['anomalies']]
    print(json.dumps(brief, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    audit()

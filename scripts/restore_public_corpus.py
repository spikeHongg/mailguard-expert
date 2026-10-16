"""Restore selected CSV exports from matching originals, without reading model results.

Preserves the v1 sample membership, source labels and split assignment. Aborts on
new duplicates crossing splits; never selects easier replacements after evaluation.
"""
import csv
import hashlib
import json
import mailbox
import sys
from collections import defaultdict, Counter
from email.parser import BytesParser
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailguard.public_corpus import DEST, SOURCES, derived, normalize, shingles, template
from mailguard.parser import parse_mail
from mailguard.evaluation import now
from mailguard.frozen_corpus import FrozenCorpus, publish_corpus


def date_key(value):
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, OverflowError):
        return value.strip()


def restore():
    target = DEST.parent/'v2'
    if target.exists():
        raise ValueError('v2 already exists; never overwrite frozen inputs')
    originals = defaultdict(list)
    for year in range(2015,2023):
        path=SOURCES/f'nazario-{year}.mbox'
        box=mailbox.mbox(path,create=False)
        for i,key in enumerate(box.iterkeys()):
            raw=box.get_bytes(key)
            try:
                mail=parse_mail(raw)
            except Exception:
                continue
            if 'FOLDER INTERNAL DATA' in mail.subject:
                continue
            header=BytesParser(policy=policy.default).parsebytes(raw,headersonly=True)
            record={'mail':mail,'year':year,'source_row':i+1,'raw_hash':hashlib.sha256(raw).hexdigest(),
                    'date':date_key(str(header.get('date','')))}
            originals[(parseaddr(mail.sender)[1].casefold(),normalize(mail.subject))].append(record)
        box.close()
    csv.field_size_limit(20_000_000)
    source_rows={name:list(csv.DictReader((SOURCES/file).open())) for name,file in
                 [('aura','hf-aura-nazario.csv'),('phishnchips','hf-phishnchips-real.csv')]}
    parent=FrozenCorpus(DEST)
    records=list(parent.cases())
    manifest=[item for item,_,_ in records]
    output, mappings, mails = [],[],[]
    for item,old,context in records:
        mail=old
        entry=dict(item)
        mapping={'previous_id':old.id,'split':item['split'],'restored':False}
        source=item['provenance']['source']
        if source in source_rows:
            row=source_rows[source][int(item['provenance']['source_row'])-1]
            content=json.loads(row['email_content']) if source=='phishnchips' else row
            sender=content.get('from') or content.get('sender','')
            candidates=originals.get((parseaddr(sender)[1].casefold(),normalize(content.get('subject',''))),[])
            tokens=shingles(content['body'])
            found=[]
            for candidate in candidates:
                if source=='phishnchips' and row.get('datasource')!=f'nazario_{candidate["year"]}':
                    continue
                m=candidate['mail']
                variants=[m.text,m.html_text,m.text+' '+m.html_text]
                coverage=max((len(tokens & shingles(s))/len(tokens) for s in variants),default=0) if tokens else 0
                date_matches=source=='aura' and date_key(row.get('date',''))==candidate['date']
                if coverage>=.8 and (date_matches or source=='phishnchips'):
                    found.append({**candidate,'body_shingle_coverage':coverage})
            # Do not choose an arbitrary member of a repeated campaign.
            if len(found)==1:
                match=found[0]
                mail=derived(match['mail'])
                mapping.update(restored=True,year=match['year'],original_row=match['source_row'],
                               body_shingle_coverage=round(match['body_shingle_coverage'],4),
                               original_raw_hash=match['raw_hash'],
                               links_before=len(old.links),links_after=len(mail.links),
                               attachments_before=len(old.attachments),attachments_after=len(mail.attachments))
                entry['provenance']={**item['provenance'],'restored_original':{k:mapping[k]for k in
                    ('year','original_row','original_raw_hash','body_shingle_coverage')}}
            else:
                mapping.update(candidate_matches=len(found),reason='ambiguous_or_no_exact_source_match')
        mapping['id']=mail.id
        entry.update(id=mail.id,content_hash=mail.content_hash,previous_id=old.id)
        output.append(entry);mappings.append(mapping);mails.append(mail)
    truths=parent.remap_labels(output)
    # Recheck all selected groups using restored text (no evaluation labels involved).
    bodies=[m.text.strip() or m.html_text.strip() for m in mails]
    tokens=[shingles(s)for s in bodies]
    for i in range(len(mails)):
        for j in range(i):
            union=tokens[i]|tokens[j]
            similarity=len(tokens[i]&tokens[j])/len(union) if union else 0
            exact=''.join(c for c in template(bodies[i])if c.isalnum())==''.join(c for c in template(bodies[j])if c.isalnum())
            if exact or similarity>=.8:
                raise ValueError(f'Restored duplicate pair requires regrouping: {mails[i].id}, {mails[j].id}')
    audit=json.loads((DEST/'audit.json').read_text())
    audit.update(created_at=now(),parent_version='v1',restoration={'matched':sum(x['restored']for x in mappings),
                 'csv_candidates':sum(x['provenance']['source']in source_rows for x in manifest),
                 'restored_by_split':dict(Counter(x['split']for x in mappings if x['restored'])),
                 'membership_preserved':True,'labels_preserved':True,'split_preserved':True,
                 'selected_pairwise_duplicates':0,'mapping_file':'restoration.json'})
    audit['source_files'].update({f'nazario-{y}.mbox':hashlib.sha256((SOURCES/f'nazario-{y}.mbox').read_bytes()).hexdigest()for y in range(2015,2023)})
    published=publish_corpus(target,manifest=output,mails=mails,labels=truths,context=context,
                             artifacts={'audit':audit,'restoration':mappings})
    freeze=published.metadata
    print(json.dumps({'version':'v2','restoration':audit['restoration'],'new_source_counts':len(originals),'freeze':freeze},ensure_ascii=False,indent=2))


if __name__=='__main__':
    restore()

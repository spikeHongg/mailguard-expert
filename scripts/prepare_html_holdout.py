"""Freeze fresh groups BEFORE model evaluation; never select by model predictions."""
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mailguard.engine import digest, request_for
from mailguard.evaluation import now
from mailguard.parser import parse_mail
from mailguard.public_corpus import DEST, CONTEXT, cases, derived, normalize, shingles, template
from mailguard.frozen_corpus import publish_corpus
from prepare_html_corpus import Originals


def signature(mail):
    text=mail.text.strip() or mail.html_text.strip()
    return ''.join(c for c in template(text)if c.isalnum()), shingles(text)


def prepare():
    target=DEST.parent/'html-holdout-v1'
    if target.exists():raise ValueError('Holdout already frozen')
    old=list(cases('dev',DEST.parent/'v2'))+list(cases('test',DEST.parent/'v2'))
    excluded_groups={item['group_id']for item,_,_ in old}
    signatures=[signature(mail)for _,mail,_ in old]
    groups=json.loads((DEST/'groups.json').read_text())
    pool=[]
    for group in groups:
        if group.get('excluded') or group['group_id'] in excluded_groups:continue
        originals=[p for p in group['members']if p['source'].startswith(('nazario2024','nazario2025','spamassassin_'))]
        if not originals:continue
        chosen=min(originals,key=lambda p:digest(['html-holdout-member-v1',p['raw_hash']]))
        pool.append((group,chosen))
    pool.sort(key=lambda pair:digest(['html-holdout-v1',pair[0]['group_id']]))
    counts=Counter();rejected=Counter();mails=[];manifest=[];truth={};requests={}
    source=Originals()
    try:
        for group,provenance in pool:
            expected=provenance['source_label']
            if counts[expected]>=50:continue
            raw=source.raw(provenance)
            try:
                mail=parse_mail(raw,inspect_html=True)
            except Exception:
                rejected['parse_error']+=1;continue
            full=mail.text+mail.html_text+mail.hidden_text
            body=mail.text.strip() or mail.html_text.strip()
            if len(full)>24000 or len(normalize(body))<80 or full.count('\ufffd')/max(1,len(full))>.03:
                rejected['same_quality_filter_as_v1']+=1;continue
            clean=derived(mail)
            sig=signature(clean)
            duplicate=any(sig[0]==previous[0] or (sig[1]|previous[1] and len(sig[1]&previous[1])/len(sig[1]|previous[1])>=.8)
                          for previous in signatures)
            if duplicate:
                rejected['restored_or_selected_template_overlap']+=1;continue
            signatures.append(sig);counts[expected]+=1;mails.append(clean)
            manifest.append({'id':clean.id,'split':'test','group_id':group['group_id'],
                             'content_hash':clean.content_hash,'provenance':provenance})
            truth[clean.id]={'id':clean.id,'expected':expected,'label_source':'published_source_annotation',
                            'review_status':'not_independently_adjudicated'}
            requests[clean.id]=digest(request_for(clean,CONTEXT,'v6'))
            if counts=={'risk':50,'clear':50}:break
    finally:source.close()
    if counts!={'risk':50,'clear':50}:raise ValueError(f'Not enough eligible unseen original groups: {dict(counts)}')
    audit={'created_at':now(),'purpose':'new heldout groups after HTML capability development',
        'selection_seed':'html-holdout-v1','selected_counts':dict(counts),'rejected_counts':dict(rejected),
        'eligible_original_groups':len(pool),'excluded_seen_groups':len(excluded_groups),
        'overlap_check':'excluded original v1 groups, plus exact template and 5-word-shingle Jaccard>=0.8 against all restored v2 and newly selected bodies',
        'source_counts':dict(Counter(x['provenance']['source']for x in manifest)),
        'limitations':['仅限可取得原始MIME的Nazario2024/2025与SpamAssassin正常来源，来源混杂因素仍存在。',
                      '未以模型结果筛选；并非真实企业在线邮件，也不是独立人工鉴定。','模型预训练数据污染和语义近重复仍未知。']}
    metadata={'evaluation_roles':{'test':'new_group_holdout'},'request_hashes':requests,
              'prior_group_file_sha256':hashlib.sha256((DEST/'groups.json').read_bytes()).hexdigest(),
              'excluded_groups_hash':digest(sorted(excluded_groups))}
    publish_corpus(target,manifest=manifest,mails=mails,labels=truth,context=CONTEXT,
                   freeze_metadata=metadata,artifacts={'audit':audit})
    print(json.dumps(audit,ensure_ascii=False,indent=2))


if __name__=='__main__':prepare()

"""Enrich the SAME frozen public cohort with bounded static HTML facts.

Reads originals only from archived source files, verifies their hashes, and never
follows URLs. The old test set is explicitly a seen regression set after P5.
"""
import hashlib
import json
import mailbox
import sys
import tarfile
from collections import Counter
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mailguard.evaluation import now
from mailguard.html_static import inspect_message, VERSION
from mailguard.public_corpus import DEST, SOURCES, redact
from mailguard.frozen_corpus import FrozenCorpus, identify_mail, publish_corpus


def sanitize(value):
    if isinstance(value,str):return redact(value)
    if isinstance(value,list):return [sanitize(x)for x in value]
    if isinstance(value,dict):return {key:sanitize(item)for key,item in value.items()}
    return value


class Originals:
    def __init__(self):self.boxes={};self.archives={}

    def raw(self,provenance):
        restored=provenance.get('restored_original')
        source=provenance['source']
        if restored or source.startswith('nazario'):
            year=restored['year'] if restored else int(source.removeprefix('nazario'))
            row=int(restored['original_row'] if restored else provenance['source_row'])
            expected=restored['original_raw_hash'] if restored else provenance['raw_hash']
            if year not in self.boxes:
                box=mailbox.mbox(SOURCES/f'nazario-{year}.mbox',create=False)
                self.boxes[year]=(box,list(box.iterkeys()))
            box,keys=self.boxes[year]
            raw=box.get_bytes(keys[row-1])
        elif source.startswith('spamassassin_'):
            category=source.removeprefix('spamassassin_')
            if category not in self.archives:
                self.archives[category]=tarfile.open(SOURCES/f'spamassassin-{category}-ham.tar.bz2','r:bz2')
            archive=self.archives[category]
            member=archive.getmember(provenance['source_row'])
            if not member.isfile() or member.size>5*1024*1024:raise ValueError('Unsafe source member')
            raw=archive.extractfile(member).read()
            expected=provenance['raw_hash']
        else:
            return None
        if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Original source hash mismatch')
        return raw

    def close(self):
        for box,_ in self.boxes.values():box.close()
        for archive in self.archives.values():archive.close()


def prepare(base=DEST.parent/'v2',target=DEST.parent/'v3-html'):
    if target.exists():raise ValueError('Frozen corpus exists; choose a new version')
    parent=FrozenCorpus(base)
    source=Originals()
    output=[];mappings=[];inputs=[];statuses=Counter()
    try:
        for split in ('dev','test'):
            for item,old,context in parent.cases(split):
                raw=source.raw(item['provenance'])
                facts=inspect_message(raw) if raw is not None else [{'source_id':'unavailable',
                    'status':'source_unavailable','inspector_version':VERSION,
                    'limitations':['仅有导出的字段，未取得唯一匹配的原始邮件，无法补做HTML静态检查。']}]
                for fact in facts:statuses[fact['status']]+=1
                data=old.model_dump()
                data.update(html_inspections=sanitize(facts),id='pending',content_hash='')
                # The already-redacted v2 text is unchanged; do not redact it twice.
                data['warnings']=['公开历史文本；地址和长号码等已做规则化脱敏，非完整匿名化。HTML静态检查覆盖见各来源；未访问链接或检查其他附件内容。']
                mail=identify_mail(data)
                inputs.append(mail)
                output.append({**item,'id':mail.id,'content_hash':mail.content_hash,'previous_id':old.id})
                mappings.append({'previous_id':old.id,'id':mail.id,'split':split,
                    'raw_available':raw is not None,'html_documents':len(facts) if raw else None,
                    'control_count':sum(f.get('counts',{}).get('controls',0)for f in facts)})
    finally:source.close()
    labels=parent.remap_labels(output)
    audit={'created_at':now(),'parent_version':base.name,'inspector_version':VERSION,
        'membership_preserved':True,'labels_preserved':True,'split_preserved':True,
        'raw_available':sum(m['raw_available']for m in mappings),'html_statuses':dict(statuses),
        'notes':['v2正文、链接、附件元数据不变；只增加来自原始MIME的静态HTML事实与准确的检查范围说明。',
                 '本构建器保守标记继承的测试分区为已见回归；独立测试需由调用方建立新冻结集合。']}
    publish_corpus(target,manifest=output,mails=inputs,labels=labels,context=context,
                   freeze_metadata={'evaluation_roles':{'dev':'development','test':'seen_regression'}},
                   artifacts={'audit':audit,'mapping':mappings})
    print(json.dumps(audit,ensure_ascii=False,indent=2))


if __name__=='__main__':prepare()

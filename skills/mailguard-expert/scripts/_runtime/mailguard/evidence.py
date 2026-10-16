"""Source-bound evidence IDs: only the program copies quotes, never the model."""
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict

FIELDS=('sender','reply_to','subject','text','html_text','hidden_text','links','attachments','html_inspections')


def catalog(email):
    entries=[]
    for field in FIELDS:
        source=email.get(field)
        if not source:
            continue
        if not isinstance(source,str):
            source=json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        for line in re.finditer(r'[^\r\n]+',source):
            if not line[0].strip():
                continue
            for offset in range(line.start(),line.end(),200):
                end=min(offset+220,line.end())
                quote=source[offset:end]
                if quote.strip():
                    entries.append({'id':f'E{len(entries):04d}','field':field,
                                    'start':offset,'end':end,'quote':quote})
    return entries


class AnchoredCheck(BaseModel):
    model_config=ConfigDict(extra='forbid')
    skill_id: str
    outcome: Literal['finding','no_finding','insufficient']
    evidence_ids: list[str]


class AnchoredDecision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    verdict: Literal['risk','clear','review']
    risk_types: list[str]
    skill_checks: list[AnchoredCheck]


def schema():
    result=AnchoredDecision.model_json_schema()
    result['properties'].update(explanation={'type':'string'},recommendation={'type':'string'})
    result['$defs']['AnchoredCheck']['properties']['note']={'type':'string'}
    return result


def presentation_status(value):
    return 'complete' if all(isinstance(value.get(k),str) and value[k].strip()
                             for k in ('explanation','recommendation')) else 'partial'


def expand(value,request):
    if not isinstance(value,dict):
        raise ValueError('决策JSON必须是对象')
    current=catalog(request['email'])
    if current!=request['evidence_catalog']:
        raise ValueError('证据目录与当前邮件不一致')
    checks=value.get('skill_checks')
    if not isinstance(checks,list) or any(not isinstance(c,dict)for c in checks):
        raise ValueError('技能检查结构无效')
    # Validate core decisions separately; presentation fields cannot erase them.
    core=AnchoredDecision.model_validate({'verdict':value.get('verdict'),'risk_types':value.get('risk_types'),
        'skill_checks':[{k:c.get(k)for k in ('skill_id','outcome','evidence_ids')} for c in checks]})
    by_id={e['id']:e for e in current}
    expanded=[]
    union=[]
    for check,raw in zip(core.skill_checks,checks):
        evidence=[]
        for eid in check.evidence_ids:
            if eid not in by_id:
                raise ValueError('未知原文证据编号')
            item={k:by_id[eid][k]for k in ('field','quote')}
            evidence.append(item)
            if item not in union:
                union.append(item)
        note=raw.get('note')
        expanded.append({'skill_id':check.skill_id,'outcome':check.outcome,'evidence':evidence,
                         'note':note if isinstance(note,str)and note.strip()else '模型未提供分项说明。'})
    def text(key,fallback):
        val=value.get(key)
        return val if isinstance(val,str)and val.strip()else fallback
    return {'verdict':core.verdict,'risk_types':core.risk_types,'skill_checks':expanded,'evidence':union,
            'explanation':text('explanation','模型未提供完整解释；分项判断和原文证据已单独保留。'),
            'recommendation':text('recommendation','请结合原文证据，通过独立可信渠道人工复核。'),
            'limitations':([('已对可读取HTML来源进行有界静态检查；检查覆盖与跳过原因见html_inspections。'
                             if request['email'].get('html_inspections') else
                             '本邮件没有可检查的HTML来源；未作HTML内容安全保证。')+
                            '未执行脚本、提交表单、访问URL或独立验证发件人。',
                            'PDF、Office、压缩包等其他附件内容未检查；静态未发现异常不等于无毒。']
                           if request['analysis_config'].get('html_static_analysis') else
                           ['未访问链接、检测附件或独立验证发件人；研判只基于提供的邮件数据。'])}

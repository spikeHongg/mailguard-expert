"""Post-run paired reports; reference labels are used for scoring only."""
import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from .evaluation import dataset, labels, score, now
from .prompt_lab import load_candidate


def pct(value):
    return '不适用' if value is None else f'{value:.1%}'


def state(row):
    return row['assessment']['verdict'] if row['status']=='ok' else row.get('error_code','not_attempted')


def summarize(path, output):
    r=json.loads(Path(path).read_text())
    c=load_candidate(r['candidate_id']);truth=labels()
    by={side:{row['mail_id']:row for row in r['reports'][side]['results']} for side in ('baseline','candidate')}
    paired={mid for mid in by['baseline'] if all(by[s][mid].get('source')!='not_attempted' for s in by)}
    wrong=lambda row,mid:row['status']!='ok' or row['assessment']['verdict']!=truth[mid]['expected']
    fixed=sorted(mid for mid in paired if wrong(by['baseline'][mid],mid) and not wrong(by['candidate'][mid],mid))
    regressed=sorted(mid for mid in paired if not wrong(by['baseline'][mid],mid) and wrong(by['candidate'][mid],mid))
    remaining=sorted(mid for mid in paired if wrong(by['baseline'][mid],mid) and wrong(by['candidate'][mid],mid))
    semantic=[mid for mid in sorted(paired) if all(by[s][mid]['status']=='ok' for s in by) and state(by['baseline'][mid])!=state(by['candidate'][mid])]
    cohorts={'全部100封':set(by['baseline']),
             '开发集':{m.id for _,m,_ in dataset('dev')},
             '原保留集（已见回归）':{m.id for _,m,_ in dataset('holdout')}}
    lines=['# 提示词同模型A/B对照','',f'报告时间：{now()}','',
        f'候选：`{c["id"]}`；SHA256：`{c["sha256"]}`。',
        f'反馈来源：{", ".join(c["payload"].get("feedback_sources",[]))}；生成来源：{c["provenance"].get("source")}。',
        f'请求模型：`{r["identity"]["model"]}`；通道：`{r["identity"]["endpoint"]}`。',
        f'生成参数：`{json.dumps(r["identity"]["generation_config"],ensure_ascii=False)}`。',
        f'运行：`{path}`；已尝试 {r["attempted"]}/{r["expected"]}；完整结束：{r["finished"]}；停止原因：{r["stop_reason"] or "无"}。','',
        '原版为未加补充说明的v3；候选仅增加冻结的优化说明。两版都真实调用同一型号，未以旧GLM-5.3成绩或回放代替基线。标签、技能文件、校验器及重试策略不变。','',
        '## 指标','', '|集合|提示词|有效结果|判断一致率|风险召回|正常误报|信息不足判对|','|---|---|---:|---:|---:|---:|---:|']
    cohort_metrics={}
    for cohort,ids in cohorts.items():
        cohort_metrics[cohort]={}
        for side,label in [('baseline','原版'),('candidate','候选')]:
            rows=[row for mid,row in by[side].items() if mid in ids]
            m=score(rows,truth);cohort_metrics[cohort][side]=m
            matrix=m['confusion_matrix'];review_n=sum(matrix['review'].values())
            lines.append(f'|{cohort}|{label}|{m["completed"]}/{m["count"]}|{pct(m["accuracy"])}|{m["tp"]}/{m["risk_count"]}（{pct(m["risk_recall"])}）|{matrix["clear"]["risk"]}/{m["benign_count"]}|{matrix["review"]["review"]}/{review_n}|')
    lines+=['','错误与未调用不从指标分母中剔除。正常误报为0不代表输出全部有效；需同时看有效结果和失败类型。开发/原保留集划分只用于分析，本100封已经多轮观察，不称为新盲测。','',
        f'固定综合分：原版 {r["scores"]["baseline"]:.4f} → 候选 {r["scores"]["candidate"]:.4f}。权重为判断一致率0.5、风险召回0.3、有效完成率0.2。','',
        '## 改善、退化与未解决','',f'两版均实际尝试 {len(paired)} 封；修复 {len(fixed)} 封，新增退化 {len(regressed)} 封，仍未解决 {len(remaining)} 封。未配对的未调用结果不列作新退化。','']
    for title,ids in [('修复',fixed),('新增退化',regressed),('仍未解决',remaining)]:
        lines += [f'### {title}','', '|邮件|参考判断|原版|候选|','|---|---|---|---|']
        for mid in ids: lines.append(f'|{mid}|{truth[mid]["expected"]}|{state(by["baseline"][mid])}|{state(by["candidate"][mid])}|')
        if not ids: lines.append('|—|—|—|—|')
        lines.append('')
    lines += ['## 两版均有效但判断发生变化','']
    if not semantic: lines.append('没有观察到此类变化。')
    for mid in semantic:
        lines += [f'- {mid}：{state(by["baseline"][mid])} → {state(by["candidate"][mid])}；参考 {truth[mid]["expected"]}。',
                  '  - 原版说明：'+by['baseline'][mid]['assessment']['explanation'].replace('\n',' '),
                  '  - 候选说明：'+by['candidate'][mid]['assessment']['explanation'].replace('\n',' ')]
    lines += ['', '## 调用与输出问题','']
    runtime={}
    for side,label in [('baseline','原版'),('candidate','候选')]:
        rows=list(by[side].values());attempts=[a for row in rows for a in row.get('attempts',[])]
        errors=dict(Counter(row.get('error_code','not_attempted') for row in rows if row['status']!='ok'))
        validations=dict(Counter(a['validation'] for a in attempts if a.get('validation') not in (None,'passed')))
        runtime[side]={'attempts':len(attempts),'errors':errors,'validations':validations,
            'repairs':sum(any(a.get('validation') not in (None,'passed') for a in row.get('attempts',[])) for row in rows),
            'prompt_tokens':sum((a.get('usage') or {}).get('prompt_tokens',0) or 0 for a in attempts),
            'completion_tokens':sum((a.get('usage') or {}).get('completion_tokens',0) or 0 for a in attempts),
            'actual_models':sorted({row['model'] for row in rows if row['status']=='ok'})}
        d=runtime[side]
        lines += [f'### {label}','',f'- 实际返回型号：{d["actual_models"]}；已记录请求尝试：{d["attempts"]}；触发格式修复的邮件：{d["repairs"]}。',
                  f'- 最终失败类型：{errors}。',f'- 校验拒绝原因（按响应次数）：{validations}。',
                  f'- 可知输入/输出tokens：{d["prompt_tokens"]}/{d["completion_tokens"]}。','']
    common=[mid for mid in paired if all(by[s][mid]['status']=='ok' and 'elapsed_seconds' in by[s][mid] for s in by)]
    if common:
        means={side:statistics.mean(by[side][mid]['elapsed_seconds'] for mid in common) for side in by}
        lines.append(f'共同有效的{len(common)}封中，平均端到端耗时：原版{means["baseline"]:.2f}秒、候选{means["candidate"]:.2f}秒；含修复与重试，不能等同于纯推理速度。')
    lines += ['','未返回usage的失败或中断请求用量未知；不据已知tokens声称完整费用。无效原始响应未存档，只能根据校验原因定位问题类别，不能证明被拒绝响应的完整语义判断。','',
        '## 采用状态与解释边界','',
        f'本次GLM完整回归门槛：{r["gate_passed"]}。这里只测试GLM，不改动当前启用提示词；项目的双模型启用条件仍独立生效。',
        '这是一轮同模型、同数据、单次采样的工程对照。AI研发反馈与样例设计不能算作独立人工奖励，不能宣称统计显著提升或真实企业识别率。候选在看到本轮结果后保持冻结，下一次修改应另建候选与轮次。']
    out=Path(output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text('\n'.join(lines)+'\n')
    summary={'candidate_id':c['id'],'finished':r['finished'],'attempted':r['attempted'],'paired':len(paired),
             'fixed':fixed,'regressed':regressed,'remaining':remaining,'semantic_changes':semantic,
             'cohort_metrics':cohort_metrics,'runtime':runtime,'scores':r['scores'],'glm_gate_passed':r['gate_passed']}
    out.with_suffix('.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    return summary


def main():
    p=argparse.ArgumentParser(description='输出同模型提示词真实对照摘要')
    p.add_argument('report');p.add_argument('--output',required=True)
    args=p.parse_args();s=summarize(args.report,args.output)
    print(json.dumps({k:s[k] for k in ('candidate_id','finished','paired','fixed','regressed','remaining')},ensure_ascii=False))

if __name__=='__main__': main()

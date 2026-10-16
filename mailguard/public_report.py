"""Summarize immutable public runs; no model calls and no benchmark label edits."""
import argparse
import json
import math
from pathlib import Path
from .config import ROOT
from .public_corpus import DEST
from .evaluation import now


def wilson(success, count):
    if not count:
        return None
    z=1.96
    p=success/count
    d=1+z*z/count
    c=(p+z*z/(2*count))/d
    r=z*math.sqrt(p*(1-p)/count+z*z/(4*count*count))/d
    return [round(c-r,4),round(c+r,4)]


def write_report(paths, output=ROOT/'docs/public-benchmark-results.md', base=DEST):
    runs=[json.loads(Path(p).read_text()) for p in paths]
    audit=json.loads((Path(base)/'audit.json').read_text())
    freeze=json.loads((Path(base)/'freeze.json').read_text())
    fmt=lambda x:'不适用' if x is None else f'{x:.1%}'
    heldout=[r for r in runs if r['split']=='test']
    conclusion=[]
    if heldout:
        final=heldout[-1]
        m=final['metrics']
        conclusion=['## 当前验收结论','',
            f'保留测试集：召回率 **{fmt(m["risk_recall"])}（{m["tp"]}/{m["risk_count"]}）**，精确率 **{fmt(m["risk_precision"])}（{m["tp"]}/{m["tp"]+m["fp"]}）**，有效完成 {m["completed"]}/{m["count"]}，运行失败 {m["errors"]}。',
            '**'+('本次样本点估计达到双90%。' if final['meets_point_targets'] else '尚未达到召回率与精确率均≥90%的目标。')+'**',
            '格式成功不等于排除了攻击。测试后实际威胁复核与附件内容盲区见 [风险缺口](public-test-risk-gaps.md)；状态设计见 [格式失败诊断](format-failure-diagnosis.md)。','']
    lines=['# 公开真实邮件 GLM 5.3 Flash 评测与提示词迭代','',f'更新时间：{now()}','',
           *conclusion,
           '## 数据冻结','',
           f'- 原始记录 {sum(audit["source_counts"].values())} 条；剔除 {sum(audit["excluded_counts"].values())} 条；去重合并 {audit["duplicate_records_removed"]} 条；留下 {audit["eligible_groups"]} 组。',
           '- 冻结开发集60封（30风险、30正常）；测试集100封（50风险、50正常）。按重复组取一个代表，组之间不跨集合。',
           '- 风险来源：Nazario 2024/2025、PhishNChips真实子集、AURA Nazario；全部属于同一Nazario来源家族。正常对照：SpamAssassin easy/hard ham。',
           '- 样例保留原语言，未将英文翻译冒充原生中文；外部请求不含来源名、标签或判定答案。地址本地部分、长号码、URL查询值已做规则脱敏，不保证完整匿名化。',
           '- 原始数据与完整逐封记录均在忽略目录 data/private/ 内；未连接公司IMAP。',
           f'- 冻结manifest SHA256：`{freeze["manifest_hash"]}`。',
           f'- 本段数据版本：{Path(base).name}；各轮版本独立列示，恢复范围与未匹配记录以audit为准。',
           '', '## 固定评测条件','',
           '- 模型、端点与并发以各轮运行记录为准；保存实际返回模型、请求哈希、技能版本、原始响应、校验错误和tokens。',
           '- 不同输出契约、输入恢复与检测能力变更分别注明，不能把差异全部归因为提示词语义提升。',
           '- 模型原文未通过校验的risk仅作诊断，不补算成功。',
           '- v5由程序提供原文编号，模型只引用编号；原文恢复和报告组装由程序完成。核心判断和证据仍严格校验，单独标记报告文字不完整。每个输出最多修复一次，临时网络重试最多两次。',
           '- 连续300秒无新邮件结果落盘则取消在途请求并报错；总运行可超过5分钟。错误、review和未调用风险邮件均不算召回。',
           '', '## 各轮结果','',
           '|轮次|集合/数据/契约|完成/总数|召回率|精确率|正常误报|错误数|两项≥90%|',
           '|---|---|---:|---:|---:|---:|---:|---|']
    for r in runs:
        m=r['metrics']
        lines.append(f'|{r["id"]}|{r["corpus"]}/{r["split"]}/{r.get("corpus_version","v1")}/{r["prompt_version"]}|{m["completed"]}/{m["count"]}|{fmt(m["risk_recall"])} ({m["tp"]}/{m["risk_count"]})|{fmt(m["risk_precision"])} ({m["tp"]}/{m["tp"]+m["fp"]})|{m["fp"]}/{m["benign_count"]}|{m["errors"]}|{"是（本集点估计）" if r["meets_point_targets"] else "否"}|')
    for r in runs:
        m=r['metrics']
        lines += ['',f'## {r["id"]}','',
                  f'- 实际返回模型：{r["models_returned"]}；完整调用完成：{r["finished"]}；停止原因：{r["stop_reason"]}。',
                  f'- 已收到响应：{r["runtime"]["responses"]}；输入/输出tokens：{r["runtime"]["prompt_tokens"]}/{r["runtime"]["completion_tokens"]}。超时无usage部分不计入已知用量。',
                  f'- 输出校验失败原因（按响应计）：{r["runtime"]["validation_failures"]}。',
                  f'- 召回率95% Wilson区间：{wilson(m["tp"],m["risk_count"])}；精确率区间：{wilson(m["tp"],m["tp"]+m["fp"])}。',
                  f'- 结果目录：`data/private/public_benchmark/runs/{r["id"]}/`。']
        if r.get('comparison'):
            for key,title in [('fixed','修复'),('regressed','新增退化'),('remaining','仍未解决')]:
                ids=r['comparison'][key]
                lines.append(f'- {title} {len(ids)} 封：'+(', '.join(ids) or '无')+'。')
        lines+=['','|参考标签|risk|clear|review|error|','|---|---:|---:|---:|---:|']
        for expected,counts in m['confusion_matrix'].items():
            lines.append(f'|{expected}|{counts["risk"]}|{counts["clear"]}|{counts["review"]}|{counts["error"]}|')
        lines+=['','未匹配来源标签或运行失败：']
        lines += [f'- {c["id"]}：参考 {c["expected"]} → 结果 {c["predicted"]}。' for c in m['issues']] or ['- 无。']
    lines+=['','## 解释边界','',
            '- 参考标签采用公开来源注释，未对所有邮件逐封独立人工鉴定。Nazario可能有误标；SpamAssassin的ham不等同保证无任何安全风险。争议样例保留，不依据模型结果改标来凑90%。',
            '- 正常样例与风险样例存在来源、年代差异；旧公开语料可能已进入基础模型训练。这里的测试集仅指本项目调提示前未使用的保留集。',
            '- 本轮基准是从经过长度/解析质量筛选及模板去重后的记录抽样，不覆盖所有真实邮件或图片/附件攻击。英文公开集成绩不能替代中文企业办公效果。',
            '- 目标为本次样本的召回率与精确率点估计均≥90%，并不代表总体性能的95%置信下界≥90%。',
            '- 开发集用于改提示，不能作为独立泛化证明。测试集一旦用于新一轮改提示，必须标注已见，另留新的测试集。']
    Path(output).write_text('\n'.join(lines)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('reports',nargs='+')
    p.add_argument('--output',type=Path,default=ROOT/'docs/public-benchmark-results.md')
    p.add_argument('--base',type=Path,default=DEST)
    a=p.parse_args()
    write_report(a.reports,a.output,a.base)

import json
import time
import os
import asyncio
from contextlib import nullcontext
from pathlib import Path
from openai import AsyncOpenAI, APITimeoutError, APIConnectionError, APIStatusError
from pydantic import ValidationError
from .models import Assessment, Evidence, AnalysisError
from .config import ROOT, provider_config, LIVE_PROVIDERS, access_mode
from .skills import skill_trace
# Compatibility exports preserve existing callers without duplicating the contract.
from .contracts import PROMPTS, canonical, digest, features, request_for
from .contracts import decode_response as validate_assessment
from .contracts import read_replay_record, response_report_status

def rule_baseline(mail):
    # Deliberately simple, fixed comparison; not used as an AI fallback.
    patterns = [('credential_phishing', '验证码'), ('credential_phishing', '密码'),
                ('payment_fraud', '转账'), ('payment_fraud', '收款账户'),
                ('attachment_lure', '启用宏'), ('impersonation_remote_control', '远程控制'),
                ('data_exfiltration', '客户名单'), ('extortion_callback', '勒索'),
                ('prompt_injection', '忽略之前')]
    for field in ('subject', 'text', 'html_text'):
        text = getattr(mail, field)
        for category, word in patterns:
            if word in text:
                return Assessment(verdict='risk', risk_types=[category], evidence=[Evidence(field=field, quote=word)],
                                  explanation='固定关键词基线命中：'+word, recommendation='人工核实邮件请求。',
                                  limitations=['规则基线不能理解完整上下文；未扫描附件或访问链接。'])
    return Assessment(verdict='clear', risk_types=[], evidence=[], explanation='固定关键词未命中。',
                      recommendation='仍应按照公司流程处理。', limitations=['未命中规则不代表安全。'])

def api_options(provider, version='v3'):
    limit=4500 if version in ('v3','v4','v5','v6') else 2500
    if provider=='deepseek':
        return {'max_tokens':limit,'extra_body':{'thinking':{'type':'disabled'}}}
    if provider=='glm':
        thinking=os.getenv('GLM_THINKING','enabled')
        if thinking not in ('enabled','disabled'):
            raise AnalysisError('GLM_THINKING 必须为 enabled 或 disabled。',code='invalid_config')
        if provider_config('glm')['model'].lower().startswith('glm-5.3') and thinking=='disabled':
            raise AnalysisError('GLM-5.3 系列需要思考模式，请设置 GLM_THINKING=enabled。',code='invalid_config')
        try:
            glm_limit=int(os.getenv('GLM_MAX_TOKENS',str(limit)))
            if not 1<=glm_limit<=131072: raise ValueError()
        except ValueError:
            raise AnalysisError('GLM_MAX_TOKENS 必须为 1 至 131072 的整数。',code='invalid_config') from None
        extra={'thinking':{'type':thinking}}
        if provider_config('glm')['model'].lower().startswith('glm-5.3'):
            effort=os.getenv('GLM_REASONING_EFFORT','low')
            if effort not in ('low','high','max'):
                raise AnalysisError('GLM-5.3 的 GLM_REASONING_EFFORT 必须为 low/high/max。',code='invalid_config')
            extra['reasoning_effort']=effort
        return {'max_tokens':glm_limit,'extra_body':extra}
    if provider=='gpt':
        return {'max_completion_tokens':limit}
    raise AnalysisError('未知模型提供方。',code='invalid_provider')

async def completion_with_cancellation(cfg, kwargs, cancel_event,observer=None):
    # The batch owns the progress clock; HTTP bytes and individual attempts do not reset it.
    async with AsyncOpenAI(api_key=cfg['api_key'], base_url=cfg['base_url'], timeout=600, max_retries=0) as client:
        with observer.track() if observer else nullcontext():
            if cancel_event is None:
                return await client.chat.completions.create(**kwargs)
            async def cancellation():
                while not cancel_event.is_set():
                    await asyncio.sleep(0.05)
            request=asyncio.ensure_future(client.chat.completions.create(**kwargs))
            cancelled=asyncio.create_task(cancellation())
            try:
                done,_=await asyncio.wait({request,cancelled},return_when=asyncio.FIRST_COMPLETED)
                if request in done:
                    return request.result()
                raise AnalysisError('批次连续无新邮件完成，已取消在途请求。',code='progress_timeout')
            finally:
                for task in (request,cancelled):
                    if not task.done(): task.cancel()
                await asyncio.gather(request,cancelled,return_exceptions=True)

def api_assess(request, provider, client=None, sleep=time.sleep, cancel_event=None, response_sink=None,
               observer=None,max_transport_retries=2):
    if type(max_transport_retries)is not int or not 0<=max_transport_retries<=2:
        raise AnalysisError('重试上限必须为0至2。',code='invalid_config')
    cfg = provider_config(provider)
    if not cfg['api_key'] or not cfg['model']:
        raise AnalysisError(f'{provider} 待配置：请在本机 .env 填写密钥和模型名。',code='not_configured')
    payload = {k: v for k, v in request.items() if k not in ('prompt','skills')}
    messages = [{'role':'system', 'content':request['prompt']}, {'role':'user', 'content':canonical(payload)}]
    transport_retries = 0
    attempts = []
    for repair in range(2):
        while True:
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise AnalysisError('批次连续无新邮件完成，已停止后续请求。',code='progress_timeout')
                kwargs=dict(model=cfg['model'], messages=messages,
                    response_format={'type':'json_object'}, stream=False,
                    **api_options(provider,request['prompt_version']))
                if client is not None:
                    with observer.track()if observer else nullcontext():
                        response=client.chat.completions.create(**kwargs)
                else:
                    response=asyncio.run(completion_with_cancellation(cfg,kwargs,cancel_event,**({'observer':observer}if observer else{})))
                break
            except AnalysisError as e:
                if e.code=='progress_timeout':
                    attempts.append({'status':'cancelled','code':'progress_timeout','usage':None})
                    raise AnalysisError(str(e),code=e.code,attempts=attempts) from None
                raise
            except (APITimeoutError, APIConnectionError, TimeoutError) as e:
                attempts.append({'status':'transport_error','code':'timeout' if isinstance(e,(APITimeoutError,TimeoutError)) else 'connection'})
                if transport_retries >= max_transport_retries:
                    raise AnalysisError('模型网络请求失败，已达到重试上限。',code='transport_error',attempts=attempts) from None
            except APIStatusError as e:
                body=e.body if isinstance(e.body,dict) else {}
                error=body.get('error',body)
                code=str(error.get('code','')) if isinstance(error,dict) else ''
                code=code if code.isalnum() and len(code)<30 else ''
                attempts.append({'status':'http_error','http_status':e.status_code,'provider_error_code':code or None})
                if provider=='glm' and code=='1113':
                    raise AnalysisError('智谱当前调用通道额度不可用（1113），请核对通用API余额与Coding Plan端点是否匹配。',code='provider_1113',attempts=attempts) from None
                if e.status_code not in (408, 429) and e.status_code < 500:
                    raise AnalysisError(f'模型请求被拒绝（HTTP {e.status_code}），请检查本机配置。',code=f'http_{e.status_code}',attempts=attempts) from None
                if transport_retries >= max_transport_retries:
                    raise AnalysisError('模型服务暂不可用，已达到重试上限。',code=f'http_{e.status_code}',attempts=attempts) from None
            except (ValueError,TypeError):
                raise AnalysisError('模型客户端配置无效，请检查本机接口地址和配置。',code='invalid_config',attempts=attempts) from None
            transport_retries += 1
            if cancel_event is not None:
                cancel_event.wait(transport_retries)
            else:
                sleep(transport_retries)
        try:
            choice = response.choices[0]
            if response_sink is not None:
                response_sink({'content':choice.message.content, 'finish_reason':choice.finish_reason,
                               'model':response.model})
            attempts.append({'status':'response','finish_reason':choice.finish_reason,
                'request_id':getattr(response,'_request_id',None) or getattr(response,'request_id',None),
                'usage':response.usage.model_dump() if response.usage else None})
            if choice.finish_reason != 'stop' or getattr(choice.message, 'refusal', None):
                raise ValueError('响应未正常完成')
            value=json.loads(choice.message.content)
            if isinstance(value,dict) and value.get('verdict') in ('risk','clear','review'):
                attempts[-1]['proposed_verdict']=value['verdict']
            assessment = validate_assessment(value, request)
            attempts[-1]['validation'] = 'passed'
            statuses={'decision_status':'validated','evidence_status':'verified',
                      'report_status':response_report_status(value,request)}
            return assessment, {'model': response.model, 'usage': response.usage.model_dump() if response.usage else None,
                                **statuses,
                                'model_requested':cfg['model'],'system_fingerprint':getattr(response,'system_fingerprint',None),
                                'attempts':attempts, 'request_count':len(attempts),
                                'access_mode':access_mode(provider,cfg),
                                'transport_config':{'read_timeout_seconds':600,'batch_cancellation':cancel_event is not None},
                                'generation_config':api_options(provider,request['prompt_version'])}
        except (ValidationError, ValueError, TypeError, IndexError, AttributeError) as e:
            reason = ('schema_invalid' if isinstance(e,ValidationError) else
                      'invalid_json' if isinstance(e,json.JSONDecodeError) else
                      str(e) if type(e) is ValueError else 'response_structure_invalid')
            if attempts and attempts[-1]['status']=='response':
                attempts[-1]['validation'] = reason
            else:
                attempts.append({'status':'invalid_response','validation':reason})
            if repair:
                raise AnalysisError('模型输出格式或证据校验失败，修复一次后仍不符合要求。',code='invalid_output',attempts=attempts) from None
            messages.append({'role':'user', 'content':'上一次输出未通过格式或原文证据校验。请重新按 schema 输出 JSON，证据必须逐字来自提供的邮件字段。'})
    raise AnalysisError('模型输出失败。')

def analyze(mail, context, provider='replay', version='v2', replay_dir=None, client=None, cancel_event=None, candidate_id=None):
    request = request_for(mail, context, version, candidate_id)
    key = digest(request)
    started = time.perf_counter()
    meta = {}
    if provider == 'rules':
        a = rule_baseline(mail)
        source = 'rule_baseline'
    elif provider == 'replay':
        path = Path(replay_dir or ROOT / 'data/replays') / f'{key}.json'
        if not path.is_file():
            raise AnalysisError('无匹配回放：邮件、上下文或提示词已变化，需要重新研判。')
        try:
            record = json.loads(path.read_text())
            a, meta = read_replay_record(record,request)
        except (ValueError, KeyError, TypeError):
            raise AnalysisError('回放记录校验失败。') from None
        source = 'assistant_replay'
    elif provider in LIVE_PROVIDERS:
        a, meta = api_assess(request, provider, client=client, cancel_event=cancel_event)
        source = 'live_api'
    else:
        raise AnalysisError('未知分析来源。')
    return {'mail_id': mail.id, 'status': 'ok', 'provider': provider, 'source': source,
            'prompt_version': version, 'request_hash': key, 'assessment': a.model_dump(),
            'elapsed_seconds': round(time.perf_counter()-started, 4),
            'skills_used':skill_trace(request) if provider != 'rules' else [],
            **({'candidate_id':candidate_id} if candidate_id else {}), **meta}

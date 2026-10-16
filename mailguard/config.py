import os
from pathlib import Path
from dotenv import load_dotenv
from .models import AnalysisError

ROOT = Path(__file__).resolve().parent.parent
if os.getenv('MAILGUARD_ENV_FILE'):
    load_dotenv(Path(os.environ['MAILGUARD_ENV_FILE']).expanduser(), override=False)
elif (ROOT / 'AGENTS.md').exists():
    load_dotenv(ROOT / '.env', override=False)
LIVE_PROVIDERS = ('deepseek', 'glm', 'gpt')
MAX_CONCURRENCY=20

def provider_concurrency(provider,override=None):
    defaults={'deepseek':20,'glm':5,'gpt':2,'replay':1,'rules':1}
    if provider not in defaults:raise AnalysisError('未知分析来源。',code='invalid_provider')
    value=override if override is not None else os.getenv(provider.upper()+'_CONCURRENCY',str(defaults[provider]))
    try:
        if isinstance(value,bool) or isinstance(value,float):raise ValueError()
        workers=int(value)
        if not 1<=workers<=MAX_CONCURRENCY:raise ValueError()
        return workers
    except (ValueError,TypeError):
        raise AnalysisError('并发数必须是1至20的整数。',code='invalid_config') from None

def progress_timeout_seconds():
    try:
        seconds=int(os.getenv('EVAL_PROGRESS_TIMEOUT_SECONDS','300'))
        if not 1<=seconds<=3600: raise ValueError()
        return seconds
    except ValueError:
        raise AnalysisError('EVAL_PROGRESS_TIMEOUT_SECONDS 必须为1至3600的整数。',code='invalid_config') from None

def access_mode(provider, config):
    from urllib.parse import urlsplit
    return 'coding_plan' if provider=='glm' and '/api/coding/' in urlsplit(config['base_url']).path else 'standard_api'

def provider_config(provider):
    if provider == 'deepseek':
        return {'api_key': os.getenv('DEEPSEEK_API_KEY', ''),
                'base_url': 'https://api.deepseek.com',
                'model': os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')}
    if provider == 'glm':
        return {'api_key': os.getenv('GLM_API_KEY', ''),
                'base_url': os.getenv('GLM_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4/'),
                'model': os.getenv('GLM_MODEL', 'glm-5.3-flash')}
    if provider == 'gpt':
        return {'api_key': os.getenv('GPT_API_KEY', ''),
                'base_url': os.getenv('GPT_BASE_URL', 'https://api.openai.com/v1'),
                'model': os.getenv('GPT_MODEL', '')}
    raise AnalysisError('未知模型提供方。',code='invalid_provider')

def configuration_status():
    return {p: all(provider_config(p).get(k) for k in ('api_key', 'model')) for p in ('deepseek', 'glm')}

# 本地独立API

服务不依赖WorkBuddy。共享核心 `mailguard.expert.Expert` 负责v6+C1请求、9项技能、HTML静态事实、证据校验和私有持久化；FastAPI只提供HTTP适配。默认使用最新已验证的C1提示，也可显式选择原v6。旧网页和历史回放请求保持原样。

## 启动和配置

按根目录INSTALL选择service模式和IMAP初始配置。安装器返回 `env_file`、`commands.start/status/stop`，用户在本机填写配置后，使用返回的完整启动命令。新装默认配置为工作区 `.mailguard/.env`，状态为 `.mailguard/state/`。

`check-imap`（安装器的 `commands.check_imap`）只验证TLS、认证和只读文件夹，返回获取邮件数0；`doctor`仅为离线自检。模型API需用模拟邮件实际调用后才标记已验证。

固定监听 `127.0.0.1`，单进程，无访问日志。`/health`公开且不返回邮箱、配置或路径；其他接口需 `Authorization: Bearer TOKEN`，令牌来自本机私有状态目录的 `service.token`。首次启动自动生成，不向终端打印。此入口供本机应用调用；远程部署所需的TLS、网络入口和多进程调度不属于当前服务契约。

DeepSeek/GLM并发从.env读取，默认20/5，上限20；本服务进程内合计限制，超额429且任务仍为prepared。其他命令或独立服务进程不共享该限额。

## HTTP契约

|方法与路径|请求|结果|
|---|---|---|
|`GET /health`|无|服务存活；不代表上游模型可用|
|`GET /v1/capabilities`|Bearer|离线能力、配置是否填写、模型并发|
|`GET /v1/openapi.json`|Bearer|完整机器可读接口schema|
|`POST /v1/prepare`|`sample`（m002/m026/m031）或`eml_base64`二选一；可选`context`、`profile`|id、request_hash、完整请求；status=prepared|
|`POST /v1/jobs/{id}/submit`|request_hash、response；可选host、model|宿主输出经过证据校验的结果|
|`POST /v1/jobs/{id}/analyze`|provider=deepseek/glm/gpt|调用配置API并保存真实结果；同步等待|
|`GET /v1/jobs/{id}`|Bearer|任务状态、结果、失败记录数和人工复核|
|`POST /v1/jobs/{id}/review`|verdict、note|追加人工复核；原始研判不改写|
|`POST /v1/imap/read`|limit=1..50，可选context|显式只读获取并prepare；不会提交给模型|

`profile`可为 `v6-c1`（默认）或 `v6`。`.eml`解码后上限5MiB，HTTP请求体上限8MiB；接口不接受服务器上的任意文件路径。背景默认空对象，无模拟公司的白名单。

`analyze`需要调用方配置至少310秒的HTTP超时；单封任务连续300秒没有完成结果即取消并保存 `progress_timeout`。本接口一个任务只有一封邮件；历史批量评测仍按“整批连续5分钟无新邮件终态”运行，整批总时间可超过5分钟。

## 可直接运行的调用示例

从仓库根目录执行以下Python；只用内置模拟邮件，Key由后端读取：

```python
import json
from pathlib import Path
from urllib.request import Request, urlopen

base = 'http://127.0.0.1:8765'
token = Path('.mailguard/state/service.token').read_text().strip()

def call(path, data=None):
    request = Request(base+path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=310) as response:
        return json.load(response)

job = call('/v1/prepare', {'sample': 'm002'})
result = call('/v1/jobs/'+job['id']+'/analyze', {'provider': 'deepseek'})
print(json.dumps(result, ensure_ascii=False, indent=2))
```

批量客户端可并行准备多个任务，每个任务调用一次analyze，按提供方并发限制派发；收到429时稍后重试原prepared任务。已运行或已有结果的ID返回409，新的研判应重新prepare。模型超时、鉴权失败或输出无效会保存独立error，不会变成clear。

## 状态与来源

`prepared → running → ok/error`，宿主提交为 `prepared → ok`。无效宿主输出记录为失败尝试，任务保留prepared以便修正；相同结果重复提交幂等，不同结果不能覆盖旧结论。源请求及哈希绑定到各自ID，旧邮件结果不可套用新邮件。

`source=host_agent`保存宿主声明和未验证模型名；`source=live_api`保存提供方实际返回的模型、usage、请求次数和耗时。回放仍走原入口并保留其来源。API成功仅说明结构与原文证据可验证，语义判断仍需复核。

服务被强制中止时，未结束任务可能保留running；get会显示未完成，不据此输出安全结论。保留记录，重新prepare再分析。私有记录由SQLite保存，CLI导出的请求文件也在私有目录。

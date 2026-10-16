# 安装、运行与故障SOP

## 安装

取得用户提供的GitHub仓库，克隆到用户工作区，先阅读根目录 `AGENTS.md` 和 `INSTALL.md`。在仓库根目录执行：

```sh
python3.12 scripts/install.py --workspace "WorkBuddy工作区绝对路径"
```

安装器创建/复用仓库 `.venv`，安装Python依赖，复制完整Skill到工作区 `.codebuddy/skills/mailguard-expert/`，生成本机 `runtime.json` 并自检。默认项目级安装；用户明确需要所有项目可用时使用 `--scope user`。保留仓库，安装后的入口仍需要这个Python运行环境。

已有 `.env` 和私有数据保留；安装器管理的文件升级可加 `--update`。若仅通过 `npx skills add` 获得本Skill，仍需按以上流程安装仓库后端。不要把SKILL.md安装成功当作完整安装完成。

## 密钥与模式

默认宿主研判无需API Key。独立API在仓库 `.env` 中填写 `DEEPSEEK_API_KEY` 或 `GLM_API_KEY`，不把密钥写进聊天、命令参数或Skill。通过离线doctor只能判断是否已填写，真实可用性要用模拟邮件单独验证。

已准备任务，独立API分析：

```sh
python3 scripts/mailguard.py analyze --job "ID" --provider deepseek
```

启动独立HTTP服务：

```sh
python3 scripts/mailguard.py serve --port 8765
```

服务只监听 `127.0.0.1`，Bearer令牌位于状态目录 `service.token`，不在终端打印。HTTP契约见仓库 `docs/local-api.md`；Key仅由后端读取。当前默认DeepSeek20、GLM5并发，修改.env相应 `*_CONCURRENCY` 后重启。单进程服务限制所有HTTP分析请求的合计并发，满额返回429，任务保持待执行。

## IMAP

用户明确要求接邮箱时，先让其在本机 `.env` 填写服务器、TLS端口、完整邮箱用户名、客户端专用密码和文件夹。`fetch-imap`仅显式执行，使用只读文件夹和不标记已读的读取方式。输出任务请求后再按主Skill研判；模型不会自动选择邮箱操作。

## 故障与完成标准

|状态|动作|
|---|---|
|`runtime_missing/runtime_invalid`|根据仓库INSTALL重新安装定位运行环境，保留.env和数据|
|`not_configured`|在本机填写所选模式必需的配置，再用新模拟任务验证|
|`invalid_output`|依据本任务schema/证据编号修正一次；持续失败则明确报告无有效判断|
|`request_mismatch/request_changed`|重新prepare新任务，完整读取新请求后重新研判|
|`job_conflict`|get查看已有结果；需要重跑时prepare新任务，保留原记录|
|`provider_busy` / HTTP429|等待当前任务结束后重试待执行任务，保持并发限制|
|`progress_timeout`|单封实时任务连续5分钟无终态即失败；查看错误后创建新任务重试|
|`file_access_denied/filesystem_error`|报告宿主文件权限或写入错误，保留沙箱与审批配置，交由用户或项目修复|
|服务/进程中断留下running|get显示未完成，不能据此判断安全；保留旧任务并prepare新任务|

安装完成：离线doctor通过、WorkBuddy发现Skill、实际模拟邮件经过prepare→当前宿主新研判→submit校验→证据展示。前三者以外的真实API、真实IMAP、Windows运行分别保留独立验证状态。人工复核通过review追加；原始结论不覆盖。AI反馈不写成human_review。

宿主限制文件或命令访问时保持其权限机制；不要移除宿主注入的PYTHONPATH、禁用沙箱shim或降低审批要求来通过自检。保留具体错误与任务状态，先修复项目兼容性再重试。

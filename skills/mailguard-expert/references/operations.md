# 运行与故障SOP

安装、IMAP初始配置、升级及模式切换先读 [安装SOP](installation.md)。以下命令均接在主SKILL指定的脚本与 `--workspace` 后。

## 复核与状态

- `get --job ID`：读取已保存状态和结果。
- `review --job ID --verdict risk|clear|review --note "说明"`：只有用户提供实际复核意见时才追加人工记录。AI研发意见保持其真实来源。
- `doctor`：离线资源、解析、证据契约和存储检查；不连接API或IMAP。
- `check-imap`：用户确认配置后，只验证TLS、登录和只读文件夹，邮件获取数为0。
- `fetch-imap --limit 3`：用户明确请求后读取，随后仍由宿主研判与submit。

配置及邮件保存在工作区 `.mailguard/` 或显式指定的旧配置/状态目录；其中值只用于本机程序。Skill更新不搬动这些数据。

## 独立服务

服务模式安装后使用安装器返回的启动和状态命令；终端Ctrl+C停止。HTTP固定监听127.0.0.1，Bearer令牌在私有状态目录 `service.token`，不打印到终端。命令行参数只传配置文件路径。

服务侧保留 `prepare / analyze --provider deepseek|glm / get / review`，通过配置的模型API研判。HTTP契约见源码 `docs/local-api.md`。Skill入口自身只提供宿主研判；需要API时按安装SOP增加service模式。

默认DeepSeek20、GLM5并发，单进程上限20；修改.env后重启。每封任务连续5分钟无完成结果会失败，批量评测沿用整批连续5分钟无新终态的规则。

## 错误处理

命令失败时读取 `error_code` 和 `recovery.next_steps`。停止当前失败步骤，保留已完成的结果，不继续执行依赖它的步骤。`retryable=true` 表示问题解决后可以重试，不是无限自动重试授权。API内部有限重试耗尽后，由宿主向用户说明。

向用户依次汇报：**已完成什么 → 哪一步失败/尚未验证 → 最小修复动作 → 修复后运行什么 → 现在仍能用什么**。附任务ID或安装命令；不附密码、令牌、完整.env或真实邮件正文。模式/IMAP暂未配置时可继续模拟邮件和.eml；模型API未配置时独立服务只报告待配置。

配置错误、鉴权失败和权限拒绝不自动重试。网络或限流恢复后只重试未完成任务；成功结果不覆盖。宿主输出只允许修正一次；仍失败交人工复核，不能换低标准或修改哈希让结果通过。连接错误、格式错误和输出错误均不等于邮件存在风险。

|错误/状态|动作|
|---|---|
|needs_input|向用户展示安装模式、IMAP或提供方问题，取得选择再执行|
|installation_error|核对完整Skill、Python3.12+和工作区；保留配置，按原选择重新setup|
|imap_not_configured|让用户在指定.env补全返回的字段名|
|imap_tls_failed|检查服务器、TLS端口与证书，保持证书验证|
|imap_auth_failed|用户在本机检查完整用户名与客户端专用密码|
|imap_folder_failed|确认文件夹名称及权限，再check-imap|
|imap_connection_failed|检查服务器、TLS端口和网络|
|invalid_output|按本任务schema与证据编号修正一次；失败继续保留无有效判断状态|
|request_mismatch/request_changed|重新prepare并读取新请求后研判|
|job_conflict|get查看旧结果；重跑另建任务|
|provider_busy|稍后重试原prepared任务|
|progress_timeout|保留失败记录，需要重试时另建任务|
|file_access_denied/filesystem_error|报告宿主权限或本机存储问题，保持沙箱与审批配置|
|service_not_installed|按用户意图安装service模式|
|runtime_unavailable/bootstrap_unavailable|使用Python3.12+或重装完整Skill；保留工作区.mailguard，不改宿主沙箱|
|dependency_install_failed/installation_timeout|检查网络与包源，恢复后重跑原setup/install命令|
|operation_cancelled|当前步骤停止，先doctor/get确认状态，按用户意图继续未完成步骤|

服务中断留下running时报告未完成；原结果和失败记录均不覆盖。校验失败、格式错误及检查范围缺口不冒充有效安全结论。

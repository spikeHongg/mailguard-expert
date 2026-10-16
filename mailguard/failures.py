"""Safe, actionable failure responses shared by installers, CLI and HTTP."""


class SetupError(RuntimeError):
    def __init__(self, message, code='installation_error'):
        super().__init__(message)
        self.code = code


def recovery(code):
    # Advice is trusted product text. Never interpolate provider responses,
    # mailbox exceptions, credentials, or paths obtained from email content.
    actions = {
        'imap_not_configured': ('configure', False, ['在安装结果给出的本机.env补齐列出的IMAP字段；密码使用客户端专用密码或授权码。', '确认填写后执行check-imap；也可先使用模拟邮件或本地.eml。']),
        'imap_auth_failed': ('configure', False, ['在本机确认IMAP_USERNAME通常为完整邮箱地址，IMAP_PASSWORD为有效客户端专用密码，且邮箱已启用IMAP。', '修改后只重跑check-imap；不要把密码发到聊天里。']),
        'imap_tls_failed': ('configure', False, ['向邮箱服务商核对TLS服务器地址、端口和证书；默认端口993。', '修正后重跑check-imap；不要关闭证书校验。']),
        'imap_folder_failed': ('configure', False, ['向用户确认IMAP_FOLDER及其访问权限；收件箱通常为INBOX。', '在本机修改后重跑check-imap，不获取正文。']),
        'imap_connection_failed': ('retry', True, ['检查网络、VPN和邮箱服务器是否可达。', '连接恢复后重跑check-imap；暂时可继续模拟邮件或.eml。']),
        'imap_read_failed': ('retry', True, ['检查邮箱连接，先用check-imap验证。', '查看已返回的任务，再只重试未完成的读取。']),
        'python_unsupported': ('environment', False, ['选择Python3.12或更新版本，重新执行同一个setup/install命令。']),
        'runtime_unavailable': ('environment', False, ['保留.mailguard/.env和state，使用可用的Python3.12+重跑setup/install。', '本地动态库自动修复未成功时停止，不修改宿主Python或移除沙箱设置。']),
        'dependency_install_failed': ('retry', True, ['检查网络和Python包源是否可用，再重跑同一个setup/install命令；配置及状态目录会复用。', '不要手动删除.mailguard；不需要重新填写已有密码。']),
        'installation_timeout': ('retry', True, ['安装子进程已停止；检查网络、包源或宿主是否等待操作。', '解决后重跑同一个setup/install命令，保留已有配置和记录。']),
        'service_not_installed': ('configure', False, ['按安装SOP选择service模式并安装服务依赖；Skill模式继续使用宿主研判。']),
        'not_configured': ('configure', False, ['在本机.env填写所选提供方的API Key、模型和正确通道，再用模拟邮件验证。']),
        'provider_1113': ('configure', False, ['核对GLM Key权益、通用API余额和Coding Plan端点是否匹配；在本机修改，不反复重试同一配置。']),
        'http_401': ('configure', False, ['核对本机API Key是否有效、是否属于所选提供方及通道，修正后用模拟邮件重试。']),
        'http_403': ('configure', False, ['核对账号模型权限、接口通道及访问限制；不要重试相同配置。']),
        'http_404': ('configure', False, ['核对模型名称和API地址；GLM明确区分Coding Plan与通用API。']),
        'transport_error': ('retry', True, ['已用尽本次网络重试；检查网络，恢复后只重跑失败任务。']),
        'progress_timeout': ('retry', True, ['本次无新邮件完成的等待已到上限，在途模型请求已取消；已完成结果保留。', '检查服务可用性与并发设置，再为失败邮件准备新任务。']),
        'invalid_output': ('review', False, ['只根据原请求schema、9项检查和本任务证据编号修正一次；不得改哈希或伪造证据。', '仍失败时停止，报告无有效判断并交人工复核；不把失败标为安全或风险。']),
        'file_access_denied': ('permission', False, ['检查本机目录权限或使用宿主提供的正常授权流程；拒绝时停止本步骤。', '保留配置和记录，不删除PYTHONPATH、不关闭沙箱、不扩大权限绕过保护。']),
        'filesystem_error': ('environment', False, ['检查磁盘空间及所选私有目录是否可读写；不要删除状态库。', '修复后先执行doctor，再继续未完成步骤。']),
        'service_not_started': ('configure', False, ['在独立终端执行安装结果中的commands.start，再执行commands.status；Ctrl+C停止。']),
        'service_unreachable': ('retry', True, ['确认对应工作区服务仍在前台运行，端口一致，客户端使用该state目录的本机令牌。', '执行commands.status复查；不要把令牌粘贴到聊天。']),
        'unauthorized': ('configure', False, ['客户端从本机服务state目录读取service.token，设置Bearer鉴权；不要公开令牌。']),
        'job_conflict': ('review', False, ['先用get查看已有任务；已成功结果不覆盖，确需重新研判时prepare新任务。']),
        'request_mismatch': ('review', False, ['读取该任务原始请求和request_hash，使用原证据重新提交；不要修改快照或哈希。']),
        'request_changed': ('review', False, ['请求快照校验失败；保留记录供排查，从原始邮件prepare新任务。']),
        'operation_cancelled': ('cancelled', False, ['当前操作已停止；保留已有配置、记录及完成结果。', '需要继续时先查看doctor/get状态，再执行未完成步骤。']),
    }
    if code in ('provider_busy', 'http_429', 'http_408') or code.startswith('http_5'):
        category, retryable, steps = 'retry', True, ['等待服务恢复或降低并发，再只重试未完成任务；不无限重试。']
    else:
        category, retryable, steps = actions.get(code, ('review', False, [
            '保留已有配置和记录，检查当前步骤的输入及doctor结果。',
            '向用户报告错误码、失败步骤和已完成状态；仅分享脱敏诊断，不附.env或邮件正文。']))
    return {'category': category, 'retryable': retryable, 'automatic_retry': False, 'next_steps': steps}


def failure(code, message):
    return {'status': 'error', 'error_code': code, 'error': message, 'recovery': recovery(code)}

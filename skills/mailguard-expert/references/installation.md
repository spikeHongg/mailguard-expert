# 安装与IMAP初始化SOP

## 1. 选择使用方式

用户说“安装这个仓库”但未指定方式时，先询问：

- **邮件专家Skill（推荐）**：使用当前Agent模型；安装SOP、9项规则和轻量解析/校验工具，不需另填模型API Key。
- **本地独立服务**：由DeepSeek或GLM API研判，提供本机HTTP接口，需要模型配置。

已明确的模式直接采用；用户明确执行npx skills add即选择Skill。新装默认当前工作区，明确要求全局时才用全局安装。

## 2. 两种模式都询问IMAP

模式确定后主动询问“现在配置IMAP邮箱，还是稍后配置、先用模拟邮件或本地EML？”已有MailGuard配置时提供沿用、修改或稍后。先取得选择，再执行setup/install；不能替用户默选稍后。

|参数|用户选择|
|---|---|
|`--imap now`|现在填写新配置|
|`--imap later`|稍后配置；仍可安装并验收模拟邮件|
|`--imap reuse`|沿用已有.env|
|`--imap edit`|补齐模板并由用户修改；保留已填写值|

服务模式再确定DeepSeek或GLM；GLM区分Coding Plan与通用API。用户也可暂缓模型配置（`--provider later`）。Key和邮箱信息统一在本机.env填写，当前Agent只读取程序返回的配置状态。

## 3. 执行所选安装

### Skill模式

从公开仓库获取完整Skill：

```sh
npx skills add spikeHongg/mailguard-expert --skill mailguard-expert --agent codebuddy --copy
```

当前工作区的Skill位于 `.codebuddy/skills/mailguard-expert/`。明确全局时加`-g`，位置为用户目录下同一路径。其他宿主使用对应Skills CLI目标；本Skill脚本保持通用。

读取安装后的SKILL.md，然后执行以下命令，将IMAP参数替换成用户的选择：

```sh
python3 "SKILL绝对路径/scripts/mailguard.py" --workspace "工作区绝对路径" setup --imap now
```

只有Python3.12+才能准备运行环境；Windows可用 `py -3.12`。首次初始化仅安装轻量依赖。npx下载完成表示Skill文件已安装；setup的离线自检通过才表示工具可用。无须另存一份完整源码仓库。

### 独立服务模式

从 `https://github.com/spikeHongg/mailguard-expert` 克隆源码到工作区内的专用目录，读取根INSTALL.md。执行所选参数，例如：

```sh
python3.12 scripts/install.py --mode service --workspace "工作区绝对路径" --imap now --provider deepseek
```

GLM选择 `--provider glm --glm-channel coding` 或 `--glm-channel standard`。可选网页另加`--with-ui`。安装器输出适合当前系统的 `commands.start/status/stop`，把这些命令完整交付用户。前台启动，不设置开机运行。

安装器未得到必需选择时，交互终端会询问；非交互Agent会得到 `status=needs_input` 与问题/选项，应将问题交给用户，再带参数执行。

## 4. 用户在本机填写配置

使用安装器返回的 `env_file` 绝对路径；默认是工作区 `.mailguard/.env`。字段如下：

|字段|填写说明|
|---|---|
|IMAP_HOST|邮箱服务商的收信服务器，不猜测|
|IMAP_PORT|默认TLS端口993|
|IMAP_USERNAME|通常为完整邮箱地址|
|IMAP_PASSWORD|客户端专用密码或授权码|
|IMAP_FOLDER|建议INBOX，由用户确认后填写|

字段是否齐全用doctor检查；密码和Key只在本机编辑，不要求发到聊天或终端参数，也不展示.env内容。已填写内容保留，模板只补缺少的字段。

用户确认填好后，Skill执行 `check-imap`，服务安装使用返回的 `commands.check_imap`。成功须返回 `imap_verified=true`、`mails_fetched=0`；只验证TLS、认证和只读文件夹。失败保留固定错误码，协助用户在本机检查对应配置。

服务模式还须用模拟邮件验证选定模型API；模型Key未填时记录待配置。初始配置验证不自动获取真实邮件。

## 5. 验收与升级

两种模式分别报告安装状态、IMAP配置/验证状态、模型API配置/验证状态。暂不配IMAP仍可完成模拟验收。

Skill按主SKILL依次处理m002、m026、m031，完整读取请求并由当前宿主新研判，提交后展示证据和每个任务ID。服务按根API文档验证HTTP调用；外部API只有实际完成后才标记已验证。

npx更新只替换Skill分发目录；工作区 `.mailguard/` 保存环境、配置和邮件记录。更新后按原IMAP选择重新setup，复用.env与状态。旧源码安装的.env/状态可通过setup或安装器的 `--env-file`、`--state-dir` 显式沿用。

遇到Python动态库错误，使用安装器的本地环境诊断；保留宿主PYTHONPATH、沙箱和审批设置。修复仅限工作区内运行环境；权限拒绝时报告具体步骤，不能绕过宿主保护。

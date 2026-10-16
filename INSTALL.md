# 安装MailGuard

完整安装步骤以 [安装与IMAP初始化SOP](skills/mailguard-expert/references/installation.md) 为准。Agent必须依次确定安装模式、主动询问IMAP、准备运行环境，再验收模拟邮件；已明确的选择直接沿用。

- **Skill模式**：当前Agent研判。通过npx安装自包含Skill，首次setup准备轻量Python依赖，无需模型API Key。
- **独立服务模式**：安装模型API与HTTP依赖，输出本机配置路径、启动、停止和状态检查命令。

仓库安装器提供 `--mode skill|service` 和 `--imap now|later|reuse|edit`；不带选择时交互询问，非交互返回 `needs_input`。服务模式另选 `--provider deepseek|glm|later`，GLM需指定 `--glm-channel coding|standard`。

Python要求3.12+。Windows可使用 `py -3.12`；命令提示符建议先执行 `set PYTHONUTF8=1`。评测文件锁支持Windows与POSIX，Windows依赖包含时区数据。Windows完整运行须有对应实机验收证据。

默认配置与邮件状态位于工作区被忽略的 `.mailguard/`，升级保留数据。旧源码安装可用 `--env-file`、`--state-dir` 显式沿用原配置和状态。源安装更新已有Skill加 `--update`；独立服务可用 `--with-ui` 加装网页。

离线自检不代表API或IMAP可用。IMAP连接检查只认证并选择只读文件夹；读取真实邮件是后续明确操作。密钥只在本机.env填写。

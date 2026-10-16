# 清邮 · MailGuard

邮件安全专家，识别凭据钓鱼、付款诈骗、危险附件诱导、资料套取和提示注入，提供可核验的原文证据与复核建议。

[安装说明](INSTALL.md) · [专家Skill](skills/mailguard-expert/SKILL.md) · [HTTP API](docs/local-api.md)

## 交给WorkBuddy安装

把 [仓库链接](https://github.com/spikeHongg/mailguard-expert) 和这句话发给WorkBuddy：

> 安装MailGuard邮件安全专家，先询问我选择Skill还是本地独立服务，再询问IMAP初始配置，按仓库安装SOP完成自检，并用内置模拟邮件演示完整研判。

|方式|适用场景|模型与配置|
|---|---|---|
|**邮件专家Skill（推荐）**|在WorkBuddy等Agent中检查邮件|使用宿主自身模型，无需额外模型Key；IMAP可现在或稍后配置|
|**本地独立服务**|其他程序通过本机HTTP API调用|填写DeepSeek或GLM配置；IMAP可选|

## 安装Skill

```sh
npx skills add spikeHongg/mailguard-expert --skill mailguard-expert --agent codebuddy --copy
```

完整Skill安装到当前工作区 `.codebuddy/skills/mailguard-expert/`。让WorkBuddy读取它并按SOP初始化即可；工具随Skill提供，无需另外保留完整源码仓库。首次使用需要Python3.12+，Agent会准备轻量依赖。

安装会询问IMAP现在配置、稍后配置，或沿用已有配置。邮箱服务器、完整用户名、客户端专用密码和文件夹在本机 `.env` 填写，`check-imap`只验证连接和只读文件夹，不获取邮件正文。配置路径由安装结果明确给出。

安装或检查失败时，Agent会说明失败步骤、保留的结果和具体恢复办法。配置问题先修正再继续；网络失败仅重试未完成任务；输出校验失败不会被当作安全结论。详见 [故障处理](skills/mailguard-expert/references/operations.md)。

## 本地独立服务

取得源码后，由Agent依据用户选择调用安装器。例如明确选择“稍后配置IMAP、使用DeepSeek”：

```sh
git clone https://github.com/spikeHongg/mailguard-expert.git
cd mailguard-expert
python3.12 scripts/install.py --mode service --imap later --provider deepseek
```

安装器返回本机 `.env` 路径和适合当前系统的启动、停止、状态检查命令。用户填写后用模拟邮件验证API。GLM使用 `--provider glm --glm-channel coding` 或 `standard` 选择接口通道。

服务固定监听 `127.0.0.1:8765`，业务接口需要本机Bearer令牌；默认前台运行，Ctrl+C停止。默认DeepSeek20、GLM5并发，上限20。接口和调用示例见 [API文档](docs/local-api.md)。

## 能力与流程

```text
读取邮件 → MIME/HTML静态解析 → 9项风险检查 → 原文证据校验 → 展示结果 → 人工复核
```

- 支持 `.eml`、三封内置演示邮件和明确触发的只读IMAP获取。
- 提取发件人、Reply-To、链接、隐藏文本、附件元数据，以及HTML正文和附件中的表单、输入控件、外部脚本等事实。
- `risk`为发现有证据支持的风险；`clear`为当前范围未发现明显风险；`review`为缺少关键信息；`error`为没有取得有效研判。
- 结论绑定请求与原文证据，保留宿主新研判、API实测及回放来源。

后端不执行邮件指令、访问链接、提交表单或运行附件。邮箱只读，不自动删除、回信或封禁。WorkBuddy工具权限由宿主控制，Skill不构成权限沙箱。HTML静态检查不能替代病毒检测；PDF、Office、压缩包内容和二维码像素不在当前范围内。

## 数据、升级与开发

新安装的配置、环境和记录在工作区被Git忽略的 `.mailguard/`，与Skill分发目录分开，更新Skill保留数据。旧源码的配置和状态可显式沿用。密钥、真实邮件、模型原始响应和内部报告不提交Git。

开发环境可安装 `.[service,ui,dev]`，然后运行：

```sh
python scripts/build_skill_bundle.py --check
python -m pytest -q
```

可选Streamlit页面用服务安装器的 `--with-ui` 安装。Windows安装与完整验收范围见 [INSTALL.md](INSTALL.md)。源码发布与隐私检查见 [PUBLISH.md](PUBLISH.md)。

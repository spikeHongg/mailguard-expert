# 清邮 · MailGuard

邮件安全专家，用于识别钓鱼、付款诈骗、凭据套取、危险附件诱导和HTML表单风险，提供可核验的原文证据与人工复核建议。

支持 **WorkBuddy Skill** 和 **本地独立HTTP API** 两种使用方式。

[安装](INSTALL.md) · [专家Skill](skills/mailguard-expert/SKILL.md) · [API文档](docs/local-api.md)

## 交给WorkBuddy安装

将[仓库链接](https://github.com/spikeHongg/mailguard-expert)与下面这句话发给WorkBuddy：

> 安装这个仓库的MailGuard邮件安全专家，按AGENTS.md和INSTALL.md完成安装、自检，再用内置模拟邮件演示一次完整研判。

WorkBuddy会准备Python环境、安装Skill并执行自检。默认使用WorkBuddy自身模型研判，无需另外填写模型API Key。

### 手动安装

准备Python **3.12+**，获取源码并在仓库根目录执行：

```sh
git clone https://github.com/spikeHongg/mailguard-expert.git
cd mailguard-expert
python3.12 scripts/install.py
```

Skill安装到当前项目 `.codebuddy/skills/mailguard-expert/`。在WorkBuddy打开该项目并开启新会话：

> 用mailguard-expert检查内置模拟邮件m002和m026，展示结论、证据、建议与未检查范围。

安装到其他工作区、更新、Windows命令和可选npx安装见 [INSTALL.md](INSTALL.md)。

## 功能

- **邮件输入**：`.eml`文件、内置模拟邮件、只读IMAP文件夹。
- **静态解析**：中文/MIME正文、发件人、Reply-To、链接、隐藏文本、附件元数据。
- **HTML检查**：正文与HTML附件中的表单、输入框、提交目标、外部脚本和iframe。
- **9项风险检查**：身份授权、付款诈骗、链接附件、资料外发、远控勒索、提示注入及误报复核等。
- **证据校验**：核对原文引用、技能覆盖和分项与汇总结论，错误独立显示。
- **人工复核**：追加复核意见，保留原始研判和任务记录。
- **两种模型入口**：由宿主Agent研判，或由本地服务调用配置的DeepSeek/GLM API。

## 使用流程

```text
读取邮件 → 提取静态事实 → 模型研判 → 校验证据 → 展示结果 → 人工复核
```

| 结果       | 含义                  |
| -------- | ------------------- |
| `risk`   | 发现有原文证据支持的攻击或违规诱导   |
| `review` | 缺少影响安全判断的必要信息，需要核实  |
| `clear`  | 在当前检查范围内未发现明显风险     |
| `error`  | 解析、调用或输出校验失败，没有有效判断 |

邮件内容只用于分析。后端不执行邮件里的指令，不打开链接、不提交表单、不运行附件，也不自动删除邮件、封禁账号或发送回复。独立API中的研判模型没有工具权限；WorkBuddy模式的工具权限由宿主控制，Skill约定固定流程，不构成权限沙箱。HTML静态检查不能替代病毒扫描；PDF、Office、压缩包内容和二维码像素不在当前检查范围内。

## 本地独立API

在仓库 `.env` 中填写所选模型Key，保持文件仅在本机：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_CONCURRENCY=20

GLM_API_KEY=
GLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
GLM_MODEL=glm-5.3-flash
GLM_CONCURRENCY=5
```

GLM入口按账号通道填写，Coding Plan与通用API地址不同；完整配置项见 `.env.example`。修改配置后重启服务。

```sh
.venv/bin/python -m mailguard.expert_cli serve --port 8765
```

服务监听 `http://127.0.0.1:8765`，仅 `/health`公开，其余接口需本机Bearer令牌。单进程支持每个模型1–20并发。调用示例、鉴权、状态和超时见 [API文档](docs/local-api.md)。

### IMAP配置（可选）

```dotenv
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_USERNAME=user@example.com
IMAP_PASSWORD=
IMAP_FOLDER=INBOX
```

用户名通常是完整邮箱地址，密码使用服务商提供的客户端专用密码或授权码。系统在用户主动触发后读取指定文件夹，采用TLS、只读方式并保持已读状态。读取邮件与调用外部模型是独立步骤。

## 本地网页（可选）

```sh
python3.12 scripts/install.py --with-ui
.venv/bin/python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

浏览器打开 `http://127.0.0.1:8501/`。网页提供单封研判、评测和复核入口；专家CLI/API与网页可独立运行。

## 数据与开发

密钥写入 `.env`；邮件、请求、原始响应和复核默认存放在被Git忽略的 `data/private/`。安装后的本机路径配置位于 `.codebuddy/`，也被忽略。自定义状态目录应放在版本库之外。

```sh
.venv/bin/python -m mailguard.expert_cli doctor
.venv/bin/python -m pytest -q
```

doctor执行离线安装自检，不调用模型或IMAP。贡献前运行相关测试；提交时排除密钥、真实邮件、个人信息和本机运行文件。项目协作约定见 [AGENTS.md](AGENTS.md)。

半公开仓库发布使用 [源码导出流程](PUBLISH.md)。

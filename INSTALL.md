# 安装MailGuard邮件安全专家

## 交给WorkBuddy的一句话

> 安装这个GitHub仓库中的MailGuard邮件安全专家。先读取AGENTS.md和INSTALL.md，按说明安装到当前WorkBuddy工作区，完成离线自检，再用内置模拟邮件走通准备、研判、证据校验和展示流程。

把仓库链接与这句话一起发送即可。仓库需要可访问，无需申请npm包或上架技能市场。当前交付为Git仓库；实际公开仓库地址由发布者提供。

## 安装流程

### 1. 获取源码

将用户给出的仓库克隆到本地，进入仓库根目录。已有本地副本时复用。环境要求Python **3.12+**；原生安装路径不需要Node.js。

### 2. 安装运行环境与Skill

```sh
python3.12 scripts/install.py
```

默认将Skill安装到仓库 `.codebuddy/skills/mailguard-expert/`，在WorkBuddy打开这个目录即可使用。需要装到另一个WorkBuddy工作区时：

```sh
python3.12 scripts/install.py --workspace "/absolute/path/to/workspace"
```

Windows使用已安装的Python 3.12+，可执行 `py -3.12 scripts/install.py --workspace "C:\work\mail"`。本轮实际验收平台为macOS；Windows完整流程待实机验证，历史评测命令还依赖POSIX文件锁。

安装器创建/复用仓库 `.venv`、安装后端依赖、保留已有 `.env`、复制完整Skill和脚本、写入本机运行环境定位，再执行离线自检。`--with-ui`可加装Streamlit依赖；默认专家/API服务无需网页界面。

整个用户账户共用Skill时，显式使用 `--scope user`。重复运行保留密钥和数据；升级本安装器已管理的Skill时使用 `--update`。不相关或被手改的文件发生冲突时停止并保留原文件。

### 3. 检查安装结果

安装输出应为 `status=ok`，`selftest.skills=9`，`selftest.contract_roundtrip=true`。也可以单独运行：

```sh
.venv/bin/python -m mailguard.expert_cli doctor
```

Windows虚拟环境入口为 `.venv\Scripts\python.exe`。

离线自检验证资源、MIME/HTML解析、9项技能、证据契约和私有存储；固定结构夹具不算AI研判，不会调用API或读取IMAP。

### 4. 在WorkBuddy验收

打开安装目标工作区，开启新会话：

> 用mailguard-expert检查内置模拟邮件m002和m026，展示结论、原文证据、建议与未检查范围，保留每封任务ID。

宿主应读取Skill，执行 `prepare`、完整读取请求、用当前模型生成新结果，再执行 `submit` 校验。宿主模型不可知时保存 `not_reported`；结果标记 `host_agent`，不算DeepSeek/GLM API实测。

如果WorkBuddy没有发现Skill，确认实际工作区存在 `.codebuddy/skills/mailguard-expert/SKILL.md`，然后重新开启会话；也可把该Skill绝对路径交给它显式读取。仅安装文件尚不算宿主完整验收通过。

## 配置与独立服务

默认WorkBuddy模式使用它自身模型，无需邮件系统再填API Key。独立服务在本机 `.env` 填写DeepSeek或GLM Key、对应模型和端点；文件保存后重启进程。IMAP可选，用户名通常为完整邮箱地址，密码为服务商签发的客户端专用密码，明确填写文件夹后再主动读取。

```sh
.venv/bin/python -m mailguard.expert_cli serve --port 8765
```

独立HTTP服务监听 `127.0.0.1:8765`。健康探针 `/health`；其余接口需要Bearer令牌，令牌自动生成在私有状态目录 `service.token`。完整调用示例见 [本地API](docs/local-api.md)。DeepSeek默认20并发、GLM默认5，`.env`中的 `*_CONCURRENCY` 可设1–20后重启。单个进程统一限制全部HTTP请求的模型并发，满额返回429，调用方稍后重试。

## 可选：npx安装Skill

仓库发布到GitHub后，可使用Vercel Skills CLI：

```sh
npx skills add spikeHongg/mailguard-expert --skill mailguard-expert --agent codebuddy --copy
```

CLI目前使用 `codebuddy` 目标写入WorkBuddy兼容的 `.codebuddy/skills`；本项目不假定存在 `--agent workbuddy`。**这条命令只安装Skill。** 随后仍需克隆仓库并执行本页安装器，安装Python运行环境和自检；无需将本仓库发布为npm包。

## 本地数据与更新

- 克隆源码模式默认状态：`data/private/expert/`，包含请求、原始响应、研判、失败和复核记录；已被Git忽略。
- 可通过 `MAILGUARD_STATE_DIR` 或CLI `--state-dir` 指定专用本机目录；自定义目录也应放在版本库外或加入Git忽略。
- `MAILGUARD_ENV_FILE` 或CLI `--env-file` 可指定配置文件。命令行填写路径，不填写密钥值。
- 安装后的 `runtime.json` 仅保存本机路径，不保存Key；目标目录 `.codebuddy/` 已被项目忽略。
- 原始任务结果不可覆盖，重新研判创建新任务；人工复核追加保存。升级前保留 `.env`、私有状态目录和旧运行记录。
- 保留源码目录与虚拟环境。目录移动后，重新运行安装器更新定位；仅删除Skill不会删除邮件和密钥。

CLI、数据边界及失败处理的完整操作步骤见 [专家运行SOP](skills/mailguard-expert/references/operations.md)。

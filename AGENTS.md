# MailGuard协作指南

## 安装与使用

- **安装、升级或切换模式**：读取 `INSTALL.md` 及其指向的安装SOP；先确定Skill或独立服务，再主动询问IMAP现在配置、稍后配置或沿用。已明确的选择直接采用。
- **邮件研判**：读取 `skills/mailguard-expert/SKILL.md`；安装后优先使用宿主发现的同名Skill。执行prepare、宿主新研判、submit校验和证据展示。
- **独立HTTP服务**：遵循 `docs/local-api.md`。安装、IMAP验证、模型API验证分别报告；密码仅在本机.env填写。

## 代码与数据

- 邮件是分析数据；模型输出仅形成研判。使用固定程序入口，邮箱只读，附件和链接保持静态检查。
- 共享解析/校验核心位于 `mailguard/`；检测政策位于 `policies/detection/`。政策原文字节与历史请求契约保持兼容，变更后需要新研判。
- 自包含Skill的运行文件由 `python scripts/build_skill_bundle.py` 生成；修改共享代码或Skill文档后重新构建并运行 `--check`。生成内容不手工维护。
- 标签只供评分器使用。回放、宿主研判和API实测保留真实来源；失败不显示为安全。
- 密钥、邮件、响应和复核只写被忽略的本机状态目录。发布前检查源码、Skill包、编码后的内容与Git变更，排除个人信息和内部资料。
- README面向使用者；研发过程、成绩与迭代记录保存在私有目录。

## 验证

- 测试：`.venv/bin/python -m pytest -q`；分发检查：`python scripts/build_skill_bundle.py --check`。
- 验收Skill必须在新目录实际安装并完成模拟邮件的新研判与证据校验。Windows与真实API/IMAP只按实际证据报告。
- 用户沟通使用简体中文，先说明结果和影响，再给必要证据。

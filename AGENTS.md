# 本项目工作规则

## 项目位置和发布顺序

- 本地源码以 `G:\机器人后台楠木` 为准，先在此目录修改。
- 修改后先执行相关本地测试；测试通过后再部署到服务器。
- 只有用户明确说“上传到 GitHub”时，才提交或推送到 GitHub。
- 不再把其他目录的旧副本当作本项目的开发入口。

## 线上服务

- 服务器：`165.154.66.129`，服务：`tgpanel`。
- 服务源码：`/home/ubuntu/tgpanel`，入口：`主程序.py`。
- 本次下载时只读 `getMe` 验证的机器人：`@ceshihao1bot`。
- 每次部署前重新核对服务路径、机器人身份及服务器源码版本。
- 部署仅更新本次修改的源码；保留服务器配置、凭据和业务数据。
- 不用本地测试数据覆盖线上账单、群组设置、权限或到期时间。
- 部署前保存源码回滚点，执行隔离测试；上线后核对文件和服务状态。
- 测试使用临时目录、模拟凭据和模拟 Telegram API。
- 不在测试中启动真实机器人轮询或向真实用户、群组发送消息。

## 本地验证

- `SOURCE_SNAPSHOT.json` 是下载时的源码哈希和排除项记录。
- `DEVELOPMENT_FILES.json` 记录补充的测试及预览工具来源。
- 本地预览：`python tools/preview_miniapp.py`，使用临时模拟数据。
- 浏览器检查：`python tools/check_config.py`。
- 交互检查：`python tools/check_interactions.py`。
- 账单和统计导航：`python tools/check_bill_navigation.py`。
- 运行位置和安装包回归：`python -m unittest _test_hosted_mode -q`。
- 面板按钮：`node tools/check_hosted_actions.js`。
- 新增测试以实际业务、权限边界及返回路径为准。

## 编码要求

- 遵循用户的 Karpathy 指引，保持修改范围最小。
- 编码任务使用 Ponytail 技能，默认 full。
- Markdown 文件使用 Markdown 技能。
- 不把密码、Token、私钥、运行配置或账单数据提交到版本库。

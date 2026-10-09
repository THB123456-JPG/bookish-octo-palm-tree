# 本地开发与上线

本目录是从正在运行的 `tgpanel` 服务下载的源码。
下载来源：`165.154.66.129:/home/ubuntu/tgpanel`。
文件校验记录在 `SOURCE_SNAPSHOT.json`。
补充测试及预览工具的来源在 `DEVELOPMENT_FILES.json`。

## 工作流程

1. 在本目录修改代码。
1. 使用模拟数据完成本地回归测试和相关手机页面检查。
1. 测试通过后部署到服务器，保存源码回滚点并验证上线结果。
1. 用户明确要求上传 GitHub 后，才进行 GitHub 发布。

服务器配置、机器人 Token、账单数据、运行日志及虚拟环境未作为
本地开发材料保留。源码示例中的疑似密钥使用占位符替换。
本地测试不需要生产凭据，也不会向 Telegram 用户或群组发消息。

## 安装依赖

```powershell
python -m pip install -r requirements.txt
```

浏览器检查另外需要已安装的 Playwright 和 Chromium。

## 回归测试

```powershell
$tests = @(
  '_test_customer_config', '_test_miniapp', '_test_customer_ui',
  '_test_personnel', '_test_disable_cutoff', '_test_entry_formats',
  '_test_personal_pricing', '_test_owner_statistics', '_test_group_ready',
  '_test_hosted_mode'
)
python -m unittest @tests -q
```

## 小程序本地预览

```powershell
python tools/preview_miniapp.py
```

打开 <http://127.0.0.1:8765/miniapp/ledger1>。
这是模拟数据预览，不是真实 Telegram 登录环境。
保持预览服务运行，在另一个终端执行浏览器检查：

```powershell
python tools/check_config.py
python tools/check_interactions.py
python tools/check_bill_navigation.py
node tools/check_hosted_actions.js
```

检查生成的截图位于 `output/playwright`。
生产入口是 `主程序.py`，本地测试请使用上述模拟预览入口。

## 运行位置和安装包

下载安装包只导出程序，不改变运行位置或拥有者权限。
“切换客户自建”会停止当前服务器上的机器人，配置中心将不可用。
如果只需要下载安装包，不要切换运行位置。
“恢复服务器运行”会重新启动当前服务器上的机器人。
切回前先停止客户服务器上的同一个机器人，避免同时轮询。

## 增量发布与回滚

发布使用 `_deploy_server.py`，连接固定的项目服务器。
首次连接前使用 SSH 核对服务器主机指纹；支持 SSH Agent、
`--key 私钥路径` 或隐藏输入的 SSH 密码，不保存凭据。

```powershell
python _deploy_server.py --dry-run
python _deploy_server.py
```

预览仅查询身份和哈希，不上传文件、不重启。
正式发布自动完成：

1. 依据源码白名单计算主目录及登记实例的差异。
1. 检测实例身份、定制源码及计划生成后的版本变化。
1. 运行本地测试，上传隔离候选源码并保存差异文件回滚点。
1. 在服务器临时副本测试恢复操作、回归及模拟并发读取。
1. 安装差异文件，核对主目录、全部实例和服务健康状态。

工具禁止并发发布；配置、Token、账单、客户代码和本地产物不在白名单内。
检测到实例定制差异时先停止发布，保留该实例，人工评估合并范围。
说明和工具更新不重启；运行源码变更会统一停止及恢复服务，
安装失败自动恢复本次已安装的文件。工具不自动删除旧源码。

工具打印本次检查点路径，以它执行校验或回滚：

```powershell
python _deploy_server.py --verify <检查点路径>
python _deploy_server.py --rollback <检查点路径>
```

回滚前核对当前源码，拒绝覆盖发布后新增的修改；
只恢复该检查点涉及的源码，不恢复客户业务数据。
发布记录保存在 `output/release_*.json`。

## 隔离并发检查

```powershell
python tools/check_isolated_load.py --output output/isolated_load_result.json
```

默认以 8 个并发请求执行 240 次概览、账单及统计读取，使用 1000 笔模拟流水。
检查响应、账单数量及数据库哈希，禁止访问外部网络。
该结果用于回归比较，不代表生产容量或真实手机体验。

实际检查结果见 [优化检查报告](优化检查报告.md)。

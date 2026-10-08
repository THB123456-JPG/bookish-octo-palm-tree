# 发布规则

## 日常维护

以下改动只提交到 `main`，不创建 Tag，不创建 Release：

- 小 Bug 修复。
- OCR 规则修复。
- OCR 性能优化。
- 文档修改。
- 管理员功能修复。
- 状态面板修复。
- 广播、学习、排序、去重等通用功能修复。

标准流程：

```bash
git add .
git commit -m "描述本次维护内容"
git push origin main
```

## 允许创建 Release 的情况

只有以下情况才创建正式 Release：

- OCR 引擎更换。
- 数据库结构变化。
- 部署方式变化。
- 重大架构升级。
- 长期稳定版封版。

## 版本线定义

Cloud Deploy：

```text
唯一标准部署版本
当前 main
默认 REMOTE_OCR_ENABLED=false
```

owner-hybrid：

```text
Cloud Deploy + RTX5070 / Tailscale / Remote OCR
通过 .env 开启
```

任何未来新增功能和 Bug 修复，必须先进入 Cloud Deploy。

# 稳定版本说明

## 永久规则

Cloud Deploy 是唯一标准部署版本，`main` 是 Cloud Deploy 的滚动最新源码。

Cloud Deploy 永远代表：

- 当前最新功能。
- 当前所有 Bug 修复。
- 当前所有 OCR 规则优化。
- 当前所有管理员功能。
- 当前所有状态、广播、学习、去重、排序功能。
- 新服务器可直接完整部署。

Cloud Deploy 唯一不包含：

- Windows RTX5070 OCR Worker。
- Tailscale。
- `REMOTE_OCR_URL` 本地地址。
- 本地 GPU 混合识别。
- Windows 本地 OCR 服务部署。

## 当前推荐

普通云服务器：

```text
Cloud Deploy / 当前 main
```

作者本人专用：

```text
owner-hybrid = Cloud Deploy + RTX5070 / Tailscale / Remote OCR
```

## 配置区别

Cloud Deploy 默认：

```env
REMOTE_OCR_ENABLED=false
REMOTE_OCR_URL=
```

owner-hybrid：

```env
REMOTE_OCR_ENABLED=true
REMOTE_OCR_URL=http://100.81.208.104:8000
```

## 历史归档

`v2.8.0-cloud-deploy` 保留为历史里程碑 Release，不代表后续最新代码。

`v1.3.0-ocr-learning-plus` 已归档，不再作为最新推荐部署版本。

`v120` 只保留为重构前历史备份。

以后新增任何功能和 Bug 修复，必须先进入 Cloud Deploy；owner-hybrid 不能拥有 Cloud Deploy 没有的业务功能。

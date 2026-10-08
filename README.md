# TG 机器人管理面板

一个后台进程同时运行多个 Telegram 机器人，配一个网页管理面板。
支持多种机器人类型，每种类型的机器人互不干扰。

**目前支持的机器人类型**

| 类型 | 干什么用 | 使用者 | 代码 |
|---|---|---|---|
| **客服机器人** | 客户私聊机器人 → 消息转到使用者的 TG → 他回复 → 自动发给客户 | 你的朋友/同事 | `runners/kefu/` |
| **USDT 助手** | 查 TRC20 地址交易 / 监听地址进出账并推送提醒 | 你自己 | `runners/usdt/` |
| **商城机器人** | 面向公众的自助服务：能量闪租 / TRX闪兑 / TG会员 | 所有客户 | `runners/shop/` |
| **记账机器人** | 群里记账：`+100` 记一笔、出日账单、群管理（清理/@全体/广播）、私聊查地址/监听 | 商户的群 | `runners/ledger/` |

**另外还有两块管理端功能**（商户看不到）：

- **双后台 / 多租户** —— 管理后台（`/`）+ 商户后台（`/m`），
  商户只看得到自己名下的机器人（`merchants.py`）
- **群消息记录** —— 把机器人收到的群消息在本地归档，面板里按
  「机器人 → 群 → 消息」三级下钻查看，支持未读、置顶、备注、图片
  （`archive.py`，管理端专属）

---

## 〇、一键部署到服务器

在服务器上（Ubuntu / Debian / CentOS 都行）：

```bash
git clone <你的仓库地址> && cd TG客服多开版
sudo bash deploy.sh
.venv/bin/python _selfcheck.py --url     # ★ 验一遍功能没缺
```

就这三条。脚本会自动干完这些：

1. 检查 Python 版本（要 3.10+）
2. 建虚拟环境 `.venv` + 装 `requirements.txt`
3. 生成 `config.json`（**监听 `0.0.0.0`**、随机强密码、签名密钥）
4. 装成 systemd 服务 `tgpanel`，**开机自启 + 崩了自动重启**
5. 启动，并把**面板地址和密码**打出来

跑完屏幕上会直接给你：

```
  面板地址：http://你的服务器IP:8080
  商户入口：http://你的服务器IP:8080/m
  面板密码：（脚本生成的 12 位）
```

**常用参数**：

```bash
sudo bash deploy.sh --port 9000     # 换端口
sudo bash deploy.sh --no-service    # 只装依赖，不配开机自启
sudo bash deploy.sh --uninstall     # 卸载服务（★ 不动你的数据）
```

**日常运维**：

```bash
systemctl restart tgpanel           # 重启
systemctl stop tgpanel              # 停止
journalctl -u tgpanel -f            # 看日志
```

> ★ **脚本不会碰你的数据**：`bots.json` / `merchants.json` / `data/`
> 原样保留。已有 `config.json` 时也**不会覆盖里面的密码**，只补缺的字段。

### ★★ 换一台新服务器

步骤跟上面**一模一样**（克隆 → deploy.sh → 自检），因为仓库里是干净的：
机器人列表和商户列表都是**空的**，`config.json` 里没有密码（`deploy.sh`
会生成一个新的）。新机器上按上面三条敲一遍就是一台全新的面板。

**★ 换完一定要跑 `_selfcheck.py`** —— 它逐项验功能，不是只「import 一下」：

```
 ⑤ 四种机器人都能构造（接线没断）      ✅ 记账 / 客服 / USDT / 商城
 ⑥ 关键功能（不是「能导入」而是「能干活」）
    ✅ 防篡改核对图能画出来（Pillow + 字体）
    ✅ USDT 合约常量没被脱敏改掉
    ✅ 「客户安装包」的源文件齐全
 ⑧ 面板能不能打开（http://127.0.0.1:8080）
    ✅ 首页 200 / ✅ 有界面 / ✅ 接口有鉴权
```

**为什么非要这一步**：换服务器最容易出的不是「起不来」，而是
**「起来了但某个功能悄悄瘸了」** —— 少装一个包（Pillow）、少传一个文件
（`panel_page.html`），表现都是「点那个功能没反应」，而进程活得好好的、
日志也干干净净。等客户用到才发现就晚了。

**从旧机器搬数据**（可选，不搬就是从空的开始）：把旧机器上的
`bots.json` / `merchants.json` / `data/` 拷到新机器同目录，再
`systemctl restart tgpanel`。★ `config.json` 别覆盖 ——
非要沿用旧密码就一起拷，否则新机器会生成一个新密码。

### 另一条路：Docker

想用容器（`Dockerfile` + `docker-compose.yml` 都准备好了）：

```bash
sudo bash deploy.sh --no-service    # 先跑一次，把 config.json 生成好
docker compose up -d
docker compose logs -f
```

> ★ 容器那条路我**没在这台 Windows 上实测过**（本机没装 Docker），
> 配置是照标准写法给的。**推荐还是用 `deploy.sh`**，那条我测过。

### ★ 上公网必做

面板本身**没有 CSRF 防护**、密码是**明文 HTTP 头**传的。
一定要套一层 Nginx + HTTPS 反代，防火墙只放行 80/443，
**别把 8080 直接暴露到公网**。详见第十节。

---

## 一、整体流程

```
运营者（你）在面板填「类型 + 备注 + token」
        ↓ 系统验证 token，生成绑定码
把绑定码发给使用者
        ↓
使用者在自己的 Telegram 里给机器人发：/admin 绑定码
        ↓
绑定成功，这个机器人开始工作
```

**为什么要这样**：多个机器人集中在一个后台跑，统一管理、统一续期，
不用给每个人发一套程序。

---

## 二、文件结构

```
TG客服多开版/
├── 主程序.py              ← 入口：起面板 + 拉起所有机器人
│
├── ── 框架层（所有机器人共用）──
├── core.py                ← TgAPI、工具函数、BaseRunner（机器人基类）
├── manager.py             ← 机器人管理器：增删、启停、读写 bots.json
├── panel.py               ← 网页面板的 HTTP 服务（只放框架接口 + 各类型的路由表）
├── panel_page.html        ← 【前端页面】改界面只需要动这个文件
├── messages_page.html     ← 群消息记录页（独立页面，只有管理员进得去）
├── archive.py             ← 群消息记录（本地归档，管理端专属）
├── login_log.py           ← 登录日志（记 IP + 归属地）
├── merchants.py           ← 商户管理（PBKDF2 密码 + 签名登录态）
├── totp.py                ← 谷歌验证码（TOTP，纯标准库）
├── tron.py                ← TRON 链上工具【USDT 助手和商城都在用】
│
├── runners/               ← ★ 各机器人类型，一个类型一个文件夹
│   ├── kefu/              客服机器人
│   ├── usdt/              USDT 助手
│   ├── shop/              商城（机器人 / 数据层 / 收款 / 上游适配 / 面板接口）
│   └── ledger/            记账（机器人 / 账本 / 账单 / 群管理 / 面板接口…）
│
├── codes/                 ← ★ 给【某一个机器人】开小灶（平时是空的，见里面 README）
├── static/                ← ★ 「添加到桌面」的图标 + manifest（_gen_icons.py 生成）
├── solo/                  ← ★ 「记账独立版」给客户自带服务器的（_build_solo.py 打包）
│
├── requirements.txt       ← 服务器上先装这个
├── TG客服多开版.spec      ← 打包配置
├── TG客服多开版.exe       ← 打包好的可执行文件（Windows）
├── _test_*.py             ← 测试（共 22 个断言文件，1100+ 项）
├── _ui_harness.js         ← 前端测试台（node 里用假 DOM 真跑前端 JS）
├── _push_github.py        ← 把项目【脱敏后】推到 GitHub 用
├── deploy.sh              ← 一键部署到服务器（在服务器上跑）
│
├── config.json            ← 面板配置【含密码】
├── bots.json              ← 所有机器人【含全部 token，机密】
├── merchants.json         ← 商户账号【含密码哈希，机密】
├── data/                  ← 运行时生成：账本、订单、消息归档、客户图片
└── 运行日志.txt
```

### ★ 为什么按类型分文件夹

**改一个类型，不碰到别的类型。** 记账出问题，打开 `runners/ledger/` 就够了，
不会路过商城的代码 —— 每个类型的面板接口也在自己的 `api.py` 里，
改记账的接口不会碰到商城的一个字。

**但共享的东西是绕不开的**，改这几个要想清楚（详见 `runners/__init__.py`）：

| 文件 | 谁在用 |
|---|---|
| `core.py` | 所有类型 |
| `tron.py` | usdt + shop（链上查询、地址编解码、汇率） |
| `panel.py` | 所有类型（路由表） |
| `archive.py` | ledger（群消息记录） |

★ 类型字符串 `'kefu'` / `'usdt'` / `'shop'` / `'ledger'` 是**存在 bots.json 里的数据**，
**不许改** —— 改了存量机器人全部认不出来。

### ★ 只想改某一个机器人？用 `codes/`

不要为了一个客户去改 `runners/` 里的公共代码（那会改到所有同类型的机器人）。
建 `codes/<机器人id>.py`，**只影响那一个**。用法见 `codes/README.md`。

### 安全提示

| 文件 | 内含 | 泄漏后果 |
|---|---|---|
| `bots.json` | **所有机器人的 token** | 别人可以完全接管你的机器人 |
| `config.json` | 面板密码、登录签名密钥 | 别人能登你的面板 / 伪造登录态 |
| `merchants.json` | 商户密码哈希 | 可以被离线爆破 |
| `data/` | 客户聊天记录、图片、账本、收款地址 | 隐私泄漏 + 资金风险 |

**这几个绝对不要上传到 GitHub、网盘或发给别人。**

> 本仓库里的这三个 `.json` **已经换成占位符了**（用 `_push_github.py` 处理的），
> 拉下来要自己填。详见该脚本开头的注释。

---

## 三、架构

### 分层

| 文件 | 内容 | 职责 |
|---|---|---|
| `core.py` | `TgAPI` | 封装 Telegram Bot API：网络重试、429 限流自动等待 |
| | `BaseRunner` | **机器人基类**（Thread）。公共逻辑都在这：绑定管理员、发消息、拉取更新、数据存取、周期任务、定制文件钩子 |
| | `load_code` | 读 `codes/<机器人id>.py`（某一个机器人的定制代码） |
| `tron.py` | `Tron` | TRON 链上接口（TronGrid），取 USDT-TRC20 转账、查余额 |
| `manager.py` | `BotManager` | 增删机器人、启停线程、读写 `bots.json`、给前端提供快照 |
| | `RUNNERS` | **类型注册表**，新类型加这里 |
| `panel.py` | `PanelHandler` | HTTP 路由 + 密码鉴权（框架接口），页面从 `panel_page.html` 读 |
| `runners/kefu/` | `KefuRunner` | 客户消息中转到管理员、回复转发、拉黑、客户列表 |
| `runners/usdt/` | `UsdtRunner` | 地址查询、地址监听、进出账推送 |
| `runners/shop/` | `ShopRunner` | 商城下单发货；`store.py` 数据层、`pay.py` 收款、`providers.py` 上游、`api.py` 面板接口 |
| `runners/ledger/` | `LedgerRunner` | 记账；`storage.py` 账本、`commands.py` 核心逻辑、`api.py` 面板接口 |
| `主程序.py` | `main()` | 读配置、起面板、拉起所有机器人、开浏览器 |

**两个原则**：
- 包内部用**相对导入**（`from .storage import ...`），引用共享模块用**绝对导入**（`from core import ...`）
- **懒加载要保持懒加载** —— `core.py` 里 import `archive`、`panel.py` 里 import 各类型 api、
  `shop/runner.py` 里 import `Rate`，都是写在函数里的，**提到模块顶层会成循环导入**，
  一启动就 `ImportError: partially initialized module`

### 线程模型

**一个机器人 = 一个 `BotRunner` 线程。**

- 各自跑 `getUpdates` 长轮询，互不阻塞
- 各自读写自己的 `data/<id>.json`，没有共享状态
- `on_tick()` 里跑周期性任务（USDT 助手用它扫链），间隔由 `tick_interval()` 决定
- 停止：设置 `stop_evt`，最多等一个长轮询周期（客服 20 秒 / USDT 5 秒）

### 接口清单

**框架层**（所有类型共用，实现都在 `panel.py`）：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` | 面板页面 |
| GET | `/messages` | 群消息记录页 |
| GET | `/api/bots` | 机器人列表 + 可用类型 |
| POST | `/api/bots` | 添加 `{note, token, type}` |
| POST | `/api/bots/<id>/enable` | 启用 |
| POST | `/api/bots/<id>/disable` | 停用 |
| POST | `/api/bots/<id>/newcode` | 换绑定码（并解绑） |
| POST | `/api/bots/<id>/unbind` | 只解绑 |
| POST | `/api/bots/<id>/delete` | 删除（含数据文件） |
| GET/POST | `/api/logins`、`/api/logins/reveal`、`/api/logins/clear` | 登录记录（打码 / 看真实 IP / 清空，后两个要验证码） |
| GET/POST | `/api/totp`、`/api/totp/bind`、`/api/totp/reset` | 谷歌验证码 |
| GET/POST | `/api/merchants` | 商户管理（管理员专属） |
| GET | `/manifest.webmanifest`、`/icon-*.png`、`/apple-touch-icon.png` | 「添加到桌面」的图标和配置（★ **免鉴权**，见下） |

### ★ 添加到桌面（手机）

手机打开面板 → 标题旁边点「📲 添加到桌面」→ 桌面多一个图标，
之后点图标直接进面板，**不用再输密码**（密码存在手机 localStorage 里，同一个源）。

**为什么点一下不一定能装上**（这是浏览器的规定，不是 bug）：

| 手机 | 能不能真「一键」 | 原因 |
|---|---|---|
| **iPhone** | ❌ 只能手动 | 苹果**不开放**这个能力，任何网页都做不到。按钮会弹图文教「分享 → 添加到主屏幕」 |
| **安卓** | ⚠️ 现在只能手动，以后可以 | Chrome 允许网页弹系统安装框（`beforeinstallprompt`），**但要求 HTTPS**。现在是 `http://IP`，拿不到这个事件，所以也走图文指引 |

★ **上了 HTTPS 之后，安卓这边自动就变成真一键了，代码一个字都不用改。**
图标和名字的配置在 `static/`（`_gen_icons.py` 生成），
⚠️ **改 `static/` 下的文件要同步改 `.spec` 的 `datas`**，不然 exe 里没有 ——
漏了的表现是「手机上加到桌面是个白图标、名字显示成网址」，电脑上完全看不出来。

★★ 这几条路由**必须免鉴权**：浏览器来取 manifest / 图标时**不会带 `X-Panel-Pass` 头**
（那是页面里的 JS 自己加的）。挂鉴权的话手机上就是白图标。
`_test_addhome.py` 里专门锁了这条。

### ★★ 客户自带服务器：只跑记账，消息回传到你这

客户不想用你的服务器时，给他一份**记账独立版**（没有面板 / 商城 / USDT / 客服 / 商户）。

```
客户的服务器                          你的服务器
记账机器人（精简版）                    你的面板
· 账本 / 账单 / 群管理                  · 收下来 → archive.record_result()
· 不存消息、没有记录界面   ──HTTP──>    · 三级下钻 / 未读 / 置顶 / 备注 / 图片
· relay.py 转发                        （**全是现成的，一行没重写**）
```

**两步（★ 全自动，不用粘任何东西）**：

1. 面板「添加机器人」勾上 **「跑在客户自己的服务器上」**
2. 操作列点 **「⬇️ 安装包」** → 浏览器直接下一个 zip
   （里面的 `config.json` **已经填好了**：机器人编号 + token + 你的地址）
   → 发给客户 / 自己拿去装 → 客户解压跑 `install.sh`，**零配置**

★ 命令行也能打：`python _build_solo.py --id X --token Y --url Z`
  （不带参数就是样板包，config 是占位符）

### ★★ 帮客户装（客户给了账号密码的时候）

**登录方式三种，看客户给什么**（★ 推荐第一种：密码不用给任何人）：

```bash
# ★ 情况一：跟客户换公钥
python _deploy_customer.py --pubkey     # 打印你的公钥 + 让客户跑的那条命令
python _deploy_customer.py --host 客户IP --user 客户账号 --key ~/.ssh/tg_server ...
# 情况二：客户给密码  → --pass 密码
# 情况三：客户给自己的私钥 → --key 私钥路径（私钥带密码再加 --keypass）
```

**先 `--check`（什么都不改），看到「可以装 ✓」再去掉它真装。**

```bash
# ① 体检
python _deploy_customer.py --host 1.2.3.4 --user root --pass 密码     --panel https://tgbotbot.duckdns.org --pw 面板密码 --bot 1923be77 --check
# ② 真装（自动从面板下包 → 传上去 → 装成服务 → 自检）
python _deploy_customer.py --host 1.2.3.4 --user root --pass 密码     --panel https://tgbotbot.duckdns.org --pw 面板密码 --bot 1923be77
# ③ 卸掉（幂等，不会留 systemd 残渣）
python _deploy_customer.py --host 1.2.3.4 --user root     --dir ~/tgledger --name tgledger --uninstall
```

★ **只碰两样东西**：装它的那个目录 + 一个 systemd 服务。
  不装系统包、不动防火墙、不碰别的服务。
★ **先体检再动手** —— 最费时间的是「装到一半发现 Python 太老」。
★ 客户服务器上**至少要有 Python 3.10**（Ubuntu 22.04/24.04、Debian 12 都行）。

★ **回传没有密钥**：暗号是两边从机器人 token 现算的（`core.ingest_sig`），
  **谁都不用生成、保存、复制、解释**（用户原话：「密钥我没看懂」）。

★★ 文件清单只有一份（`solo_pack.py`）——面板的按钮和命令行脚本都用它。
  别另写一份，漏个文件的表现是「客户那边装不上」，最难查。

**★ 客户版是从这一份代码生成的，不是第二个仓库**（`solo/` + `_build_solo.py`）——
改了记账重新生成就行，**不存在「改两遍漏一遍」**。

**要改这几个地方时先想清楚**：

| 规矩 | 为什么 |
|---|---|
| 鉴权只能用 `X-Ingest-Sig`（从 token 现算） | 让客户用面板密码 = 他能看所有商户；直接发 token = token 会泄露（能完全接管机器人） |
| **必须查到没到期** | 客户不续费数据就进不了库，不查等于白送 |
| `on_message` 只塞队列、立刻返回 | 它在记账的收消息循环里，发 HTTP 就是记账跟着变慢 |
| 客户版 `archive` 显式关掉 | 记账的归档**默认是开的**，不关会偷偷建库、下群图 |
| 图片只能客户下好传过来 | Telegram 的 `file_id` **不跨机器人通用**，你拿他的下不到 |

**各类型专属**（`panel.py` 里只有一张路由表，**实现**在各自的 `api.py`）：

| 路径 | 归谁 | 实现 |
|---|---|---|
| `/api/shop/*` | 商城 | `runners/shop/api.py` |
| `/api/ledger/*` | 记账 | `runners/ledger/api.py` |
| `/api/archive/*` | 群消息记录（目前只有记账有） | `runners/ledger/api.py` |

**鉴权**：所有 `/api/*` 需要请求头 `X-Panel-Pass: <面板密码>`。
密码含非 ASCII 字符时启动会自动重新生成（浏览器无法在 HTTP 头里发中文）。

---

## 四、运行 / 打包

```powershell
# 运行
python 主程序.py

# 打包
python -m PyInstaller TG客服多开版.spec --noconfirm --distpath dist --workpath build
```

> `TG客服多开版.spec` 里两处关键配置：
> - `datas` 里加了 `certifi` 证书，否则 exe 走 HTTPS 会失败
> - `datas` 里加了 `panel_page.html`，前端页面会被打进 exe

**打包后务必确认** `dist\TG客服多开版.exe` 的时间戳比打包开始时间新，再复制覆盖。
只看文件是否存在会复制到旧版（踩过这个坑）。

---

## 五、如何加新功能

### 1. 加一个新的机器人类型（最常见）

以「关键词监听机器人」为例，**五步**（第 ⑤ 步只有它要有面板接口时才做）：

**① 建文件夹写实现类** —— `runners/keyword/`：

```
runners/keyword/
├── __init__.py     from .runner import KeywordRunner
└── runner.py
```

```python
# runners/keyword/runner.py
from core import BaseRunner

class KeywordRunner(BaseRunner):
    kind = 'keyword'
    poll_timeout = 20

    def on_owner(self, msg):
        # 已绑定的管理员发来的消息
        ...

    def on_guest(self, msg):
        # 其他人发来的消息
        ...

    def on_tick(self):
        # 周期性任务（不需要就删掉）
        ...
```

`BaseRunner` 已经帮你处理好了：绑定流程（`/admin 绑定码`）、`/id`、
发消息（`self.send(chat_id, text)`）、发全体管理员（`self.send_to_admins`）、
数据存取（`self.data` + `self.save_data()`）、状态上报（`self.set_status`）。

★ 包内部引用别的文件用**相对导入**（`from .store import ...`），
引用公共模块用**绝对导入**（`from core import ...`）。

**② 注册类型**，在 `manager.py` 的 `RUNNERS` 里加一行：

```python
RUNNERS = {
    'ledger': (LedgerRunner, '记账机器人'),
    'kefu': (KefuRunner, '客服机器人'),
    'usdt': (UsdtRunner, 'USDT助手'),
    'shop': (ShopRunner, '商城机器人'),
    'keyword': (KeywordRunner, '关键词监听'),   # ← 新增
}
```

**③ 前端加选项**，在 `panel_page.html` 的 `<select id="type">` 里加：

```html
<option value="keyword">关键词监听</option>
```

**④ 打包清单**，在 `TG客服多开版.spec` 的 `hiddenimports` 里加上新模块：

```python
'runners.keyword', 'runners.keyword.runner',
```

**⑤（可选）要有面板接口** —— 在 `panel.py` 的**路由表**里登记，
实现写到它自己的 `runners/keyword/api.py`：

```python
# panel.py 的 do_GET / do_POST 里，就在那几行路由表旁边
if path.startswith('/api/keyword/'):
    from runners.keyword import api as keyword_api
    if keyword_api.handle_get(self, path):
        return
```

```python
# runners/keyword/api.py
def handle_get(h, path):
    """返回 True = 这个请求我处理了。h 就是 panel.PanelHandler"""
    ...
    return False
```

完事。面板会自动多出一个区块（因为前端按 `type` 分组渲染）。

### 2. 加一个和机器人无关的功能（比如卡密管理）

照 `BotManager` 的写法新建一个 `XxxManager`：

- 自己的 JSON 数据文件
- `load()` / `save()`
- 给前端提供 `snapshot()`
- 在 `主程序.py` 里实例化，挂到 `PanelHandler.xxx_mgr`
- 在 `panel.py` 里加一组 `/api/xxx/...` 路由
- 在 `panel_page.html` 里加一个 `<div class="card">` 区块

### 3. 加一条 Bot 指令

在对应 Runner 的 `on_owner()` 里加分支，参考 `runners/usdt/` 的 `cmd_watch` 写法。
记得同步更新 `help_text()`。

### 4. 改界面

只动 `panel_page.html`。CSS 在顶部 `<style>`，逻辑在底部 `<script>`。
改完**不需要重新打包 Python**，但因为这个文件打进 exe 了，
要么重新打包，要么把 `panel_page.html` 放在 exe 同目录（会优先读同目录的）。

> `panel.py` 的 `load_page()` 用 `resource_path()` 找文件：
> 打包后优先从 exe 解压目录读。开发时直接读同目录。

---

## 六、开发避坑

1. **`/admin` 绑定指令必须最先判断**
   在 `BaseRunner.handle()` 里，`text.startswith('/admin')` 要放在
   「判断是不是管理员」**之前**。否则未绑定时运营者本人也会被当成客户，
   绑定码永远发不出去 —— 这是整个系统唯一的入口。

2. **管理员列表以文件为准（文件优先，入参兜底）**
   `save_admins` 是写回 `bots.json` 的。`BaseRunner` 如果只从入参读，
   重启后绑定就丢了。

3. **面板密码必须纯 ASCII**
   它要走 HTTP 头，中文会让 `requests` 报 `UnicodeEncodeError`。
   `主程序.py` 的 `load_cfg()` 会自动换掉非 ASCII 密码，别删这个判断。

4. **`copyMessage` 带 `caption` 会失败**
   贴纸、语音、视频留言不支持 caption，必须退回「先发一条说明再复制」。
   见 `runners/kefu/` 的 `relay_to_admin()`。

5. **USDT 监听第一次扫必须只建基线**
   否则会把历史交易全推一遍。见 `runners/usdt/` 的 `_scan_one()`。

6. **TronGrid 会限流**
   扫链时每个地址之间要留间隔（现在 1.2 秒），返回 403/429 要能优雅降级。

7. **路径常量不要 `from core import BOTS_FILE`**
   那样测试或换目录时改不动。统一用 `core.BOTS_FILE` 这种模块引用写法。

8. **打包替换 exe 前先比时间戳**（见上面「运行 / 打包」）

---

## 七、USDT 助手说明

### 四个功能 + 底部菜单（模式式交互）

绑定后会带出 ReplyKeyboard 菜单（`UsdtRunner.menu()`），两行四个按钮：

| 按钮 | 做什么 | 改按钮文字 |
|---|---|---|
| 📡 地址监听 | 切到监听模式，发地址就加监听 | `BTN_WATCH` |
| 🔍 查U交易 | 切到查询模式，发地址就查询 | `BTN_QUERY` |
| 💱 查U汇率 | 买 U 的 TOP10 实时价 | `BTN_RATE` |
| 🗂 地址管理 | 列出监听地址 + 逐个删除按钮 | `BTN_MANAGE` |

按钮文字在 `runners/usdt/` 的 `BUTTONS` 里改。
`match_button()` 会认三种写法：带 emoji、不带 emoji、以及旧按钮名
（`OLD_LABELS` 里把「余额监听」映射到「地址监听」）。

菜单只会出现在：绑定成功的欢迎语、`/start`、`/help`、
以及点按钮后的回应里。告警消息不带菜单（免得刷屏）。

### ★ 关键设计：监听和查询是两条独立的路

用户明确要求「点监听就只监听，点查询就只查询，不要干多余的活」。
所以这里用**模式（mode）**来区分，而不是靠猜：

```
self._mode = {admin_id: 'watch' | 'query'}     # 内存里
```

- 点「📡 地址监听」→ `mode = 'watch'` → 后面发的地址走 `do_watch()`
  （只加监听，**不查链上任何东西**，然后问要不要加备注）
- 点「🔍 查U交易」→ `mode = 'query'` → 后面发的地址走 `do_query()`
  （只查询，**不加监听**）
- 没选模式就发地址 → `_ask_mode()` 弹 InlineKeyboard 二选一
  （`q:<地址>` / `w:<地址>`）

模式是粘性的：选了一次「地址监听」，后面连发几个地址都会加监听，
不用每个都点按钮。点另一个按钮才切换。

### ⏳ 时长 / 到期 / 自动删除

添加机器人时选时长，到期自动停用，停用满 7 天自动删除。

**档位**（`manager.DURATIONS`，改这里就改了选项）：
```python
('1d','1 天',1) ('30d','1 个月',30) ('90d','3 个月',90)
('180d','半年',180) ('365d','1 年',365) ('forever','永久',0)
```
`0 天 = 永久`，`expire_at = 0` 表示永不过期。

**三个时间戳**（存在 `bots.json`）：
- `expire_at` —— 到期时刻（0 = 永久）
- `expired_at` —— 实际进入停用状态的时刻（用来算删除倒计时）
- `created_ts` —— 添加时刻

**删除期限 = `expired_at` + `GRACE_DAYS`(7) 天**。
注意是**从停用时刻**算，不是从添加时刻 —— 用户确认过这一点。

**看门狗**（`BotManager._watchdog`，`CHECK_INTERVAL = 300` 秒）：
- `check_expiry()` 扫一遍：
  - `now >= expire_at` 且 status 不是 expired → `_expire_bot()`
  - status 是 expired 且过了删除期限 → `remove()`
- `_expire_bot()` 的顺序**不能改**：
  1. 先用 runner 给使用者发「已到期」通知（停用后就发不出去了）
  2. `stop_bot()`
  3. 再设 `enabled=False` / `status='expired'` / `expired_at=now`
     （因为 `stop_bot()` 会把 status 改成 'stopped'，必须放在后面覆盖）

**已到期的不能靠「启用」复活**，`set_enabled()` 会拒绝并提示用续期。
`extend()` 从「现在」重新算，清空 `expired_at`，清掉 runner 的提醒缓存。

> 老数据没有 `expire_at`，`load()` 里默认补成永久，别让它莫名到期。

### ★ 到期后线程不杀，改成「受限模式」

到期后 `_expire_bot()` **故意不 stop 线程** —— 线程要留着，
这样有人发消息时能回「已到期」，不然客户发消息石沉大海。

`should_run(b)` = `is_expired(b) or enabled`，所以到期的也会起线程。
重启后 `start_all()` 同样会把到期的拉起来。

**★ 判断「到没到期」只看持久化的时间戳，不看 `status`：**

`status` 不写进 `bots.json`（`save()` 里排除了它），重启后不准。
所以：
- `BaseRunner.expired()` → `mgr.is_expired(bot)` → 看 `expire_at`
- `should_run()` → 看 `is_expired()` 和 `enabled`
- `delete_deadline()` → 看 `expired_at`
- `load()` 里按 `expired_at` 恢复显示用的 `status='expired'`

（这是踩过的坑：一开始用 `status` 判断，重启后到期的机器人
线程起不来，永远回不了「已到期」。）

**收到消息时的分流**（`handle()` 里最前面）：
```python
if self.expired():
    self.on_expired(cid, frm, cid in self.admins())
    return
```
`on_expired()` 默认直接回那条到期提示（USDT 助手就这样）。
`KefuRunner` 覆盖了它：管理员发 → 回到期提示；
客户发 → **不回客户**，改成通知绑定者（带 30 分钟冷却，
`EXPIRED_NOTICE_COOLDOWN`），避免客户知道欠费又避免刷屏。

**到期文案**在 `core.EXPIRED_MSG`，改那里就改全部。

### 👥 操作人（一个机器人多人共用）

需求：朋友的店里多个客服要共用同一个机器人，不想每人建一个。
绑定者能在菜单里加人。

**数据模型**（`core.BaseRunner`）：
- `bot['admin_ids']` —— 所有有权限的人（绑定者 + 操作人）
- `bot['owner_id']` —— 绑定者，只有他能管操作人
- `bot['admin_names']` —— `{id: 名字}`，列表里显示用
- 老数据没有 `owner_id`，`manager.load()` 会自动补成 `admin_ids[0]`

**★ 关键技术限制：机器人无法用用户名反查用户 ID。**
Telegram Bot API 没有这个接口。所以「输入 @用户名 → 立即生效」
是做不到的 —— 机器人只认得「跟它说过话的人」。

因此走两条路，用户输入用户名后两条都给他：

1. **用户名匹配**（`accept_pending_op`）
   用户名存进 `data['pending_ops']`。任何人给机器人发消息时，
   先比对用户名；命中就自动加为操作人，并通知本人和绑定者。

2. **邀请深链**（`accept_invite`）
   生成 `https://t.me/<bot>?start=op_<code>`，
   对方点一下 → `/start op_XXXXXX` → 校验 code → 加为操作人。
   更可靠（没有用户名拼写问题），是一次性的。

`handle()` 里的判断顺序（别改乱）：
```
/start op_XXX  →  /admin  →  /id  →  是不是管理员  →  pending 用户名匹配  →  on_guest
```

**权限**：`op_callback()` 里对 `opadd` / `opdel:` 都先查 `is_owner()`，
操作人点这些按钮会被拒（`answerCallbackQuery` 回提示）。
`remove_admin()` 也会拒绝移除绑定者。

### 加备注的询问流程

`do_watch()` 加完监听后调 `_ask_note()`，把 `{aid: {address, at}}`
存进 `self._pending`，然后问「要加备注吗」。

用户下一条消息会先经过 `_handle_note()`（**优先级最高**）：

- 回复 `无`/`不用`/`no` 等 → 跳过
- 回复其他文字 → 当成备注（截断 40 字）
- 回复 `/开头` 或是按钮文字 → **取消等待，走正常流程**
  （这是个真 bug：用户在等备注时点按钮，按钮文字会被当成备注吞掉，
  已修，测试里有回归项）
- 超过 `NOTE_TTL`（5 分钟）→ 自动作废

`_mode` 和 `_pending` 都存在内存里，程序重启就清空 —— 失败模式很温和
（用户重新点一下按钮）。

### 🗂 地址管理（InlineKeyboard 删除）

`_manage_view()` 返回 `(文本, inline键盘)`，每个地址一个删除按钮，
`callback_data` 是 `del:<完整地址>`（34 字符地址 + 4 = 38 字节，
没超过 Telegram 的 64 字节上限）。

点删除 → `on_callback()` 里：
1. `answerCallbackQuery` 回一个提示（不然按钮会一直转圈）
2. 从 `watches` 移除 + 存盘
3. `editMessageText` 刷新那条消息，删掉的按钮消失

**注意**：`core.BaseRunner.allowed_updates` 要包含 `callback_query`，
不然 Telegram 根本不推按钮点击事件。`handle()` 里也做了分流，
只允许**已绑定的管理员**触发回调。

### 链上数据

**数据源**：TronGrid 官方接口（`api.trongrid.io`），免费、无需 API Key。
Tronscan 的接口限流更严（3 rps），不适合轮询，所以没用。

**查余额**：`/v1/accounts/{address}` —— 返回 TRX 余额（单位 SUN，除以 1e6）
和 `trc20` 数组（找 USDT 合约地址那项，除以 1e6）。

**查交易**：`/v1/accounts/{address}/transactions/trc20`，
只保留 `type == 'Transfer'` 且 `token_info.address == USDT 合约` 的记录，
过滤掉授权（`Approve`）等噪音。

### 监听怎么工作

- 每个地址保存已见过的交易哈希（最多 300 条）和最后时间戳
- 每 45 秒扫一轮，用 `min_timestamp` 只取增量
- 查询时往前多取 2 分钟，防止同一时间戳的交易漏掉（重复靠哈希去重）
- 新交易按时间正序推送，带备注、金额、对方地址、时间、哈希、
  **最新余额**、区块浏览器链接

### 汇率（买 U 的 TOP10）

**数据源**：欧易(OKX) C2C 接口
`https://www.okx.com/v3/c2c/tradingOrders/books`

只取 `side=sell`（商家在卖 U = 你买入的价），按价格从低到高取前 10 条，
每条带 `price`、`nickName`（商家名）、`availableAmount`（可卖数量）。

用户明确要求**只显示买 U 的价格，不要卖 U 的价格**，
所以 `Rate.top()` 只查一侧，`cmd_rate()` 也只渲染这一侧。

展示格式（用户明确要求）：
- 价格保留 **2 位小数**（`%.2f`）
- **不显示商家可卖数量**（`availableAmount` 仍然解析进来备着，只是不渲染）
- 列表下面**不要**任何说明性文字
- `📍 最低价` = 10 条里最便宜的那个
- `📌 第三档汇率` = **第 3 条的价格**（`rows[2]['price']`，
  不足 3 条时不显示）

带 1 分钟缓存（`RATE_TTL`），避免点太快被限流；取不到就用上次的缓存。
请求头必须带 `Referer`，不加可能被拒。

> 试过的其它源：Binance P2P（能用）、CoinGecko（市场价，不是场外价）。
> 用户指定要用欧易，所以最终用 OKX。

**注意**：整个 USDT 模块是**被动读公开链上数据和公开行情**，
不涉及任何钱包私钥，也不能转账。想让它动钱是做不到的。

---

## 八、商城机器人说明

**这是第一类「动钱」的机器人**。前两类全是只读的，这一类要收款、下单、
发货（能量走上游 API，将来兑换走热钱包）。安全等级完全不同。

### 文件与分工

| 文件 | 职责 |
|---|---|
| `runners/shop/runner.py` | `ShopRunner` —— TG 交互：菜单、下单、付款展示、发货编排 |
| `runners/shop/store.py` | 流水 / 用户档案 / 商品配置 / `StoreShim`（面板用） |
| `runners/shop/pay.py` | `PayWatcher` —— 扫收款地址、认单、发货 |
| `runners/shop/providers.py` | 能量上游适配（`EnergyProvider` 抽象 + 各家实现） |

### ★★ 能量：谁转账，就把能量发给谁

**没有订单表、不用客户登记地址、没有唯一金额匹配。**
客户从自己钱包转 TRX 到收款地址 → 扫到这笔转账的 `from` 是谁 →
能量就发给那个地址（`runners/shop/store.py` 的 `credit(frm, trx, energy)`）。
所以既不会发错人，也没有「金额对不上」的问题。

金额按倍数折算（`runners/shop/store.py` 的 `calc()``）：**3.5 TRX = 1 个单位 = 65,000 能量**。

> ⚠️ 早期设计过「固定地址 + 随机小尾数匹配订单 + 订单表」那一套
> （`create_order` / `alloc_amount` / `deliver_energy`）——
> **已经整个删掉了**，别再照那个改。

### ★★ 会员 / 笔数：金额只加 ≤0.1 的零头

会员是「给某个 TG 用户名开通」，钱和用户名天然对不上号，
所以走**先下单后付款**：客户登记自己要开通的**用户名**和**付款地址**。

- 下单金额 = 标价 + `0 ~ 0.1` 随机零头（`alloc_premium_amount(base, span=0.1)`）
- 认单靠**付款地址**（`premium_match_amount(amount, coin, frm)`），
  不是靠独一无二的金额 —— 同价订单靠地址分辨

### ★★ 收款识别顺序（`runners/shop/pay.py` 的 `handle_one`，动之前想清楚）

1. 金额**精确匹配**某个待付款会员订单 → 开会员
2. 金额**贴着**某个待付款会员订单（容差 1.5%）→ **挂起来等人工，绝不发能量**
   —— 客户从交易所付款会被扣手续费，一旦掉进能量逻辑就按倍数白送一大把能量
3. 都不匹配 → 才走「转账就发能量」那条路

### ★★ 资金安全：发货前先落标记，结果不明绝不重发

`runners/shop/pay.py` 的 `PayWatcher.deliver()` 调上游**之前**先落一个 `dispatched` 时间戳。
之后只要这个标记还在，就说明上次请求已经打给上游了、结果不明
（超时/断网/崩溃）—— **绝不自动重发**，挂「待处理」等人工核对，
因为重发会**再扣一次钱**。

只有上游**明确**返回失败（钱没扣）才清掉标记、允许自动补发
（退避 60s / 300s / 1800s，最多 3 次）。
`runners/shop/providers.py` 的 `UncertainError` 就是用来区分「结果不明」和「确定失败」的。

面板上手动点「重发」= `manual=True`，表示人已核对过，才允许越过这层保护。

### 上游能量适配

`EnergyProvider` 抽象基类，统一 `quote()` / `order()` / `status()`。
现在只有 **`MockProvider` 是真能跑的**（不花钱、立即成功），
真实供应商的 HTTP 调用等选定后填 —— 照 `tron.py` 里 `Rate` 类的模式写。

`MerxProvider` / `TronRentalProvider` / `GetBlockProvider` 已留好骨架，
类注释里记了调研到的端点和认证方式（2026-09）。

### 热钱包（Phase 3，还没做）

计划：`tronpy` + `cryptography.fernet` 加密私钥，启动时控制台输密码解密。
安全底线：总开关默认关、单笔上限、转账前查余额、全部写日志。
**没密码时程序照常跑**，只是兑换类发货转人工。

### 面板操作

- 类型下拉选「商城机器人」
- 「⚙️ 配置」：收款地址（**必填**，不填客户下不了单）、上游、加价率、闪兑/钱包开关
  - 改收款地址会**自动重置扫描基线**（否则新地址的历史转账会被当成新付款）
- 「📋 订单」：订单列表 + 手动核销 + 标记发货；
  **机器人在不在跑都能用**（走 `StoreShim` 直接读文件）

### 运营指令（机器人里，仅管理员）

```
/订单          看最近 10 条订单
/发货 A1001    标记某个订单已发货
/商户          看收款配置状态
```

## 九、已知限制

- **必须一直运行**：程序关了所有机器人下线。客户消息会积压在 Telegram 服务器
  （约保留 24 小时），重启后能收到。
- **一个 token 只能一个进程**：同一 token 在两处跑会 409 冲突，面板会显示「冲突」。
- **停止有延迟**：客服机器人最多 20 秒，USDT 助手最多 5 秒。
- **`http.server` 单请求单线程**：几十个机器人够用，上百个建议换 `aiohttp` / FastAPI。
- **面板无 CSRF 防护**：靠密码 + 本地绑定。上公网务必加 HTTPS 反代 + 复杂密码。

---

## 十、上服务器准备清单

**部署方式**（两条路，见开头「〇、服务器部署」）：

- [ ] **Python 部署**（推荐，服务器上这么跑）：`pip install -r requirements.txt`
      → `python 主程序.py` → 配 systemd 常驻
- [ ] **Windows 部署**：拷 `TG客服多开版.exe` 过去双击

**配置**：

- [ ] `config.json` 的 `host` 改成 `0.0.0.0`（默认只有本机能访问）
- [ ] `config.json` 的 `password` 改成自己设的复杂密码（纯 ASCII）
- [ ] `bots.json` / `merchants.json` 填成你自己的（仓库里是占位符）——
      或者直接把本地那份拷过去
- [ ] 防火墙只放行面板端口，或用 Nginx 反代 + HTTPS
      （★ 面板没有 CSRF 防护、密码走明文头，**别把端口直接暴露到公网**）

**机器人侧**（不做这些功能会残）：

- [ ] ★★ **记账机器人：去 @BotFather 关掉 Group Privacy**
      （`/mybots` → 选机器人 → Bot Settings → Group Privacy → Turn off），
      关完把机器人**踢出群再重拉一次**。不关的话群里发 `+100` 机器人收不到，
      记账功能全废
- [ ] 机器人在群里要有**删除消息**权限（群管理「清理」功能要用）
- [ ] 想用群消息记录，同样要关 Group Privacy

**运维**：

- [ ] 配成开机自启（Linux systemd / Windows 计划任务）
- [ ] 定期备份 `bots.json`、`merchants.json` 和 `data/`
      （这仨丢了客户数据和绑定关系就没了）
- [ ] 程序必须**一直开着**：关了所有机器人下线，客户消息会在 Telegram
      服务器积压（约 24 小时），重启后能补收到

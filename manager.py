# -*- coding: utf-8 -*-
"""机器人管理器：增删机器人、启停线程、读写 bots.json

以后要加新的机器人类型，只要：
    1. 写一个 XxxRunner(BaseRunner) 的类
    2. 加进下面 RUNNERS 这张表
    3. 前端加一个类型选项
"""
import os
import shutil
import threading
import time
import copy
import importlib
from datetime import datetime

import core
from core import TgAPI, TgError, gen_code, gen_id, load_json, log, save_json

# 路径统一走 core.xxx（而不是 from core import），
# 这样测试或换目录时改 core 的属性就能生效

# 机器人类型：type 值 -> (实现类, 中文名)
# ★ 顺序 = 用户指定的展示顺序：记账 → 客服 → USDT → 商城（2026-09-28）
#   改这里的同时，前端的 `<select id="type">` 和 `TYPE_TABS` 也要一起改，
#   三处保持同一个顺序。（前端现在不读这里的顺序，但别让它俩对不上）
RUNNERS = {
    'ledger': ('LedgerRunner', '记账机器人'),
    'kefu': ('KefuRunner', '客服机器人'),
    'usdt': ('UsdtRunner', 'USDT助手'),
    'shop': ('ShopRunner', '商城机器人'),
}
# ★ 老数据没有 type 字段时按客服算 —— 这个是**兜底**，跟上面的展示顺序无关，
#   别因为它排第二就以为默认变了
DEFAULT_TYPE = 'kefu'

# 时长选项：key -> 天数（0 表示永久）
DURATIONS = [
    ('1d',      '1 天',    1),
    ('30d',     '1 个月',  30),
    ('90d',     '3 个月',  90),
    ('180d',    '半年',    180),
    ('365d',    '1 年',    365),
    ('forever', '永久',    0),
]
DURATION_MAP = {k: (label, days) for k, label, days in DURATIONS}
DEFAULT_DURATION = 'forever'
# 机器人划给商户时要清掉的商城配置 —— 别把自己（或上一个人）的
# 收款地址、上游 Key、通知群带过去。不清的话，商户在自己后台点一下
# 「配置」就全看见了，收款地址还会把钱收进别人口袋。
SHOP_CLEAR_ON_ASSIGN = ('trx_own', 'provider_key', 'premium_key',
                        'group_ids', 'contact', 'notice')

GRACE_DAYS = 7          # 停用后再过这么多天就自动删除
CHECK_INTERVAL = 300    # 多久检查一次到期（秒）


def type_name(t):
    return RUNNERS.get(t, (None, t))[1]


def duration_label(key):
    return DURATION_MAP.get(key, DURATION_MAP[DEFAULT_DURATION])[0]


def days_to_ts(days):
    """天数转成到期时间戳；0（永久）返回 0"""
    return int(time.time() + days * 86400) if days > 0 else 0


def make_runner(manager, bot):
    if bot.get('instance_folder') and os.environ.get('PANEL_INSTANCE_CHILD') != '1':
        from bot_instances import InstanceRunner
        return InstanceRunner(manager, bot)
    kind = bot.get('type') or DEFAULT_TYPE
    if kind not in RUNNERS:
        kind = DEFAULT_TYPE
    cls = getattr(importlib.import_module('runners.'+kind), RUNNERS[kind][0])
    return cls(manager, bot)


class BotManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self._add_lock = threading.Lock()
        self.bots = []          # 列表，保持添加顺序
        self.runners = {}       # id -> Runner 实例
        self.watchdog_stop = threading.Event()
        self._instance_seen = {}
        self.load()

    # -------- 读写 --------
    def load(self):
        d = load_json(core.BOTS_FILE, {"bots": []})
        self.bots = d.get('bots') or []
        for b in self.bots:
            b.setdefault('type', DEFAULT_TYPE)      # 老数据没有 type，默认客服
            b.setdefault('status', 'stopped')
            b.setdefault('error', '')
            b.setdefault('admin_ids', [])
            # 老数据没有 owner_id：把第一个绑定的人当绑定者
            if not b.get('owner_id'):
                ids = b.get('admin_ids') or []
                if ids:
                    b['owner_id'] = ids[0]
            # 老数据没有时长：当永久，别让它莫名其妙到期
            b.setdefault('duration', DEFAULT_DURATION)
            b.setdefault('expire_at', 0)
            b.setdefault('expired_at', 0)
            # 归属哪个商户（空 = 管理员直管）。老数据全部天然归管理员
            b.setdefault('mid', '')
            # status 不写进 bots.json，重启后按持久化的时间戳恢复显示
            # （逻辑判断不依赖它，这里只是为了面板显示对）
            if int(b.get('expired_at') or 0):
                b['status'] = 'expired'
            # 商城机器人要有 shop 配置块，老记录没有就补默认
            if (b.get('type') or DEFAULT_TYPE) == 'shop':
                b.setdefault('shop', {})
            # ★ 记账机器人的群消息记录**默认就开**（用户 2026-09-28 明确：
            #   「机器人默认都是开的，只有消息列表是我个人选择开或者关的」）。
            #   这里把「没写过」的补成显式的 True，面板和前端看到的值才一致。
            #   ★ 已经显式关掉的（比如换商户时被清掉的）**保持关**，别覆盖。
            if (b.get('type') or DEFAULT_TYPE) in core.ARCHIVE_TYPES:
                arc = b.get('archive')
                arc = dict(arc) if isinstance(arc, dict) else {}
                arc.setdefault('enabled', True)
                b['archive'] = arc
            # ★ 老数据里可能有个 ingest_key 字段 —— 现在不用了
            #   （回传暗号改成从 token 现算，见 core.ingest_sig）。
            #   留着不删：万一已经发给过客户，删了反而对不上。
            if b.get('instance_folder'):
                self.find(b['id'])

    def save(self):
        with self.lock:
            from bot_instances import sync
            for bot in self.bots:
                if bot.get('instance_folder'):
                    self._instance_seen[bot['id']] = sync(bot, self._instance_seen.get(bot['id']),
                        commit=True, child=os.environ.get('PANEL_INSTANCE_CHILD') == '1')
            if os.environ.get('PANEL_INSTANCE_CHILD') == '1' and self.bots and self.bots[0].get('instance_folder'):
                return
            save_json(core.BOTS_FILE, {"bots": [
                {k: v for k, v in b.items() if k not in ('status', 'error')}
                for b in self.bots
            ]})

    def find(self, bid):
        for b in self.bots:
            if b['id'] == bid:
                if b.get('instance_folder'):
                    from bot_instances import sync
                    with self.lock:
                        self._instance_seen[bid] = sync(b, self._instance_seen.get(bid),
                            child=os.environ.get('PANEL_INSTANCE_CHILD') == '1')
                return b
        return None

    def of_type(self, t):
        return [b for b in self.bots if (b.get('type') or DEFAULT_TYPE) == t]

    # -------- 增删改 --------
    def add(self, note, token, bot_type=DEFAULT_TYPE, duration=DEFAULT_DURATION,
            mid='', remote=False, node_id=None):
        with self._add_lock:
            return self._add(note,token,bot_type,duration,mid,remote,node_id)

    def _add(self, note, token, bot_type=DEFAULT_TYPE, duration=DEFAULT_DURATION,
             mid='', remote=False, node_id=None):
        """加一个机器人。

        remote=True = 「客户自建」：跑在客户自己的服务器上，
        面板**不启动**它（见 should_run），消息靠它主动回传。
        """
        token = (token or '').strip()
        if not token or ':' not in token:
            return None, 'token 格式不对，应该是 数字:字母数字 的形式'
        if bot_type not in RUNNERS:
            return None, '不支持的机器人类型'
        if duration not in DURATION_MAP:
            duration = DEFAULT_DURATION
        registry = getattr(self,'nodes',None)
        node_id = (registry.state['default'] if registry and not remote else '') if node_id is None else node_id
        if node_id:
            if remote:
                return None, '运行服务器与客户自建不能同时选择'
            from nodes import for_manager, NodeError
            registry = for_manager(self)
            try:
                registry.call(node_id,'info',timeout=5)
            except NodeError as exc:
                return None, str(exc)

        for b in self.bots:
            if b.get('token') == token:
                return None, '这个 token 已经添加过了（备注：%s）' % (b.get('note') or '无')

        try:
            me = TgAPI(token).call('getMe')
        except TgError as e:
            return None, '验证失败：%s' % e
        except Exception as e:
            return None, '验证失败：%s' % e

        bid = gen_id()
        while self.find(bid):
            bid = gen_id()

        bot = {
            'id': bid,
            'type': bot_type,
            'note': (note or '').strip() or me.get('first_name') or '未命名',
            'token': token,
            'username': me.get('username') or '',
            'name': me.get('first_name') or '',
            'bind_code': gen_code(),
            'admin_ids': [],
            'admin_name': '',
            'admin_username': '',
            'enabled': True,
            'created': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'created_ts': int(time.time()),
            'duration': duration,
            'expire_at': days_to_ts(DURATION_MAP[duration][1]),
            'expired_at': 0,
            'mid': (mid or '').strip(),     # 归哪个商户，空 = 管理员直管
            # ★ 记账机器人一加进来就默认开始记录群消息（见 load() 里那段注释）
            'archive': ({'enabled': True}
                        if bot_type in core.ARCHIVE_TYPES else {}),
            # 客户自己服务器上那台（面板**不启动**它，见 should_run）——
            # 消息靠它主动回传，见 runners/ledger/api.py 的 handle_ingest
            # ★ 回传**不需要单独的密钥**：暗号是从 token 现算的
            #   （core.ingest_sig），两边都能算，用户不用管。
            'remote': bool(remote),
            'status': 'starting',
            'error': '',
        }
        if node_id:
            bot.update(node_id=node_id,node_pending=True)
            with self.lock:
                self.bots.append(bot)
                self.save()
            try:
                registry.provision(bot)
            except NodeError:
                bot.update(status='error',error='部署结果待确认，请重试部署；不会在本机启动')
                self.save()
            return bot,None
        if os.environ.get('PANEL_INSTANCE_CHILD') != '1':
            from bot_instances import provision
            try:
                provision(bot, self.cfg)
                self._instance_seen[bid] = copy.deepcopy({k: v for k, v in bot.items() if k not in ('status', 'error')})
            except (OSError, ValueError) as exc:
                return None, '建立独立机器人目录失败：%s' % exc
        with self.lock:
            self.bots.append(bot)
            self.save()
        self.start_bot(bid)
        log('已添加%s：%s (@%s) 时长 %s'
            % (type_name(bot_type), bot['note'], bot['username'], duration_label(duration)))
        return bot, None

    def assign(self, bid, mid):
        """把机器人分配给某个商户。mid 传空 = 收回，归管理员直管"""
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        b['mid'] = (mid or '').strip()
        self.save()
        log('%s 的归属改成了 %s'
            % (b.get('note') or bid, b['mid'] or '管理员直管'))
        return True, ''

    def of_merchant(self, mid):
        """某个商户名下的机器人"""
        m = (mid or '').strip()
        return [b for b in self.bots if (b.get('mid') or '') == m]

    def assign_bots(self, mid, bids, clear_secrets=True):
        """把 bids 里这些机器人划给商户 mid；原本属于他但不在 bids 里的收回来。

        返回 (划过来几台, 收回几台, [配置被清空的 bot id])

        ★★ clear_secrets 很关键：不清的话，商户在自己后台点「配置」
           就直接看到**你的收款地址和上游 API Key**（商城机器人的 shop 块里
           存着 trx_own / provider_key / premium_key）。
           调用方拿到第三个返回值后要顺手重置扫描基线，否则新收款地址的
           历史转账会被当成新收款，白送能量出去。
        """
        mid = (mid or '').strip()
        want = set(bids or [])
        moved = taken = 0
        cleared = []
        handover = []          # 换主人的记账机器人（要清账本 + 重启）
        registry = getattr(self,'nodes',None)
        if registry:
            for nid in {b['node_id'] for b in self.bots if b.get('node_id') and
                        (b['id'] in want or (mid and b.get('mid')==mid))}:
                result = registry.call(nid,'assign',dict(mid=mid,bids=[b['id'] for b in self.bots if b.get('node_id')==nid and b['id'] in want],clear=clear_secrets))
                moved += result['moved'];taken += result['taken'];cleared += result['cleared']
                registry.refresh(nid)
        with self.lock:
            for b in self.bots:
                if b.get('node_id'):
                    continue
                bid = b['id']
                if bid in want:
                    if (b.get('mid') or '') != mid:
                        b['mid'] = mid
                        moved += 1
                        # ★ 一换主人，上一个主人留下的数据必须删干净：
                        #   · 记账机器人 → 账本（否则新商户的「广播/清理」
                        #     菜单里会列出上个商户的群名和成员名单）
                        #   · 开了群消息记录的 → 归档和图片（更直接，
                        #     那就是上个商户的群聊内容）
                        #   文件被占着删不掉，所以只打标记，
                        #   等重启时（线程已停）再删。
                        if ((b.get('type') or '') == 'ledger'
                                or (b.get('archive') or {}).get('enabled')):
                            b['wipe_data'] = True
                            handover.append(bid)
                        # 归档开关不给下家留着 —— 他要记录得自己开
                        if (b.get('archive') or {}).get('enabled'):
                            b['archive'] = dict(b.get('archive') or {},
                                                enabled=False)
                            if bid not in cleared:
                                cleared.append(bid)
                    if clear_secrets and (b.get('type') or '') == 'shop':
                        sh = b.get('shop') or {}
                        hit = False
                        for k in SHOP_CLEAR_ON_ASSIGN:
                            if sh.get(k):
                                sh[k] = ''
                                hit = True
                        if hit:
                            b['shop'] = sh
                            cleared.append(bid)
                elif mid and (b.get('mid') or '') == mid:
                    b['mid'] = ''
                    taken += 1
                    if ((b.get('type') or '') == 'ledger'
                            or (b.get('archive') or {}).get('enabled')):
                        b['wipe_data'] = True
                        handover.append(bid)
            self.save()
        # ★ 重启换手的记账机器人：线程停了文件才删得掉，删完再起来就是空账本
        #   ★ 一定要 wait_runner(返回的对象) —— 等不到就删不掉，
        #     机器人会带着**上一个商户的群和成员名单**起来
        for bid in handover:
            try:
                self.wait_runner(self.stop_bot(bid))
                self.start_bot(bid)
            except Exception as e:
                log('重启记账机器人 %s 失败：%s' % (bid, e))
        log('分配机器人：%d 台划给 %s，%d 台收回%s%s'
            % (moved, mid or '（空）', taken,
               ('，%d 台清空了配置' % len(cleared)) if cleared else '',
               ('，%d 台清空了账本' % len(handover)) if handover else ''))
        return moved, taken, cleared

    def reset_shop_scans(self, cleared):
        for bid in cleared:
            bot = self.find(bid)
            if not bot or bot.get('type')!='shop' or bot.get('node_id'):
                continue
            runner = self.runners.get(bid)
            if bot.get('instance_folder') and runner and runner.is_alive():
                with runner.request('/instance/shop-reset',{}):
                    pass
            else:
                from runners.shop import store
                current = store.store_for(self,bid)
                if current is not None:
                    current.data['scan']={'baseline':False,'seen':[],'last_ts':0}
                    current.save()

    def set_archive_cfg(self, bid, patch):
        """改「群消息记录」的开关和保留天数。

        ★ 任何类型的机器人都能用（不只是记账）。开了之后：
          · 机器人开始收群消息 → 前提是在 BotFather 关掉 Group Privacy
          · 消息落盘到 data/<bid>.archive.sqlite3（只在你自己的机器上）
        ★ 开关变化要重启机器人线程才生效（收消息的分支要重新走），
          调用方负责 restart。
        """
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        # ★ 只有记账和客服能开（用户指定）——
        #   前端已经过滤了，这里再拦一道，免得绕过界面直接打接口
        btype = b.get('type') or ''
        if btype not in core.ARCHIVE_TYPES:
            return False, '%s 不支持消息记录（只有记账机器人和客服机器人可以）' \
                          % type_name(btype)
        cfg = dict(b.get('archive') or {})
        if 'enabled' in (patch or {}):
            cfg['enabled'] = bool(patch['enabled'])
        with self.lock:
            b['archive'] = cfg
            self.save()
        return True, ''

    def set_archive_mute(self, bid, chat_id, mute):
        """按群开/关记录（面板群列表里那个按钮）

        ★ 是**整个不写库**，不是存了再隐藏 —— 关了就不该再占地方。
        ★ 已经记下的不动：关了之后老消息还在，到 48 小时自然删掉。
        """
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        cid = str(chat_id or '').strip()
        if not cid:
            return False, '没给群 id'
        with self.lock:
            cfg = dict(b.get('archive') or {})
            mutes = [str(x) for x in (cfg.get('mute_chats') or [])]
            if mute:
                if cid not in mutes:
                    mutes.append(cid)
            else:
                mutes = [x for x in mutes if x != cid]
            cfg['mute_chats'] = mutes
            b['archive'] = cfg
            self.save()
        log('群消息记录：%s 的群 %s %s' % (bid, cid, '停止记录' if mute else '恢复记录'))
        return True, ''

    def set_archive_chatnote(self, bid, chat_id, note):
        """给某个群加个备注（面板群列表里那个）

        ★ 这是**你自己的备注**，不是改群名 —— 只显示在你的面板上，
          不影响 Telegram 里那个群。
        """
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        cid = str(chat_id or '').strip()
        if not cid:
            return False, '没给群 id'
        note = (note or '').strip()
        if len(note) > 40:
            return False, '备注太长了（最多 40 个字）'
        with self.lock:
            cfg = dict(b.get('archive') or {})
            notes = dict(cfg.get('chat_notes') or {})
            if note:
                notes[cid] = note
            else:
                notes.pop(cid, None)      # 清空 = 删掉这条备注
            cfg['chat_notes'] = notes
            b['archive'] = cfg
            self.save()
        return True, ''

    def set_note(self, bid, note):
        """改机器人的备注名（面板上那个「备注」列）

        就是个显示名，随时能改，不影响任何功能。
        """
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        note = (note or '').strip()
        if not note:
            return False, '备注不能为空'
        if len(note) > 40:
            return False, '备注太长了（最多 40 个字）'
        with self.lock:
            b['note'] = note
            self.save()
        log('机器人 %s 的备注改成「%s」' % (bid, note))
        return True, ''

    def restart_bot(self, bid):
        """停→等→起。改完配置要靠它让新配置生效"""
        if not self.wait_runner(self.stop_bot(bid)):
            raise ValueError('原进程尚未退出，未启动第二个机器人进程')
        self.start_bot(bid)

    def set_remote_state(self, bid, data):
        """客户自己那台机器人**报上来的**状态（谁绑定了）。

        ★ 为什么需要：绑定是在**客户的服务器上**发生的，管理员 id 存在他
          那边的 data/<id>.state.json 里。面板自己那份永远是空的 ——
          不报回来的话，面板就永远显示「等待绑定」，客户明明已经绑好了。
          （2026-09-29 第一次真给客户装，就卡在这儿）

        ★ 只认下面这几个字段，别的丢掉 —— 这是从外面来的数据，
          别让它有机会改配置。而且只在 remote 的机器人上生效。
        """
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        if not b.get('remote'):
            return False, '这不是客户自建的机器人'
        ids = []
        for x in (data.get('admin_ids') or [])[:20]:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                pass
        with self.lock:
            had = list(b.get('admin_ids') or [])
            b['admin_ids'] = ids
            oid = data.get('owner_id')
            try:
                if oid:
                    b['owner_id'] = int(oid)
                elif ids:
                    b['owner_id'] = ids[0]
            except (TypeError, ValueError):
                pass
            if data.get('admin_name') is not None:
                b['admin_name'] = str(data.get('admin_name') or '')[:64]
            if data.get('admin_username') is not None:
                b['admin_username'] = str(data.get('admin_username') or '')[:64]
            self.save()
        if ids and not had:
            log('客户自建机器人 %s 绑定了管理员（%s）'
                % (bid, b.get('admin_name') or ids[0]))
        return True, ''

    def set_remote(self, bid, on):
        """把机器人改成「客户自建」（或改回来）。

        ★ 为什么要这个方法：那个勾选框只在**添加**的时候有 ——
          忘了勾就再也没法补，只能删掉重建，而重建会换一个 id，
          **已经发给客户的安装包当场失效**（里面的 id 对不上了）。
          所以留一个补票的口子。

        ★ 改完要重启线程：改成客户自建要停掉本机的（不然两边抢 token
          409）；改回来要把它启动起来。
        """
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        if (b.get('type') or '') != 'ledger':
            return False, '只有记账机器人能改成客户自建'
        on = bool(on)
        if bool(b.get('remote')) == on:
            return True, ''
        with self.lock:
            b['remote'] = on
            self.save()
        # ★ 方向不同处理不同：改成自建要先停掉本机的线程（别让它继续抢），
        #   改回本机跑要重新拉起来
        try:
            if on:
                r = self.stop_bot(bid)
                if r:
                    self.wait_runner(r, timeout=8)
            else:
                self.start_bot(bid)
        except Exception as e:
            log('切换 %s 的运行位置时出错：%s' % (bid, e))
        log('%s 改成了「%s」' % (bid, '客户自建' if on else '本机运行'))
        return True, ''

    def set_ledger_cfg(self, bid, patch):
        """改记账机器人的面板配置（现在只有 welcome_text 一项）

        群里的汇率/费率/日切是**在群里用命令设的**，存在各自的 sqlite 里，
        不走这儿 —— 所以别指望在这儿能改那些。
        """
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        if (b.get('type') or '') != 'ledger':
            return False, '这不是记账机器人'
        with self.lock:
            cfg = b.get('ledger') or {}
            for k, v in (patch or {}).items():
                cfg[k] = v
            b['ledger'] = cfg
            self.save()
        return True, ''

    def token_used(self, token, exclude=''):
        """这个 token 有没有被别的机器人占着。

        ★ 必须全库查 —— 两个商户填同一个 token 会互相抢 getUpdates，
          两边都收不到消息（现有 status='conflict' 就是这个）。
        """
        t = (token or '').strip()
        if not t:
            return None
        for b in self.bots:
            if b.get('token') == t and b['id'] != exclude:
                return b
        return None

    def set_token(self, bid, token):
        """换机器人的 token。返回 (是否成功, 错误说明)"""
        token = (token or '').strip()
        if not token or ':' not in token:
            return False, 'token 格式不对，应该是 数字:字母数字 的形式'
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        other = self.token_used(token, exclude=bid)
        if other:
            return False, '这个 token 已经被「%s」用了' % (other.get('note') or '无备注')
        try:
            me = TgAPI(token).call('getMe')
        except Exception as e:
            return False, '验证失败：%s' % e
        if not self.wait_runner(self.stop_bot(bid)):
            return False, '原机器人进程尚未退出，未更换Token'
        if b.get('instance_folder') and me.get('username') != b['instance_folder']:
            from bot_instances import folder
            old_path = folder(b)
            candidate = dict(b, instance_folder=me.get('username', ''))
            renamed = False
            try:
                new_path = folder(candidate)
                if new_path.exists():
                    raise ValueError('新用户名的目录已存在')
                old_path.rename(new_path)
                renamed = True
                manifest = load_json(str(new_path / 'INSTANCE.json'), {})
                manifest['username'] = candidate['instance_folder']
                save_json(str(new_path / 'INSTANCE.json'), manifest)
                b['instance_folder'] = candidate['instance_folder']
            except (ValueError, OSError) as exc:
                if renamed:
                    new_path.rename(old_path)
                self.start_bot(bid)
                return False, '实例目录改名失败：%s' % exc
        with self.lock:
            b['token'] = token
            b['username'] = me.get('username') or ''
            b['name'] = me.get('first_name') or ''
            self.save()
        self.start_bot(bid)
        log('%s 换了 token，新机器人 @%s'
            % (b.get('note') or bid, b.get('username')))
        return True, ''

    def extend(self, bid, duration):
        """续期：从「现在」重新算时长，并解除停用状态"""
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        if duration not in DURATION_MAP:
            return False, '不支持的时长'
        label, days = DURATION_MAP[duration]
        with self.lock:
            b['duration'] = duration
            b['expire_at'] = days_to_ts(days)
            b['expired_at'] = 0
            b['enabled'] = True
            b['status'] = 'starting'
            self.save()
        # 线程到期时没被杀，清掉「已提醒过」的记录让它重新算
        r = self.runners.get(bid)
        if r:
            r._expired_notice.clear()
        self.start_bot(bid)
        log('机器人 %s 已续期：%s' % (b.get('note'), label))
        return True, None

    def remove(self, bid):
        # ★ 必须等线程真的退出再删文件 —— 否则 sqlite 还开着，
        #   Windows 上删不掉，账本会留在硬盘上（商户数据泄露）
        #   ★ 拿 stop_bot 返回的对象去等，不能按 bid 查（那时已经摘掉了）
        bot = self.find(bid)
        if not bot:
            return False, '找不到这个机器人'
        if not self.wait_runner(self.stop_bot(bid)):
            return False, '机器人进程尚未退出，未删除目录和记录'
        if bot.get('instance_folder'):
            from bot_instances import remove
            try:
                remove(bot)
            except (OSError, ValueError) as exc:
                return False, '实例目录删除失败：%s' % exc
            with self.lock:
                self.bots = [b for b in self.bots if b['id'] != bid]
                self._instance_seen.pop(bid, None)
                self.save()
            log('已删除机器人 @%s 的整个独立目录' % bot['username'])
            return True, None
        with self.lock:
            self.bots = [b for b in self.bots if b['id'] != bid]
            self.save()
        # 除了 .json，还有账本、群消息位置、群消息记录（含图片目录）
        names = [bid + s for s in
                 ('.json', '.sqlite3', '.positions.sqlite3', '.archive.sqlite3',
                  '.sqlite3-wal', '.sqlite3-shm',
                  '.positions.sqlite3-wal', '.positions.sqlite3-shm',
                  '.archive.sqlite3-wal', '.archive.sqlite3-shm')]
        names += [bid + d for d in self.HANDOVER_DIRS]
        for name in names:
            p = os.path.join(core.DATA_DIR, name)
            if not os.path.exists(p):
                continue
            for _ in range(3):
                if not os.path.exists(p):
                    break
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                except OSError:
                    time.sleep(0.3)
            if os.path.exists(p):
                log('删不掉 %s（文件还占着，请手动删）' % name)
        log('已删除机器人 %s' % bid)
        return True, None

    def set_enabled(self, bid, enabled):
        b = self.find(bid)
        if not b:
            return False, '找不到这个机器人'
        # 已到期的不能靠「启用」复活，得走续期
        if enabled and self.is_expired(b):
            return False, '这个机器人已到期，请用「续期」恢复'
        with self.lock:
            b['enabled'] = bool(enabled)
            self.save()
        if enabled:
            self.start_bot(bid)
        else:
            self.stop_bot(bid)
        return True, None

    def new_code(self, bid):
        b = self.find(bid)
        if not b:
            return
        with self.lock:
            b['bind_code'] = gen_code()
            b['admin_ids'] = []
            b['admin_name'] = ''
            b['admin_username'] = ''
            # ★ 必须一起清 owner_id：core.owner_id() 是「有 owner_id 就用它」，
            #   不清的话，接手的人绑定了也当不了 owner，/操作人 那套全用不了
            b['owner_id'] = 0
            self.save()

    def unbind(self, bid):
        b = self.find(bid)
        if not b:
            return
        with self.lock:
            b['admin_ids'] = []
            b['admin_name'] = ''
            b['admin_username'] = ''
            b['owner_id'] = 0          # 同上
            self.save()

    # -------- 到期处理 --------
    def grace_days(self):
        return GRACE_DAYS

    def should_run(self, b):
        """这个机器人该不该跑线程。

        到期的也要跑 —— 线程留着才能回复「已到期」，不然客户发消息石沉大海。
        注意：判断只看持久化的时间戳，不看 status
        （status 不写进 bots.json，重启后不准）
        """
        # ★★ 「客户自建」的**绝对不能跑**：客户的机器上已经在跑了，
        #    同一个 token 两边同时 getUpdates = 409 冲突，两边都收不到消息。
        #    它只靠客户那边主动回传（见 runners/ledger/api.py）。
        if b.get('remote') or b.get('node_id'):
            return False
        return self.is_expired(b) or bool(b.get('enabled', True))

    def is_expired(self, b):
        """过了到期时间就算到期。只看 expire_at，重启后依然准确"""
        exp = int(b.get('expire_at') or 0)
        return bool(exp) and time.time() >= exp

    def delete_deadline(self, b):
        """返回自动删除的时间戳；0 表示不会被删"""
        dead = int(b.get('expired_at') or 0)
        return dead + GRACE_DAYS * 86400 if dead else 0

    def check_expiry(self):
        """扫描所有机器人：到点的停用，停用超期的删除"""
        now = time.time()
        to_expire, to_delete = [], []

        with self.lock:
            for b in list(self.bots):
                if b.get('node_id'):
                    continue  # The execution node owns expiry even while the panel is offline.
                exp = int(b.get('expire_at') or 0)
                if not exp:
                    continue                        # 永久，不管
                if int(b.get('expired_at') or 0):
                    # 已经在停用期，看出没出宽限期
                    dead = self.delete_deadline(b)
                    if dead and now >= dead:
                        to_delete.append(b['id'])
                elif now >= exp:
                    to_expire.append(b['id'])

        for bid in to_expire:
            self._expire_bot(bid)
        for bid in to_delete:
            b = self.find(bid)
            log('机器人 %s 停用已满 %d 天，自动删除'
                % ((b or {}).get('note') or bid, GRACE_DAYS))
            self.remove(bid)

        return len(to_expire), len(to_delete)

    def _expire_bot(self, bid):
        b = self.find(bid)
        if not b:
            return
        # 先通知所有使用者（这时候机器人还能发消息）
        r = self.runners.get(bid)
        if r:
            try:
                r.send_to_admins(
                    '⏰ 这个机器人已到期，现在暂停使用。\n\n'
                    '请及时续费。如果 %d 天内没有续费，'
                    '这个机器人会被自动删除。' % GRACE_DAYS)
            except Exception as e:
                log('发送到期通知失败：%s' % e)

        with self.lock:
            b['expired_at'] = int(time.time())
            b['enabled'] = False
            b['status'] = 'expired'
            self.save()

        # ★ 故意不停线程 —— 线程要留着，好在有人发消息时回「已到期」
        log('机器人 %s 已到期，暂停使用（%d 天内可恢复）'
            % (b.get('note'), GRACE_DAYS))

    def start_watchdog(self):
        t = threading.Thread(target=self._watchdog, daemon=True,
                             name='expiry-watchdog')
        t.start()

    def _watchdog(self):
        log('到期检查已启动（每 %d 秒一次）' % CHECK_INTERVAL)
        while not self.watchdog_stop.is_set():
            try:
                self.check_expiry()
            except Exception as e:
                log('到期检查出错：%s' % e)
            self.watchdog_stop.wait(CHECK_INTERVAL)

    def stop_watchdog(self):
        self.watchdog_stop.set()

    # -------- 线程管理 --------
    def start_bot(self, bid):
        b = self.find(bid)
        if not b:
            return
        if b.get('node_id'):
            return
        # ★ 清历史数据必须**走在 should_run 之前**：停用/过期的机器人也要清，
        #   否则商户把机器人停着不动，上个商户的群名就一直留在库里，
        #   等他哪天启用就直接看到了
        self._wipe_on_handover(b)
        if not self.should_run(b):
            return
        with self.lock:
            old = self.runners.get(bid)
            if old and old.is_alive():
                return
            if b.get('type') == 'ledger' and not b.get('instance_folder') and os.environ.get('PANEL_INSTANCE_CHILD') != '1':
                from bot_instances import provision
                provision(b, self.cfg, migrate=True)
                self._instance_seen[bid] = copy.deepcopy({k: v for k, v in b.items() if k not in ('status', 'error')})
                self.save()
            r = make_runner(self, b)
            self.runners[bid] = r
            r.start()

    def wait_runner(self, r, timeout=15):
        """等这个 runner 的线程真的退出，然后把它的 sqlite 关掉。

        ★★ 参数是 runner 对象、**不是 bid**，这点很关键：
           `stop_bot()` 会先把 runner 从 self.runners 里摘掉，
           摘完再按 bid 去查就查不到了 —— 那样既等不到线程退出、
           也关不掉 sqlite，文件一直占着删不掉。
           （记账机器人换商户要清账本，就靠这里真的等干净）
        """
        if r is None:
            return True
        if r.is_alive():
            try:
                r.join(timeout)
            except Exception:
                pass
        if not r.is_alive():
            closer = getattr(r, 'close_db', None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        return not r.is_alive()

    def wait_bot(self, bid, timeout=15):
        """按 id 等（runner 还在字典里时用这个）。

        ⚠️ 已经 stop_bot 过的查不到，会直接返回 True ——
           那种情况请改用 wait_runner(刚 stop_bot 返回的那个对象)。
        """
        with self.lock:
            r = self.runners.get(bid)
        return self.wait_runner(r, timeout)

    # 换主人时要删干净的东西：记账的账本 + 群消息记录（含图片）
    HANDOVER_FILES = ('.sqlite3', '.positions.sqlite3', '.archive.sqlite3')
    HANDOVER_DIRS = ('.archive-media',)

    def _wipe_on_handover(self, b):
        """换过主人的机器人：开机前把「上一个主人留下的数据」删掉

        ★ 不清的话，新商户的「广播/清理」菜单会列出上个商户的群名，
          「群消息记录」里更是能看到上个商户的群聊内容 —— 实打实的泄露。
        ★ 删不掉就留着标记，下次启动再试 —— **绝不能假装删成功**。
        """
        if not b.get('wipe_data'):
            return
        bid = b['id']
        leftover = []
        targets = [os.path.join(core.bot_data_dir(b), bid + s)
                   for s in self.HANDOVER_FILES]
        targets += [os.path.join(core.bot_data_dir(b), bid + d)
                    for d in self.HANDOVER_DIRS]
        for p in targets:
            exists = os.path.exists(p)
            for _ in range(3):
                if not exists:
                    break
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                except OSError:
                    time.sleep(0.4)     # 文件可能刚被释放，等一下再试
                exists = os.path.exists(p)
            if exists:
                leftover.append(os.path.basename(p))
        if leftover:
            log('换主人要清的数据没删掉（还占着）：%s —— 留到下次启动再试'
                % '、'.join(leftover))
            return
        with self.lock:
            b.pop('wipe_data', None)
            self.save()
        log('已清空机器人 %s 的历史数据（换主人）' % bid)

    def stop_bot(self, bid):
        """停掉机器人。★ 返回被停掉的 runner —— 要删文件的话
        必须拿它去调 wait_runner()，因为它已经从 runners 里摘掉了"""
        with self.lock:
            r = self.runners.pop(bid, None)
        if r:
            r.stop_evt.set()
        b = self.find(bid)
        if b:
            with self.lock:
                # 到期的状态别被覆盖掉，面板要显示「已到期」
                if b.get('status') != 'expired':
                    b['status'] = 'stopped'
                b['error'] = ''
        return r

    def start_all(self):
        for b in list(self.bots):
            if self.should_run(b):
                self.start_bot(b['id'])

    def stop_all(self):
        # ★ 退出前等线程收尾并关掉 sqlite —— 不然记账的库会留下 -wal 文件，
        #   下次打开要重新做恢复。
        #   ★ runners 要**先存下来**：stop_bot 会把它们从字典里摘掉，
        #     之后按 bid 查就查不到了，也就等不到、关不掉
        with self.lock:
            pairs = list(self.runners.items())
        for bid, _ in pairs:
            self.stop_bot(bid)
        for _, r in pairs:
            self.wait_runner(r, timeout=8)

    # -------- 给前端的数据 --------
    def snapshot(self, mid=None):
        """给前端的机器人列表。

        mid=None → 全部（管理员）；传字符串 → 只看这个商户名下的
        （面板靠这个做租户隔离，别绕过它直接遍历 self.bots）
        """
        only = None if mid is None else (mid or '').strip()
        now = time.time()
        with self.lock:
            out = []
            for b in self.bots:
                if only is not None and (b.get('mid') or '') != only:
                    continue
                self.find(b['id'])
                r = self.runners.get(b['id'])
                if b.get('instance_folder') and r and hasattr(r, 'info'):
                    info = r.info()
                    if info:
                        b['status'], b['error'] = info.get('status') or 'starting', info.get('error') or ''
                item = {
                    'id': b['id'],
                    'mid': b.get('mid') or '',      # 归属哪个商户，前端要用
                    'type': b.get('type') or DEFAULT_TYPE,
                    'type_name': type_name(b.get('type') or DEFAULT_TYPE),
                    'note': b.get('note') or '',
                    'username': b.get('username') or '',
                    'instance_folder': b.get('instance_folder') or '',
                    'node_id': b.get('node_id') or '',
                    'node_folder': b.get('node_folder') or '',
                    'node_pending': bool(b.get('node_pending')),
                    'name': b.get('name') or '',
                    'bind_code': b.get('bind_code') or '',
                    'bound': bool(b.get('admin_ids')),
                    'admin_name': b.get('admin_name') or '',
                    'admin_id': (b.get('admin_ids') or [None])[0],
                    'admin_count': len(b.get('admin_ids') or []),
                    'owner_id': b.get('owner_id'),
                    'bound_at': b.get('bound_at') or '',
                    'enabled': bool(b.get('enabled', True)),
                    # ★★ 这个必须带上！前端靠它决定：
                    #    · 状态列显示「客户自建」（不是「已停止」）
                    #    · 操作列多一个「⬇️ 安装包」按钮
                    #    2026-09-29 漏了这行 → 面板上怎么都不显示，
                    #    因为 snapshot 是**白名单式**拼字段的，不加就传不过去。
                    'remote': bool(b.get('remote')),
                    'status': b.get('status') or 'stopped',
                    'error': b.get('error') or '',
                    'created': b.get('created') or '',
                    'duration': b.get('duration') or DEFAULT_DURATION,
                    'duration_label': duration_label(b.get('duration')),
                    'expire_at': int(b.get('expire_at') or 0),
                    'expired': b.get('status') == 'expired',
                    'remain_sec': max(0, int(b.get('expire_at') or 0) - int(now)),
                    'delete_at': self.delete_deadline(b),
                    'stat': 0,          # 各类型自定义的统计数字
                    'stat_label': '',
                    # 群消息记录：前端要拿它画开关。★ 不含任何机密
                    'archive': {
                        'enabled': bool((b.get('archive') or {}).get('enabled')),
                        # 按群停记的、按群备注的 —— 前端画群列表要用
                        'mute_chats': [str(x) for x in
                                       ((b.get('archive') or {})
                                        .get('mute_chats') or [])],
                        'chat_notes': dict(((b.get('archive') or {})
                                            .get('chat_notes') or {})),
                    },
                }
                if r is not None:
                    if b.get('instance_folder'):
                        item['stat'] = int(r.stat() or 0)
                        item['stat_label'] = {'ledger':'记账群','shop':'待处理','usdt':'监听地址','kefu':'客户'}.get(item['type'],'')
                    elif item['type'] == 'usdt':
                        item['stat'] = len(getattr(r, 'watches', []) or [])
                        item['stat_label'] = '监听地址'
                    elif item['type'] == 'shop':
                        st = getattr(r, 'store', None)
                        item['stat'] = len(st.need_attention()) if st else 0
                        item['stat_label'] = '待处理'
                    elif item['type'] == 'ledger':
                        # ★ 读 runner 的内存快照，**绝不能碰它的 sqlite** ——
                        #   sqlite 连接是那个线程私有的，面板线程去读会报错
                        item['stat'] = int(r.stat() if b.get('instance_folder') else getattr(r, '_group_count', 0) or 0)
                        item['stat_label'] = '记账群'
                    else:
                        item['stat'] = len(getattr(r, 'customers', {}) or {})
                        item['stat_label'] = '客户'
                # 身上还带着「分配时会清掉」的那些机密吗？
                # 只给个 True/False，内容绝不外传 —— 前端要拿它提醒你
                if item['type'] == 'shop':
                    sh = b.get('shop') or {}
                    item['has_secrets'] = any(
                        str(sh.get(k) or '').strip()
                        for k in SHOP_CLEAR_ON_ASSIGN)
                if b.get('node_id'):
                    registry = getattr(self,'nodes',None)
                    try:
                        item['server_name'] = registry.get(b['node_id'])['name'] if registry else '运行服务器'
                    except OSError:
                        item['server_name'] = '未连接服务器'
                    node_status = registry.cache.get(b['node_id'],{}) if registry else {}
                    if not node_status.get('online') or time.time()-node_status.get('updated_at',0)>45:
                        item.update(status='error',error='服务器连接中断，实际运行状态待确认')
                    item.update(stat=b.get('node_stat',0),stat_label=b.get('node_stat_label',''),
                                has_secrets=bool(b.get('node_has_secrets')))
                else:
                    registry = getattr(self,'nodes',None)
                    item['server_name'] = registry.state['local']['name'] if registry else '本机服务器'
                out.append(item)
            return out

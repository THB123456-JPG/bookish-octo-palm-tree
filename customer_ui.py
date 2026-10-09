"""Customer navigation; existing accounting and checkout handlers remain in charge."""
import os
from html import escape
from urllib.parse import urlsplit

CONFIG_BUTTON = '⚙️ 配置管理'
HELP_BUTTON = '📖 使用说明'

BOT_DESCRIPTIONS = {
    'ledger': ('这是一款面向 Telegram 群组的专业记账机器人，支持入款、减分、下发、'
               '汇率费率、个人统计与小程序账单查看。\n\n'
               '它可以帮助团队快速记录每一笔资金变动，自动生成清晰账单，减少人工统计错误，'
               '让群内财务管理更高效、更透明。\n\n'
               '适合：\n• 群组记账\n• 团队收支管理\n• 多操作员协作\n• 每日对账与账单留存\n\n'
               '激活后，点击底部配置管理，即可自行配置机器人。\n\n'
               '把繁琐的记账交给机器人，把时间留给真正重要的业务。'),
    'shop': ('这是一款 Telegram 自助商城机器人，支持能量充值、订单查询，'
             '以及按商家设置开放的笔数套餐和 Telegram 会员。\n\n'
             '客户按提示付款，机器人自动识别订单并发货，方便随时查询购买记录。\n\n'
             '适合：\n• 自助能量充值\n• 笔数套餐购买\n• Telegram 会员开通\n• 商家订单管理\n\n'
             '商家激活后，点击底部配置管理，即可自行设置商品与收款信息。\n\n'
             '把重复的订单处理交给机器人，把时间留给客户服务。'),
}


def configure_description(r):
    if supported(r):
        try:
            text = BOT_DESCRIPTIONS[r.bot['type']]
            for lang in ('', 'zh'):
                current = r.api.call('getMyDescription', language_code=lang)
                if current.get('description') != text:
                    r.api.call('setMyDescription', description=text, language_code=lang)
        except Exception:
            # Profile setup must not interrupt accounting or checkout.
            from core import log
            log('[%s] 机器人简介更新失败，可重启后重试' % r.bid)


def base_url(mgr):
    url = os.environ.get('CUSTOMER_MINIAPP_BASE_URL',
                         mgr.cfg.get('miniapp_base_url', '')).rstrip('/')
    p = urlsplit(url)
    return url if p.scheme == 'https' and p.hostname and not p.username and not p.query and not p.fragment else ''


def app_url(r):
    return base_url(r.mgr) + '/miniapp/' + r.bid


def supported(r):
    return r.bot.get('type') in ('ledger', 'shop') and bool(base_url(r.mgr))


def configure_menu(r, uid=None):
    if supported(r):
        params = {'menu_button': {'type': 'web_app', 'text': '配置中心',
                                 'web_app': {'url': app_url(r)}}}
        if uid:
            params['chat_id'] = uid
        try:
            r.api.call('setChatMenuButton', **params)
        except Exception:
            # A missing Mini App menu must not stop the bot's existing poller.
            pass


def keyboard(r, uid):
    if r.bot.get('type') == 'shop':
        kb = r.menu()
        if uid in r.admins():
            kb['keyboard'].append([{'text': CONFIG_BUTTON}, {'text': HELP_BUTTON}])
        return kb
    return {'keyboard': [[{'text': CONFIG_BUTTON}, {'text': HELP_BUTTON}],
                         [{'text': '📡 地址簿'}, {'text': '📊 我的账目'}]],
            'resize_keyboard': True, 'is_persistent': True}


def open_config(r, uid):
    configure_menu(r, uid)
    r.send(uid, '⚙️ 配置中心\n激活后可在 Telegram 内配置自己的机器人。',
           reply_markup={'inline_keyboard': [[{'text': '打开配置管理',
                          'web_app': {'url': app_url(r)}}]]})


def help_sections(r, uid):
    if r.bot.get('type') == 'ledger':
        return LEDGER_HELP
    sections = [('shop', '商城说明', help_text('商城说明', [
        ('能量充值', '钱包转 TRX', '从自己的钱包转至商城收款地址，能量发往付款地址'),
        ('充值记录', '📊 我的记录', '点击底部按钮查看自己的充值记录'),
        ('转账费用', '💲 查手续费', '点击底部按钮查询转账需要的能量'),
        ('实时币价', '📈 实时U价', '点击底部按钮查询 USDT 行情'),
        ('联系商家', '☎️ 联系客服', '点击底部按钮联系商家'),
    ]) + '\n\n⚠️ 请使用自己的钱包付款；从交易所转账，能量会发到交易所地址。')]
    if r.store.cfg.get('auto_enabled'):
        sections.append(('auto', '笔数套餐', help_text('笔数套餐', [
            ('购买套餐', '🔥 笔数套餐', '选择档位、填写地址，按订单金额转 TRX'),
            ('查询余量', '🔢 剩余笔数', '发送购买地址，查看剩余笔数'),
        ])))
    if r.store.cfg.get('premium_enabled'):
        sections.append(('premium', '会员开通', help_text('会员开通', [
            ('购买会员', '🎁 开通会员', '选择时长，填写用户名和付款地址，按订单指定币种与精确金额付款'),
            ('会员订单', '/会员', '查看自己下过的会员订单'),
        ])))
    if uid in r.admins():
        sections.append(('shop_admin', '运营管理', help_text('运营管理', [
            ('收款配置', '/收款', '查看收款信息与扫描状态'),
            ('查看流水', '/流水', '查看最近十笔流水'),
            ('今日统计', '/统计', '查看今日汇总'),
            ('上游余额', '/余额', '查看发货上游的账户余额'),
        ]) + '\n\n仅管理员可执行运营命令；商家配置请点击最下方按钮。'))
    return sections


def help_text(title, rows):
    heading = title if title.endswith('说明') else title + '说明'
    lines = ['<blockquote><b>📖 %s</b></blockquote>' % escape(heading)]
    for label, command, description in rows:
        commands = ' / '.join('<code>%s</code>' % escape(c) for c in command.split(' / '))
        lines.append('<b>%s</b> ▪️ %s（%s）' %
                     (escape(label), commands, escape(description)))
    return '\n'.join(lines)


LEDGER_HELP = [
    ('manage', '管理权限', help_text('管理权限', [
        ('配置管理', '打开配置管理', '私聊打开 Telegram 小程序，管理当前机器人'),
        ('激活绑定', '/admin 激活码', '新客户私聊激活，激活码由服务商提供'),
        ('基础配置', '面板 → 概览 → 基础配置', '有管理权限的人可设置新群默认值和各群共用的入群欢迎语'),
        ('内部人员', '面板 → 概览 → 内部人员', '仅拥有者授权；管理、全局操作、单群操作和广播可分别勾选'),
        ('群组配置', '面板 → 群组', '设置有权限管理的群：汇率、费率、日切、显示及记账开关'),
        ('群组授权', '添加操作人 / 删除操作人', '本群老板在群内回复人员消息，或指定 @用户名'),
        ('群发广播', '广播', '机器人管理人员私聊发送，选择群、输入内容并确认发送'),
        ('人员统计', '/统计', '仅拥有者私聊，选群查看每个人的记账笔数与合计，不列明细'),
        ('定时广告', '面板 → 概览 → 定时广告', '新增标题、内容、图片、目标群和启用开关；支持单次、每天北京时间和按分钟循环'),
        ('自动回复', '面板 → 概览 → 自动回复', '多个关键词用空格分隔，任一关键词按精确/包含/前缀方式匹配后回复；可设置目标群和启用开关'),
        ('规则管理', '编辑 / 删除 / 启用', '拥有管理或广播权限的人可配置；创建者被撤权后规则停止执行'),
    ]) + '\n\n拥有者拥有全部权限；全局操作适用于所有群，包括以后加入的新群。'
          '\n群列表仅包含授权群，单群操作人员可修改自己的个人参数。'
          '\n所有人均可浏览功能；修改范围按管理、操作和广播授权分别控制。'
          '\n只有拥有者可授权内部人员或绑定 API Key，群操作权限不包含这两项。'
          '\n群内单独授权的操作人按本群授权执行，群配置优先于新群默认值。'
          '\n陌生人拉入机器人不会自动授权，也不能记账或修改配置。'
          '\n\n广告约每分钟检查一次，到期或退出群不发送；重启不补发已领取的同一次任务。'
          '\n自动回复只在匹配的目标群触发，现有记账与查询指令优先。'),
    ('permissions', '操作权限', help_text('操作权限', [
        ('上课下课', '上课 / 下课', '开启或暂停当前群记账，不删除流水'),
        ('增加记员', '添加操作人 @aaa @bbb', '增加本群记账操作人，支持多个用户名'),
        ('删除记员', '删除操作人 @aaa @bbb', '删除本群记账操作人，支持多个用户名'),
        ('增加记员', '添加操作人', '回复指定人员消息，由本群老板添加'),
        ('删除记员', '删除操作人', '回复指定人员消息，由本群老板删除'),
        ('查看记员', '显示操作人', '查看本群已授权操作人'),
    ])),
    ('examples', '配置指南', help_text('配置指南', [
        ('激活绑定', '/admin 激活码', '新客户在机器人私聊中激活，激活码由服务商提供'),
        ('进入面板', '打开配置管理', '点击私聊说明最下方按钮，或输入框旁的配置中心'),
        ('群组配置', '面板 → 群组', '选择有管理权限的群，查看并修改群设置'),
        ('人员管理', '面板 → 概览 → 内部人员', '仅机器人拥有者可增删内部人员；群操作人在群组页管理'),
        ('保存设置', '保存配置', '修改后点击保存，提示配置已保存才表示生效'),
        ('功能开关', '面板 → 功能开关', '分别开启或关闭通知、账单显示、号码查询、USDT防篡改验证与欧易汇率功能'),
        ('记账授权', '面板 → 内部人员 / 群组 → 群操作人', '仅拥有者、内部授权操作人员和本群操作人可记账，单群授权仅在指定群生效'),
        ('进群欢迎', '面板 → 基础配置 → 进群欢迎', '分别设置欢迎开关、先@新人、昵称和欢迎内容；{name}替换姓名'),
        ('欢迎图片', '面板 → 基础配置 → 管理欢迎图片', '查看历史图片，选择新图并保存，或确认移除；支持6MB以内JPG、PNG及WebP，欢迎文案单独保留'),
        ('退群提醒', '面板 → 基础配置 → 退群提醒', '可开关或编辑退群文案；默认{name} 已离开该群。{name}替换离开成员姓名'),
        ('通知设置', '面板 → 功能开关 → 通知类', '分别控制入群通知、用户修改信息通知、群名更改通知；群押金预警需填写群押金'),
        ('全局地址', '面板 → 基础配置 → 下发地址', '汇总群内和面板地址；新增、编辑或删除后统一同步所有群；删除全部则所有群保持空列表'),
    ]) + '\n\n所有人均可浏览功能；无修改权限时显示只读预览。实际群组和账单仅限授权账号查看。'),
    ('display', '账单显示', help_text('账单显示', [
        ('今日账单', '账单 / 今日账单', '查看当前账期的全部流水'),
        ('昨日账单', '昨日账单', '查看上一账期的全部流水'),
        ('完整账单', '完整账单 / +0', '查看当前账期的完整账单'),
        ('显示模式', '账单底部：简洁 / 详细', '点击已有账单按钮切换当前群显示模式'),
        ('个人明细', '/我', '只查看自己记录的账目；私聊可选择多个群统计'),
        ('人员统计', '/统计', '仅拥有者私聊；选群查看每人的记账笔数与合计，不列明细'),
        ('账单切换', '面板 → 功能开关 → 详细／简洁账单切换', '控制账单底部切换按钮；关闭后不显示切换按钮'),
        ('期初结余', '面板 → 功能开关 → 上期结余未下发', '上期未下发参与期初与当前未下发展示，不生成重复流水'),
        ('分类明细', '面板 → 功能开关 → 分类明细', '按备注汇总入款与支出'),
        ('分类结算', '面板 → 功能开关 → 完整账单备注分类结算', '按备注展示入款笔数、应下发、已下发与未下发U'),
        ('账单统计', '小程序 → 账单 / 统计', '按群与日切周期看账单；本月统计清空计入、撤销不计，跨月清理旧明细'),
    ])),
    ('params', '账单参数', help_text('账单参数', [
        ('日切时间', '设置日切时间3', '按北京时间设置 0 至 23 点，0 表示每天零点；也可恢复日切'),
        ('关闭日切', '关闭日切', '从当前账期继续累计，不重新计入历史账期'),
        ('显示日切', '显示日切', '在群内查看北京时间的日切时间、当前账期、下次日切与汇率费率；关闭日切时显示累计账期'),
        ('群组配置', '打开配置管理 → 群组', '查看与配置自己有权限的群组'),
        ('新群默认', '面板 → 基础配置', '日切时间、账单显示和币种只用于以后加入的新群，现有群不重设'),
        ('群组押金', '面板 → 群组 → 群押金', '填U金额并开启功能开关中的押金提醒；未下发到80%提醒，达到押金预警'),
    ])),
    ('entries', '群内设置', help_text('群内设置', [
        ('修改原账', '回复原记账消息发送 +金额 / 分红金额', '替换原流水金额，不新增重复流水；开启二次确认时由发起人确认'),
        ('修改确认', '面板 → 功能开关 → 修改账单二次确认', '开启时先确认，关闭后直接修改；授权和原流水仍会校验'),
        ('模式设置', '面板 → 功能开关 → 纯U／分红模式', '仅没有历史账目的机器人可切换；出现账目后不能再改变模式'),
        ('纯U分红', '分红100 / 分红-100', '纯U模式下记录U支出或冲回；入款 +100 直接按U，旧下发命令不响应'),
        ('记录入款', '+1000', '入款 1000 CNY'),
        ('汇率入款', '+1000/7.3', '按单笔汇率入款，不改默认汇率'),
        ('费率入款', '+1000*0.12', '本笔费率 12%，1000 扣费后为 880，再按汇率换算'),
        ('费率规则', '+1000*3', '星号后绝对值小于 1 按比例换算；0.12 为 12%，3 为 3%'),
        ('返佣入款', '+1000*-5', '本笔费率 -5%，加计 5% 后再按汇率换算'),
        ('U币入款', '+1000u', '入款 1000 USDT，不扣入款费率'),
        ('U币换算', '+1000u/7.3', '入款 1000 USDT，折合 7300；不扣入款费率'),
        ('识别规则', '+1000 / -1000', '只有消息开头为加号或减号、后面跟数字金额才记录入款；13-17的、张三+1000等聊天不记账'),
        ('回复入款', '+1000', '回复指定人员消息，使用被回复人的个人参数'),
        ('备注入款', '+1000 备注', '带备注入款'),
        ('入款修正', '-1000', '反向冲减入款，不记为出款'),
        ('记账下发', '下发100 / 下发100u / /下发1000', '记账下发，金额直接按 U 处理'),
        ('指定下发', '张三下发1000', '按记录用户名下发'),
        ('回复下发', '下发1000', '回复指定人员消息'),
        ('备注下发', '下发1000 备注', '带备注下发'),
        ('下发修正', '下发-1000', '反向冲回下发'),
    ])),
    ('rates', '汇率费率', help_text('汇率费率', [
        ('固定汇率', '设置汇率7.3', '未指定人员时设置本群默认汇率'),
        ('入款费率', '设置费率3', '未指定人员时设置本群默认费率；3 表示 3%，0.12 表示 0.12%'),
        ('个人汇率', '设置汇率@aaaa7.3', '也可用空格：设置汇率 @aaaa 7.3；回复人员消息发送设置汇率7.3'),
        ('个人费率', '设置费率@aaaa3', '也可用空格：设置费率 @aaaa 3；回复人员消息发送设置费率3'),
        ('恢复默认', '设置汇率 @aaaa 默认 / 设置费率 @aaaa 默认', '恢复该用户这一项本群默认，也可回复人员消息设置默认'),
        ('查看费率', '查看费率', '查看本群默认；加 @用户名或回复人员消息查看个人有效参数'),
        ('实时汇率', '设置实时汇率', '使用欧意最新报价更新当前群汇率'),
        ('查询币价', '币价 / bj / z0', '查询 USDT/CNY 最新报价；BJ、Z0 同样可用'),
        ('汇率开关', '面板 → 功能开关 → 查询欧易汇率', '控制欧易USDT报价查询'),
        ('单笔汇率', '+9000/9 备注', '仅此笔入款使用汇率 9，不改群默认汇率'),
    ]) + '\n\n个人参数仅作用于后续入款，回复记账按被回复人，直接记账按发送人。'
          '\n汇率优先：单笔指定 → 个人设置 → 群默认。费率支持 0 至小于 100。'),
    ('query', '查询撤销', help_text('查询撤销', [
        ('查询账目', '账单', '查询当前群当前账期账单'),
        ('撤销流水', '撤销', '回复要撤销的记账消息或含流水编号的回执'),
        ('清空账单', '清空', '清空当前群流水并重新计数，需管理权限'),
        ('查询标识', '/id', '私聊查看自己的 ID；群内同时显示群 ID'),
        ('清账存档', '面板 → 功能开关 → 删除账单完整账单存档', '启用后清空前保留所有留存账期，点击机器人发送的存档按钮查看'),
    ]) + '\n\n⚠️ 撤销、清空会改变账目，确认目标后再在群内执行。'),
    ('tools', '辅助功能', help_text('辅助功能', [
        ('通知全员', '通知所有人 内容', '在群内按现有权限规则通知成员'),
        ('消息清理', '/del', '群内清理消息；主人私聊可选择群进行清理'),
        ('算式计算', '1000/9', '直接发送算式得到结果；带正负号的纯记账格式优先记账'),
        ('贵金属价', 'G', '查询贵金属实时价格'),
        ('号码归属', '手机号 / 身份证号 / 银行卡号', '整条消息发送号码，查询归属地信息'),
        ('查U监听', '私聊发送 TRC20 地址', '查询余额、最近转账，点击加入监听设置通知'),
        ('小程查询', '配置中心 → 地址查询与密钥', '在小程序查询 TRX/USDT 余额与最近交易；拥有者可按申请指南绑定自己的 API Key，未绑定时使用平台默认密钥轮换'),
        ('地址核对', '群内发送 TRC20 地址', '生成防篡改核对图片'),
        ('地址管理', '📡 地址簿', '私聊底部按钮，管理监听币种、方向与金额'),
        ('设置欢迎', '设置欢迎语', '主人私聊发送，按提示输入文字或图片'),
        ('成员名称', '{name}', '在欢迎语中替换为新成员名字'),
        ('恢复默认', '默认', '进入设置欢迎语流程后发送，恢复默认内容'),
        ('关闭欢迎', '关闭', '进入设置欢迎语流程后发送，关闭入群欢迎'),
        ('新增地址', '设置下发地址', '群内有管理权限的人发送，按提示输入完整TRON地址；设置成功后本群生效'),
        ('查看地址', '下发地址', '任何群成员可查看本群地址及转入核对提醒；功能开关关闭群内地址展示后不显示'),
        ('删除地址', '删除下发地址', '列出地址后输入编号，再输入确认删除；仅发起人可完成'),
        ('取消操作', '取消', '结束当前地址设置或删除流程；五分钟未完成会过期，每群最多20个地址'),
        ('号码开关', '面板 → 功能开关 → 外部数字查询', '总开关控制号码查询，也可分别开关手机号、身份证和银行卡查询'),
        ('地址核验', '面板 → 功能开关 → USDT 防篡改验证', '控制群内核对图片，可配置USDT/TRX余额、地址交易详情和链上交易次数；获取失败会提示'),
        ('管理确认', '面板 → 功能开关 → 允许群管理员确认', '群管理员可点击确认核对地址，不代表已收款；机器人不执行转账'),
    ]) + '\n\n欢迎语也可在私聊最下方配置管理的基础配置中修改。'),
]

LEDGER_HELP_ICONS = {
    'manage': '⚙️', 'permissions': '👥', 'examples': '🧭',
    'display': '📊', 'params': '🧾', 'entries': '🏠',
    'rates': '💱', 'query': '🔎', 'tools': '🧰',
}


def help_view(r, uid, key='home'):
    if r.bot.get('type') == 'ledger':
        key = {'welcome': 'tools', 'addresses': 'tools', 'config': 'examples', 'rules': 'manage'}.get(key, key)
    sections = help_sections(r, uid)
    if key != 'home':
        section = next((s for s in sections if s[0] == key), None)
        if not section:
            return None
        text = section[2]
    else:
        text = ('<blockquote><b>📖 群组命令</b></blockquote>\n'
                '请选择命令模块，进入后可复制命令发到群里。\n'
                '后台配置：请私聊机器人打开「使用说明」。\n'
                '群里可发送：帮助 或 群组命令。') if uid < 0 else (
                '<blockquote><b>📖 使用说明</b></blockquote>\n'
                '请选择命令模块，点击命令可复制后发送到群里。\n'
                '后台配置：点击底部「打开配置管理」。')
    buttons = [{'text': '%s %s' % (LEDGER_HELP_ICONS.get(name, '🏷'), title),
                'callback_data': 'customer:help:' + name}
               for name, title, _ in sections]
    rows = [[{'text': '返回目录', 'callback_data': 'customer:help:home'}]] if key != 'home' else []
    rows += [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    rows.append([{'text': '返回目录', 'callback_data': 'customer:help:home'}] if uid < 0 else
                [{'text': '打开配置管理', 'web_app': {'url': app_url(r)}}])
    return text, {'inline_keyboard': rows}


def help_menu(r, uid):
    text, kb = help_view(r, uid)
    r.send(uid, text, parse_mode='HTML', reply_markup=kb)


def handle(r, update):
    if not supported(r):
        return False
    cq = update.get('callback_query')
    if cq and (str(cq.get('data', '')).startswith('customer:help:') or
               (cq.get('data') == 'ledger:help' and r.bot.get('type') == 'ledger')):
        uid = (cq.get('from') or {}).get('id')
        chat = (cq.get('message') or {}).get('chat') or {}
        group = (r.bot.get('type') == 'ledger' and chat.get('type') in ('group', 'supergroup')
                 and int(chat.get('id') or 0) < 0)
        if not group and (chat.get('type') != 'private' or chat.get('id') != uid):
            return False
        key = 'home' if cq['data'] == 'ledger:help' else cq['data'].rsplit(':', 1)[1]
        # Keep the six indexed buttons in older messages usable.
        if key.isdigit():
            legacy = ['entries', 'params', 'permissions', 'tools', 'welcome', 'tools']
            i = int(key)
            if r.bot['type'] == 'ledger':
                key = legacy[i] if i < len(legacy) else ''
            else:
                key = 'shop' if i == 0 else ''
        view = help_view(r, chat['id'], key)
        if not view:
            r.api.call('answerCallbackQuery', callback_query_id=cq['id'], text='该说明已更新，请重新打开使用说明。')
            return True
        from core import TgError
        try:
            r.api.call('editMessageText', chat_id=chat['id'], message_id=cq['message']['message_id'],
                       text=view[0], parse_mode='HTML', reply_markup=view[1])
        except TgError as exc:
            if 'message is not modified' not in str(exc).lower():
                r.api.call('answerCallbackQuery', callback_query_id=cq['id'],
                           text='暂时无法更新，请重新打开使用说明。', show_alert=True)
                return True
        r.api.call('answerCallbackQuery', callback_query_id=cq['id'])
        return True
    msg = update.get('message') or {}
    chat = msg.get('chat') or {}
    uid = (msg.get('from') or {}).get('id')
    text = (msg.get('text') or '').strip()
    if (msg.get('from') or {}).get('is_bot') or not uid or r.expired():
        return False
    if chat.get('type') in ('group', 'supergroup'):
        if r.bot.get('type') == 'ledger' and text in ('帮助', '群组命令', '使用说明', '菜单', '/help', '/使用说明'):
            help_menu(r, chat['id'])
            return True
        return False
    if chat.get('type') != 'private':
        return False
    if text in (CONFIG_BUTTON, '配置管理'):
        open_config(r, uid)
        return True
    if text in (HELP_BUTTON, '使用说明'):
        help_menu(r, uid)
        return True
    if r.bot.get('type') == 'ledger' and text in ('帮助', '菜单', '群组命令', '/使用说明', 'help'):
        help_menu(r, uid)
        return True
    # Ledger has no previous customer keyboard. Leave shop /start and /help
    # with its own session and checkout code, augmenting its menu separately.
    if r.bot.get('type') == 'ledger' and text in ('/start', '/help'):
        configure_menu(r, uid)
        help_menu(r, uid)
        return True
    if r.bot.get('type') == 'ledger' and text == '📊 我的账目':
        msg = dict(msg, text='/我')
        r.on_owner(msg) if uid in r.admins() else r.on_guest(msg)
        return True
    if r.bot.get('type') == 'ledger' and text == '📡 地址簿':
        content, kb = r.tronw.book_view(uid)
        r.send_html(uid, content, kb=kb)
        return True
    return False

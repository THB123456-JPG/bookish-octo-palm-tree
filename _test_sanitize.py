# -*- coding: utf-8 -*-
"""锁住「脱敏脚本不能误删公开常量」

★★ 为什么要有这个文件（2026-09-28 真事故）：
   用户的 USDT 助手查一个余额 12846 USDT 的地址，永远返回 0。

   根因不是查询代码 —— 是**脱敏脚本把公开常量当成了密钥**：
   USDT 官方合约地址 `TR7NHqje...` 出现在 `运行日志.txt` 里（扫链时打印过），
   于是被收进「待清理的真值清单」，然后在**所有文件**里替换 ——
   包括 `usdt.py` 里的 `USDT = 'TR7NHqje...'` 这个**公开协议常量**。
   替换成 `你的TRON地址` 之后，比对合约永远不匹配 → 余额永远 0。

   脱敏脚本分不清「密钥」和「公开常量」，所以必须有个白名单，
   而且必须有测试盯着它。改 _push_github.py 的时候别把这个白名单弄丢了。
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


usdt_contract = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'

print('一、白名单本身')
try:
    import _push_github as P
    check('能导入脱敏脚本', True)
except Exception as e:
    check('能导入脱敏脚本', False, '%s: %s' % (type(e).__name__, e))
    P = None

if P:
    check('★ 有 PUBLIC_CONSTANTS 白名单', hasattr(P, 'PUBLIC_CONSTANTS'),
          str(getattr(P, 'PUBLIC_CONSTANTS', '缺失')))
    check('★★ USDT 官方合约在白名单里',
          usdt_contract in getattr(P, 'PUBLIC_CONSTANTS', set()),
          str(getattr(P, 'PUBLIC_CONSTANTS', '')))

    print()
    print('二、★ 真值清单里不能有它')
    patterned, literal = P.collect_real_values()
    check('★★ USDT 合约没被当成「密钥」收进清单（这是那次事故的根因）',
          usdt_contract not in patterned,
          'patterned 里有它' if usdt_contract in patterned else '')
    check('★★ 也没被当成「私密字段值」',
          usdt_contract not in literal)
    # ★★ 这条只有在**本地（有真密钥的）工程**里才成立。
    #    GitHub 上那份是脱敏副本：config.json 里的密码/密钥全是空串、
    #    运行日志也清过了 → 抽不出任何真值 → 断言「清单非空」必然挂。
    #    那不是 bug，是**在错的机器上跑这条**。
    #    （跟 _test_deliver_guard.py 里「没真 Key 就跳过」一个道理：
    #      公开仓库里挂一条红会让人以为有 bug。）
    #    ★ 判据用「配置里有没有真值」，不能用「清单空不空」——
    #      后者正是这条要防的回归，拿它当判据等于把检查关掉了。
    _cfg_p = os.path.join(HERE, 'config.json')
    try:
        import json as _json
        with open(_cfg_p, encoding='utf-8') as _f:
            _cfg = _json.load(_f)
    except Exception:
        _cfg = {}
    _priv = ('password', 'token_secret', 'totp_secret', 'password_hash')
    if not any((_cfg.get(k) or '') for k in _priv):
        print('  ⏭ 跳过：这是脱敏后的副本（配置里没有真密钥），'
              '清单为空是对的。在本地工程里跑才有意义。')
    else:
        check('★ 清单本身还是有东西的（白名单没把功能整个关掉）',
              len(patterned) + len(literal) > 0,
              '%d 个密钥 + %d 个私密字段值' % (len(patterned), len(literal)))

print()
print('三、★★ 源码里的公开常量必须原样在')
# ★ 合约地址跟着链上工具从 usdt.py 拆到 tron.py 了（2026-09-29 重构）
src = open(os.path.join(HERE, 'tron.py'), encoding='utf-8').read()
check('★★ tron.py 里 USDT 常量还是那个官方合约地址'
      '（没被脱敏换成占位符）',
      "USDT = '%s'" % usdt_contract in src,
      [l.strip() for l in src.splitlines()
       if l.strip().startswith('USDT =')][:1])
# 顺带锁一条：拆出来的新文件里合约地址也必须只出现这一次，
# 多一份副本 = 以后改地址会漏掉一处，余额就是 0
check('★ 合约地址没有在别处又写一份（全项目只此一处定义）',
      sum(1 for f in ('tron.py', 'runners/usdt/runner.py',
                      'runners/shop/runner.py', 'runners/shop/pay.py',
                      'runners/shop/providers.py')
          if usdt_contract in open(os.path.join(HERE, f),
                                   encoding='utf-8').read()) == 1)

print()
print('四、★★★ totp_secret 必须被清掉（2026-09-29 真泄露过）')
# 事故：GitHub 上那份 config.json 里 password / token_secret 都清空了，
#      唯独 totp_secret 留着 32 位真值 —— **管理员的两步验证等于没有**。
#      根因就是 SENSITIVE_FIELDS / scrub_json 的字段清单不全，
#      而验收基准也不覆盖它（验收没覆盖到的东西 = 等于没验）。
if P:
    import io
    import json
    import shutil
    import tempfile

    check('★★★ 字段清单里有 totp_secret',
          'totp_secret' in P.SENSITIVE_FIELDS.get('config.json', ()),
          str(P.SENSITIVE_FIELDS.get('config.json')))

    tmp = tempfile.mkdtemp(prefix='_sanitize_')
    try:
        # 造一份「像真的」config.json
        io.open(os.path.join(tmp, 'config.json'), 'w', encoding='utf-8').write(
            json.dumps({'host': '127.0.0.1', 'port': 8080, 'welcome': 'hi',
                        'password': '真密码不许外传',
                        'token_secret': '真签名密钥不许外传',
                        'totp_secret': 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'},
                       ensure_ascii=False))
        done = P.scrub_json(tmp, '.', 'config.json', set())
        left = json.load(io.open(os.path.join(tmp, 'config.json'),
                                 encoding='utf-8'))
        check('★★★ 跑一遍 scrub_json → totp_secret 被清成空串',
              left.get('totp_secret') == '', repr(left.get('totp_secret'))[:20])
        check('★ 密码和签名密钥也清了（别只修一个）',
              left.get('password') == '' and left.get('token_secret') == '')
        check('★ host/port/welcome 保留（不是无脑清空整个文件）',
              left.get('host') == '127.0.0.1' and left.get('port') == 8080)
        check('★ 返回值里报出了它清过哪些字段',
              'totp_secret' in done, str(done))

        print()
        print('五、★★ 兜底断言：config.json 里不许有「没点名」的非空值')
        # ★ 这条是**反过来写**的：不列敏感字段，而是「除白名单外一律必须为空」。
        #   这样以后 config.json 加了新字段，默认就是「必须为空」——
        #   再也不会出现「漏了一个字段，验收还报全过」。
        bad_dir = tempfile.mkdtemp(prefix='_sanitize_bad_')
        io.open(os.path.join(bad_dir, 'config.json'), 'w',
                encoding='utf-8').write(json.dumps(
                    {'host': '0.0.0.0', 'port': 8080, 'welcome': '',
                     '某个新加的密钥': '真值还在'}))
        ok_bad, _ = P.verify(bad_dir, {'dummy-secret-value'}, set())
        check('★★ 兜底断言能拦住「清单里没点名」的非空字段',
              ok_bad is False, '居然放行了')
        shutil.rmtree(bad_dir, ignore_errors=True)

        good_dir = tempfile.mkdtemp(prefix='_sanitize_ok_')
        io.open(os.path.join(good_dir, 'config.json'), 'w',
                encoding='utf-8').write(json.dumps(
                    {'host': '0.0.0.0', 'port': 8080, 'welcome': '',
                     'password': '', 'token_secret': '', 'totp_secret': ''}))
        ok_good, _ = P.verify(good_dir, {'dummy-secret-value'}, set())
        check('★ 全清干净的正常副本要放行（别把功能整个卡死）', ok_good is True)
        shutil.rmtree(good_dir, ignore_errors=True)

        # ★ 白名单要**明确列出来**（越少越严）。加新项必须来这里改 ——
        #   逼着人想一下「这个东西写进公开仓库真的没关系吗」。
        check('★★ 白名单就是这几项（少而明确，加东西必须来这里改）',
              set(P.CONFIG_MAY_BE_NONEMPTY) == {'host', 'port', 'welcome',
                                                'merchant_host'},
              str(sorted(P.CONFIG_MAY_BE_NONEMPTY)))
        check('★ 白名单里没有像密钥的东西',
              not [k for k in P.CONFIG_MAY_BE_NONEMPTY
                   if any(w in k.lower() for w in
                          ('pass', 'secret', 'token', 'key', 'salt'))],
              str(sorted(P.CONFIG_MAY_BE_NONEMPTY)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

print()
print('=' * 58)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 58)
sys.exit(1 if BAD else 0)

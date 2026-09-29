/* 前端逻辑测试台
 *
 * 为什么要有这个：
 *   前几个 bug（标签点不开、图片不更新、未读不清零）全在前端，
 *   而我只能"读代码猜" —— 测不到就只能靠你试出来。
 *   这个台子把面板的 <script> 抠出来，在 node 里用假的 DOM +
 *   假的 fetch 跑起来，**真的执行前端逻辑**，把「读完了未读没清零」
 *   这种事在提交前就抓住。
 *
 * 用法：node _ui_harness.js <panel_page.html>
 */
const fs = require('fs');
const path = require('path');

const file = process.argv[2];
if (!file) { console.error('用法: node _ui_harness.js <html文件>'); process.exit(1); }
const html = fs.readFileSync(file, 'utf8');
const i = html.indexOf('<script>'), j = html.lastIndexOf('</script>');
const PANEL_JS = html.slice(i + 8, j);

/* ---------------- 假 DOM ---------------- */
const OK = [], BAD = [];
function check(name, cond, extra) {
  (cond ? OK : BAD).push(name);
  console.log('  ' + (cond ? '✅' : '❌') + ' ' + name +
              (extra ? '  → ' + extra : ''));
}

const LS = {};
global.localStorage = {
  getItem: k => (k in LS ? LS[k] : null),
  setItem: (k, v) => { LS[k] = String(v); },
  removeItem: k => { delete LS[k]; },
};

function mkEl(id) {
  const attrs = {};          // ★ 属性要真的存起来 —— 测图片缓存时要用
  const el = {
    id, style: {}, innerHTML: '', textContent: '', value: '', checked: true,
    options: [], selectedIndex: 0, scrollTop: 0, scrollHeight: 0,
    clientHeight: 0, offsetWidth: 0, offsetHeight: 0, hidden: false, src: '',
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    querySelector() { return null; }, querySelectorAll() { return []; },
    appendChild() {}, parentNode: { tagName: 'DIV', replaceWith() {} },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return k in attrs ? attrs[k] : null; },
    removeAttribute(k) { delete attrs[k]; },
    addEventListener() {}, removeEventListener() {},
    closest() { return null; }, insertAdjacentHTML() {}, replaceWith() {},
    scrollTo() {}, focus() {}, remove() {}, getBoundingClientRect() {
      return { left: 0, top: 0, width: 0, height: 0 };
    },
    // ★ 复制兜底那条路要用（execCommand 之前得先 select 文本）
    select() { this._selected = true; },
    setSelectionRange() { this._selected = true; },
    firstElementChild: null, lastElementChild: null,
    _attrs: attrs,
  };
  return el;
}

/* ★ 假的 <img data-name=...> 节点：测「图片缓存」用。
   页面里 msgLoadImages() 是 querySelectorAll('#msgStream img[data-name]:not([src])')
   选出来的，所以这里也按同样的条件过滤 —— 测的才是真的那段逻辑。 */
const IMG_NODES = [];
function mkImg(name) {
  const el = mkEl('');
  el.tagName = 'IMG';
  el.setAttribute('data-name', name);
  // ★ 页面里调的是 `img.replaceWith(...)`（节点自己的方法，不是父节点的），
  //   所以要覆盖 el 上的那个才算数
  el.replaceWith = () => { el._removed = true; };
  el.parentNode = { tagName: 'DIV', replaceWith() { el._removed = true; } };
  return el;
}
/* ★ 假标签节点：测「商户端藏了哪些标签」用。
   applyRoleTabs() 是 querySelectorAll('#tabs .tab') 选出来的，
   这里按同样的选择器给节点，测的才是真的那段逻辑。 */
const TAB_NODES = ['msg', 'bots', 'add', 'merch', 'help', 'logs'].map(p => {
  const el = mkEl('tab-' + p);
  el.setAttribute('data-pane', p);
  return el;
});
/* 使用说明里带 data-for 的块：**直接从页面 HTML 里抠**，跟真页面一致。
   ★ 是 `[data-for]` 不是 `details[data-for]` —— 顶部那块「📖 使用说明」
     提示也标了（商户看不到「怎么拿到机器人」那节，这提示留着会误导）。 */
const HELP_NODES = (html.match(/data-for="[a-z]+"/g) || [])
  .map((m, i) => {
    const el = mkEl('help-' + i);
    el.setAttribute('data-for', m.replace(/data-for="|"/g, ''));
    return el;
  });

const ELS = {};
const BODY_CLASSES = new Set();
global.document = {
  getElementById: id => (ELS[id] = ELS[id] || mkEl(id)),
  querySelector: () => null,
  // 按选择器返回对应的假节点（只支持测试真的用到的几个）
  querySelectorAll: sel => {
    const s = String(sel);
    if (s.indexOf('img[data-name]') >= 0) {
      return IMG_NODES.filter(n => !n.src && !n._removed);
    }
    if (s.indexOf('#tabs .tab') >= 0) return TAB_NODES;
    // 使用说明那几块：按页面里真实的 data-for 建，测的才是真数据
    // ★ 匹配用的是 `[data-for]` —— 页面里是 `#pane-help [data-for]`，
    //   不是 `details[data-for]`（顶部那块提示也是 div，不是 details）
    if (s.indexOf('[data-for]') >= 0) return HELP_NODES;
    return [];
  },
  createElement: () => mkEl(''),
  createTextNode: t => ({ nodeValue: String(t), textContent: String(t) }),
  addEventListener() {}, removeEventListener() {},
  hidden: false, title: '',
  // ★ 复制兜底要往 body 里临时塞一个 textarea 再删掉，得真的记着
  execCommand: () => global.__EXEC_OK !== false,
  // 页面会往 body 上加/去 'chatting' 类（全屏聊天），假的也得支持
  body: {
    children: [],
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      return c;
    },
    classList: {
      toggle(c, on) {
        if (on === undefined) { BODY_CLASSES.has(c) ? BODY_CLASSES.delete(c) : BODY_CLASSES.add(c); }
        else if (on) { BODY_CLASSES.add(c); } else { BODY_CLASSES.delete(c); }
      },
      add(c) { BODY_CLASSES.add(c); },
      remove(c) { BODY_CLASSES.delete(c); },
      contains(c) { return BODY_CLASSES.has(c); },
    },
  },
};
global.window = {
  addEventListener() {}, open() {}, innerWidth: 1200, innerHeight: 800,
  scrollY: 0, scrollTo() {},
};
global.location = { search: '', pathname: '/', href: '/' };
global.URLSearchParams = URLSearchParams;
// ★ 别整个覆盖 URL（页面里 new URL(...) 要用），只换 createObjectURL。
//   ★★ 必须**无条件**换掉：Node 自己也有 URL.createObjectURL，但它要求
//   参数是真的 Blob —— 我们的假 fetch 给的是 {}，会直接抛 TypeError，
//   表现成「图片加载全走了失败分支」，排查半天。踩过。
let BLOB_SEQ = 0;
global.URL.createObjectURL = () => 'blob:fake-' + (++BLOB_SEQ);
global.alert = () => {};
global.confirm = () => true;
global.prompt = () => '7';
global.setInterval = () => 0;      // ★ 定时器不真跑，测试里手动调
global.clearInterval = () => {};
global.setTimeout = () => 0;
global.Blob = function () {};

/* ---------------- 假 fetch：按 URL 路由 ---------------- */
// 假的「服务器」：一份事件表，未读按跟真服务器一样的规则算
let EVENTS = [];
const CALLS = [];

function unreadFrom(seen) {
  const out = {};
  EVENTS.forEach(e => {
    // ★ 跟真服务器一样：机器人自己发的（账单/回执）不算未读
    if (e.is_bot) return;
    if (e.ts > (seen[e.chat_id] || '')) out[e.chat_id] = (out[e.chat_id] || 0) + 1;
  });
  return out;
}
function chatsOf() {
  const m = {};
  EVENTS.forEach(e => {
    if (!m[e.chat_id]) m[e.chat_id] = { chat_id: e.chat_id, title: e.title, count: 0, last_ts: '' };
    m[e.chat_id].count++;
    if (e.ts > m[e.chat_id].last_ts) m[e.chat_id].last_ts = e.ts;
  });
  return Object.values(m);
}

function route(url, opts) {
  const u = new URL(url, 'http://x');
  const p = u.pathname;
  const q = u.searchParams;
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  CALLS.push({ path: p, q: Object.fromEntries(q) });

  if (p === '/api/bots') {
    return { ok: true, me: { role: 'admin' }, bots: global.__BOTS || [] };
  }
  if (p === '/api/archive/unread_all') {
    const seen = body.seen || {};
    const unread = {}, base = {};
    (global.__BOTS || []).forEach(b => {
      if (!(b.archive || {}).enabled) return;
      const mine = seen[b.id];
      if (!mine || !Object.keys(mine).length) { base[b.id] = baseOf(); }
      else unread[b.id] = unreadFrom(mine);
    });
    return { ok: true, unread, base };
  }
  let m;
  if ((m = p.match(/^\/api\/archive\/([^/]+)\/chats$/))) {
    return { ok: true, chats: chatsOf(),
             stats: { count: EVENTS.length, media_bytes: 0 } };
  }
  if ((m = p.match(/^\/api\/archive\/([^/]+)\/unread$/))) {
    const seen = JSON.parse(q.get('seen') || '{}');
    return { ok: true, unread: unreadFrom(seen) };
  }
  // ★ 开启记录（机器人列表里那个按钮）—— 真的改状态，测才测得出来
  if ((m = p.match(/^\/api\/archive\/([^/]+)\/config$/))) {
    const b = (global.__BOTS || []).find(x => x.id === m[1]);
    if (b) b.archive = Object.assign({}, b.archive, body);
    return { ok: true, enabled: !!(b && b.archive && b.archive.enabled) };
  }
  // ★ 商城配置：测「笔数档位那个文本框到底渲染了没有」
  if ((m = p.match(/^\/api\/shop\/([^/]+)\/config$/))) {
    if (opts && opts.method === 'POST') {
      global.__SHOP_CFG = Object.assign({}, global.__SHOP_CFG, body);
    }
    return { ok: true, config: global.__SHOP_CFG || {}, providers: [] };
  }
  // ★ 谷歌验证码：测「绑定 / 看真实 IP / 清空」那套前端流程
  if (p === '/api/totp') {
    return { ok: true, secret: global.__TOTP_SECRET || 'ABCDEFGHIJKLMNOP',
             url: 'otpauth://totp/x?secret=' + (global.__TOTP_SECRET || ''),
             bound: !!global.__TOTP_BOUND };
  }
  if (p === '/api/totp/bind' || p === '/api/totp/reset') {
    // 假服务端：验证码必须是 123456 才算对（跟真服务端一样会拒）
    if (body.code !== '123456') {
      return { ok: false, error: '验证码不对，确认一下手机时间准不准' };
    }
    if (p.endsWith('/bind')) global.__TOTP_BOUND = true;
    else { global.__TOTP_BOUND = false; global.__TOTP_SECRET = 'NEWSECRET234567'; }
    return { ok: true, secret: global.__TOTP_SECRET };
  }
  if (p === '/api/logins') {
    return { ok: true, logins: global.__LOGINS || [], stats: { total: 1, fail: 0 },
             bound: !!global.__TOTP_BOUND, unlocked: !!global.__TOTP_UNLOCKED };
  }
  if (p === '/api/logins/reveal') {
    if (!global.__TOTP_BOUND) {
      return { ok: true, logins: global.__LOGINS || [], unlocked: true };
    }
    if (body.code !== '123456') {
      return { ok: false, error: '验证码不对', need_code: true };
    }
    global.__TOTP_UNLOCKED = true;
    return { ok: true, logins: global.__LOGINS || [], unlocked: true };
  }
  if (p === '/api/logins/clear') {
    if (global.__TOTP_BOUND && body.code !== '123456') {
      return { ok: false, error: '验证码不对', need_code: true };
    }
    global.__LOGINS = [];
    return { ok: true, cleared: 3 };
  }
  if ((m = p.match(/^\/api\/archive\/([^/]+)\/messages$/))) {
    const cid = q.get('chat');
    const rows = EVENTS.filter(e => !cid || String(e.chat_id) === String(cid))
                       .map(e => Object.assign({}, e));
    rows.sort((a, b) => (a.ts < b.ts ? 1 : -1));      // 新的在前（跟真接口一致）
    return { ok: true, messages: rows };
  }
  if ((m = p.match(/^\/api\/archive\/([^/]+)\/since$/))) {
    const after = q.get('after') || '';
    const rows = EVENTS.filter(e => e.ts >= after)
                       .map(e => Object.assign({}, e));
    rows.sort((a, b) => (a.ts < b.ts ? 1 : -1));
    return { ok: true, messages: rows, newest: newestTs() };
  }
  return { ok: false, error: '没有这个接口 ' + p };
}

function baseOf() { return {}; }
function newestTs() {
  let m = '';
  EVENTS.forEach(e => { if (e.ts > m) m = e.ts; });
  return m;
}

/* ★ 每个请求都记一笔 —— 不管调用方读的是 json() 还是 blob()。
   只靠 route() 里的 CALLS 记不全：图片走的是 .blob()，根本不碰 .json()，
   结果「发了几次请求」永远数成 0（我第一版就踩了这个）。 */
const FETCHES = [];
global.fetch = function (url, opts) {
  FETCHES.push(String(url));
  return Promise.resolve({
    status: 200,
    ok: true,
    json: () => Promise.resolve(route(url, opts)),
    blob: () => Promise.resolve({}),
  });
};

/* ---------------- 跑起来 ---------------- */
const vm = require('vm');
vm.runInThisContext(PANEL_JS, { filename: 'panel_page.html' });

/* ---------------- 场景 ---------------- */
const base = Date.now() - 3600 * 1000;
function ts(sec) { return new Date(base + sec * 1000).toISOString(); }

function mkEvent(chat_id, mid, sec, text) {
  return { chat_id: String(chat_id), message_id: String(mid), ts: ts(sec),
           title: '测试群', text, display_name: '张三', username: 'zs',
           kind: 'text', media: '', has_photo: false, is_owner: false,
           edited: false };
}

(async () => {
  console.log('=== 前端逻辑测试台 ===\n');

  // 一台记账机器人，已开记录
  global.__BOTS = [{ id: 'botA', note: '测试1', type: 'ledger',
                     type_name: '记账机器人', username: 'zzbot',
                     archive: { enabled: true, keep_days: 7 },
                     enabled: true, status: 'running', stat: 0 }];

  // 场景：群里已经有 3 条，用户看过（seen = 第3条的时间）
  EVENTS = [
    mkEvent(-100, 1, 10, '第一条'),
    mkEvent(-100, 2, 20, 'a发了个888'),
    mkEvent(-100, 3, 30, '第三条'),
  ];
  const seenAfterRead = ts(30);
  LS['panel_seen_botA'] = JSON.stringify({ '-100': seenAfterRead });

  // 让面板自己把机器人列表填进 LAST_ALL（msgLoadBots 用的是它）
  ME = { role: 'admin' };
  render({ bots: global.__BOTS, durations: [], grace_days: 7 });

  // ---- ① 读完之后，未读必须清零 ----
  console.log('一、读过的群，未读应该是 0');
  MSG.BOTS = global.__BOTS;
  MSG.BID = 'botA';
  msgOpenBot('botA');
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const html2 = ELS['msgChats'].innerHTML;
  check('  群列表渲染出来了（先确认不是报错页）',
        html2.indexOf('<table') >= 0, html2.slice(0, 90));
  check('★ 读过的群未读显示「—」（不是红数字）',
        html2.indexOf('<table') >= 0 && html2.indexOf('badge') < 0,
        html2.slice(0, 150).replace(/\n/g, ''));

  // ---- ② 又来 8 条 → 未读 8 ----
  console.log('\n二、来了 8 条新消息 → 未读 8');
  for (let k = 0; k < 8; k++) EVENTS.push(mkEvent(-100, 10 + k, 100 + k * 10, '新消息' + k));
  MSG.BOTS = global.__BOTS;
  msgOpenBot('botA');
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const h3 = ELS['msgChats'].innerHTML;
  check('★ 未读显示 8', h3.indexOf('>8<') >= 0, h3.slice(0, 200).replace(/\n/g, ''));

  // ---- ③ 点进群看消息 → 应该标记已读 ----
  console.log('\n三、点进群看完 → 已读时间应该被推进');
  msgOpenChat('-100');
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const saved = JSON.parse(LS['panel_seen_botA'] || '{}');
  check('★★ 看完之后 seen 被推进到最新一条',
        saved['-100'] === ts(100 + 7 * 10),
        'seen=' + saved['-100'] + '  最新=' + ts(100 + 7 * 10));

  // ---- ④ 返回群列表 → 未读必须变成 0 ----
  console.log('\n四、★ 返回群列表 → 未读必须清零（用户报的 bug）');
  msgGoto(2);
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const h4 = ELS['msgChats'].innerHTML;
  check('★★ 返回后未读不显示红数字了', h4.indexOf('badge') < 0,
        h4.slice(0, 200).replace(/\n/g, ''));

  // ---- ④之二 轮询的两面 ----
  // 用户 2026-09-29 报：手机上把表格滑到右边看后面的列，
  // 过几秒自己跳回最左边 —— 根因就是每 3 秒重画一次，innerHTML
  // 一重设浏览器就把 scrollLeft 归零。
  // 现在的规矩：**数据没变就不动 DOM**（滚动位置自然保住），
  //             数据变了才重画（该刷新的还得刷新）。
  console.log('\n四之二、轮询：数据没变不碰 DOM，变了才重画');
  MSG.LEVEL = 2; MSG.BID = 'botA';

  // ① 数据没变 → 必须**一个字节都不碰**（用「弄脏后脏数据还在」来证明）
  ELS['msgChats'].innerHTML = '脏数据 badge 8';
  msgPoll();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 数据没变时轮询不碰 DOM（手机上的横向滚动位置就保住了）',
        ELS['msgChats'].innerHTML === '脏数据 badge 8',
        ELS['msgChats'].innerHTML.slice(0, 90));

  // ② 数据变了 → 必须照常重画（脏数据被冲掉）
  EVENTS.push(mkEvent(-100, 9, 99, '新来了一条'));
  msgPoll();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 数据变了 → 轮询立刻重画（脏数据被冲掉）',
        ELS['msgChats'].innerHTML.indexOf('脏数据') < 0
        && ELS['msgChats'].innerHTML.indexOf('<table') >= 0,
        ELS['msgChats'].innerHTML.slice(0, 90));

  // ---- ⑤ 机器人列表那一级也要清零 ----
  console.log('\n五、★ 回到机器人列表 → 也未读清零');
  msgLoadBots();
  await new Promise(r => setImmediate(r));
  const h5 = ELS['msgBots'].innerHTML;
  check('★★ 机器人那一行没有红数字了', h5.indexOf('badge') < 0,
        h5.slice(0, 200).replace(/\n/g, ''));

  // ---- ⑥ 第一次打开（没有 seen）→ 历史记录不算未读 ----
  console.log('\n六、★ 第一次打开：历史记录不该顶着红点');
  delete LS['panel_seen_botA'];
  delete LS['panel_seen_botB'];
  global.__BOTS.push({ id: 'botB', note: '新机器人', type: 'ledger',
                       type_name: '记账机器人', username: 'nbot',
                       archive: { enabled: true, keep_days: 7 },
                       enabled: true, status: 'running', stat: 0 });
  MSG.BOTS = global.__BOTS;
  msgLoadBots();
  await new Promise(r => setImmediate(r));
  const h6 = ELS['msgBots'].innerHTML;
  check('★★ 第一次打开不显示未读', h6.indexOf('badge') < 0,
        h6.slice(0, 260).replace(/\n/g, ''));
  check('★ 而且把基线存下来了', !!LS['panel_seen_botB'] && !!LS['panel_seen_botA'],
        'botB=' + (LS['panel_seen_botB'] || '').slice(0, 40));

  // ---- ⑦ 「上次看到这里」的定位 ----
  console.log('\n七、★ 点进去停在「上次读到的地方」');
  const rowsAsc = [
    { chat_id: '-100', message_id: '1', ts: ts(10) },
    { chat_id: '-100', message_id: '2', ts: ts(20) },   // a发了个888
    { chat_id: '-100', message_id: '3', ts: ts(30) },
    { chat_id: '-100', message_id: '4', ts: ts(40) },
  ];
  check('★ 上次读到第2条 → 从第3条开始是新消息',
        firstUnreadKey(rowsAsc, ts(20)) === '-100|3',
        firstUnreadKey(rowsAsc, ts(20)));
  check('★ 上次读到最新 → 没有新消息（停在最底）',
        firstUnreadKey(rowsAsc, ts(40)) === '', firstUnreadKey(rowsAsc, ts(40)));
  check('★ 从没读过 → 从第一条开始',
        firstUnreadKey(rowsAsc, '') === '-100|1', firstUnreadKey(rowsAsc, ''));
  check('★ 上次读到中间 → 定位到紧跟着的那条',
        firstUnreadKey(rowsAsc, ts(30)) === '-100|4');
  check('空列表不炸', firstUnreadKey([], ts(10)) === '');
  check('传 null 不炸', firstUnreadKey(null, ts(10)) === '');
  // ★ 口径要跟服务器一致：机器人自己发的（账单）不算未读，分界线也要跳过它
  const withBot = [
    { chat_id: '-100', message_id: '1', ts: ts(10) },
    { chat_id: '-100', message_id: '2', ts: ts(20) },                 // 上次读到这
    { chat_id: '-100', message_id: '3', ts: ts(30), is_bot: true },   // 机器人账单
    { chat_id: '-100', message_id: '4', ts: ts(40) },                 // 人说的新消息
  ];
  check('★★ 分界线跳过机器人消息，落在人说的那条上',
        firstUnreadKey(withBot, ts(20)) === '-100|4',
        firstUnreadKey(withBot, ts(20)));
  const onlyBot = [
    { chat_id: '-100', message_id: '1', ts: ts(10) },
    { chat_id: '-100', message_id: '2', ts: ts(30), is_bot: true },
  ];
  check('★★ 之后只有机器人消息 → 不画分界线（跟未读 0 对得上）',
        firstUnreadKey(onlyBot, ts(10)) === '',
        firstUnreadKey(onlyBot, ts(10)));

  // 真渲染一遍，看分界线画在哪
  EVENTS = rowsAsc.map(r => Object.assign({ title: '测试群', text: 'x',
    display_name: '张三', kind: 'text', media: '', has_photo: false,
    is_owner: false, edited: false }, r));
  LS['panel_seen_botA'] = JSON.stringify({ '-100': ts(20) });
  MSG.BID = 'botA'; MSG.CHAT = '-100';
  msgLoadMsgs();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const h7 = ELS['msgStream'].innerHTML;
  check('★ 页面上画出了「上次看到这里」的分界线',
        h7.indexOf('上次看到这里') >= 0, h7.slice(0, 120));
  const posLine = h7.indexOf('上次看到这里');
  const pos3 = h7.indexOf('data-key="-100|3"');
  const pos2 = h7.indexOf('data-key="-100|2"');
  check('★★ 分界线正好在第2条和第3条之间',
        pos2 >= 0 && pos3 >= 0 && pos2 < posLine && posLine < pos3,
        '第2条 %d < 分界 %d < 第3条 %d' % (pos2, posLine, pos3));

  // ---- ⑧ 点整行就进；点行里的按钮不能误进 ----
  console.log('\n八、★ 点整行进 + 按钮不误触');
  ME = { role: 'admin' };
  global.__BOTS = [{ id: 'botA', note: '测试1', type: 'ledger',
                     type_name: '记账机器人', username: 'zzbot',
                     archive: { enabled: true, keep_days: 7 },
                     enabled: true, status: 'running', stat: 0 }];
  render({ bots: global.__BOTS, durations: [], grace_days: 7 });
  MSG.BOTS = global.__BOTS;
  msgLoadBots();
  await new Promise(r => setImmediate(r));
  const hb = ELS['msgBots'].innerHTML;
  check('★ 机器人那一行整行可点（进群列表）',
        hb.indexOf("onclick=\"msgOpenBot('botA')\"") >= 0, hb.slice(0, 150));
  check('★ 记录 / 状态 两列已删',
        hb.indexOf('<th>记录</th>') < 0 && hb.indexOf('<th>状态</th>') < 0);
  check('★ 「看消息」按钮已删', hb.indexOf('看消息') < 0);
  check('★★ 行里的按钮都 stopPropagation（不然点备注会误进）',
        hb.indexOf('stopEv(event);editNote') >= 0
        && hb.indexOf('stopEv(event);pinBot') >= 0);
  check('★ 机器人级的「关闭记录」已删（改成按群开关）',
        hb.indexOf('arcToggle') < 0 && hb.indexOf('关闭记录') < 0,
        hb.slice(0, 120));
  // ★★ 记录是**默认开**的（用户明确：机器人默认都是开的，关的粒度在按群），
  //    所以机器人这一级不该有「开启记录」按钮 —— 也别再加回来
  check('★★ 机器人级没有「开启记录」按钮（默认就是开的，不用点）',
        hb.indexOf('开启记录') < 0 && hb.indexOf('arcEnable') < 0
        && hb.indexOf('记录中') < 0,
        hb.slice(0, 140));

  // 群列表那一级
  MSG.BID = 'botA';
  msgOpenBot('botA');
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const hc = ELS['msgChats'].innerHTML;
  check('★ 群那一行整行可点，且带上群名称',
        hc.indexOf("onclick=\"msgOpenChat('-100', '测试群')\"") >= 0,
        hc.slice(0, 150));
  check('★ 群列表里的「看消息」按钮也删了', hc.indexOf('看消息') < 0);
  check('★ 群行的置顶按钮也 stopPropagation',
        hc.indexOf('stopEv(event);pinChat') >= 0);
  check('★★ 群行有按群「关闭记录」按钮',
        hc.indexOf('stopEv(event);muteChat(') >= 0 && hc.indexOf('关闭记录') >= 0);

  // 真进一次群，看面包屑显示的是群名还是 id
  msgOpenChat('-100', '测试群');
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 面包屑第 3 级显示**群名称**（不是 -100 那种 id）',
        ELS['msgChatName'].textContent === '测试群',
        '实际显示：' + ELS['msgChatName'].textContent);
  check('★ 没传群名时退回显示 id（不炸）', (function(){
    msgOpenChat('-999');
    return ELS['msgChatName'].textContent === '-999';
  })());

  // ---- ⑨ 手机端 & 滑动稳定 ----
  // ★ 注意：`in` 只能用于对象，用在字符串上会抛 TypeError —— 用 indexOf
  console.log('\n九、★ 手机端适配 + 滑动不晃');
  const has = (s, t) => s.indexOf(t) >= 0;
  check('★ 有手机端的媒体查询', has(html, '@media (max-width:720px)'));
  check('★ 标签栏在手机上能横拨（不换行挤成两排）',
        has(html, 'flex-wrap:nowrap')
        && html.indexOf('flex-wrap:nowrap')
           > html.indexOf('@media (max-width:720px)'));
  check('★ 表格套了横向滚动层（不然多列撑破手机屏）',
        has(html, 'class="tblwrap"') && has(html, '.tblwrap{overflow-x:auto'));
  check('★ 聊天区加了惯性滚动 + 滚到头不带走整页',
        has(html, '-webkit-overflow-scrolling:touch')
        && has(html, 'overscroll-behavior:contain'));
  check('★★ 新消息只在「已贴底」时才自动滚（不然每 3 秒把人拽回底部 = 晃）',
        has(PANEL_JS, 'if(msgNearBottom()) msgScrollBottom();'));
  check('★ 有 msgNearBottom 判断函数',
        has(PANEL_JS, 'function msgNearBottom()'));
  check('★ 群备注功能在',
        has(PANEL_JS, 'function noteChat') && has(PANEL_JS, '/chatnote'));
  check('★ 按钮图标去掉了（关闭记录/备注/置顶）',
        !has(html, '⏸') && !has(html, '✏️ 备注') && !has(html, '📌 置顶'),
        html.slice(html.indexOf('恢复记录') - 40,
                   html.indexOf('恢复记录') + 20).replace(/\n/g, ' '));
  check('★ 「全屏打开」按钮已删', !has(html, '全屏打开')
        && !has(PANEL_JS, 'arcOpenAll'));
  check('★★ 进群消息时整页切成全屏（底部不留空）',
        has(PANEL_JS, "classList.toggle('chatting', level === 3)")
        && has(html, 'body.chatting #msgStream')
        && has(html, 'body.chatting #tabs'));
  check('★ 手机端群名不竖排（表格按内容撑开、可横向滚）',
        has(html, '.tblwrap th,.tblwrap td{white-space:nowrap}'));
  check('★★ 进群消息时 body 真的加了 chatting 类（全屏）',
        BODY_CLASSES.has('chatting'),
        '实际：' + Array.from(BODY_CLASSES).join(','));
  msgGoto(2);      // 退回群列表
  check('★ 退回上一级会退出全屏', !BODY_CLASSES.has('chatting'));
  check('★ 那一行说明文字已删',
        !has(html, '添加机器人 → 生成绑定码'),
        html.slice(html.indexOf('<h1>'), html.indexOf('<h1>') + 90));
  check('★ 表头改成「群」了（没有「群 / 会话」）',
        has(html, '<th>群</th>') && !has(html, '<th>群 / 会话</th>'));

  // ---- ⑩ 机器人自己发的账单 / 回执 ----
  console.log('\n十、★ 机器人发的账单要在消息流里，且跟群友的话分开');
  const botRow = { chat_id: '-100', message_id: '9', ts: ts(50),
                   title: '测试群', text: '今日账单\n总入款金额：1100',
                   display_name: 'zzbot', username: 'zzbot', kind: 'text',
                   media: '', has_photo: false, is_owner: false,
                   is_bot: true, edited: false };
  check('★ 机器人消息渲染成「机器人」并带 bot 样式',
        msgBubble(botRow).indexOf('class="mb bot"') >= 0
        && msgBubble(botRow).indexOf('<b>机器人</b>') >= 0,
        msgBubble(botRow).slice(0, 130));
  check('★ 机器人消息不显示 @用户名（不是人）',
        msgBubble(botRow).indexOf('@zzbot') < 0);
  check('★ 机器人消息不靠右（不是「我」）', msgBubble(botRow).indexOf('mb right') < 0,
        msgBubble(botRow).slice(0, 40));
  check('★ 普通群友的消息没被带成 bot 样式',
        msgBubble(mkEvent(-100, 8, 45, '群友的话')).indexOf('mb bot') < 0);
  check('★ 绑定人自己的消息还是靠右我这一列',
        msgBubble(Object.assign({}, botRow, { is_bot: false, is_owner: true }))
          .indexOf('class="mb right"') >= 0);
  // 机器人消息太多也不该把未读撑起来（群里聊 2 句、机器人回 3 句 → 未读是 2 不是 5）
  const kept = EVENTS;
  EVENTS = [mkEvent(-100, 20, 60, '群友甲'), mkEvent(-100, 21, 61, '群友乙'),
            mkEvent(-100, 22, 62, '账单A'), mkEvent(-100, 23, 63, '账单B'),
            mkEvent(-100, 24, 64, '账单C')];
  EVENTS[2].is_bot = EVENTS[3].is_bot = EVENTS[4].is_bot = true;
  check('★★ 机器人发的消息不算未读（5 条里只有 2 条是人说的）',
        unreadFrom({})['-100'] === 2, '算出来 ' + unreadFrom({})['-100']);
  EVENTS = kept;

  // ---- ⑪ 机器人消息靠左 + 图片缓存 ----
  console.log('\n十一、★ 机器人消息靠左（跟群友同一条流）');
  check('★★ 机器人消息左对齐（不是居中、也不是右侧）',
        html.indexOf('.mb.bot{justify-content:center}') < 0
        && html.indexOf('.mb.bot{justify-content:flex-start}') >= 0,
        (html.match(/\.mb\.bot\{[^}]*\}/) || [''])[0]);
  check('★ 还是能看出是机器人（虚线框 + 「机器人」标签）',
        html.indexOf('.mb.bot .bb{background:#11161f;border-style:dashed') >= 0
        && msgBubble(botRow).indexOf('<b>机器人</b>') >= 0);

  console.log('\n十二、★★ 缓存好的图片，重开群消息不用重下');
  MSG.BID = 'botA';
  MSG.IMG = {}; MSG.IMG_WAIT = {};
  IMG_NODES.length = 0;
  IMG_NODES.push(mkImg('a.png'), mkImg('b.png'));
  function mediaCalls() {
    return FETCHES.filter(u => u.indexOf('/media/') >= 0).length;
  }
  const before11 = mediaCalls();
  msgLoadImages();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★ 第一次：两张图各拉一次',
        mediaCalls() - before11 === 2, '拉了 %d 次' % (mediaCalls() - before11));
  check('★ 两张图都显示出来了',
        IMG_NODES.every(n => !!n.src), IMG_NODES.map(n => n.src).join(','));

  // 模拟「每次都整体重画」：src 被清掉，再走一遍
  IMG_NODES.forEach(n => { n.src = ''; });
  const before12 = mediaCalls();
  msgLoadImages();
  await new Promise(r => setImmediate(r));
  check('★★ 重画后从缓存直接显示，一次网络都不发',
        mediaCalls() - before12 === 0, '又拉了 %d 次' % (mediaCalls() - before12));
  check('★★ 图还在（不是空白）', IMG_NODES.every(n => !!n.src));

  // 同名的图同时出现好几个（同一张图在两条消息里）—— 只能拉一次
  MSG.IMG = {}; MSG.IMG_WAIT = {};
  IMG_NODES.length = 0;
  IMG_NODES.push(mkImg('same.png'), mkImg('same.png'), mkImg('same.png'));
  const before13 = mediaCalls();
  msgLoadImages();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 同一张图并发出现 3 次，只拉 1 次（不重复下载）',
        mediaCalls() - before13 === 1, '拉了 %d 次' % (mediaCalls() - before13));
  check('★ 三个位置都显示出来了', IMG_NODES.every(n => !!n.src));

  // 拉不到的话不能把页面搞崩
  MSG.IMG = {}; MSG.IMG_WAIT = {};
  IMG_NODES.length = 0;
  const badImg = mkImg('bad.png');
  IMG_NODES.push(badImg);
  const realFetch = global.fetch;
  global.fetch = () => Promise.resolve({ status: 404, ok: false,
                                         json: () => Promise.resolve({}) });
  msgLoadImages();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const badDone = !!badImg._removed;
  global.fetch = realFetch;
  check('★ 图片取不到 → 显示占位文字，不把页面搞崩', badDone);
  check('★ 取不到的那张没被塞进缓存（下次还能重试）', !MSG.IMG['bad.png']);
  MSG.IMG = {}; MSG.IMG_WAIT = {}; IMG_NODES.length = 0;

  // ---- ⑬ ★★ 复制绑定码：服务器上是 HTTP，navigator.clipboard 不存在 ----
  // node 里本来就没有 navigator.clipboard，正好等于「服务器走 http://IP」那个环境
  console.log('\n十三、★★ 复制功能（本地 127.0.0.1 能用，服务器 HTTP 上不能用）');
  function toastText() { return ELS['toast'] ? ELS['toast'].textContent : ''; }
  function resetToast() { if (ELS['toast']) ELS['toast'].textContent = ''; }
  const hadClipboard = !!(global.navigator && global.navigator.clipboard);

  check('★ 测试环境确实没有 navigator.clipboard（跟服务器一样）', !hadClipboard,
        hadClipboard ? '有 —— 那就测不到这个 bug 了' : '');

  // ① 没有 clipboard API 时，必须走 execCommand 兜底，而且真的复制到
  global.__EXEC_OK = true;
  resetToast();
  copyCode('ABC12345');
  check('★★ 没有 clipboard API 时用 execCommand 兜底（不是静默失败）',
        toastText().indexOf('已复制') >= 0, 'toast：' + toastText().slice(0, 60));
  check('★★ 复制的内容是 /admin + 绑定码（**不是 token**）',
        toastText().indexOf('/admin ABC12345') >= 0
        && toastText().indexOf(':AA') < 0,
        toastText().slice(0, 70));
  check('★ 临时的 textarea 用完了（没留在页面上）',
        document.body.children.length === 0,
        'body 里还剩 ' + document.body.children.length + ' 个');

  // ② 连 execCommand 都失败 → 必须**明说失败**，绝不能装作成功
  global.__EXEC_OK = false;
  resetToast();
  copyCode('ABC12345');
  check('★★ 彻底复制不了时必须报失败（不能静默 —— 用户会以为复制到了）',
        toastText().indexOf('复制失败') >= 0, 'toast：' + toastText().slice(0, 60));
  check('★ 失败时把内容摆出来让用户手动选',
        toastText().indexOf('ABC12345') >= 0, toastText().slice(0, 70));
  check('★ 失败时也不能说「已复制」', toastText().indexOf('已复制') < 0);
  check('★ 失败路径也把临时 textarea 清掉了',
        document.body.children.length === 0);
  global.__EXEC_OK = true;

  // ③ 教程复制走同一条路（别又漏一个）
  resetToast();
  copyTut('这是一段教程');
  check('★ 复制教程也走同一个函数（不会再漏）',
        toastText().indexOf('已复制') >= 0, toastText().slice(0, 50));

  // ---- ⑭ 帮助文字不能瞎说 ----
  console.log('\n十四、★ 消息记录的帮助文字（写错了会误导用户）');
  const helpStart = html.indexOf('📜 消息记录（把群消息存');
  const helpTxt = html.slice(helpStart, helpStart + 1400);
  check('★ 帮助里说了「默认就是开的」（不是让人去找按钮）',
        helpTxt.indexOf('默认就是开的') >= 0,
        helpTxt.slice(0, 0) || '');
  check('★★ 帮助里没说「开启记录」这种已经不存在的按钮',
        helpTxt.indexOf('开启记录') < 0,
        (helpTxt.match(/开启记录[^<]{0,20}/) || [''])[0]);
  check('★★ 支持的类型没写错（只支持记账机器人，不是「记账和客服」）',
        helpTxt.indexOf('只支持记账机器人') >= 0
        && helpTxt.indexOf('和客服机器人') < 0,
        (helpTxt.match(/支持哪些机器人[^<]{0,60}/) || [''])[0]);
  check('★ 保留时长写的是 48 小时（不是「填保留天数」）',
        helpTxt.indexOf('48 小时') >= 0
        && helpTxt.indexOf('填保留天数') < 0);

  // ---- ⑮ ★★ 全屏聊天的两条滚动条 ----
  console.log('\n十五、★★ 全屏聊天不该有两条滚动条（用户报过）');
  check('★★ chatting 时页面本身不滚（不然右边多一条）',
        /body\.chatting\{[^}]*overflow:hidden/.test(html),
        (html.match(/body\.chatting\{[^}]*\}/) || [''])[0]);
  check('★★ 聊天区用 position:fixed 钉住（height:100vh 会被父元素挤出去）',
        /body\.chatting #msgLv3\{[^}]*position:fixed/.test(html),
        (html.match(/body\.chatting #msgLv3\{[^}]*\}/) || [''])[0].slice(0, 90));
  check('★ 还在用 inset:0 撑满（不是 100vh）',
        /body\.chatting #msgLv3\{[^}]*inset:0/.test(html));

  // ---- ⑯ ★ 使用说明每节都有「一键复制」----
  console.log('\n十六、★ 使用说明每节一个复制按钮');
  check('★ HTML 转纯文本的函数在（复制质量全靠它）',
        typeof htmlToPlain === 'function');
  check('★★ <br> 要变成真换行（不然复制出来挤成一整行）',
        htmlToPlain('第一行<br>第二行') === '第一行\n第二行',
        JSON.stringify(htmlToPlain('第一行<br>第二行')));
  check('★ 块级标签收尾也换行',
        htmlToPlain('<div>甲</div><div>乙</div>') === '甲\n乙',
        JSON.stringify(htmlToPlain('<div>甲</div><div>乙</div>')));
  check('★ 标签去干净、实体还原',
        htmlToPlain('<b>粗</b>&lt;代码&gt;&amp;号') === '粗<代码>&号',
        JSON.stringify(htmlToPlain('<b>粗</b>&lt;代码&gt;&amp;号')));
  check('★ 三个以上空行压成两个（复制的文本别一堆空行）',
        htmlToPlain('甲<br><br><br><br>乙') === '甲\n\n乙',
        JSON.stringify(htmlToPlain('甲<br><br><br><br>乙')));
  check('★ 复制出来的不带 HTML 标签',
        htmlToPlain('<div class="muted">hi</div>').indexOf('<') < 0);
  check('传空不炸', htmlToPlain('') === '' && htmlToPlain(null) === '');

  const helpStart2 = html.indexOf('id="pane-help"');
  const helpEnd = html.indexOf('</div>', html.indexOf('常见问题', helpStart2));
  const helpPane = html.slice(helpStart2, helpStart2 + 22000);
  const btnN = (helpPane.match(/copyHelp\(this\)/g) || []).length;
  check('★★ 每个说明分类都有复制按钮（7 个分类）', btnN === 7,
        '找到 ' + btnN + ' 个');
  // ★ 标题要写全 —— 有的带括号（「商城机器人（能量自助充值）」），
  //   写成简称会匹配不上，误报成「没按钮」
  for (const t of ['🆕 怎么拿到机器人（两种方式）', '📨 客服机器人',
                   '💰 USDT 助手', '🛒 商城机器人（能量自助充值）',
                   '📒 记账机器人', '📜 消息记录（把群消息存自己电脑上）',
                   '❓ 常见问题']) {
    check('  「' + t + '」那节有按钮',
          helpPane.indexOf('<summary>' + t + '<button') >= 0);
  }
  check('★★ 按钮不会顺带把这一节展开/收起（阻止了冒泡和默认行为）',
        helpPane.indexOf('event.preventDefault();event.stopPropagation();copyHelp') >= 0);

  // ---- ⑰ ★ 类型顺序（用户指定：记账 → 客服 → USDT → 商城）----
  console.log('\n十七、★ 机器人类型的顺序');
  const WANT_ORDER = ['ledger', 'kefu', 'usdt', 'shop'];
  check('★★ TYPE_TABS 顺序对',
        JSON.stringify(TYPE_TABS.map(t => t.v)) === JSON.stringify(WANT_ORDER),
        TYPE_TABS.map(t => t.v).join(' → '));
  check('★★ 默认选中的是第一个（不是写死的 kefu）',
        CUR_TYPE === TYPE_TABS[0].v, 'CUR_TYPE=' + CUR_TYPE);
  check('★ 每个类型都有中文名',
        TYPE_TABS.every(t => t.label && t.label.length > 2),
        TYPE_TABS.map(t => t.label).join(' / '));

  // 真的渲染一遍，看按钮顺序
  ME = { role: 'admin' };
  LAST_ALL = [];
  renderTypeTabs();
  const tabHtml = ELS['typeTabs'].innerHTML;
  const got = (tabHtml.match(/data-t="([a-z]+)"/g) || [])
    .map(s => s.replace(/data-t="|"/g, ''));
  check('★★ 页面上筛选条的顺序也对',
        JSON.stringify(got) === JSON.stringify(WANT_ORDER), got.join(' → '));

  // 添加机器人那个下拉
  const selHtml = html.slice(html.indexOf('<select id="type">'),
                              html.indexOf('</select>', html.indexOf('<select id="type">')));
  const selOrder = (selHtml.match(/value="([a-z]+)"/g) || [])
    .map(s => s.replace(/value="|"/g, ''));
  check('★★ 添加机器人下拉的顺序也对',
        JSON.stringify(selOrder) === JSON.stringify(WANT_ORDER),
        selOrder.join(' → '));

  check('★ 前端两处顺序一致（下拉 vs 筛选条）',
        JSON.stringify(selOrder) === JSON.stringify(TYPE_TABS.map(t => t.v)));

  // ---- ⑱ ★★ 商城配置弹窗：笔数档位（定价）要真的渲染出来 ----
  // 用户报「笔数套餐那里没有可以定价的地方」。后端测过了是好的，
  // 那就得确认前端到底有没有把这个文本框画出来。
  console.log('\n十八、★★ 商城配置里的「笔数档位」（定价的地方）');
  global.__SHOP_CFG = {
    enabled: true, trx_own: 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta',
    provider: 'mock', auto_enabled: true,
    auto_prices: { '10': 30, '50': 140 },
    premium_prices: { '3': 20, '6': 26 },
  };
  ME = { role: 'admin' };
  openCfg('botShop', '测试商城');
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const cb = ELS['cfgBody'] ? ELS['cfgBody'].innerHTML : '';
  check('配置弹窗渲染出来了（不是报错页）',
        cb.indexOf('cfgrow') >= 0, cb.slice(0, 80).replace(/\n/g, ''));
  check('★★ 有「笔数档位」这一行（就是定价的地方）',
        cb.indexOf('笔数档位') >= 0, cb.slice(0, 0) || '');
  check('★★ 有那个可输入的文本框 input/textarea#c_ap',
        cb.indexOf('id="c_ap"') >= 0);
  check('★ 已配好的档位会预填进去（「10 30」）',
        cb.indexOf('10 30') >= 0,
        (cb.match(/id="c_ap"[^>]*>[^<]{0,60}/) || [''])[0].slice(-40));
  check('★ 有「笔数套餐」开关',
        cb.indexOf('笔数套餐') >= 0 && cb.indexOf('id="c_aen"') >= 0);
  check('★ 写清楚了格式（每行「笔数 价格TRX」）',
        cb.indexOf('每行一档') >= 0);

  // 存回去：文本框里改了之后要能提交
  // ★ 假 DOM 不解析 innerHTML，所以要先把节点「摸」出来（getElementById 会建）
  //   再改它的 value —— 直接写 ELS['c_ap'] 那时候它还不存在，赋值会静默丢掉
  document.getElementById('c_ap').value = '20 55\n  30 80\n坏行';
  saveCfg();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const savedTiers = global.__SHOP_CFG.auto_prices || {};
  check('★★ 文本框改了能存回去（不然就是「设不了价」）',
        Object.keys(savedTiers).length > 0, JSON.stringify(savedTiers));
  check('★ 乱行会被丢掉、合法行留下',
        savedTiers['20'] !== undefined && savedTiers['坏行'] === undefined,
        JSON.stringify(savedTiers));

  // ★★ 布局：文本框被同行的说明文字挤成一条缝（用户报的那个）
  //    假 DOM 不做排版，所以只能盯 CSS 规则本身
  console.log('\n   —— 布局：文本框不能被挤没 ——');
  const cfgrowCss = html.slice(html.indexOf('.cfgrow{'),
                                html.indexOf('.ordtbl td{'));
  check('★★ .cfgrow textarea 有 flex:1（漏了它就会被挤成一条缝）',
        /\.cfgrow textarea\{[^}]*flex:1/.test(cfgrowCss),
        (cfgrowCss.match(/\.cfgrow textarea\{[^}]*\}/) || [''])[0].slice(0, 70));
  check('★★ 而且有 min-width（不然还能被压到一个字符宽）',
        /\.cfgrow textarea\{[^}]*min-width:\s*\d+px/.test(cfgrowCss));
  check('★★ 说明文字允许收缩（flex:none 会把宽度全占掉）',
        /\.cfgrow \.muted\{[^}]*flex:0 1/.test(cfgrowCss),
        (cfgrowCss.match(/\.cfgrow \.muted\{[^}]*\}/) || [''])[0]);
  check('★★ 笔数档位那个框没写死内联宽度（写了会跟 flex 打架）',
        cb.indexOf('id="c_ap"') >= 0
        && cb.slice(cb.indexOf('id="c_ap"'),
                    cb.indexOf('id="c_ap"') + 120).indexOf('width:') < 0,
        cb.slice(cb.indexOf('id="c_ap"'), cb.indexOf('id="c_ap"') + 100));
  // ★★ 光靠 flex 还不够：标签+框+一大段说明横着排，宽度本来就不够。
  //    所以笔数档位用的是「上下排」的 .l_cfgrow（框和说明在右边一列竖着放）
  check('★★ 用 .l_cfgrow 上下排（横排放不下会把框挤没）',
        cb.indexOf('class="l_cfgrow"') >= 0
        && cb.indexOf('class="col"') >= 0,
        cb.slice(cb.indexOf('l_cfgrow'), cb.indexOf('l_cfgrow') + 90));
  check('★★ .l_cfgrow 的 CSS 在（textarea 撑满 + 说明竖着放）',
        html.indexOf('.l_cfgrow') >= 0
        && /\.l_cfgrow textarea\{[^}]*min-width:0/.test(html),
        (html.match(/\.l_cfgrow textarea\{[^}]*\}/) || [''])[0].slice(0, 60));
  check('★★ 商城配置弹窗加宽了（380px 太窄，装不下这些行）',
        /#shopCfgModal \.box\{[^}]*width:min\(\d+px/.test(html),
        (html.match(/#shopCfgModal \.box\{[^}]*\}/) || [''])[0].slice(0, 60));

  // ---- ⑲ ★★ 商户后台：只给客户配商城机器人 ----
  console.log('\n十九、★★ 商户后台只留「机器人列表 / 使用说明」，且只有商城');
  const shown = el => el.style.display !== 'none';
  const tabOf = p => TAB_NODES.find(t => t.getAttribute('data-pane') === p);

  // —— 管理员：全都看得到 ——
  ME = { role: 'admin' };
  applyRoleTabs();
  check('★ 管理员看得到「消息记录」标签', shown(tabOf('msg')));
  check('★ 管理员看得到「添加机器人」', shown(tabOf('add')));
  check('★ 管理员看得到全部 4 类机器人',
        typeTabs().length === 4, String(typeTabs().length));
  check('★ 管理员看得到全部 8 块（7 节说明 + 顶部提示）',
        HELP_NODES.filter(shown).length === 8,
        String(HELP_NODES.filter(shown).length));

  // —— 商户：只留机器人列表 + 使用说明（商城那节）——
  ME = { role: 'merchant', name: '张三' };
  applyRoleTabs();
  check('★★ 商户看不到「消息记录」标签（管理端专属）', !shown(tabOf('msg')));
  check('★★ 商户看不到「添加机器人」', !shown(tabOf('add')));
  check('★★ 商户看不到「商户管理」', !shown(tabOf('merch')));
  check('★★ 商户看不到「登录记录」', !shown(tabOf('logs')));
  check('★ 商户还能看到「机器人列表」', shown(tabOf('bots')));
  check('★ 商户还能看到「使用说明」', shown(tabOf('help')));

  check('★★ 商户只看到「🛒 商城机器人」这一类',
        JSON.stringify(typeTabs().map(t => t.v)) === '["shop"]',
        typeTabs().map(t => t.v).join(','));
  check('★★ 使用说明只剩 1 块（商城机器人；顶部那块提示也没了）',
        HELP_NODES.filter(shown).length === 1,
        HELP_NODES.filter(shown).map(n => n.getAttribute('data-for')).join(','));
  check('★★ 顶部那块「第一次用先看…」提示标了 admin（商户看不到）',
        /<div class="card" data-for="admin">\s*<h2>📖 使用说明<\/h2>/.test(html),
        '那块提示（让人先看「怎么拿到机器人」）对商户是误导，得一起藏');
  check('★★ 而且留下的那节是 shop',
        HELP_NODES.filter(shown)[0]
          && HELP_NODES.filter(shown)[0].getAttribute('data-for') === 'shop',
        String(HELP_NODES.filter(shown)[0]
               && HELP_NODES.filter(shown)[0].getAttribute('data-for')));

  // 渲染出来的类型标签也只该有一个
  ME = { role: 'merchant', name: '张三' };
  LAST_ALL = [];
  renderTypeTabs();
  const mTabs = (ELS['typeTabs'].innerHTML.match(/data-t="([a-z]+)"/g) || []);
  check('★★ 页面上真的只渲染出商城那一个类型标签',
        mTabs.length === 1 && mTabs[0] === 'data-t="shop"',
        mTabs.join(','));
  check('★★ 商户默认停在「商城机器人」那一类（不是记账）',
        CUR_TYPE === 'shop', CUR_TYPE);

  // 就算被塞了别的类型，也不能显示出来
  ME = { role: 'merchant' };
  showType('ledger');
  check('★★ 商户想切到「记账机器人」也切不过去（兜底）',
        CUR_TYPE === 'shop', CUR_TYPE);

  // ---- ⑳ ★★ 谷歌验证码：绑定 / 看真实 IP / 清空 的完整前端流程 ----
  // 用户在服务器上「绑了 App 但面板没绑上」，得先证明**前端这段没写错**
  console.log('\n二十、★★ 谷歌验证码的前端流程');
  ME = { role: 'admin' };
  global.__TOTP_BOUND = false;
  global.__TOTP_UNLOCKED = false;
  global.__TOTP_SECRET = 'ABCDEFGHIJKLMNOP';
  global.__LOGINS = [{ t: 1790609607, ip: '203.0.113.77',
                       ip_masked: '203.0.*.*', city: '测试市',
                       role: 'admin', user: '（管理员）', ok: true,
                       kind: 'login', ua: 'Mozilla/5.0 (Windows NT)' }];

  loadLogins();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));

  const tb = ELS['totp-body'] ? ELS['totp-body'].innerHTML : '';
  check('★ 未绑定时把密钥摆出来给用户去 App 里加',
        tb.indexOf("ABCDEFGHIJKLMNOP") >= 0, tb.slice(0, 70));
  check('★ 密钥点一下能复制（不用手抄）',
        tb.indexOf('copyText(TOTP.secret') >= 0);
  check('★★ 有「启用验证码保护」按钮',
        tb.indexOf('bindTotp()') >= 0 && tb.indexOf('启用验证码保护') >= 0,
        tb.slice(0, 110));
  // ★★ 用户最容易漏的就是「在 App 里加好了，但没回来点按钮」——
  //    2026-09-28 就是这么栽的，所以这句必须在最显眼的地方
  check('★★ 分成第 1 步/第 2 步，说清楚「光在 App 里加好不算」',
        tb.indexOf('第 1 步') >= 0 && tb.indexOf('第 2 步') >= 0
        && tb.indexOf('必须点这个按钮才生效') >= 0,
        tb.slice(-160));

  const logsHtml = ELS['list-logs'].innerHTML;
  check('★★ 未绑定时 IP 也是打码的', logsHtml.indexOf('203.0.*.*') >= 0
        && logsHtml.indexOf('203.0.113.77') < 0,
        logsHtml.slice(logsHtml.indexOf('203'), logsHtml.indexOf('203') + 40));

  // —— 绑定：输错码不该绑上 ——
  resetToast();
  bindTotp();
  check('★ 点「我已绑定」会弹输码框',
        ELS['totpModal'].style.display === 'flex'
        && ELS['totp_code'] !== undefined);
  document.getElementById('totp_code').value = '000000';
  submitTotp();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 输错码 → 绑不上，而且提示出来',
        global.__TOTP_BOUND === false && toastText().indexOf('不对') >= 0,
        toastText().slice(0, 50));

  // —— 输对码 ——
  resetToast();
  bindTotp();
  document.getElementById('totp_code').value = '123456';
  submitTotp();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 输对码 → 真的绑上了（前端这段没写错）',
        global.__TOTP_BOUND === true, String(global.__TOTP_BOUND));
  check('★ 绑完提示成功', toastText().indexOf('绑定成功') >= 0,
        toastText().slice(0, 40));
  // ★★ 那块承载绑定说明的容器**不能删** —— 删了的话 loadTotp() 找不到它
  //    就直接 return，结果是「没绑定的时候什么都不显示」，永远没法绑。
  //    （这是我改这块时踩过的：把 totp-body 一起删掉了）
  check('★★ 绑定说明的容器 #totp-body 还在（删了就没法绑了）',
        html.indexOf('id="totp-body"') >= 0);
  // ★ 登录记录那一大段说明删了，只留标题里的「共 N 次」
  check('★★ 登录记录下面那一大段说明已删（只留「共 N 次」）',
        html.indexOf('谁在什么时候、从哪个 IP') < 0,
        html.slice(0, 0) || '');
  check('★ 但「共 N 次」那个计数还在',
        html.indexOf('id="cnt-logs"') >= 0);
  // ★★ 启用之后那块**整个空着**（用户要求：「默认就是开启了」，
  //    不想再看到状态文字，也不给「换密钥」的入口）
  check('★★ 启用后那块变成空白（不再念叨「已启用」）',
        (ELS['totp-body'].innerHTML || '').trim() === '',
        (ELS['totp-body'].innerHTML || '').slice(0, 60));
  check('★★ 界面上没有「换一个密钥」入口了',
        html.indexOf('>换一个密钥<') < 0);
  check('★ 绑完 IP 又藏起来了（要重新解锁才看）',
        ELS['list-logs'].innerHTML.indexOf('203.0.113.77') < 0);

  // —— 已绑定：看真实 IP 要输码 ——
  global.__TOTP_UNLOCKED = false;
  loadLogins();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 已绑定后 IP 又藏起来了',
        ELS['list-logs'].innerHTML.indexOf('203.0.113.77') < 0,
        ELS['list-logs'].innerHTML.slice(0, 0) || '');

  resetToast();
  revealLogins();
  check('★★ 点「看真实 IP」→ 弹输码框（不是直接就给）',
        ELS['totpModal'].style.display === 'flex');
  document.getElementById('totp_code').value = '123456';
  submitTotp();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 输对码 → 看到真实 IP',
        ELS['list-logs'].innerHTML.indexOf('203.0.113.77') >= 0,
        ELS['list-logs'].innerHTML.slice(0, 0) || '');
  // ★★ 真实 IP 不加黄色高亮（用户：「颜色不需要加深，和背景颜色一样即可」）
  //    class="code" 是绑定码那种高亮样式，别用在 IP 上
  check('★★ 真实 IP 没有黄色高亮（不带 class="code"）',
        ELS['list-logs'].innerHTML.indexOf('class="code"') < 0,
        (ELS['list-logs'].innerHTML.match(/<td[^>]*>203\.0\.113\.77/) || [''])[0]);

  // ★★ 登录记录只留四列：时间 / IP / 城市 / 设备
  //    用户 2026-09-29：「把谁还有方式还有结果去掉」。别加回来，他嫌乱。
  (function(){
    var t = ELS['list-logs'].innerHTML;
    var ths = (t.match(/<th>[^<]*<\/th>/g) || []).map(function(x){
      return x.replace(/<\/?th>/g, '');
    });
    check('★★ 登录记录只剩 4 列：时间 / IP / 城市 / 设备',
          ths.join(',') === '时间,IP,城市,设备', ths.join(','));
    check('★★ 去掉的三列（谁 / 方式 / 结果）没回来',
          t.indexOf('<th>谁</th>') < 0 && t.indexOf('<th>方式</th>') < 0
          && t.indexOf('<th>结果</th>') < 0);
    check('★ 表格里不再有「成功 / 失败」和「访问 / 登录」标签',
          t.indexOf('>成功<') < 0 && t.indexOf('>失败<') < 0
          && t.indexOf('>访问<') < 0 && t.indexOf('>登录<') < 0);
    check('★ 时间 / 城市 / 设备三列的内容还在（别把列删过头）',
          t.indexOf('203.0.113.77') >= 0 && t.indexOf('测试市') >= 0);
    var cnt = String(ELS['cnt-logs'] && ELS['cnt-logs'].textContent || '');
    check('★ 计数只报总数（「失败 M 次」也去掉了 —— 那也是「结果」）',
          cnt.indexOf('共') >= 0 && cnt.indexOf('失败') < 0, cnt);
  })();
  check('★ 按钮变成「锁上」了',
        (ELS['btn-reveal'] && ELS['btn-reveal'].textContent) === '🔒 锁上',
        String(ELS['btn-reveal'] && ELS['btn-reveal'].textContent));

  // —— 已绑定：清空要输码 ——
  global.__TOTP_UNLOCKED = false;
  reloadLoginsCount = 0;
  global.__LOGINS = [{ t: 1, ip: '203.0.113.77', ip_masked: '203.0.*.*',
                       city: 'x', role: 'admin', user: '（管理员）', ok: true,
                       kind: 'login', ua: '' }];
  loadLogins();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  resetToast();
  clearLogins();          // 已绑定 → 应该弹输码框，而不是直接清
  check('★★ 点「清空」→ 弹输码框（已绑定时不能直接清）',
        ELS['totpModal'].style.display === 'flex');
  check('★ 这时还没清（等验证码）',
        (global.__LOGINS || []).length === 1,
        String((global.__LOGINS || []).length));
  document.getElementById('totp_code').value = '123456';
  submitTotp();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 输对码 → 清空成功', (global.__LOGINS || []).length === 0,
        String((global.__LOGINS || []).length));

  // —— 没绑定的时候：清空不该弹输码框（不然用户被卡死）——
  global.__TOTP_BOUND = false;
  global.__LOGINS = [{ t: 1, ip: 'x', ip_masked: 'x', city: 'x',
                       role: 'admin', user: '', ok: true, kind: 'login', ua: '' }];
  loadLogins();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const wasModal = ELS['totpModal'].style.display;
  clearLogins();
  check('★★ 没绑定时清空不弹输码框（confirm 默认 true 就直接清）',
        ELS['totpModal'].style.display !== 'flex'
        || wasModal === 'flex',
        '实际 display=' + ELS['totpModal'].style.display);

  // ---- ⑫ ★ 机器人列表的操作按钮 / 统计列 ----
  // 用户 2026-09-29 要求：
  //   · 记账 / 客服 / USDT —— 操作里只留「续期 / 备注 / 删除」，
  //     统计列（记账群 / 客户 / 监听地址）撤掉
  //   · 商城 —— 续期 / 配置 / 备注 / 删除，统计列「待处理」留着
  //   · 订单只有**商户后台**能看，管理后台不给入口
  // ★ 放在最后跑，并且把 __BOTS / ME 原样还原 —— 免得影响前面的用例
  console.log('\n二十一、★ 机器人列表：操作按钮 + 统计列');
  {
    const savedBots = global.__BOTS, savedME = ME;
    const mkBot = (t, id) => ({ id: id, note: id, type: t, type_name: t,
                                username: id + 'bot', enabled: true,
                                status: 'running', expired: false, stat: 7,
                                bound: true, admin_name: '我', admin_id: '1',
                                admin_count: 1, bind_code: 'ABC123',
                                created: '09-29 00:00' });

    global.__BOTS = [mkBot('kefu', 'kA'), mkBot('usdt', 'uA'),
                     mkBot('ledger', 'lA'), mkBot('shop', 'sA')];

    // —— 管理员视角 ——
    ME = { role: 'admin' };
    render({ bots: global.__BOTS, durations: [], grace_days: 7 });

    ['kefu', 'usdt', 'ledger'].forEach(function(t, i){
      const id = ['kA', 'uA', 'lA'][i];
      const tb = ELS['list-' + t].innerHTML;
      const ths = (tb.match(/<th>[^<]*<\/th>/g) || [])
                    .map(x => x.replace(/<\/?th>/g, ''));
      check('★ ' + t + ' 表头没有统计列了（只有 备注/机器人/绑定码/使用者/状态/到期/操作）',
            ths.join(',') === '备注,机器人,绑定码,使用者,状态,到期,操作',
            ths.join(','));
      check('★ ' + t + ' 操作里只有 续期 / 备注 / 删除',
            tb.indexOf('>续期<') >= 0 && tb.indexOf('>备注<') >= 0
            && tb.indexOf('>删除<') >= 0
            && tb.indexOf('停用') < 0 && tb.indexOf('启用') < 0
            && tb.indexOf('换绑定码') < 0 && tb.indexOf('Token') < 0
            && tb.indexOf('⚙️ 配置') < 0);
      check('  ' + t + ' 备注按钮接的是 editNote（复用已有函数）',
            tb.indexOf("editNote('" + id + "')") >= 0);
    });

    const shopTb = ELS['list-shop'].innerHTML;
    const shopThs = (shopTb.match(/<th>[^<]*<\/th>/g) || [])
                      .map(x => x.replace(/<\/?th>/g, ''));
    check('★ 商城保留统计列「待处理」',
          shopThs.join(',') === '备注,机器人,绑定码,使用者,状态,到期,待处理,操作',
          shopThs.join(','));
    check('★ 商城操作：续期 / 配置 / 备注 / 删除',
          shopTb.indexOf('>续期<') >= 0 && shopTb.indexOf('⚙️ 配置') >= 0
          && shopTb.indexOf('>备注<') >= 0 && shopTb.indexOf('>删除<') >= 0
          && shopTb.indexOf('停用') < 0 && shopTb.indexOf('换绑定码') < 0
          && shopTb.indexOf('Token') < 0);
    check('★★ 管理后台**看不到**「📋 订单」按钮',
          shopTb.indexOf('📋 订单') < 0, '管理后台不该有订单入口');

    // —— 商户视角：订单要给 ——
    ME = { role: 'merch', mid: 'm1' };
    render({ bots: global.__BOTS, durations: [], grace_days: 7 });
    const shopM = ELS['list-shop'].innerHTML;
    check('★★ 商户后台**能**看到「📋 订单」按钮',
          shopM.indexOf('📋 订单') >= 0);
    check('★ 商户还能配自己的商城 + 换 Token',
          shopM.indexOf('⚙️ 配置') >= 0 && shopM.indexOf('Token') >= 0);
    check('★ 商户没有「删除 / 续期」（那是管理员的事）',
          shopM.indexOf('>删除<') < 0 && shopM.indexOf('>续期<') < 0);


    // ---- ★★ 手机上的横向滚动：数据没变就不许重画 ----
    // 每 5 秒 refresh() 一次，一重画 innerHTML 就把 scrollLeft 归零 ——
    // 表现为「滑到右边看后面的列，过几秒自己跳回最左边」（用户报的）
    ME = { role: 'admin' };
    // 先按管理员视角渲染一次，让 setHTML 的缓存稳定下来
    // （上一步是商户视角，HTML 本来就不一样，直接弄脏会误判）
    render({ bots: global.__BOTS, durations: [], grace_days: 7 });
    ELS['list-kefu'].innerHTML = '脏数据';
    render({ bots: global.__BOTS, durations: [], grace_days: 7 });
    check('★★ 机器人列表数据没变时不碰 DOM（横向滚动位置保得住）',
          ELS['list-kefu'].innerHTML === '脏数据',
          ELS['list-kefu'].innerHTML.slice(0, 60));
    global.__BOTS[0].note = '改了名';
    render({ bots: global.__BOTS, durations: [], grace_days: 7 });
    check('★ 数据变了还是要重画（别为了保滚动连刷新都不做了）',
          ELS['list-kefu'].innerHTML.indexOf('改了名') >= 0);

    check('★ 有 setHTML 这个保滚动的替换函数',
          typeof setHTML === 'function' && typeof clearHTMLCache === 'function');
    check('★★ 换人登录 / 退出时会清缓存（不然可能看到上一个人的页面）',
          PANEL_JS.indexOf('clearHTMLCache();\n  applyRole') >= 0
          || (PANEL_JS.indexOf('clearHTMLCache()') >= 0
              && PANEL_JS.split('clearHTMLCache()').length >= 3));

    // ---- ★ 手机上的弹窗不能超屏 ----
    check('★★ 手机端弹窗改成撑满 + 盒内可滚（验证码框原来会溢出）',
          has(html, '#totpModal .box{\n    width:100%;max-width:100%')
          || (has(html, 'max-height:86vh;overflow-y:auto')
              && has(html, '#totpModal{padding:12px;overflow-y:auto}')));
    check('★ 手机端验证码输入框撑满（原来只占一半宽）',
          has(html, '#totpModal input'));

    // ---- ★★ 「客户自建」的机器人 ----
    // 背景：`manager.snapshot()` 是**白名单式**拼字段的，
    // 漏了 remote 前端就永远收不到 —— 表现为「面板上怎么都不显示
    // 客户自建、也没有安装包按钮」，但接口层怎么测都是对的。
    ME = { role: 'admin' };
    global.__BOTS = [Object.assign(mkBot('ledger', 'rA'), { remote: true }),
                     mkBot('ledger', 'nA')];
    clearHTMLCache();
    render({ bots: global.__BOTS, durations: [], grace_days: 7 });
    const rTb = ELS['list-ledger'].innerHTML;
    check('★★ 客户自建的显示「客户自建」（不是「已停止」）',
          has(rTb, '客户自建'), rTb.slice(0, 70));
    check('★★ 客户自建的才有「⬇️ 安装包」按钮（点了直接下填好的包）',
          has(rTb, '⬇️ 安装包') && has(rTb, 'soloZip('));
    check('★ 普通机器人**没有**这个按钮（只有客户自建的才多出来）',
          (rTb.match(/soloZip\(/g) || []).length === 1,
          '找到 ' + (rTb.match(/soloZip\(/g) || []).length + ' 个');
    // ★ 补票：没勾「客户自建」的记账机器人，操作列要有个「☁ 客户自建」
    //   （勾选框只在添加时才有，忘了勾只能删掉重建 —— 而重建会换 id，
    //     已经发给客户的安装包就失效了）
    check('★★ 没设成客户自建的记账机器人，有个「☁ 客户自建」补票按钮',
          has(rTb, '☁ 客户自建') && has(rTb, 'setRemote('));
    check('★ 补票按钮只出现在**没设过**的那台上（设过的用不着）',
          (rTb.match(/setRemote\(/g) || []).length === 1,
          '找到 ' + (rTb.match(/setRemote\(/g) || []).length + ' 个');

    // 还原，免得影响别处
    global.__BOTS = savedBots;
    ME = savedME;
    clearHTMLCache();
    render({ bots: global.__BOTS, durations: [], grace_days: 7 });
  }

  // ---- ⑬ ★ 表格对齐（用户 2026-09-29）----
  // 「机器人列表的操作在屏幕的最右边，显得规整一点」
  // 「机器人列表和群消息里面的标签居中对齐」
  // 「群消息列表的最后一条删除」
  console.log('\n二十二、★ 表格对齐 + 群消息去掉「最后一条」');
  // 注意要连 ctr-mid 一起数（那两张是 class="ctr ctr-mid"）
  const ctrCount = (PANEL_JS.match(/<table class="ctr[\s"]/g) || []).length;
  check('★ 机器人列表(×2，标签页里 + 消息记录里)和群消息的表都带 class="ctr"',
        ctrCount >= 3, '找到 ' + ctrCount + ' 处');
  check('★★ 中间各列居中',
        has(html, '.ctr th,.ctr td{text-align:center}'));
  check('★★ 第一列靠左 —— 居中的话名字会飘在列中间、左边空一大块',
        has(html, '.ctr th:first-child,.ctr td:first-child{text-align:left}'));
  // ★★ 「操作」要落在按钮正中间上方（用户 2026-09-29 圈了三次截图才说清）
  //    关键在 width:1% —— 把列宽收缩到刚好放下按钮。
  //    少了它，这一列会把表格剩余宽度全吃掉，「居中」就变成
  //    「在一大片空地里居中」，又飘走了。改完跑 _shot_css.py 看一眼图。
  check('★★ 按钮贴右、「操作」落在按钮正中间（列宽收缩到按钮那么宽）',
        has(html, '.ctr th:last-child,.ctr td:last-child{width:1%;white-space:nowrap}')
        && has(html, '.ctr th:last-child{text-align:center}')
        && has(html, '.ctr td:last-child{text-align:right}'),
        '少了 width:1% 就飘走了');
  // ★★ 首尾两列必须**成对**收缩 —— 只收一个的话，表格剩余宽度会
  //    全跑到另一头去，中间列就被挤偏（用户报过两次）。
  //    改完跑 `python _shot_css.py` 看一眼图，别靠脑补。
  check('★★ 首尾两列都收缩到「刚好放得下内容」（成对，不能只改一个）',
        has(html, '.ctr th:first-child,.ctr td:first-child{width:1%;white-space:nowrap}')
        && has(html, '.ctr th:last-child,.ctr td:last-child{width:1%;white-space:nowrap}'));
  check('★★ 消息记录两张表标了 ctr-mid（让「未读」吸收剩余宽度、落在正中）',
        has(html, '.ctr-mid th:nth-last-child(2),.ctr-mid td:nth-last-child(2){width:auto}')
        && (PANEL_JS.match(/<table class="ctr ctr-mid">/g) || []).length >= 2,
        '找到 ' + (PANEL_JS.match(/<table class="ctr ctr-mid">/g) || []).length + ' 处');
  check('★ 机器人列表那两张表**没有** ctr-mid（列多，均摊更好看）',
        (PANEL_JS.match(/<table class="ctr">/g) || []).length >= 1);
  check('★ 群消息列表不再有「最后一条」列',
        PANEL_JS.indexOf('<th>最后一条</th>') < 0
        && PANEL_JS.indexOf('c.last_ts') < 0);
  check('★ 群消息实际渲染出来就是 4 列（群 / 消息数 / 未读 / 操作）',
        (function(){
          const ths = (ELS['msgChats'].innerHTML.match(/<th>[^<]*<\/th>/g) || [])
                        .map(x => x.replace(/<\/?th>/g, ''));
          return ths.join(',') === '群,消息数,未读,操作';
        })(), ELS['msgChats'].innerHTML.slice(0, 130));

  // ---- ⑭ ★★★ 「切走再切回来，卡在加载中…再也不动了」----
  // 用户 2026-09-29 报的：登录后消息记录正常 → 点「机器人列表」→
  // 再点回「消息记录」，就一直停在「加载中…」。
  //
  // 根因：往容器里写「加载中…」的时候**直接 innerHTML 赋值**，
  // 绕过了 setHTML 的「上次画了什么」缓存；等真数据回来，
  // setHTML 一比对「跟上次那个表格一样」→ **跳过重画** →
  // 「加载中…」永远留在那儿。
  //
  // 规矩：**凡是 setHTML 管的容器，写任何东西都必须走 setHTML**，
  // 否则缓存就和真实 DOM 对不上了。
  console.log('\n二十三、★★★ 切走再切回来不能卡在「加载中…」');
  // ★ 先等一拍：上一段的 clearLogins() 是个 promise，它的 .then 里会
  //   把 __LOGINS 清空 —— 不等它跑完就设数据，会被它反手清掉，
  //   于是这里读回来是空的（第一次写这段时就栽在这儿）
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  global.__TOTP_BOUND = false;
  global.__LOGINS = [{ t: 1, ip: '1.2.3.4', ip_masked: '1.2.*.*',
                       city: '测试市', role: 'admin', user: '', ok: true,
                       kind: 'login', ua: '' }];
  loadLogins();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('第一次：登录记录画出来了（不是「加载中…」）',
        ELS['list-logs'].innerHTML.indexOf('<table') >= 0,
        ELS['list-logs'].innerHTML.slice(0, 80));
  loadLogins();                       // ← 切走再切回来
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 切回来照样画得出来（就是这条被卡住过）',
        ELS['list-logs'].innerHTML.indexOf('<table') >= 0
        && ELS['list-logs'].innerHTML.indexOf('加载中') < 0,
        ELS['list-logs'].innerHTML.slice(0, 80));

  MSG.LEVEL = 1;
  msgLoadBots();
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  const botsFirst = ELS['msgBots'].innerHTML.indexOf('<table') >= 0;
  msgLoadBots();                      // ← 切走再切回来
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  check('★★ 消息记录的机器人列表也一样（同一个毛病）',
        botsFirst && ELS['msgBots'].innerHTML.indexOf('<table') >= 0
        && ELS['msgBots'].innerHTML.indexOf('加载中') < 0,
        ELS['msgBots'].innerHTML.slice(0, 80));

  check('★★ 三个容器都规规矩矩走 setHTML（别再直写）',
        ['list-logs', 'msgBots', 'msgChats'].every(function(id){
          return PANEL_JS.indexOf("setHTML('" + id + "'") >= 0;
        }));

  console.log('\n' + '='.repeat(62));
  console.log('结果：' + OK.length + ' 项通过，' + BAD.length + ' 项失败');
  BAD.forEach(x => console.log('  ❌ ' + x));
  console.log('='.repeat(62));
  process.exit(BAD.length ? 1 : 0);
})();

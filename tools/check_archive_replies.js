const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'messages_page.html'), 'utf8');
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(html.slice(html.indexOf('function esc('), html.indexOf('/* ---------- 登录'))
  + html.slice(html.indexOf('var KIND_NAMES'), html.indexOf('/* 消息区自己能滚')), sandbox);
const record = {chat_id: '-1', message_id: '2', display_name: 'Sender', text: 'Reply body',
  reply: {message_id: '1', display_name: '<img src=x onerror=alert(1)>',
    text: '<script>alert(1)</script>', media: 'quoted.png'}};
const rendered = sandbox.msgHtml(record);
assert.match(rendered, /data-reply-key="-1\|1"/);
assert.match(rendered, /data-name="quoted.png"/);
assert.match(rendered, /Reply body/);
assert.match(rendered, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
assert.ok(!rendered.includes('<script>') && !rendered.includes('<img src=x'));
const panel = fs.readFileSync(path.join(__dirname, '..', 'panel_page.html'), 'utf8');
sandbox.fmtTs = sandbox.fmt;
vm.runInContext(panel.slice(panel.indexOf('function msgReplyHtml('), panel.indexOf('function msgScrollBottom(')), sandbox);
const mainRendered = sandbox.msgBubble(record);
assert.match(mainRendered, /data-reply-key="-1\|1"/);
assert.match(mainRendered, /data-name="quoted.png"/);
assert.match(mainRendered, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
assert.ok(!mainRendered.includes('<script>') && !mainRendered.includes('<img src=x'));
delete record.reply;
assert.ok(!sandbox.msgHtml(record).includes('data-reply-key'));
assert.ok(!sandbox.msgBubble(record).includes('data-reply-key'));
console.log('Both archive pages: reply preview, photo thumbnail and HTML escaping passed.');

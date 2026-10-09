const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '..', 'panel_page.html'), 'utf8');
const functions = html.slice(html.indexOf('function setRemote('), html.indexOf('function sumUnread('));
const calls = [];
let accept = false;
const sandbox = {
  confirm(message) { calls.push(['confirm', message]); return accept; },
  api(url, options) { calls.push(['api', url, JSON.parse(options.body)]); return Promise.resolve({ok: true}); },
  toast() {}, refresh() {}, apiHeaders() { return {}; }, location: {origin: 'https://example.test'},
  fetch(url) { calls.push(['download', url]); return Promise.resolve({ok: true, blob() { return Promise.resolve({}); }}); },
  URL: {createObjectURL() { return 'blob:fake'; }, revokeObjectURL() {}}, setTimeout() {},
  document: {createElement() { return {click() {}, remove() {}}; }, body: {appendChild() {}}},
};
vm.createContext(sandbox);
vm.runInContext(functions, sandbox);
async function main() {
  sandbox.setRemote('testbot', '测试', true);
  assert.equal(calls.filter(c => c[0] === 'api').length, 0);
  assert.match(calls[0][1], /小程序配置中心继续可用/);
  accept = true;
  sandbox.setRemote('testbot', '测试', true);
  sandbox.setRemote('testbot', '测试', false);
  assert.deepEqual(calls.filter(c => c[0] === 'api').map(c => c[2]), [{on: true}, {on: false}]);
  calls.length = 0;
  sandbox.soloZip('testbot', '测试');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(c => c[0] === 'api').length, 0);
  assert.equal(calls.filter(c => c[0] === 'download').length, 1);
  assert.match(calls[0][1], /\/solozip\?url=/);
  console.log('Panel actions: cancellation, explicit self-host/restore and download without mode mutation passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });

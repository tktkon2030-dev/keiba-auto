// Offline behavioral tests; no Google account, external calls, or file deletion.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'Code.gs'), 'utf8');
function harness(seed = [], failAt = null) {
  const files = seed.map(x => ({mime: 'text/csv', modified: 1, trashed: false, ...x}));
  const calls = [];
  let tick = 100, lockHeld = false, unlocks = 0;
  const context = {
    Utilities: {newBlob: (content, mime, name) => ({content, mime, name})},
    PropertiesService: {getScriptProperties: () => ({getProperty: () => 'folder'})},
    LockService: {getScriptLock: () => ({tryLock: () => !lockHeld && (lockHeld = true), releaseLock: () => {lockHeld = false; unlocks++;}})},
    ContentService: {MimeType: {JSON: 'json'}, createTextOutput: text => ({text, setMimeType() {return this;}})}
  };
  function expose(file) { return {getId: () => file.id, getMimeType: () => file.mime,
    getLastUpdated: () => new Date(file.modified), isTrashed: () => file.trashed}; }
  const folder = {getId: () => 'folder', getFilesByName(name) {
    const matching = files.filter(f => f.name === name && !f.trashed); let i = 0;
    return {hasNext: () => i < matching.length, next: () => expose(matching[i++])};
  }};
  function before(kind) {calls.push(kind); if (calls.length === failAt) throw new Error('private failure detail');}
  const drive = {Files: {
    update(meta, id, blob) { before('update'); const file = files.find(f => f.id === id);
      Object.assign(file, {content: blob.content, mime: meta.mimeType, modified: tick++}); return {id}; },
    create(meta, blob) {before('create'); const id = 'new-' + tick++;
      files.push({id, name: meta.name, content: blob.content, mime: meta.mimeType, modified: tick++, trashed: false}); return {id};}
  }};
  context.Drive = drive; context.DriveApp = {getFolderById: () => folder};
  vm.createContext(context); vm.runInContext(source, context);
  const post = payload => JSON.parse(context.doPost({postData: {contents: JSON.stringify(payload)}}).text);
  return {files, calls, post, unlocks: () => unlocks, failOff() {failAt = null;}};
}
const file = (prefix, content = '馬番,馬名\n1,テスト\n', date = '2026-10-31') => ({filename: prefix + '_' + date + '.csv', content});
const four = ['keiba_integrated', 'horse_history', 'keiba_combinations', 'keiba_ticket_candidates'].map(p => file(p));
let h = harness();
assert.equal(h.post({files: four}).success, true);
const ids = h.files.map(f => f.id);
assert.equal(h.post({files: four}).success, true);
assert.equal(h.files.length, 4); assert.deepEqual(h.files.map(f => f.id), ids);
assert.equal(h.unlocks(), 2);
assert.ok(h.files.every(f => f.content.startsWith('\uFEFF馬番')));
assert.equal(h.post({files: [file('keiba_integrated', '\uFEFF更新\n')]}).success, true);
assert.equal(h.files[0].content, '\uFEFF更新\n');
assert.equal(h.post({files: [file('keiba_integrated', '翌日\n', '2026-11-01')]}).success, true);
assert.equal(h.files.length, 5);
// Legacy single-file payload remains supported.
assert.equal(h.post(file('horse_history', '旧形式\n')).success, true);
h = harness([{id:'old',name:four[0].filename,modified:1,content:'old'},
             {id:'recent',name:four[0].filename,modified:2,content:'recent'}]);
let result = h.post({files:[four[0]]});
assert.equal(result.files[0].id, 'recent'); assert.equal(result.files[0].existing_duplicates, 1);
assert.equal(h.files.length, 2); assert.equal(h.files[0].content, 'old');
for (const payload of [{files:[]}, {files:[four[0],four[0]]}, {files:[four[0],{filename:'bad',content:'x'}]}, {files:[file('horse_history','')]}]) {
  h = harness(); assert.equal(h.post(payload).success, false); assert.equal(h.calls.length, 0);
}
h = harness([{id:'doc',name:four[1].filename,mime:'application/vnd.google-apps.document'}]);
assert.equal(h.post({files:four}).error, 'NAME_CONFLICT'); assert.equal(h.calls.length, 0);
h = harness([], 3); result = h.post({files:four});
assert.equal(result.success, false); assert.equal(result.files.length, 2); assert.equal(h.unlocks(),1);
assert.ok(!JSON.stringify(result).includes('private failure'));
h.failOff(); assert.equal(h.post({files:four}).success,true); assert.equal(h.files.length,4);
// Updates use media blobs, including content beyond File.setContent's 10 MB limit.
h = harness([{id:'large',name:four[2].filename}]);
assert.equal(h.post({files:[file('keiba_combinations','あ'.repeat(4*1024*1024))]}).success,true);
assert.equal(h.files.length,1); assert.ok(Buffer.byteLength(h.files[0].content,'utf8')>10*1024*1024);
console.log('Passed: repeat-save IDs/count, BOM, date separation, legacy payload, existing duplicates, validation, name conflict, partial retry, lock release, large UTF-8 media update.');

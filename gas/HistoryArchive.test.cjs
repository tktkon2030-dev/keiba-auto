const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
function iterator(items) {
  let i = 0;
  return {hasNext: () => i < items.length, next: () => items[i++],
    getContinuationToken: () => JSON.stringify(items.slice(i).map(x => x.getId()))};
}
const files = new Map();
let counter = 0;
class Folder {
  constructor(name) {this.name = name; this.id = 'folder-' + (++counter); this.folders=[]; this.files=[];}
  getName(){return this.name;}
  getId(){return this.id;}
  getFoldersByName(name){return iterator(this.folders.filter(f=>f.name===name));}
  getFolders(){return iterator(this.folders);}
  createFolder(name){const f=new Folder(name);this.folders.push(f);return f;}
  getFiles(){return iterator(this.files);}
  getFilesByName(name){return iterator(this.files.filter(f=>f.name===name));}
  createFile(blob){
    const file={name:blob.name,content:blob.content,id:'file-'+(++counter),
      getName(){return this.name;},getId(){return this.id;},isTrashed(){return false;},
      getBlob(){return {getDataAsString:()=>this.content};},getParents:()=>iterator([this])};
    this.files.push(file);files.set(file.id,file);return file;
  }
}
const root=new Folder('CSV');
const scope={
  LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
  PropertiesService:{getScriptProperties:()=>({getProperty:k=>({KEIBA_FOLDER_ID:'root',KEIBA_ARCHIVE_TOKEN:'private'}[k])})},
  DriveApp:{getFolderById:id=>{assert.equal(id,'root');return root;},getFileById:id=>files.get(id),
    continueFileIterator:token=>iterator(JSON.parse(token).map(id=>files.get(id)))},
  Utilities:{DigestAlgorithm:{SHA_256:'sha256'},Charset:{UTF_8:'utf8'},
    computeDigest:(_,s)=>Array.from(crypto.createHash('sha256').update(s,'utf8').digest()),
    newBlob:(content,type,name)=>({content,type,name})},
  ContentService:{MimeType:{JSON:'json'},createTextOutput:text=>({setMimeType:()=>text})}
};
vm.createContext(scope);
vm.runInContext(fs.readFileSync(__dirname+'/HistoryArchive.gs','utf8'),scope);
const call=p=>JSON.parse(scope.keibaHistoryResponse_({archive_token:'private',...p}));
const text='{"kind":"keiba_results_v1","results":{},"日本語":"予測"}\n';
const hash=crypto.createHash('sha256').update(text).digest('hex');
const put={action:'history_put',month:'2026-11',archive_content:text,
  archive_sha256:hash,archive_filename:'history-'+hash+'.json'};
assert.equal(call({...put,archive_token:'wrong'}).success,false);
assert.equal(root.folders.length,0,'unauthorized call must not create folders');
const first=call(put),second=call(put);
assert.equal(first.success,true);assert.equal(first.created,true);
assert.equal(second.created,false);assert.equal(first.id,second.id);
assert.deepEqual(call({action:'history_months'}).months,['2026-11']);
const list=call({action:'history_list',month:'2026-11'});
assert.equal(list.files.length,1);
assert.equal(call({action:'history_get',month:'2026-11',file_id:first.id}).content,text);
assert.equal(call({action:'history_get',month:'2026-10',file_id:first.id}).success,false);
assert.equal(call({...put,archive_sha256:'incorrect'}).success,false);
assert.equal(root.folders[0].folders[0].files.length,1);
const month=root.folders[0].folders[0];
for(let n=0;n<101;n++)month.createFile({name:'history-'+String(n).padStart(64,'0')+'.json',content:'{}'});
const page=call({action:'history_list',month:'2026-11'});
assert.equal(page.files.length,100);assert.ok(page.next_cursor);
assert.equal(call({action:'history_list',month:'2026-11',cursor:page.next_cursor}).files.length,2);
console.log('GAS archive: auth, UTF-8 hash, immutable retry, folder scope, corruption, pagination passed');

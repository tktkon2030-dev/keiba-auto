/** Add this file beside the existing CSV receiver; see HISTORY_SETUP.md. */
function keibaHistoryResponse_(payload) {
  var lock = LockService.getScriptLock();
  var locked = false;
  try {
    var props = PropertiesService.getScriptProperties();
    var expected = props.getProperty('KEIBA_ARCHIVE_TOKEN');
    if (!expected || !payload || payload.archive_token !== expected) throw new Error('DENIED');
    locked = lock.tryLock(30000);
    if (!locked) throw new Error('BUSY');
    var reply = keibaHistoryAction_(payload, props);
    reply.success = true;
    reply.action = payload.action;
    reply.archive_protocol = 1;
    return ContentService.createTextOutput(JSON.stringify(reply))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (error) {
    return ContentService.createTextOutput(JSON.stringify({success: false,
      action: payload && payload.action, archive_protocol: 1, error: 'ARCHIVE_FAILED'}))
      .setMimeType(ContentService.MimeType.JSON);
  } finally {
    if (locked) lock.releaseLock();
  }
}

function keibaHistoryHash_(content) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, content,
    Utilities.Charset.UTF_8).map(function(v) {
      return ('0' + ((v + 256) % 256).toString(16)).slice(-2);
    }).join('');
}

function keibaHistoryFolder_(parent, name, create) {
  var folders = parent.getFoldersByName(name);
  if (folders.hasNext()) {
    var found = folders.next();
    if (folders.hasNext()) throw new Error('DUPLICATE_FOLDER');
    return found;
  }
  return create ? parent.createFolder(name) : null;
}

function keibaHistoryAction_(payload, props) {
  var rootId = props.getProperty('KEIBA_FOLDER_ID');
  if (!rootId) throw new Error('FOLDER_NOT_CONFIGURED');
  var root = DriveApp.getFolderById(rootId);
  var create = payload.action === 'history_put';
  var history = keibaHistoryFolder_(root, '学習履歴', create);
  if (payload.action === 'history_months') {
    var months = [], folders = history ? history.getFolders() : null;
    while (folders && folders.hasNext()) {
      var name = folders.next().getName();
      if (/^\d{4}-\d{2}$/.test(name)) months.push(name);
    }
    return {months: months.sort()};
  }
  if (!/^\d{4}-\d{2}$/.test(payload.month || '')) throw new Error('INVALID_MONTH');
  var month = history ? keibaHistoryFolder_(history, payload.month, create) : null;
  if (payload.action === 'history_list') {
    if (!month) return {files: [], next_cursor: ''};
    var files = payload.cursor ? DriveApp.continueFileIterator(payload.cursor) : month.getFiles();
    var items = [], scanned = 0;
    while (files.hasNext() && scanned < 100) {
      var file = files.next();
      scanned++;
      if (file.isTrashed() || !/^history-[0-9a-f]{64}\.json$/.test(file.getName())) continue;
      // Continuation tokens are opaque; do not trust one to define folder scope.
      if (!keibaHistoryHasParent_(file, month.getId())) throw new Error('INVALID_PARENT');
      items.push({id: file.getId(), filename: file.getName()});
    }
    return {files: items, next_cursor: files.hasNext() ? files.getContinuationToken() : ''};
  }
  if (!month) throw new Error('MISSING_MONTH');
  if (payload.action === 'history_get') {
    var item = DriveApp.getFileById(String(payload.file_id || ''));
    if (item.isTrashed() || !keibaHistoryHasParent_(item, month.getId()) ||
        !/^history-[0-9a-f]{64}\.json$/.test(item.getName())) throw new Error('INVALID_FILE');
    var text = item.getBlob().getDataAsString('UTF-8');
    return {filename: item.getName(), content: text, sha256: keibaHistoryHash_(text)};
  }
  if (payload.action !== 'history_put') throw new Error('INVALID_ACTION');
  var content = payload.archive_content;
  if (typeof content !== 'string' || content.length > 10000000) throw new Error('INVALID_CONTENT');
  var value = JSON.parse(content);
  if (['keiba_pre_race_snapshots_v1', 'keiba_results_v1'].indexOf(value.kind) < 0)
    throw new Error('INVALID_KIND');
  var checksum = keibaHistoryHash_(content);
  var filename = 'history-' + checksum + '.json';
  if (payload.archive_sha256 !== checksum || payload.archive_filename !== filename)
    throw new Error('HASH_MISMATCH');
  var matches = month.getFilesByName(filename);
  if (matches.hasNext()) {
    var existing = matches.next();
    if (matches.hasNext() || existing.isTrashed() ||
        existing.getBlob().getDataAsString('UTF-8') !== content) throw new Error('NAME_CONFLICT');
    return {filename: filename, sha256: checksum, id: existing.getId(), created: false};
  }
  var saved = month.createFile(Utilities.newBlob(content, 'application/json', filename));
  return {filename: filename, sha256: checksum, id: saved.getId(), created: true};
}

function keibaHistoryHasParent_(file, folderId) {
  var parents = file.getParents();
  while (parents.hasNext()) if (parents.next().getId() === folderId) return true;
  return false;
}

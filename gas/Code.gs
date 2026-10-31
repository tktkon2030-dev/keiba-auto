/** Replace the existing CSV receiver. Requires the Advanced Drive service v3. */
function doPost(e) {
  const lock = LockService.getScriptLock();
  let locked = false;
  const saved = [];
  try {
    const files = validateKeibaFiles_(JSON.parse(e.postData.contents));
    const folderId = PropertiesService.getScriptProperties().getProperty('KEIBA_FOLDER_ID');
    if (!folderId) throw new Error('FOLDER_NOT_CONFIGURED');
    locked = lock.tryLock(30000);
    if (!locked) throw new Error('SAVE_BUSY');
    const folder = DriveApp.getFolderById(folderId);
    saveKeibaFiles_(files, folder, Drive, saved);
    return keibaJson_({success: true, files: saved});
  } catch (error) {
    // Never return exception bodies, file contents, credentials, or folder IDs.
    const known = ['INVALID_FILES', 'INVALID_FILE', 'DUPLICATE_FILENAME',
      'FOLDER_NOT_CONFIGURED', 'SAVE_BUSY', 'NAME_CONFLICT'];
    return keibaJson_({success: false,
      error: known.indexOf(error.message) >= 0 ? error.message : 'SAVE_FAILED',
      files: saved});
  } finally {
    if (locked) lock.releaseLock();
  }
}

function keibaJson_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function validateKeibaFiles_(payload) {
  const files = payload && Array.isArray(payload.files) ? payload.files : [payload];
  if (!files.length || files.length > 4) throw new Error('INVALID_FILES');
  const names = new Set();
  files.forEach(function(file) {
    if (!file || typeof file.filename !== 'string' ||
        !/^(keiba_integrated|horse_history|keiba_combinations|keiba_ticket_candidates)_\d{4}-\d{2}-\d{2}\.csv$/.test(file.filename) ||
        typeof file.content !== 'string' || !file.content.replace(/^\uFEFF/, '').trim()) {
      throw new Error('INVALID_FILE');
    }
    if (names.has(file.filename)) throw new Error('DUPLICATE_FILENAME');
    names.add(file.filename);
  });
  return files;
}

function saveKeibaFiles_(files, folder, drive, saved) {
  // Resolve every target before changing any file. Existing duplicate files stay intact.
  const targets = files.map(function(file) {
    const iterator = folder.getFilesByName(file.filename);
    const matches = [];
    while (iterator.hasNext()) {
      const candidate = iterator.next();
      if (candidate.isTrashed()) continue;
      if (['text/csv', 'text/plain', 'application/csv', 'application/octet-stream']
          .indexOf(candidate.getMimeType()) < 0) throw new Error('NAME_CONFLICT');
      matches.push(candidate);
    }
    matches.sort(function(a, b) {
      const delta = b.getLastUpdated().getTime() - a.getLastUpdated().getTime();
      return delta || a.getId().localeCompare(b.getId());
    });
    return {file: file, existing: matches[0], duplicates: Math.max(0, matches.length - 1)};
  });
  targets.forEach(function(target) {
    const file = target.file;
    // A UTF-8 BOM keeps Japanese CSVs readable in Excel. Avoid duplicate BOMs.
    const content = '\uFEFF' + file.content.replace(/^\uFEFF/, '');
    const blob = Utilities.newBlob(content, 'text/csv', file.filename);
    const options = {fields: 'id,name', supportsAllDrives: true};
    let result;
    if (target.existing) {
      // File.setContent has a 10 MB limit. Drive media updates preserve the ID for larger CSVs.
      result = drive.Files.update({mimeType: 'text/csv'}, target.existing.getId(), blob, options);
    } else {
      result = drive.Files.create({name: file.filename, mimeType: 'text/csv',
        parents: [folder.getId()]}, blob, options);
    }
    saved.push({filename: file.filename, id: result.id,
      action: target.existing ? 'updated' : 'created', existing_duplicates: target.duplicates});
  });
}

/** Run once in the editor to check configuration and grant the required access. */
function checkKeibaSetup() {
  const folderId = PropertiesService.getScriptProperties().getProperty('KEIBA_FOLDER_ID');
  if (!folderId) throw new Error('Set KEIBA_FOLDER_ID in Script properties first.');
  DriveApp.getFolderById(folderId).getName();
  Drive.Files.get(folderId, {fields: 'id', supportsAllDrives: true});
  console.log('保存先フォルダとDrive APIの設定を確認しました。');
}

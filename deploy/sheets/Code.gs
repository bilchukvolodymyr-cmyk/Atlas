/**
 * Atlas -> Google Sheet receiver. Paste into the sheet: Extensions > Apps Script.
 * Deploy > New deployment > Web app, Execute as: Me, Who has access: Anyone.
 *
 * The Mac sends {token, sheets: {"<tab>": {header: [...], rows: [[key, ...], ...]}}}.
 * Column A is the key: a row with a known key is overwritten, an unknown key is appended.
 * Sending the same data twice never creates duplicates. Nothing is ever deleted.
 */
var TOKEN = 'PUT_TOKEN_HERE';

function doPost(e) {
  var out;
  try {
    var data = JSON.parse(e.postData.contents);
    if (!data || data.token !== TOKEN) {
      out = {ok: false, error: 'bad token'};
    } else {
      out = upsertAll_(SpreadsheetApp.getActiveSpreadsheet(), data.sheets || {});
    }
  } catch (err) {
    out = {ok: false, error: String(err)};
  }
  return ContentService.createTextOutput(JSON.stringify(out))
      .setMimeType(ContentService.MimeType.JSON);
}

function doGet() {
  return ContentService.createTextOutput(JSON.stringify({ok: true, service: 'atlas-sheet'}))
      .setMimeType(ContentService.MimeType.JSON);
}

function upsertAll_(ss, sheets) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var updated = 0, appended = 0;
    Object.keys(sheets).forEach(function (name) {
      var r = upsert_(ss, name, sheets[name].header || [], sheets[name].rows || []);
      updated += r.updated;
      appended += r.appended;
    });
    SpreadsheetApp.flush();
    return {ok: true, updated: updated, appended: appended};
  } finally {
    lock.releaseLock();
  }
}

function upsert_(ss, name, header, rows) {
  var sh = ss.getSheetByName(name) || ss.insertSheet(name);
  var width = header.length;
  if (width) {
    sh.getRange(1, 1, 1, width).setValues([header]).setFontWeight('bold').setWrap(true);
    if (sh.getFrozenRows() < 1) sh.setFrozenRows(1);
  }
  sh.getRange('A:A').setNumberFormat('@');   // keys stay text: '2026-07-01' must not become a date
  var last = sh.getLastRow();
  var index = {};
  if (last >= 2) {
    var keys = sh.getRange(2, 1, last - 1, 1).getDisplayValues();
    for (var i = 0; i < keys.length; i++) {
      if (keys[i][0] !== '' && !(keys[i][0] in index)) index[keys[i][0]] = i + 2;
    }
  }
  var updated = 0, appended = 0, fresh = [], freshAt = {};
  rows.forEach(function (row) {
    row = row.slice(0, width || row.length);
    while (width && row.length < width) row.push('');
    row[0] = String(row[0]);
    var key = row[0];
    if (index[key] === -1) {
      fresh[freshAt[key]] = row;   // same key twice in one request: the last one wins
    } else if (key in index) {
      sh.getRange(index[key], 1, 1, row.length).setValues([row]);
      updated++;
    } else {
      freshAt[key] = fresh.push(row) - 1;
      index[key] = -1;
    }
  });
  if (fresh.length) {
    var start = Math.max(sh.getLastRow(), 1) + 1;
    sh.getRange(start, 1, fresh.length, fresh[0].length).setValues(fresh);
    appended = fresh.length;
  }
  return {updated: updated, appended: appended};
}

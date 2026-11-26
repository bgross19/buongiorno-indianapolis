/**
 * Google Apps Script cloud dispatcher for Buongiorno, Indianapolis!
 *
 * Watches the podcast Google Drive folder for newly generated YYYY-MM-DD.json
 * episode scripts (or edits to existing ones) and immediately dispatches the
 * GitHub Actions `publish.yml` workflow via `workflow_dispatch`, bypassing
 * multi-hour GitHub cron scheduler delays.
 *
 * Setup: store `GITHUB_TOKEN` in Script Properties (see apps-script/README.md)
 * and run `setupTrigger()` once.
 */

const CONFIG = {
  OWNER: 'bgross19',
  REPO: 'buongiorno-indianapolis',
  WORKFLOW_FILE: 'publish.yml',
  BRANCH: 'main',
  DRIVE_FOLDER_ID: '1glOqWl8hwgyz7BrwTI6-ZF7qdRzLhR_j',
  TIMEZONE: 'America/Indiana/Indianapolis',
  COOLDOWN_MINUTES: 10,
  MAX_DAILY_RETRIES: 6,
};

const SCRIPT_PROP_KEYS = {
  GITHUB_TOKEN: 'GITHUB_TOKEN',
  LAST_DISPATCH_MS: 'LAST_DISPATCH_MS',
  LAST_DRIVE_SIGNATURE: 'LAST_DRIVE_SIGNATURE',
  RETRY_STATE_JSON: 'RETRY_STATE_JSON',
};

/**
 * Read the GitHub fine-grained PAT from Script Properties.
 * @return {string}
 */
function getGitHubToken_() {
  const token = (
    PropertiesService.getScriptProperties().getProperty(SCRIPT_PROP_KEYS.GITHUB_TOKEN) || ''
  ).trim();
  if (!token) {
    throw new Error(
      'Missing GITHUB_TOKEN in Script Properties. Add it under Project Settings -> Script Properties.'
    );
  }
  return token;
}

/**
 * Build standard GitHub REST API request headers.
 * @param {string} token
 * @return {Object<string, string>}
 */
function buildGitHubHeaders_(token) {
  return {
    Authorization: 'Bearer ' + token,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'buongiorno-indianapolis-apps-script',
  };
}

/**
 * List non-trashed YYYY-MM-DD.json episode scripts in Google Drive.
 * Keeps the newest modifiedMs if duplicate filenames exist for the same date.
 * @return {Array<{date: string, modifiedMs: number}>}
 */
function listDriveEpisodeScripts_() {
  const folder = DriveApp.getFolderById(CONFIG.DRIVE_FOLDER_ID);
  const files = folder.getFiles();
  const byDate = {};
  const jsonDatePattern = /^(\d{4}-\d{2}-\d{2})\.json$/i;

  while (files.hasNext()) {
    const file = files.next();
    if (file.isTrashed()) {
      continue;
    }
    const match = jsonDatePattern.exec(file.getName().trim());
    if (!match) {
      continue;
    }
    const date = match[1];
    const modifiedMs = file.getLastUpdated().getTime();
    if (!byDate[date] || modifiedMs > byDate[date]) {
      byDate[date] = modifiedMs;
    }
  }

  return Object.keys(byDate)
    .sort()
    .map(function (date) {
      return { date: date, modifiedMs: byDate[date] };
    });
}

/**
 * Fetch the set of episode dates (YYYY-MM-DD) already published as MP3s in GitHub.
 * @param {string} token
 * @return {Object<string, boolean>}
 */
function listPublishedMp3Dates_(token) {
  const url =
    'https://api.github.com/repos/' +
    encodeURIComponent(CONFIG.OWNER) +
    '/' +
    encodeURIComponent(CONFIG.REPO) +
    '/contents/episodes?ref=' +
    encodeURIComponent(CONFIG.BRANCH);

  const response = UrlFetchApp.fetch(url, {
    method: 'get',
    headers: buildGitHubHeaders_(token),
    muteHttpExceptions: true,
  });

  const code = response.getResponseCode();
  if (code !== 200) {
    throw new Error(
      'GitHub contents API returned HTTP ' + code + ': ' + response.getContentText().slice(0, 300)
    );
  }

  const entries = JSON.parse(response.getContentText());
  if (!Array.isArray(entries)) {
    throw new Error('Unexpected GitHub contents API payload: expected array.');
  }

  const published = {};
  const mp3DatePattern = /^(\d{4}-\d{2}-\d{2})\.mp3$/i;
  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    if (!entry || entry.type !== 'file' || typeof entry.name !== 'string') {
      continue;
    }
    const match = mp3DatePattern.exec(entry.name.trim());
    if (match) {
      published[match[1]] = true;
    }
  }
  return published;
}

/**
 * Trigger the GitHub Actions workflow via workflow_dispatch.
 * @param {string} token
 */
function triggerWorkflowDispatch_(token) {
  const url =
    'https://api.github.com/repos/' +
    encodeURIComponent(CONFIG.OWNER) +
    '/' +
    encodeURIComponent(CONFIG.REPO) +
    '/actions/workflows/' +
    encodeURIComponent(CONFIG.WORKFLOW_FILE) +
    '/dispatches';

  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: buildGitHubHeaders_(token),
    payload: JSON.stringify({ ref: CONFIG.BRANCH }),
    muteHttpExceptions: true,
  });

  const code = response.getResponseCode();
  if (code !== 204) {
    throw new Error(
      'GitHub workflow_dispatch failed with HTTP ' +
        code +
        ': ' +
        response.getContentText().slice(0, 300)
    );
  }
}

/**
 * Parse stored daily retry state, resetting whenever the local Indianapolis date changes.
 * @param {GoogleAppsScript.Properties.Properties} props
 * @param {string} todayKey
 * @return {{date: string, count: number}}
 */
function getDailyRetryState_(props, todayKey) {
  const raw = props.getProperty(SCRIPT_PROP_KEYS.RETRY_STATE_JSON);
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && parsed.date === todayKey && typeof parsed.count === 'number') {
        return parsed;
      }
    } catch (_err) {
      // Ignore corrupted state and reset for today.
    }
  }
  return { date: todayKey, count: 0 };
}

/**
 * Check Drive vs GitHub episodes and dispatch publish.yml when needed.
 */
function checkAndDispatchPodcast() {
  const token = getGitHubToken_();
  const driveEpisodes = listDriveEpisodeScripts_();
  const publishedDates = listPublishedMp3Dates_(token);

  const missingDates = [];
  const signatureParts = [];
  for (let i = 0; i < driveEpisodes.length; i++) {
    const ep = driveEpisodes[i];
    signatureParts.push(ep.date + ':' + ep.modifiedMs);
    if (!publishedDates[ep.date]) {
      missingDates.push(ep.date);
    }
  }
  const driveSignature = signatureParts.join(',');

  const props = PropertiesService.getScriptProperties();
  const nowMs = Date.now();
  const lastDispatchMs = Number(props.getProperty(SCRIPT_PROP_KEYS.LAST_DISPATCH_MS) || '0');
  const lastSignature = props.getProperty(SCRIPT_PROP_KEYS.LAST_DRIVE_SIGNATURE) || '';
  const cooldownMs = CONFIG.COOLDOWN_MINUTES * 60 * 1000;
  const inCooldown = lastDispatchMs > 0 && nowMs - lastDispatchMs < cooldownMs;
  const todayKey = Utilities.formatDate(new Date(nowMs), CONFIG.TIMEZONE, 'yyyy-MM-dd');

  if (missingDates.length > 0) {
    if (inCooldown) {
      Logger.log(
        'Missing episode(s) [' +
          missingDates.join(', ') +
          '], but within ' +
          CONFIG.COOLDOWN_MINUTES +
          '-minute cooldown. Skipping dispatch.'
      );
      return;
    }

    const retryState = getDailyRetryState_(props, todayKey);
    if (retryState.count >= CONFIG.MAX_DAILY_RETRIES) {
      Logger.log(
        'Warning: reached MAX_DAILY_RETRIES (' +
          CONFIG.MAX_DAILY_RETRIES +
          ') for ' +
          todayKey +
          ' with missing episode(s) [' +
          missingDates.join(', ') +
          ']. Skipping dispatch.'
      );
      return;
    }

    triggerWorkflowDispatch_(token);
    props.setProperties({
      [SCRIPT_PROP_KEYS.LAST_DISPATCH_MS]: String(nowMs),
      [SCRIPT_PROP_KEYS.LAST_DRIVE_SIGNATURE]: driveSignature,
      [SCRIPT_PROP_KEYS.RETRY_STATE_JSON]: JSON.stringify({
        date: todayKey,
        count: retryState.count + 1,
      }),
    });
    Logger.log('Dispatched ' + CONFIG.WORKFLOW_FILE + ' for missing episode(s): ' + missingDates.join(', '));
    return;
  }

  if (lastSignature && driveSignature !== lastSignature) {
    if (inCooldown) {
      Logger.log(
        'Drive script metadata changed, but within ' +
          CONFIG.COOLDOWN_MINUTES +
          '-minute cooldown. Skipping dispatch.'
      );
      return;
    }

    triggerWorkflowDispatch_(token);
    props.setProperties({
      [SCRIPT_PROP_KEYS.LAST_DISPATCH_MS]: String(nowMs),
      [SCRIPT_PROP_KEYS.LAST_DRIVE_SIGNATURE]: driveSignature,
    });
    Logger.log('Dispatched ' + CONFIG.WORKFLOW_FILE + ' after Drive script update.');
    return;
  }

  if (!lastSignature) {
    props.setProperty(SCRIPT_PROP_KEYS.LAST_DRIVE_SIGNATURE, driveSignature);
  }
  Logger.log('Up to date: all ' + driveEpisodes.length + ' Drive episode(s) are already published.');
}

/**
 * Install (or reset) the 15-minute cloud trigger and run an initial verification check.
 */
function setupTrigger() {
  const existing = ScriptApp.getProjectTriggers();
  for (let i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'checkAndDispatchPodcast') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  ScriptApp.newTrigger('checkAndDispatchPodcast')
    .timeBased()
    .everyMinutes(15)
    .create();

  Logger.log('Installed 15-minute trigger for checkAndDispatchPodcast(). Running initial check...');
  checkAndDispatchPodcast();
}

/**
 * Manually trigger publish.yml immediately to verify end-to-end GitHub dispatch.
 */
function testDispatchNow() {
  const token = getGitHubToken_();
  triggerWorkflowDispatch_(token);
  Logger.log('Test workflow_dispatch succeeded for ' + CONFIG.OWNER + '/' + CONFIG.REPO + '.');
}

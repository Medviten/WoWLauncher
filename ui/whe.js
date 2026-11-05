// WHE: first-run setup, saved game login, and hands-off install/update. Kept apart from the Esteria
// scripts so upstream launcher changes merge cleanly.

const SYNC_RECHECK_MS = 2 * 60 * 1000;
const SYNC_RETRY_MS = 30 * 1000;
const SYNC_PERIODIC_MS = 15 * 60 * 1000;
let lastSyncAt = 0;
let lastSyncFailed = false;

// Verifies against the manifest, then installs or updates without asking.
async function syncClient() {
  if (appState.updateRunning || !appState.settings.gamePath) return;
  const result = await verifyInstallation({ silent: true });
  lastSyncAt = Date.now();
  lastSyncFailed = !result;
  if (!result) {
    // The server is out of reach, which must not stop someone playing an installed client.
    const info = await invokeTauri('launcher_diagnostics', { settings: appState.settings }).catch(() => null);
    appState.clientInstalled = Boolean(info?.wowExists);
    appState.needsRepair = false;
    updatePlayState();
    return;
  }
  if (appState.gameRunning) return;
  if (appState.needsRepair || !appState.clientInstalled) await repairInstallation();
  await installRequiredAddons({ silent: true });
  await autoUpdateManagedAddons({ silent: true });
}

async function syncIfStale() {
  if (Date.now() - lastSyncAt > SYNC_RECHECK_MS) await syncClient();
}

function startBackgroundSync() {
  setInterval(() => {
    if (!appState.settings.setupComplete || appState.updateRunning || appState.gameRunning || appState.downloadPaused) return;
    const wait = lastSyncFailed ? SYNC_RETRY_MS : SYNC_PERIODIC_MS;
    if (Date.now() - lastSyncAt >= wait) {
      syncClient();
      refreshRealmStatus();
    }
  }, SYNC_RETRY_MS);
}

async function prepareSetupScreen() {
  if (appState.settings.setupComplete) return;
  const suggested = appState.settings.gamePath || await invokeTauri('default_install_path');
  $('#setupPathInput').value = suggested || '';
}

$('#setupBrowseButton').addEventListener('click', async () => {
  const folder = await invokeTauri('choose_install_directory');
  if (folder) $('#setupPathInput').value = folder;
});

async function completeSetup() {
  const error = $('#authError');
  const gamePath = $('#setupPathInput').value.trim();
  const account = $('#accountUsernameInput').value.trim();
  const password = $('#accountPasswordInput').value;
  error.hidden = true;
  if (!gamePath) {
    error.textContent = 'Choose an install folder.';
    error.hidden = false;
    return;
  }
  if (Boolean(account) !== Boolean(password)) {
    error.textContent = 'Enter both the account and the password, or leave both blank.';
    error.hidden = false;
    return;
  }
  try {
    if (account) await invokeTauri('save_game_login', { account, password });
    const next = { ...appState.settings, gamePath, autoLogin: Boolean(account), setupComplete: true };
    appState.settings = (await invokeTauri('save_settings', { settings: next })) || next;
  } catch (failure) {
    error.textContent = String(failure);
    error.hidden = false;
    return;
  }
  $('#accountPasswordInput').value = '';
  populateSettingsForm();
  syncAccountGate();
  updatePlayState();
  await syncClient();
}

async function populateLoginForm() {
  const saved = await invokeTauri('get_saved_login').catch(() => null);
  $('#loginAccountInput').value = saved?.account || '';
  $('#loginPasswordInput').value = '';
  $('#loginPasswordInput').placeholder = saved?.hasPassword ? 'Saved. Type to replace it.' : 'Not saved';
  $('#autoLoginInput').checked = Boolean(appState.settings.autoLogin);
}

// Called while the settings dialog saves. A blank password keeps the saved one; a blank account forgets it.
async function saveLoginFromSettings(nextSettings) {
  const account = $('#loginAccountInput').value.trim();
  const password = $('#loginPasswordInput').value;
  if (!account) {
    await invokeTauri('clear_game_login');
    nextSettings.autoLogin = false;
    return;
  }
  const saved = await invokeTauri('get_saved_login');
  if (password) {
    await invokeTauri('save_game_login', { account, password });
  } else if (saved?.account !== account) {
    throw new Error('Enter the password for this account.');
  }
  nextSettings.autoLogin = $('#autoLoginInput').checked;
}

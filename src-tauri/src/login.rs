//! Optional saved game login. Encrypted with Windows DPAPI, so only the same Windows user on the same
//! PC can read it. The client takes it through its -login and -password switches.

use serde::{Deserialize, Serialize};
use std::{fs, path::PathBuf};

const LOGIN_FILE: &str = "login.dat";

#[derive(Serialize, Deserialize)]
pub struct SavedLogin {
    pub account: String,
    pub password: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SavedLoginView {
    account: String,
    has_password: bool,
}

fn login_path() -> Result<PathBuf, String> {
    Ok(crate::app_data_dir()?.join(LOGIN_FILE))
}

pub fn load() -> Option<SavedLogin> {
    let encrypted = fs::read(login_path().ok()?).ok()?;
    let plain = dpapi::unprotect(&encrypted).ok()?;
    serde_json::from_slice(&plain).ok()
}

#[tauri::command]
pub fn get_saved_login() -> SavedLoginView {
    match load() {
        Some(saved) => SavedLoginView { account: saved.account, has_password: !saved.password.is_empty() },
        None => SavedLoginView { account: String::new(), has_password: false },
    }
}

#[tauri::command]
pub fn save_game_login(account: String, password: String) -> Result<(), String> {
    let account = account.trim().to_string();
    if account.is_empty() || password.is_empty() {
        return Err("Enter both the account and the password.".to_string());
    }
    let plain = serde_json::to_vec(&SavedLogin { account, password }).map_err(|error| error.to_string())?;
    let encrypted = dpapi::protect(&plain)?;
    let path = login_path()?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| format!("Could not create the settings folder: {error}"))?;
    }
    fs::write(path, encrypted).map_err(|error| format!("Could not save the login: {error}"))
}

#[tauri::command]
pub fn clear_game_login() -> Result<(), String> {
    match fs::remove_file(login_path()?) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!("Could not remove the saved login: {error}")),
    }
}

#[cfg(windows)]
mod dpapi {
    use std::ptr::{null, null_mut};
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Cryptography::{
        CryptProtectData, CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };

    fn input(data: &[u8]) -> CRYPT_INTEGER_BLOB {
        CRYPT_INTEGER_BLOB { cbData: data.len() as u32, pbData: data.as_ptr() as *mut u8 }
    }

    unsafe fn take(output: CRYPT_INTEGER_BLOB) -> Vec<u8> {
        let bytes = std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec();
        LocalFree(output.pbData as _);
        bytes
    }

    pub fn protect(data: &[u8]) -> Result<Vec<u8>, String> {
        let source = input(data);
        let mut output = CRYPT_INTEGER_BLOB { cbData: 0, pbData: null_mut() };
        let done = unsafe {
            CryptProtectData(&source, null(), null(), null(), null(), CRYPTPROTECT_UI_FORBIDDEN, &mut output)
        };
        if done == 0 {
            return Err("Windows could not encrypt the login.".to_string());
        }
        Ok(unsafe { take(output) })
    }

    pub fn unprotect(data: &[u8]) -> Result<Vec<u8>, String> {
        let source = input(data);
        let mut output = CRYPT_INTEGER_BLOB { cbData: 0, pbData: null_mut() };
        let done = unsafe {
            CryptUnprotectData(&source, null_mut(), null(), null(), null(), CRYPTPROTECT_UI_FORBIDDEN, &mut output)
        };
        if done == 0 {
            return Err("Windows could not decrypt the login.".to_string());
        }
        Ok(unsafe { take(output) })
    }
}

#[cfg(not(windows))]
mod dpapi {
    pub fn protect(_data: &[u8]) -> Result<Vec<u8>, String> {
        Err("Saved logins need Windows.".to_string())
    }

    pub fn unprotect(_data: &[u8]) -> Result<Vec<u8>, String> {
        Err("Saved logins need Windows.".to_string())
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::dpapi;

    #[test]
    fn dpapi_round_trip() {
        let encrypted = dpapi::protect(b"secret").unwrap();
        assert!(!encrypted.windows(6).any(|part| part == b"secret"));
        assert_eq!(dpapi::unprotect(&encrypted).unwrap(), b"secret");
    }
}

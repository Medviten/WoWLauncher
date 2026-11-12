//! Linux (and SteamOS) support. The client is a Windows program, so Play runs it through Wine or Proton.
//! Nothing here is compiled into the Windows build.

use std::env;
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};

/// Variables an AppImage sets for its own bundled GTK libraries. Passed on, they can break Wine, Proton (a
/// Python script) and xdg-open, so programs the launcher starts get the user's environment back.
const APPIMAGE_VARS: &[&str] = &[
    "LD_LIBRARY_PATH",
    "GDK_PIXBUF_MODULE_FILE",
    "GDK_PIXBUF_MODULEDIR",
    "GIO_MODULE_DIR",
    "GIO_EXTRA_MODULES",
    "GTK_PATH",
    "GTK_EXE_PREFIX",
    "GTK_DATA_PREFIX",
    "GTK_IM_MODULE_FILE",
    "GSETTINGS_SCHEMA_DIR",
    "GI_TYPELIB_PATH",
    "PYTHONHOME",
    "PYTHONPATH",
];

pub fn host_command(program: impl AsRef<OsStr>) -> Command {
    let mut command = Command::new(program);
    if env::var_os("APPIMAGE").is_some() {
        for name in APPIMAGE_VARS {
            command.env_remove(name);
        }
    }
    command
}

pub fn xdg_open(target: &OsStr) -> std::io::Result<Child> {
    host_command("xdg-open").arg(target).spawn()
}

/// WebKitGTK's DMA-BUF renderer shows a blank window on some drivers, mostly when run from an AppImage.
pub fn prepare_webview() {
    if env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
}

pub fn is_game_running() -> bool {
    host_command("pgrep")
        .args(["-f", "-i", r"WowClient64\.exe"])
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false)
}

fn home() -> Option<PathBuf> {
    env::var_os("HOME").map(PathBuf::from)
}

fn in_path(name: &str) -> Option<PathBuf> {
    env::split_paths(&env::var_os("PATH")?).map(|dir| dir.join(name)).find(|path| path.is_file())
}

/// Steam installs: native (SteamOS, most distributions) and Flatpak.
fn steam_roots() -> Vec<PathBuf> {
    let Some(home) = home() else { return Vec::new() };
    [".steam/steam", ".local/share/Steam", ".var/app/com.valvesoftware.Steam/data/Steam"]
        .iter()
        .map(|relative| home.join(relative))
        .filter(|path| path.is_dir())
        .collect()
}

/// Proton builds Steam has installed, newest name last; custom ones (GE-Proton) count too.
/// shortcut: only the main Steam library is searched, not extra libraries such as an SD card; set the path in
/// Settings for those.
fn find_proton() -> Option<PathBuf> {
    let mut found: Vec<PathBuf> = steam_roots()
        .iter()
        .flat_map(|root| [root.join("steamapps/common"), root.join("compatibilitytools.d")])
        .filter_map(|dir| fs::read_dir(dir).ok())
        .flatten()
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.path().join("proton"))
        .filter(|path| path.is_file())
        .collect();
    found.sort();
    found.pop()
}

fn is_proton(runner: &Path) -> bool {
    runner.file_name().is_some_and(|name| name == "proton")
}

/// The command that runs the client. `configured` is the Settings value: wine, umu-run, a Proton script, or
/// empty to pick wine from PATH, or else Steam's Proton.
pub fn game_command(executable: &Path, configured: &str) -> Result<Command, String> {
    let configured = configured.trim();
    let runner = if !configured.is_empty() {
        let path = PathBuf::from(configured);
        // A bare name such as "wine" or "umu-run" is looked up in PATH.
        if path.components().count() == 1 { in_path(configured).unwrap_or(path) } else { path }
    } else {
        in_path("wine").or_else(find_proton).ok_or_else(|| {
            "Wine was not found. Install Wine, or Proton through Steam, or set Wine or Proton in Settings.".to_string()
        })?
    };

    let mut command = host_command(&runner);
    if is_proton(&runner) {
        let prefix = crate::app_data_dir()?.join("proton-prefix");
        fs::create_dir_all(&prefix)
            .map_err(|error| format!("Could not create the Proton prefix {}: {error}", prefix.display()))?;
        let steam = steam_roots().into_iter().next().or_else(|| home().map(|home| home.join(".steam/steam")));
        command.arg("run");
        command.env("STEAM_COMPAT_DATA_PATH", prefix);
        if let Some(steam) = steam {
            command.env("STEAM_COMPAT_CLIENT_INSTALL_PATH", steam);
        }
    }
    command.arg(executable);
    Ok(command)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proton_runs_the_client_in_its_own_prefix() {
        let command = game_command(Path::new("/games/WHE/WowClient64.exe"), "/opt/Proton 9.0/proton").unwrap();
        let args: Vec<_> = command.get_args().collect();
        assert_eq!(args, ["run", "/games/WHE/WowClient64.exe"]);
        assert!(command.get_envs().any(|(name, _)| name == "STEAM_COMPAT_DATA_PATH"));
    }

    #[test]
    fn wine_gets_the_client_alone() {
        let command = game_command(Path::new("/games/WHE/WowClient64.exe"), "/usr/bin/wine").unwrap();
        assert_eq!(command.get_program(), "/usr/bin/wine");
        assert_eq!(command.get_args().collect::<Vec<_>>(), ["/games/WHE/WowClient64.exe"]);
    }
}

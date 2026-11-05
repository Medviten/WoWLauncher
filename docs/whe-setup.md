# WHE launcher setup

How this fork of the Esteria launcher is hosted and published. Everything runs over Tailscale, with no port forwarding.

```text
Your PC                          gai1gai1 (100.110.63.66, Tailscale only)        Tester's PC
whe_publish.py  -- rsync/ssh -->  nginx :7384  /srv/wow-cdn   <-- HTTP --  WHE Launcher
                                  auth + world server         <-- game --  WowClient64.exe
```

## 1. Make room on the server

The server's root partition has 17 GB free and the client needs about 25 GB, so the files go on the old Windows partition (`nvme0n1p3`, 232 GB). This avoids moving or resizing the Linux partition.

**This erases Windows on that partition. Copy anything you want to keep first.** Check the device name again with `lsblk` before you run it.

```bash
sudo mkfs.ext4 -L wow-cdn /dev/nvme0n1p3
sudo mkdir -p /srv/wow-cdn
echo 'LABEL=wow-cdn /srv/wow-cdn ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /srv/wow-cdn
df -h /srv/wow-cdn
```

Leave `nvme0n1p5` (`BIOS_RVY`) and `nvme0n1p4` (`WinRE tools`) alone. Afterwards `sudo update-grub` removes Windows from the boot menu.

## 2. Install the file server

Copy `tools/server/` to the server, then run:

```bash
sudo bash setup-cdn.sh
```

nginx listens only on the Tailscale address. `tailscale status` on the server should still show the testers' machines.

## 3. Publish the client

On your PC (Python, and WSL with `rsync` and your SSH key):

```powershell
python tools/whe_publish.py --dry-run   # what would ship
python tools/whe_publish.py             # build and upload
```

`tools/whe-publish.json` decides what ships. Run it again after every client or DBC change: only changed files and bundles are sent, and running launchers pick up the new version by themselves within 15 minutes, or when Play is pressed.

- `exclude` leaves out `Data/Retail/Cache`, which the client writes to and downloads from Blizzard's CDN when needed.
- `loose` keeps `DBFilesClient` as individual files, so DBC changes stay small downloads.
- `prune` lists folders the launcher owns. Files there that leave the manifest are **deleted** from testers' clients, so stale overrides cannot linger. Only folders under `Data/` are accepted.
- Old loose files stay on the server until you delete them. They do nothing, because only the manifest counts.

News, patch notes and add-ons are edited directly on the server, and publishing never overwrites them: `/srv/wow-cdn/news.json`, `patchnotes.md`, and `addons/`. See the README for their formats.

## 4. Build and publish the launcher

```powershell
$env:CARGO_TARGET_DIR = 'D:\wow-scratch\launcher-target'
$env:TAURI_SIGNING_PRIVATE_KEY = "$env:USERPROFILE\.tauri\whe.key"
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = ''
npx tauri build
```

The signing key is `%USERPROFILE%\.tauri\whe.key`. **Back it up.** Without it, installed launchers can never update themselves again and must be reinstalled by hand.

The installer is `D:\wow-scratch\launcher-target\release\bundle\nsis\WHE Launcher_<version>_x64-setup.exe`. To release an update, bump the version in `package.json`, `src-tauri/tauri.conf.json` and `src-tauri/Cargo.toml`, build, then:

```powershell
powershell -File tools/Publish-EsteriaLauncher.ps1 -BundleRoot 'D:\wow-scratch\launcher-target\release\bundle\nsis' -UpdatesRoot 'D:\wow-scratch\whe-cdn\launcher-updates' -Notes 'What changed'
wsl rsync -rt --chmod=D755,F644 /mnt/d/wow-scratch/whe-cdn/launcher-updates/ medviten@100.110.63.66:/srv/wow-cdn/launcher-updates/
```

## 5. Add a tester

1. Share `gai1gai1` with them in Tailscale, and have them install and sign in to Tailscale.
2. Create their game account on the server.
3. Send them `http://100.110.63.66:7384/launcher-updates/WHE%20Launcher_<version>_x64-setup.exe`. Windows SmartScreen warns about it because it is unsigned: **More info**, then **Run anyway**.
4. They open the launcher, choose a folder, enter their account, and wait for the download. After that it is just Play.

The first game launch shows the EULA and Terms of Service once. After they accept, auto-login continues.

If the download is very slow, have them run `tailscale ping gai1gai1`. "via DERP" means a relayed connection, which is much slower than a direct one. Downloads resume after interruptions.

## What differs from upstream

- `WowClient64.exe` replaces `Wow.exe`.
- A first-run screen (`ui/whe.js`) replaces the sign-in wall, and installing and updating start without being asked.
- Saved logins are encrypted with Windows DPAPI (`src-tauri/src/login.rs`) and passed to the client as `-login`/`-password`. Other programs running on that PC can read a process's command line, so don't reuse an important password.
- Community, friends, armory, database, stats and the account page are hidden by `ui/whe.css`, because they need Esteria's closed-source social service or `mod-launcher-api`. Their code is still there, which keeps upstream merges simple.
- Manifests gain a `prune` list (`stale_files` in `main.rs`).

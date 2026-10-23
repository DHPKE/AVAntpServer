# Updating the AVAntpServer box

The box downloads new versions from GitHub (https://github.com/DHPKE/AVAntpServer, branch `main`).
Its firewall blocks every download by design, so an update needs **Update mode** for a few minutes.

## The easy way: the Software card (WebUI -> SYSTEM)

1. **CHECK FOR UPDATES** asks GitHub for the newest version and shows what is new.
2. **UPDATE NOW** opens web access just for the download, installs the newest version and closes web access again.
   You see the steps live. The WebUI restarts, the page reloads by itself, log in again.
   It installs nothing when you are already up to date. Do not use it while an APPLY waits for confirmation.

Everything below is the same thing done by hand, and the way back if you need it.
If an update fails, the Software card shows the reason; the details are in LOG -> source "Apply".

## By hand: every update (about 2 minutes)

1. **Check nothing is half done.** In the WebUI there must be no banner asking you to CONFIRM an apply.
2. **Open Update mode.** WebUI -> **SYSTEM** -> **ENTER UPDATE MODE**. It lasts 15 minutes and closes by itself.
3. **Log in to the box** from a computer on the management VLAN (VLAN 10):
   ```
   ssh node@10.1.10.1
   ```
4. **Run the update:**
   ```
   cd ~
   sudo /opt/avantp/deploy/update.sh
   ```
   Expected output: `code: <old> -> <new>`, then `installing version <new>` and `installed version <new>: chronyd ...`.
   The first time it also installs git and clones the repository into `~/AVAntpServer-src`.
5. **End Update mode.** WebUI -> SYSTEM -> **END UPDATE MODE** (optional, it closes by itself).
6. **Log in to the WebUI again** (the update restarts it) and check:
   - SYSTEM tab -> "Installed version" shows the new version.
   - MONITOR: SYNCED, the VLAN IP addresses are there, the timecode card shows your senders.
   - LOG tab (level "Warnings and errors"): nothing new and red.
7. **MIDI over RTP (macOS):** if your receiver shows the session as connected but nothing arrives,
   Audio MIDI Setup -> MIDI Network Setup -> select the participant -> Disconnect -> Connect.

What an update does and does not do: it replaces the program files and restarts the WebUI and the timecode sender
(Art-Net pauses for a second or two). It does **not** change your network, firewall or settings.
Do not start an update while an APPLY is waiting for confirmation.

## If something goes wrong

| What you see | Meaning | What to do |
|---|---|---|
| `Could not reach GitHub/Debian. Is Update mode ON?` | Update mode is off or expired | Enter Update mode again, run the command again. Nothing was changed. |
| `sudo: a password is required` | node asks for its own password | Type node's password. |
| New version misbehaves | | Go back, see below. |
| WebUI unreachable after the update | | `ssh node@10.1.10.1`, then `sudo systemctl status avantp-web`, `sudo journalctl -u avantp-web -n 40`. |

## If the repository is private again

The repository is public, so the box needs no login. If it is made private again, the box (which has no GitHub login) gets "GitHub answered 404" from
the Software card and a "repository not found" from `update.sh`. Two ways to give it access:

1. **Make the repository public** (GitHub -> Settings -> Danger zone -> Change visibility). Nothing else to do.
2. **Read-only deploy key** (recommended for a private repository): a key pair created on the box whose public half is
   added to the repository as a *deploy key without write access*. It can read this one repository and nothing else.
   The box then clones over SSH on port 443 (`ssh.github.com:443`), which the update window allows.

## Going back to an earlier version

```
cd ~/AVAntpServer-src
git log --oneline -10                  # pick the hash of the version you want
git checkout <hash>
sudo ./deploy/install.sh
```
The next normal update (button or script) returns to the newest version by itself.

## Where things are

| | |
|---|---|
| Installed program (what runs) | `/opt/avantp` (`/opt/avantp/VERSION`) |
| Source clone used for updates | `~/AVAntpServer-src` |
| Update script | `/opt/avantp/deploy/update.sh` (this file: `/opt/avantp/deploy/UPDATE.md`, after the next update) |
| Your settings | `/etc/avantp/config.json` (active), `/var/lib/avantp/` (users, certificate, saved changes) |
| Logs | WebUI -> LOG, or `sudo journalctl -u avantp-web -u avantp-apply -u avantp-timecode -n 100` |

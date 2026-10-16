# nas-media

**Every TV show in Plex or on the Plex Watchlist, and every movie on the Watchlist, downloads by itself on the NAS: new episodes arrive in Plex a few episodes ahead of what we watched, within the free disk space. Everything the user does happens in Plex (watch, Watchlist, mark watched, add to the list "Bad files", delete); no visits to Sonarr, Radarr or Prowlarr.**

## Rules

- **Code is edited here, on the Mac; the NAS runs the deployed copy** in `~/opt/nas-media/`. Deploy with the `install.sh` command below (the NAS's apt is broken, 2026-10-15: never `apt --fix-broken`, ask the user).
- **Settings of the apps are set by `tools/configure.py`**, not by hand in their web pages: re-run it after any change (idempotent).
- **Keep it simple** (the user, 2026-10-15): the keep-ahead job is one check in five steps (`src/keep_ahead.py`); Sonarr, Radarr and Decluttarr do the rest. Propose removals first.
- **Tracked = in Plex or on the Watchlist** (or with a bad file being replaced). The Watchlist means "keep following": a show deleted in Plex stays tracked while on it. A movie stops being wanted once downloaded.
- **The keep-ahead job deletes only bad files, never anything else**: when space is low it shows the warning playlist and stops new downloads; the user deletes in Plex. Never delete a show from Sonarr with its files (`deleteFiles=false`).
- **Languages:** TV in English or French, movies in their original language (`tools/configure.py`).
- **The media folder is one mount** (`MEDIA` in `nas.env`): Plex, Sonarr and Radarr see it as `/media`, qBittorrent as `/downloads`. Keep it one mount, or imports become copies.
- **This repo is public: nothing personal in it** (the user, 2026-10-16): no names, logins, home addresses, machine names or what we watch, in files, comments or commit messages. This home's values are in `~/.config/nas-media/nas.env` on the NAS (example: `nas.env.example`); `<nas>` = the NAS's ssh host (the user's global instructions name it). Commits use GitHub's no-reply email.
- **Secrets stay on the NAS** in `~/.config/nas-media/*.env` (`apps.env`: the apps' login; `qbit.env`: qBittorrent's), never printed or committed.

## Commands

| | |
|---|---|
| deploy | `git -C ~/dev/nas-media archive HEAD \| ssh <nas> 'T=$(mktemp -d) && tar -x -C "$T" && bash "$T/install.sh"; rm -rf "$T"'` |
| configure the apps | `ssh <nas> python3 -I - < ~/dev/nas-media/tools/configure.py` |
| one check, dry | `ssh <nas> 'KEEP_AHEAD_DRY=1 python3 -I ~/opt/nas-media/keep_ahead.py'` |

## Vocabulary

The only words for these concepts, in every file and when talking to the user. A new word is added here first, with the user's agreement.

| Word | Means | Never say |
|---|---|---|
| **tracked** (show) | in Sonarr: in Plex, on the Plex Watchlist, or with a bad file being replaced; the keep-ahead job manages its episodes, new seasons included | followed, subscribed |
| **keep-ahead job** | the check every 5 min (`src/keep_ahead.py`): tracked shows, the next 3 episodes after the last one watched or started (in this Plex, or marked watched on plex.tv), Watchlist movies, bad files, the warning playlist | catch-up job, window job |
| **wanted** (episode, movie) | monitored in Sonarr/Radarr: grabbed as soon as a site has it | monitored (Sonarr's own word, fine inside code) |
| **bad file** | a video file with holes, or added to the Plex list "Bad files" (not 1 star: a TV rating stays on plex.tv): deleted, downloaded again | corrupt, broken file |
| **holes** | runs of zeros in a file: torrent pieces never written (seen in a release itself, 2026-10-15) | gaps, zero blocks |
| **warning playlist** | the Plex playlist "⚠ NAS almost full: … GB left", on Plex Home while free space is low: what was watched, biggest first | alert, notification |
| **landing folder** | where qBittorrent saves: `media/torrents/tv`, `media/torrents/movies` (Sonarr/Radarr's downloads); `media/` itself for manual downloads | download folder |
| **site** | a torrent site in Prowlarr (YTS, Knaben, The Pirate Bay) | indexer (Prowlarr's word, fine inside code) |

## Where to look

| | |
|---|---|
| what runs where | [`README.html`](README.html) (open it in a browser) |
| the job's rules and numbers (3 ahead, 50 / 100 GB, holes, "Bad files") | `src/keep_ahead.py`, its first lines |
| an addition to a list in Plex → a check at once | `src/listen.py`, service `<NAME>.listen` |
| the job's memory (files checked, bad files being replaced, where each show is, plex.tv's watched state) | `~/opt/nas-media/state/memory.json` on the NAS |

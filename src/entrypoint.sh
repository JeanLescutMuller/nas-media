#!/bin/bash
# The job nas-media (the keep-ahead job), for job-runner (separate project, ~/dev/job-runner): every show in Plex or on the
# Plex Watchlist keeps 3 episodes downloaded ahead of the last one watched; Watchlist movies; bad files replaced; disk warning. Deployed by install.sh to
# ~/opt/nas-media/entrypoint.sh; the NAS's trigger ($NAME in ~/.config/nas-media/nas.env) runs it every 5 min: that is the whole schedule.
# Requirements:
#   - the NAS only (Sonarr, Plex and the disk are there)
#   - every 5 min (systemd --user timer OnCalendar *:00/5 UTC); the first check of each UTC day also searches everything missing
#   - no "already done" rule: every trigger works
#   - timeout 20 min (the first check reads every file once for holes); systemd never starts a running oneshot again: no claim
#   - summary: the line "N tracked shows · …"
. ~/opt/job-runner/lib.sh                # defaults: syncs on LOCAL (JR_SYNC_ENV), writes LOCAL + VM (JR_REPORT_ENVS)

jr_execute 20 python3 -I "$HOME/opt/nas-media/keep_ahead.py"
jr_summary_grep 'tracked shows'

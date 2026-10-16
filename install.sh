#!/bin/bash
# Install nas-media on the NAS, in ~/opt/nas-media, from this copy. Idempotent. Needs ~/.config/nas-media/nas.env (this home's
# settings: copy nas.env.example there and fill it in).
#   The apps of compose.yaml (pulled, started); the landing folders media/torrents/{tv,movies};
#   the keep-ahead job (keep_ahead.py, entrypoint.sh) and its trigger $NAME (nas.env), a systemd --user timer, every 5 min
#   (needs job-runner on the NAS: ~/opt/job-runner/lib.sh); the service $N.listen (listen.py, all the time): an addition to a list in Plex
#   starts that check at once.
# Usage, from the Mac (the NAS has no git): deploys the last commit (HEAD)
#   git -C ~/dev/nas-media archive HEAD | ssh <nas> 'T=$(mktemp -d) && tar -x -C "$T" && bash "$T/install.sh"; rm -rf "$T"'
#   (<nas>: the NAS's ssh host)
# Undo: bash uninstall.sh (same way)
set -eu
C=$HOME/.config/nas-media/nas.env
[ -f "$C" ] || { echo "nas-media: no $C: copy nas.env.example there and fill it in" >&2; exit 1; }
. "$C"
S=$(cd "$(dirname "$0")" && pwd) O=$HOME/opt/nas-media M=$MEDIA N=$NAME

# ---------- The apps ----------
mkdir -p "$O"/config/{prowlarr,sonarr,radarr} "$O/units" "$M"/torrents/{tv,movies}
cp "$S/compose.yaml" "$S/decluttarr.yaml" "$O/"
docker compose --env-file "$C" -f "$O/compose.yaml" pull -q
docker compose --env-file "$C" -f "$O/compose.yaml" up -d --remove-orphans
docker compose --env-file "$C" -f "$O/compose.yaml" ps --format '{{.Name}}  {{.Status}}'

# ---------- The keep-ahead job ----------
cp "$S/src/keep_ahead.py" "$O/keep_ahead.py"
cp "$S/src/entrypoint.sh" "$O/entrypoint.sh"
if [ ! -f ~/opt/job-runner/lib.sh ]; then
  echo "WARNING: job-runner is not installed on the NAS: no trigger for the keep-ahead job yet" >&2
else
  mkdir -p ~/.config/systemd/user
  printf "[Unit]\nDescription=nas-media keep-ahead job\n[Service]\nType=oneshot\nExecStart=/bin/bash %s/entrypoint.sh\n" "$O" > "$O/units/$N.service"
  printf "[Unit]\nDescription=nas-media keep-ahead job, every 5 min\n[Timer]\nOnCalendar=*-*-* *:00/5:00 UTC\nPersistent=true\n[Install]\nWantedBy=timers.target\n" > "$O/units/$N.timer"
  ln -sfn "$O/units/$N.service" ~/.config/systemd/user/$N.service
  ln -sfn "$O/units/$N.timer" ~/.config/systemd/user/$N.timer
  systemctl --user daemon-reload && systemctl --user enable --now $N.timer 2>&1 | grep -v Created || :
  loginctl enable-linger "$USER" 2>/dev/null || :
  echo "timer $N $(systemctl --user is-active $N.timer)"
  # ---------- The service: an addition to a list in Plex starts the check at once ----------
  cp "$S/src/listen.py" "$O/listen.py"
  printf "[Unit]\nDescription=nas-media: an addition to a list in Plex starts the keep-ahead check at once\n[Service]\nExecStart=/usr/bin/python3 -I %s/listen.py\nRestart=always\nRestartSec=10\n[Install]\nWantedBy=default.target\n" "$O" > "$O/units/$N.listen.service"
  ln -sfn "$O/units/$N.listen.service" ~/.config/systemd/user/$N.listen.service
  systemctl --user daemon-reload && systemctl --user enable $N.listen.service 2>&1 | grep -v Created || :
  systemctl --user restart $N.listen.service
  echo "service $N.listen $(systemctl --user is-active $N.listen.service)"
fi

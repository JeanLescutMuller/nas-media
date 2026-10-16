#!/bin/bash
# Remove nas-media from the NAS: the keep-ahead job's trigger and the list listener service, then the apps. Keeps their settings (~/opt/nas-media/config),
# the "already watched" marks (~/opt/nas-media/state) and every media file.
# Usage, from the Mac:
#   git -C ~/dev/nas-media archive HEAD | ssh <nas> 'T=$(mktemp -d) && tar -x -C "$T" && bash "$T/uninstall.sh"; rm -rf "$T"'
set -eu
C=$HOME/.config/nas-media/nas.env
. "$C"
O=$HOME/opt/nas-media N=$NAME
systemctl --user disable --now $N.timer $N.listen.service 2>/dev/null || :
rm -f ~/.config/systemd/user/$N.service ~/.config/systemd/user/$N.timer ~/.config/systemd/user/$N.listen.service
systemctl --user daemon-reload 2>/dev/null || :
[ -f "$O/compose.yaml" ] && docker compose --env-file "$C" -f "$O/compose.yaml" down
echo "trigger and apps removed; settings kept in $O/config"

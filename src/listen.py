# Runs all the time on the NAS (service <NAME>.listen, NAME in ~/.config/nas-media/nas.env); deployed to ~/opt/nas-media/listen.py.
# Listens to Plex's live event stream: when a Plex app (the TV's included) adds something to a list, the keep-ahead job's check
# starts at once, instead of within 5 min (the user, 2026-10-15: "a very smooth experience"). The check decides everything
# (in the list "Bad files" = bad file); this only starts it early. If this stops, the check every 5 min still does the work.
#   Seen 2026-10-16: the TV adding an episode to "Bad files" logs "Request: [<TV address>:<port> (Subnet)] PUT
#   /playlists/<id>/items?uri=…" on the NAS's Plex. (A rating given on the TV never reaches the NAS: it stays on plex.tv.)
# Reads:  Plex's event stream http://127.0.0.1:32400/:/eventsource/notifications (its own log lines among them),
#         ~/.config/nas-media/nas.env (NAME, PLEX_PREFS).
# Writes: nothing; starts <NAME>.service (the keep-ahead job).
import os, re, subprocess, threading, time, urllib.request

HOME = os.path.expanduser("~")
# this home's settings (nas.env.example): {"MEDIA": "/volume1/media", …}
d_site = {k: v.strip().strip('"') for k, v in (l.split("=", 1) for l in open(f"{HOME}/.config/nas-media/nas.env")
                                                if "=" in l and not l.lstrip().startswith("#"))}
PREFS = d_site["PLEX_PREFS"]
# an app adds to a list: PUT /playlists/<id>/items, or POST /playlists (a new list with its first item). Not the job's own
# requests (127.0.0.1): its warning playlist would start another check
ADDED = re.compile(r"Request: \[(?!127\.0\.0\.1)[^\]]*\] (PUT /playlists/\d+/items|POST /playlists\?)")
JOB = d_site["NAME"] + ".service"
wake = threading.Event()


def run_checks():
    # one check per burst of additions: one made while a check runs gets a check of its own right after
    while True:
        wake.wait()
        wake.clear()
        print("added to a list in Plex → check now", flush=True)
        subprocess.run(["systemctl", "--user", "start", JOB])     # waits for the check to end (Type=oneshot)


threading.Thread(target=run_checks, daemon=True).start()
while True:
    try:
        token = re.search(r'PlexOnlineToken="([^"]+)"', open(PREFS).read())[1]
        req = urllib.request.Request("http://127.0.0.1:32400/:/eventsource/notifications?filters=", headers={"X-Plex-Token": token})
        with urllib.request.urlopen(req, timeout=120) as r:      # Plex sends an empty event every ~10 s: a silent 120 s = gone
            for line in r:
                if b"/playlists" in line and ADDED.search(line.decode(errors="replace")):
                    wake.set()
    except Exception as e:
        print(f"Plex event stream lost ({e}); again in 10 s", flush=True)
        time.sleep(10)

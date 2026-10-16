# Configure the apps of nas-media through their own APIs. Run by hand, on the NAS, as often as needed (idempotent):
#   ssh <nas> python3 -I - < ~/dev/nas-media/tools/configure.py      (<nas>: the NAS's ssh host)
# Reads:  the apps' API keys (~/opt/nas-media/config/<app>/config.xml), Plex's token (its Preferences.xml),
#         ~/.config/nas-media/apps.env (the apps' login, created here once), ~/.config/nas-media/qbit.env (qBittorrent's login),
#         ~/.config/nas-media/nas.env (this home's settings: nas.env.example).
# Writes: the apps' settings only. Prints what it changed, never a secret.
import json, os, re, secrets, urllib.request, urllib.parse, http.cookiejar

HOME = os.path.expanduser("~")
# Prowlarr's definitionName of each torrent site. Not uindex: the NAS's network blocks it (TLS error, verified 2026-10-14).
# Not EZTV: behind a Cloudflare check (needed the Byparr container, 350 MB of RAM) and gave 1 of the first 20 downloads, a release
# Knaben listed too (measured 2026-10-15); removed below if present.
SITES = ["yts", "Knaben", "thepiratebay"]


def env_file(name, d_default=None):
    # KEY=value lines in ~/.config/nas-media/<name>; created (chmod 600) with d_default when missing
    path = f"{HOME}/.config/nas-media/{name}"
    if not os.path.exists(path):
        if d_default is None:
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write("".join(f"{k}={v}\n" for k, v in d_default.items()))
    return {k: v.strip().strip('"') for k, v in (l.split("=", 1) for l in open(path) if "=" in l and not l.lstrip().startswith("#"))}


def api_key(app):
    return re.search(r"<ApiKey>([^<]+)", open(f"{HOME}/opt/nas-media/config/{app}/config.xml").read())[1]


class Arr:
    # One *arr app: GET/POST/PUT on http://127.0.0.1:<port>/api/<version>/...
    def __init__(self, name, port, version):
        self.name, self.url, self.key = name, f"http://127.0.0.1:{port}", api_key(name)
        self.base = f"{self.url}/api/{version}"

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Api-Key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                txt = r.read()
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{self.name} {method} {path}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}")

    def set_config(self, path, **d_want):
        # change only the keys that differ, then PUT the whole object back
        d = self.call("GET", path)
        d_diff = {k: v for k, v in d_want.items() if d.get(k) != v}
        if d_diff:
            d.update(d_diff)
            self.call("PUT", f"{path}/{d['id']}" if "id" in d else path, d)
            print(f"{self.name}: {path} {sorted(d_diff)} set")

    def ensure(self, path, match, body):
        # add one item (root folder, mapping, notification…) unless an item matching it is there
        if any(match(x) for x in self.call("GET", path)):
            return
        self.call("POST", path, body)
        print(f"{self.name}: {path} added")

    def from_schema(self, path, pick, **d_fields):
        # a new item built from the app's own schema, its fields filled from d_fields
        d = next(x for x in self.call("GET", f"{path}/schema") if pick(x))
        for f in d["fields"]:
            if f["name"] in d_fields:
                f["value"] = d_fields[f["name"]]
        return d


d_site = env_file("nas.env")
if d_site is None:
    raise SystemExit("no ~/.config/nas-media/nas.env: copy nas.env.example there and fill it in")
PLEX_PREFS = d_site["PLEX_PREFS"]
d_login = env_file("apps.env", {"APPS_USER": os.environ.get("USER", "nas-media"), "APPS_PASS": secrets.token_urlsafe(18)})
d_qbit = env_file("qbit.env")
plex_token = re.search(r'PlexOnlineToken="([^"]+)"', open(PLEX_PREFS).read())[1]
prowlarr, sonarr, radarr = Arr("prowlarr", 9696, "v1"), Arr("sonarr", 8989, "v3"), Arr("radarr", 7878, "v3")

# --- Every app: a login (the browser remembers it) ---
for a in (prowlarr, sonarr, radarr):
    d = a.call("GET", "/config/host")
    if d["authenticationMethod"] != "forms" or d["username"] != d_login["APPS_USER"]:
        d.update(authenticationMethod="forms", authenticationRequired="enabled",
                 username=d_login["APPS_USER"], password=d_login["APPS_PASS"], passwordConfirmation=d_login["APPS_PASS"])
        a.call("PUT", f"/config/host/{d['id']}", d)
        print(f"{a.name}: login set")

# --- Sonarr and Radarr: library folder, renaming, hardlinks, path mapping, Plex ---
for a, root, d_naming in ((sonarr, "/media/TV Shows", {"renameEpisodes": True}), (radarr, "/media/Movies", {"renameMovies": True})):
    a.ensure("/rootfolder", lambda x: x["path"] == root, {"path": root})
    a.set_config("/config/naming", **d_naming)
    a.set_config("/config/mediamanagement", copyUsingHardlinks=True, deleteEmptyFolders=True)
    # qBittorrent sees the media folder as /downloads, Sonarr and Radarr as /media
    a.ensure("/remotepathmapping", lambda x: x["remotePath"] == "/downloads/",
             {"host": "127.0.0.1", "remotePath": "/downloads/", "localPath": "/media/"})
    a.ensure("/notification", lambda x: x["implementation"] == "PlexServer",
             dict(a.from_schema("/notification", lambda x: x["implementation"] == "PlexServer",
                                host="127.0.0.1", port=32400, authToken=plex_token, updateLibrary=True),
                  name="Plex", onDownload=True, onUpgrade=True, onRename=True, onSeriesDelete=True, onEpisodeFileDelete=True,
                  onMovieDelete=True, onMovieFileDelete=True))

# --- Sonarr: TV episodes only in the languages of TV_LANGUAGES (nas.env). A release whose title says another language (or,
# untagged, a show whose original language is another one) gets the custom format "Not an accepted language", scored −10000:
# below the profile's minimum (0), so refused. Radarr: its HD-1080p profile already wants each movie's original language.
CF = "Not an accepted language"
d_lang = {x["name"]: x["id"] for x in sonarr.call("GET", "/language")}
spec = next(x for x in sonarr.call("GET", "/customformat/schema") if x["implementation"] == "LanguageSpecification")
l_specs = []
for lang in [x.strip() for x in d_site["TV_LANGUAGES"].split(",")]:
    d = json.loads(json.dumps(spec))
    d.update(name=f"not {lang}", negate=True, required=True)
    next(f for f in d["fields"] if f["name"] == "value")["value"] = d_lang[lang]
    l_specs.append(d)
want = sorted((s["name"], next(f["value"] for f in s["fields"] if f["name"] == "value")) for s in l_specs)
d = next((x for x in sonarr.call("GET", "/customformat") if x["name"] == CF or x["name"] == "Not English or French"), None)  # former name
if d is None:
    sonarr.call("POST", "/customformat", {"name": CF, "includeCustomFormatWhenRenaming": False, "specifications": l_specs})
    print(f"sonarr: custom format '{CF}' added")
elif d["name"] != CF or sorted((s["name"], next(f["value"] for f in s["fields"] if f["name"] == "value")) for s in d["specifications"]) != want:
    sonarr.call("PUT", f"/customformat/{d['id']}", dict(d, name=CF, specifications=l_specs))
    print(f"sonarr: custom format '{CF}': {d_site['TV_LANGUAGES']}")
cf_id = next(x["id"] for x in sonarr.call("GET", "/customformat") if x["name"] == CF)
d = next(p for p in sonarr.call("GET", "/qualityprofile") if p["name"] == "HD-1080p")
item = next(x for x in d["formatItems"] if x["format"] == cf_id)
if item["score"] != -10000:
    item["score"] = -10000
    sonarr.call("PUT", f"/qualityprofile/{d['id']}", d)
    print(f"sonarr: HD-1080p refuses '{CF}'")

# --- Sonarr and Radarr: 720p only when no good 1080p exists (the user, 2026-10-15; one episode's only 1080p release had
# holes). The HD-1080p profile also allows 720p, ranked below every 1080p. A 720p release waits until it is 1 day old: measured
# 2026-10-15, the 1080p releases of new episodes came 4–12 h after airing (3 releases: 4.3 h, 5.0 h, 12.3 h).
# A 1080p release is taken at once (custom format "1080p", score 1, skips the wait). A 720p file is replaced by a 1080p one when
# one comes (cutoff: the lowest 1080p, so a 1080p file is never replaced by another 1080p).
s_720 = {"HDTV-720p", "WEB 720p", "Bluray-720p"}
name_of = lambda it: it.get("name") or it["quality"]["name"]
for a in (sonarr, radarr):
    if not any(x["name"] == "1080p" for x in a.call("GET", "/customformat")):
        spec = next(x for x in a.call("GET", "/customformat/schema") if x["implementation"] == "ResolutionSpecification")
        spec.update(name="1080p", required=True)
        next(f for f in spec["fields"] if f["name"] == "value")["value"] = 1080
        a.call("POST", "/customformat", {"name": "1080p", "includeCustomFormatWhenRenaming": False, "specifications": [spec]})
        print(f"{a.name}: custom format '1080p' added")
    cf_id = next(x["id"] for x in a.call("GET", "/customformat") if x["name"] == "1080p")
    d = next(p for p in a.call("GET", "/qualityprofile") if p["name"] == "HD-1080p")
    before = json.dumps(d, sort_keys=True)
    l_720 = [it for it in d["items"] if name_of(it) in s_720]
    l_rest = [it for it in d["items"] if name_of(it) not in s_720]
    i = next(i for i, it in enumerate(l_rest) if name_of(it) == "HDTV-1080p")
    d["items"] = l_rest[:i] + l_720 + l_rest[i:]          # first = lowest: every 720p just below the lowest 1080p
    for it in l_720:
        for x in [it] + it.get("items", []):
            x["allowed"] = True
    d.update(cutoff=l_rest[i]["quality"]["id"], upgradeAllowed=True)
    next(x for x in d["formatItems"] if x["format"] == cf_id)["score"] = 1
    if json.dumps(d, sort_keys=True) != before:
        a.call("PUT", f"/qualityprofile/{d['id']}", d)
        print(f"{a.name}: HD-1080p allows 720p when no 1080p")
    d = next(x for x in a.call("GET", "/delayprofile") if x["order"] == 2147483647)   # the default one, for every show and movie
    d_want = dict(preferredProtocol="torrent", torrentDelay=24 * 60, bypassIfHighestQuality=True,
                  bypassIfAboveCustomFormatScore=True, minimumCustomFormatScore=1)
    if any(d.get(k) != v for k, v in d_want.items()):
        a.call("PUT", f"/delayprofile/{d['id']}", dict(d, **d_want))
        print(f"{a.name}: a 720p release waits until it is 1 day old")

# --- qBittorrent: once its login is known (~/.config/nas-media/qbit.env: QBIT_USER=…, QBIT_PASS=…) ---
if d_qbit:
    jar = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def qb(path, **d_form):
        return jar.open(f"http://127.0.0.1:8888/api/v2/{path}", urllib.parse.urlencode(d_form).encode(), timeout=30).read().decode()
    if qb("auth/login", username=d_qbit["QBIT_USER"], password=d_qbit["QBIT_PASS"]) != "Ok.":
        raise SystemExit("qBittorrent: login refused, check ~/.config/nas-media/qbit.env")
    # landing folder per category. No sharing once a download is complete (the user, 2026-10-15): ratio 0 stops it at once; Sonarr
    # and Radarr then copy it into Plex (hardlink) and remove the torrent and its landing-folder copy
    d_cats = json.loads(qb("torrents/categories"))
    for cat, path in (("tv", "/downloads/torrents/tv"), ("movies", "/downloads/torrents/movies")):
        if cat not in d_cats:
            qb("torrents/createCategory", category=cat, savePath=path)
        elif d_cats[cat]["savePath"].rstrip("/") != path:     # editing to the same folder is refused (409)
            qb("torrents/editCategory", category=cat, savePath=path)
    qb("app/setPreferences", json=json.dumps({
        "auto_tmm_enabled": True, "max_ratio_enabled": True, "max_ratio": 0,
        "max_seeding_time_enabled": True, "max_seeding_time": 0, "max_ratio_act": 0,   # 0: stop the torrent
        # a removed torrent's files are deleted, not moved to media/.Trash-0: there, a file deleted in Plex kept its space
        # (seen 2026-10-15: 65 GB in that trash)
        "torrent_content_remove_option": "Delete",
        "dont_count_slow_torrents": True,          # a stuck download doesn't hold one of the download slots
        "excluded_file_names_enabled": True,
        "excluded_file_names": "\n".join(f"*.{x}" for x in "exe lnk scr bat cmd com pif vbs js msi apk".split())}))
    print("qbittorrent: categories tv, movies; no sharing after download; blocked file types set")
    # Decluttarr's secrets, read by compose (env_file); restart it after a change: docker compose ... up -d
    env_file("decluttarr.env", {"SONARR_API_KEY": sonarr.key, "RADARR_API_KEY": radarr.key,
                                "QBIT_USER": d_qbit["QBIT_USER"], "QBIT_PASS": d_qbit["QBIT_PASS"]})
    for a, d_cat in ((sonarr, {"tvCategory": "tv"}), (radarr, {"movieCategory": "movies"})):
        a.ensure("/downloadclient", lambda x: x["implementation"] == "QBittorrent",
                 dict(a.from_schema("/downloadclient", lambda x: x["implementation"] == "QBittorrent", host="127.0.0.1", port=8888,
                                    username=d_qbit["QBIT_USER"], password=d_qbit["QBIT_PASS"], **d_cat),
                      name="qBittorrent", enable=True, removeCompletedDownloads=True, removeFailedDownloads=True))
        # Sonarr: an episode aired in the last 14 days goes to the top of qBittorrent's queue (before bad-file replacements)
        if a is sonarr:
            d = next(x for x in a.call("GET", "/downloadclient") if x["implementation"] == "QBittorrent")
            f = next(f for f in d["fields"] if f["name"] == "recentTvPriority")
            if f["value"] != 1:                    # 0 = last, 1 = first
                f["value"] = 1
                a.call("PUT", f"/downloadclient/{d['id']}", d)
                print("sonarr: recent episodes first in qBittorrent's queue")
else:
    print("qbittorrent: skipped, no ~/.config/nas-media/qbit.env yet")

# --- Prowlarr: the torrent sites, shared with Sonarr and Radarr ---
for a in (sonarr, radarr):
    impl = a.name.capitalize()
    prowlarr.ensure("/applications", lambda x: x["implementation"] == impl,
                    dict(prowlarr.from_schema("/applications", lambda x: x["implementation"] == impl,
                                              prowlarrUrl=prowlarr.url, baseUrl=a.url, apiKey=a.key),
                         name=impl, syncLevel="fullSync"))
# no longer used (2026-10-15): EZTV, and the Byparr proxy with its "cloudflare" tag
for x in prowlarr.call("GET", "/indexer"):
    if x["definitionName"] == "eztv":
        prowlarr.call("DELETE", f"/indexer/{x['id']}"); print("prowlarr: site eztv removed")
    elif x["tags"]:
        prowlarr.call("PUT", f"/indexer/{x['id']}", dict(x, tags=[]))
for x in prowlarr.call("GET", "/indexerproxy"):
    prowlarr.call("DELETE", f"/indexerproxy/{x['id']}"); print(f"prowlarr: proxy {x['name']} removed")
for x in prowlarr.call("GET", "/tag"):
    if x["label"] == "cloudflare":
        prowlarr.call("DELETE", f"/tag/{x['id']}")
l_have = [x["definitionName"] for x in prowlarr.call("GET", "/indexer")]
for site in SITES:
    if site in l_have:
        continue
    d = prowlarr.from_schema("/indexer", lambda x: x["definitionName"] == site)
    d.update(enable=True, appProfileId=1, priority=25)
    for url in d["indexerUrls"]:                 # a site's mirrors, in Prowlarr's order: the first that passes wins
        next(f for f in d["fields"] if f["name"] == "baseUrl")["value"] = url
        try:
            prowlarr.call("POST", "/indexer", d)     # Prowlarr tests the site before saving
            print(f"prowlarr: site {site} added ({url})")
            break
        except RuntimeError as e:
            err = re.search(r'"errorMessage": "([^"]*)', str(e))
            print(f"prowlarr: site {site} {url} refused: {err[1] if err else str(e)[:120]}")
print("done")

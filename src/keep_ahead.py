# The keep-ahead job. Runs on the NAS every 5 min; deployed to ~/opt/nas-media/keep_ahead.py.
#   python3 keep_ahead.py                      one check
#   KEEP_AHEAD_DRY=1 python3 keep_ahead.py     says what it would do, changes nothing
# One check:
#   1. Sonarr holds exactly the shows in Plex or on your Plex Watchlist (and those with a bad file being replaced).
#   2. Per show: the AHEAD episodes after the last one watched are wanted (= monitored in Sonarr), the others not. One of them
#      out but not found for 2 days: its whole season is wanted (a whole-season torrent), while space allows.
#   3. Bad files (holes; added to the Plex list "Bad files") are deleted and downloaded again.
#   4. Movies on your Watchlist go to Radarr; a movie stops being wanted once downloaded (deleting it in Plex is final).
#   5. Disk low: a warning playlist on Plex's Home; nothing new is downloaded. Nothing is ever deleted except bad files.
# Reads:  Sonarr, Radarr, Plex (shows, movies, what your account watched, the list "Bad files"), plex.tv (your Watchlist; episodes marked
#         watched anywhere in Plex), the video files (holes), the free space, ~/opt/nas-media/state/memory.json,
#         ~/.config/nas-media/nas.env (this home's settings: nas.env.example).
# Writes: Sonarr, Radarr (shows, movies, wanted, searches, bad files deleted, their release blocklisted), qBittorrent (a bad manual
#         download's torrent removed from the list), Plex (an item done removed from "Bad files", that list created when missing, warning playlist), memory.json.
import json, os, re, time, urllib.request, urllib.parse, datetime as dt
from http.cookiejar import CookieJar

AHEAD = 3                     # episodes downloaded ahead of the last one watched, per show (the user, 2026-10-14)
MIN_FREE_GB = 50              # below this free space: nothing new is downloaded
ALERT_FREE_GB = 100           # below this: the warning playlist on Plex's Home. 100 − 50 GB ≈ 18 episodes of 2.4–3.0 GB
                              # (sizes measured 2026-10-15) to act before downloads stop
DISCOVER_EVERY_S = 3600       # plex.tv's watched state is read hourly (the job runs every 5 min)
BAD_LIST = "Bad files"        # the Plex list where you put a bad video (the user, 2026-10-16). Not 1 star: a rating given on the
                              # TV is stored on plex.tv only, never seen by the NAS (tested 2026-10-16); a list lives on the NAS
HOLE_BLOCK = 64 * 1024        # a file with 4 such blocks of zeros in a row (256 KiB, the smallest torrent piece) has holes:
                              # missing pieces. Measured 2026-10-15 on the 203 videos of the library: 185 had none at all; 18 had
                              # 9,000–38,000 blocks (one episode: 29 % of the file, and ffprobe found 29 % of its frames
                              # missing; another: 70 % of its frames missing)
PACK_AFTER_S = 2 * 86400      # an episode out but not found for this long: its whole season is wanted (the user, 2026-10-15)
ALERT = "⚠ NAS almost full"   # the warning playlist's title starts with this
HOME = os.path.expanduser("~")
# this home's settings (nas.env.example): {"MEDIA": "/volume1/media", …}
d_site = {k: v.strip().strip('"') for k, v in (l.split("=", 1) for l in open(f"{HOME}/.config/nas-media/nas.env")
                                                if "=" in l and not l.lstrip().startswith("#"))}
MEDIA = d_site["MEDIA"]
MEMORY = f"{HOME}/opt/nas-media/state/memory.json"
PLEX_PREFS = d_site["PLEX_PREFS"]
DISCOVER = "https://discover.provider.plex.tv"
DRY = os.environ.get("KEEP_AHEAD_DRY") == "1"
NOW = time.time()


def http(method, url, d_headers, body=None):
    req = urllib.request.Request(url, method=method, headers={"Accept": "application/json", "Content-Type": "application/json", **d_headers},
                                 data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=120) as r:
        txt = r.read()
        return json.loads(txt) if txt.strip() else None


def arr(name, port):
    # Sonarr or Radarr's API: sonarr("GET", "/series"); in a dry run, only GETs are sent
    key = re.search(r"<ApiKey>([^<]+)", open(f"{HOME}/opt/nas-media/config/{name}/config.xml").read())[1]
    def call(method, path, body=None):
        if DRY and method != "GET":
            return print(f"  (dry) {name} {method} {path} {json.dumps(body)[:150] if body else ''}")
        return http(method, f"http://127.0.0.1:{port}/api/v3{path}", {"X-Api-Key": key}, body)
    return call
sonarr, radarr = arr("sonarr", 8989), arr("radarr", 7878)


plex_token = re.search(r'PlexOnlineToken="([^"]+)"', open(PLEX_PREFS).read())[1]
def plex(path, method="GET", base="http://127.0.0.1:32400"):
    if DRY and method != "GET":
        return print(f"  (dry) Plex {method} {path[:120]}")
    # the NAS's Plex: a list without X-Plex-Container-Size "will fail with status code 400 in the future" (its log, 2026-10-15);
    # plex.tv refuses that header (400)
    d_h = {"X-Plex-Token": plex_token} | ({"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "100000"} if "127.0.0.1" in base else {})
    return (http(method, base + path, d_h) or {}).get("MediaContainer", {})


_qb = []
def qbit(path, **d_form):
    # qBittorrent's API, logged in once with ~/.config/nas-media/qbit.env
    if not _qb:
        d = dict(l.strip().split("=", 1) for l in open(f"{HOME}/.config/nas-media/qbit.env") if "=" in l)
        _qb.append(urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar())))
        qbit("auth/login", username=d["QBIT_USER"], password=d["QBIT_PASS"])
    if DRY and path != "torrents/info" and not path.startswith("auth/"):
        return print(f"  (dry) qBittorrent {path} {d_form}")
    return _qb[0].open(f"http://127.0.0.1:8888/api/v2/{path}", urllib.parse.urlencode(d_form).encode(), timeout=30).read().decode()


def guid(m, scheme):   # a Plex item's id in another database: guid(m, "tvdb") → 76733, or None
    return next((int(g["id"].split("://")[1]) for g in m.get("Guid", []) if g["id"].startswith(scheme + "://")), None)


def files(m):          # the files Plex plays for an item: [(path as Plex sees it, size)]
    return [(p["file"], p.get("size")) for x in m.get("Media", []) for p in x.get("Part", [])]


def plex_library(kind):
    # every item of Plex's libraries of that kind ("show" or "movie")
    return [m for sec in plex("/library/sections").get("Directory", []) if sec["type"] == kind
            for m in plex(f"/library/sections/{sec['key']}/all?includeGuids=1").get("Metadata", [])]


def aired(e):     # the episode is out
    return bool(e.get("airDateUtc")) and dt.datetime.fromisoformat(e["airDateUtc"].replace("Z", "+00:00")).timestamp() < NOW


def code(e):      # S02E03
    return f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}"


def profile(app):
    return next(p["id"] for p in app("GET", "/qualityprofile") if p["name"] == "HD-1080p")


def holes(path):
    # number of HOLE_BLOCK blocks of zeros in runs of 4 or more (0 = no hole); ~3 s per 3 GB file
    n = run = 0
    zero = bytes(HOLE_BLOCK)
    with open(path, "rb", buffering=0) as f:
        while (b := f.read(HOLE_BLOCK)):
            if b == zero:
                run += 1
            else:
                n, run = n + (run if run >= 4 else 0), 0
    return n + (run if run >= 4 else 0)


d_bad = {}   # the list "Bad files": {Plex ratingKey of an item in it: its playlistItemID}; filled by bad_list()
def bad_list():
    # reads the Plex list "Bad files"; creates it (empty) when missing: Plex creates a list only with an item, removed at once
    pl = next((p for p in plex("/playlists").get("Metadata", []) if p["title"] == BAD_LIST), None)
    if not pl:
        mid = plex("/identity")["machineIdentifier"]
        rk = next(m["ratingKey"] for kind in ("movie", "show") for m in plex_library(kind))   # any item
        uri = urllib.parse.quote(f"server://{mid}/com.plexapp.plugins.library/library/metadata/{rk}", safe="")
        pl = ((plex(f"/playlists?type=video&title={urllib.parse.quote(BAD_LIST)}&smart=0&uri={uri}", method="POST") or {}).get("Metadata") or [None])[0]
        if not pl:
            return                                          # dry run
        for i in plex(f"/playlists/{pl['ratingKey']}/items").get("Metadata", []):
            plex(f"/playlists/{pl['ratingKey']}/items/{i['playlistItemID']}", method="DELETE")
        print(f"Plex: list '{BAD_LIST}' created")
    d_bad.update({i["ratingKey"]: (pl["ratingKey"], i["playlistItemID"]) for i in plex(f"/playlists/{pl['ratingKey']}/items").get("Metadata", [])})


def bad_files(d_mem, m):
    # a Plex item's bad files, each checked once (path and size): [(path, why)]. In the list "Bad files": all its files (the item
    # is taken off the list)
    if m.get("ratingKey") in d_bad:
        pl, item = d_bad.pop(m["ratingKey"])
        plex(f"/playlists/{pl}/items/{item}", method="DELETE")
        return [(p, f"in the list {BAD_LIST}") for p, _ in files(m)]
    l_out = []
    for path, size in files(m):
        if f"{path}|{size}" not in d_mem["checked"]:
            try:
                n = d_mem["checked"][f"{path}|{size}"] = holes(MEDIA + path[len("/media"):])
            except OSError:
                continue                                   # gone since Plex's last scan
            if n:
                l_out.append((path, f"{n * HOLE_BLOCK / 1e6:.0f} MB of holes"))
    return l_out


def drop_bad(app, label, path, why, file_id, l_hist):
    # Deletes a bad file. Downloaded by Sonarr/Radarr (file_id, its history): through them, and its release blocklisted (they then
    # take another one). Else (a manual download): deleted from the disk, and its torrent, if still in qBittorrent, removed from the
    # list (its files stay): it would block the same release ("already there").
    print(f"  {label}: bad file ({why}) → deleted, another download wanted: {path}")
    grab = None
    if file_id:
        imported = next((h for h in l_hist if h["eventType"] == "downloadFolderImported"), None)
        grab = imported and next((h for h in l_hist if h["eventType"] == "grabbed" and h.get("downloadId") == imported.get("downloadId")), None)
        app("DELETE", f"/{'episodefile' if app is sonarr else 'moviefile'}/{file_id}")
        if grab:
            app("POST", f"/history/failed/{grab['id']}")
            print(f"    release blocklisted: {grab['sourceTitle']}")
    elif not DRY:
        os.remove(MEDIA + path[len("/media"):])
    if not grab:
        for t in json.loads(qbit("torrents/info")):
            if os.path.basename(t["content_path"]) == os.path.basename(path):
                qbit("torrents/delete", hashes=t["hash"], deleteFiles="false")
                print(f"    old torrent removed from qBittorrent: {t['name']}")


def discover_last(rk):
    # the last episode marked watched in Plex anywhere (plex.tv; your account): "S50E13", or None. rk: the show's plex.tv id
    l_seen = [s for s in plex(f"/library/metadata/{rk}/children?includeUserState=1", base=DISCOVER).get("Metadata", [])
              if s.get("viewedLeafCount") and s.get("index", 0) > 0]
    if not l_seen:
        return None
    season = max(l_seen, key=lambda s: s["index"])
    ep = max((e["index"] for e in plex(f"/library/metadata/{season['ratingKey']}/children?includeUserState=1", base=DISCOVER)
              .get("Metadata", []) if e.get("viewCount")), default=None)
    return ep and f"S{season['index']:02d}E{ep:02d}"


# ---------------------------------------------------------------------------------------------------------------------
def shows(d_mem, l_watch, low, roomy, daily):
    # Plex's shows {TheTVDB id: show} with their episodes {(season, episode): Plex item}; your Watchlist's shows {TheTVDB id: item}
    d_plex, d_eps = {}, {}
    for show in plex_library("show"):
        if not guid(show, "tvdb"):
            print(f"{show['title']}: in Plex without a TheTVDB id, not tracked (fix its match in Plex)"); continue
        d_plex[guid(show, "tvdb")] = show
        d_eps[guid(show, "tvdb")] = {(e["parentIndex"], e["index"]): e for e in plex(f"/library/metadata/{show['ratingKey']}/allLeaves")
                                     .get("Metadata", []) if "parentIndex" in e}
    d_watch = {guid(m, "tvdb"): m for m in l_watch or [] if m.get("type") == "show" and guid(m, "tvdb")}

    # 1. Sonarr holds exactly the shows in Plex or on the Watchlist, and those with a bad file being replaced. Added: no episode
    # wanted yet (step 2 decides), in the show's Plex folder when it has one. Removed: never with its files.
    d_have = {s["tvdbId"]: s for s in sonarr("GET", "/series")}
    s_keep = set(d_plex) | set(d_watch) | set(d_mem["replace"].values())
    for tvdb in s_keep - set(d_have):
        l_found = sonarr("GET", f"/series/lookup?term=tvdb:{tvdb}")
        if l_found:
            d = dict(l_found[0], qualityProfileId=profile(sonarr), rootFolderPath="/media/TV Shows", seasonFolder=True, monitored=True,
                     monitorNewItems="none", addOptions={"monitor": "none", "searchForMissingEpisodes": False})
            if tvdb in d_plex:   # Plex and Sonarr both see the media folder as /media
                d["path"] = plex(f"/library/metadata/{d_plex[tvdb]['ratingKey']}")["Metadata"][0]["Location"][0]["path"]
            sonarr("POST", "/series", d)
            print(f"  {d['title']} ({d.get('year')}): tracked{', folder ' + d['path'] if tvdb in d_plex else ''}")
    if d_plex and l_watch is not None:   # an empty Plex or an unreadable Watchlist is a hiccup, not "stop everything"
        for tvdb in set(d_have) - s_keep:
            print(f"  {d_have[tvdb]['title']}: in neither Plex nor the Watchlist, no longer tracked")
            sonarr("DELETE", f"/series/{d_have[tvdb]['id']}?deleteFiles=false")

    # plex.tv: the last episode marked watched anywhere in Plex, per show (hourly; a show new to the Watchlist at once)
    for tvdb, rk in [(t, s["guid"].split("/")[-1]) for t, s in d_plex.items() if s.get("guid", "").startswith("plex://show/")] + \
                    [(t, m["ratingKey"]) for t, m in d_watch.items()]:
        if NOW - d_mem["discover_at"] > DISCOVER_EVERY_S or str(tvdb) not in d_mem["discover"]:
            try:
                d_mem["discover"][str(tvdb)] = discover_last(rk)
            except Exception as e:
                print(f"plex.tv watched state of {tvdb} unreadable: {e}")
    if NOW - d_mem["discover_at"] > DISCOVER_EVERY_S:
        d_mem["discover_at"] = NOW

    s_queued = {q.get("episodeId") for q in sonarr("GET", "/queue?pageSize=1000&includeEpisode=false")["records"]}
    l_search, l_on, l_off, l_season, d_missing, d_season = [], [], [], [], {}, {}
    for s in sonarr("GET", "/series"):
        tvdb, k_of = s["tvdbId"], lambda e: (e["seasonNumber"], e["episodeNumber"])
        l_eps = sorted((e for e in sonarr("GET", f"/episode?seriesId={s['id']}&includeEpisodeFile=true") if e["seasonNumber"] > 0), key=k_of)
        if not l_eps:
            continue                                    # just added: Sonarr is still reading it
        d_plex_eps = d_eps.get(tvdb, {})
        # 2. where the show is: after the last episode watched or started in this Plex (started counts: seen 2026-10-16, a show
        # whose E02, E04, E05 were stopped part-way never got E06), marked watched anywhere in Plex (plex.tv), or where
        # it was at the last check ("after": kept when you delete the show); none → before the first episode in Plex, or S01E01
        i_last = max((i for i, e in enumerate(l_eps) if (lambda pe: pe.get("viewCount") or pe.get("viewOffset"))(d_plex_eps.get(k_of(e), {}))),
                     default=-1)
        for mark in (d_mem["discover"].get(str(tvdb)), d_mem["after"].get(str(tvdb))):
            if mark:
                i_last = max([i_last] + [i for i, e in enumerate(l_eps) if code(e) <= mark])
        if i_last < 0 and d_plex_eps:
            i_last = next((i - 1 for i, e in enumerate(l_eps) if k_of(e) in d_plex_eps), -1)
        d_mem["after"][str(tvdb)] = code(l_eps[i_last]) if i_last >= 0 else "S00E00"
        l_window = l_eps[i_last + 1: i_last + 1 + AHEAD]
        s_window = {e["id"] for e in l_window}
        # whole season: an episode among them wanted and out, but not found for PACK_AFTER_S (no single release; Sonarr takes a
        # whole-season torrent only when every episode in it is wanted) → its whole season is wanted and searched, while free space
        # is above ALERT_FREE_GB; until that episode is here or watched
        pack = d_mem["season"].get(str(tvdb))
        for e in l_window:
            if e["monitored"] and aired(e) and not e["hasFile"] and e["id"] not in s_queued and k_of(e) not in d_plex_eps:
                d_missing[str(e["id"])] = since = d_mem["missing"].get(str(e["id"]), NOW)
                if NOW - since > PACK_AFTER_S and roomy and not pack:
                    pack = [e["seasonNumber"], e["id"]]
                    l_season.append((s["id"], e["seasonNumber"]))
                    print(f"  {s['title']} {code(e)}: not found for 2 days → whole season {e['seasonNumber']} wanted, searched")
        if pack and any(e["id"] == pack[1] and not e["hasFile"] for e in l_window):
            d_season[str(tvdb)] = pack
        else:
            pack = None                                     # that episode is here, or watched

        for e in l_eps:
            # 3. bad files: every file Plex plays is checked once for holes; an episode in the list "Bad files" is replaced
            l_bad = bad_files(d_mem, d_plex_eps.get(k_of(e), {}))
            for path, why in l_bad:
                by_sonarr = e["hasFile"] and e["episodeFile"]["path"] == path
                drop_bad(sonarr, f"{s['title']} {code(e)}", path, why, e["episodeFileId"] if by_sonarr else None,
                         sonarr("GET", f"/history?episodeId={e['id']}&pageSize=50&sortKey=date&sortDirection=descending")["records"] if by_sonarr else ())
                e["hasFile"] = e["hasFile"] and not by_sonarr
                d_mem["replace"][str(e["id"])] = tvdb
            if e["hasFile"]:
                d_mem["replace"].pop(str(e["id"]), None)          # replaced (its new file is checked once Plex has it)
            replacing = str(e["id"]) in d_mem["replace"]
            # in Plex from a folder Sonarr doesn't manage (an old manual download): already there, not downloaded again
            elsewhere = k_of(e) in d_plex_eps and not e["hasFile"] and not replacing
            in_pack = bool(pack) and e["seasonNumber"] == pack[0]
            want = (in_pack or (e["id"] in s_window or replacing) and not elsewhere) and (e["hasFile"] or not low)
            if want != e["monitored"]:
                (l_on if want else l_off).append(e["id"])
            # searched once when it becomes wanted or its bad file was just deleted; after that, Sonarr's own check of new releases
            # (every 15 min) and the daily search. A whole season: one search for the season
            if want and not in_pack and (not e["monitored"] or l_bad) and aired(e) and not e["hasFile"] and e["id"] not in s_queued:
                l_search.append(e["id"])

        have = sum(e["hasFile"] or k_of(e) in d_plex_eps for e in l_window)
        win_txt = f"{code(l_window[0])}–{code(l_window[-1])}" if l_window else "none (caught up, waiting for new episodes)"
        print(f"{s['title']}: watched up to {code(l_eps[i_last]) if i_last >= 0 else 'nothing'} · wanted {win_txt} · {have}/{len(l_window)} here")

    if l_on:
        sonarr("PUT", "/episode/monitor", {"episodeIds": l_on, "monitored": True})
    if l_off:
        sonarr("PUT", "/episode/monitor", {"episodeIds": l_off, "monitored": False})
    if l_search:
        sonarr("POST", "/command", {"name": "EpisodeSearch", "episodeIds": l_search})
    for sid, season in l_season:
        sonarr("POST", "/command", {"name": "SeasonSearch", "seriesId": sid, "seasonNumber": season})
    if daily:
        sonarr("POST", "/command", {"name": "MissingEpisodeSearch"})
    d_mem["missing"], d_mem["season"] = d_missing, d_season
    print(f"{len(d_have)} tracked shows · {len(l_on)} episodes now wanted, {len(l_off)} no longer · {len(l_search)} searched"
          f" · {len(d_season)} whole seasons wanted · {len(d_mem['replace'])} bad files being replaced")
    return [(sum(s or 0 for _, s in files(e)), e["ratingKey"]) for d in d_eps.values() for e in d.values() if e.get("viewCount")]


def movies(d_mem, l_watch, low, daily):
    # 4. Movies: those on your Watchlist are added to Radarr; a movie is wanted (monitored) until downloaded; bad files replaced.
    l_plex = [m for m in plex_library("movie") if guid(m, "tmdb")]
    d_have = {m["tmdbId"]: m for m in radarr("GET", "/movie")}
    for r in d_have.values():
        if r["hasFile"] and r["monitored"]:             # downloaded: deleting it in Plex is final
            radarr("PUT", f"/movie/{r['id']}", dict(r, monitored=False))

    def want(tmdb, title):
        if tmdb not in d_have:
            d = dict(radarr("GET", f"/movie/lookup/tmdb?tmdbId={tmdb}"), qualityProfileId=profile(radarr), rootFolderPath="/media/Movies",
                     monitored=True, minimumAvailability="released", addOptions={"searchForMovie": True})
            d_have[tmdb] = radarr("POST", "/movie", d) or d
        else:
            radarr("PUT", f"/movie/{d_have[tmdb]['id']}", dict(d_have[tmdb], monitored=True))
            radarr("POST", "/command", {"name": "MoviesSearch", "movieIds": [d_have[tmdb]["id"]]})
        print(f"  {title}: wanted, searched")

    s_in_plex = {guid(m, "tmdb") for m in l_plex}
    for w in l_watch or []:
        tmdb = guid(w, "tmdb")
        if w.get("type") == "movie" and tmdb and tmdb not in s_in_plex and tmdb not in d_have:
            print(f"watchlist: {w['title']}" + (": waits for free space" if low else ""))
            if not low:
                want(tmdb, w["title"])
    for m in l_plex:
        tmdb = guid(m, "tmdb")
        r = d_have.get(tmdb, {})
        r_file = (r.get("movieFile") or {}) if r.get("hasFile") else {}
        for path, why in bad_files(d_mem, m):
            by_radarr = r_file.get("path") == path
            drop_bad(radarr, m["title"], path, why, r_file.get("id") if by_radarr else None,
                     radarr("GET", f"/history/movie?movieId={r['id']}") if by_radarr else ())
            want(tmdb, m["title"])
    if daily:
        radarr("POST", "/command", {"name": "MissingMoviesSearch"})
    return [(sum(s or 0 for _, s in files(m)), m["ratingKey"]) for m in l_plex if m.get("viewCount")]


def alert(free_gb, l_watched):
    # 5. The warning on Plex's Home (its "Recent Playlists" row): a playlist named "⚠ NAS almost full: 85 GB left · watched, biggest
    # first", listing what you watched, biggest files first, for you to delete in Plex. Gone again once space is back.
    # l_watched: [(size, Plex ratingKey)]
    l_have = [p for p in plex("/playlists").get("Metadata", []) if p["title"].startswith(ALERT)]
    title = f"{ALERT}: {5 * round(free_gb / 5):.0f} GB left · watched, biggest first"
    if free_gb < ALERT_FREE_GB and any(p["title"] == title for p in l_have):
        return
    for p in l_have:
        plex(f"/playlists/{p['ratingKey']}", method="DELETE")
    if free_gb < ALERT_FREE_GB and l_watched:
        mid = plex("/identity")["machineIdentifier"]
        l_rk = [rk for _, rk in sorted(l_watched, reverse=True)[:40]]
        uri = urllib.parse.quote(f"server://{mid}/com.plexapp.plugins.library/library/metadata/{','.join(l_rk)}", safe="")
        plex(f"/playlists?type=video&title={urllib.parse.quote(title)}&smart=0&uri={uri}", method="POST")
        print(f"Plex Home: playlist '{title}'")
    elif l_have:
        print("Plex Home: warning playlist removed (space is back)")


def check():
    st = os.statvfs(MEDIA)
    free_gb = st.f_bavail * st.f_frsize / 1e9
    low = free_gb < MIN_FREE_GB
    print(f"free space {free_gb:.0f} GB{' → LOW: nothing new is downloaded' if low else ''}"
          f"{' → warning on Plex Home' if free_gb < ALERT_FREE_GB else ''}")
    d_old = json.load(open(MEMORY)) if os.path.exists(MEMORY) else {}
    d_mem = {k: d_old.get(k, v) for k, v in {"checked": {}, "replace": {}, "after": {}, "discover": {}, "discover_at": 0, "daily": "",
                                         "missing": {}, "season": {}}.items()}
    for eid, v in d_mem["replace"].items():   # older format {episode: time} → {episode: its show's TheTVDB id}
        if not isinstance(v, int):
            d_mem["replace"][eid] = next(s["tvdbId"] for s in sonarr("GET", "/series") if s["id"] == sonarr("GET", f"/episode/{eid}")["seriesId"])
    today = dt.datetime.utcfromtimestamp(NOW).strftime("%Y-%m-%d")
    daily, d_mem["daily"] = d_mem["daily"] != today, today   # once a day: Sonarr and Radarr search everything still missing
    try:
        l_watch = plex("/library/sections/watchlist/all?includeGuids=1", base=DISCOVER).get("Metadata", [])
    except Exception as e:
        l_watch = None
        print(f"Plex Watchlist unreadable: {e}")
    bad_list()
    l_watched = shows(d_mem, l_watch, low, free_gb >= ALERT_FREE_GB, daily) + movies(d_mem, l_watch, low, daily)
    alert(free_gb, l_watched)
    if not DRY:
        os.makedirs(os.path.dirname(MEMORY), exist_ok=True)
        json.dump(d_mem, open(MEMORY + ".new", "w"), indent=1)
        os.replace(MEMORY + ".new", MEMORY)


if __name__ == "__main__":
    check()

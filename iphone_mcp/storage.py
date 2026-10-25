"""Storage report without touching the screen.

Over the lockdown connection (USB, or Wi-Fi sync once paired): capacity and
every app's size split into the app itself and its documents & data. With
deep=True, a `du` of the media partition over AFC (the camera roll and Photos'
own caches), scanned by several parallel connections.

Everything here is read-only. Suggestions say where space can come back and
how; deleting stays the user's call.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections import Counter, defaultdict
from typing import Any

from . import device

GB = 1e9

# Where an app keeps clearable data, in its own settings. Keyed by bundle id prefix.
IN_APP = {
    "net.whatsapp.WhatsApp": "WhatsApp > Settings > Storage and Data > Manage Storage (large files, forwarded media, per chat)",
    "com.spotify.client": "Spotify > Settings > Storage: clear cache, remove downloads you don't need offline",
    "com.zhiliaoapp.musically": "TikTok > Profile > menu > Settings and privacy > Free up space > Cache",
    "com.toyopagroup.picaboo": "Snapchat > Settings > Clear Data > Clear Cache",
    "com.burbn.instagram": "Instagram has no cache button: delete and reinstall it to drop the cache (log back in)",
    "com.apple.MobileSMS": "Settings > Messages > Keep Messages: 1 Year, and Settings > General > iPhone Storage > "
                           "Messages > Review Large Attachments / Top Conversations",
    "com.apple.mobilenotes": "Notes: attachments (scans, photos, PDFs) inside notes; delete big attachments in place",
    "ph.telegra.Telegraph": "Telegram > Settings > Data and Storage > Storage Usage > Clear Cache",
    "com.hammerandchisel.discord": "Discord > Settings > Advanced > Clear cache",
    "com.google.chrome.ios": "Chrome > Settings > Privacy and security > Delete browsing data > Cached images and files",
    "com.google.Maps": "Google Maps > profile > Offline maps: delete maps you don't need",
    "com.apple.podcasts": "Podcasts: remove downloaded episodes (Library > Downloaded)",
    "com.apple.Music": "Music: remove downloaded songs (Settings > Music > Downloaded Music)",
    "com.netflix.Netflix": "Netflix > My Netflix > Downloads",
    "com.google.ios.youtube": "YouTube > Library > Downloads",
    "com.atebits.Tweetie2": "X keeps a media cache: delete and reinstall to drop it",
    "com.linkedin.LinkedIn": "LinkedIn keeps a media cache: delete and reinstall to drop it",
    "com.google.Gmail": "Gmail caches mail and attachments: delete and reinstall to drop the cache",
}


async def _call(x):
    return await x if inspect.isawaitable(x) else x


async def apps_and_disk() -> dict[str, Any]:
    info = await device.info()
    disk = info["disk"]
    cap = disk.get("TotalDataCapacity") or disk.get("TotalDiskCapacity") or 0
    free = disk.get("AmountDataAvailable") or disk.get("TotalDataAvailable") or 0
    apps = sorted(await device.apps("User", sizes=True), key=lambda a: -a["total_bytes"])
    return {"device": info["values"].get("DeviceName"), "ios": info["values"].get("ProductVersion"),
            "capacity": cap, "free": free, "apps": apps}


async def media_du(workers: int = 6) -> tuple[list[list], list[str]]:
    """[path, size, mtime-date] for every file on the media partition, plus the
    folders that could not be read. Second-level folders are spread over
    several AFC connections (Photos' own caches alone hold ~45k files)."""
    from pymobiledevice3.services.afc import AfcService
    ld = await device._lockdown()
    try:
        root = AfcService(lockdown=ld)
        files: list[list] = []
        jobs: list[str] = []
        for t in await _call(root.listdir("/")):
            if t in (".", ".."):
                continue
            top = f"/{t}"
            if not await _call(root.isdir(top)):
                st = await _call(root.stat(top))
                files.append([top, int(st.get("st_size", 0)), str(st.get("st_mtime", ""))[:10]])
                continue
            for n in await _call(root.listdir(top)):
                if n not in (".", ".."):
                    jobs.append(f"{top}/{n}")
        queue: asyncio.Queue[str] = asyncio.Queue()
        for j in jobs:
            queue.put_nowait(j)
        failed: list[str] = []

        async def scan(afc: AfcService, top: str) -> list[list]:
            got: list[list] = []
            if not await _call(afc.isdir(top)):
                st = await _call(afc.stat(top))
                return [[top, int(st.get("st_size", 0)), str(st.get("st_mtime", ""))[:10]]]
            async for d, _, names in afc.walk(top):
                for n in names:
                    p = f"{d.rstrip('/')}/{n}"
                    try:
                        st = await _call(afc.stat(p))
                    except Exception:
                        continue  # vanished mid-scan
                    got.append([p, int(st.get("st_size", 0)), str(st.get("st_mtime", ""))[:10]])
            return got

        async def worker() -> None:
            afc = AfcService(lockdown=ld)
            while not queue.empty():
                top = queue.get_nowait()
                for attempt in (1, 2):
                    try:
                        files.extend(await scan(afc, top))
                        break
                    except Exception as e:
                        afc = AfcService(lockdown=ld)  # a broken connection; retry the folder on a new one
                        if attempt == 2:
                            failed.append(f"{top} ({type(e).__name__})")

        await asyncio.gather(*(worker() for _ in range(workers)))
        return files, failed
    finally:
        await ld.close()


def analyze_media(files: list[list]) -> dict[str, Any]:
    total = sum(s for _, s, _ in files)
    folders: Counter = Counter()
    for p, s, _ in files:
        folders["/" + p.strip("/").split("/")[0]] += s
    dcim = [(p, s, t) for p, s, t in files if p.startswith("/DCIM/")]
    kinds: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    stems: dict[str, set] = defaultdict(set)
    for p, s, _ in dcim:
        base, ext = os.path.splitext(p)
        kinds[ext.upper().lstrip(".")][0] += 1
        kinds[ext.upper().lstrip(".")][1] += s
        stems[base].add(ext.upper())
    live = {b for b, exts in stems.items() if ".MOV" in exts and exts & {".HEIC", ".JPG", ".JPEG"}}
    live_bytes = sum(s for p, s, _ in dcim if p.upper().endswith(".MOV") and os.path.splitext(p)[0] in live)
    videos = sorted(((s, p, t) for p, s, t in dcim if p.upper().endswith((".MOV", ".MP4"))
                     and os.path.splitext(p)[0] not in live), reverse=True)
    shots = [(p, s) for p, s, _ in dcim if p.upper().endswith(".PNG")]
    years: Counter = Counter()
    for _, s, t in dcim:
        years[t[:4] or "?"] += s
    same: dict[int, list[str]] = defaultdict(list)
    for p, s, _ in dcim:
        if s > 500_000:
            same[s].append(p)
    dup_bytes = sum(s * (len(ps) - 1) for s, ps in same.items() if len(ps) > 1)
    return {
        "files": len(files), "bytes": total, "folders": folders.most_common(8),
        "camera_roll": {"files": len(dcim), "bytes": sum(s for _, s, _ in dcim),
                        "by_type": sorted(((k, n, b) for k, (n, b) in kinds.items()), key=lambda x: -x[2]),
                        "by_year": sorted(years.items()),
                        "live_motion": {"count": len(live), "bytes": live_bytes},
                        "videos": {"count": len(videos), "bytes": sum(s for s, _, _ in videos),
                                   "largest": [(p, s, t) for s, p, t in videos[:10]]},
                        "screenshots": {"count": len(shots), "bytes": sum(s for _, s in shots)},
                        "same_size_copies_bytes": dup_bytes},
        "photos_caches": sum(s for p, s, _ in files if p.startswith(("/PhotoData/", "/MediaAnalysis/"))),
    }


def suggestions(r: dict[str, Any]) -> list[str]:
    out: list[str] = []
    media = r.get("media")
    if media:
        cr = media["camera_roll"]
        out.append(f"Camera roll is {cr['bytes'] / GB:.1f} GB ({cr['files']} files). Once it is backed up (Bottomless, "
                   "Google Photos), deleting originals is the biggest win; Recently Deleted keeps them 30 days.")
        if cr["live_motion"]["bytes"] > 1 * GB:
            out.append(f"Live Photo motion clips take {cr['live_motion']['bytes'] / GB:.1f} GB "
                       f"({cr['live_motion']['count']} clips). Turning Live off in the Camera stops new ones; "
                       "existing clips only go when the photo is deleted (back up first).")
        old = [(y, b) for y, b in cr["by_year"] if y.isdigit() and int(y) <= time.localtime().tm_year - 3]
        if old and sum(b for _, b in old) > 2 * GB:
            out.append("Older years: " + ", ".join(f"{y} {b / GB:.1f} GB" for y, b in old)
                       + " (by file date). Good candidates to keep only in the backup.")
        if cr["screenshots"]["count"] > 100:
            out.append(f"{cr['screenshots']['count']} screenshots ({cr['screenshots']['bytes'] / GB:.2f} GB): "
                       "Photos > Collections > Screenshots, select and delete in bulk.")
        if media["photos_caches"] > 2 * GB:
            out.append(f"Photos' own thumbnails, search and analysis data take {media['photos_caches'] / GB:.1f} GB; "
                       "they shrink by themselves as the library shrinks.")
    for a in r.get("apps", []):
        tip = next((v for k, v in IN_APP.items() if a["bundle_id"].startswith(k)), None)
        if a["data_bytes"] > 0.8 * GB and tip:
            out.append(f"{a['name']}: {a['data_bytes'] / GB:.1f} GB of data. {tip}.")
        elif a["data_bytes"] > 1.5 * GB:
            out.append(f"{a['name']}: {a['data_bytes'] / GB:.1f} GB of data; look for downloads or a cache "
                       "setting in the app, or delete and reinstall to drop its cache.")
    big_bins = [a for a in r.get("apps", []) if a["app_bytes"] > 0.9 * GB and a["data_bytes"] < 0.3 * GB]
    for a in big_bins[:4]:
        out.append(f"{a['name']} is a {a['app_bytes'] / GB:.1f} GB app with little data: if it is rarely used, "
                   "Offload App (Settings > General > iPhone Storage) frees that and keeps its data.")
    return out


async def report(deep: bool = False) -> dict[str, Any]:
    t0 = time.time()
    r = await apps_and_disk()
    if deep:
        files, failed = await media_du()
        r["media"] = analyze_media(files)
        r["media"]["unreadable"] = failed
    r["suggestions"] = suggestions(r)
    r["seconds"] = round(time.time() - t0, 1)
    return r


def format_report(r: dict[str, Any], top: int = 15) -> str:
    g = lambda n: f"{n / GB:.2f} GB" if n >= 0.1 * GB else f"{n / 1e6:.0f} MB"  # noqa: E731
    cap, free = r["capacity"], r["free"]
    lines = [f"{r.get('device') or 'iPhone'} (iOS {r.get('ios')}): {g(free)} free of {g(cap)}"
             + (f" ({100 * (1 - free / cap):.0f}% used)" if cap else ""),
             f"{len(r['apps'])} apps use {g(sum(a['total_bytes'] for a in r['apps']))}. Largest (app + data):"]
    for a in r["apps"][:top]:
        lines.append(f"  {g(a['total_bytes']):>9}  {a['name']}  (app {g(a['app_bytes'])}, data {g(a['data_bytes'])})")
    m = r.get("media")
    if m:
        cr = m["camera_roll"]
        lines += ["", f"Media partition: {g(m['bytes'])} in {m['files']} files",
                  "  camera roll by type: " + ", ".join(f"{k} {n} = {g(b)}" for k, n, b in cr["by_type"][:6]),
                  f"  Live Photo motion: {cr['live_motion']['count']} clips = {g(cr['live_motion']['bytes'])}",
                  f"  videos: {cr['videos']['count']} = {g(cr['videos']['bytes'])}; largest "
                  + ", ".join(f"{os.path.basename(p)} {g(s)}" for p, s, _ in cr["videos"]["largest"][:5]),
                  "  by year: " + ", ".join(f"{y} {g(b)}" for y, b in cr["by_year"]),
                  f"  Photos caches: {g(m['photos_caches'])}; exact-size copies: {g(cr['same_size_copies_bytes'])}"]
        if m.get("unreadable"):
            lines.append("  could not read: " + ", ".join(m["unreadable"]))
    if r["suggestions"]:
        lines += ["", "Where to get space back:"] + [f"  - {s}" for s in r["suggestions"]]
    lines.append("\nApp sizes come from the phone's install service, which leaves out data some apps keep in a "
                 "shared container (WhatsApp, for one); Settings > General > iPhone Storage shows those.")
    lines.append(f"(read-only, {r['seconds']}s)")
    return "\n".join(lines)

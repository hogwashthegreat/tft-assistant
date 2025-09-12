#!/usr/bin/env python3
"""
tft_lobby_contested.py
- Grabs your current TFT lobby via Riot spectator (platform-routed).
- For each player, tries to scrape tactics.tools to infer likely comps.
- If a player page yields nothing, falls back to a tiny Riot match-history probe.
- Prints per-player top cores + a lobby "trait contestedness" summary.

Edit RIOT_ID / PLATFORM_GUESS / API_KEY_HARDCODE below, then run:
  $env:RIOT_API_KEY="RGAPI-..."    # or hardcode into API_KEY_HARDCODE
  python -u tft_lobby_contested.py

Deps: requests (pip install requests)
"""

# ====== EDIT ME ======
RIOT_ID        = "sparky#dsg"   # <-- change to your "Name#Tag"
PLATFORM_GUESS = "na1"            # e.g. "na1" to skip probing, else None
API_KEY_HARDCODE = ""            # optional: paste your key here (less safe)
# =====================

import os, sys, time, re, json, html
from urllib.parse import quote
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

USER_AGENT       = "tft-assistant/1.0 (contact: you@example.com)"  # be polite
SCRAPE_DELAY_S   = 1.0   # ~1 req/sec per player to be gentle
SPECTATOR_PATH   = "/lol/spectator/tft/v5/active-games/by-puuid/{puuid}"  # correct TFT spectator path

ALL_PLATFORMS = [
    "na1","br1","la1","la2","oc1","euw1","eun1","tr1","ru","jp1","kr",
    "ph2","sg2","th2","tw2","vn2"
]
PLATFORM_TO_REGION = {
    "na1":"americas","br1":"americas","la1":"americas","la2":"americas","oc1":"americas",
    "euw1":"europe","eun1":"europe","tr1":"europe","ru":"europe",
    "jp1":"asia","kr":"asia","ph2":"asia","sg2":"asia","th2":"asia","tw2":"asia","vn2":"asia",
}
# tactics.tools region slugs
PLATFORM_TO_TT = {
    "na1":"na","br1":"br","la1":"lan","la2":"las","oc1":"oce","euw1":"euw","eun1":"eune",
    "tr1":"tr","ru":"ru","jp1":"jp","kr":"kr","ph2":"sea","sg2":"sea","th2":"sea","tw2":"tw","vn2":"vn"
}
NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL|re.IGNORECASE)

# ------------------------------ utils ------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)

def mask_key(k: str) -> str:
    k = k or ""
    return k if len(k) < 12 else f"{k[:8]}...{k[-4:]}"

def riot_platform_url(platform: str, path: str) -> str:
    return f"https://{platform}.api.riotgames.com{path}"

def riot_region_url(region: str, path: str) -> str:
    return f"https://{region}.api.riotgames.com{path}"

def split_riot_id(riot_id: str):
    if "#" not in riot_id:
        sys.exit('RIOT_ID must look like "Name#Tag".')
    name, tag = riot_id.split("#", 1)
    return name.strip(), tag.strip()

def get_json(session: requests.Session, url: str, *, params=None, soft_404=False):
    r = session.get(url, params=params, timeout=10)
    if soft_404 and r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json() if "application/json" in r.headers.get("content-type","") else None

# --------------------------- tactics.tools ---------------------------
ROBOTS_CACHE = {"txt": None}

def robots_allows(session: requests.Session, path: str) -> bool:
    """Minimal robots.txt check; if unreadable, allow and go slow."""
    try:
        if ROBOTS_CACHE["txt"] is None:
            resp = session.get("https://tactics.tools/robots.txt", timeout=6)
            ROBOTS_CACHE["txt"] = resp.text if resp.status_code == 200 else ""
        txt = ROBOTS_CACHE["txt"] or ""
        ua_star = False
        disallows = []
        for line in txt.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("user-agent:"):
                ua_star = (line.split(":",1)[1].strip() == "*")
            elif ua_star and line.lower().startswith("disallow:"):
                d = line.split(":",1)[1].strip()
                if d:
                    disallows.append(d)
        for d in disallows:
            if path.startswith(d):
                return False
        return True
    except Exception:
        return True

def deep_find_comps(obj):
    """Heuristic: gather dicts that look like comp summaries (have 'traits' list)."""
    found = []
    if isinstance(obj, dict):
        if "traits" in obj and isinstance(obj["traits"], (list, tuple)):
            found.append(obj)
        for v in obj.values():
            found.extend(deep_find_comps(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found.extend(deep_find_comps(v))
    return found

def trait_names_from_comp(comp_dict):
    names = []
    for t in comp_dict.get("traits", []):
        if isinstance(t, str):
            names.append(t)
        elif isinstance(t, dict):
            names.append(t.get("name") or t.get("slug") or t.get("key") or "")
    return tuple([n for n in names if n][:2])  # define core by top 2 traits

def scrape_player_likely_cores(session: requests.Session, tt_region: str, game_name: str, tag_line: str|None):
    path = f"/player/{tt_region}/{quote(game_name)}"
    if tag_line:
        path += "/" + quote(tag_line)
    if not robots_allows(session, path):
        log(f"[robots] skipping {path}")
        return []
    url = "https://tactics.tools" + path
    resp = session.get(url, timeout=12)
    time.sleep(SCRAPE_DELAY_S)  # polite pacing
    if resp.status_code != 200:
        return []
    m = NEXT_DATA_RE.search(resp.text)
    if not m:
        return []
    try:
        data = json.loads(html.unescape(m.group(1)))
    except Exception:
        return []
    root = (data.get("props", {}) or {}).get("pageProps") or data.get("pageProps") or data
    comps = deep_find_comps(root)
    if not comps:
        return []
    tallies = Counter()
    for c in comps:
        core = trait_names_from_comp(c)
        if not core:
            continue
        w = 1.0
        for k in ("games","matches","count"):
            v = c.get(k)
            if isinstance(v, (int, float)):
                w = max(w, float(v))
        for k in ("playRate","playrate","rate","pr"):
            v = c.get(k)
            if isinstance(v, (int, float)):
                w = max(w, float(v) * 100.0)
        for k in ("winRate","wr"):
            v = c.get(k)
            if isinstance(v, (int, float)):
                w += float(v) * 10.0
        tallies[core] += w
    total = sum(tallies.values()) or 1.0
    return [(core, tallies[core]/total) for core,_ in tallies.most_common(5)]

# -------------------------- Riot helpers --------------------------
def names_by_puuid(riot_session: requests.Session, region: str, puuids: list[str]) -> dict[str, tuple[str, str]]:
    """Map each PUUID -> (gameName, tagLine) via account-v1. Small concurrency, very cheap."""
    base = riot_region_url(region, "/riot/account/v1/accounts/by-puuid/")
    out: dict[str, tuple[str,str]] = {}

    def fetch(pu):
        try:
            r = riot_session.get(base + pu, timeout=8)
            if r.status_code == 200:
                js = r.json()
                return pu, (js.get("gameName","").strip(), js.get("tagLine","").strip())
        except Exception:
            pass
        return pu, ("", "")

    with ThreadPoolExecutor(max_workers=min(6, len(puuids))) as ex:
        futs = [ex.submit(fetch, pu) for pu in puuids]
        for f in as_completed(futs):
            pu, pair = f.result()
            out[pu] = pair
    return out

def fallback_cores_from_riot(riot_session: requests.Session, region: str, puuid: str, max_ids: int = 4):
    """Minimal API fallback: up to ~5 calls/player (ids + a few matches)."""
    try:
        ids = riot_session.get(
            riot_region_url(region, f"/tft/match/v1/matches/by-puuid/{puuid}/ids"),
            params={"count": max_ids}, timeout=8
        )
        if ids.status_code != 200:
            return []
        mids = ids.json() or []
        tallies = Counter()
        total = 0.0
        for i, mid in enumerate(mids):
            m = riot_session.get(riot_region_url(region, f"/tft/match/v1/matches/{mid}"), timeout=10)
            if m.status_code != 200:
                continue
            info = m.json().get("info", {})
            me = next((pp for pp in info.get("participants", []) if pp.get("puuid")==puuid), None)
            if not me:
                continue
            traits = sorted(
                [t for t in me.get("traits", []) if t.get("tier_current",0) > 0],
                key=lambda t: (t.get("tier_current",0), t.get("num_units",0)), reverse=True
            )
            names = [t.get("name","") for t in traits if t.get("name")]
            core = tuple(names[:2]) if len(names)>=2 else tuple(names)
            if not core:
                continue
            recency_w = 0.85 ** i
            place_w   = 1.3 if (me.get("placement", 9) <= 4) else 1.0
            w = recency_w * place_w
            tallies[core] += w
            total += w
        if not tallies:
            return []
        return [(c, tallies[c]/total) for c,_ in tallies.most_common(5)]
    except Exception:
        return []

# ------------------------------ main ------------------------------
def main():
    # API key
    api_key = (os.getenv("RIOT_API_KEY") or API_KEY_HARDCODE or "").strip().strip('"').strip("'")
    if not api_key.startswith("RGAPI-"):
        sys.exit("Set RIOT_API_KEY in your shell or paste your key into API_KEY_HARDCODE.")
    log(f"Using RIOT_API_KEY={mask_key(api_key)}")

    game_name, tag_line = split_riot_id(RIOT_ID)

    # Riot sessions
    riot = requests.Session()
    riot.headers.update({"X-Riot-Token": api_key})

    # sanity ping (helps surface bad keys fast)
    st = riot.get(riot_platform_url("na1", "/tft/status/v1/platform-data"), timeout=8)
    if st.status_code == 401: sys.exit("401 on /tft/status → key missing/expired.")
    if st.status_code == 403: sys.exit("403 on /tft/status → key/app not authorized for TFT.")

    # 1) Riot ID -> PUUID (try americas/europe/asia; some tags migrate weirdly)
    acct = None
    for reg in ("americas","europe","asia"):
        try:
            acct = get_json(riot, riot_region_url(reg, f"/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tag_line)}"))
            if acct:
                region_for_account = reg
                break
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (404,403):
                continue
            raise
    if not acct:
        sys.exit(f"Account not found for {game_name}#{tag_line}. Double-check spelling/case.")
    puuid = acct["puuid"]
    log(f"✔ account → puuid resolved ({puuid[:8]}…)")

    # 2) Resolve platform for this PUUID (use guess or probe /tft/summoner)
    platform, summ_profile = None, None
    if PLATFORM_GUESS and PLATFORM_GUESS in PLATFORM_TO_REGION:
        r = riot.get(riot_platform_url(PLATFORM_GUESS, f"/tft/summoner/v1/summoners/by-puuid/{puuid}"), timeout=8)
        if r.status_code == 200:
            platform, summ_profile = PLATFORM_GUESS, r.json()
    if not platform:
        for plat in ALL_PLATFORMS:
            r = riot.get(riot_platform_url(plat, f"/tft/summoner/v1/summoners/by-puuid/{puuid}"), timeout=8)
            if r.status_code == 200:
                platform, summ_profile = plat, r.json()
                break
            if r.status_code in (401,429):
                time.sleep(0.5)
    if not platform:
        sys.exit("Couldn’t resolve platform via /tft/summoner.")
    region = PLATFORM_TO_REGION[platform]
    tt_slug = PLATFORM_TO_TT.get(platform)
    log(f"✔ platform resolved: {platform} (region {region}, tactics.tools {tt_slug})")

    # 3) Spectator (platform) — correct path under LoL namespace
    try:
        live = get_json(riot, riot_platform_url(platform, SPECTATOR_PATH.format(puuid=puuid)), soft_404=True)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 403: sys.exit("Spectator 403: forbidden (key not TFT-enabled or wrong host/path).")
        if code == 401: sys.exit("Spectator 401: unauthorized (key missing/expired).")
        raise
    if not live:
        sys.exit("Not in an active TFT game (spectator 404). Try during champ select/loading/in-game).")
    participants = live.get("participants", [])
    log(f"✔ live game: {len(participants)} players")

    # 4) Resolve names for every participant (some spectator payloads omit riotId)
    puuids = [p.get("puuid") for p in participants if p.get("puuid")]
    name_map = names_by_puuid(riot, region, puuids)   # {puuid: (gameName, tagLine)}

    # 5) Scrape tactics.tools, fallback to Riot match-history where needed
    scrape = requests.Session()
    scrape.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})

    def display_name(pu, fallback_summ_name=""):
        g, t = name_map.get(pu, ("",""))
        if g and t: return f"{g}#{t}"
        return fallback_summ_name or g or "?"

    lobby_preds = {}
    for idx, p in enumerate(participants, 1):
        pu = p.get("puuid", "")
        summname = p.get("summonerName","")
        gname, tline = name_map.get(pu, ("",""))
        show = display_name(pu, summname)

        print(f"[{idx}/{len(participants)}] {show} → scraping tactics.tools/{tt_slug} …", flush=True)

        preds = []
        if gname:
            try:
                preds = scrape_player_likely_cores(scrape, tt_slug, gname, (tline or None))
            except Exception as ex:
                print(f"   → scrape failed: {ex}", flush=True)

        if not preds:
            print("   → no comps on tactics.tools; falling back to Riot match history (quick)…", flush=True)
            preds = fallback_cores_from_riot(riot, region, pu)

        lobby_preds[pu] = preds
        if preds:
            tops = ",  ".join([f"{' + '.join(c)} ({prob*100:.0f}%)" for c, prob in preds[:3]])
            print(f"   → {tops}", flush=True)
        else:
            print("   → (still no signal)", flush=True)

    # 6) Contestedness summary
    trait_pressure = Counter()
    for preds in lobby_preds.values():
        if preds:
            core, prob = preds[0]
            for t in core:
                trait_pressure[t] += prob

    print("\n=== Trait contestedness ===", flush=True)
    if not trait_pressure:
        print("(no signal)", flush=True)
        return
    items = sorted(trait_pressure.items(), key=lambda kv: kv[1], reverse=True)
    print("Most contested:", flush=True)
    for t, s in items[:8]:
        print(f"  • {t}: {s:.2f} players-likely", flush=True)
    print("Least contested:", flush=True)
    for t, s in items[-8:]:
        print(f"  • {t}: {s:.2f} players-likely", flush=True)

if __name__ == "__main__":
    main()

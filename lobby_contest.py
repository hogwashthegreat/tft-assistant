# pip install requests
import os, time, math, collections, itertools, requests

API_KEY   = os.getenv("RIOT_API_KEY") or "RGAPI_your_key_here"
GAME_NAME = "Sparky"   # before '#'
TAG_LINE  = "dsg"        # after '#'
PLATFORM  = "na1"        # na1, euw1, kr, etc.

# map platform -> regional routing for TFT matches
PLAT2REG = {
    "na1":"americas","br1":"americas","la1":"americas","la2":"americas",
    "oc1":"americas","euw1":"europe","eun1":"europe","tr1":"europe",
    "ru":"europe","kr":"asia","jp1":"asia","sg2":"asia","tw2":"asia","vn2":"asia"
}

HEADERS = {"X-Riot-Token": API_KEY}
REGION  = PLAT2REG.get(PLATFORM, "americas")

def get_json(url, params=None, host_type="regional", backoff=1.0):
    for attempt in range(7):
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if r.status_code == 429:
            # simple exponential backoff using X-Rate-Limit headers would be nicer
            time.sleep(backoff); backoff *= 1.6; continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate limited or failing: {url}")

def regional(base):  return f"https://{REGION}.api.riotgames.com{base}"
def platform(base):  return f"https://{PLATFORM}.api.riotgames.com{base}"

# 1) Riot ID -> PUUID
acct = get_json(regional(f"/riot/account/v1/accounts/by-riot-id/{GAME_NAME}/{TAG_LINE}"))
if not acct: raise SystemExit("Riot ID not found (double-check name#tag).")
puuid = acct["puuid"]

# 2) Live game via TFT spectator (by PUUID on platform host)
live = get_json(platform(f"/lol/spectator/tft/v5/active-games/by-puuid/{puuid}"))

if not live:
    print("Not in a TFT game (or lobby hasn’t started). Try again when you’re in champ select/loading.")
    raise SystemExit()

participants = live.get("participants", [])
p_list = [p["puuid"] for p in participants if "puuid" in p]

# 3) fetch recent matches for each participant
def recent_match_ids(puuid, n=10):
    return get_json(regional(f"/tft/match/v1/matches/by-puuid/{puuid}/ids"), params={"count": n}) or []

def fetch_match(mid):
    return get_json(regional(f"/tft/match/v1/matches/{mid}"))

def core_traits_from_match(m):
    info = m.get("info", {})
    placement = m.get("info", {}).get("participants", [{}])[0].get("placement")
    # find our participant in this match by PUUID? We'll return per-participant below.
    return info

def trait_core_for_participant(m, player_puuid):
    info = m.get("info", {})
    me = next((pp for pp in info.get("participants", []) if pp.get("puuid")==player_puuid), None)
    if not me: return None
    # pick top 2-3 traits by tier_current; ignore vanishing traits
    traits = sorted(
        [t for t in me.get("traits", []) if t.get("tier_current",0) > 0],
        key=lambda t: (t.get("tier_current",0), t.get("num_units",0)), reverse=True
    )
    names = [t["name"] for t in traits]
    # compact: use first 2 (fallback to 3 if the 2nd is weak)
    core = tuple(names[:2]) if len(names)>=2 else tuple(names)
    if len(core)==2 and traits and traits[1].get("tier_current",0) < 2 and len(names)>=3:
        core = (names[0], names[2])
    placement = me.get("placement", 9)
    augments  = tuple(me.get("augments", []))
    return {"core": core, "placement": placement, "augments": augments}

def predict_player_cores(puuid, max_ids=10):
    ids = recent_match_ids(puuid, max_ids)
    cores = []
    for i, mid in enumerate(ids):
        m = fetch_match(mid)
        d = trait_core_for_participant(m, puuid)
        if not d: continue
        # weight: newer games heavier + top4 bonus
        recency_w = 0.85 ** i
        place_w   = 1.3 if d["placement"] <= 4 else 1.0
        w = recency_w * place_w
        cores.append((d["core"], w))
    tally = collections.Counter()
    for core, w in cores:
        if core: tally[core] += w
    total_w = sum(tally.values()) or 1.0
    ranked = [(core, score/total_w) for core, score in tally.most_common(5)]
    return ranked  # list of (core_tuple, probability)

# 4) build lobby predictions
lobby_preds = {}
for p in p_list:
    try:
        lobby_preds[p] = predict_player_cores(p, max_ids=12)
    except Exception as e:
        lobby_preds[p] = []

# 5) contestedness: sum probability mass for each trait
trait_pressure = collections.Counter()
for p, preds in lobby_preds.items():
    if not preds: continue
    top_core, prob = preds[0]
    for t in top_core:
        trait_pressure[t] += prob

# 6) pretty print
def fmt_core(core): return " + ".join(t.replace("Set", "").replace("set", "") for t in core)

print("\n=== Likely cores per player (top 3) ===")
for p, preds in lobby_preds.items():
    tag = next((x.get("riotId", x.get("summonerName","?")) for x in participants if x.get("puuid")==p), p[:8])
    if not preds:
        print(f"- {tag}: (not enough data)")
        continue
    tops = ",  ".join([f"{fmt_core(c)} ({prob*100:.0f}%)" for c,prob in preds[:3]])
    print(f"- {tag}: {tops}")

print("\n=== Trait contestedness (lower is better / more open) ===")
if not trait_pressure:
    print("(no signal)")
else:
    # show 10 most/least contested
    items = sorted(trait_pressure.items(), key=lambda kv: kv[1], reverse=True)
    print("Most contested:")
    for t, s in items[:8]:
        print(f"  • {t}: {s:.2f} players-likely")
    print("Least contested:")
    for t, s in items[-8:]:
        print(f"  • {t}: {s:.2f} players-likely")

print("\nTip: if your target core overlaps the top 2-3 traits above, pivot to adjacent traits with lower pressure.")

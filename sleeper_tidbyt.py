#!/usr/bin/env python3
"""Sleeper fantasy matchup -> Tidbyt (64x32 LED).

Fetches live data from Sleeper (scores computed by Sleeper with the league's
own scoring) and NFL game status from ESPN, draws an animated WebP, and pushes
it to a Tidbyt as a background installation so it stays in the app rotation.

Usage:
  python sleeper_tidbyt.py                      # fetch live data, render, push if creds set
  python sleeper_tidbyt.py --fixture FILE.json  # render from saved data (testing)
  python sleeper_tidbyt.py --no-push            # render only
Env: TIDBYT_DEVICE_ID, TIDBYT_API_TOKEN, optional TIDBYT_API_URL, PLAYERS_CACHE
"""
import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw

import fonts

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(HERE, "config.json")))
TZ = ZoneInfo(CONFIG.get("timezone", "America/Chicago"))
SLEEPER = "https://api.sleeper.app/v1"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
W, H = 64, 32

# ---------- colors (saturated, LED friendly) ----------
WHITE = (255, 255, 255)
GRAY = (130, 130, 130)
DIM = (70, 70, 70)
FAINT = (28, 28, 28)
GREEN = (60, 255, 110)
RED = (255, 70, 70)
YELLOW = (255, 210, 60)
POS_COLORS = {
    "QB": (255, 80, 120), "RB": (40, 230, 170), "WR": (80, 170, 255),
    "TE": (255, 165, 50), "K": (180, 140, 255), "DEF": (170, 170, 170),
}
ESPN_ABBR = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR"}


# ---------- tiny text renderer ----------
def glyph_cols(font, ch):
    cols = list(font.get(ord(ch), font[ord("?")]))
    if ch == " ":
        return [0, 0] if font is fonts.TINY_FONT else [0, 0, 0]
    while cols and cols[-1] == 0:
        cols.pop()
    while cols and cols[0] == 0:
        cols.pop(0)
    return cols or [0]


# Each font: (table, top_row_offset) so y means the first visible pixel row.
TINY = (fonts.TINY_FONT, 1, 5)   # 5px tall
BIG = (fonts.LCD_FONT, 0, 7)     # 7px tall


def text_width(font, s):
    tbl = font[0]
    return sum(len(glyph_cols(tbl, c)) + 1 for c in s) - 1 if s else 0


def draw_text(img, font, s, x, y, color):
    tbl, off, _ = font
    px = img.load()
    for ch in s:
        for col in glyph_cols(tbl, ch):
            for b in range(8):
                if col >> b & 1:
                    xx, yy = x, y + b - off
                    if 0 <= xx < W and 0 <= yy < H:
                        px[xx, yy] = color
            x += 1
        x += 1
    return x


def fit(font, s, max_w):
    while s and text_width(font, s) > max_w:
        s = s[:-1]
    return s.rstrip(" .")


# Thick 2px-stroke digits for the big matchup scores (6x10; "1" is 4 wide).
BOLD_DIGITS = {
    "0": [".####.", "######", "##..##", "##..##", "##..##", "##..##", "##..##", "##..##", "######", ".####."],
    "1": ["..##", ".###", "####", "..##", "..##", "..##", "..##", "..##", "..##", "..##"],
    "2": [".####.", "######", "##..##", "....##", "...###", "..###.", ".###..", "###...", "######", "######"],
    "3": [".####.", "######", "##..##", "....##", "..###.", "..####", "....##", "##..##", "######", ".####."],
    "4": ["...###", "..####", ".##.##", "##..##", "##..##", "######", "######", "....##", "....##", "....##"],
    "5": ["######", "######", "##....", "#####.", "######", "....##", "....##", "##..##", "######", ".####."],
    "6": [".####.", "######", "##....", "#####.", "######", "##..##", "##..##", "##..##", "######", ".####."],
    "7": ["######", "######", "....##", "...##.", "...##.", "..##..", "..##..", "..##..", "..##..", "..##.."],
    "8": [".####.", "######", "##..##", "##..##", ".####.", "######", "##..##", "##..##", "######", ".####."],
    "9": [".####.", "######", "##..##", "##..##", "######", ".#####", "....##", "....##", "######", ".####."],
    ".": ["..", "..", "..", "..", "..", "..", "..", "..", "##", "##"],
    "-": ["....", "....", "....", "....", "####", "####", "....", "....", "....", "...."],
}
BOLD_H = 10


def bold_width(s):
    return sum(len(BOLD_DIGITS[c][0]) + 1 for c in s) - 1 if s else 0


def draw_bold(img, s, x, y, color):
    px = img.load()
    for ch in s:
        g = BOLD_DIGITS[ch]
        for r, row in enumerate(g):
            for c, v in enumerate(row):
                if v == "#" and 0 <= x + c < W and 0 <= y + r < H:
                    px[x + c, y + r] = color
        x += len(g[0]) + 1
    return x


def fmt_pts(p):
    return f"{p:.1f}"


# ---------- data ----------
def get(url, **kw):
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=20, **kw)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)


def load_players(ids):
    """Sleeper asks that /players/nfl (~5MB) be fetched at most daily; cache it."""
    path = os.environ.get("PLAYERS_CACHE", os.path.join(HERE, ".cache", "players.json"))
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < 20 * 3600
    if not fresh:
        try:
            data = get(f"{SLEEPER}/players/nfl")
            slim = {k: {f: v.get(f) for f in ("first_name", "last_name", "position", "team", "injury_status")}
                    for k, v in data.items()}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump(slim, open(path, "w"))
        except Exception as e:
            if not os.path.exists(path):
                raise
            print("players refresh failed, using cache:", e)
    allp = json.load(open(path))
    return {i: allp.get(i, {}) for i in ids}


def fetch_live():
    lid = CONFIG["league_id"]
    state = get(f"{SLEEPER}/state/nfl")
    week = state.get("display_week") or state.get("week") or 1
    league = get(f"{SLEEPER}/league/{lid}")
    users = get(f"{SLEEPER}/league/{lid}/users")
    rosters = get(f"{SLEEPER}/league/{lid}/rosters")
    matchups = get(f"{SLEEPER}/league/{lid}/matchups/{week}") or []
    ids = {p for m in matchups for p in (m.get("starters") or [])}
    players = load_players(ids)
    try:
        espn = get(ESPN, params={"week": week, "seasontype": 3 if state.get("season_type") == "post" else 2,
                                 "dates": state.get("season")})
    except Exception as e:
        print("ESPN unavailable (game status only):", e)
        espn = {"events": []}
    try:
        pos = "&".join(f"position[]={p}" for p in ("QB", "RB", "WR", "TE", "K", "DEF"))
        stype = "post" if state.get("season_type") == "post" else "regular"
        projections = get(f"https://api.sleeper.app/projections/nfl/{state.get('season')}/{week}?season_type={stype}&{pos}")
    except Exception as e:
        print("Projections unavailable:", e)
        projections = None
    return dict(state=state, league=league, users=users, rosters=rosters,
                matchups=matchups, players=players, espn=espn, projections=projections)


def next_kickoff(espn):
    """Earliest not-yet-started kickoff (aware datetime) or None."""
    best = None
    for ev in espn.get("events", []):
        if ev.get("status", {}).get("type", {}).get("state") != "pre":
            continue
        try:
            k = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        except Exception:
            continue
        best = k if best is None or k < best else best
    return best


def game_status(espn):
    """team abbr -> dict(state=pre|in|post, label=short text)"""
    out = {}
    for ev in espn.get("events", []):
        st = ev.get("status", {})
        state = st.get("type", {}).get("state", "pre")
        remaining = 1.0 if state == "pre" else 0.0
        if state == "in":
            per = st.get("period", 0)
            try:
                mm, ss = (st.get("displayClock") or "0:00").split(":")
                clock = int(mm) + int(ss) / 60
            except ValueError:
                clock = 0
            remaining = 0.0 if per > 4 else max(0.0, min(1.0, ((4 - per) * 15 + clock) / 60))
            if "half" in st.get("type", {}).get("shortDetail", "").lower():
                remaining = 0.5
            label = "HALF" if "half" in st.get("type", {}).get("shortDetail", "").lower() else (f"Q{per}" if per <= 4 else "OT")
        elif state == "post":
            label = "FIN"
        else:
            try:
                kick = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(TZ)
                h = kick.hour % 12 or 12
                label = (f"{h}:{kick.minute:02d}" if kick.minute else f"{h}") + ("P" if kick.hour >= 12 else "A")
                if kick.date() != dt.datetime.now(TZ).date():
                    label = kick.strftime("%a").upper()[:3]
            except Exception:
                label = "PRE"
        for c in ev.get("competitions", [{}])[0].get("competitors", []):
            ab = c.get("team", {}).get("abbreviation", "")
            out[ESPN_ABBR.get(ab, ab)] = {"state": state, "label": label, "remaining": remaining}
    return out


STOP = {"THE", "WILL", "YOU", "HE", "SHE", "WAY", "OF", "AND", "MY", "TEAM", "FC"}


def short_name(team_name, display_name, override=None):
    if override:
        return override.upper()
    words = re.findall(r"[A-Za-z]+", team_name or "")
    for w in words:
        if len(w) >= 3 and w.upper() not in STOP:
            return w.upper()
    return re.sub(r"[^A-Za-z0-9]", "", display_name or "TEAM").upper()


def build_model(d, now=None):
    now = now or dt.datetime.now(TZ)
    owners = {}
    for r in d["rosters"]:
        for o in [r.get("owner_id")] + (r.get("co_owners") or []):
            if o:
                owners[o] = r["roster_id"]
    users = {u["user_id"]: u for u in d["users"]}
    my_user = next((u for u in d["users"] if u.get("display_name", "").lower() == CONFIG["sleeper_username"].lower()), None)
    model = {"week": d["state"].get("display_week") or d["state"].get("week"), "updated": now, "matchup": None}
    if not my_user or my_user["user_id"] not in owners:
        model["error"] = "TEAM NOT FOUND"
        return model
    my_rid = owners[my_user["user_id"]]
    me = next((m for m in d["matchups"] if m["roster_id"] == my_rid), None)
    if not me or me.get("matchup_id") is None:
        return model  # bye / offseason / eliminated
    opp = next((m for m in d["matchups"] if m.get("matchup_id") == me["matchup_id"] and m["roster_id"] != my_rid), None)
    rid_user = {}
    for r in d["rosters"]:
        rid_user[r["roster_id"]] = users.get(r.get("owner_id"), {})
    games = game_status(d.get("espn") or {})
    scoring = (d.get("league") or {}).get("scoring_settings") or {}
    proj_pts = None
    if d.get("projections") and scoring:
        proj_pts = {}
        for row in d["projections"]:
            st_ = row.get("stats") or {}
            proj_pts[row.get("player_id")] = sum(scoring.get(k, 0) * (v or 0) for k, v in st_.items()
                                                 if isinstance(v, (int, float)))

    def live_proj(m):
        """Same idea as the Sleeper app: finished = actual, not started = projection,
        in progress = actual + projection for the time left. Out/bye/empty = actual."""
        if proj_pts is None:
            return None
        total = 0.0
        sp = m.get("starters_points") or []
        for i, pid in enumerate(m.get("starters") or []):
            actual = float(sp[i] if i < len(sp) and sp[i] is not None else 0)
            p = d["players"].get(pid, {})
            g = games.get(p.get("team") if p.get("position") != "DEF" else pid)
            inj = (p.get("injury_status") or "").upper()
            pj = proj_pts.get(pid, 0.0)
            if not pid or pid == "0" or g is None:
                total += actual
            elif g["state"] == "pre":
                total += 0.0 if inj in ("OUT", "IR", "SUS", "PUP", "NA", "DNR") else pj
            else:
                total += actual + pj * g["remaining"]
        return total

    overrides = CONFIG.get("team_short_names", {})

    def team(m):
        u = rid_user.get(m["roster_id"], {})
        pts = m.get("custom_points") if m.get("custom_points") is not None else m.get("points") or 0
        return {"name": short_name((u.get("metadata") or {}).get("team_name"), u.get("display_name"),
                                   overrides.get(u.get("display_name", ""))),
                "points": float(pts), "proj": live_proj(m)}

    starters = []
    for i, pid in enumerate(me.get("starters") or []):
        pts = (me.get("starters_points") or [0] * 20)[i] if i < len(me.get("starters_points") or []) else 0
        if not pid or pid == "0":
            starters.append({"name": "EMPTY SLOT", "pos": "--", "team": "", "pts": 0.0, "status": "", "state": "none"})
            continue
        p = d["players"].get(pid, {})
        pos = p.get("position") or "?"
        if pos == "DEF":
            name, tm = pid, pid
        else:
            last = re.sub(r"\s+(JR\.?|SR\.?|II|III|IV|V)$", "", (p.get("last_name") or pid).upper())
            name, tm = last, p.get("team") or "FA"
        g = games.get(tm)
        inj = (p.get("injury_status") or "").upper()
        if g is None:
            state, status = "bye", "BYE"
        else:
            state, status = g["state"], g["label"]
        if state == "pre" and inj in ("OUT", "IR", "SUS", "PUP", "NA", "DNR"):
            status, state = ("OUT" if inj != "SUS" else "SUS"), "out"
        starters.append({"name": name, "pos": pos, "team": tm, "pts": float(pts or 0), "status": status, "state": state})

    states = [s["state"] for s in starters]
    any_live = any(g["state"] == "in" for g in games.values())
    if any_live:
        phase = "LIVE"
    elif games and all(g["state"] == "post" for g in games.values()):
        phase = "FINAL"
    elif not games:
        phase = ""
    else:
        phase = "PRE" if all(g["state"] == "pre" for g in games.values()) else "BREAK"
    model.update(matchup={"me": team(me), "opp": team(opp) if opp else {"name": "BYE", "points": 0.0, "proj": None}},
                 starters=starters, phase=phase, mine_left=sum(s in ("pre",) for s in states))
    return model


# ---------- screens ----------
def blank():
    return Image.new("RGB", (W, H), (0, 0, 0))


def screen_overview(m):
    """Side-by-side: names, big live scores (leader green), projected totals."""
    img = blank()
    mu = m["matchup"]
    me, op = mu["me"], mu["opp"]
    # header: WEEK 9 . LIVE (centered), update time far right
    t = m["updated"]
    clock = f"{t.hour % 12 or 12}:{t.minute:02d}"
    tag = {"LIVE": ("LIVE", RED), "FINAL": ("FINAL", YELLOW)}.get(m["phase"])
    head = f"WEEK {m['week']}"
    hw = text_width(TINY, head) + (text_width(TINY, tag[0]) + 6 if tag else 0)
    x = (W - text_width(TINY, clock) - 2 - hw) // 2
    x = draw_text(img, TINY, head, max(0, x), 0, GRAY)
    if tag:
        img.putpixel((x + 1, 2), DIM)
        draw_text(img, TINY, tag[0], x + 4, 0, tag[1])
    draw_text(img, TINY, clock, W - text_width(TINY, clock), 0, DIM)
    # divider
    d = ImageDraw.Draw(img)
    d.line([(31, 8), (31, 30)], fill=FAINT)
    diff = round(me["points"] - op["points"], 2)
    for t_, x0, col_name in ((me, 0, WHITE), (op, 33, GRAY)):
        half = 31
        lead = (t_ is me and diff > 0) or (t_ is op and diff < 0)
        col = GREEN if lead else (YELLOW if diff == 0 and me["points"] > 0 else WHITE)
        nm = fit(TINY, t_["name"], half - 1)
        draw_text(img, TINY, nm, x0 + (half - text_width(TINY, nm)) // 2, 8, col_name)
        s_ = fmt_pts(t_["points"])
        if all(ch in BOLD_DIGITS for ch in s_) and bold_width(s_) <= half:
            draw_bold(img, s_, x0 + (half - bold_width(s_)) // 2, 14, col)
        else:
            draw_text(img, BIG, s_, x0 + (half - text_width(BIG, s_)) // 2, 15, col)
        if t_.get("proj") is not None:
            p = "P" + fmt_pts(t_["proj"])
            draw_text(img, TINY, p, x0 + (half - text_width(TINY, p)) // 2, 27, GRAY)
    return img


PTS_COL = {"in": YELLOW, "post": WHITE, "pre": DIM, "bye": DIM, "out": DIM, "none": DIM}
STATUS_COL = {"in": YELLOW, "post": GRAY, "pre": GRAY, "bye": GRAY, "out": RED, "none": RED}


def draw_player(img, p, y):
    pts = fmt_pts(p["pts"])
    if all(ch in BOLD_DIGITS for ch in pts) and bold_width(pts) <= 26:
        pw = bold_width(pts)
        draw_bold(img, pts, W - pw, y + 2, PTS_COL.get(p["state"], WHITE))
    else:
        pw = text_width(BIG, pts)
        draw_text(img, BIG, pts, W - pw, y + 4, PTS_COL.get(p["state"], WHITE))
    left_max = W - pw - 3
    draw_text(img, TINY, fit(TINY, p["name"], left_max), 0, y + 1, WHITE)
    x = draw_text(img, TINY, p["pos"][:3], 0, y + 8, POS_COLORS.get(p["pos"], GRAY))
    if p["team"] and p["team"] != p["name"]:
        x = draw_text(img, TINY, p["team"], x + 2, y + 8, GRAY)
    st = p["status"]
    if x + 3 + text_width(TINY, st) <= left_max:
        draw_text(img, TINY, st, x + 3, y + 8, STATUS_COL.get(p["state"], GRAY))


def screen_players(pair):
    img = blank()
    draw_player(img, pair[0], 0)
    if len(pair) > 1:
        d = ImageDraw.Draw(img)
        d.line([(0, 15), (W - 1, 15)], fill=FAINT)
        draw_player(img, pair[1], 17)
    return img


def screen_message(title, sub):
    img = blank()
    draw_text(img, TINY, "SLEEPER", (W - text_width(TINY, "SLEEPER")) // 2, 3, (80, 200, 255))
    draw_text(img, BIG, title, max(0, (W - text_width(BIG, title)) // 2), 12, WHITE)
    draw_text(img, TINY, sub, max(0, (W - text_width(TINY, sub)) // 2, ), 24, GRAY)
    return img


def build_screens(m):
    if m.get("error"):
        return [(screen_message("ERROR", m["error"]), 10000)]
    if not m.get("matchup"):
        return [(screen_message("NO GAME", f"WEEK {m['week']}"), 10000)]
    screens = [(screen_overview(m), CONFIG.get("overview_ms", 4000))]
    st = m["starters"]
    for i in range(0, len(st), 2):
        screens.append((screen_players(st[i:i + 2]), CONFIG.get("players_ms", 3000)))
    return screens


def animate(screens, step_ms=40, steps=8):
    """Frames with a quick vertical slide between screens."""
    frames, durs = [], []
    for i, (img, ms) in enumerate(screens):
        frames.append(img)
        durs.append(ms)
        if len(screens) > 1:
            nxt = screens[(i + 1) % len(screens)][0]
            if i == len(screens) - 1:
                break
            for s in range(1, steps):
                off = round(H * s / steps)
                f = blank()
                f.paste(img.crop((0, off, W, H)), (0, 0))
                f.paste(nxt.crop((0, 0, W, off)), (0, H - off))
                frames.append(f)
                durs.append(step_ms)
    return frames, durs


def save_webp(frames, durs, path):
    frames[0].save(path, format="WEBP", save_all=True, append_images=frames[1:], duration=durs,
                   loop=0, lossless=True, quality=100, method=6)


def preview_png(frames, durs, path, scale=8):
    """Static sheet of each held screen, drawn as round LEDs like the real device."""
    held = [f for f, d in zip(frames, durs) if d > 100]
    pad = 12
    sheet = Image.new("RGB", (W * scale + pad * 2, (H * scale + pad) * len(held) + pad), (15, 15, 15))
    for n, f in enumerate(held):
        oy = pad + n * (H * scale + pad)
        d = ImageDraw.Draw(sheet)
        d.rectangle([pad, oy, pad + W * scale - 1, oy + H * scale - 1], fill=(0, 0, 0))
        px = f.load()
        for yy in range(H):
            for xx in range(W):
                c = px[xx, yy]
                if c == (0, 0, 0):
                    c = (12, 12, 12)
                x0, y0 = pad + xx * scale + 1, oy + yy * scale + 1
                d.ellipse([x0, y0, x0 + scale - 2, y0 + scale - 2], fill=c)
    sheet.save(path)


def push(webp_path, installation_id):
    dev, tok = os.environ.get("TIDBYT_DEVICE_ID"), os.environ.get("TIDBYT_API_TOKEN")
    if not dev or not tok:
        print("No Tidbyt credentials set; skipping push.")
        return False
    base = (os.environ.get("TIDBYT_API_URL") or "https://api.tidbyt.com").rstrip("/")
    body = {"image": base64.b64encode(open(webp_path, "rb").read()).decode(),
            "installationID": installation_id, "background": True}
    r = requests.post(f"{base}/v0/devices/{dev}/push", json=body,
                      headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    print("Tidbyt push:", r.status_code, r.text[:200])
    r.raise_for_status()
    return True


def run_once(a):
    data = json.load(open(a.fixture)) if a.fixture else fetch_live()
    now = dt.datetime.fromisoformat(a.now).astimezone(TZ) if a.now else None
    model = build_model(data, now)
    mu = model.get("matchup") or {}
    print(dt.datetime.now(TZ).strftime("%H:%M:%S"), model.get("phase"), {k: (v.get("points"), round(v["proj"], 2) if v.get("proj") is not None else None) for k, v in mu.items()})
    screens = build_screens(model)
    # Tidbyt cuts pushed apps off at 15s, so split the screens into apps that each fit.
    budget = CONFIG.get("max_app_ms", 14500)
    apps, cur, used = [], [], 0
    for scr in screens:
        need = scr[1] + (280 if cur else 0)
        if cur and used + need > budget:
            apps.append(cur)
            cur, used = [], 0
            need = scr[1]
        cur.append(scr)
        used += need
    apps.append(cur)
    n_apps = CONFIG.get("app_count", 2)
    while len(apps) < n_apps:          # keep every installation fresh (e.g. bye week)
        apps.append(apps[-1])
    all_frames, all_durs = [], []
    paths = []
    for i, app in enumerate(apps):
        frames, durs = animate(app)
        p = os.path.join(a.out, f"sleeper{i + 1}.webp")
        save_webp(frames, durs, p)
        paths.append(p)
        all_frames += frames
        all_durs += durs
        print(f"App {i + 1}: {len(frames)} frames, {sum(durs)/1000:.1f}s, {os.path.getsize(p)} bytes")
    all_frames[0].save(os.path.join(a.out, "preview.gif"), save_all=True, append_images=all_frames[1:],
                       duration=all_durs, loop=0)
    preview_png(all_frames, all_durs, os.path.join(a.out, "preview.png"))
    if not a.no_push:
        base_id = CONFIG.get("installation_id", "sleeperff")
        for i, p in enumerate(paths):
            push(p, base_id if i == 0 else f"{base_id}{i + 1}")
    return model, data


def keep_going(model, data):
    """True while a game is live or one kicks off within the next hour."""
    if model.get("phase") == "LIVE":
        return True
    k = next_kickoff((data or {}).get("espn") or {})
    return bool(k and k - dt.datetime.now(dt.timezone.utc) < dt.timedelta(minutes=60))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture")
    ap.add_argument("--now", help="ISO time to pretend it is (testing)")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--loop", action="store_true", help="refresh every --interval seconds while games are on")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--max-minutes", type=float, default=345)
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if not a.loop:
        run_once(a)
        return 0
    start = time.time()
    while True:
        t0 = time.time()
        try:
            model, data = run_once(a)
            go = keep_going(model, data)
        except Exception as e:  # network blip: keep the last image, try again next minute
            print("Update failed, will retry:", repr(e))
            go = True
        if not go:
            print("No live or upcoming games; stopping until the next scheduled start.")
            return 0
        if (time.time() - start) / 60 > a.max_minutes:
            print("Reached max run time; the next scheduled run takes over.")
            return 0
        time.sleep(max(5, a.interval - (time.time() - t0)))

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Road Work SA - refresh job.

Pulls TxDOT's DriveTexas road-work feed with the owner's free API key and writes the
data files the app reads:
  data/sa.json     San Antonio District counties (loaded first)
  data/texas.json  every TxDOT work zone / closure statewide

Usage:
  python3 refresh.py --key KEY --out DIR          download and write data files
  python3 refresh.py --feed FILE --out DIR        parse a saved feed file (testing)
  python3 refresh.py --counties-only --out DIR    write tx-counties.json (map outlines)

Exit codes: 0 ok, 2 key rejected, 3 network/host blocked, 4 feed unreadable.
"""
import argparse, json, os, re, ssl, sys, urllib.request, urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

COUNTIES_URL = "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"
WZDX_URL = "https://api.drivetexas.org/api/conditions.wzdx.geojson?key={}"
FULL_URL = "https://api.drivetexas.org/api/conditions.geojson?key={}"
SA = ["Atascosa", "Bandera", "Bexar", "Comal", "Frio", "Guadalupe", "Kendall",
      "Kerr", "McMullen", "Medina", "Uvalde", "Wilson"]


class FetchError(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def get(url, timeout=120):
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    req = urllib.request.Request(url, headers={"User-Agent": "RoadWorkSA/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8-sig"))
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code} from {url.split('?')[0]}", 2 if e.code in (401, 403) else 3)
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"Could not reach {url.split('?')[0]}: {e}", 3)
    except ValueError as e:
        raise FetchError(f"Feed was not valid JSON: {e}", 4)


# ---------- geometry ----------
def simplify(pts, tol):
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]; bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        best, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if L == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, i
        if idx > 0 and best > tol * tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


def flat_coords(geom):
    if not geom:
        return []
    t, c = geom.get("type"), geom.get("coordinates")
    try:
        if t == "Point":
            return [c[:2]]
        if t in ("LineString", "MultiPoint"):
            return [p[:2] for p in c]
        if t == "MultiLineString":
            return [p[:2] for line in c for p in line]
        if t == "GeometryCollection":
            return [p for g in geom.get("geometries", []) for p in flat_coords(g)]
    except (TypeError, IndexError):
        pass
    return []


def midpoint(pts):
    if len(pts) == 1:
        return pts[0]
    segs = [((pts[i][0] - pts[i + 1][0]) ** 2 + (pts[i][1] - pts[i + 1][1]) ** 2) ** .5 for i in range(len(pts) - 1)]
    half, run = sum(segs) / 2, 0.0
    for i, s in enumerate(segs):
        if run + s >= half and s > 0:
            t = (half - run) / s
            return [pts[i][0] + t * (pts[i + 1][0] - pts[i][0]), pts[i][1] + t * (pts[i + 1][1] - pts[i][1])]
        run += s
    return pts[len(pts) // 2]


def load_counties(tol=0.0, digits=4):
    d = get(COUNTIES_URL)
    out = []
    for f in d["features"]:
        p = f["properties"]
        if p.get("STATE") != "48":
            continue
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        rings = []
        for poly in polys:
            ring = [[x, y] for x, y in poly[0]]
            if tol:
                ring = simplify(ring, tol)
            rings.append([[round(x, digits), round(y, digits)] for x, y in ring])
        xs = [x for r in rings for x, _ in r]
        ys = [y for r in rings for _, y in r]
        out.append({"n": p["NAME"], "b": [min(xs), min(ys), max(xs), max(ys)], "r": rings})
    out.sort(key=lambda c: c["n"])
    return out


def in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]; xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def county_of(pt, counties):
    x, y = pt
    for c in counties:
        b = c["b"]
        if b[0] <= x <= b[2] and b[1] <= y <= b[3] and any(in_ring(x, y, r) for r in c["r"]):
            return c["n"]
    return ""


# ---------- text helpers ----------
def clean(s):
    s = re.sub(r"<br\s*/?>", " ", str(s or ""), flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip(" -")


ROAD = [(r"^PA", "PA "), (r"^IH", "I-"), (r"^SL", "Loop "), (r"^SS", "Spur "), (r"^US", "US "), (r"^SH", "SH "),
        (r"^FM", "FM "), (r"^RM", "RM "), (r"^BU|^BS|^BI", "Bus. "), (r"^PR", "Park Rd "), (r"^RE", "Rec Rd ")]


def road_name(r):
    r = clean(r).upper()
    m = re.match(r"^(IH|SL|SS|US|SH|FM|RM|BU|BS|BI|PR|RE|PA)[\s-]*0*(\d+)\s*([A-Z]?)$", r)
    if m:
        for pat, rep in ROAD:
            if re.match(pat, m.group(1)):
                return f"{rep}{m.group(2)}{m.group(3)}"
    m = re.match(r"^(LOOP|SPUR)\s*0*(\d+)$", r)
    if m:
        return f"{m.group(1).title()} {m.group(2)}"
    return clean(r).title() if r.isupper() and not re.match(r"^[A-Z]{1,3}[\s-]?\d", r) else clean(r)


DIRS = {"northbound": "NB", "southbound": "SB", "eastbound": "EB", "westbound": "WB",
        "inner-loop": "Inner loop", "outer-loop": "Outer loop", "both": "Both directions",
        "N": "NB", "S": "SB", "E": "EB", "W": "WB", "B": "Both directions"}


def iso(v):
    if v in (None, "", 0):
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        s = str(v).strip()
        if re.fullmatch(r"\d{10,13}", s):
            return iso(int(s))
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:  # no offset given: TxDOT local (Central) time
            d = d.replace(tzinfo=ZoneInfo("America/Chicago"))
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return None


FULL_RE = re.compile(r"full(y)? clos|road (is )?closed|all lanes (are )?closed|closed in both directions|"
                     r"mainlanes? (are |will be )?closed|full mainlane closure", re.I)
LANE_RE = re.compile(r"lane(s)? clos|lanes? (is |are |will be )?closed|alternating|ramp (is |will be )?closed|"
                     r"ramp closure|frontage road closure|one lane|lane reduction|shift", re.I)


def classify(impact, text):
    impact = (impact or "unknown").lower()
    if impact == "all-lanes-closed":
        return "closed"
    if "closed" in impact or impact in ("alternating-one-way", "flagging"):
        return "lanes"
    if impact.startswith("all-lanes-open"):
        return "work"
    if FULL_RE.search(text):
        return "closed"
    if LANE_RE.search(text):
        return "lanes"
    return "work"


def line_of(geom):
    pts = [[float(x), float(y)] for x, y in flat_coords(geom) if x is not None and y is not None]
    pts = [p for p in pts if -107.5 < p[0] < -93 and 25.5 < p[1] < 37]  # inside Texas
    if not pts:
        return None, None
    mid = midpoint(pts)
    s = simplify(pts, 0.0004)
    while len(s) > 40:
        s = s[::2] + ([s[-1]] if len(s) % 2 == 0 else [])
    return [[round(x, 5), round(y, 5)] for x, y in s], [round(mid[0], 5), round(mid[1], 5)]


# ---------- TxDOT notes ----------
# TxDOT descriptions start with standard notes ("- Alternating lanes closed.<br/>") followed by free text.
# Its vehicle_impact is "all-lanes-closed" for most entries, even ramp or lane work, so the notes decide.
TAGS = [(r"main lanes closed", "Main lanes closed"), (r"alternating lanes", "Alternating lanes"),
        (r"main lanes (not affected|are open)", "Main lanes open"), (r"exit ramp closed", "Exit ramp closed"),
        (r"entrance ramp closed", "Entrance ramp closed"), (r"right lane closed", "Right lane closed"),
        (r"left lane closed", "Left lane closed"), (r"center lane closed", "Center lane closed"),
        (r"multiple (left |right )?lane", "Multiple lanes closed"), (r"travel lanes reduced to one lane", "Down to one lane"),
        (r"two-way traffic control|one-way traffic|one way traffic", "One lane, two-way traffic"),
        (r"daytime closure only", "Daytime only"), (r"night(time)? (work|closure) only", "Nights only"),
        (r"expect delays|delays due to", "Expect delays"), (r"use alternate route", "Use alternate route"),
        (r"follow the marked detour|detour in place|detour to the frontage", "Detour posted"),
        (r"flag personnel", "Flaggers"), (r"pilot car", "Pilot car"), (r"travel discouraged", "Travel discouraged"),
        (r"rough roadway", "Rough road")]


def split_desc(d):
    d = str(d or "")
    parts = re.split(r"(?:<br\s*/?>\s*){3,}", d, maxsplit=1)
    head = parts[0] if len(parts) > 1 or d.lstrip().startswith("-") else ""
    note = parts[1] if len(parts) > 1 else ("" if head else d)
    notes = [clean(b).strip(" .") for b in re.split(r"<br\s*/?>", head)]
    return [n for n in notes if n], clean(note)


def txdot_kind(impact, notes, note, name):
    t = " | ".join(notes).lower()
    x = f"{t} | {note} | {name}".lower()
    side = x.replace("main lanes", "")
    when = " at night" if re.search(r"night(time)? (work|closure) only", t) else " in daytime" if "daytime closure only" in t else ""
    if re.search(r"main lanes closed|lanes closed to traffic, detour|detour to the frontage", t):
        return "closed", ("Main lanes closed" + when) if when or "alternating" not in t else "Main lanes, alternating closures"
    if re.search(r"ramp (closed|only)|connector ramp", t):
        return "lanes", "Ramp closed"
    if "roadway is closed" in t:
        return "closed", ("Road closed" + when) if when else "Alternating closures" if "alternating" in t else "Road closed"
    if re.search(r"turn-?around|urnaround|u-turn", x) and (impact == "all-lanes-closed" or re.search(r"not affected|main lanes are open", t)):
        return "lanes", "Turnaround closed"
    if "frontage road only" in t or re.search(r"frontage|\bfr\b|wbfr|ebfr|nbfr|sbfr", f"{note} {name}".lower()) and "not affected" in t:
        if re.search(r"all lanes of|frontage road closure|full closure", side):
            return "lanes", "Frontage road closed"
        if re.search(r"lane (is )?closed|lane closure|lanes closed|alternating|one lane", side):
            return "lanes", "Frontage lane closed"
        return "lanes", "Frontage road closed" if impact == "all-lanes-closed" else "Frontage lane closed"
    if re.search(r"reduced to one lane, two-way|one-way traffic|one way traffic|pilot car|flag personnel", t):
        return "lanes", "One lane, take turns"
    if re.search(r"alternating lanes|lane closed|lanes closed|lane closures|reduced to one lane", t):
        return "lanes", "Lane closure"
    if "not affected" in t:
        return "lanes", "Side road closed"
    if impact == "all-lanes-closed":
        return "closed", "Road closed"
    k = classify(impact, f"{t} {note}")
    return k, {"closed": "Road closed", "lanes": "Lane closure", "work": "Work zone"}[k]


# ---------- feed formats ----------
def norm_wzdx(f):
    p = f.get("properties") or {}
    core = p.get("core_details") or p
    et = (core.get("event_type") or "work-zone").lower()
    if et not in ("work-zone", "detour", "restriction"):
        return None
    roads = core.get("road_names") or ([core["road_name"]] if core.get("road_name") else [])
    impact = p.get("vehicle_impact") or "unknown"
    notes, note = split_desc(core.get("description"))
    name = clean(core.get("name") or "")
    kind, label = txdot_kind(impact, notes, note, name)
    if et == "detour" and kind == "work":
        kind, label = "closed", "Detour"
    if name and name.lower() != "construction" and name.lower() not in note.lower():
        note = f"{name}. {note}".strip(" .") + ("." if note else "")
    tags = [lab for pat, lab in TAGS if re.search(pat, " ".join(notes).lower())]
    desc = note
    lanes = p.get("lanes") or []
    work = [str(t.get("type_name", "")).replace("-", " ") for t in (p.get("types_of_work") or []) if t.get("type_name")]
    wp = p.get("worker_presence") or {}
    spd = p.get("reduced_speed_limit_kph")
    return {
        "id": str(f.get("id") or p.get("road_event_id") or ""),
        "roads": [road_name(r) for r in roads if r],
        "dir": DIRS.get(str(core.get("direction") or "").lower(), ""),
        "desc": desc,
        "from": road_name(p.get("beginning_cross_street") or ""),
        "to": road_name(p.get("ending_cross_street") or ""),
        "start": iso(p.get("start_date")), "end": iso(p.get("end_date")),
        "kind": kind, "label": label, "tags": tags,
        "closedLanes": sum(1 for l in lanes if l.get("status") == "closed"),
        "totalLanes": sum(1 for l in lanes if l.get("type") in ("general", "exit-lane", "exit-ramp", "entrance-lane", "entrance-ramp")),
        "work": work,
        "workers": True if wp.get("are_workers_present") is True else None,
        "mph": round(spd / 1.609) if isinstance(spd, (int, float)) else None,
    }


def pick(p, *keys):
    low = {k.lower(): v for k, v in p.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return v
    return None


TYPE_CODES = {"C": "Construction", "Z": "Closed", "A": "Accident", "D": "Damage", "I": "Snow/Ice",
              "F": "Flooding", "X": "Evaculane", "Y": "Contraflow", "O": "Other"}


def truthy(v):
    return str(v).strip().lower() in ("y", "yes", "true", "1")


def norm_native(f):
    p = f.get("properties") or {}
    typ = str(pick(p, "type", "condition", "conditionType", "CNSTRNT_TYPE_CD", "CNSTRT_TYPE_CD") or "")
    typ = TYPE_CODES.get(typ.upper(), typ)
    if not re.search(r"construct|clos|work", typ, re.I):
        return None  # keep road work and closures; skip crashes, weather, etc.
    desc = clean(pick(p, "description", "COND_DSCR", "conditionDescription") or "")
    road = pick(p, "RTE_NM", "routeName", "route") or ""
    rdway = pick(p, "RDWAY_NM", "roadwayName", "roadway") or ""
    text = f"{typ} {desc}"
    kind = "closed" if re.search(r"^clos", typ, re.I) else classify("unknown", text)
    return {
        "id": str(pick(p, "id", "GLOBALID", "OBJECTID", "conditionId") or f.get("id") or ""),
        "roads": list(dict.fromkeys(x for x in [road_name(road), road_name(rdway)] if x)),
        "dir": DIRS.get(str(pick(p, "TRVL_DRCT_CD", "direction", "travelDirection") or "").strip(), ""),
        "desc": desc,
        "from": clean(pick(p, "COND_LMT_FROM_DSCR", "from", "fromDescription") or ""),
        "to": clean(pick(p, "COND_LMT_TO_DSCR", "to", "toDescription") or ""),
        "start": iso(pick(p, "startTime", "COND_START_TS", "startDate")),
        "end": iso(pick(p, "endTime", "COND_END_TS", "endDate")),
        "kind": kind, "label": {"closed": "Road closed", "lanes": "Lane closure", "work": "Work zone"}[kind],
        "tags": [t for t, on in (("Detour posted", truthy(pick(p, "detourFlag", "CNSTRNT_DETOUR_FLAG")) or re.search(r"\bdetour", desc, re.I)),
                                 ("Expect delays", truthy(pick(p, "delayFlag", "CNSTRNT_DELAY_FLAG")) or re.search(r"\bdelay", desc, re.I))) if on],
        "closedLanes": 0, "totalLanes": 0,
        "work": [typ] if typ and typ != "Closed" else [], "workers": None, "mph": None,
    }


def normalize(feed, counties):
    feats = feed.get("features") or []
    info = feed.get("feed_info") or feed.get("road_event_feed_info") or {}
    is_wzdx = bool(info) or any((f.get("properties") or {}).get("core_details") for f in feats[:20])
    events, skipped = [], 0
    for f in feats:
        try:
            e = norm_wzdx(f) if is_wzdx else norm_native(f)
        except (AttributeError, TypeError, KeyError, ValueError):
            e = None
        if not e:
            skipped += 1
            continue
        e["line"], e["mid"] = line_of(f.get("geometry"))
        e["county"] = county_of(e["mid"], counties) if e["mid"] else ""
        if not e["id"]:
            e["id"] = f"e{len(events)}"
        events.append(e)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    groups = {}
    for e in events:
        if e.get("end") and e["end"] <= now:
            continue  # already over
        key = (tuple(e["roads"]), e.get("dir"), e.get("from"), e.get("to"), e.get("desc"), e.get("label"),
               json.dumps(e.get("line")))
        g = groups.get(key)
        if g:
            g["t"].append([e.get("start"), e.get("end")])
        else:
            e["t"] = [[e.get("start"), e.get("end")]]
            groups[key] = e
    out = []
    for e in groups.values():
        e["t"] = sorted({tuple(x) for x in e["t"]}, key=lambda x: x[0] or "")
        e["t"] = [list(x) for x in e["t"]]
        e.pop("start", None); e.pop("end", None)
        out.append({k: v for k, v in e.items() if v not in (None, "", [], False, 0) or k in ("kind", "roads")})
    out.sort(key=lambda e: ({"closed": 0, "lanes": 1, "work": 2}[e["kind"]], e["t"][0][0] or ""))
    return out, ("WZDx" if is_wzdx else "DriveTexas"), info.get("update_date"), skipped


def write(out, name, obj):
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, name)
    with open(path, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False)
    return os.path.getsize(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key")
    ap.add_argument("--feed")
    ap.add_argument("--out", default="out")
    ap.add_argument("--counties-only", action="store_true")
    a = ap.parse_args()
    try:
        if a.counties_only:
            size = write(a.out, "tx-counties.json", load_counties(tol=0.004, digits=3))
            print(f"tx-counties.json {size/1024:.0f} KB")
            return 0
        counties = load_counties()
        if a.feed:
            feed, url_used = json.load(open(a.feed)), "file"
        elif a.key:
            try:
                feed, url_used = get(WZDX_URL.format(a.key)), "wzdx"
            except FetchError as e:
                if e.code != 2:
                    raise
                feed, url_used = get(FULL_URL.format(a.key)), "full"
        else:
            ap.error("--key or --feed is required")
    except FetchError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return e.code
    if not isinstance(feed, dict) or not isinstance(feed.get("features"), list):
        print("FAILED: feed has no features list", file=sys.stderr)
        return 4
    events, fmt, feed_updated, skipped = normalize(feed, counties)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = {}
    for e in events:
        counts[e.get("county") or "Unknown"] = counts.get(e.get("county") or "Unknown", 0) + 1
    meta = {"updated": now, "feedUpdated": feed_updated, "format": fmt, "total": len(events), "counts": counts,
            "source": "TxDOT DriveTexas", "sourceUrl": "https://drivetexas.org/"}
    sa = [e for e in events if e.get("county") in SA]
    s1 = write(a.out, "sa.json", {**meta, "scope": "sa", "events": sa})
    s2 = write(a.out, "texas.json", {**meta, "scope": "texas", "events": events})
    kinds = {k: sum(1 for e in events if e["kind"] == k) for k in ("closed", "lanes", "work")}
    labels = {}
    for e in events:
        labels[e["label"]] = labels.get(e["label"], 0) + 1
    print(f"labels={labels}")
    print(f"OK source={url_used} format={fmt} events={len(events)} skipped={skipped} sa={len(sa)} "
          f"kinds={kinds} sa.json={s1/1024:.0f}KB texas.json={s2/1024:.0f}KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

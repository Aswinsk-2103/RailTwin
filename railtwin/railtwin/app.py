"""RailTwin backend: a small digital twin of a rail corridor that simulates trains,
tests candidate maintenance-block windows and explains the scheduling decision."""
import csv, io, itertools, json, sqlite3, functools
from flask import Flask, jsonify, request, send_from_directory, session, Response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder="static")
app.secret_key = "railtwin-dev-change-me"
DB = "railtwin.db"

STATIONS = [("Alpha Jn", 0), ("Bravo", 12), ("Charlie", 30), ("Delta Jn", 45), ("Echo", 65), ("Foxtrot", 75)]
LENGTH = 75
SECTIONS = [{"id": f"S{i+1}", "from": STATIONS[i][0], "to": STATIONS[i+1][0],
             "a": STATIONS[i][1], "b": STATIONS[i+1][1]} for i in range(len(STATIONS)-1)]
TYPES = {"express": (80, 5, "Express"), "passenger": (55, 3, "Passenger"), "freight": (40, 1, "Freight")}
HEADWAY = 6          # minutes between trains released after a block
DIV_SPEED = 30       # km/h on the diversion (loop) line
CLEAR_PENALTY = 15   # train already inside the section when the block starts
PEAKS = [(420, 600), (1020, 1200)]
WORK = {"Track renewal": 1.0, "Signal repair": 0.8, "OHE inspection": 0.6, "Bridge examination": 0.9}


def build_trains():
    trains, n = [], 1
    for kind, gap, off in (("express", 90, 10), ("passenger", 60, 25), ("freight", 120, 45)):
        for k, t in enumerate(range(off, 1440, gap)):
            d = 1 if (k % 2 == 0) else -1
            trains.append({"id": f"{kind[0].upper()}{n:02d}", "type": kind, "dir": d, "start": t,
                           "speed": TYPES[kind][0], "weight": TYPES[kind][1]})
            n += 1
    return trains


TRAINS = build_trains()


def hhmm(m):
    m = int(m) % 1440
    return f"{m//60:02d}:{m%60:02d}"


def occupancy(tr, sec):
    """Minutes at which the train enters and leaves a block section."""
    s, v = tr["start"], tr["speed"]
    if tr["dir"] == 1:
        return s + sec["a"] / v * 60, s + sec["b"] / v * 60
    return s + (LENGTH - sec["b"]) / v * 60, s + (LENGTH - sec["a"]) / v * 60


def evaluate(sec, start, dur):
    end = start + dur
    hits = []
    for tr in TRAINS:
        en, ex = occupancy(tr, sec)
        if en < end and ex > start:
            hits.append((tr, en, ex))
    delays = []
    for d in (1, -1):  # trains queue behind the block in each direction
        cursor = end
        for tr, en, ex in sorted([h for h in hits if h[0]["dir"] == d], key=lambda h: h[1]):
            if en < start:
                delay, why, act = CLEAR_PENALTY, "already inside section - must clear first", "clear"
            else:
                dep = max(en, cursor); hold = dep - en
                div = (sec["b"] - sec["a"]) * (60 / DIV_SPEED - 60 / tr["speed"])
                if div < hold:
                    delay, why, act = div, "diverted via the loop line (slower, but no waiting)", "divert"
                else:
                    delay, why, act = hold, "held at section boundary until block ends", "hold"
                    cursor = dep + HEADWAY
            delays.append({"id": tr["id"], "type": tr["type"], "dir": d, "delay": round(delay, 1),
                           "cost": round(delay * tr["weight"], 1), "why": why, "action": act, "enters": hhmm(en)})
    peak = any(start < b and end > a for a, b in PEAKS)
    cost = sum(x["cost"] for x in delays) + (40 if peak else 0)
    return {"start": start, "end": end, "label": hhmm(start), "cost": round(cost, 1), "trains": delays,
            "total_delay": round(sum(x["delay"] for x in delays), 1), "peak": peak}


@app.get("/api/network")
def network():
    return jsonify(stations=STATIONS, sections=SECTIONS, trains=TRAINS, length=LENGTH, peaks=PEAKS)


def analyse(sec, dur, work):
    cands = [evaluate(sec, s, dur) for s in range(0, 1440 - dur + 1, 15)]
    for c in cands:
        c["cost"] = round(c["cost"] * WORK.get(work, 1.0), 1)
    ranked = sorted(cands, key=lambda c: c["cost"])
    best = ranked[0]
    return dict(section=sec, duration=dur, work=work, candidates=cands, best=best["start"],
                explanation=explain(sec, dur, work, best, ranked, cands))


@app.post("/api/plan")
def plan():
    p = request.get_json(force=True)
    sec = next((s for s in SECTIONS if s["id"] == p.get("section")), SECTIONS[0])
    return jsonify(analyse(sec, max(30, min(300, int(p.get("duration", 120)))), p.get("work", "Track renewal")))


@app.post("/api/auto")
def auto():
    """Planner gives only a duration: RailTwin picks the section and time with least disruption."""
    dur = max(30, min(300, int(request.get_json(force=True).get("duration", 120))))
    res = [analyse(sec, dur, "Track renewal") for sec in SECTIONS]
    for r in res:
        r["_c"] = min(c["cost"] for c in r["candidates"])
    win = min(res, key=lambda r: r["_c"])
    ranking = sorted([{"section": r["section"]["id"], "route": f"{r['section']['from']} - {r['section']['to']}",
                       "cost": r["_c"], "km": r["section"]["b"] - r["section"]["a"]} for r in res], key=lambda x: x["cost"])
    out = {k: v for k, v in win.items() if k != "_c"}
    out["ranking"] = ranking
    return jsonify(out)


def explain(sec, dur, work, best, ranked, cands):
    avg = sum(c["cost"] for c in cands) / len(cands)
    nb = len(best["trains"])
    by = {}
    for t in best["trains"]:
        by[t["type"]] = by.get(t["type"], 0) + 1
    mix = ", ".join(f"{v} {k}" for k, v in by.items()) or "no trains"
    steps = [
        f"The twin replayed all {len(TRAINS)} timetabled trains over section {sec['id']} ({sec['from']} to {sec['to']}) "
        f"and tested {len(cands)} possible {dur}-minute windows for '{work}'.",
        f"{hhmm(best['start'])}-{hhmm(best['end'])} scored lowest: {nb} train(s) affected ({mix}), "
        f"{best['total_delay']} min total delay, weighted cost {best['cost']} versus an average of {avg:.0f}.",
        "Priority weights make an express delay 5x costlier than a freight delay, so the plan shields high-priority trains first.",
    ]
    if not best["peak"]:
        steps.append("The window avoids the morning (07:00-10:00) and evening (17:00-20:00) peak hours, which carry a fixed penalty.")
    rej = []
    for c in sorted(cands, key=lambda c: -c["cost"])[:3]:
        why = "overlaps a peak period; " if c["peak"] else ""
        rej.append(f"{c['label']} rejected: {why}{len(c['trains'])} trains affected, cost {c['cost']}.")
    dv = [t for t in best["trains"] if t["action"] == "divert"]; hd = [t for t in best["trains"] if t["action"] == "hold"]
    steps.append(f"Routing during the block: {len(dv)} train(s) are diverted via the loop line and {len(hd)} are held at the boundary until the block ends - whichever is cheaper for each train.")
    runner = ranked[1]
    steps.append(f"Runner-up: {runner['label']} (cost {runner['cost']}). " + (f"Choosing it would add {runner['cost']-best['cost']:.1f} cost points." if runner['cost']>best['cost'] else "It ties on cost, so the earlier, lower-risk night window is preferred."))
    return {"summary": f"Best block: {hhmm(best['start'])}-{hhmm(best['end'])}", "steps": steps, "rejected": rej,
            "saving": round((1 - best['cost'] / max(avg, 1)) * 100)}


GLOSSARY = {
    "block": "A block (or possession) is a planned period when a track section is closed to trains so engineers can work safely.",
    "possession": "A possession is the formal handover of a track section from traffic control to the maintenance team.",
    "headway": "Headway is the minimum time gap between two trains on the same line. RailTwin uses 6 minutes after a block ends.",
    "digital twin": "A digital twin is a virtual copy of the railway - tracks, stations and train timings - that lets you test plans before running them for real.",
    "priority": "Priority weights express > passenger > freight. Delaying an express costs 5, a passenger 3 and a freight 1 per minute.",
    "peak": "Peak hours (07:00-10:00 and 17:00-20:00) carry a fixed penalty because many passenger trains run.",
    "explain": "Explainable scheduling means the software shows why a window was chosen: which trains are hit, the cost, and what else was rejected.",
    "cost": "Cost = sum of (delay minutes x train priority weight), plus a peak penalty, scaled by the type of work.",
    "power": "A power block switches off overhead electric supply so crews can safely work on OHE equipment.",
}


@app.post("/api/ask")
def ask():
    q = (request.get_json(force=True).get("q") or "").lower()
    for k, v in GLOSSARY.items():
        if k in q:
            return jsonify(answer=v)
    return jsonify(answer="Try asking about: block, possession, headway, digital twin, priority, peak hours, cost, power block or explainability.")


# ---------------- database, login and roles ----------------
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c


def init_db():
    with db() as c:
        c.execute("create table if not exists users(name text primary key, pw text, role text)")
        c.execute("""create table if not exists plans(id integer primary key autoincrement, owner text, day int,
                     section text, start int, duration int, work text, cost real, status text default 'proposed', decided_by text)""")
        if not c.execute("select 1 from users where name='admin'").fetchone():
            c.execute("insert into users values('admin',?,'admin')", (generate_password_hash("admin123"),))


def need(*roles):
    def deco(f):
        @functools.wraps(f)
        def w(*a, **k):
            if "user" not in session:
                return jsonify(error="Please log in first."), 401
            if roles and session["role"] not in roles:
                return jsonify(error=f"Requires role: {' or '.join(roles)}."), 403
            return f(*a, **k)
        return w
    return deco


@app.post("/api/register")
def register():
    p = request.get_json(force=True); n, pw, role = p.get("name", "").strip(), p.get("pw", ""), p.get("role", "planner")
    if not n or len(pw) < 4 or role not in ("planner", "controller"):
        return jsonify(error="Name, password (4+ chars) and role planner/controller required."), 400
    try:
        with db() as c:
            c.execute("insert into users values(?,?,?)", (n, generate_password_hash(pw), role))
    except sqlite3.IntegrityError:
        return jsonify(error="User already exists."), 400
    session.update(user=n, role=role); return jsonify(user=n, role=role)


@app.post("/api/login")
def login():
    p = request.get_json(force=True)
    u = db().execute("select * from users where name=?", (p.get("name"),)).fetchone()
    if not u or not check_password_hash(u["pw"], p.get("pw", "")):
        return jsonify(error="Wrong name or password."), 401
    session.update(user=u["name"], role=u["role"]); return jsonify(user=u["name"], role=u["role"])


@app.post("/api/logout")
def logout():
    session.clear(); return jsonify(ok=True)


@app.get("/api/me")
def me():
    return jsonify(user=session.get("user"), role=session.get("role"))


# ---------------- multi-section optimiser and why-not ----------------
def section_options(sec, dur, work, top=12):
    cands = [evaluate(sec, s, dur) for s in range(0, 1440 - dur + 1, 15)]
    for c in cands:
        c["cost"] = round(c["cost"] * WORK.get(work, 1.0), 1)
    return sorted(cands, key=lambda c: c["cost"])[:top]


@app.post("/api/optimise")
def optimise():
    """Joint plan: adjacent sections may not be blocked at the same time (30 min buffer)."""
    p = request.get_json(force=True); dur = max(30, min(300, int(p.get("duration", 120)))); work = p.get("work", "Track renewal")
    secs = [s for s in SECTIONS if s["id"] in p.get("sections", [])][:5]
    opts = [section_options(s, dur, work) for s in secs]
    best, bc = None, 1e18
    for combo in itertools.product(*opts):
        if any(abs(int(secs[i]["id"][1:]) - int(secs[j]["id"][1:])) == 1 and
               combo[i]["start"] < combo[j]["end"] + 30 and combo[j]["start"] < combo[i]["end"] + 30
               for i in range(len(secs)) for j in range(i + 1, len(secs))):
            continue
        t = sum(c["cost"] for c in combo)
        if t < bc: best, bc = combo, t
    if not best:
        return jsonify(error="No conflict-free combination found; try fewer sections."), 400
    indep = sum(o[0]["cost"] for o in opts)
    blocks = [{"section": s["id"], "route": f"{s['from']} - {s['to']}", "start": c["start"], "label": c["label"],
               "end": hhmm(c["end"]), "duration": dur, "work": work, "cost": c["cost"], "trains": len(c["trains"])}
              for s, c in zip(secs, best)]
    note = ("Every section got its individually best window." if abs(bc - indep) < 0.1 else
            f"Adjacent sections cannot be closed together (30 min buffer), so the joint plan costs {bc - indep:.1f} more than "
            "the sum of each section's own best window - the price of keeping a diversion route open.")
    return jsonify(blocks=blocks, total=round(bc, 1), note=note)


@app.post("/api/whynot")
def whynot():
    p = request.get_json(force=True)
    sec = next((s for s in SECTIONS if s["id"] == p.get("section")), SECTIONS[0]); dur = int(p.get("duration", 120))
    work = p.get("work", "Track renewal"); st = max(0, min(1440 - dur, int(p.get("start", 0)) // 15 * 15))
    opts = section_options(sec, dur, work, top=1); b = opts[0]
    c = evaluate(sec, st, dur); c["cost"] = round(c["cost"] * WORK.get(work, 1.0), 1)
    why = [f"{hhmm(st)}-{hhmm(st + dur)} costs {c['cost']} against {b['cost']} for the best window ({b['label']})."]
    if c["peak"]: why.append("It overlaps a peak period, which adds a fixed 40-point penalty.")
    for t in sorted(c["trains"], key=lambda t: -t["cost"])[:3]:
        why.append(f"{t['id']} ({t['type']}) is delayed {t['delay']} min - {t['why']} (cost {t['cost']}).")
    why.append("It would actually be the best window." if c["cost"] <= b["cost"] else
               f"Verdict: {c['cost'] - b['cost']:.1f} cost points worse than the optimum.")
    return jsonify(reasons=why)


# ---------------- calendar plans ----------------
@app.post("/api/save")
@need("planner", "admin")
def save():
    p = request.get_json(force=True)
    with db() as c:
        for b in p.get("blocks", []):
            c.execute("insert into plans(owner,day,section,start,duration,work,cost) values(?,?,?,?,?,?,?)",
                      (session["user"], int(p.get("day", 0)), b["section"], b["start"], b["duration"], b["work"], b["cost"]))
    return jsonify(ok=True)


@app.get("/api/plans")
def plans():
    return jsonify(plans=[dict(r) for r in db().execute("select * from plans order by day,start")])


@app.post("/api/plans/<int:pid>/decide")
@need("controller", "admin")
def decide(pid):
    st = request.get_json(force=True).get("status")
    if st not in ("approved", "rejected"): return jsonify(error="Bad status"), 400
    with db() as c: c.execute("update plans set status=?, decided_by=? where id=?", (st, session["user"], pid))
    return jsonify(ok=True)


# ---------------- CSV import and PDF export ----------------
@app.get("/api/sample.csv")
def sample():
    out = io.StringIO(); w = csv.writer(out); w.writerow(["id", "type", "dir", "start", "speed"])
    for t in TRAINS[:6]: w.writerow([t["id"], t["type"], t["dir"], hhmm(t["start"]), t["speed"]])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=timetable.csv"})


@app.post("/api/import")
@need("planner", "admin")
def import_csv():
    global TRAINS
    f = request.files.get("file")
    if not f: return jsonify(error="Choose a CSV file."), 400
    new = []
    try:
        for i, r in enumerate(csv.DictReader(io.StringIO(f.read().decode("utf-8-sig"))), 2):
            kind = r["type"].strip().lower(); h, m = (r["start"].split(":") + ["0"])[:2]
            if kind not in TYPES: raise ValueError(f"row {i}: unknown type '{kind}'")
            sp = float(r.get("speed") or TYPES[kind][0])
            new.append({"id": r["id"].strip(), "type": kind, "dir": 1 if int(r["dir"]) >= 0 else -1,
                        "start": int(h) * 60 + int(m), "speed": sp, "weight": TYPES[kind][1]})
    except Exception as e:
        return jsonify(error=f"Could not read CSV: {e}. Columns: id,type,dir,start,speed"), 400
    if not new: return jsonify(error="CSV has no trains."), 400
    TRAINS = new; return jsonify(count=len(new))


@app.get("/api/pdf")
def pdf():
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4); y = 800
    c.setFont("Helvetica-Bold", 16); c.drawString(40, y, "RailTwin - Block Schedule Report"); y -= 30
    days = "Mon Tue Wed Thu Fri Sat Sun".split()
    for r in db().execute("select * from plans order by day,start"):
        if y < 80: c.showPage(); y = 800
        c.setFont("Helvetica-Bold", 10); c.drawString(40, y, f"{days[r['day']]}  {hhmm(r['start'])}-{hhmm(r['start'] + r['duration'])}  Section {r['section']}  [{r['status'].upper()}]")
        c.setFont("Helvetica", 9); y -= 13
        c.drawString(50, y, f"{r['work']} | cost {r['cost']} | planned by {r['owner']}" + (f" | decided by {r['decided_by']}" if r['decided_by'] else "")); y -= 20
    if y == 770: c.drawString(40, y, "No saved blocks yet.")
    c.save(); return Response(buf.getvalue(), mimetype="application/pdf", headers={"Content-Disposition": "attachment;filename=railtwin_report.pdf"})


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5000)

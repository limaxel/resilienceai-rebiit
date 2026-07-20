"""ResilienceAI backend — production serving layer on Cloud Run.

- Gemini 2.5 Flash via Vertex AI (IAM/ADC auth — no API key anywhere).
  Falls back to a GEMINI_API_KEY only for local development.
- Firestore persists every classified citizen report.
- /api/state serves a time-evolving simulated sensor plane (deterministic
  functions of wall-clock time, so every viewer sees the same city).
- /api/weather proxies a real Open-Meteo 72h rainfall forecast (cached).
- /api/plan generates the ranked action plan live from state + reports.
"""
import base64
import binascii
import datetime
import json
import math
import os
import re
import time
import zlib

import requests
from flask import Flask, jsonify, request, send_from_directory
from google import genai
from google.genai import types

app = Flask(__name__, static_folder=None)
APP_DIR = os.path.dirname(os.path.abspath(__file__))

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
LOCATION = os.environ.get("VERTEX_LOCATION", "global")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-2.5-flash"


# ---------------------------------------------------------------- Gemini
def engine():
    if PROJECT:
        return "vertex"
    if API_KEY:
        return "gemini-api"
    return None


_client = None


def gclient():
    global _client
    if _client is None:
        if PROJECT:
            _client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
        elif API_KEY:
            _client = genai.Client(api_key=API_KEY)
        else:
            raise RuntimeError("no Gemini credentials configured")
    return _client


# Tracks whether the model layer is answering, so the UI can say so honestly
# rather than silently serving canned text as if it were AI.
_ai_health = {"ok": True, "last_error": "", "last_fail": 0.0}


def note_ai(ok, err=""):
    _ai_health["ok"] = ok
    if not ok:
        _ai_health["last_error"] = str(err)[:200]
        _ai_health["last_fail"] = time.time()


def generate(contents, system=None, temp=0.4, max_tokens=2048,
             json_mode=False, no_thinking=False):
    cfg = types.GenerateContentConfig(temperature=temp, max_output_tokens=max_tokens)
    if system:
        cfg.system_instruction = system
    if json_mode:
        cfg.response_mime_type = "application/json"
    if no_thinking:
        cfg.thinking_config = types.ThinkingConfig(thinking_budget=0)
    try:
        resp = gclient().models.generate_content(model=MODEL, contents=contents, config=cfg)
    except Exception as exc:
        note_ai(False, exc)
        raise
    text = (resp.text or "").strip()
    if not text:
        note_ai(False, "empty response")
        raise RuntimeError("empty Gemini response")
    note_ai(True)
    return text


# ---------------------------------------------------------------- Firestore
_db = None
_db_failed = False


def fsdb():
    global _db, _db_failed
    if _db is not None:
        return _db
    if _db_failed:
        return None
    try:
        from google.cloud import firestore
        _db = firestore.Client(project=PROJECT or None)
    except Exception:
        _db_failed = True
        return None
    return _db


def store_report(doc):
    db = fsdb()
    if not db:
        return None
    try:
        ref = db.collection("reports").document()
        ref.set(doc)
        return ref.id
    except Exception:
        return None


def get_ops_doc(name, default=None):
    db = fsdb()
    if not db:
        return default
    try:
        snap = db.collection("ops").document(name).get()
        return snap.to_dict() if snap.exists else default
    except Exception:
        return default


def set_ops_doc(name, data):
    db = fsdb()
    if not db:
        return False
    try:
        db.collection("ops").document(name).set(data)
        return True
    except Exception:
        return False


def recent_reports(limit=20):
    db = fsdb()
    if not db:
        return []
    try:
        from google.cloud import firestore
        docs = (db.collection("reports")
                .order_by("ts", direction=firestore.Query.DESCENDING)
                .limit(limit).stream())
        out = []
        for d in docs:
            o = d.to_dict() or {}
            o["id"] = o.get("id") or d.id
            out.append(o)
        return out
    except Exception:
        return []


# ---------------------------------------------------------------- City model
# Mirrors the frontend's district model exactly (same risk formula).
DISTRICTS = [
    dict(id="riverside", name="Riverside North", pop=320000, gauge=6.4, gaugeChg=0.8, rain=180, baseVuln=0.85, riverExp=1.0, coastalExp=0.10, centerExp=0.15),
    dict(id="oldquarter", name="Old Quarter", pop=260000, gauge=4.8, gaugeChg=0.4, rain=120, baseVuln=0.50, riverExp=0.60, coastalExp=0.10, centerExp=0.60),
    dict(id="harbor", name="Harbor East", pop=300000, gauge=2.1, gaugeChg=0.3, rain=70, baseVuln=0.45, riverExp=0.40, coastalExp=1.00, centerExp=0.30),
    dict(id="kampung", name="Kampung Baru", pop=285000, gauge=5.9, gaugeChg=0.7, rain=165, baseVuln=0.80, riverExp=0.90, coastalExp=0.20, centerExp=0.10),
    dict(id="cbd", name="Central Business District", pop=210000, gauge=3.2, gaugeChg=0.2, rain=60, baseVuln=0.35, riverExp=0.30, coastalExp=0.25, centerExp=1.00),
    dict(id="hillside", name="Hillside West", pop=240000, gauge=1.8, gaugeChg=0.1, rain=40, baseVuln=0.20, riverExp=0.05, coastalExp=0.02, centerExp=0.15),
    dict(id="eastgate", name="Eastgate", pop=360000, gauge=2.9, gaugeChg=0.2, rain=55, baseVuln=0.30, riverExp=0.20, coastalExp=0.35, centerExp=0.30),
    dict(id="southbank", name="Southbank", pop=315000, gauge=4.5, gaugeChg=0.5, rain=110, baseVuln=0.55, riverExp=0.55, coastalExp=0.55, centerExp=0.50),
]

SENSORS = [
    dict(id="RVN-07", name="Riverside North · main gauge", base=6.4, unit="m", type="river"),
    dict(id="KPB-04", name="Kampung Baru · embankment", base=5.9, unit="m", type="river"),
    dict(id="SBK-02", name="Southbank · canal", base=4.5, unit="m", type="river"),
    dict(id="OLQ-03", name="Old Quarter · storm drain", base=4.8, unit="m", type="river"),
    dict(id="RVN-11", name="Riverside North · rain gauge", base=24, unit="mm/h", type="rain"),
    dict(id="HBE-06", name="Harbor East · tide", base=2.1, unit="m", type="tide"),
    dict(id="CBD-01", name="CBD · underpass sensor", base=3.2, unit="m", type="river"),
    dict(id="KPB-09", name="Kampung Baru · rain gauge", base=22, unit="mm/h", type="rain"),
    dict(id="EGT-05", name="Eastgate · culvert", base=2.9, unit="m", type="river"),
    dict(id="HLW-02", name="Hillside West · runoff", base=1.8, unit="m", type="river"),
]

# Seed reports that ship in the frontend feed — used for grounding + dedupe.
SEED_REPORTS = [
    ("CR-4821", "Riverside North", "SEVERE", "Flooding", "Water up to waist level on Jalan Sungai, cars stranded, elderly residents on 2nd floor need help."),
    ("CR-4820", "Kampung Baru", "HIGH", "Flooding", "Embankment overflowing near the market, water entering ground-floor homes rapidly."),
    ("CR-4818", "Kampung Baru", "HIGH", "Flooding", "Market area flooding, stalls underwater (duplicate cluster with CR-4820)."),
    ("CR-4817", "Old Quarter", "MODERATE", "Blocked drain", "Storm drain on Heritage Lane completely blocked with debris, water pooling fast."),
    ("CR-4815", "Hillside West", "HIGH", "Landslide risk", "Soil slipping on the slope behind the school, cracks visible in retaining wall."),
    ("CR-4814", "Southbank", "MODERATE", "Flooding", "Canal level rising, riverside footpath underwater near the pedestrian bridge."),
    ("CR-4812", "Riverside North", "MODERATE", "Debris flow", "Fallen tree and debris blocking the drainage channel on Riverbank Road."),
    ("CR-4810", "Riverside North", "SEVERE", "Flooding", "Jalan Sungai fully submerged, rescue boats needed (duplicate cluster with CR-4821)."),
    ("CR-4808", "Harbor East", "LOW", "Coastal flooding", "Minor tidal flooding at the quay, water over the lower dock but manageable."),
    ("CR-4805", "Eastgate", "LOW", "Blocked drain", "Possible spam / unclear photo, text mentions a clogged gutter."),
]

ASSETS_CONTEXT = (
    "Response assets: 6 mobile high-capacity pumps (Central depot, ETA 25 min anywhere), "
    "14 response crews (11 deployable), 4 rescue-boat teams, 3 evacuation bus convoys. "
    "Shelters: 12 active, 30,000 total capacity, 18,400 spaces open; 2 reserve shelters "
    "in Hillside West (community hall + sports complex, +4,500 spaces, high ground). "
    "Egress: 3 primary routes from Riverside North clear, Riverbank Rd partially blocked (CR-4812)."
)


def risk_score(d, rain, track):
    s = d["baseVuln"] * 25 + (rain / 300.0) ** 2 * 63.333 * d["riverExp"]
    if track == "riverine":
        s += d["riverExp"] * 8
    elif track == "center":
        s += d["baseVuln"] * 10 + d["centerExp"] * 10
    elif track == "coastal":
        s += d["coastalExp"] * 18 + (rain / 300.0) * 8 * d["coastalExp"]
    elif track == "offshore":
        s = max(0.0, s - 12)
    return s


def score_to_risk(s):
    if s >= 60:
        return "SEVERE"
    if s >= 40:
        return "HIGH"
    if s >= 25:
        return "MODERATE"
    return "LOW"


def wobble(seed, t, period, amp):
    """Deterministic smooth oscillation — same value for every viewer/instance."""
    phase = (zlib.crc32(seed.encode()) % 6283) / 1000.0
    return amp * math.sin(2 * math.pi * t / period + phase)


# ---------------------------------------------------------------- Comms resilience
# A flood takes out power and cell sites. When a district stops reporting, that
# silence is treated as escalation, not reassurance — the doctrine real emergency
# operations centers use. Blackout state lives in Firestore so every instance and
# every viewer sees the same picture.
_blackout_cache = {"t": 0.0, "data": None}
_blackout_mem = {}   # fallback when Firestore is unavailable (local dev)
BLACKOUT_TTL = 5.0
# Eastgate ships with 2 offline gauges in the baseline scenario.
PARTIAL_DEGRADED = {"eastgate": 2}
# Gauges per district — sums to the 42-gauge network quoted across the product.
SENSOR_COUNTS = {"riverside": 7, "oldquarter": 5, "harbor": 6, "kampung": 6,
                 "cbd": 4, "hillside": 4, "eastgate": 5, "southbank": 5}


def blackout_state():
    if _blackout_cache["data"] is not None and time.time() - _blackout_cache["t"] < BLACKOUT_TTL:
        return _blackout_cache["data"]
    doc = get_ops_doc("comms")
    data = (doc or {}).get("blackouts") if doc else None
    if data is None:
        data = _blackout_mem
    _blackout_cache.update(t=time.time(), data=data)
    return data


def comms_for(district_id, t):
    """Return (status, silent_minutes, note) for a district."""
    bo = blackout_state().get(district_id)
    if bo:
        since = float(bo.get("since") or t)
        return "blackout", max(0, int((t - since) / 60)), bo.get("cause", "no sensor or citizen traffic")
    if district_id in PARTIAL_DEGRADED:
        return "degraded", 0, f"{PARTIAL_DEGRADED[district_id]} gauges offline"
    return "ok", 0, ""


def city_state(t=None):
    t = t or time.time()
    districts = []
    for d in DISTRICTS:
        gauge = d["gauge"] + wobble(d["id"] + ":g", t, 10800, 0.09) + wobble(d["id"] + ":g2", t, 1400, 0.03)
        rain48 = d["rain"] + wobble(d["id"] + ":r", t, 7200, 6)
        status, silent, note = comms_for(d["id"], t)
        entry = {
            "id": d["id"], "name": d["name"], "pop": d["pop"],
            "gauge": round(gauge, 2), "gaugeChg": d["gaugeChg"],
            "rain48": int(round(rain48)),
            "risk": score_to_risk(risk_score(d, 180, "riverine")),
            "comms": status, "silentMin": silent, "commsNote": note,
        }
        if status == "blackout":
            # Last known values are stale — freeze them and flag the gap.
            entry["stale"] = True
        districts.append(entry)
    blacked = {d["id"] for d in districts if d["comms"] == "blackout"}
    sensor_district = {"RVN": "riverside", "KPB": "kampung", "SBK": "southbank",
                       "OLQ": "oldquarter", "HBE": "harbor", "CBD": "cbd",
                       "EGT": "eastgate", "HLW": "hillside"}
    sensors = []
    for s in SENSORS:
        if sensor_district.get(s["id"][:3]) in blacked:
            sensors.append({
                "id": s["id"], "name": s["name"], "unit": s["unit"], "type": s["type"],
                "val": None, "delta": 0, "status": "NO SIGNAL",
            })
            continue
        amp = 2.5 if s["type"] == "rain" else 0.08
        delta = wobble(s["id"] + ":d", t, 900, amp)
        val = s["base"] + wobble(s["id"] + ":v", t, 5400, amp * 1.6) + delta
        if s["type"] == "river" and val > 5.5:
            status = "ALERT"
        elif s["type"] == "river" and val > 4.5:
            status = "WATCH"
        elif s["type"] == "rain" and val > 20:
            status = "HEAVY"
        else:
            status = "NOMINAL"
        sensors.append({
            "id": s["id"], "name": s["name"], "unit": s["unit"], "type": s["type"],
            "val": round(val, 0 if s["type"] == "rain" else 2),
            "delta": round(delta, 1 if s["type"] == "rain" else 2),
            "status": status,
        })
    at_risk = sum(d["pop"] for d in districts if d["risk"] in ("MODERATE", "HIGH", "SEVERE"))
    alerts_mod = 5 + (1 if wobble("alerts", t, 6600, 1) > 0.55 else 0)
    shelter_open = 18400 - int(abs(wobble("shelter", t, 8400, 1)) * 350)
    blackout_list = [d for d in districts if d["comms"] == "blackout"]
    unreachable = sum(SENSOR_COUNTS.get(d["id"], 0) for d in blackout_list)
    baseline_offline = sum(PARTIAL_DEGRADED.values())
    kpis = {
        "popAtRisk": at_risk,
        "alerts": 2 + alerts_mod + len(blackout_list),
        "alertsHigh": 2 + len(blackout_list), "alertsMod": alerts_mod,
        "sensorsOnline": 42 - baseline_offline - unreachable, "sensorsTotal": 42,
        "sensorsUnreachable": unreachable, "sensorsFaulty": baseline_offline,
        "blackoutNames": [d["name"] for d in blackout_list],
        "shelterOpen": shelter_open, "shelterTotal": 30000, "sheltersActive": 12,
        "blackouts": len(blackout_list),
        "popUnobserved": sum(d["pop"] for d in blackout_list),
    }
    return {"t": int(t), "districts": districts, "sensors": sensors, "kpis": kpis,
            "comms": {
                "blackouts": [{"id": d["id"], "name": d["name"], "pop": d["pop"],
                               "silentMin": d["silentMin"], "lastRisk": d["risk"],
                               "note": d["commsNote"]} for d in blackout_list],
                "degraded": [{"id": d["id"], "name": d["name"], "note": d["commsNote"]}
                             for d in districts if d["comms"] == "degraded"],
                "doctrine": ("Loss of signal from a flood-exposed district is treated as "
                             "escalation, not reassurance: assume conditions are worse than "
                             "last observed until a field team confirms otherwise."),
            }}


# ---------------------------------------------------------------- Weather
_weather_cache = {"t": 0.0, "data": None}
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
PILOT = {"name": "Jakarta pilot region", "lat": -6.2088, "lon": 106.8456}


def get_weather():
    if _weather_cache["data"] and time.time() - _weather_cache["t"] < 1800:
        return _weather_cache["data"]
    r = requests.get(OPEN_METEO, params={
        "latitude": PILOT["lat"], "longitude": PILOT["lon"],
        "hourly": "precipitation", "forecast_days": 4, "timezone": "UTC",
    }, timeout=10)
    r.raise_for_status()
    j = r.json()
    times = j["hourly"]["time"]
    precip = [p or 0.0 for p in j["hourly"]["precipitation"]]
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:00")
    start = times.index(now_iso) if now_iso in times else 0
    window = precip[start:start + 73]
    data = {
        "place": PILOT["name"], "source": "Open-Meteo live forecast",
        "hours": list(range(len(window))), "precip": [round(p, 2) for p in window],
        "total48": round(sum(window[:49]), 1), "total72": round(sum(window), 1),
        "fetched": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    _weather_cache.update(t=time.time(), data=data)
    return data


# ---------------------------------------------------------------- Prompts
def reports_digest(limit=8):
    lines = [f"- [{r[1]} · {r[2]} · {r[3]}] {r[4]}" for r in SEED_REPORTS[:6]]
    for r in recent_reports(limit):
        sev = r.get("severity", "MODERATE")
        typ = r.get("type", "other")
        summ = (r.get("summary") or "")[:200]
        lines.append(f"- [{r.get('district', 'Citizen submission')} · {sev} · {typ}] {summ}")
    return "\n".join(lines)


def chat_context():
    st = city_state()
    k = st["kpis"]
    lines = "\n".join(
        f"- {d['name']}: risk {d['risk']}, gauge {d['gauge']}m (+{d['gaugeChg']}m/24h), "
        f"48h rain {d['rain48']}mm, pop {d['pop']:,}" for d in st["districts"])
    weather_line = ""
    try:
        w = get_weather()
        weather_line = (f"\nLive regional weather feed ({w['place']}, Open-Meteo): "
                        f"{w['total48']}mm real precipitation expected in the next 48h.")
    except Exception:
        pass
    comms_line = ""
    bo = st["comms"]["blackouts"]
    if bo:
        detail = "; ".join(f"{b['name']} (pop {b['pop']:,}, silent {b['silentMin']} min, "
                           f"last known risk {b['lastRisk']}, {b['note']})" for b in bo)
        comms_line = (f"\nCOMMS BLACKOUT — no sensor or citizen data from: {detail}. "
                      f"{st['comms']['doctrine']} These districts are blind spots: their readings "
                      f"are stale, they must not be described as safe, and they need a field team "
                      f"for eyes-on confirmation.")
    return f"""You are the ResilienceAI Response Assistant for Delta City (pop 2.4M),
a Southeast Asian river city during monsoon season. A tropical storm is approaching.
Current district state (live):
{lines}
72h forecast: river peaks ~6.8m at T+42h; flood stage 5.5m, danger stage 6.5m.
Sensors: {k['sensorsOnline']}/{k['sensorsTotal']} online (2 offline in Eastgate).
Shelters: {k['sheltersActive']} active, {k['shelterOpen']:,} spaces open of {k['shelterTotal']:,}.
{k['alerts']} active alerts ({k['alertsHigh']} HIGH, {k['alertsMod']} MODERATE).
Population at risk (MODERATE or worse): {k['popAtRisk']:,}.{weather_line}{comms_line}
{ASSETS_CONTEXT}
If a district is in a comms blackout, never call it safe: state that its data is stale, that
conditions must be assumed worse than last observed, and that a field team is needed.
Recent citizen reports:
{reports_digest(5)}
Answer as a concise, calm operations assistant: ground every answer in this data,
cite figures, and remind that public-facing actions require human confirmation.
When recommending evacuations, always end by checking shelter capacity: state whether
the open shelter spaces are sufficient for the population being evacuated. When asked
for advisories, provide English and Bahasa Indonesia versions."""


# ---------------------------------------------------------------- ADK multi-agent
# Root operations agent + comms & planner specialists (Agent Development Kit).
# Tool functions give the agents live grounding; docstrings are tool specs.
def get_city_state() -> dict:
    """Live Delta City state: district river gauges, risk levels, 48h rain, population, sensor readings, shelter capacity and active alerts."""
    return city_state()


def get_citizen_reports() -> dict:
    """Latest AI-triaged citizen reports from the field (severity, type, district, summary)."""
    return {"reports": reports_digest(10)}


def get_weather_feed() -> dict:
    """Real 72-hour rainfall forecast from the live Open-Meteo feed for the pilot region."""
    try:
        return get_weather()
    except Exception as exc:
        return {"error": str(exc)}


def get_response_assets() -> dict:
    """Available response assets: pumps, crews, rescue boats, buses, shelters (incl. reserves) and egress route status."""
    return {"assets": ASSETS_CONTEXT}


def get_comms_status() -> dict:
    """Communications status per district: which districts have lost all sensor and citizen data (comms blackout), how long they have been silent, and the operational doctrine for handling that silence. Call this before assessing risk — a silent district is a blind spot, not a safe one."""
    return city_state()["comms"]


_adk = {"runner": None, "failed": False}


def adk_runner():
    if _adk["runner"] is not None:
        return _adk["runner"]
    if _adk["failed"] or not engine():
        return None
    try:
        if engine() == "vertex":
            os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
            os.environ.setdefault("GOOGLE_CLOUD_LOCATION", LOCATION)
        else:
            os.environ.setdefault("GOOGLE_API_KEY", API_KEY)
        from google.adk.agents import LlmAgent
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.adk.tools.agent_tool import AgentTool

        comms = LlmAgent(
            name="comms_agent", model=MODEL,
            description="Drafts calm, clear public advisories in English AND Bahasa Indonesia for a given district and hazard.",
            instruction=("You draft public emergency advisories for Delta City. Always produce both an "
                         "English and a Bahasa Indonesia version, tone-checked: calm, clear, actionable, "
                         "~grade-6 reading level. Include where to go (nearest shelters), what to bring, "
                         "and what to avoid. The ONLY named shelters are: Riverside Community Hall and "
                         "Northgate Sports Complex (Riverside North zone), Kampung Baru Community School "
                         "Hall (Kampung Baru zone), Hillside West Community Hall (reserve, high ground). "
                         "Never invent street addresses or other place names — otherwise say 'the nearest "
                         "relief shelter'. End by noting human confirmation is required before broadcast."))
        planner = LlmAgent(
            name="response_planner", model=MODEL,
            description="Produces or revises the ranked flood-response action plan (evacuations, pumps, closures, shelters).",
            instruction=("You are the response planning specialist for Delta City flood operations. "
                         "Given the situation, produce concise ranked actions with population impact, "
                         "trigger data and required resources. Cite concrete figures."))
        root = LlmAgent(
            name="resilience_root", model=MODEL,
            description="Root operations assistant for the ResilienceAI flood command center.",
            instruction=("You are the ResilienceAI Response Assistant for Delta City (pop 2.4M), a Southeast "
                         "Asian river city in monsoon season with a tropical storm approaching. Flood stage is "
                         "5.5m, danger stage 6.5m; river forecast peaks ~6.8m at T+42h. Always ground answers "
                         "in live data: call get_city_state, get_citizen_reports, get_weather_feed, "
                         "get_response_assets or get_comms_status before answering factual questions. "
                         "If any district is in a comms blackout, treat it as a blind spot and say so "
                         "explicitly: its last known reading is stale, conditions must be assumed worse "
                         "than last observed, and it needs a field team for eyes-on confirmation. "
                         "Never describe a silent district as safe. Delegate advisory drafting "
                         "to comms_agent and plan construction to response_planner; when a specialist agent "
                         "returns content, include its FULL output verbatim in your answer — never summarize "
                         "or describe it. Answer as a concise, calm "
                         "operations assistant citing figures. When recommending evacuations, always check "
                         "whether open shelter spaces are sufficient for the population being evacuated. "
                         "Remind that public-facing actions require human confirmation."),
            tools=[get_city_state, get_citizen_reports, get_weather_feed, get_response_assets,
                   get_comms_status, AgentTool(agent=comms), AgentTool(agent=planner)])
        service = InMemorySessionService()
        _adk["service"] = service
        _adk["runner"] = Runner(agent=root, app_name="resilienceai", session_service=service)
        return _adk["runner"]
    except Exception:
        _adk["failed"] = True
        return None


def run_agent(question):
    """Run one question through the ADK agent team; returns (answer, tool_trace)."""
    import asyncio
    import uuid
    runner = adk_runner()
    if runner is None:
        raise RuntimeError("agent runner unavailable")

    async def _go():
        sid = uuid.uuid4().hex
        await _adk["service"].create_session(app_name="resilienceai", user_id="ops", session_id=sid)
        msg = types.Content(role="user", parts=[types.Part.from_text(text=question[:4000])])
        final, trace = "", []
        async for ev in runner.run_async(user_id="ops", session_id=sid, new_message=msg):
            try:
                for fc in ev.get_function_calls() or []:
                    if fc.name and fc.name not in trace:
                        trace.append(fc.name)
            except Exception:
                pass
            if ev.is_final_response() and ev.content and ev.content.parts:
                final = "".join(p.text or "" for p in ev.content.parts)
        return final.strip(), trace

    return asyncio.run(_go())


# ---------------------------------------------------------------- Routes
@app.get("/api/health")
def health():
    return jsonify({
        "mode": "live" if engine() else "demo",
        "engine": engine(),
        "firestore": fsdb() is not None,
        "agents": "adk" if (engine() and not _adk["failed"]) else None,
        "ai": "ok" if _ai_health["ok"] else "degraded",
        "aiError": "" if _ai_health["ok"] else _ai_health["last_error"],
    })


@app.get("/api/state")
def state():
    return jsonify(city_state())


@app.post("/api/comms")
def comms_control():
    """Drill control: simulate losing (or restoring) a district's comms."""
    body = request.get_json(force=True, silent=True) or {}
    did = str(body.get("district") or "").strip()
    if did not in {d["id"] for d in DISTRICTS}:
        return jsonify({"error": "unknown district"}), 400
    doc = get_ops_doc("comms")
    blackouts = dict((doc or {}).get("blackouts") or {}) if doc else dict(_blackout_mem)
    if body.get("on"):
        blackouts[did] = {"since": time.time(),
                          "cause": str(body.get("cause") or "cell site down, power loss")[:120]}
    else:
        blackouts.pop(did, None)
    persisted = set_ops_doc("comms", {"blackouts": blackouts})
    _blackout_mem.clear()
    _blackout_mem.update(blackouts)
    _blackout_cache.update(t=0.0, data=None)
    return jsonify({"ok": True, "blackouts": list(blackouts), "persisted": persisted})


@app.get("/api/weather")
def weather():
    try:
        return jsonify(get_weather())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.get("/api/reports")
def reports():
    limit = min(int(request.args.get("limit", 20)), 50)
    out = []
    for r in recent_reports(limit):
        out.append({
            "id": r.get("id"), "ts": r.get("ts"),
            "type": r.get("type"), "severity": r.get("severity"),
            "confidence": r.get("confidence"), "duplicate_suspect": r.get("duplicate_suspect"),
            "summary": r.get("summary"), "note": r.get("note"),
            "district": r.get("district"), "thumb": r.get("thumb"),
            "source": r.get("source"),
        })
    return jsonify({"reports": out})


@app.post("/api/chat")
def chat():
    body = request.get_json(force=True, silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question required"}), 400
    contents = []
    for turn in (body.get("history") or [])[-10:]:
        role = "user" if turn.get("role") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": str(turn.get("text", ""))[:4000]}]})
    contents.append({"role": "user", "parts": [{"text": question[:4000]}]})
    # Preferred path: ADK multi-agent team (root + comms + planner with live tools)
    if not body.get("context") and adk_runner() is not None:
        try:
            answer, trace = run_agent(question)
            if answer:
                return jsonify({"text": answer, "trace": trace, "agents": "adk"})
        except Exception:
            pass  # fall through to the direct-generation path
    system = body.get("context") or chat_context()
    try:
        return jsonify({"text": generate(contents, system=str(system)[:16000])})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/api/triage")
def triage():
    body = request.get_json(force=True, silent=True) or {}
    image = body.get("imageBase64") or ""
    mime = body.get("mimeType") or "image/jpeg"
    note = (body.get("note") or "").strip()
    district = (body.get("district") or "Citizen submission").strip()[:60]
    source = (body.get("source") or "command-center").strip()[:30]
    thumb = body.get("thumbnail") or ""
    if not image:
        return jsonify({"error": "imageBase64 required"}), 400
    if len(image) > 8_000_000:
        return jsonify({"error": "image too large"}), 413
    if len(thumb) > 160_000 or not thumb.startswith("data:image/"):
        thumb = ""
    try:
        raw = base64.b64decode(image, validate=False)
    except (binascii.Error, ValueError):
        return jsonify({"error": "invalid base64 image"}), 400
    instruction = (
        "You are triaging a citizen disaster report for Delta City flood operations. "
        "Classify the attached photo (and note, if any). Return ONLY JSON with keys: "
        '"type" (one of flooding|blocked_drain|landslide|infrastructure_damage|other), '
        '"severity" (LOW|MODERATE|HIGH|SEVERE), "confidence" (0-100 integer), '
        '"duplicate_suspect" (boolean), "summary" (one sentence). '
        "Base severity on visible water depth, structural danger and threat to life.\n"
        "Reports already in the feed:\n" + reports_digest(8) + "\n"
        "Set duplicate_suspect true if this photo+note likely describes the same "
        "incident and location as one of the reports above."
    )
    if note:
        instruction += f'\nCitizen note: "{note[:500]}"'
    contents = [types.Content(role="user", parts=[
        types.Part.from_text(text=instruction),
        types.Part.from_bytes(data=raw, mime_type=mime),
    ])]
    try:
        text = generate(contents, temp=0.2, max_tokens=1024, json_mode=True, no_thinking=True)
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        result = json.loads(text)
    except json.JSONDecodeError:
        return jsonify({"error": "unparseable model output"}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    doc = {
        "ts": int(time.time()),
        "type": str(result.get("type", "other"))[:40],
        "severity": str(result.get("severity", "MODERATE"))[:10],
        "confidence": int(result.get("confidence") or 75),
        "duplicate_suspect": bool(result.get("duplicate_suspect")),
        "summary": str(result.get("summary", ""))[:400],
        "note": note[:500], "district": district, "source": source, "thumb": thumb,
    }
    doc_id = store_report(doc)
    result["id"] = doc_id
    result["stored"] = bool(doc_id)
    return jsonify(result)


# ---------------------------------------------------------------- SMS channel
# Data networks congest long before SMS does, and a feature phone has no browser
# at all. Text is the lowest common denominator: no app, no install, no data plan.
DISTRICT_NAMES = {d["name"].lower(): d["name"] for d in DISTRICTS}


@app.post("/api/sms")
def sms_inbound():
    """Inbound citizen report by SMS.

    Shaped for a carrier webhook (Twilio-style `From`/`Body`). No carrier is
    connected in the prototype; the endpoint itself is live and does the real work.
    """
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("Body") or body.get("text") or "").strip()
    sender = (body.get("From") or body.get("from") or "anonymous").strip()[:40]
    if not text:
        return jsonify({"error": "empty message"}), 400
    instruction = (
        "You are triaging a flood report that a citizen sent by SMS in Delta City during "
        "monsoon season. The message may be terse, misspelled, in English or Bahasa "
        "Indonesia, or mixed. Extract what you can and return ONLY JSON with keys: "
        '"district" (one of: ' + ", ".join(d["name"] for d in DISTRICTS) + ', or "Unknown"), '
        '"type" (flooding|blocked_drain|landslide|infrastructure_damage|other), '
        '"severity" (LOW|MODERATE|HIGH|SEVERE), "confidence" (0-100 integer), '
        '"summary" (one sentence in English), '
        '"language" (the ISO code of the language the citizen wrote in: "en" or "id"), '
        '"reply" (a calm SMS reply, max 300 characters, telling them what to do right '
        'now and that the report was received). '
        "CRITICAL: the reply MUST be written in the same language as the citizen's "
        'message. If "language" is "en" the reply is in English only; if "id" the reply '
        "is in Bahasa Indonesia only. Do not mix languages. "
        "Base severity on threat to life and the depth described.\n"
        "Citizen SMS: " + text[:600]
    )
    try:
        out = generate([{"role": "user", "parts": [{"text": instruction}]}],
                       temp=0.2, max_tokens=1024, json_mode=True, no_thinking=True)
        out = re.sub(r"^```(?:json)?|```$", "", out.strip(), flags=re.M).strip()
        parsed = json.loads(out)
    except Exception as exc:
        # Never drop a citizen's message because the model is unavailable.
        store_report({"ts": int(time.time()), "type": "other", "severity": "MODERATE",
                      "confidence": 0, "duplicate_suspect": False,
                      "summary": f"Unparsed SMS: {text[:200]}", "note": text[:500],
                      "district": "Unknown", "source": "sms", "thumb": ""})
        return jsonify({"stored": True, "parsed": False, "error": str(exc)[:120],
                        "reply": "Report received. Move to higher ground if water is "
                                 "rising and call 112 if you are in danger."}), 200
    district = str(parsed.get("district") or "Unknown")
    district = DISTRICT_NAMES.get(district.lower(), district)[:60]
    doc = {
        "ts": int(time.time()),
        "type": str(parsed.get("type", "other"))[:40],
        "severity": str(parsed.get("severity", "MODERATE"))[:10],
        "confidence": int(parsed.get("confidence") or 60),
        "duplicate_suspect": False,
        "summary": str(parsed.get("summary", ""))[:400],
        "note": text[:500], "district": district, "source": "sms",
        "sender": sender, "thumb": "",
    }
    doc_id = store_report(doc)
    return jsonify({"stored": bool(doc_id), "parsed": True, "id": doc_id,
                    "district": district, "type": doc["type"], "severity": doc["severity"],
                    "confidence": doc["confidence"], "summary": doc["summary"],
                    "language": str(parsed.get("language", ""))[:5],
                    "reply": str(parsed.get("reply", ""))[:320]})


PLAN_CATS = ["Evacuation", "Pump deployment", "Road closure", "Shelter activation",
             "Public advisory", "Rescue operation", "Field recon", "Infrastructure", "Other"]


def sanitize_rich(s, max_len=400):
    """Allow only <b> tags in model-written strings rendered as HTML."""
    s = str(s)[:max_len]
    s = re.sub(r"<(?!/?b>)[^>]*>", "", s)
    return s


@app.post("/api/plan")
def plan():
    body = request.get_json(force=True, silent=True) or {}
    try:
        rain = max(20, min(320, int(body.get("rain") or 180)))
    except (TypeError, ValueError):
        rain = 180
    track = body.get("track") if body.get("track") in ("riverine", "center", "coastal", "offshore") else "riverine"
    st = city_state()
    lines = []
    for d, dd in zip(DISTRICTS, st["districts"]):
        r = score_to_risk(risk_score(d, rain, track))
        flag = ""
        if dd["comms"] == "blackout":
            flag = (f"  ** COMMS BLACKOUT — silent {dd['silentMin']} min, readings stale, "
                    f"treat as blind spot needing eyes-on **")
        lines.append(f"- {d['name']}: scenario risk {r}, gauge {dd['gauge']}m "
                     f"(+{d['gaugeChg']}m/24h), pop {d['pop']:,}{flag}")
    blackout_rule = ""
    if st["comms"]["blackouts"]:
        blackout_rule = ("\nIMPORTANT: one or more districts have lost communications. You MUST include a "
                         "high-priority action to send a field reconnaissance team to each blacked-out "
                         "district. Silence is treated as escalation, not reassurance.")
    k = st["kpis"]
    scenario = ("current baseline forecast" if (rain == 180 and track == "riverine")
                else f"WHAT-IF scenario: {rain}mm rain over 48h, storm track '{track}'")
    prompt = f"""You are the Response Planner agent of ResilienceAI, planning flood response for
Delta City (pop 2.4M, monsoon season, tropical storm approaching). Plan for the {scenario}.
District state:
{chr(10).join(lines)}
Flood stage 5.5m, danger stage 6.5m. River forecast peak ~6.8m at T+42h (baseline).
Sensors {k['sensorsOnline']}/{k['sensorsTotal']} online. Shelters: {k['sheltersActive']} active,
{k['shelterOpen']:,} of {k['shelterTotal']:,} spaces open (+4,500 in 2 reserve shelters).
{ASSETS_CONTEXT}{blackout_rule}
Citizen reports:
{reports_digest(6)}

Produce the ranked action plan. Return ONLY a JSON array of exactly 6 objects, ranked most
urgent first, each with keys:
"rank" (1-6), "risk" (priority level: LOW|MODERATE|HIGH|SEVERE),
"cat" (one of {"|".join(PLAN_CATS)}), "title" (imperative, max 60 chars),
"reason" (max 40 words, cite concrete figures from the data),
"pop" (affected population as a string like "131,000", or "—"),
"conf" (integer 60-97, your confidence),
"detail" (array of 3-4 strings, each starting with a bold label like "<b>Trigger:</b>",
"<b>SOP match:</b>", "<b>Resources:</b>", "<b>Dependency:</b> " — concrete and operational).
Only <b> HTML tags are allowed. Respond with the JSON array only."""
    try:
        text = generate([{"role": "user", "parts": [{"text": prompt}]}],
                        temp=0.35, max_tokens=4096, json_mode=True, no_thinking=True)
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        raw_actions = json.loads(text)
        if isinstance(raw_actions, dict):
            raw_actions = raw_actions.get("actions") or raw_actions.get("plan") or []
        actions = []
        for i, a in enumerate(raw_actions[:6]):
            risk = str(a.get("risk", "MODERATE")).upper()
            if risk not in ("LOW", "MODERATE", "HIGH", "SEVERE"):
                risk = "MODERATE"
            conf = int(a.get("conf") or 80)
            actions.append({
                "rank": i + 1,
                "risk": risk,
                "cat": sanitize_rich(a.get("cat", "Other"), 30),
                "title": sanitize_rich(a.get("title", ""), 90),
                "reason": sanitize_rich(a.get("reason", ""), 400),
                "pop": sanitize_rich(a.get("pop", "—"), 20),
                "conf": max(50, min(97, conf)),
                "detail": [sanitize_rich(x) for x in (a.get("detail") or [])[:4]],
            })
        if not actions:
            raise RuntimeError("empty plan")
        return jsonify({"scenario": {"rain": rain, "track": track},
                        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                        "actions": actions})
    except json.JSONDecodeError:
        return jsonify({"error": "unparseable model output"}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


def rule_based_plan(rain=180, track="riverine"):
    """Deterministic triage used when the model layer is unreachable.

    Not AI output and never presented as such — it is a defensible ordering so the
    command center still functions during an outage.
    """
    st = city_state()
    by_id = {d["id"]: d for d in st["districts"]}
    scored = []
    for d in DISTRICTS:
        dd = by_id[d["id"]]
        s = risk_score(d, rain, track)
        if dd["comms"] == "blackout":
            s += 15  # silence escalates, never reassures
        scored.append((s, d, dd))
    scored.sort(key=lambda x: (-x[0], -x[1]["pop"]))
    actions, rank = [], 0
    for s, d, dd in scored:
        risk = score_to_risk(s)
        if risk == "LOW" or rank >= 5:
            continue
        rank += 1
        if dd["comms"] == "blackout":
            silent_txt = f"{dd['silentMin']} min" if dd["silentMin"] else "under a minute"
            cat, title = "Field recon", f"Send reconnaissance team to {d['name']} - no data for {silent_txt}"
            reason = (f"All sensor and citizen traffic from {d['name']} stopped {silent_txt} ago. "
                      f"Last known risk {dd['risk']} at gauge {dd['gauge']}m. Silence is treated as escalation: "
                      f"{d['pop']:,} residents are currently unobserved.")
            detail = [f"<b>Trigger:</b> Comms blackout — {dd['commsNote']}.",
                      "<b>Doctrine:</b> Assume conditions worse than last observed until eyes-on confirms.",
                      "<b>Resources:</b> Dispatch 1 recon team with satellite messenger and portable repeater."]
        elif risk == "SEVERE" or dd["gauge"] >= 6.0:
            cat, title = "Evacuation", f"Evacuate {d['name']} floodplain"
            reason = (f"Gauge {dd['gauge']}m against a 5.5m flood stage and 6.5m danger stage, "
                      f"rising {d['gaugeChg']}m/24h. {d['pop']:,} residents exposed.")
            detail = [f"<b>Trigger:</b> Gauge {dd['gauge']}m, risk {risk}.",
                      "<b>SOP match:</b> Mandatory evacuation within 0.2m of danger stage with a rising trend.",
                      f"<b>Shelters:</b> {st['kpis']['shelterOpen']:,} spaces open city-wide."]
        else:
            cat, title = "Pump deployment", f"Pre-position pumps and crews in {d['name']}"
            reason = (f"Risk {risk} with gauge {dd['gauge']}m and {dd['rain48']}mm forecast over 48h. "
                      f"Pre-emptive drainage keeps the district below flood stage.")
            detail = [f"<b>Trigger:</b> Gauge {dd['gauge']}m, 48h rain {dd['rain48']}mm.",
                      "<b>Resources:</b> Mobile pumps from Central depot, ETA 25 min.",
                      "<b>Dependency:</b> Clear known drain blockages first."]
        actions.append({"rank": rank, "risk": risk, "cat": cat, "title": title[:90],
                        "reason": reason, "pop": f"{d['pop']:,}", "conf": 0, "detail": detail})
    return actions


@app.get("/api/fallback-plan")
def fallback_plan():
    """Rule-based plan — explicitly not AI-generated."""
    return jsonify({"mode": "rule-based", "actions": rule_based_plan(),
                    "note": "Deterministic triage — the model layer is not involved."})


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(APP_DIR, "sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(APP_DIR, "manifest.webmanifest",
                               mimetype="application/manifest+json")


@app.get("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.get("/report")
def report_page():
    return send_from_directory(APP_DIR, "report.html")


@app.get("/<path:path>")
def static_files(path):
    if path.endswith(".html") or "." not in path:
        return send_from_directory(APP_DIR, "index.html")
    return send_from_directory(APP_DIR, path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

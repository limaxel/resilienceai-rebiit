"""ResilienceAI backend — serves the command center and proxies Gemini calls.

The Gemini API key lives server-side (GEMINI_API_KEY env var); the browser
never sees it. Deployed on Cloud Run.
"""
import json
import os
import re

import requests
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=None)

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent?key={key}"
)
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Baseline Delta City state — mirrors the frontend's simulated data plane.
CITY_CONTEXT = """You are the ResilienceAI Response Assistant for Delta City (pop 2.4M),
a Southeast Asian river city during monsoon season. A tropical storm is approaching.
Current district risk: Riverside North HIGH (pop 320k, gauge 5.9m and rising,
forecast peak 6.8m at T+42h vs danger stage 6.5m, flood stage 5.5m), Kampung Baru HIGH
(pop 285k, informal housing along embankment), Old Quarter MODERATE (260k),
Southbank MODERATE (315k), CBD LOW (210k), Hillside West LOW (240k), Eastgate LOW (360k),
Harbor East LOW (300k). 180mm rain forecast within 48h. Sensors: 40/42 online
(2 offline in Eastgate). Shelters: 12 active, 18,400 spaces open of 30,000.
7 active alerts (2 HIGH, 5 MODERATE). Answer as a concise, calm operations assistant:
ground every answer in this data, cite figures, and remind that public-facing actions
require human confirmation. When recommending evacuations, always end by checking
shelter capacity: state whether the 18,400 open shelter spaces are sufficient for the
population being evacuated. When asked for advisories, provide English and Bahasa
Indonesia versions."""


def call_gemini(payload):
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY not configured")
    resp = requests.post(
        GEMINI_URL.format(key=GEMINI_KEY),
        json=payload,
        timeout=60,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("empty Gemini response")
    return text


@app.get("/api/health")
def health():
    return jsonify({"mode": "live" if GEMINI_KEY else "demo"})


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
    context = body.get("context") or CITY_CONTEXT
    payload = {
        "systemInstruction": {"parts": [{"text": str(context)[:12000]}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2048},
    }
    try:
        return jsonify({"text": call_gemini(payload)})
    except Exception as exc:  # surface a clean error; frontend falls back to demo mode
        return jsonify({"error": str(exc)}), 502


@app.post("/api/triage")
def triage():
    body = request.get_json(force=True, silent=True) or {}
    image = body.get("imageBase64") or ""
    mime = body.get("mimeType") or "image/jpeg"
    note = (body.get("note") or "").strip()
    if not image:
        return jsonify({"error": "imageBase64 required"}), 400
    if len(image) > 8_000_000:
        return jsonify({"error": "image too large"}), 413
    instruction = (
        "You are triaging a citizen disaster report for Delta City flood operations. "
        "Classify the attached photo (and note, if any). Return ONLY JSON with keys: "
        '"type" (one of flooding|blocked_drain|landslide|infrastructure_damage|other), '
        '"severity" (LOW|MODERATE|HIGH|SEVERE), "confidence" (0-100 integer), '
        '"duplicate_suspect" (boolean), "summary" (one sentence).'
    )
    parts = [
        {"text": instruction + (f' Citizen note: "{note[:500]}"' if note else "")},
        {"inline_data": {"mime_type": mime, "data": image}},
    ]
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        text = call_gemini(payload)
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        return jsonify(json.loads(text))
    except json.JSONDecodeError:
        return jsonify({"error": "unparseable model output"}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.get("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path):
    if path.endswith(".html") or "." not in path:
        return send_from_directory(APP_DIR, "index.html")
    return send_from_directory(APP_DIR, path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

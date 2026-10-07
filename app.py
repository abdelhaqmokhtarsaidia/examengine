"""ExamEngine — serveur : limite d'examens par appareil/IP côté serveur, multi-IA avec bascule automatique."""
import os, time, json, re, sqlite3, secrets, threading, math
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from functools import wraps
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, session, g
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()
app = Flask(__name__, static_folder="static", static_url_path="")
if os.getenv("BEHIND_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
SECRET = os.getenv("SECRET_KEY")
if not SECRET:
    print("ATTENTION: SECRET_KEY absent, les appareils seront réinitialisés à chaque redémarrage.")
    SECRET = secrets.token_hex(32)
app.secret_key = SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=400), MAX_CONTENT_LENGTH=2 * 1024 * 1024)

EXAMS_PER_DAY = int(os.getenv("EXAMS_PER_DAY", "2"))            # examens par appareil
IP_EXAMS = int(os.getenv("IP_EXAMS_PER_WINDOW", "6"))           # filet de sécurité par adresse IP
WINDOW = float(os.getenv("LIMIT_WINDOW_HOURS", "24")) * 3600    # fenêtre glissante
AI_CALLS_PER_EXAM = int(os.getenv("AI_CALLS_PER_EXAM", "30"))
ANALYSE_PER_DAY = int(os.getenv("AI_ANALYSE_PER_DAY", "10"))
TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET_HOURS", "1"))))  # 1 = Algérie
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
TIMEOUT = int(os.getenv("AI_TIMEOUT", "90"))
MAX_PROMPT = 150_000

# ---------- Base de données (SQLite) ----------
DB_PATH = os.getenv("DB_PATH", "examengine.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS starts(id INTEGER PRIMARY KEY AUTOINCREMENT, did TEXT, ip TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS i_starts_did ON starts(did, ts);
CREATE INDEX IF NOT EXISTS i_starts_ip ON starts(ip, ts);
CREATE TABLE IF NOT EXISTS tickets(did TEXT PRIMARY KEY, left INTEGER, exp REAL, ok INTEGER);
CREATE TABLE IF NOT EXISTS usage(did TEXT, day TEXT, kind TEXT, n INTEGER, PRIMARY KEY(did,day,kind));
"""
def init_db():
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.close()
init_db()

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

@contextmanager
def tx(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

today = lambda: datetime.now(TZ).strftime("%Y-%m-%d")

def device():
    """Identifiant anonyme de l'appareil, stocké dans un cookie signé (1 an+)."""
    did = session.get("did")
    if not did:
        did = session["did"] = secrets.token_urlsafe(16)
        session.permanent = True
    return did

def client_ip():
    return request.remote_addr or "?"

@app.before_request
def csrf_guard():
    if request.path.startswith("/api/") and request.method != "GET" and request.headers.get("X-Requested-With") != "fetch":
        return jsonify(error="Requête refusée."), 400

def limit_state(db, did, ip):
    """État de la limite : examens utilisés dans la fenêtre glissante et secondes avant le prochain examen possible."""
    now = time.time(); cut = now - WINDOW
    td = [r["ts"] for r in db.execute("SELECT ts FROM starts WHERE did=? AND ts>? ORDER BY ts", (did, cut))]
    ti = [r["ts"] for r in db.execute("SELECT ts FROM starts WHERE ip=? AND ts>? ORDER BY ts", (ip, cut))]
    retry = 0.0
    if len(td) >= EXAMS_PER_DAY:
        retry = max(retry, td[len(td) - EXAMS_PER_DAY] + WINDOW - now)
    ip_blocked = len(ti) >= IP_EXAMS
    if ip_blocked:
        retry = max(retry, ti[len(ti) - IP_EXAMS] + WINDOW - now)
    used = EXAMS_PER_DAY if ip_blocked else min(len(td), EXAMS_PER_DAY)
    return {"used": used, "max": EXAMS_PER_DAY, "retry_in": max(0, math.ceil(retry))}

# ---------- Fournisseurs d'IA avec bascule ----------
def env_keys(name):
    return [k.strip() for k in os.getenv(name, "").split(",") if k.strip()]

# kind: openai = API compatible OpenAI ; anthropic ; gemini
CATALOG = {
    "anthropic":  dict(env="ANTHROPIC_API_KEY", kind="anthropic", model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")),
    "openai":     dict(env="OPENAI_API_KEY", kind="openai", url="https://api.openai.com/v1/chat/completions", model=os.getenv("OPENAI_MODEL", "gpt-4o-mini")),
    "gemini":     dict(env="GEMINI_API_KEY", kind="gemini", model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash")),
    "groq":       dict(env="GROQ_API_KEY", kind="openai", url="https://api.groq.com/openai/v1/chat/completions", model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")),
    "mistral":    dict(env="MISTRAL_API_KEY", kind="openai", url="https://api.mistral.ai/v1/chat/completions", model=os.getenv("MISTRAL_MODEL", "mistral-small-latest")),
    "deepseek":   dict(env="DEEPSEEK_API_KEY", kind="openai", url="https://api.deepseek.com/chat/completions", model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat")),
    "openrouter": dict(env="OPENROUTER_API_KEY", kind="openai", url="https://openrouter.ai/api/v1/chat/completions", model=os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")),
}
ORDER = [x.strip() for x in os.getenv("AI_ORDER", ",".join(CATALOG)).split(",") if x.strip() in CATALOG]

# Un "slot" = un fournisseur + une clé (plusieurs clés possibles, séparées par des virgules)
SLOTS = []
for pid in ORDER:
    for i, key in enumerate(env_keys(CATALOG[pid]["env"])):
        SLOTS.append({**CATALOG[pid], "id": pid, "n": i + 1, "key": key, "cool": 0.0, "why": "", "ok": 0, "ko": 0})
LOCK = threading.Lock()
LAST = {"id": None}

class ProviderError(Exception):
    def __init__(self, status, body, retry=None):
        self.status, self.body, self.retry = status, str(body)[:300], retry

def call(slot, prompt):
    try:
        if slot["kind"] == "openai":
            r = requests.post(slot["url"], timeout=TIMEOUT,
                headers={"Authorization": "Bearer " + slot["key"]},
                json={"model": slot["model"], "temperature": 0.4, "messages": [{"role": "user", "content": prompt}]})
        elif slot["kind"] == "anthropic":
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=TIMEOUT,
                headers={"x-api-key": slot["key"], "anthropic-version": "2023-06-01"},
                json={"model": slot["model"], "max_tokens": 6000, "messages": [{"role": "user", "content": prompt}]})
        else:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{slot['model']}:generateContent", timeout=TIMEOUT,
                headers={"x-goog-api-key": slot["key"]},
                json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.4}})
    except requests.RequestException as x:
        raise ProviderError(0, x)
    if r.status_code != 200:
        ra = r.headers.get("retry-after")
        raise ProviderError(r.status_code, r.text, int(ra) if ra and ra.isdigit() else None)
    d = r.json()
    try:
        if slot["kind"] == "openai":
            return d["choices"][0]["message"]["content"]
        if slot["kind"] == "anthropic":
            return "".join(b.get("text", "") for b in d["content"] if b.get("type") == "text")
        return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError):
        raise ProviderError(502, d)

def cooldown(err):
    """Durée de mise à l'écart d'un fournisseur selon l'erreur."""
    b = err.body.lower()
    if err.status == 401:
        return 86400, "clé invalide"
    if err.status in (402, 403) or any(w in b for w in ("quota", "insufficient", "billing", "credit", "exhausted", "per day")):
        return int(os.getenv("AI_LONG_COOLDOWN", "3600")), "quota épuisé"
    if err.status == 429:
        return min(max(err.retry or 60, 20), 3600), "limite atteinte"
    if err.status in (400, 404):
        return 300, "requête/modèle refusé"
    return 30, "indisponible"

def generate(prompt):
    tried = []
    for slot in SLOTS:
        with LOCK:
            if slot["cool"] > time.time():
                continue
        try:
            text = call(slot, prompt)
        except Exception as exc:
            err = exc if isinstance(exc, ProviderError) else ProviderError(500, exc)
            secs, why = cooldown(err)
            with LOCK:
                slot["cool"], slot["why"], slot["ko"] = time.time() + secs, why, slot["ko"] + 1
            app.logger.warning("%s#%s -> %s (%s) pause %ss", slot["id"], slot["n"], err.status, why, secs)
            tried.append(slot["id"])
            continue
        with LOCK:
            slot["ok"] += 1
            switched = LAST["id"] not in (None, slot["id"])
            LAST["id"] = slot["id"]
        return {"text": text, "provider": slot["id"], "switched": switched}
    return None

HITS = defaultdict(deque)
def limited(key):
    q, now = HITS[key], time.time()
    while q and q[0] < now - 600:
        q.popleft()
    if len(q) >= int(os.getenv("RATE_LIMIT_10MIN", "80")):
        return True
    q.append(now)
    return False

# ---------- Limite d'examens (côté serveur) ----------
@app.get("/api/me")
def me():
    device()
    return jsonify(limit_state(get_db(), device(), client_ip()))

@app.post("/api/exam/start")
def exam_start():
    db, did, ip = get_db(), device(), client_ip()
    allowed = False
    with tx(db):
        db.execute("DELETE FROM starts WHERE ts<?", (time.time() - 7 * 86400,))
        st = limit_state(db, did, ip)
        if st["retry_in"] == 0:
            allowed = True
            db.execute("INSERT INTO starts(did,ip,ts) VALUES(?,?,?)", (did, ip, time.time()))
            db.execute("INSERT INTO tickets(did,left,exp,ok) VALUES(?,?,?,0) ON CONFLICT(did) DO UPDATE SET left=excluded.left,exp=excluded.exp,ok=0",
                       (did, AI_CALLS_PER_EXAM, time.time() + 1200))
            st = limit_state(db, did, ip)
    if not allowed:
        return jsonify({**st, "error": "Limite atteinte : vous avez utilisé vos examens. Revenez quand le compte à rebours est terminé."}), 429
    return jsonify(st)

@app.post("/api/exam/cancel")
def exam_cancel():
    db, did = get_db(), device()
    with tx(db):
        t = db.execute("SELECT ok,exp FROM tickets WHERE did=?", (did,)).fetchone()
        if t and t["ok"] == 0 and t["exp"] > time.time():  # remboursé seulement si aucune génération n'a réussi
            db.execute("DELETE FROM tickets WHERE did=?", (did,))
            db.execute("DELETE FROM starts WHERE id=(SELECT id FROM starts WHERE did=? ORDER BY ts DESC LIMIT 1)", (did,))
    return jsonify(limit_state(db, did, client_ip()))

# ---------- IA ----------
@app.post("/api/ai")
def api_ai():
    db, did = get_db(), device()
    body = request.get_json(silent=True) or {}
    prompt, kind = body.get("prompt", ""), body.get("kind", "gen")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT or kind not in ("gen", "analyse"):
        return jsonify(error="Requête invalide"), 400
    if limited(did) or limited("ip:" + client_ip()):
        return jsonify(error="Trop de requêtes, réessayez dans quelques minutes."), 429
    if not SLOTS:
        return jsonify(error="Aucune clé API configurée (voir .env)."), 500
    ok = False
    with tx(db):
        if kind == "analyse":
            r = db.execute("SELECT n FROM usage WHERE did=? AND day=? AND kind='analyse'", (did, today())).fetchone()
            if (r["n"] if r else 0) < ANALYSE_PER_DAY:
                ok = True
                db.execute("INSERT INTO usage(did,day,kind,n) VALUES(?,?,'analyse',1) ON CONFLICT(did,day,kind) DO UPDATE SET n=n+1", (did, today()))
        else:
            t = db.execute("SELECT left,exp FROM tickets WHERE did=?", (did,)).fetchone()
            if t and t["left"] > 0 and t["exp"] > time.time():
                ok = True
                db.execute("UPDATE tickets SET left=left-1 WHERE did=?", (did,))
    if not ok:
        return jsonify(error="Quota atteint : lancez un examen pour générer des questions."), 403
    out = generate(prompt)
    if out is None:
        wait = max(0, int(min(s["cool"] for s in SLOTS) - time.time()))
        return jsonify(error=f"Toutes les IA ont atteint leur limite. Réessayez dans ~{wait // 60 + 1} min."), 503
    if kind == "gen":
        db.execute("UPDATE tickets SET ok=ok+1 WHERE did=?", (did,))
    return jsonify(out)

@app.get("/api/status")
def status():
    if not ADMIN_TOKEN or request.args.get("token") != ADMIN_TOKEN:
        return jsonify(error="Réservé à l'administrateur."), 403
    now = time.time()
    return jsonify([{"provider": s["id"], "key": s["n"], "model": s["model"], "available": s["cool"] <= now,
                     "reason": s["why"] if s["cool"] > now else "", "retry_in_s": max(0, int(s["cool"] - now)),
                     "ok": s["ok"], "errors": s["ko"]} for s in SLOTS])

@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.get("/healthz")
def healthz():
    return "ok"

if __name__ == "__main__":
    print("Fournisseurs IA actifs:", [f"{s['id']}#{s['n']}" for s in SLOTS] or "AUCUN")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

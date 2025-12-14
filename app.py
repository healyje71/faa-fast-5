import csv
import hashlib
import json
import os
import random
import sqlite3
from datetime import date
from functools import wraps

from flask import Flask, g, redirect, render_template_string, request, session, url_for

APP_TITLE = "FAA Daily Quiz"
DB_FILE = "faa_local.db"
QUESTION_FILE = "faa_questions.csv"
QUESTIONS_PER_DAY = 5

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_FILE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        streak INTEGER DEFAULT 0,
        last_completed TEXT
    );

    CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        quiz_date TEXT NOT NULL,
        question_ids_json TEXT NOT NULL,
        answers_json TEXT NOT NULL,
        submitted INTEGER NOT NULL DEFAULT 0,
        score INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(user_id, quiz_date)
    );

    CREATE TABLE IF NOT EXISTS reactions (
        user_id INTEGER NOT NULL,
        quiz_date TEXT NOT NULL,
        question_id TEXT NOT NULL,
        reaction TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, quiz_date, question_id)
    );
    """)
    db.commit()


def hash_password(pw: str) -> str:
    salt = "faa-quiz-local-v1"
    return hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def load_questions():
    if not os.path.exists(QUESTION_FILE):
        raise FileNotFoundError(f"Missing {QUESTION_FILE} next to app.py")
    with open(QUESTION_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def stable_rng(user_id: int, day: str, tag: str):
    seed = int(hashlib.sha256(f"{user_id}:{day}:{tag}".encode("utf-8")).hexdigest()[:16], 16)
    return random.Random(seed)

def pick_daily_questions(all_qs, user_id: int, day: str):
    rng = stable_rng(user_id, day, "daily")
    picked = rng.sample(all_qs, min(QUESTIONS_PER_DAY, len(all_qs)))
    out = []
    for q in picked:
        out.append({
            "id": str(q["id"]),
            "question": q["question"],
            "choices": {"A": q["choice_a"], "B": q["choice_b"], "C": q["choice_c"]},
            "correct": q["correct"].strip().upper(),
            "category": (q.get("category") or "Uncategorized").strip()
        })
    return out

def grade(questions, answers):
    return sum(1 for q in questions if answers.get(q["id"]) == q["correct"])


BASE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{{title}}</title>
  <link rel="manifest" href="/static/manifest.webmanifest">
  <meta name="theme-color" content="#111111">

  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 0; background: #f7f7fb; color: #111; }
    .wrap { max-width: 900px; margin: 0 auto; padding: 20px; }
    .card { background: white; border-radius: 14px; box-shadow: 0 6px 18px rgba(0,0,0,.06); padding: 18px; margin: 14px 0; }
    .meta { display:flex; gap:10px; flex-wrap:wrap; color:#444; font-size: 13px; align-items:center; justify-content:space-between; }
    .pill { background:#eef; border-radius: 999px; padding: 4px 10px; }
    input { padding: 10px 12px; border-radius: 10px; border: 1px solid #ddd; width: 240px; }
    button { border: 0; border-radius: 12px; padding: 10px 14px; cursor:pointer; background:#111; color:white; font-weight:600; }
    button.secondary { background:#e9e9ef; color:#111; }
    .row { display:flex; gap: 10px; flex-wrap:wrap; align-items:center; }
    .small { font-size: 13px; color:#555; }
    .err { color:#b00; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="meta">
      <span class="pill">✈️ {{app_title}}</span>
      <span>
        {% if username %}
          <span class="pill">👤 {{username}}</span>
          <a class="pill" href="{{url_for('quiz')}}" style="text-decoration:none;">Today</a>
          <a class="pill" href="{{url_for('logout')}}" style="text-decoration:none;">Logout</a>
        {% else %}
          <a class="pill" href="{{url_for('login')}}" style="text-decoration:none;">Login</a>
          <a class="pill" href="{{url_for('register')}}" style="text-decoration:none;">Register</a>
        {% endif %}
      </span>
    </div>
    {{body|safe}}
  </div>
<script>
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/static/sw.js");
  }
</script>

</body>
</html>
"""

LOGIN = """
<div class="card">
  <h2>Login</h2>
  {% if error %}<p class="err">{{error}}</p>{% endif %}
  <form method="post">
    <div class="row">
      <input name="username" placeholder="username" required />
      <input name="password" type="password" placeholder="password" required />
      <button type="submit">login</button>
    </div>
  </form>
</div>
"""

REGISTER = """
<div class="card">
  <h2>Register</h2>
  {% if error %}<p class="err">{{error}}</p>{% endif %}
  <form method="post">
    <div class="row">
      <input name="username" placeholder="username" minlength="3" required />
      <input name="password" type="password" placeholder="password" minlength="6" required />
      <button type="submit">create</button>
    </div>
  </form>
</div>
"""

QUIZ = """
<div class="card" style="padding:0; overflow:hidden;">
  <div style="padding:16px;">
    <div class="meta">
      <span class="pill">📅 {{quiz_date}}</span>
      <span class="pill">🔥 streak: {{streak}}</span>
    </div>
    <div style="margin-top:10px;">
      <div style="font-size:18px; font-weight:800;">today’s 5 just dropped ✈️</div>
      <div class="small">tap answers. quick feedback. done fast.</div>
    </div>
  </div>

  <div id="chat" style="background:#f7f7fb; padding:14px; height:65vh; overflow:auto;"></div>

  <div style="padding:12px; background:#fff; border-top:1px solid #eee;">
    <div class="row" style="justify-content:space-between;">
      <div class="small" id="progressText">loading…</div>
      <div class="small" id="dots">○○○○○</div>
    </div>
    <div style="display:grid; gap:10px; margin-top:10px;">
      <button id="btnA" disabled style="padding:14px; border-radius:14px;">A</button>
      <button id="btnB" disabled style="padding:14px; border-radius:14px;">B</button>
      <button id="btnC" disabled style="padding:14px; border-radius:14px;">C</button>
    </div>
    <div class="row" style="margin-top:10px;">
      <button class="secondary" id="repBtn" style="flex:1;">🔁 more reps</button>
      <button class="secondary" id="gotBtn" style="flex:1;">👍 got it</button>
      <button id="finishBtn" style="flex:1;" disabled>finish</button>
    </div>
  </div>
</div>

<script>
  const QUESTIONS = {{ questions|tojson }};
  const ANSWERS = {{ answers|tojson }};
  const SUBMITTED = {{ 'true' if submitted else 'false' }};
  let idx = 0;
  let lastQ = null;

  const correctNormal = ["🔥 nice. locked in.", "solid. keep rolling →", "clean answer. this shows up a lot."];
  const wrongNormal = ["close — this one trips people up.", "not quite. quick fix below.", "ehh almost. bookmark this one 💾"];
  const chat = document.getElementById('chat');
  const btnA = document.getElementById('btnA');
  const btnB = document.getElementById('btnB');
  const btnC = document.getElementById('btnC');
  const finishBtn = document.getElementById('finishBtn');
  const repBtn = document.getElementById('repBtn');
  const gotBtn = document.getElementById('gotBtn');
  const progressText = document.getElementById('progressText');
  const dots = document.getElementById('dots');

  function bubble(text, side="left") {
    const wrap = document.createElement('div');
    wrap.style.display = 'flex';
    wrap.style.justifyContent = (side === "right" ? "flex-end" : "flex-start");
    wrap.style.margin = "10px 0";
    const b = document.createElement('div');
    b.style.maxWidth = "82%";
    b.style.padding = "12px 14px";
    b.style.borderRadius = "16px";
    b.style.boxShadow = "0 6px 18px rgba(0,0,0,.05)";
    b.style.whiteSpace = "pre-wrap";
    b.style.background = (side === "right" ? "#111" : "#fff");
    b.style.color = (side === "right" ? "#fff" : "#111");
    b.innerText = text;
    wrap.appendChild(b);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
  }

  function setButtons(q) {
    btnA.disabled = false; btnB.disabled = false; btnC.disabled = false;
    btnA.textContent = `A · ${q.choices.A}`;
    btnB.textContent = `B · ${q.choices.B}`;
    btnC.textContent = `C · ${q.choices.C}`;
  }

  function updateProgress() {
    const answered = Object.keys(ANSWERS).length;
    progressText.textContent = `${answered} down · ${QUESTIONS.length - answered} to go`;
    const filled = Math.min(answered, 5);
    dots.textContent = "●".repeat(filled) + "○".repeat(5-filled);
    finishBtn.disabled = !(answered === QUESTIONS.length) || SUBMITTED;
  }

  async function saveAnswer(qid, letter) {
    const form = new FormData();
    form.append(`q_${qid}`, letter);
    await fetch("/save", { method: "POST", body: form });
  }

  async function react(type) {
    if (!lastQ) return;
    const form = new FormData();
    form.append("question_id", lastQ.id);
    form.append("reaction", type);
    await fetch("/react", { method: "POST", body: form });
    bubble(type === "more_reps" ? "cool — we’ll hit this again soon" : "logged 👍");
  }

  async function onPick(letter) {
    const q = QUESTIONS[idx];
    if (!q || SUBMITTED) return;
    lastQ = q;

    ANSWERS[q.id] = letter;
    bubble(letter, "right");
    await saveAnswer(q.id, letter);

    const correct = q.correct;
    if (letter === correct) {
      bubble(correctNormal[Math.floor(Math.random()*correctNormal.length)]);
    } else {
      bubble(wrongNormal[Math.floor(Math.random()*wrongNormal.length)]);
      bubble(`quick why: correct is ${correct}. (we’ll add real explanations later)`);
    }

    idx++;
    if (idx < QUESTIONS.length) showQuestion();
    else bubble("done 🎉 hit finish to lock it in.");

    updateProgress();
  }

  function showQuestion() {
    const q = QUESTIONS[idx];
    if (!q) return;
    const cat = q.category ? `· ${q.category}` : "";
    bubble(`Q${idx+1}/5 ${cat}\n${q.question}`);
    setButtons(q);

    if (ANSWERS[q.id]) {
      idx++;
      if (idx < QUESTIONS.length) showQuestion();
    }
  }

  btnA.onclick = () => onPick("A");
  btnB.onclick = () => onPick("B");
  btnC.onclick = () => onPick("C");
  repBtn.onclick = () => react("more_reps");
  gotBtn.onclick = () => react("got_it");

  finishBtn.onclick = async () => {
    const resp = await fetch("/submit", { method: "POST" });
    if (resp.redirected) window.location = resp.url;
  };

  bubble("hey 👋 ready for today’s 5?");
  updateProgress();
  if (!SUBMITTED) showQuestion();
  else bubble("you already finished today ✅ come back tomorrow.");
</script>
"""

@app.before_request
def ensure_db():
    init_db()

@app.get("/")
def home():
    if "user_id" in session:
        return redirect(url_for("quiz"))
    return redirect(url_for("login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if len(username) < 3:
            error = "username must be 3+ chars"
        elif len(password) < 6:
            error = "password must be 6+ chars"
        else:
            db = get_db()
            try:
                db.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, hash_password(password), date.today().isoformat()),
                )
                db.commit()
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                error = "username already taken"
    body = render_template_string(REGISTER, error=error)
    return render_template_string(BASE, title="Register", body=body, app_title=APP_TITLE, username=session.get("username"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user or user["password_hash"] != hash_password(password):
            error = "invalid login"
        else:
            session["user_id"] = int(user["id"])
            session["username"] = user["username"]
            return redirect(url_for("quiz"))
    body = render_template_string(LOGIN, error=error)
    return render_template_string(BASE, title="Login", body=body, app_title=APP_TITLE, username=session.get("username"))

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

def get_attempt(user_id: int, day: str):
    db = get_db()
    row = db.execute("SELECT * FROM attempts WHERE user_id=? AND quiz_date=?", (user_id, day)).fetchone()
    all_qs = load_questions()

    if row is None:
        questions = pick_daily_questions(all_qs, user_id, day)
        qids = [q["id"] for q in questions]
        now = date.today().isoformat()
        db.execute(
            "INSERT INTO attempts (user_id, quiz_date, question_ids_json, answers_json, submitted, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (user_id, day, json.dumps(qids), json.dumps({}), now, now),
        )
        db.commit()
        row = db.execute("SELECT * FROM attempts WHERE user_id=? AND quiz_date=?", (user_id, day)).fetchone()

    qids = json.loads(row["question_ids_json"])
    qmap = {str(q["id"]): q for q in pick_daily_questions(all_qs, user_id, day)}
    questions = [qmap[qid] for qid in qids if qid in qmap]
    answers = json.loads(row["answers_json"])
    return row, questions, answers

@app.get("/quiz")
@login_required
def quiz():
    user_id = int(session["user_id"])
    day = date.today().isoformat()
    row, questions, answers = get_attempt(user_id, day)

    db = get_db()
    u = db.execute("SELECT streak FROM users WHERE id=?", (user_id,)).fetchone()
    streak = int(u["streak"] or 0)

    body = render_template_string(
        QUIZ,
        quiz_date=day,
        questions=questions,
        answers=answers,
        submitted=bool(row["submitted"]),
        streak=streak,
    )
    return render_template_string(BASE, title="Quiz", body=body, app_title=APP_TITLE, username=session.get("username"))

@app.post("/save")
@login_required
def save():
    user_id = int(session["user_id"])
    day = date.today().isoformat()
    row, questions, answers = get_attempt(user_id, day)
    if row["submitted"]:
        return redirect(url_for("quiz"))

    for q in questions:
        qid = q["id"]
        val = request.form.get(f"q_{qid}")
        if val in {"A", "B", "C"}:
            answers[qid] = val

    db = get_db()
    db.execute("UPDATE attempts SET answers_json=?, updated_at=? WHERE id=?", (json.dumps(answers), day, row["id"]))
    db.commit()
    return redirect(url_for("quiz"))

@app.post("/react")
@login_required
def react():
    user_id = int(session["user_id"])
    day = date.today().isoformat()
    qid = request.form.get("question_id")
    reaction = request.form.get("reaction")
    if reaction not in {"got_it", "more_reps"} or not qid:
        return ("bad", 400)

    db = get_db()
    db.execute(
        """
        INSERT INTO reactions (user_id, quiz_date, question_id, reaction, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, quiz_date, question_id) DO UPDATE SET reaction=excluded.reaction
        """,
        (user_id, day, qid, reaction, day),
    )
    db.commit()
    return ("ok", 200)

@app.post("/submit")
@login_required
def submit():
    user_id = int(session["user_id"])
    day = date.today().isoformat()
    row, questions, answers = get_attempt(user_id, day)
    if row["submitted"]:
        return redirect(url_for("quiz"))

    if any(answers.get(q["id"]) not in {"A", "B", "C"} for q in questions):
        return redirect(url_for("quiz"))

    score = grade(questions, answers)

    db = get_db()
    db.execute("UPDATE attempts SET submitted=1, score=?, updated_at=? WHERE id=?", (score, day, row["id"]))

    # simple streak
    u = db.execute("SELECT streak, last_completed FROM users WHERE id=?", (user_id,)).fetchone()
    streak = int(u["streak"] or 0)
    last = u["last_completed"]

    from datetime import datetime, timedelta
    d = datetime.strptime(day, "%Y-%m-%d").date()
    y = (d - timedelta(days=1)).isoformat()
    if last == y:
        streak += 1
    else:
        streak = 1

    db.execute("UPDATE users SET streak=?, last_completed=? WHERE id=?", (streak, day, user_id))
    db.commit()
    return redirect(url_for("quiz"))

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)

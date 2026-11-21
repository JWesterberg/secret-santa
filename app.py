#!/usr/bin/env python3
"""Secret Santa: draw names and email each person their match.

Run:  python3 app.py   then open http://localhost:8000

The organizer never sees the assignments. They are generated in memory,
emailed, and discarded. Nothing is written to disk or logged. The browser
only ever receives the list of addresses that were (or were not) sent.
"""

import html
import json
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Messages that failed to send, kept only in memory so they can be retried
# without redrawing (which would hand out conflicting assignments).
_pending_lock = threading.Lock()
_pending: list = []
_pending_smtp: dict = {}


class UserError(Exception):
    pass


def draw(people):
    """Return a list where result[i] is the index person i gives to.

    Uniform random derangement (nobody draws themselves) that also respects
    exclusions: people sharing a non-empty "group" never draw each other.
    """
    n = len(people)
    rng = secrets.SystemRandom()
    for _ in range(20000):
        order = list(range(n))
        rng.shuffle(order)
        ok = True
        for giver, receiver in enumerate(order):
            if giver == receiver:
                ok = False
                break
            g1, g2 = people[giver]["group"], people[receiver]["group"]
            if g1 and g1 == g2:
                ok = False
                break
        if ok:
            return order
    raise UserError(
        "Couldn't find a valid draw with those exclusion groups. "
        "A group can't contain more than half the participants."
    )


def render(template, giver, receiver, event):
    values = {"name": giver["name"], "match": receiver["name"], "event": event}
    return re.sub(r"\{(name|match|event)\}", lambda m: values[m.group(1)], template)


def build_message(cfg, giver, receiver):
    subject = render(cfg["subject"], giver, receiver, cfg["event"])
    directions = render(cfg["directions"], giver, receiver, cfg["event"])

    text = (
        f"Hi {giver['name']},\n\n"
        f"You are the Secret Santa for:\n\n    {receiver['name']}\n\n"
        f"{directions}\n\n"
        "Keep it secret!\n"
    )
    dirs_html = html.escape(directions).replace("\n", "<br>")
    body_html = f"""\
<div style="font-family:Georgia,serif;max-width:560px;margin:auto;color:#222">
  <p>Hi {html.escape(giver['name'])},</p>
  <p>You are the Secret Santa for:</p>
  <p style="font-size:28px;font-weight:bold;color:#b3261e;margin:16px 0">
    {html.escape(receiver['name'])}</p>
  <hr style="border:none;border-top:1px solid #ddd">
  <p>{dirs_html}</p>
  <p><em>Keep it secret!</em></p>
</div>"""

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["from_addr"]))
    msg["To"] = formataddr((giver["name"], giver["email"]))
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")
    return msg


def smtp_connect(smtp):
    host, port = smtp["host"], int(smtp["port"])
    ctx = ssl.create_default_context()
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
        s.starttls(context=ctx)
    if smtp["user"]:
        s.login(smtp["user"], smtp["password"])
    return s


def send_all(smtp, messages):
    """Send (email, msg) pairs. Return (sent_emails, failed_pairs)."""
    sent, failed = [], []
    conn = None
    for email, msg in messages:
        for attempt in range(3):
            try:
                if conn is None:
                    conn = smtp_connect(smtp)
                conn.send_message(msg)
                sent.append(email)
                break
            except Exception:
                try:
                    conn and conn.quit()
                except Exception:
                    pass
                conn = None
                time.sleep(1 + attempt * 2)
        else:
            failed.append((email, msg))
    try:
        conn and conn.quit()
    except Exception:
        pass
    return sent, failed


def parse_config(data):
    def s(key, default=""):
        return str(data.get(key, default) or "").strip()

    smtp = {
        "host": s("smtp_host"),
        "port": s("smtp_port", "587"),
        "user": s("smtp_user"),
        "password": str(data.get("smtp_password") or ""),
    }
    if not smtp["host"]:
        raise UserError("SMTP server is required.")
    if not smtp["port"].isdigit():
        raise UserError("SMTP port must be a number.")

    cfg = {
        "from_addr": s("from_addr") or smtp["user"],
        "from_name": s("from_name") or "Secret Santa",
        "subject": s("subject") or "Your Secret Santa assignment",
        "directions": str(data.get("directions") or "").strip(),
        "event": s("event"),
    }
    if not EMAIL_RE.match(cfg["from_addr"]):
        raise UserError("A valid 'From' email address is required.")
    return smtp, cfg


def parse_people(data):
    people, seen = [], set()
    for row in data.get("people") or []:
        name = str(row.get("name") or "").strip()
        email = str(row.get("email") or "").strip()
        group = str(row.get("group") or "").strip().lower()
        if not name and not email:
            continue
        if not name or not EMAIL_RE.match(email):
            raise UserError(f"Check the row for '{name or email}': needs a name and valid email.")
        if email.lower() in seen:
            raise UserError(f"{email} is listed twice.")
        seen.add(email.lower())
        people.append({"name": name, "email": email, "group": group})
    if len(people) < 3:
        raise UserError("You need at least 3 people.")
    return people


def handle_send(data):
    smtp, cfg = parse_config(data)
    people = parse_people(data)

    # Verify login before drawing so a bad password doesn't burn a draw.
    try:
        smtp_connect(smtp).quit()
    except Exception as e:
        raise UserError(f"Couldn't log in to the email server: {e}")

    order = draw(people)
    messages = [(p["email"], build_message(cfg, p, people[order[i]])) for i, p in enumerate(people)]
    del order  # assignments now exist only inside the outgoing messages

    sent, failed = send_all(smtp, messages)
    messages.clear()
    with _pending_lock:
        _pending[:] = failed
        _pending_smtp.clear()
        _pending_smtp.update(smtp if failed else {})
    return {"sent": sent, "failed": [e for e, _ in failed]}


def handle_retry():
    with _pending_lock:
        if not _pending:
            raise UserError("Nothing to retry.")
        sent, failed = send_all(dict(_pending_smtp), list(_pending))
        _pending[:] = failed
        if not failed:
            _pending_smtp.clear()
    return {"sent": sent, "failed": [e for e, _ in failed]}


def handle_test(data):
    """Send a sample email to the organizer with a placeholder match."""
    smtp, cfg = parse_config(data)
    to = str(data.get("test_to") or "").strip() or cfg["from_addr"]
    if not EMAIL_RE.match(to):
        raise UserError("Enter a valid address for the test email.")
    giver = {"name": "Test Person", "email": to}
    receiver = {"name": "(their match will appear here)"}
    msg = build_message(cfg, giver, receiver)
    msg.replace_header("Subject", "[TEST] " + msg["Subject"])
    try:
        conn = smtp_connect(smtp)
        conn.send_message(msg)
        conn.quit()
    except Exception as e:
        raise UserError(f"Test email failed: {e}")
    return {"sent": [to], "failed": []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep request bodies out of the console
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        routes = {"/api/send": handle_send, "/api/test": handle_test}
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/retry":
                result = handle_retry()
            elif self.path in routes:
                result = routes[self.path](data)
            else:
                self.send_error(404)
                return
            self._json(200, result)
        except UserError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(500, {"error": f"Unexpected error: {type(e).__name__}"})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Secret Santa</title>
<style>
:root{--red:#b3261e;--bg:#faf7f2;--card:#fff;--line:#e5ded3;--muted:#6b645a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);font:15px/1.5 system-ui,sans-serif;color:#222}
main{max-width:760px;margin:0 auto;padding:24px 16px 64px}
h1{color:var(--red);margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 24px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:18px}
h2{font-size:16px;margin:0 0 12px}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input,textarea{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;font:inherit}
textarea{min-height:160px;resize:vertical}
.row{display:grid;grid-template-columns:1fr 1.4fr .8fr 32px;gap:8px;margin-bottom:8px}
.row.head{font-size:12px;color:var(--muted);margin-bottom:4px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
button{font:inherit;border-radius:6px;border:1px solid var(--line);background:#fff;padding:8px 14px;cursor:pointer}
button.primary{background:var(--red);color:#fff;border-color:var(--red);font-weight:600;padding:12px 20px}
button.x{padding:0;width:32px;color:var(--muted)}
button:disabled{opacity:.5;cursor:default}
.hint{font-size:13px;color:var(--muted)}
code{background:#f1ece4;padding:1px 4px;border-radius:4px}
#status{margin-top:14px;padding:12px;border-radius:6px;display:none;white-space:pre-wrap}
#status.ok{display:block;background:#e8f3ea;border:1px solid #b8d8bf}
#status.err{display:block;background:#fbeaea;border:1px solid #efc2c2}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
@media(max-width:600px){.grid2{grid-template-columns:1fr}.row{grid-template-columns:1fr 1fr 32px}.row .grp{grid-column:1/3}.row.head{display:none}}
</style></head><body><main>
<h1>Secret Santa</h1>
<p class="sub">Assignments are drawn and emailed on the server. You will only see who was sent an email, never who drew whom.</p>

<section>
<h2>1. Participants</h2>
<div class="row head"><span>Name</span><span>Email</span><span>Exclusion group</span><span></span></div>
<div id="people"></div>
<div class="actions"><button type="button" id="add">+ Add person</button>
<span class="hint">Same exclusion group (e.g. <code>smiths</code>) = can't draw each other. Leave blank for none.</span></div>
</section>

<section>
<h2>2. Email content</h2>
<div class="grid2">
<div><label>Subject</label><input id="subject" value="Your Secret Santa assignment for {event}"></div>
<div><label>Event name (optional, fills <code>{event}</code>)</label><input id="event" placeholder="Family Christmas 2026"></div>
</div>
<label>Directions</label>
<textarea id="directions" placeholder="Budget: $50&#10;Exchange: Dec 24 at Grandma's&#10;Bring the gift wrapped with a tag for {match}."></textarea>
<p class="hint">Each email opens with "Hi {name}, you are the Secret Santa for: <b>[match]</b>" and then these directions. Placeholders: <code>{name}</code> recipient, <code>{match}</code> who they drew, <code>{event}</code>.</p>
</section>

<section>
<h2>3. Sending account</h2>
<p class="hint">Use an account whose Sent folder you won't open, or the assignments will be sitting there. Gmail: create a fresh account, turn on 2-step verification, and make an <a href="https://myaccount.google.com/apppasswords" target="_blank">app password</a>.</p>
<div class="grid2">
<div><label>SMTP server</label><input id="smtp_host" value="smtp.gmail.com"></div>
<div><label>Port</label><input id="smtp_port" value="587"></div>
<div><label>Username</label><input id="smtp_user" autocomplete="off" placeholder="santa.bot@gmail.com"></div>
<div><label>Password / app password</label><input id="smtp_password" type="password" autocomplete="off"></div>
<div><label>From name</label><input id="from_name" value="Secret Santa"></div>
<div><label>From address (defaults to username)</label><input id="from_addr"></div>
</div>
</section>

<section>
<h2>4. Send</h2>
<div class="actions">
<input id="test_to" placeholder="Your email for a test" style="max-width:260px">
<button type="button" id="test">Send me a test</button>
</div>
<p class="hint">The test uses a placeholder match, so it's safe to look at.</p>
<div class="actions" style="margin-top:14px">
<button type="button" class="primary" id="send">Draw names &amp; send emails</button>
<button type="button" id="retry" style="display:none">Retry failed emails</button>
</div>
<div id="status"></div>
</section>
</main>
<script>
const $ = id => document.getElementById(id);
const SAVE_KEYS = ["subject","event","directions","smtp_host","smtp_port","smtp_user","from_name","from_addr","test_to"];

function addRow(p = {}) {
  const r = document.createElement("div");
  r.className = "row";
  r.innerHTML = '<input class="name" placeholder="Name"><input class="email" type="email" placeholder="email@example.com"><input class="grp" placeholder="optional"><button type="button" class="x" title="Remove">&times;</button>';
  r.querySelector(".name").value = p.name || "";
  r.querySelector(".email").value = p.email || "";
  r.querySelector(".grp").value = p.group || "";
  r.querySelector(".x").onclick = () => { r.remove(); save(); };
  $("people").appendChild(r);
}
function people() {
  return [...document.querySelectorAll("#people .row")].map(r => ({
    name: r.querySelector(".name").value, email: r.querySelector(".email").value, group: r.querySelector(".grp").value }));
}
function payload() {
  const d = { people: people(), smtp_password: $("smtp_password").value };
  SAVE_KEYS.forEach(k => d[k] = $(k).value);
  return d;
}
function save() {  // never saves the password
  try { const d = payload(); delete d.smtp_password; localStorage.setItem("santa", JSON.stringify(d)); } catch (e) {}
}
function load() {
  let d = null;
  try { d = JSON.parse(localStorage.getItem("santa")); } catch (e) {}
  if (d) { SAVE_KEYS.forEach(k => { if (d[k] != null) $(k).value = d[k]; }); (d.people || []).forEach(addRow); }
  if (!document.querySelector("#people .row")) for (let i = 0; i < 3; i++) addRow();
}
function status(kind, text) { const s = $("status"); s.className = kind; s.textContent = text; }
function report(r) {
  let t = `Sent to ${r.sent.length}:\n` + r.sent.map(e => "  ✓ " + e).join("\n");
  if (r.failed.length) t += `\n\nFailed (${r.failed.length}):\n` + r.failed.map(e => "  ✗ " + e).join("\n") +
    "\n\nThese are held in memory. Click Retry; don't redraw, or people who already got emails will have stale matches.";
  status(r.failed.length ? "err" : "ok", t);
  $("retry").style.display = r.failed.length ? "" : "none";
}
async function post(url, body, btn) {
  const buttons = document.querySelectorAll("button"); buttons.forEach(b => b.disabled = true);
  status("ok", "Working…");
  try {
    const res = await fetch(url, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body || {}) });
    const j = await res.json();
    if (!res.ok) throw new Error(j.error || "Request failed");
    return j;
  } catch (e) { status("err", e.message); return null; }
  finally { buttons.forEach(b => b.disabled = false); }
}
$("add").onclick = () => { addRow(); save(); };
document.addEventListener("input", save);
$("test").onclick = async () => { const r = await post("/api/test", payload()); if (r) status("ok", "Test sent to " + r.sent[0] + ". Check the inbox (and spam)."); };
$("send").onclick = async () => {
  const n = people().filter(p => p.name || p.email).length;
  if (!confirm(`Draw names and email ${n} people now? This can't be undone without re-sending everyone a new match.`)) return;
  const r = await post("/api/send", payload()); if (r) report(r);
};
$("retry").onclick = async () => { const r = await post("/api/retry"); if (r) report(r); };
load();
</script></body></html>
"""


if __name__ == "__main__":
    print(f"Secret Santa running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

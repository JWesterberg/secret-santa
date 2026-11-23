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
    exclusions: partners never draw each other.
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
            if receiver in people[giver]["avoid"]:
                ok = False
                break
        if ok:
            return order
    raise UserError(
        "Couldn't find a valid draw with those partners. "
        "Add a few more people or remove a partner pairing."
    )


def render(template, giver, receiver, event):
    values = {"name": giver["name"], "match": receiver["name"], "event": event}
    return re.sub(r"\{(name|match|event)\}", lambda m: values[m.group(1)], template)


def build_message(cfg, giver, receiver):
    subject = render(cfg["subject"], giver, receiver, cfg["event"])
    directions = render(cfg["directions"], giver, receiver, cfg["event"])

    text = (
        f"Ho ho ho, {giver['name']}!\n\n"
        f"You are the Secret Santa for:\n\n    {receiver['name']}\n\n"
        f"{directions}\n\n"
        "Shh, keep it secret!\n"
    )
    dirs_html = html.escape(directions).replace("\n", "<br>")
    body_html = f"""\
<div style="background:#eaf3fc;padding:24px 12px;font-family:'Trebuchet MS',Arial,sans-serif">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:560px;margin:auto;background:#ffffff;border-radius:20px;overflow:hidden">
  <tr><td style="background:#1d3461;padding:28px 24px 0;text-align:center">
    <div style="font-size:40px;line-height:1">&#127877;</div>
    <div style="color:#ffffff;font-size:30px;font-weight:bold;margin:8px 0 2px">Secret <span style="color:#ffc93c">Santa</span></div>
    <div style="color:#cfe0f7;font-size:14px;padding-bottom:26px">{html.escape(cfg['event'] or 'A special delivery from the North Pole')}</div>
  </td></tr>
  <tr><td style="padding:24px 28px 8px;color:#1d2a3a;font-size:16px">
    <p style="margin:0 0 14px">Ho ho ho, <b>{html.escape(giver['name'])}</b>!</p>
    <p style="margin:0">You are the Secret Santa for:</p>
  </td></tr>
  <tr><td style="padding:8px 28px 20px">
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#e53935;border-radius:16px">
      <tr><td style="padding:22px;text-align:center">
        <div style="font-size:30px;line-height:1">&#127873;</div>
        <div style="color:#ffffff;font-size:32px;font-weight:bold;margin-top:8px">{html.escape(receiver['name'])}</div>
      </td></tr>
    </table>
  </td></tr>
  <tr><td style="padding:0 28px 8px;color:#1d2a3a;font-size:15px;line-height:1.6">
    <div style="background:#f3f8fd;border-radius:14px;padding:16px 18px">{dirs_html}</div>
  </td></tr>
  <tr><td style="padding:18px 28px 26px;text-align:center;color:#2e9e5b;font-size:15px;font-weight:bold">
    &#127876; Shh, keep it secret! &#127876;
  </td></tr>
</table>
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
        partner = str(row.get("partner") or "").strip().lower()
        if not name and not email:
            continue
        if not name or not EMAIL_RE.match(email):
            raise UserError(f"Check the row for '{name or email}': needs a name and valid email.")
        if email.lower() in seen:
            raise UserError(f"{email} is listed twice.")
        seen.add(email.lower())
        people.append({"name": name, "email": email, "partner": partner, "avoid": set()})
    if len(people) < 3:
        raise UserError("You need at least 3 people.")
    index = {p["email"].lower(): i for i, p in enumerate(people)}
    for i, p in enumerate(people):
        j = index.get(p["partner"])
        if j is not None and j != i:  # partners can't draw each other, either direction
            p["avoid"].add(j)
            people[j]["avoid"].add(i)
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600;700&family=Nunito:wght@400;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --night:#1d3461;--night2:#26467e;--red:#e53935;--red-d:#b71c1c;--green:#2e9e5b;--green-d:#1f7a45;
  --gold:#ffc93c;--gold-d:#e0a800;--sky:#4f8fd8;--snow:#ffffff;--ice:#eaf3fc;--line:#d7e4f2;--ink:#1d2a3a;--muted:#5f6f82;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ice);font:16px/1.55 Nunito,system-ui,sans-serif;color:var(--ink)}
h1,h2,button,.badge{font-family:Fredoka,Nunito,system-ui,sans-serif}

/* ---------- header scene ---------- */
header{position:relative;background:var(--night);overflow:hidden;height:360px}
header svg.scene{position:absolute;left:0;right:0;bottom:-2px;width:100%;height:calc(100% + 2px)}
.title{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;padding-top:56px;text-align:center;color:#fff;z-index:3;padding-left:16px;padding-right:16px}
.title h1{margin:0;font-size:clamp(40px,7vw,68px);font-weight:700;letter-spacing:.5px;line-height:1.05}
.title h1 span{color:var(--gold)}
.title p{margin:10px 0 0;max-width:520px;color:#cfe0f7;font-size:16px}
.flake{position:absolute;top:-10px;background:#fff;border-radius:50%;opacity:.85;z-index:2;animation:fall linear infinite}
@keyframes fall{to{transform:translate(var(--drift),380px)}}
.sleigh{animation:fly 22s linear infinite}
@keyframes fly{from{transform:translate(-260px,0)}to{transform:translate(1460px,-40px)}}
@media (prefers-reduced-motion:reduce){.flake,.sleigh{animation:none}.flake{display:none}}

/* ---------- layout ---------- */
main{max-width:780px;margin:-24px auto 0;padding:0 16px 40px;position:relative;z-index:4}
section{background:#fff;border-radius:24px;padding:22px 24px 24px;margin-bottom:22px;box-shadow:0 6px 0 var(--line)}
.sec-head{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.badge{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;flex:none}
.badge svg{width:24px;height:24px}
.b-red{background:var(--red)}.b-green{background:var(--green)}.b-gold{background:var(--gold)}.b-blue{background:var(--sky)}
h2{font-size:22px;font-weight:600;margin:0}
h2 small{display:block;font-family:Nunito,sans-serif;font-size:13px;font-weight:600;color:var(--muted);letter-spacing:.3px;text-transform:uppercase}

label{display:block;font-size:13px;font-weight:700;color:var(--muted);margin:12px 0 5px}
input,textarea,select{width:100%;padding:10px 14px;border:2px solid var(--line);border-radius:14px;font:inherit;color:var(--ink);background:#fbfdff;transition:border-color .15s}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--green);background:#fff}
textarea{min-height:160px;resize:vertical}
select{appearance:none;cursor:pointer;padding-right:34px;background:#fbfdff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%235f6f82' stroke-width='2' fill='none'/%3E%3C/svg%3E") no-repeat right 14px center}
.row{display:grid;grid-template-columns:1fr 1.3fr 1fr 40px;gap:10px;margin-bottom:10px}
.fld{display:block;min-width:0;margin:0;font-size:16px;font-weight:400;color:var(--ink)}.fld>span{display:none}
.row.head{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}

button{font-size:16px;font-weight:600;border:none;border-radius:999px;padding:10px 20px;cursor:pointer;background:var(--ice);color:var(--night);box-shadow:0 4px 0 var(--line);transition:transform .08s,box-shadow .08s}
button:active{transform:translateY(3px);box-shadow:0 1px 0 var(--line)}
button.primary{background:var(--red);color:#fff;font-size:19px;padding:14px 30px;box-shadow:0 5px 0 var(--red-d)}
button.primary:active{box-shadow:0 2px 0 var(--red-d)}
button.green{background:var(--green);color:#fff;box-shadow:0 4px 0 var(--green-d)}
button.gold{background:var(--gold);color:var(--night);box-shadow:0 4px 0 var(--gold-d)}
button.x{padding:0;width:40px;height:44px;border-radius:14px;font-size:22px;line-height:1;color:var(--muted);box-shadow:none;background:transparent}
button.x:hover{background:#fdecec;color:var(--red)}
button:disabled{opacity:.55;cursor:default;transform:none}

.hint{font-size:14px;color:var(--muted)}
.callout{background:#fff7dd;border-radius:14px;padding:12px 14px;font-size:14px;color:#6b5100;margin-bottom:6px}
.callout a{color:#6b5100;font-weight:700}
code{background:var(--ice);color:var(--night2);padding:1px 6px;border-radius:6px;font-size:13px}
.actions{display:flex;gap:12px;flex-wrap:wrap;align-items:center}

#status{margin-top:18px;padding:16px 18px;border-radius:18px;display:none;white-space:pre-wrap}
#status.ok{display:block;background:#e6f5ec;color:#185c35}
#status.err{display:block;background:#fdecec;color:#8c1d18}
#status .big{font-family:Fredoka,sans-serif;font-size:22px;font-weight:600;display:block;margin-bottom:4px}
.pop{animation:pop .45s cubic-bezier(.3,1.6,.5,1)}
@keyframes pop{from{transform:scale(.85);opacity:0}}

footer{text-align:center;color:var(--muted);font-size:13px;padding:0 16px 32px}
footer svg{display:block;margin:0 auto 6px}

@media(max-width:600px){
  header{height:300px}.title{padding-top:40px}
  section{padding:18px 16px 20px;border-radius:20px}
  .grid2{grid-template-columns:1fr}
  .row{grid-template-columns:1fr 40px;gap:4px 10px;padding-bottom:14px;margin-bottom:14px;border-bottom:2px dashed var(--line)}
  .row.head{display:none}
  .row .f-name,.row .f-email{grid-column:1/3}
  #people{counter-reset:person}
  .row{counter-increment:person}
  .row::before{content:"Person " counter(person);grid-column:1/3;font-family:Fredoka,Nunito,sans-serif;font-weight:600;font-size:17px;color:var(--night)}
  .fld>span{display:block;font-size:13px;font-weight:700;color:var(--muted);margin:4px 0 5px}
  .row .x{align-self:end}
}
</style></head><body>

<header>
  <svg class="scene" viewBox="0 0 1200 360" preserveAspectRatio="xMidYMax slice" aria-hidden="true">
    <!-- stars -->
    <g fill="#fff" opacity=".8">
      <circle cx="90" cy="60" r="2"/><circle cx="210" cy="130" r="1.5"/><circle cx="330" cy="40" r="2.2"/>
      <circle cx="470" cy="110" r="1.4"/><circle cx="760" cy="50" r="1.8"/><circle cx="880" cy="140" r="1.4"/>
      <circle cx="1010" cy="70" r="2"/><circle cx="1150" cy="150" r="1.6"/><circle cx="620" cy="30" r="1.5"/>
    </g>
    <!-- moon -->
    <circle cx="1040" cy="92" r="38" fill="#ffe9a8"/>
    <circle cx="1056" cy="80" r="34" fill="var(--night)"/>
    <!-- sleigh + reindeer -->
    <g class="sleigh">
      <g transform="translate(0,175) scale(.9)">
        <path d="M-6 30 Q-40 26 -60 18" stroke="#ffe9a8" stroke-width="2" stroke-dasharray="2 6" fill="none" opacity=".8"/>
        <!-- sack -->
        <path d="M8 6 Q4 -14 22 -18 Q40 -14 34 6Z" fill="#8d5a3b"/>
        <!-- santa -->
        <circle cx="46" cy="-2" r="9" fill="var(--red)"/>
        <circle cx="50" cy="-12" r="6" fill="#ffd9b8"/>
        <path d="M44 -15 L56 -15 L52 -26Z" fill="var(--red)"/>
        <circle cx="52" cy="-27" r="2.5" fill="#fff"/>
        <!-- sleigh body -->
        <path d="M0 4 H62 Q72 4 72 -6 L78 -6 Q80 22 58 22 H6 Q0 22 0 14Z" fill="var(--red)"/>
        <path d="M-2 30 H64 Q76 30 80 20" stroke="var(--gold)" stroke-width="3.5" fill="none" stroke-linecap="round"/>
        <line x1="14" y1="22" x2="14" y2="30" stroke="var(--gold)" stroke-width="3"/>
        <line x1="54" y1="22" x2="54" y2="30" stroke="var(--gold)" stroke-width="3"/>
        <!-- reins -->
        <path d="M76 -2 Q100 4 122 -2" stroke="#ffe9a8" stroke-width="1.5" fill="none"/>
        <!-- reindeer -->
        <g transform="translate(118,-6)">
          <ellipse cx="18" cy="6" rx="18" ry="8" fill="#a0673f"/>
          <rect x="4" y="10" width="4" height="14" rx="2" fill="#a0673f" transform="rotate(25 6 10)"/>
          <rect x="28" y="10" width="4" height="14" rx="2" fill="#a0673f" transform="rotate(-25 30 10)"/>
          <path d="M32 2 L40 -10" stroke="#a0673f" stroke-width="6" stroke-linecap="round"/>
          <circle cx="44" cy="-12" r="6" fill="#a0673f"/>
          <circle cx="50" cy="-11" r="3" fill="var(--red)"/>
          <path d="M40 -17 L36 -28 M37 -24 L31 -26 M44 -18 L46 -30 M45 -25 L51 -28" stroke="#ffe9a8" stroke-width="2" stroke-linecap="round" fill="none"/>
        </g>
      </g>
    </g>
    <!-- hills -->
    <path d="M0 250 Q160 190 330 235 T700 225 T1050 215 T1200 230 V360 H0Z" fill="#3c62a8"/>
    <path d="M0 290 Q200 240 420 280 T820 270 T1200 265 V360 H0Z" fill="#cfe2f8"/>
    <!-- trees back -->
    <g fill="var(--green-d)">
      <path d="M140 270 l22 -60 l22 60Z"/><path d="M180 262 l16 -44 l16 44Z"/>
      <path d="M960 262 l20 -56 l20 56Z"/><path d="M1000 268 l15 -40 l15 40Z"/>
    </g>
    <!-- cabin -->
    <g transform="translate(760,222)">
      <rect x="0" y="22" width="70" height="44" fill="#c0392b"/>
      <path d="M-8 26 L35 -6 L78 26Z" fill="#fff"/>
      <rect x="44" y="-2" width="10" height="18" fill="#8d5a3b"/>
      <rect x="10" y="34" width="16" height="14" rx="2" fill="var(--gold)"/>
      <rect x="40" y="40" width="16" height="26" rx="2" fill="#7a2a20"/>
    </g>
    <!-- front snow -->
    <path d="M0 320 Q300 285 600 318 T1200 305 V360 H0Z" fill="var(--ice)"/>
    <!-- trees front -->
    <g>
      <rect x="66" y="300" width="10" height="20" fill="#8d5a3b"/>
      <path d="M36 304 L71 220 L106 304Z" fill="var(--green)"/><path d="M46 270 L71 205 L96 270Z" fill="var(--green)"/>
      <path d="M71 205 l0 0" /><circle cx="71" cy="200" r="7" fill="var(--gold)"/>
      <rect x="1106" y="296" width="10" height="20" fill="#8d5a3b"/>
      <path d="M1076 300 L1111 222 L1146 300Z" fill="var(--green)"/><path d="M1086 266 L1111 208 L1136 266Z" fill="var(--green)"/>
      <circle cx="1111" cy="204" r="7" fill="var(--gold)"/>
      <!-- presents -->
      <rect x="1150" y="290" width="26" height="22" rx="3" fill="var(--red)"/><rect x="1160" y="290" width="6" height="22" fill="var(--gold)"/>
      <rect x="128" y="294" width="22" height="18" rx="3" fill="var(--sky)"/><rect x="136" y="294" width="6" height="18" fill="#fff"/>
    </g>
  </svg>
  <div class="title">
    <h1>Secret <span>Santa</span></h1>
    <p>Names are drawn and delivered by elves. You'll only find out that everyone got theirs, never who drew whom.</p>
  </div>
</header>

<main>
<section>
  <div class="sec-head">
    <div class="badge b-red"><svg viewBox="0 0 24 24" fill="#fff"><circle cx="9" cy="8" r="4"/><path d="M1 21c0-4.4 3.6-7 8-7s8 2.6 8 7z"/><circle cx="17.5" cy="9" r="3" opacity=".75"/><path d="M15 14.4c4.3-.6 8 1.6 8 5.6h-4.5c0-2.3-1.2-4.2-3.5-5.6z" opacity=".75"/></svg></div>
    <h2><small>Step 1</small>Who's on the nice list?</h2>
  </div>
  <div class="row head"><span>Name</span><span>Email</span><span>Partner</span><span></span></div>
  <div id="people"></div>
  <div class="actions" style="margin-top:6px"><button type="button" class="green" id="add">+ Add person</button>
  <span class="hint">Pick someone's significant other under Partner and the two won't draw each other.</span></div>
</section>

<section>
  <div class="sec-head">
    <div class="badge b-green"><svg viewBox="0 0 24 24" fill="#fff"><path d="M3 5h18a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zm1 2.2V17h16V7.2l-8 5.3z"/></svg></div>
    <h2><small>Step 2</small>Write the letter</h2>
  </div>
  <div class="grid2">
    <div><label>Subject</label><input id="subject" value="Your Secret Santa assignment for {event}"></div>
    <div><label>Event name (fills <code>{event}</code>)</label><input id="event" placeholder="Family Christmas 2026"></div>
  </div>
  <label>Directions</label>
  <textarea id="directions" placeholder="Budget: $50&#10;Exchange: Dec 24 at Grandma's&#10;Bring the gift wrapped with a tag for {match}."></textarea>
  <p class="hint">Each email opens with "Ho ho ho, {name}! You are the Secret Santa for <b>[match]</b>", followed by your directions. Placeholders: <code>{name}</code> recipient, <code>{match}</code> who they drew, <code>{event}</code>.</p>
</section>

<section>
  <div class="sec-head">
    <div class="badge b-gold"><svg viewBox="0 0 24 24" fill="var(--night)"><path d="M7 14a4 4 0 1 1 3.9-5H22v3h-2v3h-3v-3h-6.1A4 4 0 0 1 7 14zm0-2.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z"/></svg></div>
    <h2><small>Step 3</small>The elves' mailbox</h2>
  </div>
  <div class="callout">Use an account whose Sent folder you won't open, or the assignments will be sitting there. For Gmail: make a fresh account, turn on 2-step verification, and create an <a href="https://myaccount.google.com/apppasswords" target="_blank">app password</a>.</div>
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
  <div class="sec-head">
    <div class="badge b-blue"><svg viewBox="0 0 24 24" fill="#fff"><path d="M3 10h18v3H3zM4 13h16v8H4z" /><path d="M11 10h2v11h-2z" fill="var(--sky)"/><path d="M12 10c-2-4-6-5-6-2s4 2 6 2zm0 0c2-4 6-5 6-2s-4 2-6 2z"/></svg></div>
    <h2><small>Step 4</small>Deliver the presents</h2>
  </div>
  <div class="actions">
    <input id="test_to" placeholder="Your email for a test" style="max-width:280px">
    <button type="button" class="gold" id="test">Send me a test</button>
  </div>
  <p class="hint">The test uses a placeholder match, so it's safe to peek.</p>
  <div class="actions" style="margin-top:18px">
    <button type="button" class="primary" id="send">🎅 Draw names &amp; send</button>
    <button type="button" id="retry" style="display:none">Retry failed emails</button>
  </div>
  <div id="status"></div>
</section>
</main>

<footer>
  <svg width="40" height="40" viewBox="0 0 40 40" aria-hidden="true"><g stroke="#9fb8d6" stroke-width="2.4" stroke-linecap="round"><path d="M20 4v32M6 12l28 16M6 28l28-16"/><path d="M16 7l4 4 4-4M16 33l4-4 4 4"/></g></svg>
  Runs on your computer. Assignments are never saved or shown.
</footer>

<script>
const $ = id => document.getElementById(id);
const SAVE_KEYS = ["subject","event","directions","smtp_host","smtp_port","smtp_user","from_name","from_addr","test_to"];

(function snow(){
  const h = document.querySelector("header");
  for (let i = 0; i < 45; i++) {
    const f = document.createElement("span"), s = 2 + Math.random() * 4;
    f.className = "flake";
    f.style.cssText = `left:${Math.random()*100}%;width:${s}px;height:${s}px;animation-duration:${7+Math.random()*9}s;animation-delay:${-Math.random()*16}s;--drift:${(Math.random()*60-30)}px`;
    h.appendChild(f);
  }
})();

let loading = false;
const rows = () => [...document.querySelectorAll("#people .row")];
const rowById = id => rows().find(r => r.dataset.id === id);
function addRow(p = {}) {
  const r = document.createElement("div");
  r.className = "row";
  r.dataset.id = p.id || Math.random().toString(36).slice(2, 10);
  r.dataset.partner = p.partner || "";
  r.innerHTML = '<label class="fld f-name"><span>Name</span><input class="name" placeholder="Their name"></label>' +
    '<label class="fld f-email"><span>Email</span><input class="email" type="email" placeholder="name@example.com"></label>' +
    '<label class="fld f-partner"><span>Partner (won\'t draw each other)</span><select class="partner"></select></label>' +
    '<button type="button" class="x" title="Remove person" aria-label="Remove person">&times;</button>';
  r.querySelector(".name").value = p.name || "";
  r.querySelector(".email").value = p.email || "";
  r.querySelector(".name").addEventListener("input", refreshPartners);
  r.querySelector(".partner").onchange = e => { setPartner(r, e.target.value); save(); };
  r.querySelector(".x").onclick = () => { setPartner(r, ""); r.remove(); refreshPartners(); save(); };
  $("people").appendChild(r);
  refreshPartners();
}
// Partners are mutual: linking A to B unlinks whoever either of them was paired with before.
function setPartner(r, id) {
  const unlink = x => { const old = x && rowById(x.dataset.partner); if (old) old.dataset.partner = ""; if (x) x.dataset.partner = ""; };
  unlink(r);
  const other = id && rowById(id);
  if (other) { unlink(other); r.dataset.partner = other.dataset.id; other.dataset.partner = r.dataset.id; }
  refreshPartners();
}
function refreshPartners() {
  if (loading) return;
  const all = rows();
  all.forEach(r => {
    const sel = r.querySelector(".partner");
    if (r.dataset.partner && !rowById(r.dataset.partner)) r.dataset.partner = "";
    sel.innerHTML = "";
    sel.add(new Option("No partner", ""));
    all.forEach(o => {
      if (o === r) return;
      const label = o.querySelector(".name").value.trim();
      if (label || o.dataset.id === r.dataset.partner) sel.add(new Option(label || "(unnamed)", o.dataset.id));
    });
    sel.value = r.dataset.partner;
  });
}
function people() {
  return rows().map(r => {
    const partner = rowById(r.dataset.partner);
    return { id: r.dataset.id, partnerId: r.dataset.partner, name: r.querySelector(".name").value, email: r.querySelector(".email").value,
             partner: partner ? partner.querySelector(".email").value : "" };
  });
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
  if (d) { SAVE_KEYS.forEach(k => { if (d[k] != null) $(k).value = d[k]; }); loading = true; (d.people || []).forEach(p => addRow({ ...p, partner: p.partnerId })); loading = false; refreshPartners(); }
  if (!document.querySelector("#people .row")) for (let i = 0; i < 3; i++) addRow();
}
function status(kind, title, text) {
  const s = $("status");
  s.className = kind; s.innerHTML = "";
  if (title) { const b = document.createElement("span"); b.className = "big"; b.textContent = title; s.appendChild(b); }
  s.appendChild(document.createTextNode(text || ""));
  s.classList.remove("pop"); void s.offsetWidth; s.classList.add("pop");
}
function report(r) {
  let t = r.sent.map(e => "✓ " + e).join("\n");
  if (r.failed.length) {
    t += `\n\nNot delivered (${r.failed.length}):\n` + r.failed.map(e => "✗ " + e).join("\n") +
      "\n\nThese are held in memory. Click Retry. Don't redraw, or people who already got emails will have stale matches.";
    status("err", `${r.sent.length} delivered, ${r.failed.length} stuck in the chimney`, t);
  } else {
    status("ok", `🎄 All ${r.sent.length} assignments delivered!`, t);
  }
  $("retry").style.display = r.failed.length ? "" : "none";
}
async function post(url, body) {
  const buttons = document.querySelectorAll("button"); buttons.forEach(b => b.disabled = true);
  status("ok", "The elves are on it…", "");
  try {
    const res = await fetch(url, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body || {}) });
    const j = await res.json();
    if (!res.ok) throw new Error(j.error || "Request failed");
    return j;
  } catch (e) { status("err", "Uh oh, coal.", e.message); return null; }
  finally { buttons.forEach(b => b.disabled = false); }
}
$("add").onclick = () => { addRow(); save(); };
document.addEventListener("input", save);
$("test").onclick = async () => { const r = await post("/api/test", payload()); if (r) status("ok", "Test sent!", "Check " + r.sent[0] + " (and the spam folder)."); };
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
    import webbrowser
    url = f"http://localhost:{PORT}"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"\n  Secret Santa is running at {url}")
    print("  Keep this window open while you use it. Close it when you're done.\n")
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

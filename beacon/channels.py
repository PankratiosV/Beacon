"""Extra notification channels: Discord, Pushover, email and SMS (Twilio),
plus photo uploads for Telegram and Discord. Standard library only; every
function raises RuntimeError with a readable reason when something fails."""

import base64
import json
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage


def _post(url, data=None, headers=None, timeout=12):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError("Can't reach the service. Check the internet connection.") from e


def multipart(fields, files):
    """(body, content_type) for a multipart/form-data POST.
    files: [(field, filename, bytes, mime)]"""
    boundary = uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode("utf-8")
    for field, filename, data, mime in files:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n").encode("utf-8")
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- Telegram
def telegram_photo(token, chat, caption, jpeg):
    body, ctype = multipart({"chat_id": chat, "caption": caption[:1000]},
                            [("photo", "snapshot.jpg", jpeg, "image/jpeg")])
    code, text = _post(f"https://api.telegram.org/bot{token}/sendPhoto", body,
                       {"Content-Type": ctype}, timeout=30)
    try:
        reply = json.loads(text)
    except ValueError:
        reply = {}
    if code != 200 or not reply.get("ok"):
        raise RuntimeError(reply.get("description") or f"Telegram error {code}")


# ----------------------------------------------------------------- Discord
def discord_send(webhook, title, body, jpeg=None):
    if not webhook.startswith("https://"):
        raise RuntimeError("Paste the full Discord webhook URL (starts with https://).")
    payload = {"content": f"**{title}**\n{body}"[:1990], "username": "Beacon"}
    if jpeg:
        data, ctype = multipart({"payload_json": json.dumps(payload)},
                                [("files[0]", "snapshot.jpg", jpeg, "image/jpeg")])
    else:
        data, ctype = json.dumps(payload).encode("utf-8"), "application/json"
    code, text = _post(webhook, data, {"Content-Type": ctype, "User-Agent": "Beacon"}, timeout=30)
    if code not in (200, 204):
        if code in (401, 404):
            raise RuntimeError("Discord doesn't recognise that webhook. Copy it again from "
                               "the channel's Integrations settings.")
        raise RuntimeError(f"Discord error {code}")


# ---------------------------------------------------------------- Pushover
def pushover_send(app_token, user_key, title, body, priority=0):
    data = urllib.parse.urlencode({"token": app_token, "user": user_key, "title": title[:250],
                                   "message": body[:1024] or title,
                                   "priority": priority}).encode("utf-8")
    code, text = _post("https://api.pushover.net/1/messages.json", data)
    try:
        reply = json.loads(text)
    except ValueError:
        reply = {}
    if code != 200 or reply.get("status") != 1:
        errors = "; ".join(reply.get("errors") or []) or f"Pushover error {code}"
        raise RuntimeError(errors)


# ------------------------------------------------------------------- Email
def email_send(host, port, user, password, to, subject, body, jpeg=None, timeout=20):
    if not (host and to):
        raise RuntimeError("Fill in the mail server and the address to send to.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user or to
    msg["To"] = to
    msg.set_content(body)
    if jpeg:
        msg.add_attachment(jpeg, maintype="image", subtype="jpeg", filename="snapshot.jpg")
    port = int(port or 587)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(),
                                  timeout=timeout) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as s:
                s.ehlo()
                if s.has_extn("starttls"):
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if user:
                    s.login(user, password)
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError("The mail server rejected the username or password. For Gmail, "
                           "use an App Password.") from e
    except (smtplib.SMTPException, OSError) as e:
        raise RuntimeError(f"Couldn't send the email: {e}") from e


# --------------------------------------------------------------- SMS Twilio
def twilio_send(sid, token, sender, to, body):
    if not (sid and token and sender and to):
        raise RuntimeError("Fill in the Twilio Account SID, Auth Token, From and To numbers.")
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    data = urllib.parse.urlencode({"From": sender, "To": to, "Body": body[:1500]}).encode()
    code, text = _post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data,
                       {"Authorization": f"Basic {auth}"})
    if code not in (200, 201):
        try:
            reason = json.loads(text).get("message")
        except ValueError:
            reason = None
        raise RuntimeError(reason or f"Twilio error {code}")

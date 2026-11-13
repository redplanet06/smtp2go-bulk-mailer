#!/usr/bin/env python3
"""
SMTP Bulk Mailer — DCB Technology, LLC (c) 2026

A local dispatch console for templated client email, sent through SMTP2GO.

Run it:      py smtp2go_sender.py     (Windows)
             python3 smtp2go_sender.py
Then open:   http://127.0.0.1:8765  (opens automatically)

Everything runs on your own machine. The API key stays server-side in this
script's memory (or in smtp2go_config.json next to it) and is never sent to the
browser page. Standard library only — nothing to install.
"""

import http.server
import json
import os
import secrets
import socketserver
import ssl
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser

API_BASE = "https://api.smtp2go.com/v3"
HOST = "127.0.0.1"
PORT = int(os.environ.get("SMTP2GO_PORT", "8765"))
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smtp2go_config.json")
SESSION_TOKEN = secrets.token_urlsafe(24)
FILE_LOCK = threading.Lock()  # request handlers run on threads; keep file writes serial

# Standing defaults. Anything saved to smtp2go_config.json overrides these on
# launch; the sender fields fall back here if the file is missing or cleared.
DEFAULTS = {
    "sender_name": "Support Team",
    "sender_email": "support@example.com",
    "reply_to": "support@example.com",
    "template_id": "",
    "subject": "",
    "mode": "each",
    "bcc_to": "",
    "batch_size": "50",
    "pace": "500",
    "test_addr": "",
    "dry_run": False,
    "skip_disabled": True,
    "theme": "dark",
    "keys": [],
    "active_key_id": "",
    "active_tab": "list",
    "recipients_list": "",
    "recipients_csv": "",
    "global_vars": [],
}

CONFIG = dict(DEFAULTS)
CONFIG["api_key"] = os.environ.get("SMTP2GO_API_KEY", "")

SENT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smtp2go_sent_ids.json")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def coerce(key, value):
    """Keep stored types stable so a reload restores the same UI."""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return bool(value)
    if key == "keys":
        out = []
        for item in (value or []):
            if not isinstance(item, dict):
                continue
            secret = str(item.get("key") or "").strip()
            if not secret:
                continue
            out.append({
                "id": str(item.get("id") or secrets.token_hex(4)),
                "label": (str(item.get("label") or "").strip() or "Unlabelled key")[:60],
                "key": secret,
            })
        return out[:20]
    if key == "theme":
        return "light" if str(value).strip().lower() == "light" else "dark"
    if key == "mode":
        return "bcc" if str(value).strip().lower() == "bcc" else "each"
    if key == "batch_size":
        try:
            return str(max(1, min(int(str(value).strip() or 50), 100)))
        except (TypeError, ValueError):
            return "50"
    if isinstance(default, list):
        rows = []
        for item in (value or []):
            if isinstance(item, dict) and str(item.get("name", "")).strip():
                rows.append({"name": str(item["name"]).strip(), "value": str(item.get("value", ""))})
        return rows
    return "" if value is None else str(value)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sync_active_key()
        return
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError) as exc:
        print("Could not read %s (%s) - starting with empty settings." % (CONFIG_PATH, exc))
        sync_active_key()
        return
    for key in DEFAULTS:
        # Accept empty values too — a cleared field is a real preference.
        # Coerce on the way in: the file is user-editable and may be malformed.
        if key in saved:
            try:
                CONFIG[key] = coerce(key, saved[key])
            except Exception:
                CONFIG[key] = DEFAULTS[key]
    # Older config files held one bare api_key. Fold it into the labelled list.
    legacy = str(saved.get("api_key") or "").strip()
    if legacy and not any(k["key"] == legacy for k in CONFIG["keys"]):
        CONFIG["keys"].insert(0, {"id": secrets.token_hex(4), "label": "Saved key", "key": legacy})
    sync_active_key()
    # Env var always wins, so you can keep the key out of the file entirely.
    if os.environ.get("SMTP2GO_API_KEY"):
        CONFIG["api_key"] = os.environ["SMTP2GO_API_KEY"]


def write_json_atomic(path, payload):
    """Write via a temp file and rename, so an interrupted save can never leave
    a half-written config (which would lose the saved client list)."""
    folder = os.path.dirname(path) or "."
    handle, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def key_entries():
    """Every key available right now. The environment variable, if set, is
    offered as a read-only entry that is never written to disk."""
    entries = []
    env = os.environ.get("SMTP2GO_API_KEY", "")
    if env:
        entries.append({"id": "env", "label": "Environment variable", "key": env, "from_env": True})
    for row in CONFIG["keys"]:
        entries.append({"id": row["id"], "label": row["label"], "key": row["key"], "from_env": False})
    return entries


def active_entry():
    entries = key_entries()
    for entry in entries:
        if entry["id"] == CONFIG.get("active_key_id"):
            return entry
    return entries[0] if entries else None


def sync_active_key():
    """CONFIG['api_key'] is what call_api uses — keep it pointed at the selection."""
    entry = active_entry()
    CONFIG["api_key"] = entry["key"] if entry else ""
    CONFIG["active_key_id"] = entry["id"] if entry else ""


def load_raw_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def save_config():
    """Merge into whatever is already on disk. A settings-only save must never
    wipe a key the user previously chose to store."""
    payload = dict(load_raw_config())
    payload.pop("api_key", None)  # migrated into the labelled key list; never keep a second copy
    for key in DEFAULTS:
        payload[key] = CONFIG[key]
    write_json_atomic(CONFIG_PATH, payload)
    try:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)  # owner read/write only
    except OSError:
        pass


def extract_address(value):
    """Pull bare@address out of 'Name <bare@address>' or return it as-is."""
    text = str(value or "").strip()
    if "<" in text and ">" in text:
        text = text[text.index("<") + 1:text.index(">")]
    return text.strip().lower()


def load_sent_ids():
    """Every email_id this tool has produced. The activity API has no
    'which key sent this' filter, so we keep our own record and look up
    only these IDs — that is what makes the delivery log key-scoped."""
    if not os.path.exists(SENT_LOG_PATH):
        return []
    try:
        with open(SENT_LOG_PATH, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def record_sent_id(email_id, recipients=None):
    if not email_id:
        return
    with FILE_LOCK:
        _record_sent_id(email_id, recipients or [])


def _record_sent_id(email_id, recipients):
    rows = load_sent_ids()
    if any(r.get("id") == email_id for r in rows):
        return
    addrs = []
    for entry in recipients[:120]:
        addr = extract_address(entry)
        if addr:
            addrs.append(addr)
    rows.append({"id": email_id, "at": time.time(), "to": addrs,
                 "key": CONFIG.get("active_key_id", "")})
    # Activity records age out at the provider anyway; keep this file small.
    cutoff = time.time() - 45 * 86400
    rows = [r for r in rows if r.get("at", 0) > cutoff][-2000:]
    try:
        write_json_atomic(SENT_LOG_PATH, rows)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# SMTP2GO calls
# --------------------------------------------------------------------------- #

def call_api(path, body):
    """POST to the SMTP2GO API. Returns (status_code, parsed_json_or_text)."""
    if not CONFIG["api_key"]:
        return 0, {"error": "No API key set yet."}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + path,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Smtp2go-Api-Key": CONFIG["api_key"],
            "User-Agent": "smtp2go-sender/1.0",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw[:500] or exc.reason}
    except urllib.error.URLError as exc:
        return 0, {"error": "Could not reach api.smtp2go.com: %s" % exc.reason}
    except Exception as exc:  # noqa: BLE001 - surface anything odd in the UI
        return 0, {"error": str(exc)}


def readable_error(status, payload):
    if isinstance(payload, dict):
        for key in ("error", "error_message", "message"):
            if payload.get(key):
                return str(payload[key])
        errors = payload.get("data", {}).get("field_validation_errors") if isinstance(payload.get("data"), dict) else None
        if errors:
            if isinstance(errors, dict):
                return "%s: %s" % (errors.get("field", "field"), errors.get("message", "invalid"))
            return str(errors)
        failures = payload.get("data", {}).get("failures") if isinstance(payload.get("data"), dict) else None
        if failures:
            return "; ".join(str(f) for f in failures)
    return "HTTP %s" % status


def normalise_templates(payload):
    """The search endpoint's key names vary a little; pull out what we need."""
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = []
    if isinstance(data, dict):
        for key in ("templates", "results", "email_templates"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    elif isinstance(data, list):
        rows = data
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        send_id = row.get("template_id") or row.get("templateId") or row.get("id")
        view_id = row.get("id") or row.get("template_id") or row.get("templateId")
        if not send_id:
            continue
        body_html = ""
        for key in ("html_body", "body_html", "html", "template_body", "body"):
            if row.get(key):
                body_html = str(row[key])
                break
        out.append({
            "id": str(send_id),
            "view_id": str(view_id),
            "name": str(row.get("template_name") or row.get("name") or send_id),
            "subject": str(row.get("subject") or ""),
            "has_body": bool(body_html),
        })
    out.sort(key=lambda t: t["name"].lower())
    return out


def build_send_payload(body):
    to_email = (body.get("to_email") or "").strip()
    to_name = (body.get("to_name") or "").strip()
    recipient = "%s <%s>" % (to_name, to_email) if to_name else to_email

    from_email = (body.get("sender_email") or "").strip()
    from_name = (body.get("sender_name") or "").strip()
    sender = "%s <%s>" % (from_name, from_email) if from_name else from_email

    payload = {
        "sender": sender,
        "to": [recipient],
        "template_id": (body.get("template_id") or "").strip(),
        "template_data": body.get("template_data") or {},
    }
    bcc = [str(a).strip() for a in (body.get("bcc") or []) if str(a).strip()]
    if bcc:
        payload["bcc"] = bcc
    subject = (body.get("subject") or "").strip()
    if subject:
        payload["subject"] = subject
    reply_to = (body.get("reply_to") or "").strip()
    if reply_to:
        payload["custom_headers"] = [{"header": "Reply-To", "value": reply_to}]
    return payload


# --------------------------------------------------------------------------- #
# HTTP handlers
# --------------------------------------------------------------------------- #

def handle_state(_body):
    entry = active_entry()
    settings = {k: CONFIG[k] for k in DEFAULTS}
    settings.pop("keys", None)  # secrets never leave the server
    return {
        "ok": True,
        "has_key": bool(CONFIG["api_key"]),
        "key_hint": CONFIG["api_key"][-4:] if CONFIG["api_key"] else "",
        # Report the resolved entry, not the stored id — they differ whenever the
        # selection falls back (env-var-only, or after the active key is removed).
        "active_key_id": entry["id"] if entry else "",
        "active_key_label": entry["label"] if entry else "",
        "keys": [{"id": e["id"], "label": e["label"], "hint": e["key"][-4:],
                  "from_env": e.get("from_env", False)} for e in key_entries()],
        "settings": settings,
        "config_path": CONFIG_PATH,
        "saved_file_exists": os.path.exists(CONFIG_PATH),
    }


def handle_settings(body):
    incoming = body.get("settings")
    if isinstance(incoming, dict):
        for key in DEFAULTS:
            if key == "keys":
                continue  # managed only through /api/keys
            if key in incoming:
                CONFIG[key] = coerce(key, incoming[key])
        sync_active_key()

    if body.get("remember"):
        try:
            with FILE_LOCK:
                save_config()
        except OSError as exc:
            return {"ok": False, "error": "Could not write %s: %s" % (CONFIG_PATH, exc)}
    return handle_state(body)


def handle_keys(body):
    with FILE_LOCK:
        return _handle_keys(body)


def _handle_keys(body):
    action = str(body.get("action") or "").strip()
    key_id = str(body.get("id") or "").strip()
    label = str(body.get("label") or "").strip()[:60]

    if action == "add":
        secret = str(body.get("key") or "").strip()
        if not secret:
            return {"ok": False, "error": "Paste the API key."}
        if any(k["key"] == secret for k in CONFIG["keys"]):
            return {"ok": False, "error": "That key is already saved."}
        if len(CONFIG["keys"]) >= 20:
            return {"ok": False, "error": "Twenty saved keys is the limit."}
        new_id = secrets.token_hex(4)
        CONFIG["keys"].append({"id": new_id, "label": label or "Unlabelled key", "key": secret})
        CONFIG["active_key_id"] = new_id

    elif action == "remove":
        if key_id == "env":
            return {"ok": False, "error": "That key comes from the environment variable, not this file."}
        CONFIG["keys"] = [k for k in CONFIG["keys"] if k["id"] != key_id]
        if CONFIG.get("active_key_id") == key_id:
            CONFIG["active_key_id"] = ""

    elif action == "rename":
        if not label:
            return {"ok": False, "error": "Give it a label."}
        for row in CONFIG["keys"]:
            if row["id"] == key_id:
                row["label"] = label

    elif action == "select":
        if not any(e["id"] == key_id for e in key_entries()):
            return {"ok": False, "error": "That key is no longer available."}
        CONFIG["active_key_id"] = key_id

    else:
        return {"ok": False, "error": "Unknown key action."}

    sync_active_key()
    try:
        save_config()
    except OSError as exc:
        return {"ok": False, "error": "Could not write %s: %s" % (CONFIG_PATH, exc)}
    return handle_state(body)


def handle_reset(body):
    """Wipe saved preferences. Keeps the key unless asked to drop that too."""
    saved_keys = [] if body.get("forget_keys") else list(CONFIG["keys"])
    active = CONFIG.get("active_key_id", "")
    for key in DEFAULTS:
        CONFIG[key] = DEFAULTS[key] if not isinstance(DEFAULTS[key], list) else []
    CONFIG["keys"] = saved_keys
    CONFIG["active_key_id"] = active if any(k["id"] == active for k in saved_keys) else ""
    sync_active_key()
    try:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
        if CONFIG["keys"]:
            save_config()
    except OSError as exc:
        return {"ok": False, "error": "Could not clear %s: %s" % (CONFIG_PATH, exc)}
    return handle_state(body)


def handle_templates(_body):
    status, payload = call_api("/template/search", {"page_size": 100})
    if status != 200:
        return {"ok": False, "error": readable_error(status, payload)}
    return {"ok": True, "templates": normalise_templates(payload)}


def extract_template_html(node):
    """Dig the HTML body out of whatever shape the response uses."""
    if isinstance(node, list):
        for item in node:
            found = extract_template_html(item)
            if found and found[0]:
                return found
        return ("", "", "")
    if not isinstance(node, dict):
        return ("", "", "")
    for key in ("html_body", "body_html", "html", "template_body", "body"):
        if node.get(key):
            name = str(node.get("template_name") or node.get("name") or "")
            return (str(node[key]), str(node.get("subject") or ""), name)
    for key in ("templates", "template", "data"):
        if key in node:
            found = extract_template_html(node[key])
            if found and found[0]:
                return found
    return ("", "", "")


def handle_template_view(body):
    """Fetch a template's HTML.

    /template/view names its parameter 'id', not 'template_id' — and that id is
    not always the same string /email/send wants. Try what the caller knows,
    then fall back to the search endpoint, which returns the whole list.
    """
    send_id = (body.get("template_id") or "").strip()
    view_id = (body.get("view_id") or "").strip() or send_id
    if not send_id and not view_id:
        return {"ok": False, "error": "Pick a template first."}

    attempts = []
    for value in [v for v in (view_id, send_id) if v]:
        attempts.append({"id": value})
        attempts.append({"template_id": value})

    last_error = ""
    seen = set()
    for params in attempts:
        key = tuple(sorted(params.items()))
        if key in seen:
            continue
        seen.add(key)
        status, payload = call_api("/template/view", params)
        if status == 403:
            return {"ok": False, "error": "This API key is not allowed to call /template/view. "
                                          "Enable it under Sending > API Keys > Templates."}
        if status == 200:
            html, subject, name = extract_template_html(payload)
            if html:
                return {"ok": True, "html": html, "subject": subject, "name": name,
                        "used_id": list(params.values())[0], "via": list(params)[0]}
        else:
            last_error = readable_error(status, payload)

    # Last resort: the search endpoint returns every template, sometimes with
    # the body included. Match on either identifier.
    status, payload = call_api("/template/search", {"page_size": 100})
    if status == 200:
        data = payload.get("data") if isinstance(payload, dict) else {}
        rows = []
        if isinstance(data, dict):
            for key in ("templates", "results", "email_templates"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
        for row in rows:
            if not isinstance(row, dict):
                continue
            ids = {str(row.get(k)) for k in ("id", "template_id", "templateId") if row.get(k)}
            if ids & {send_id, view_id}:
                html, subject, name = extract_template_html(row)
                if html:
                    return {"ok": True, "html": html, "subject": subject,
                            "name": name or str(row.get("template_name") or ""),
                            "used_id": str(row.get("id") or row.get("template_id") or ""),
                            "via": "search"}

    return {"ok": False, "error": (last_error or "Could not read that template's HTML.")
            + " Tried both 'id' and 'template_id'. If you typed the ID by hand, load the"
            + " template list and pick it from the dropdown instead."}


def handle_send(body):
    payload = build_send_payload(body)
    if not payload["template_id"]:
        return {"ok": False, "error": "Pick a template first."}
    if not payload["sender"]:
        return {"ok": False, "error": "Set the From address."}
    if not payload["to"][0]:
        return {"ok": False, "error": "Missing recipient address."}
    if len(payload.get("bcc", [])) > 100:
        return {"ok": False, "error": "SMTP2GO caps BCC at 100 per email. Lower the batch size."}

    if body.get("dry_run"):
        return {"ok": True, "dry_run": True, "payload": payload}

    status, resp = call_api("/email/send", payload)
    if status != 200:
        return {"ok": False, "status": status, "error": readable_error(status, resp)}

    data = resp.get("data", {}) if isinstance(resp, dict) else {}
    succeeded = data.get("succeeded") or 0
    failed = data.get("failed") or 0
    failures = data.get("failures") or []

    # Nothing got through — safe to retry the whole thing.
    if failed and not succeeded:
        return {"ok": False, "status": status, "error": readable_error(status, resp)}

    record_sent_id(data.get("email_id", ""), payload.get("to", []) + payload.get("bcc", []))
    result = {
        "ok": True,
        "email_id": data.get("email_id", ""),
        "request_id": resp.get("request_id", "") if isinstance(resp, dict) else "",
        "succeeded": succeeded,
    }
    # Some of the batch went out and some did not. Naming the rejected addresses
    # lets the page mark only those rows, so a retry can't double-send to the
    # recipients who already received it.
    if failed:
        rejected = []
        for item in failures:
            if isinstance(item, dict):
                rejected.append(str(item.get("email") or item.get("recipient") or item))
            else:
                rejected.append(str(item))
        result["partial"] = True
        result["failed"] = failed
        result["rejected"] = rejected
    return result


EVENT_ID_KEYS = ("email_id", "emailId", "id", "message_id", "messageId")


def event_identifiers(event):
    """Every string in the event that could be the message ID. The API's field
    naming isn't guaranteed, so check the known keys and fall back to scanning
    short string values rather than trusting one spelling."""
    found = set()
    for key in EVENT_ID_KEYS:
        if event.get(key):
            found.add(str(event[key]).strip())
    for value in event.values():
        if isinstance(value, str) and 6 <= len(value) <= 40:
            found.add(value.strip())
    return found


def handle_activity(body):
    """Delivery status for mail THIS tool sent with THIS key, and nothing else.

    /activity/search has no parameter for filtering by message ID — passing one
    is silently ignored and the account's whole recent stream comes back. So the
    date range only narrows the request; the actual filtering happens here,
    against the ledger of IDs and recipients we recorded at send time. Anything
    that doesn't match something we sent is dropped, so mail from the SMTP2GO
    app, SMTP relay, monitoring alerts or another key can never appear.
    """
    active_key = CONFIG.get("active_key_id", "")
    ledger = load_sent_ids()
    # Entries written before keys were labelled carry no key — keep those, but
    # anything tagged with a different key belongs to a different account view.
    ledger = [r for r in ledger if r.get("key", active_key) == active_key]
    if body.get("scope") == "all":
        wanted = ledger
    else:
        session = set(str(i) for i in (body.get("email_ids") or []) if i)
        wanted = [r for r in ledger if str(r.get("id")) in session]

    if not wanted:
        return {"ok": True, "events": [], "note": "nothing sent through this key yet"}

    id_set = set(str(r.get("id")) for r in wanted if r.get("id"))
    recipient_set = set()
    for row in wanted:
        for addr in (row.get("to") or []):
            recipient_set.add(str(addr).lower())

    earliest = min([r.get("at", time.time()) for r in wanted] or [time.time()])
    # Generous back-off: the account's clock/timezone may not match ours, and
    # precision comes from the ID match below, not from this boundary.
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(earliest - 26 * 3600))

    # No search_sender: it filters server-side on the CURRENT From address, so
    # anything sent earlier under a different sender silently vanishes. We filter
    # locally against the ledger anyway, so let everything through and cut it here.
    params = {"start_date": start, "only_latest": True, "limit": 250}

    rows, seen = [], set()
    examined = 0
    token = None
    used_recipient_fallback = False
    dropped_sample = []
    for _ in range(12):  # bounded pagination; 60 requests/minute allowed
        if token:
            params["continue_token"] = token
        status, payload = call_api("/activity/search", params)
        if status == 403:
            return {"ok": False, "error": "This API key is not allowed to call /activity/search. "
                                          "Enable it under Sending > API Keys > Activity."}
        if status != 200:
            if rows:
                break
            return {"ok": False, "error": readable_error(status, payload)}

        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            break
        events = data.get("events") or data.get("emails") or []

        # Recipient matching is a last resort only. Your From address receives
        # mail from other systems too (monitoring alerts, the app's own test
        # sends), so matching on it would let those through. If the events carry
        # IDs — they normally do — match on those alone.
        ids_present = any(
            isinstance(ev, dict) and any(ev.get(k) for k in EVENT_ID_KEYS)
            for ev in events
        )
        if not ids_present:
            used_recipient_fallback = True

        for ev in events:
            if not isinstance(ev, dict):
                continue
            examined += 1
            recipient = str(ev.get("recipient") or "")
            matched = bool(event_identifiers(ev) & id_set)
            if not matched and not ids_present and recipient and recipient.lower() in recipient_set:
                matched = True
            if not matched:
                if len(dropped_sample) < 12:
                    dropped_sample.append({
                        "recipient": recipient,
                        "subject": str(ev.get("subject") or "")[:70],
                        "email_id": str(ev.get("email_id") or ""),
                    })
                continue  # somebody else's mail — drop it
            row = {
                "recipient": recipient,
                "subject": str(ev.get("subject") or ""),
                "status": str(ev.get("status") or ev.get("event") or ""),
                "when": str(ev.get("delivered_at") or ev.get("email_ts") or ev.get("date") or ""),
                "detail": str(ev.get("response") or ev.get("smtpcode") or ""),
                "email_id": str(ev.get("email_id") or ""),
            }
            key = (row["email_id"], row["recipient"], row["status"], row["when"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

        token = data.get("continue_token")
        if not token:
            break

    rows.sort(key=lambda r: r["when"], reverse=True)

    # Which sends produced no delivery record at all? Reporting these is the
    # difference between "nothing to show" and silently dropping a send.
    covered = set()
    for row in rows:
        if row["email_id"]:
            covered.add(row["email_id"])
        if row["recipient"]:
            covered.add(row["recipient"].lower())
    unrecorded = []
    for entry in wanted:
        eid = str(entry.get("id") or "")
        addrs = [str(a).lower() for a in (entry.get("to") or [])]
        if eid in covered or any(a in covered for a in addrs):
            continue
        unrecorded.append({"id": eid, "to": addrs[:5], "at": entry.get("at", 0)})

    return {"ok": True, "events": rows, "examined": examined,
            "filtered_out": examined - len(rows), "sends_tracked": len(wanted),
            "loose_match": used_recipient_fallback,
            "unrecorded": unrecorded,
            "dropped_sample": dropped_sample[:12]}


ROUTES = {
    "/api/state": handle_state,
    "/api/settings": handle_settings,
    "/api/templates": handle_templates,
    "/api/send": handle_send,
    "/api/activity": handle_activity,
    "/api/reset": handle_reset,
    "/api/template": handle_template_view,
    "/api/keys": handle_keys,
}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "smtp2go-sender"

    def log_message(self, fmt, *args):  # quieter console
        first = str(args[0]) if args else ""
        if "/api/send" in first:
            sys.stdout.write("  %s\n" % first)

    def _reject_remote(self):
        """Only serve requests that really came from this machine."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            self._send_json(403, {"ok": False, "error": "Local access only."})
            return True
        return False

    def _send_json(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self._reject_remote():
            return
        if self.path in ("/", "/index.html"):
            page = PAGE.replace("__TOKEN__", SESSION_TOKEN).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self._reject_remote():
            return
        if self.headers.get("X-Token") != SESSION_TOKEN:
            self._send_json(403, {"ok": False, "error": "Stale page — reload http://%s:%s" % (HOST, PORT)})
            return
        handler = ROUTES.get(self.path)
        if handler is None:
            self._send_json(404, {"ok": False, "error": "Unknown endpoint."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send_json(400, {"ok": False, "error": "Malformed request."})
            return
        try:
            result = handler(body)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        self._send_json(200, result)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>SMTP Bulk Mailer — DCB Technology, LLC</title>
<style>
  /* Dark by default. Every colour is a variable so the light override below
     only has to restate the palette, not the rules. */
  :root {
    color-scheme: dark;
    --paper: #0e141a;
    --surface: #161d26;
    --surface-2: #1c2530;
    --ink: #e3eaf1;
    --ink-soft: #93a3b3;
    --line: #2b3641;
    --accent: #5aa9cd;
    --accent-strong: #7cc2e2;
    --accent-soft: #14303d;
    --accent-border: #2d5f78;
    --accent-ink: #a9d6ec;
    --amber: #d8a04a;
    --amber-soft: #322616;
    --amber-border: #6b5222;
    --ok: #58c08c;
    --ok-soft: #142f23;
    --ok-border: #2c5f45;
    --bad: #e2796f;
    --bad-soft: #341c1a;
    --bad-border: #6d3733;
    --row-bad: #241618;
    --header-bg: #080c11;
    --header-fg: #eef3f8;
    --input-bg: #10171e;
    --code-bg: #0a0f14;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  [data-theme="light"] {
    color-scheme: light;
    --paper: #eceff3;
    --surface: #ffffff;
    --surface-2: #f6f8fa;
    --ink: #16202b;
    --ink-soft: #56646f;
    --line: #d2d9e0;
    --accent: #205d78;
    --accent-strong: #17495e;
    --accent-soft: #e3eef3;
    --accent-border: #a9cbd8;
    --accent-ink: #14424f;
    --amber: #a2660f;
    --amber-soft: #fdf5e8;
    --amber-border: #e6cfa8;
    --ok: #1c7048;
    --ok-soft: #ecf6f1;
    --ok-border: #bfdccf;
    --bad: #9d2f2b;
    --bad-soft: #fbeeed;
    --bad-border: #e6c3c0;
    --row-bad: #fdf6f5;
    --header-bg: #16202b;
    --header-fg: #ffffff;
    --input-bg: #ffffff;
    --code-bg: #16202b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--paper); color: var(--ink);
    font-family: var(--sans); font-size: 14px; line-height: 1.5;
  }
  header {
    background: var(--header-bg); color: var(--header-fg);
    padding: 16px 22px 14px; text-align: center; position: relative;
  }
  header h1 { font-size: 17px; margin: 0; font-weight: 600; letter-spacing: .01em; }
  header .company {
    margin-top: 4px; font-size: 13px; color: var(--header-fg); opacity: .75;
    letter-spacing: .02em;
  }
  header .subline {
    margin-top: 6px; display: flex; justify-content: center;
    gap: 16px; flex-wrap: wrap;
  }
  header .meta { font-family: var(--mono); font-size: 12px; color: var(--ink-soft); }
  #themeToggle {
    position: absolute; top: 16px; right: 22px;
    background: transparent; border: 1px solid var(--line); color: var(--ink-soft);
    padding: 4px 10px; font-size: 12px; border-radius: 3px; cursor: pointer;
  }
  @media (max-width: 560px) {
    #themeToggle { position: static; margin-top: 10px; }
  }
  #themeToggle:hover { color: var(--accent-strong); border-color: var(--accent); }
  main { max-width: 1080px; margin: 0 auto; padding: 22px; }
  section {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 4px; padding: 18px 20px; margin-bottom: 16px;
  }
  h2 {
    font-size: 11px; text-transform: uppercase; letter-spacing: .09em;
    color: var(--ink-soft); margin: 0 0 14px; font-weight: 600;
  }
  label { display: block; font-size: 12px; color: var(--ink-soft); margin-bottom: 4px; }
  input[type=text], input[type=email], select, textarea {
    width: 100%; padding: 8px 10px; border: 1px solid var(--line);
    border-radius: 3px; font-family: var(--sans); font-size: 14px;
    color: var(--ink); background: var(--input-bg);
  }
  textarea { font-family: var(--mono); font-size: 13px; min-height: 150px; resize: vertical; }
  input:focus, select:focus, textarea:focus {
    outline: 2px solid var(--accent); outline-offset: -1px; border-color: var(--accent);
  }
  .grid { display: grid; gap: 14px; grid-template-columns: 1fr 1fr; }
  .grid.three { grid-template-columns: 1fr 1fr 1fr; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  button {
    font-family: var(--sans); font-size: 13px; font-weight: 500;
    padding: 8px 14px; border-radius: 3px; border: 1px solid var(--line);
    background: var(--surface-2); color: var(--ink); cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent-strong); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #071018; font-weight: 600; }
  button.primary:hover:not(:disabled) { background: var(--accent-strong); color: #071018; }
  .tabs { display: flex; gap: 2px; margin-bottom: 10px; }
  .tabs button {
    border-radius: 3px 3px 0 0; border: none; border-bottom: 2px solid transparent;
    background: transparent; color: var(--ink-soft);
  }
  .tabs button[aria-selected=true] {
    color: var(--accent-strong); border-bottom-color: var(--accent); font-weight: 600;
  }
  .hint { font-size: 12px; color: var(--ink-soft); margin-top: 6px; }
  .hint code { font-family: var(--mono); background: var(--surface-2); padding: 1px 4px; border-radius: 2px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th {
    text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
    color: var(--ink-soft); font-weight: 600; padding: 6px 8px; border-bottom: 1px solid var(--line);
  }
  td { padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
  td.addr { font-family: var(--mono); font-size: 12px; }
  td.vars { font-family: var(--mono); font-size: 11px; color: var(--ink-soft); }
  .pill {
    display: inline-block; font-family: var(--mono); font-size: 11px;
    padding: 1px 7px; border-radius: 10px; border: 1px solid var(--line); color: var(--ink-soft);
  }
  .pill.sending { border-color: var(--amber-border); color: var(--amber); background: var(--amber-soft); }
  .pill.sent { border-color: var(--ok-border); color: var(--ok); background: var(--ok-soft); }
  .pill.failed { border-color: var(--bad-border); color: var(--bad); background: var(--bad-soft); }
  .pill.skipped { text-decoration: line-through; }
  tr.is-failed td { background: var(--row-bad); }
  .banner {
    padding: 9px 12px; border-radius: 3px; font-size: 13px; margin-bottom: 12px;
    border: 1px solid var(--accent-border); background: var(--accent-soft); color: var(--accent-ink);
  }
  .banner.bad { background: var(--bad-soft); border-color: var(--bad-border); color: var(--bad); }
  .banner.good { background: var(--ok-soft); border-color: var(--ok-border); color: var(--ok); }
  .banner:empty { display: none; }
  .checkline { display: flex; align-items: center; gap: 7px; font-size: 13px; color: var(--ink); }
  .checkline input { width: auto; }
  .tablewrap { max-height: 420px; overflow: auto; border: 1px solid var(--line); border-radius: 3px; }
  .tablewrap th { position: sticky; top: 0; background: var(--surface); z-index: 1; }
  pre {
    font-family: var(--mono); font-size: 12px; background: var(--code-bg); color: var(--accent-ink);
    padding: 12px; border-radius: 3px; overflow: auto; max-height: 260px; margin: 0;
    border: 1px solid var(--line);
  }
  .kv { display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; margin-bottom: 8px; }
  .droparea {
    border: 1px dashed var(--line); border-radius: 4px; padding: 12px 14px;
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap; background: var(--surface-2);
  }
  .droparea.over { border-color: var(--accent); background: var(--accent-soft); }
  .droparea .hint { margin: 0; }
  .modes { display: grid; gap: 10px; grid-template-columns: 1fr 1fr; }
  .modecard {
    display: block; border: 1px solid var(--line); border-radius: 4px;
    padding: 12px 14px; cursor: pointer; background: var(--surface-2); margin: 0;
  }
  .modecard:hover { border-color: var(--accent); }
  .modecard.selected { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); background: var(--accent-soft); }
  .modecard input { width: auto; margin-right: 8px; }
  .modetitle { font-size: 14px; font-weight: 600; color: var(--ink); }
  .modecard.selected .modetitle { color: var(--accent-strong); }
  .modebadge {
    float: right; font-family: var(--mono); font-size: 10px; letter-spacing: .08em;
    color: var(--accent-strong); visibility: hidden;
  }
  .modecard.selected .modebadge { visibility: visible; }
  .modedesc { display: block; font-size: 12px; color: var(--ink-soft); margin-top: 5px; }
  .modestrip {
    border-radius: 3px; padding: 11px 14px; margin-bottom: 14px;
    font-size: 14px; font-weight: 600; border: 1px solid;
  }
  .modestrip.bcc { background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent-ink); }
  .modestrip.each { background: var(--amber-soft); border-color: var(--amber-border); color: var(--amber); }
  .modestrip .sub { display: block; font-weight: 400; font-size: 12px; margin-top: 3px; opacity: .85; }
  .greet {
    border-bottom: 1px solid var(--line); padding: 9px 10px; font-size: 13px;
  }
  .greet:last-child { border-bottom: none; }
  .greet .who { font-family: var(--mono); font-size: 11px; color: var(--ink-soft); display: block; }
  .greet .line { color: var(--ink); }
  .greet .line mark { background: var(--ok-soft); color: var(--ok); padding: 0 3px; border-radius: 2px; }
  .greet.miss .line { color: var(--bad); }
  .footnote { font-size: 12px; color: var(--ink-soft); text-align: center; padding-bottom: 30px; }
  @media (max-width: 720px) { .grid, .grid.three, .modes { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>SMTP Bulk Mailer</h1>
  <div class="company">DCB Technology, LLC &copy; 2026</div>
  <div class="subline">
    <span class="meta">send templated email</span>
    <span class="meta" id="keyState">checking key…</span>
  </div>
  <button id="themeToggle" type="button">Light mode</button>
</header>

<main>
  <div class="banner" id="topBanner"></div>

  <section>
    <h2>Account</h2>
    <div class="grid">
      <div>
        <label for="keySelect">API key in use</label>
        <select id="keySelect"><option value="">No keys saved yet</option></select>
        <div class="hint" id="keyHint"></div>
      </div>
      <div>
        <label>&nbsp;</label>
        <div class="row">
          <button id="addKeyBtn">Add a key</button>
          <button id="renameKeyBtn">Rename</button>
          <button id="removeKeyBtn">Remove</button>
          <button id="resetSettings">Clear saved settings</button>
          <span class="hint" id="savedNote"></span>
        </div>
      </div>
    </div>

    <div id="addKeyPanel" hidden style="margin-top:14px; padding-top:14px; border-top:1px solid var(--line)">
      <div class="grid three">
        <div>
          <label for="newKeyLabel">Label</label>
          <input type="text" id="newKeyLabel" placeholder="Main account" autocomplete="off">
        </div>
        <div>
          <label for="newKeyValue">API key</label>
          <input type="text" id="newKeyValue" placeholder="api-xxxxxxxxxxxxxxxxxxxx" autocomplete="off">
        </div>
        <div>
          <label>&nbsp;</label>
          <div class="row">
            <button id="saveNewKey" class="primary">Save key</button>
            <button id="cancelNewKey">Cancel</button>
          </div>
        </div>
      </div>
    </div>

    <div class="hint" style="margin-top:10px">Keys are stored with their labels in <code id="configPath">smtp2go_config.json</code> beside this script, as plain text — the same file that holds your sender details and recipient lists. Set <code>SMTP2GO_API_KEY</code> as a Windows environment variable instead if you would rather a key never touch disk; it shows up in the list automatically and is never written to the file. The delivery log tracks sends per key, so switching keys switches which history you see.</div>
  </section>

  <section>
    <h2>Message</h2>
    <div class="grid three">
      <div>
        <label for="fromName">From name</label>
        <input type="text" id="fromName" value="Support Team">
      </div>
      <div>
        <label for="fromEmail">From address (must be a verified sender)</label>
        <input type="email" id="fromEmail" value="support@example.com">
      </div>
      <div>
        <label for="replyTo">Reply-To</label>
        <input type="email" id="replyTo" value="support@example.com">
      </div>
    </div>
    <div class="grid" style="margin-top:14px">
      <div>
        <label for="templateSel">Template</label>
        <div class="row">
          <select id="templateSel" style="flex:1"><option value="">Load templates…</option></select>
          <button id="loadTemplates">Load</button>
        </div>
        <div class="hint" id="templateHint">Pick from your account, or type an ID below.</div>
      </div>
      <div>
        <label for="templateId">Template ID</label>
        <input type="text" id="templateId" placeholder="1234567">
        <div class="hint">Overrides the dropdown when filled in.</div>
      </div>
    </div>
    <div style="margin-top:14px">
      <label for="subject">Subject override (optional)</label>
      <input type="text" id="subject" placeholder="Leave blank to use the template's own subject">
    </div>
  </section>

  <section>
    <h2>Recipients</h2>
    <div class="tabs" role="tablist">
      <button role="tab" id="tabList" aria-selected="true">Plain list</button>
      <button role="tab" id="tabCsv" aria-selected="false">CSV with merge fields</button>
    </div>

    <div id="panelList">
      <textarea id="listInput" placeholder="jane@example.com&#10;John Smith &lt;john@example.com&gt;&#10;billing@example.org"></textarea>
      <div class="hint">One per line. <code>Name &lt;email&gt;</code> or just the address.</div>
    </div>

    <div id="panelCsv" hidden>
      <div class="droparea" id="dropArea">
        <input type="file" id="csvFile" accept=".csv,.tsv,.txt,text/csv" hidden>
        <button id="pickFile">Choose a CSV file</button>
        <span class="hint" id="fileNote">or drag one here, or just paste below — the file is read in this page and never uploaded anywhere</span>
      </div>
      <label class="checkline" style="margin:10px 0"><input type="checkbox" id="skipDisabled" checked> Skip disabled accounts and guest users</label>
      <textarea id="csvInput" placeholder="displayName,mail&#10;Jane Doe,jdoe@example.com&#10;John Smith,jsmith@example.com&#10;Shared Mailbox,No data"></textarea>
      <div class="hint">Paste a Microsoft 365 or Entra export straight in — the address column is found automatically from <code>email</code>, <code>mail</code>, <code>emailAddress</code>, <code>primary SMTP address</code> or <code>userPrincipalName</code>, and the name from <code>displayName</code>, <code>name</code> or <code>full name</code>. That name is split into <code>{{first_name}}</code>, <code>{{last_name}}</code> and <code>{{full_name}}</code>. Every original column stays available under its own header too. Rows reading "No data" are skipped and listed.</div>
    </div>

    <div style="margin-top:16px">
      <label>Variables applied to everyone</label>
      <div id="globalVars"></div>
      <button id="addVar">Add variable</button>
      <div class="hint">Per-recipient CSV columns win over these.</div>
    </div>

    <div style="margin-top:18px; padding-top:16px; border-top:1px solid var(--line)">
      <label>How to address it</label>
      <div class="modes">
        <label class="modecard" id="cardEach" for="modeEach">
          <span class="modebadge">SELECTED</span>
          <input type="radio" name="mode" id="modeEach" value="each" checked>
          <span class="modetitle">One email each</span>
          <span class="modedesc">A separate send per person, addressed only to them. Required for {{first_name}} and any other per-person variable.</span>
        </label>
        <label class="modecard" id="cardBcc" for="modeBcc">
          <span class="modebadge">SELECTED</span>
          <input type="radio" name="mode" id="modeBcc" value="bcc">
          <span class="modetitle">BCC in batches</span>
          <span class="modedesc">One shared email per batch, everyone hidden from each other. Identical text for all of them — no personalisation.</span>
        </label>
      </div>

      <div id="bccOptions" style="margin-top:14px">
        <div class="grid">
          <div>
            <label for="bccTo">Visible To address on each batch</label>
            <input type="email" id="bccTo" placeholder="defaults to your From address">
            <div class="hint">Every email needs one visible recipient. Your own address is the usual choice.</div>
          </div>
          <div>
            <label for="batchSize">Recipients per batch</label>
            <input type="text" id="batchSize" value="50" inputmode="numeric">
            <div class="hint">SMTP2GO's ceiling is 100. Fifty keeps you clear of the older dedicated-IP routing rule.</div>
          </div>
        </div>
        <div class="banner bad" style="margin-top:12px"><strong>BCC cannot personalise.</strong> One email body is rendered and sent to the whole batch, so {{first_name}}, {{last_name}}, {{full_name}} and every other per-person column are dropped. If your template uses them, every recipient sees the fallback greeting instead of their name. Only the "applies to everyone" variables below are merged.</div>
      </div>
    </div>

    <div class="row" style="margin-top:16px">
      <button id="buildList" class="primary">Build the list</button>
      <button id="useEach" hidden>Switch to one email each</button>
      <span class="hint" id="buildCount"></span>
    </div>
    <div class="hint" id="nameHint" style="margin-top:8px"></div>

    <div id="greetStep" hidden style="margin-top:16px; padding-top:16px; border-top:1px solid var(--line)">
      <div class="row">
        <button id="checkGreeting" class="primary">Check how each person is addressed</button>
        <span class="hint" id="greetSummary"></span>
      </div>
      <div class="banner" id="greetBanner" style="margin-top:12px"></div>
      <div class="tablewrap" id="greetWrap" hidden style="max-height:300px; margin-top:4px"></div>
    </div>
  </section>

  <section id="reviewSection" hidden>
    <h2>Review and send</h2>
    <div class="modestrip" id="modeStrip"></div>
    <div class="banner" id="sendBanner"></div>
    <div class="grid three">
      <div>
        <label for="pace">Pace</label>
        <select id="pace">
          <option value="1000">Careful — 1 per second</option>
          <option value="500" selected>Normal — 2 per second</option>
          <option value="200">Fast — 5 per second</option>
        </select>
      </div>
      <div>
        <label for="testAddr">Test send to</label>
        <div class="row">
          <input type="email" id="testAddr" placeholder="you@example.com" style="flex:1">
          <button id="sendTest">Send test</button>
        </div>
      </div>
      <div>
        <label>&nbsp;</label>
        <div class="row">
          <label class="checkline"><input type="checkbox" id="dryRun"> Dry run (build only, send nothing)</label>
        </div>
      </div>
    </div>

    <div class="row" style="margin:16px 0">
      <button id="sendAll" class="primary">Send to everyone</button>
      <button id="stopSend" class="danger" hidden>Stop</button>
      <button id="retryFailed" hidden>Retry the failures</button>
      <button id="copyFailed" hidden>Copy failed addresses</button>
      <span class="hint" id="progress"></span>
    </div>

    <div class="tablewrap">
      <table>
        <thead><tr><th style="width:20%">Name</th><th style="width:28%">Address</th><th>Variables</th><th style="width:22%">Status</th></tr></thead>
        <tbody id="tableBody"></tbody>
      </table>
    </div>

    <div style="margin-top:14px" id="payloadWrap" hidden>
      <label>What gets posted for the first recipient</label>
      <pre id="payloadOut"></pre>
    </div>
  </section>

  <section id="logSection" hidden>
    <h2>Delivery log — what actually landed</h2>
    <div class="row">
      <button id="refreshLog" class="primary">Check delivery</button>
      <label class="checkline"><input type="checkbox" id="autoLog"> Keep checking every 20 seconds</label>
      <button id="copyLog">Copy as CSV</button>
      <button id="widenLog">Include earlier sends from this key</button>
      <button id="showDropped" hidden>Why is something missing?</button>
    </div>
    <div class="banner" id="logBanner" style="margin-top:12px"></div>
    <div class="hint" id="logSummary" style="margin-bottom:8px"></div>
    <div class="tablewrap" style="max-height:380px">
      <table>
        <thead><tr><th style="width:16%">When</th><th style="width:26%">Recipient</th><th style="width:22%">Subject</th><th style="width:14%">Status</th><th>Server response</th></tr></thead>
        <tbody id="logBody"></tbody>
      </table>
    </div>
    <pre id="droppedBox" hidden style="margin:12px 0"></pre>
    <div class="hint">Only mail this tool sent appears here. SMTP2GO's activity endpoint has no message-ID filter, so the whole account stream comes back and every row is checked against the message IDs and recipients recorded at send time — anything else is discarded before it reaches this table. Mail from the SMTP2GO app, SMTP relay, monitoring alerts or another key can't show up. Status is what the receiving server said, not just that the API accepted the send. BCC'd recipients each get their own row.</div>
  </section>

  <p class="footnote">SMTP Bulk Mailer · DCB Technology, LLC &copy; 2026 · running locally on port __PORT__ · close the terminal window to stop</p>
</main>

<script>
const TOKEN = "__TOKEN__";
let recipients = [];
let sending = false;
let stopRequested = false;

const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Token": TOKEN },
    body: JSON.stringify(body || {})
  });
  let data;
  try { data = await res.json(); }
  catch (e) { throw new Error("The local server returned something unreadable."); }
  if (data.ok === false) throw new Error(data.error || "Request failed.");
  return data;
}

function banner(el, message, kind) {
  el.textContent = message || "";
  el.className = "banner" + (kind ? " " + kind : "");
}

/* ---------- settings ---------- */

let saveTimer = null;
let bootDone = false;

function collectSettings() {
  return {
    sender_name: $("fromName").value,
    sender_email: $("fromEmail").value,
    reply_to: $("replyTo").value,
    template_id: currentTemplateId(),
    subject: $("subject").value,
    mode: sendMode(),
    bcc_to: $("bccTo").value,
    batch_size: $("batchSize").value,
    pace: $("pace").value,
    test_addr: $("testAddr").value,
    dry_run: $("dryRun").checked,
    skip_disabled: $("skipDisabled").checked,
    theme: document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark",
    active_tab: $("panelCsv").hidden ? "list" : "csv",
    recipients_list: $("listInput").value,
    recipients_csv: $("csvInput").value,
    global_vars: readGlobalVarRows()
  };
}

function applySettings(cfg) {
  if (!cfg) return;
  $("fromName").value = cfg.sender_name || "";
  $("fromEmail").value = cfg.sender_email || "";
  $("replyTo").value = cfg.reply_to || "";
  $("templateId").value = cfg.template_id || "";
  $("subject").value = cfg.subject || "";
  $("bccTo").value = cfg.bcc_to || "";
  $("batchSize").value = cfg.batch_size || "50";
  $("testAddr").value = cfg.test_addr || "";
  $("dryRun").checked = !!cfg.dry_run;
  $("skipDisabled").checked = cfg.skip_disabled !== false;
  applyTheme(cfg.theme === "light" ? "light" : "dark");
  // Only accept a pace the dropdown actually offers, so a hand-edited config
  // cannot leave the select blank and remove the throttle.
  if (cfg.pace && [...$("pace").options].some(o => o.value === String(cfg.pace))) {
    $("pace").value = cfg.pace;
  }
  $("listInput").value = cfg.recipients_list || "";
  $("csvInput").value = cfg.recipients_csv || "";
  selectTab(cfg.active_tab === "csv" ? "csv" : "list");

  $("globalVars").innerHTML = "";
  const rows = Array.isArray(cfg.global_vars) ? cfg.global_vars : [];
  if (rows.length) rows.forEach(r => addVarRow(r.name, r.value));
  else addVarRow("", "");

  const bcc = cfg.mode === "bcc";
  $("modeBcc").checked = bcc;
  $("modeEach").checked = !bcc;
}

function applyTheme(theme) {
  if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  $("themeToggle").textContent = theme === "light" ? "Dark mode" : "Light mode";
}

$("themeToggle").addEventListener("click", () => {
  const nowLight = document.documentElement.getAttribute("data-theme") === "light";
  applyTheme(nowLight ? "dark" : "light");
  scheduleSave();
});

function markSaved(text) {
  $("savedNote").textContent = text || "";
  if (text) setTimeout(() => { if ($("savedNote").textContent === text) $("savedNote").textContent = ""; }, 2500);
}

async function saveSettings() {
  const state = await api("/api/settings", { settings: collectSettings(), remember: true });
  markSaved("Settings saved");
  return state;
}

function scheduleSave() {
  if (!bootDone) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveSettings().catch(e => markSaved("Not saved: " + e.message));
  }, 700);
}

/* Everything the page remembers. Typing in any of these queues a save. */
const WATCHED = ["fromName", "fromEmail", "replyTo", "templateId", "templateSel", "subject",
                 "bccTo", "batchSize", "pace", "testAddr", "dryRun", "skipDisabled",
                 "listInput", "csvInput", "modeBcc", "modeEach"];

function wireAutosave() {
  WATCHED.forEach(id => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", scheduleSave);
    el.addEventListener("change", scheduleSave);
  });
  $("globalVars").addEventListener("input", scheduleSave);
  $("globalVars").addEventListener("click", () => setTimeout(scheduleSave, 0));
  $("tabList").addEventListener("click", scheduleSave);
  $("tabCsv").addEventListener("click", scheduleSave);
  const flush = () => {
    if (!saveTimer) return;
    clearTimeout(saveTimer);
    saveTimer = null;
    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Token": TOKEN },
      body: JSON.stringify({ settings: collectSettings(), remember: true }),
      keepalive: true
    }).catch(() => {});
  };
  window.addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", () => { if (document.hidden) flush(); });
}

/* ---------- api keys ---------- */

let knownKeys = [];

function renderKeys(state) {
  knownKeys = state.keys || [];
  const sel = $("keySelect");
  sel.textContent = "";
  if (!knownKeys.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No keys saved yet — click Add a key";
    sel.appendChild(opt);
  }
  knownKeys.forEach(k => {
    const opt = document.createElement("option");
    opt.value = k.id;
    opt.textContent = k.label + "  (…" + k.hint + ")" + (k.from_env ? "  [from environment]" : "");
    sel.appendChild(opt);
  });
  sel.value = state.active_key_id || "";

  const active = knownKeys.find(k => k.id === state.active_key_id);
  $("keyHint").textContent = active
    ? (active.from_env
        ? "Loaded from the SMTP2GO_API_KEY environment variable — not stored in the config file."
        : "Saved in the config file. Sends, templates and the delivery log all use this key.")
    : "Add a key to get started.";
  $("removeKeyBtn").disabled = !active || active.from_env;
  $("renameKeyBtn").disabled = !active || active.from_env;

  $("keyState").textContent = active ? "key: " + active.label + " …" + active.hint : "no API key yet";
}

async function keyAction(payload) {
  const state = await api("/api/keys", payload);
  renderKeys(state);
  return state;
}

$("keySelect").addEventListener("change", async () => {
  try {
    await keyAction({ action: "select", id: $("keySelect").value });
    markSaved("Key switched");
    // A different key means a different account: different templates, different
    // send history. Anything cached from the previous key is now meaningless.
    sessionEmailIds = [];
    lastLogRows = [];
    $("logBody").textContent = "";
    $("logSummary").textContent = "";
    banner($("logBanner"), "Switched key — the delivery log now shows this key's sends only.");

    Object.keys(templateViewIds).forEach(k => delete templateViewIds[k]);
    $("templateSel").textContent = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Load templates for this key…";
    $("templateSel").appendChild(opt);
    $("templateId").value = "";
    $("templateHint").textContent = "Templates belong to the account this key is on — click Load.";
    $("greetStep").hidden = recipients.length === 0;
    $("greetWrap").hidden = true;
    $("greetWrap").textContent = "";
    $("greetSummary").textContent = "";
    banner($("greetBanner"), "");
    banner($("topBanner"), "Key switched. Templates belong to that account — click Load and pick the template again before sending.", "bad");
    scheduleSave();
  } catch (e) { banner($("topBanner"), e.message, "bad"); }
});

$("addKeyBtn").addEventListener("click", () => {
  $("addKeyPanel").hidden = false;
  $("newKeyLabel").focus();
});
$("cancelNewKey").addEventListener("click", () => {
  $("addKeyPanel").hidden = true;
  $("newKeyLabel").value = "";
  $("newKeyValue").value = "";
});

$("saveNewKey").addEventListener("click", async () => {
  const label = $("newKeyLabel").value.trim();
  const key = $("newKeyValue").value.trim();
  if (!key) { banner($("topBanner"), "Paste the API key first.", "bad"); return; }
  try {
    await keyAction({ action: "add", label: label, key: key });
    $("addKeyPanel").hidden = true;
    $("newKeyLabel").value = "";
    $("newKeyValue").value = "";
    banner($("topBanner"), "Key saved and selected. Load your templates to check it works.", "good");
  } catch (e) { banner($("topBanner"), e.message, "bad"); }
});

$("renameKeyBtn").addEventListener("click", async () => {
  const id = $("keySelect").value;
  const current = knownKeys.find(k => k.id === id);
  if (!current) return;
  const label = prompt("Label for this key:", current.label);
  if (label === null || !label.trim()) return;
  try { await keyAction({ action: "rename", id: id, label: label.trim() }); }
  catch (e) { banner($("topBanner"), e.message, "bad"); }
});

$("removeKeyBtn").addEventListener("click", async () => {
  const id = $("keySelect").value;
  const current = knownKeys.find(k => k.id === id);
  if (!current) return;
  if (!confirm("Remove the key \"" + current.label + "\"?\n\nIt is deleted from the config file. Sends already made are not affected.")) return;
  try { await keyAction({ action: "remove", id: id }); }
  catch (e) { banner($("topBanner"), e.message, "bad"); }
});

async function refreshState() {
  const state = await api("/api/state");
  renderKeys(state);
  $("configPath").textContent = state.config_path;
  return state;
}

$("resetSettings").addEventListener("click", async () => {
  if (!confirm("Clear every saved setting, including the recipient lists?\n\nYour saved API keys are kept.")) return;
  try {
    const state = await api("/api/reset", {});
    applySettings(state.settings);
    renderKeys(state);
    onModeChange();
    markSaved("Saved settings cleared");
  } catch (e) { banner($("topBanner"), e.message, "bad"); }
});

/* ---------- templates ---------- */

const templateViewIds = {};

function currentViewId() {
  const id = currentTemplateId();
  return templateViewIds[id] || id;
}

function currentTemplateId() {
  return ($("templateId").value.trim() || $("templateSel").value || "").trim();
}

$("loadTemplates").addEventListener("click", async () => {
  banner($("topBanner"), "");
  $("loadTemplates").disabled = true;
  try {
    const res = await api("/api/templates");
    const sel = $("templateSel");
    sel.innerHTML = '<option value="">— choose a template —</option>';
    res.templates.forEach(t => {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = t.name + " (" + t.id + ")";
      opt.dataset.subject = t.subject || "";
      opt.dataset.viewId = t.view_id || t.id;
      templateViewIds[t.id] = t.view_id || t.id;
      sel.appendChild(opt);
    });
    $("templateHint").textContent = res.templates.length
      ? res.templates.length + " templates in this account."
      : "No templates came back — create one in Settings > Templates.";
  } catch (e) {
    banner($("topBanner"), e.message, "bad");
  } finally {
    $("loadTemplates").disabled = false;
  }
});

$("templateSel").addEventListener("change", () => {
  const opt = $("templateSel").selectedOptions[0];
  $("templateId").value = "";
  $("templateHint").textContent = opt && opt.dataset.subject
    ? "Template subject: " + opt.dataset.subject
    : "Pick from your account, or type an ID below.";
});

/* ---------- tabs ---------- */

function selectTab(which) {
  const list = which === "list";
  $("tabList").setAttribute("aria-selected", String(list));
  $("tabCsv").setAttribute("aria-selected", String(!list));
  $("panelList").hidden = !list;
  $("panelCsv").hidden = list;
}
function loadCsvFile(file) {
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) {
    banner($("topBanner"), "That file is over 8 MB — larger than any sensible recipient list.", "bad");
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    $("csvInput").value = String(reader.result || "");
    selectTab("csv");
    $("fileNote").textContent = file.name + " loaded — " + ($("csvInput").value.split("\n").length - 1) + " rows. Click Build the list.";
    banner($("topBanner"), "");
    scheduleSave();
  };
  reader.onerror = () => banner($("topBanner"), "Could not read that file.", "bad");
  reader.readAsText(file);
}

$("pickFile").addEventListener("click", () => $("csvFile").click());
$("csvFile").addEventListener("change", (ev) => loadCsvFile(ev.target.files[0]));

["dragenter", "dragover"].forEach(evt =>
  $("dropArea").addEventListener(evt, (e) => { e.preventDefault(); $("dropArea").classList.add("over"); }));
["dragleave", "drop"].forEach(evt =>
  $("dropArea").addEventListener(evt, (e) => { e.preventDefault(); $("dropArea").classList.remove("over"); }));
$("dropArea").addEventListener("drop", (e) => {
  if (e.dataTransfer && e.dataTransfer.files.length) loadCsvFile(e.dataTransfer.files[0]);
});
// Stop a stray drop elsewhere on the page from navigating away from the tool.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

$("tabList").addEventListener("click", () => selectTab("list"));
$("tabCsv").addEventListener("click", () => selectTab("csv"));

/* ---------- global variables ---------- */

function addVarRow(name, value) {
  const wrap = document.createElement("div");
  wrap.className = "kv";
  const k = document.createElement("input");
  k.type = "text"; k.placeholder = "variable name"; k.value = name || "";
  const v = document.createElement("input");
  v.type = "text"; v.placeholder = "value"; v.value = value || "";
  const rm = document.createElement("button");
  rm.textContent = "Remove";
  rm.addEventListener("click", () => wrap.remove());
  wrap.append(k, v, rm);
  $("globalVars").appendChild(wrap);
}
$("addVar").addEventListener("click", () => addVarRow("", ""));

$("useEach").addEventListener("click", () => {
  $("modeEach").checked = true;
  $("modeBcc").checked = false;
  onModeChange();
  $("useEach").hidden = true;
  scheduleSave();
});

function readGlobalVarRows() {
  const rows = [];
  $("globalVars").querySelectorAll(".kv").forEach(row => {
    const inputs = row.querySelectorAll("input");
    const name = inputs[0].value.trim();
    if (name) rows.push({ name: name, value: inputs[1].value });
  });
  return rows;
}

function readGlobalVars() {
  const out = {};
  $("globalVars").querySelectorAll(".kv").forEach(row => {
    const inputs = row.querySelectorAll("input");
    const key = inputs[0].value.trim();
    if (key) out[key] = inputs[1].value;
  });
  return out;
}

/* ---------- parsing ---------- */

function parseDelimited(text, delim) {
  const rows = [];
  let row = [], cur = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') { cur += '"'; i++; } else { quoted = false; }
      } else { cur += ch; }
    } else if (ch === '"') { quoted = true; }
    else if (ch === delim) { row.push(cur); cur = ""; }
    else if (ch === "\n") { row.push(cur); rows.push(row); row = []; cur = ""; }
    else if (ch !== "\r") { cur += ch; }
  }
  if (cur.length || row.length) { row.push(cur); rows.push(row); }
  return rows.filter(r => r.some(f => f.trim() !== ""));
}

/* Pasting from Excel gives tabs; a saved export gives commas; some European
   exports give semicolons. Try each and keep whichever splits into most columns. */
function parseCsv(text) {
  const clean = String(text || "").replace(/^\uFEFF/, "");   // strip the BOM Excel writes
  let best = [], bestWidth = 0;
  ["\t", ",", ";", "|"].forEach(delim => {
    const rows = parseDelimited(clean, delim);
    const width = rows.length ? rows[0].length : 0;
    if (width > bestWidth) { bestWidth = width; best = rows; }
  });
  return best;
}

function splitAddress(line) {
  const m = line.match(/^\s*(.*?)\s*<\s*([^>]+)\s*>\s*$/);
  if (m) return { name: m[1].replace(/^["']|["']$/g, "").trim(), email: m[2].trim() };
  return { name: "", email: line.trim() };
}

const EMAIL_RE = /^[^@\s]+@[^@\s.]+\.[^@\s]+$/;

/* Column names that Microsoft 365, Entra, Exchange and most CRM exports use. */
const EMAIL_COLS = ["email", "mail", "email address", "emailaddress", "e mail",
                    "primary smtp address", "primarysmtpaddress", "smtp address",
                    "userprincipalname", "user principal name", "upn", "address"];
const NAME_COLS  = ["name", "displayname", "display name", "full name", "fullname",
                    "user", "contact", "recipient"];
const ENABLED_COLS = ["accountenabled", "account enabled", "enabled", "status"];
const TYPE_COLS = ["usertype", "user type", "type"];
const IS_DISABLED = /^(no|false|disabled|blocked|inactive)$/i;
/* A column like licenseAssignmentStates holds a JSON blob nobody templates on.
   Sending it as a variable per recipient just bloats every request. */
const MAX_VAR_LENGTH = 120;
/* Cells an export writes when a mailbox has no address. */
const NO_ADDRESS = /^(no data|none|n\/a|na|null|-|--)$/i;

function normHeader(h) {
  return String(h || "").replace(/^\uFEFF/, "").trim().toLowerCase().replace(/[_\s]+/g, " ");
}

function findColumn(headers, candidates) {
  const norm = headers.map(normHeader);
  for (const want of candidates) {
    const at = norm.indexOf(want);
    if (at !== -1) return at;
  }
  return -1;
}

/* "Jane Doe" -> Jane / Doe.  "Doe, Jane" -> Jane / Doe. */
function splitName(display) {
  const clean = String(display || "").trim().replace(/\s+/g, " ");
  if (!clean) return { first: "", last: "", full: "" };
  if (clean.indexOf(",") !== -1) {
    const parts = clean.split(",");
    const last = parts[0].trim();
    const first = (parts[1] || "").trim().split(" ")[0] || "";
    return { first: first, last: last, full: (first + " " + last).trim() };
  }
  const words = clean.split(" ");
  return {
    first: words[0],
    last: words.length > 1 ? words[words.length - 1] : "",
    full: clean
  };
}

function nameVars(display) {
  const n = splitName(display);
  return { first_name: n.first, last_name: n.last, full_name: n.full || display || "" };
}

function buildRecipients() {
  const globals = readGlobalVars();
  const out = [];
  const problems = [];
  const noAddress = [];
  const inactive = [];

  if ($("panelList").hidden === false) {
    $("listInput").value.split("\n").forEach(line => {
      if (!line.trim()) return;
      const parsed = splitAddress(line);
      if (!EMAIL_RE.test(parsed.email)) { problems.push(line.trim()); return; }
      const data = Object.assign({}, globals, nameVars(parsed.name), {
        email: parsed.email,
        name: parsed.name
      });
      out.push({ name: parsed.name, email: parsed.email, data: data, status: "queued", detail: "" });
    });
  } else {
    const rows = parseCsv($("csvInput").value);
    if (!rows.length) return { list: [], problems: [], noAddress: [], inactive: [], error: "Paste or upload a CSV with a header row first." };

    const header = rows[0].map(h => String(h).trim());
    const emailIdx = findColumn(header, EMAIL_COLS);
    const nameIdx = findColumn(header, NAME_COLS);
    if (emailIdx === -1) {
      return { list: [], problems: [], noAddress: [], inactive: [], error:
        "No address column found. Recognised headers: email, mail, emailAddress, "
        + "primary SMTP address, userPrincipalName. Yours were: " + header.join(", ") };
    }

    const enabledIdx = findColumn(header, ENABLED_COLS);
    const typeIdx = findColumn(header, TYPE_COLS);
    const skipInactive = $("skipDisabled").checked;

    rows.slice(1).forEach(cells => {
      const email = String(cells[emailIdx] || "").trim();
      const display = nameIdx === -1 ? "" : String(cells[nameIdx] || "").trim();

      if (skipInactive && enabledIdx !== -1 && IS_DISABLED.test(String(cells[enabledIdx] || "").trim())) {
        inactive.push((display || email) + " (disabled)");
        return;
      }
      if (skipInactive && typeIdx !== -1 && /^guest$/i.test(String(cells[typeIdx] || "").trim())) {
        inactive.push((display || email) + " (guest)");
        return;
      }
      if (!email || NO_ADDRESS.test(email)) { noAddress.push(display || "(unnamed row)"); return; }
      if (!EMAIL_RE.test(email)) { problems.push(display ? display + " — " + email : email); return; }

      // Start from the shared variables, layer on the derived name fields, then
      // every original column so a template can also use {{displayName}} as-is.
      const data = Object.assign({}, globals, nameVars(display));
      header.forEach((col, i) => {
        if (!col) return;
        const value = String(cells[i] || "").trim();
        if (value.length > MAX_VAR_LENGTH) return;
        data[col] = value;
      });
      data.email = email;
      data.name = display;

      out.push({ name: display, email: email, data: data, status: "queued", detail: "" });
    });
  }

  const seen = new Set();
  const deduped = out.filter(r => {
    const key = r.email.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return { list: deduped, problems: problems, noAddress: noAddress, inactive: inactive,
           dropped: out.length - deduped.length };
}

/* ---------- table ---------- */

function varSummary(data) {
  const keys = Object.keys(data);
  if (!keys.length) return "—";
  const shown = keys.slice(0, 6).map(k => {
    const v = String(data[k] || "");
    return k + "=" + (v.length > 32 ? v.slice(0, 32) + "…" : v);
  }).join("  ");
  return keys.length > 6 ? shown + "  (+" + (keys.length - 6) + " more)" : shown;
}

function renderTable() {
  const body = $("tableBody");
  const wrap = body.closest(".tablewrap");
  const keepScroll = wrap ? wrap.scrollTop : 0;
  body.innerHTML = "";
  recipients.forEach((r, i) => {
    const tr = document.createElement("tr");
    tr.id = "row-" + i;
    if (r.status === "failed") tr.className = "is-failed";
    const name = document.createElement("td");
    name.textContent = r.name || "—";
    const addr = document.createElement("td");
    addr.className = "addr";
    addr.textContent = r.email;
    const vars = document.createElement("td");
    vars.className = "vars";
    vars.textContent = varSummary(r.data);
    const status = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = "pill " + r.status;
    pill.textContent = r.status;
    status.appendChild(pill);
    if (r.detail) {
      const d = document.createElement("div");
      d.className = "hint";
      d.textContent = r.detail;
      status.appendChild(d);
    }
    tr.append(name, addr, vars, status);
    body.appendChild(tr);
  });
  if (wrap) wrap.scrollTop = keepScroll;
}

function scrollRowIntoView(index) {
  const tr = document.getElementById("row-" + index);
  if (!tr) return;
  const wrap = tr.closest(".tablewrap");
  if (!wrap) return;
  const row = tr.getBoundingClientRect();
  const box = wrap.getBoundingClientRect();
  if (row.top < box.top || row.bottom > box.bottom) {
    // Keep the active row around the middle rather than snapping to an edge.
    wrap.scrollTop += (row.top - box.top) - (box.height / 2 - row.height / 2);
  }
}

function patchRows(indexes) {
  indexes.forEach(i => {
    const tr = document.getElementById("row-" + i);
    if (!tr) return;
    const rec = recipients[i];
    tr.className = rec.status === "failed" ? "is-failed" : "";
    const cell = tr.cells[3];
    cell.textContent = "";
    const pill = document.createElement("span");
    pill.className = "pill " + rec.status;
    pill.textContent = rec.status;
    cell.appendChild(pill);
    if (rec.detail) {
      const note = document.createElement("div");
      note.className = "hint";
      note.textContent = rec.detail;
      cell.appendChild(note);
    }
  });
}

$("buildList").addEventListener("click", () => {
  banner($("topBanner"), "");
  const result = buildRecipients();
  if (result.error) { banner($("topBanner"), result.error, "bad"); return; }
  recipients = result.list;
  renderTable();
  $("reviewSection").hidden = recipients.length === 0;
  $("greetStep").hidden = recipients.length === 0;
  $("greetWrap").hidden = true;
  $("greetWrap").textContent = "";
  $("greetSummary").textContent = "";
  banner($("greetBanner"), "");
  if (recipients.length) $("logSection").hidden = false;
  const notes = [recipients.length + " recipients ready"];
  if (result.dropped) notes.push(result.dropped + " duplicates removed");
  if (result.inactive.length) notes.push(result.inactive.length + " disabled or guest accounts skipped");
  if (result.noAddress.length) notes.push(result.noAddress.length + " rows had no address");
  if (result.problems.length) notes.push(result.problems.length + " unreadable addresses");
  $("buildCount").textContent = notes.join(" · ");

  const skipped = [];
  if (result.inactive.length) skipped.push("Skipped: " + result.inactive.slice(0, 6).join(", ")
    + (result.inactive.length > 6 ? " and " + (result.inactive.length - 6) + " more" : ""));
  if (result.noAddress.length) skipped.push("No address: " + result.noAddress.slice(0, 6).join(", ")
    + (result.noAddress.length > 6 ? " and " + (result.noAddress.length - 6) + " more" : ""));
  if (result.problems.length) skipped.push("Unreadable: " + result.problems.slice(0, 4).join(" | "));
  banner($("topBanner"), skipped.join("  —  "), skipped.length ? "bad" : "");

  // Names only mean anything if each person gets their own email.
  const hasNames = recipients.some(r => r.data.first_name);
  $("useEach").hidden = !(hasNames && sendMode() === "bcc");
  if (hasNames) {
    const sample = recipients.slice(0, 3).map(r => (r.data.first_name || "?")).join(", ");
    $("nameHint").textContent = "First names picked up: " + sample
      + (recipients.length > 3 ? ", …" : "")
      + ". Use {{first_name}}, {{last_name}} or {{full_name}} in the template.";
  } else {
    $("nameHint").textContent = "";
  }
  $("retryFailed").hidden = true;
  $("copyFailed").hidden = true;
  describeMode();
});

/* ---------- sending ---------- */

function sendMode() {
  const picked = document.querySelector("input[name=mode]:checked");
  return picked ? picked.value : "each";
}

function baseBody() {
  return {
    sender_name: $("fromName").value.trim(),
    sender_email: $("fromEmail").value.trim(),
    reply_to: $("replyTo").value.trim(),
    template_id: currentTemplateId(),
    subject: $("subject").value.trim(),
    dry_run: $("dryRun").checked
  };
}

function addressOf(rec) {
  return rec.name ? rec.name + " <" + rec.email + ">" : rec.email;
}

function batchSize() {
  const n = parseInt($("batchSize").value, 10);
  if (!n || n < 1) return 50;
  return Math.min(n, 100);
}

/* A job is one API call. One recipient in "each" mode, one batch in BCC mode. */
function buildJobs(indexes) {
  if (sendMode() === "each") {
    return indexes.map(i => ({
      indexes: [i],
      label: "",
      body: Object.assign(baseBody(), {
        to_name: recipients[i].name,
        to_email: recipients[i].email,
        template_data: recipients[i].data
      })
    }));
  }
  const size = batchSize();
  const visibleTo = $("bccTo").value.trim() || $("fromEmail").value.trim();
  const globals = readGlobalVars();
  const jobs = [];
  for (let start = 0; start < indexes.length; start += size) {
    const chunk = indexes.slice(start, start + size);
    jobs.push({
      indexes: chunk,
      label: "batch " + (jobs.length + 1),
      body: Object.assign(baseBody(), {
        to_name: $("fromName").value.trim(),
        to_email: visibleTo,
        bcc: chunk.map(i => addressOf(recipients[i])),
        template_data: globals
      })
    });
  }
  return jobs;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function postSend(body) {
  let attempt = 0;
  while (true) {
    const res = await fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Token": TOKEN },
      body: JSON.stringify(body)
    }).then(r => r.json());
    if (res.ok) return res;
    const rateLimited = res.status === 429 || /rate ?limit|too many/i.test(res.error || "");
    if (rateLimited && attempt < 2) { attempt++; await sleep(6000); continue; }
    throw new Error(res.error || "Send failed.");
  }
}

async function runSend(indexes) {
  if (sending) return;
  const missing = [];
  if (!currentTemplateId()) missing.push("a template");
  if (!$("fromEmail").value.trim()) missing.push("a From address");
  if (missing.length) { banner($("sendBanner"), "Still need " + missing.join(" and ") + ".", "bad"); return; }

  sending = true;
  stopRequested = false;
  $("sendAll").disabled = true;
  $("stopSend").hidden = false;
  $("retryFailed").hidden = true;
  $("copyFailed").hidden = true;
  banner($("sendBanner"), "");
  await saveSettings().catch(() => {});

  const pace = parseInt($("pace").value, 10) || 500;  // never fall through to no throttle
  const jobs = buildJobs(indexes);
  let sent = 0, failed = 0, first = true;

  for (const job of jobs) {
    if (stopRequested) {
      job.indexes.forEach(i => {
        recipients[i].status = "skipped";
        recipients[i].detail = "stopped before sending";
      });
      continue;
    }
    job.indexes.forEach(i => {
      recipients[i].status = "sending";
      recipients[i].detail = job.label;
    });
    patchRows(job.indexes);
    scrollRowIntoView(job.indexes[0]);
    try {
      const res = await postSend(job.body);
      if (res.email_id && !sessionEmailIds.includes(res.email_id)) sessionEmailIds.push(res.email_id);
      const rejected = (res.rejected || []).map(s => String(s).toLowerCase());
      const wasRejected = (rec) => rejected.some(r => r.indexOf(rec.email.toLowerCase()) !== -1);
      job.indexes.forEach(i => {
        if (res.partial && wasRejected(recipients[i])) {
          recipients[i].status = "failed";
          recipients[i].detail = [job.label, "rejected by SMTP2GO"].filter(Boolean).join(" · ");
          failed++;
          return;
        }
        recipients[i].status = "sent";
        recipients[i].detail = res.dry_run
          ? "dry run — nothing sent"
          : [job.label, res.email_id || ""].filter(Boolean).join(" · ");
        sent++;
      });
      if (res.partial && !rejected.length) {
        banner($("sendBanner"), "One batch reported " + res.failed + " rejected address(es) without naming them. "
          + "Check the delivery log before resending anything — part of that batch did go out.", "bad");
      }
      if (first) {
        $("payloadWrap").hidden = !res.dry_run;
        if (res.dry_run) $("payloadOut").textContent = JSON.stringify(res.payload, null, 2);
      }
    } catch (e) {
      job.indexes.forEach(i => {
        recipients[i].status = "failed";
        recipients[i].detail = [job.label, e.message].filter(Boolean).join(" · ");
      });
      failed += job.indexes.length;
    }
    first = false;
    patchRows(job.indexes);
    scrollRowIntoView(job.indexes[job.indexes.length - 1]);
    $("progress").textContent = sent + " sent · " + failed + " failed · "
      + (indexes.length - sent - failed) + " to go"
      + (jobs.length !== indexes.length ? " (" + jobs.length + " batches)" : "");
    if (pace) await sleep(pace);
  }

  sending = false;
  $("sendAll").disabled = false;
  $("stopSend").hidden = true;
  if (!$("dryRun").checked && sessionEmailIds.length) {
    $("logSection").hidden = false;
    setTimeout(() => refreshLog(false), 6000);
  }
  if (failed) {
    $("retryFailed").hidden = false;
    $("copyFailed").hidden = false;
    banner($("sendBanner"), failed + " did not go out. Check the reasons in the table, then retry just those.", "bad");
  } else if (stopRequested) {
    banner($("sendBanner"), "Stopped. " + sent + " went out before you hit stop.", "bad");
  } else {
    banner($("sendBanner"), $("dryRun").checked
      ? "Dry run finished — nothing was actually sent."
      : "All " + sent + " sent. Delivery shows up in the SMTP2GO activity log.", "good");
  }
}

function describeMode() {
  const bcc = sendMode() === "bcc";
  const strip = $("modeStrip");
  const count = recipients.length;
  const jobs = count ? buildJobs(recipients.map((_, i) => i)).length : 0;

  if (bcc) {
    strip.className = "modestrip bcc";
    strip.innerHTML = "";
    strip.appendChild(document.createTextNode(
      "BCC — " + count + " recipients across " + jobs + " email" + (jobs === 1 ? "" : "s")
    ));
    const sub = document.createElement("span");
    sub.className = "sub";
    const named = recipients.filter(r => r.data && r.data.first_name).length;
    sub.textContent = "No recipient sees another. Visible To line: "
      + ($("bccTo").value.trim() || $("fromEmail").value.trim() || "your From address")
      + (named ? ". Personalisation is OFF — " + named + " names will not be merged; everyone gets the same greeting." : ".");
    strip.appendChild(sub);
    $("sendAll").textContent = "BCC to " + count + " recipients";
  } else {
    strip.className = "modestrip each";
    strip.innerHTML = "";
    strip.appendChild(document.createTextNode("One email each — " + count + " separate sends"));
    const sub = document.createElement("span");
    sub.className = "sub";
    sub.textContent = "Not BCC. Every person gets their own email addressed to them.";
    strip.appendChild(sub);
    $("sendAll").textContent = "Send " + count + " individual emails";
  }
}

function onModeChange() {
  const bcc = sendMode() === "bcc";
  $("cardBcc").classList.toggle("selected", bcc);
  $("cardEach").classList.toggle("selected", !bcc);
  $("bccOptions").hidden = !bcc;
  if (bcc && recipients.length) {
    const extras = recipients.some(r => Object.keys(r.data).length > Object.keys(readGlobalVars()).length);
    if (extras) banner($("topBanner"), "Per-recipient CSV columns are ignored in BCC mode — everyone gets the same merged text.", "bad");
  }
  describeMode();
}
$("modeEach").addEventListener("change", onModeChange);
$("modeBcc").addEventListener("change", onModeChange);
$("bccTo").addEventListener("input", describeMode);
$("batchSize").addEventListener("input", describeMode);
$("fromEmail").addEventListener("input", describeMode);

$("sendAll").addEventListener("click", () => {
  const jobs = buildJobs(recipients.map((_, i) => i));
  if ($("dryRun").checked) { runSend(recipients.map((_, i) => i)); return; }
  const named = recipients.filter(r => r.data && r.data.first_name).length;
  const what = sendMode() === "bcc"
    ? "BCC MODE\n\n" + recipients.length + " recipients, sent as " + jobs.length + " email"
      + (jobs.length === 1 ? "" : "s") + " of up to " + batchSize() + ".\nNobody sees anybody else's address."
      + (named ? "\n\nWARNING: names will NOT be merged. A shared BCC body cannot say a\ndifferent name to each person, so all " + named
                 + " recipients get the fallback\ngreeting instead of their own name.\nCancel and switch to \"One email each\" if you want names."
                : "")
      + "\n\nSend now?"
    : "ONE EMAIL EACH — NOT BCC\n\n" + recipients.length + " separate emails, one per person."
      + (named ? "\nNames will be merged." : "") + "\n\nSend now?";
  if (!confirm(what)) return;
  runSend(recipients.map((_, i) => i));
});

$("stopSend").addEventListener("click", () => {
  stopRequested = true;
  $("stopSend").disabled = true;
  setTimeout(() => { $("stopSend").disabled = false; }, 1000);
});

$("retryFailed").addEventListener("click", () => {
  const idx = recipients.map((r, i) => r.status === "failed" ? i : -1).filter(i => i >= 0);
  runSend(idx);
});

$("copyFailed").addEventListener("click", () => {
  const text = recipients.filter(r => r.status === "failed").map(r => r.email + "  " + r.detail).join("\n");
  navigator.clipboard.writeText(text).then(
    () => banner($("sendBanner"), "Failed addresses copied.", "good"),
    () => banner($("sendBanner"), "Could not reach the clipboard — select the rows instead.", "bad")
  );
});

$("sendTest").addEventListener("click", async () => {
  const addr = $("testAddr").value.trim();
  if (!EMAIL_RE.test(addr)) { banner($("sendBanner"), "Type a valid address to test with.", "bad"); return; }
  const sample = recipients[0] || { name: "", data: readGlobalVars() };
  const body = Object.assign(baseBody(), {
    to_name: sample.name,
    to_email: addr,
    template_data: sendMode() === "bcc" ? readGlobalVars() : sample.data
  });
  $("sendTest").disabled = true;
  banner($("sendBanner"), "Sending the test…");
  try {
    const res = await postSend(body);
    banner($("sendBanner"), res.dry_run ? "Dry run — nothing sent." : "Test sent to " + addr + ".", "good");
    if (res.dry_run) { $("payloadWrap").hidden = false; $("payloadOut").textContent = JSON.stringify(res.payload, null, 2); }
  } catch (e) {
    banner($("sendBanner"), e.message, "bad");
  } finally {
    $("sendTest").disabled = false;
  }
});

/* ---------- greeting check ---------- */

/* A small subset of Handlebars — enough to resolve {{var}}, {{#if var}}…{{/if}}
   and {{else}}. SMTP2GO does the real rendering; this only previews it. */
function renderTemplate(tpl, data) {
  let out = String(tpl);
  for (let pass = 0; pass < 4; pass++) {
    const before = out;
    out = out.replace(/\{\{#if\s+([\w.]+)\s*\}\}([\s\S]*?)(?:\{\{else\}\}([\s\S]*?))?\{\{\/if\}\}/g,
      (m, key, yes, no) => (data[key] ? yes : (no || "")));
    if (out === before) break;
  }
  out = out.replace(/\{\{\{?\s*([\w.]+)\s*\}?\}\}/g, (m, key) => (data[key] == null ? "" : String(data[key])));
  return out;
}

function visibleText(html) {
  return String(html)
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<(style|script|head)[\s\S]*?<\/\1>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ").replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<").replace(/&gt;/gi, ">")
    .replace(/&#\d+;/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

$("checkGreeting").addEventListener("click", async () => {
  const templateId = currentTemplateId();
  if (!templateId) { banner($("greetBanner"), "Pick a template first.", "bad"); return; }
  if (!recipients.length) { banner($("greetBanner"), "Build the list first.", "bad"); return; }

  $("checkGreeting").disabled = true;
  banner($("greetBanner"), "Fetching the template…");
  try {
    const tpl = await api("/api/template", { template_id: templateId, view_id: currentViewId() });
    const usesVars = [...new Set((tpl.html.match(/\{\{[#\/]?\s*([\w.]+)/g) || [])
      .map(v => v.replace(/[{#\/\s]/g, "")))].filter(v => v !== "if" && v !== "else");

    const wrap = $("greetWrap");
    wrap.textContent = "";
    let missing = 0, unresolved = 0, notFound = 0;
    const bcc = sendMode() === "bcc";

    recipients.slice(0, 25).forEach(rec => {
      // BCC renders once with shared variables only — preview it honestly.
      const data = bcc ? readGlobalVars() : rec.data;
      const rendered = renderTemplate(tpl.html, data);
      const text = visibleText(rendered);

      const needle = String(data.first_name || data.full_name || "");
      let line, miss = false;
      if (needle && text.indexOf(needle) !== -1) {
        const at = text.indexOf(needle);
        line = text.slice(Math.max(0, at - 45), at + needle.length + 45);
      } else {
        miss = true;
        notFound++;
        line = needle
          ? "Their name does not appear anywhere in the rendered email."
          : "No name available for this recipient.";
      }
      if (rendered.indexOf("{{") !== -1) unresolved++;

      const row = document.createElement("div");
      row.className = "greet" + (miss ? " miss" : "");
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = rec.email;
      const body = document.createElement("span");
      body.className = "line";
      if (!miss) {
        const at = line.indexOf(needle);
        body.appendChild(document.createTextNode("…" + line.slice(0, at)));
        const mk = document.createElement("mark");
        mk.textContent = needle;
        body.appendChild(mk);
        body.appendChild(document.createTextNode(line.slice(at + needle.length) + "…"));
      } else {
        body.textContent = line;
      }
      row.append(who, body);
      wrap.appendChild(row);
    });

    // Which variables does the template want that a recipient doesn't have?
    recipients.forEach(rec => {
      const data = bcc ? readGlobalVars() : rec.data;
      if (usesVars.some(v => !(v in data))) missing++;
    });

    wrap.hidden = false;
    const bits = [];
    bits.push("read: " + (tpl.name || "template") + " (" + (tpl.used_id || templateId) + ")");
    bits.push("uses: " + (usesVars.length ? usesVars.join(", ") : "no variables at all"));
    bits.push("showing first " + Math.min(25, recipients.length) + " of " + recipients.length);
    $("greetSummary").textContent = bits.join(" · ");

    const warn = [];
    if (!usesVars.length) {
      warn.push("The template just read (" + (tpl.name || tpl.used_id || templateId)
        + ") contains no {{variables}}, so everyone would get identical text. Either the greeting edit "
        + "was not saved in SMTP2GO, or this is not the template you edited — check the name above, and "
        + "if you have switched API keys, click Load and pick the template again.");
    }
    if (notFound) warn.push(notFound + " of the previewed recipients do not have their name in the rendered email.");
    if (missing) warn.push(missing + " recipients are missing a variable the template asks for.");
    if (unresolved) warn.push(unresolved + " renders still contain unresolved {{ }} markers.");
    if (bcc) warn.push("BCC mode: this preview uses the shared variables only, which is what recipients will get.");
    banner($("greetBanner"), warn.join("  ") || "Every previewed recipient is addressed by name.", warn.length ? "bad" : "good");
  } catch (e) {
    banner($("greetBanner"), e.message, "bad");
  } finally {
    $("checkGreeting").disabled = false;
  }
});

/* ---------- delivery log ---------- */

let sessionEmailIds = [];
let logTimer = null;

const DELIVERED = /^(delivered|sent)$/i;
const BAD = /(bounce|reject|fail|spam|block|defer|error)/i;

function statusClass(status) {
  if (DELIVERED.test(status)) return "sent";
  if (BAD.test(status)) return "failed";
  return "sending";
}

function shortTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (isNaN(d.getTime())) return value;
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

let lastLogRows = [];
let lastFilteredOut = 0;
let lastDropped = [];
let lastUnrecorded = [];

function renderLog(rows) {
  lastLogRows = rows;
  const body = $("logBody");
  body.innerHTML = "";
  rows.forEach(row => {
    const tr = document.createElement("tr");
    const cls = statusClass(row.status);
    if (cls === "failed") tr.className = "is-failed";

    const when = document.createElement("td");
    when.className = "addr";
    when.textContent = shortTime(row.when);

    const who = document.createElement("td");
    who.className = "addr";
    who.textContent = row.recipient || "—";

    const subj = document.createElement("td");
    subj.textContent = row.subject || "—";

    const st = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = "pill " + cls;
    pill.textContent = row.status || "unknown";
    st.appendChild(pill);

    const detail = document.createElement("td");
    detail.className = "vars";
    detail.textContent = row.detail || "";

    tr.append(when, who, subj, st, detail);
    body.appendChild(tr);
  });

  let delivered = 0, failed = 0, other = 0;
  rows.forEach(r => {
    const c = statusClass(r.status);
    if (c === "sent") delivered++; else if (c === "failed") failed++; else other++;
  });
  const parts = [delivered + " delivered"];
  if (failed) parts.push(failed + " bounced or rejected");
  if (other) parts.push(other + " still in flight");
  parts.push(rows.length + " rows");
  parts.push(logScope === "all" ? "every send recorded for this key" : "this session only");
  if (lastFilteredOut) parts.push(lastFilteredOut + " unrelated events filtered out");
  $("logSummary").textContent = parts.join(" · ");
}

let logScope = "session";

async function refreshLog(widen) {
  if (widen) logScope = "all";
  const body = logScope === "all" ? { scope: "all" } : { email_ids: sessionEmailIds };
  if (logScope === "session" && !sessionEmailIds.length) {
    banner($("logBanner"), "Nothing sent yet this session. Use \u201cInclude earlier sends\u201d for past runs.", "bad");
    return;
  }
  $("refreshLog").disabled = true;
  try {
    const res = await api("/api/activity", body);
    lastFilteredOut = res.filtered_out || 0;
    lastDropped = res.dropped_sample || [];
    lastUnrecorded = res.unrecorded || [];
    $("showDropped").hidden = !(lastDropped.length || lastUnrecorded.length);
    if (lastUnrecorded.length) {
      banner($("logBanner"), lastUnrecorded.length + " send"
        + (lastUnrecorded.length === 1 ? " has" : "s have") + " no delivery record yet — "
        + lastUnrecorded.map(u => (u.to[0] || u.id)).slice(0, 4).join(", ")
        + ". SMTP2GO can lag by a minute or two; check again before assuming it failed.", "bad");
    }
    if (res.loose_match) {
      banner($("logBanner"), "SMTP2GO returned events without message IDs this time, so rows were matched "
        + "on recipient instead. Treat the list as approximate.", "bad");
    }
    renderLog(res.events);
    if (!res.events.length) {
      banner($("logBanner"), res.note === "nothing sent through this key yet"
        ? "No sends recorded for this key yet."
        : "Nothing back yet. Delivery records can lag a send by a few seconds.");
    } else {
      banner($("logBanner"), "");
    }
  } catch (e) {
    banner($("logBanner"), e.message, "bad");
    stopAutoLog();
  } finally {
    $("refreshLog").disabled = false;
  }
}

function stopAutoLog() {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
  $("autoLog").checked = false;
}

$("refreshLog").addEventListener("click", () => refreshLog(false));
$("widenLog").addEventListener("click", () => refreshLog(true));
$("autoLog").addEventListener("change", () => {
  if ($("autoLog").checked) {
    refreshLog(false);
    let ticks = 0;
    logTimer = setInterval(() => {
      if (++ticks > 45) {  // roughly 15 minutes, then stop hitting the API
        stopAutoLog();
        banner($("logBanner"), "Stopped auto-checking after 15 minutes. Use Check delivery for a fresh look.");
        return;
      }
      refreshLog(false);
    }, 20000);
  } else {
    stopAutoLog();
  }
});

$("showDropped").addEventListener("click", () => {
  const lines = [];
  if (lastUnrecorded.length) {
    lines.push("Sent by this tool, but SMTP2GO has no activity record yet:");
    lastUnrecorded.forEach(u => lines.push("   " + (u.to.join(", ") || "(unknown)") + "   id " + u.id));
    lines.push("");
  }
  if (lastDropped.length) {
    lines.push("Events returned by SMTP2GO that this tool did not send (first " + lastDropped.length + "):");
    lastDropped.forEach(d => lines.push("   " + (d.recipient || "(no recipient)") + "   " + d.subject));
  }
  const box = $("droppedBox");
  box.textContent = lines.join("\n") || "Nothing was dropped and nothing is missing.";
  box.hidden = false;
});

$("copyLog").addEventListener("click", () => {
  if (!lastLogRows.length) { banner($("logBanner"), "Nothing in the log to copy yet.", "bad"); return; }
  const esc = (v) => '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"';
  const csv = ["when,recipient,subject,status,response"]
    .concat(lastLogRows.map(r => [r.when, r.recipient, r.subject, r.status, r.detail].map(esc).join(",")))
    .join("\n");
  navigator.clipboard.writeText(csv).then(
    () => banner($("logBanner"), "Copied " + lastLogRows.length + " rows as CSV.", "good"),
    () => banner($("logBanner"), "Could not reach the clipboard — select the table instead.", "bad")
  );
});

/* ---------- boot ---------- */

(async function boot() {
  try {
    const state = await refreshState();
    applySettings(state.settings);
  } catch (e) {
    banner($("topBanner"), e.message, "bad");
    addVarRow("", "");
  }
  onModeChange();
  wireAutosave();
  bootDone = true;
  if ($("dryRun").checked) {
    banner($("topBanner"), "Dry run is still switched on from last time — nothing will actually send until you untick it.", "bad");
  }
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #

def main():
    load_config()
    PAGE_URL = "http://%s:%s" % (HOST, PORT)
    globals()["PAGE"] = PAGE.replace("__PORT__", str(PORT))
    try:
        server = Server((HOST, PORT), Handler)
    except OSError as exc:
        print("Could not start on port %s (%s)." % (PORT, exc))
        print("Something else may be using it. Try: SMTP2GO_PORT=8790 python3 %s" % os.path.basename(__file__))
        sys.exit(1)

    print("SMTP Bulk Mailer")
    print("  DCB Technology, LLC (c) 2026")
    print("  open      %s" % PAGE_URL)
    print("  key       %s" % ("loaded" if CONFIG["api_key"] else "not set - paste it in the page"))
    print("  settings  %s" % CONFIG_PATH)
    print("  stop      Ctrl+C")
    try:
        webbrowser.open(PAGE_URL)
    except Exception:  # noqa: BLE001
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
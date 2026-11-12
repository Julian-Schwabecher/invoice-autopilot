"""In-memory live state, safe for the web server's worker threads: the safe-mode switch, the log of what this
app has sent, and the PayPal webhook events it has verified. Nothing here is saved to disk."""
import datetime as dt
import os
import threading
import time
from collections import deque

KEEP = 50
_lock = threading.Lock()


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------- safe mode

_safe = {"on": _env_flag("SAFE_MODE")}


def safe_mode() -> bool:
    with _lock:
        return _safe["on"]


def set_safe_mode(on: bool) -> bool:
    with _lock:
        _safe["on"] = bool(on)
        return _safe["on"]


# ---------------------------------------------------------------- send log

_log: deque = deque(maxlen=KEEP)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log_send(kind: str, invoice_id, number, client_name) -> None:
    """Remember something that really went out. kind is 'send' or 'remind'. A repeated 'send' for the same
    invoice (a double click replaying the first result) is not logged twice."""
    with _lock:
        if kind == "send" and invoice_id and any(
                e["kind"] == "send" and e["invoice_id"] == invoice_id for e in _log):
            return
        _log.append({"time": _now_iso(), "kind": kind, "invoice_id": invoice_id, "number": number,
                     "client_name": client_name or "", "_ts": time.time()})


def get_log() -> list[dict]:
    """Newest first."""
    with _lock:
        return [{k: v for k, v in e.items() if not k.startswith("_")} for e in reversed(_log)]


def reminders_today(invoice_id: str) -> int:
    today = dt.date.today()
    with _lock:
        return sum(1 for e in _log if e["kind"] == "remind" and e["invoice_id"] == invoice_id
                   and dt.date.fromtimestamp(e["_ts"]) == today)


# ---------------------------------------------------------------- verified webhook events

_events: deque = deque(maxlen=KEEP)
_seen_ids: deque = deque(maxlen=200)   # PayPal event ids, so a retried delivery is not stored twice
_seq = {"n": 0}


def add_event(event_id, event_type: str, invoice_id, when) -> dict | None:
    """Store one already-verified event. Returns it, or None when it was a repeat delivery."""
    with _lock:
        if event_id:
            if event_id in _seen_ids:
                return None
            _seen_ids.append(event_id)
        _seq["n"] += 1
        ev = {"seq": _seq["n"], "type": event_type, "invoice_id": invoice_id,
              "time": when if isinstance(when, str) and when else _now_iso()}
        _events.append(ev)
        return dict(ev)


def events_since(since: int) -> tuple[list[dict], int]:
    """(events newer than `since`, last_seq). If `since` is ahead of us (the app restarted), start over."""
    with _lock:
        last = _seq["n"]
        if since > last:
            since = 0
        return [dict(e) for e in _events if e["seq"] > since], last


# A cheap brake: the webhook address is open to the internet, so cap how many unknown callers can make us
# ask PayPal to check a signature.
_verify_times: deque = deque()
VERIFY_PER_MINUTE = 60


def allow_verify() -> bool:
    now = time.monotonic()
    with _lock:
        while _verify_times and now - _verify_times[0] > 60:
            _verify_times.popleft()
        if len(_verify_times) >= VERIFY_PER_MINUTE:
            return False
        _verify_times.append(now)
        return True


def reset() -> None:
    """For tests."""
    with _lock:
        _safe["on"] = _env_flag("SAFE_MODE")
        _log.clear()
        _events.clear()
        _seen_ids.clear()
        _seq["n"] = 0
        _verify_times.clear()

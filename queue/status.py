"""Operations dashboard: GET /status (JSON) and GET / (auto-refreshing page).

An operator tool, not part of the generation path. It only READS what the rest
of the system already writes and never writes anything:

  queue depth, reject rate, failures    -> Postgres (queue_items, prompts)
  worker / provider health, busy, quota -> Redis heartbeats (worker:<name>)
  rolling latency                       -> generation_stats.db (reaper-fed)
  stream uptime                         -> playout log, ffmpeg_started events
  filler pool, disk                     -> filler-pool/ and the tracks volume

Design rules:
  * CHEAP. The whole payload is cached for CACHE_TTL_SEC, so ten open tabs
    cost the same as one and the dashboard can never become load on the
    system it is monitoring.
  * ISOLATED. Each section is computed on its own, so a dead Redis shows an
    error in that section and everything else still renders. A monitor that
    500s when something is down is useless exactly when it is needed.
  * HONEST. Sections whose source cannot be read say so instead of showing a
    plausible zero.
  * NO SECRETS and NO PROMPT TEXT in the output. Counts and states only.

Security: there is no authentication, which is why Compose publishes it on
localhost only by default. Do not expose it to a network you do not trust.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reaper import MiniRedis
from store import connect
from timing import (
    PROVIDERS,
    configured_providers,
    provider_health_from,
    resolve_mode,
)

WORKERS = ("windows", "mac")
HEARTBEAT_TTL_SEC = 20          # workers SETEX worker:<name> with this TTL
ROLLING_WINDOW = 20

CACHE_TTL_SEC = float(os.environ.get("STATUS_CACHE_SEC", "3"))
STATS_DB = os.environ.get("STATS_DB", "/app/queue/generation_stats.db")
PLAYOUT_LOG = Path(os.environ.get("PLAYOUT_LOG", "/app/logs/playout.jsonl"))
FILLER_DIR = Path(os.environ.get("FILLER_DIR", "/app/filler-pool"))
TRACKS_DIR = Path(os.environ.get("TRACKS_DIR", "/srv/radio/tracks"))
# Alert thresholds shown as warnings on the page (operator-tunable).
QUEUE_DEPTH_WARN = int(os.environ.get("STATUS_QUEUE_WARN", "10"))
REJECT_RATE_WARN = float(os.environ.get("STATUS_REJECT_WARN", "0.5"))


def log(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
    entry.update(fields)
    print(json.dumps(entry, default=str), flush=True)


def _json_object(raw: str | None) -> dict:
    """Parse a JSON object, or {} for anything absent or malformed."""
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def heartbeat_view(raw: str | None, ttl: int | None) -> dict:
    """Interpret a worker heartbeat. Pure.

    `ttl` is Redis's remaining TTL, which the worker refreshes to
    HEARTBEAT_TTL_SEC every ~5s, so age = HEARTBEAT_TTL_SEC - ttl. A missing
    key means no heartbeat in that window: the worker is down, which is also
    the manager's definition of unhealthy.
    """
    if not raw:
        return {"state": "down", "busy": False, "age_sec": None, "detail": {}}
    body = _json_object(raw)
    age = max(0, HEARTBEAT_TTL_SEC - ttl) if isinstance(ttl, int) and ttl >= 0 else None
    busy = bool(body.get("busy"))
    return {
        "state": "busy" if busy else "idle",
        "busy": busy,
        "age_sec": age,
        "detail": {k: v for k, v in body.items() if k != "busy"},
    }


def rolling_average(samples: list[float]) -> float | None:
    chunk = [float(s) for s in samples[:ROLLING_WINDOW]]
    return round(sum(chunk) / len(chunk), 2) if chunk else None


def reject_rate(rejected: int, total: int) -> float | None:
    """Fraction of moderated prompts rejected. None when there were none — a
    rate over zero prompts is undefined, not 0%. Pure."""
    return None if total <= 0 else round(rejected / total, 4)


def failure_rate(failed: int, finished: int) -> float | None:
    """Fraction of finished jobs (ready/played/failed) that failed. Pure."""
    return None if finished <= 0 else round(failed / finished, 4)


def uptime_seconds(started_iso: str | None, now: datetime) -> float | None:
    """Seconds since an ISO-8601 timestamp, or None. Never negative. Pure."""
    if not started_iso:
        return None
    try:
        started = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0.0, (now - started).total_seconds())


def _scan_event(lines: list[str], event: str) -> tuple[str | None, int]:
    """(timestamp of the most recent `event`, how many were seen). Lines are
    playout JSONL, oldest first. Pure."""
    quoted = f'"{event}"'
    last, count = None, 0
    for line in lines:
        if quoted not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("event") == event:
            last, count = entry.get("ts"), count + 1
    return last, count


def last_ffmpeg_start(lines: list[str]) -> tuple[str | None, int]:
    """(most recent ffmpeg_started timestamp, number of starts in `lines`).
    Restarts = count - 1. Pure."""
    return _scan_event(lines, "ffmpeg_started")


def last_stream_problem(lines: list[str]) -> str | None:
    """Most recent ffmpeg_error timestamp in `lines`, else None. Pure."""
    return _scan_event(lines, "ffmpeg_error")[0]


def tail_lines(path: Path, max_bytes: int = 400_000) -> list[str]:
    """Last ~max_bytes of a text file as lines (cheap even for a big log)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    # Drop the first line when the file was truncated: it is a partial record.
    return lines[1:] if size > max_bytes and lines else lines


# playout.jsonl rotates at 5 MB (backupCount 3). Read the current file plus the
# newest backup so an ffmpeg_started written before a rotation is still seen.
# ~10 MB per refresh is trivial because responses are cached.
LOG_READ_BYTES = 6_000_000


def read_playout_lines(log_path: Path) -> list[str]:
    """Oldest-first lines from the rotated backup (if any) then the live file."""
    lines: list[str] = []
    for path in (log_path.with_name(log_path.name + ".1"), log_path):
        if path.exists():
            lines += tail_lines(path, max_bytes=LOG_READ_BYTES)
    return lines


# --- now playing -----------------------------------------------------------

# Events that mean "whatever was playing is no longer playing".
_STOP_EVENTS = {"ffmpeg_started", "ffmpeg_error", "ffmpeg_session_end",
                "container_stop", "container_start", "playlist_complete"}
_STALE_SLACK_SEC = 60.0
_GENERATED_SUFFIX = re.compile(r"-(?:[0-9a-f]{8}|\d{8}t\d{6})$")


def title_from_filename(name: str) -> str:
    """Human title from a track file name. Pure.

    Generated tracks are '<prompt-slug>-<jobid8>.<ext>', so the extension and
    the id suffix are dropped; other names (filler) only lose the extension.
    """
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    stem = _GENERATED_SUFFIX.sub("", stem)
    title = re.sub(r"[-_]+", " ", stem).strip()
    return title[:1].upper() + title[1:] if title else (name or "")


def find_now_playing(lines: list[str], now: datetime) -> dict:
    """What is on air, from playout's track_start / track_end events. Pure.

    Walks the log oldest-first keeping the current track. A matching track_end,
    or any event meaning FFmpeg or the container restarted, clears it. If the
    last track_start is older than its own duration plus slack with no
    track_end, playout died mid-track: say the log is stale rather than claim
    the song is still playing.
    """
    current = None
    last = None
    for line in lines:
        if '"track_' not in line and not any(f'"{e}"' in line for e in _STOP_EVENTS):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        event = entry.get("event")
        if event == "track_start" and entry.get("track"):
            current = last = {"file": entry["track"], "started_at": entry.get("ts"),
                              "duration_sec": entry.get("duration_sec")}
        elif event == "track_end":
            if current and entry.get("track") == current["file"]:
                current = None
        elif event in _STOP_EVENTS:
            current = None

    if last is None:
        return {"state": "unknown", "note": "no track has been logged yet"}
    base = {"file": last["file"], "title": title_from_filename(last["file"]),
            "duration_sec": last["duration_sec"]}
    if current is None:
        return {"state": "between_tracks", **base, "note": "last track finished"}

    elapsed = uptime_seconds(current["started_at"], now)
    duration = current["duration_sec"]
    if (elapsed is not None and isinstance(duration, (int, float))
            and elapsed > duration + _STALE_SLACK_SEC):
        return {"state": "unknown", **base, "elapsed_sec": round(elapsed, 1),
                "note": "last logged track should have ended already; the "
                        "playout log looks stale"}
    return {"state": "playing", **base, "started_at": current["started_at"],
            "elapsed_sec": None if elapsed is None
            else round(min(elapsed, duration or elapsed), 1)}


def lookup_track_source(store, filename: str) -> str | None:
    """Which worker or provider generated this file, from the tracks table, or
    None (filler tracks are not in it). Matches on the file's base name."""
    like = "%" + filename.replace("\\", "\\\\").replace("%", "\\%")
    rows = store._exec(
        "SELECT file_path, source FROM tracks WHERE file_path LIKE ? LIMIT 20",
        (like,),
    ).fetchall()
    for row in rows:
        if os.path.basename(row["file_path"]) == filename:
            return row["source"]
    return None


def overall_level(sections: dict) -> str:
    """'ok' | 'warn' | 'down' for the banner. Pure: reads the section dicts."""
    if sections.get("stream", {}).get("state") == "down":
        return "down"
    if any("error" in s for s in sections.values() if isinstance(s, dict)):
        return "warn"
    if sections.get("queue", {}).get("warn") or sections.get("moderation", {}).get("warn"):
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# Section collectors. Each returns a plain dict; safe_section() turns an
# exception into {"error": ...} so one dead source never fails the page.
# ---------------------------------------------------------------------------

def safe_section(fn, *args) -> dict:
    try:
        return fn(*args)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def collect_queue(store) -> dict:
    counts = {}
    for row in store._exec(
        "SELECT status, COUNT(*) AS n FROM queue_items GROUP BY status"
    ).fetchall():
        counts[row["status"]] = int(row["n"])
    depth = store.open_depth()  # queued + generating + ready: the buffer
    return {
        "depth": depth,
        "by_status": counts,
        "warn": depth >= QUEUE_DEPTH_WARN,
        "warn_threshold": QUEUE_DEPTH_WARN,
    }


def collect_moderation(store, now: datetime) -> dict:
    cutoff = (now - timedelta(hours=1)).isoformat()
    row = store._exec(
        "SELECT"
        " SUM(CASE WHEN moderation_status = 'rejected' THEN 1 ELSE 0 END) AS rej,"
        " COUNT(*) AS total FROM prompts WHERE created_at >= ?",
        (cutoff,),
    ).fetchone()
    rejected, total = int(row["rej"] or 0), int(row["total"] or 0)
    rate = reject_rate(rejected, total)
    return {
        "window": "last 60 minutes",
        "rejected": rejected,
        "moderated": total,
        "rate": rate,
        # A spike can mean abuse, but one rejection out of two prompts is noise.
        "warn": rate is not None and total >= 5 and rate >= REJECT_RATE_WARN,
        "note": "counts prompts that reached moderation; rate-limited and "
                "malformed submissions are rejected earlier and not stored",
    }


def collect_failures(store, now: datetime) -> dict:
    """Per-source failure rate from the queue table. generation_stats only
    receives successes, so the queue table is the only honest source here."""
    cutoff = (now - timedelta(hours=24)).isoformat()
    rows = store._exec(
        "SELECT source_assigned AS src, status, COUNT(*) AS n FROM queue_items"
        " WHERE source_assigned IS NOT NULL AND created_at >= ?"
        " AND status IN ('ready','playing','played','failed') GROUP BY src, status",
        (cutoff,),
    ).fetchall()
    per: dict = {}
    for row in rows:
        entry = per.setdefault(row["src"], {"failed": 0, "finished": 0})
        entry["finished"] += int(row["n"])
        if row["status"] == "failed":
            entry["failed"] += int(row["n"])
    return {
        "window": "last 24 hours",
        "by_source": {
            src: {**entry, "rate": failure_rate(entry["failed"], entry["finished"])}
            for src, entry in sorted(per.items())
        },
    }


def read_latency(path: str) -> dict:
    """Newest ROLLING_WINDOW generation times per worker/provider."""
    out: dict = {}
    if not path or not os.path.exists(path):
        return out
    conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT DISTINCT worker FROM generation_stats")]
        for name in names:
            rows = conn.execute(
                "SELECT generation_time_sec FROM generation_stats WHERE worker = ?"
                " ORDER BY id DESC LIMIT ?", (name, ROLLING_WINDOW)).fetchall()
            out[name] = {"avg_sec": rolling_average([r[0] for r in rows]),
                         "samples": len(rows),
                         "last_sec": round(float(rows[0][0]), 2) if rows else None}
    finally:
        conn.close()
    return out


def collect_workers(r, latency: dict, failures: dict) -> dict:
    out = {}
    for name in WORKERS:
        view = heartbeat_view(r.command("GET", f"worker:{name}"),
                              r.command("TTL", f"worker:{name}"))
        lat = latency.get(name, {})
        fail = failures.get("by_source", {}).get(name, {})
        out[name] = {
            **view,
            "pending_jobs": int(r.command("LLEN", f"jobs:pending:{name}") or 0),
            "avg_latency_sec": lat.get("avg_sec"),
            "latency_samples": lat.get("samples", 0),
            "failure_rate_24h": fail.get("rate"),
            # Reported by the worker itself: GPU temp (Windows), thermal (Mac).
            "temperature_c": view["detail"].get("temperature_c"),
            "thermal": view["detail"].get("thermal"),
        }
    return out


def collect_providers(r, mode: str, configured: list[str], latency: dict,
                      failures: dict) -> dict:
    heartbeats = {}
    for name in PROVIDERS:
        raw = r.command("GET", f"worker:{name}")
        heartbeats[name] = None if not raw else _json_object(raw)
    health = provider_health_from(mode, heartbeats, configured)
    out = {}
    for name in PROVIDERS:
        if name not in configured:
            out[name] = {"configured": False}
            continue
        lat = latency.get(name, {})
        fail = failures.get("by_source", {}).get(name, {})
        hb = heartbeats[name]
        out[name] = {
            "configured": True,
            "alive": hb is not None,
            # routing_ok is what the manager uses; it is False in local_only by
            # design even when the provider itself is alive.
            "routing_ok": health[name]["ok"],
            "quota_remaining": (hb or {}).get("quota_remaining"),
            "avg_latency_sec": lat.get("avg_sec"),
            "latency_samples": lat.get("samples", 0),
            "failure_rate_24h": fail.get("rate"),
        }
    return out


def collect_stream(now: datetime, log_path: Path | None = None) -> dict:
    # Resolved HERE, not as a default argument: binding PLAYOUT_LOG at import
    # time made overriding it (by env or test) silently ignored, and the
    # section always reported "log not found". That was a real bug.
    log_path = log_path or PLAYOUT_LOG
    if not log_path.exists():
        return {"state": "unknown", "error": f"playout log not found at {log_path}"}
    lines = read_playout_lines(log_path)
    started, count = last_ffmpeg_start(lines)
    if started is None:
        return {"state": "unknown",
                "error": "no ffmpeg_started event in the recent playout log "
                         "(is playout running the build that logs it?)"}
    return {
        "state": "up",
        "uptime_sec": uptime_seconds(started, now),
        "last_ffmpeg_start": started,
        "ffmpeg_starts_in_log": count,
        "restarts_in_log": max(0, count - 1),
        "last_ffmpeg_error": last_stream_problem(lines),
        "note": "uptime = time since FFmpeg was last started; it does not prove "
                "YouTube is receiving data - check Studio for that",
    }


def collect_now_playing(store, now: datetime, log_path: Path | None = None) -> dict:
    log_path = log_path or PLAYOUT_LOG  # resolved at call time; see collect_stream
    if not log_path.exists():
        return {"state": "unknown", "note": f"playout log not found at {log_path}"}
    info = find_now_playing(read_playout_lines(log_path), now)
    if info.get("file"):
        source = lookup_track_source(store, info["file"])
        info["kind"] = "generated" if source else "filler"
        info["source"] = source
    return info


def collect_filler(filler_dir: Path = FILLER_DIR, tracks_dir: Path = TRACKS_DIR) -> dict:
    audio = filler_dir / "audio"
    files = [p for p in audio.iterdir()
             if p.is_file() and not p.name.startswith(".")] if audio.is_dir() else []
    out = {
        "filler_tracks": len(files),
        "filler_bytes": sum(p.stat().st_size for p in files),
    }
    if tracks_dir.is_dir():
        usage = shutil.disk_usage(tracks_dir)
        out["tracks_disk_free_gb"] = round(usage.free / 1e9, 1)
        out["tracks_disk_used_pct"] = round(100 * usage.used / usage.total, 1)
    return out


# ---------------------------------------------------------------------------
# Assembly + cache
# ---------------------------------------------------------------------------

class Collector:
    def __init__(self, store, redis_factory, env: dict | None = None):
        self.store = store
        self._redis_factory = redis_factory
        self._redis = None
        self.env = env if env is not None else os.environ
        self._cache: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    def _r(self):
        if self._redis is None:
            self._redis = self._redis_factory()
        return self._redis

    def snapshot(self) -> dict:
        # Cached so the dashboard can never become load: one recompute at a
        # time, and every other caller gets the cached payload.
        with self._lock:
            now_mono = time.monotonic()
            if self._cache and now_mono - self._cache[0] < CACHE_TTL_SEC:
                return self._cache[1]
            data = self._build()
            self._cache = (now_mono, data)
            return data

    def _redis_section(self, fn, *args) -> dict:
        try:
            return fn(self._r(), *args)
        except Exception as exc:
            self._redis = None  # reconnect next time
            return {"error": f"redis: {type(exc).__name__}: {str(exc)[:120]}"}

    def _build(self) -> dict:
        now = datetime.now(timezone.utc)
        configured = configured_providers(self.env)
        mode = resolve_mode(self.env.get("GENERATION_MODE"), configured)

        failures = safe_section(collect_failures, self.store, now)
        healthy_failures = failures if "error" not in failures else {}
        try:
            latency = read_latency(STATS_DB)
            latency_err = None
        except Exception as exc:
            latency, latency_err = {}, f"{type(exc).__name__}: {str(exc)[:120]}"

        sections = {
            "mode": {
                "generation_mode": mode,
                "configured_providers": configured,
                "local_min_duration_sec":
                    self.env.get("LOCAL_MIN_DURATION_SEC") or "90 (default)",
                "provider_duration_sec":
                    self.env.get("PROVIDER_DURATION_SEC") or "180 (default)",
            },
            "queue": safe_section(collect_queue, self.store),
            "workers": self._redis_section(collect_workers, latency, healthy_failures),
            "providers": self._redis_section(collect_providers, mode, configured,
                                             latency, healthy_failures),
            "now_playing": safe_section(collect_now_playing, self.store, now),
            "stream": safe_section(collect_stream, now),
            "moderation": safe_section(collect_moderation, self.store, now),
            "failures": failures,
            "pool": safe_section(collect_filler),
        }
        if latency_err:
            sections["latency_error"] = {"error": latency_err}
        return {"generated_at": now.isoformat(),
                "level": overall_level(sections), **sections}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Radio status</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font:14px/1.45 system-ui,sans-serif;margin:1.2rem;max-width:62rem;color:#1b1b1b}
 h1{font-size:1.2rem;margin:0 0 .2rem} h2{font-size:.95rem;margin:1.2rem 0 .3rem;
   border-bottom:1px solid #ccc;padding-bottom:.15rem}
 table{border-collapse:collapse;width:100%} td,th{padding:.25rem .5rem;text-align:left;
   border-bottom:1px solid #eee;vertical-align:top} th{font-weight:600;width:14rem}
 .pill{display:inline-block;padding:.05rem .5rem;border-radius:.7rem;font-weight:600;color:#fff}
 .ok{background:#1a7f37}.warn{background:#9a6700}.down{background:#cf222e}.idle{background:#1a7f37}
 .busy{background:#0969da}.unknown{background:#6e7781}
 #banner{padding:.5rem .8rem;border-radius:.4rem;color:#fff;font-weight:600}
 .muted{color:#666;font-size:.85em} .err{color:#cf222e}
</style></head><body>
<h1>Radio status</h1>
<div id="banner" class="unknown">loading…</div>
<p class="muted" id="meta"></p>
<div id="out"></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pill=(t,c)=>'<span class="pill '+c+'">'+esc(t)+'</span>';
const dur=s=>{if(s==null)return"–";s=Math.round(s);const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return(d?d+"d ":"")+(h||d?h+"h ":"")+m+"m "+(s%60)+"s"};
const clock=s=>{if(s==null)return"–";s=Math.round(s);return Math.floor(s/60)+":"+String(s%60).padStart(2,"0")};
const pct=x=>x==null?"–":(x*100).toFixed(1)+"%";
const num=(x,u)=>x==null?"–":x+(u||"");
const row=(k,v)=>'<tr><th>'+esc(k)+'</th><td>'+v+'</td></tr>';
const sect=(t,body)=>'<h2>'+esc(t)+'</h2>'+(body.error?'<p class="err">'+esc(body.error)+'</p>':'<table>'+body+'</table>');
function render(d){
  const b=$("banner");b.className=d.level;
  b.textContent={ok:"All systems nominal",warn:"Attention needed",down:"Stream is down"}[d.level]||d.level;
  $("meta").textContent="updated "+d.generated_at+" · refreshes every 5s";
  let h="";
  const np=d.now_playing;
  if(np.error){h+=sect("Now playing",np)}
  else if(np.state==="playing"){h+=sect("Now playing",row("song",'<strong style="font-size:1.1em">'+esc(np.title)+'</strong>')
    +row("source",esc(np.kind==="generated"?"generated by "+(np.source||"?"):"filler pool"))
    +row("progress",clock(np.elapsed_sec)+" / "+clock(np.duration_sec))
    +row("file",'<span class="muted">'+esc(np.file)+'</span>'))}
  else{h+=sect("Now playing",row("state",pill(np.state==="between_tracks"?"between tracks":"unknown",np.state==="between_tracks"?"idle":"unknown"))
    +(np.title?row("last track",esc(np.title)):"")+'<tr><td colspan=2 class="muted">'+esc(np.note||"")+'</td></tr>')}
  h+=sect("Mode",row("generation mode",esc(d.mode.generation_mode))+row("configured providers",esc(d.mode.configured_providers.join(", ")||"none"))
    +row("local min length",esc(d.mode.local_min_duration_sec))+row("provider length",esc(d.mode.provider_duration_sec)));
  const q=d.queue; h+=q.error?sect("Queue",q):sect("Queue",row("depth (queued+generating+ready)",
    num(q.depth)+(q.warn?" "+pill("≥ "+q.warn_threshold,"warn"):""))
    +row("by status",esc(Object.entries(q.by_status).map(([k,v])=>k+": "+v).join(" · ")||"empty")));
  const s=d.stream; h+=s.state==="up"?sect("Stream",row("state",pill("up","ok"))+row("uptime",dur(s.uptime_sec))
    +row("FFmpeg restarts (in log)",num(s.restarts_in_log))+row("last FFmpeg error",esc(s.last_ffmpeg_error||"none in log"))
    +'<tr><td colspan=2 class="muted">'+esc(s.note)+'</td></tr>'):sect("Stream",{error:s.error||"unknown"});
  if(d.workers.error){h+=sect("Local workers",d.workers)}else{
   h+=sect("Local workers",Object.entries(d.workers).map(([n,w])=>
    row(n,pill(w.state,w.state==="down"?"down":w.state)+" "+(w.age_sec!=null?'<span class="muted">heartbeat '+w.age_sec+'s ago</span>':'<span class="muted">no heartbeat</span>')
     +'<br>avg latency '+num(w.avg_latency_sec,"s")+' ('+w.latency_samples+' samples) · failures 24h '+pct(w.failure_rate_24h)
     +' · pending jobs '+w.pending_jobs
     +(w.temperature_c!=null?' · GPU '+w.temperature_c+'°C':'')+(w.thermal?' · thermal '+esc(w.thermal):''))).join(""))}
  if(d.providers.error){h+=sect("Providers",d.providers)}else{
   h+=sect("Paid providers",Object.entries(d.providers).map(([n,p])=>p.configured===false?row(n,'<span class="muted">not configured</span>'):
    row(n,pill(p.alive?(p.routing_ok?"routing":"alive (not routed in this mode)"):"down",p.alive?(p.routing_ok?"ok":"unknown"):"down")
     +'<br>credits '+(p.quota_remaining==null?"unknown":p.quota_remaining)+' · avg latency '+num(p.avg_latency_sec,"s")
     +' · failures 24h '+pct(p.failure_rate_24h))).join(""))}
  const m=d.moderation; h+=m.error?sect("Moderation",m):sect("Moderation (last hour)",row("rejected / moderated",m.rejected+" / "+m.moderated)
    +row("reject rate",pct(m.rate)+(m.warn?" "+pill("spike","warn"):""))+'<tr><td colspan=2 class="muted">'+esc(m.note)+'</td></tr>');
  const p=d.pool; h+=p.error?sect("Filler pool & disk",p):sect("Filler pool & disk",row("filler tracks",num(p.filler_tracks))
    +row("tracks disk free",num(p.tracks_disk_free_gb," GB")+(p.tracks_disk_used_pct!=null?" ("+p.tracks_disk_used_pct+"% used)":"")));
  $("out").innerHTML=h;
}
async function tick(){
  try{const r=await fetch("/status",{cache:"no-store"});render(await r.json())}
  catch(e){const b=$("banner");b.className="down";b.textContent="Cannot reach the status service"}
}
tick();setInterval(tick,5000);
</script></body></html>
"""


def make_handler(collector: Collector):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, ctype: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/status":
                try:
                    body = json.dumps(collector.snapshot(), default=str).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                except Exception as exc:  # last resort: still answer
                    err = json.dumps({"level": "down", "error": str(exc)[:200]}).encode()
                    self._send(500, "application/json; charset=utf-8", err)
            elif path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            else:
                self._send(404, "application/json", b'{"error":"Not found."}')

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    password = os.environ.get("REDIS_PASSWORD", "")
    if not url:
        print("DATABASE_URL is not set", flush=True)
        return 2
    port = int(os.environ.get("STATUS_PORT", "8081"))
    collector = Collector(connect(url), lambda: MiniRedis(redis_url, password))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(collector))
    log("status_start", port=port, cache_sec=CACHE_TTL_SEC)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

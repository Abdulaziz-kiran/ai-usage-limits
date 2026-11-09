#!/usr/bin/env python3
"""Collect AI limits from files and agy; fast output modes only read files."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
AGY = shutil.which("agy") or next(
    (path for path in ("/opt/homebrew/bin/agy", "/usr/local/bin/agy")
     if os.path.isfile(path) and os.access(path, os.X_OK)), None)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DATA = ROOT / "data"
CACHE = DATA / "limits.json"
CLAUDE_CACHE = DATA / "claude-usage.json"
MODELS_FILE = DATA / "known-models.json"
SETTINGS = DATA / "settings.json"
HELLO_STATE = DATA / "hello.json"
HELLO_LOG = DATA / "hello-log.jsonl"
LOCK = DATA / "refreshing"
LOW_THRESHOLD, RESET_THRESHOLD, CACHE_MINUTES = 10, 30, 30
SOURCES = (("claude", "Claude"), ("codex", "Codex"), ("agy", "Antigravity"))
# Order determines menu title color priority.
WARNING_COLORS = {"RESETS IN 12H": "red", "RESETS IN 24H": "orange",
                 "NEW MODEL": "#0A84FF", "LOW": ""}
_lock = None


def number(value, minimum=0, maximum=253402214400):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid numeric field")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Numeric field out of range")
    return value


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid text field")
    return value.replace("\n", " ").replace("\r", " ").replace("|", "/")


def iso_epoch(value):
    dt = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Missing timezone")
    return number(dt.timestamp())


def format_date(epoch):
    local = datetime.fromtimestamp(number(epoch))
    return (f"{MONTHS[local.month - 1]} {local.day:02d} "
            f"{local.hour:02d}:{local.minute:02d}")


def format_duration(seconds):
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    days, hours_left = divmod(hours, 24)
    return (f"{days}d {hours_left}h" if days else
            f"{hours}h {minutes}m" if hours else f"{minutes}m")


def read_json(path):
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".limits-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe(operation, *args):
    try:
        return operation(*args)
    except Exception as error:
        return {"error": f"{getattr(operation, '__name__', 'source')}: {type(error).__name__}"}


def limit(source, name, window, remaining, reset, timestamp):
    return dict(source=text(source), name=text(name), window=text(window),
                remaining=round(number(remaining, 0, 100)), reset=number(reset),
                data_time=number(timestamp))


def run_agy(args, timeout):
    hello = (isinstance(args, list) and len(args) == 8
             and args[:3] == ["-p", "hello", "--model"]
             and isinstance(args[3], str) and bool(args[3])
             and args[4:] == ["--output-format", "json", "--print-timeout", "60s"])
    if args != ["models"] and args != [
            "-p", "/usage", "--output-format", "json", "--print-timeout", "60s"] and not hello:
        raise ValueError("Only agy usage, model listing, and the fixed hello prompt are allowed")
    if AGY is None:
        raise FileNotFoundError("agy is not installed")
    process = subprocess.Popen([AGY] + args, cwd=str(HOME), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.stdout.close()
            process.stderr.close()
        raise TimeoutError("agy timed out") from None
    if process.returncode:
        raise RuntimeError(f"agy exit code {process.returncode}")
    return output


def agy_usage():
    if AGY is None:
        return {}
    raw = json.loads(run_agy(
        ["-p", "/usage", "--output-format", "json", "--print-timeout", "60s"], 40))
    timestamp, items = time.time(), []
    for group in raw["command"]["data"]["groups"]:
        for bucket in group["buckets"]:
            window = {"weekly": "weekly", "5h": "5-hour"}[bucket["window"]]
            items.append(limit("Antigravity", group["name"], window,
                               number(bucket["remaining_fraction"], 0, 1) * 100,
                               iso_epoch(bucket["reset_time"]), timestamp))
    if not items:
        raise ValueError("No Antigravity limits found")
    return {"limits": items, "data_time": timestamp}


def tail(path, size=512 * 1024):
    with path.open("rb") as file:
        file.seek(0, 2)
        start = max(0, file.tell() - size)
        file.seek(start)
        raw = file.read(size)
    # The truncated first line may contain partial UTF-8 or JSON.
    return (raw.split(b"\n", 1)[-1] if start else raw).decode(
        "utf-8", errors="replace").splitlines()


def codex_record(obj, timestamp):
    if isinstance(obj, dict):
        if "timestamp" in obj:
            timestamp = iso_epoch(obj["timestamp"])
        rates = obj.get("rate_limits")
        if isinstance(rates, dict):
            items = []
            for name in ("primary", "secondary"):
                try:
                    record = rates[name]
                    minutes = number(record["window_minutes"], 1)
                    window = {300: "5-hour", 10080: "weekly"}.get(minutes, f"{minutes:g} minutes")
                    reset = number(record["resets_at"])
                    remaining = 100 - number(record["used_percent"], 0, 100)
                    items.append(limit("Codex", "Codex", window,
                                       100 if reset <= time.time() else remaining,
                                       reset, timestamp))
                except (KeyError, TypeError, ValueError):
                    continue
            if items:
                return {"limits": items, "data_time": timestamp}
        for child in obj.values():
            found = codex_record(child, timestamp)
            if found:
                return found
    elif isinstance(obj, list):
        for child in obj:
            found = codex_record(child, timestamp)
            if found:
                return found
    return None


def codex_home():
    return Path(os.environ.get("CODEX_HOME", str(HOME / ".codex"))).expanduser()


def codex_usage():
    sessions = codex_home() / "sessions"
    if not sessions.exists():
        return {}
    deadline, files, candidates = time.monotonic() + 1, [], []
    for path in sessions.rglob("*.jsonl"):
        if time.monotonic() >= deadline:
            break
        try:
            files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for timestamp, path in sorted(files, reverse=True)[:5]:
        if time.monotonic() >= deadline:
            break
        try:
            for line in reversed(tail(path)):
                if time.monotonic() >= deadline:
                    break
                if '"rate_limits"' not in line:
                    continue
                try:
                    found = codex_record(json.loads(line), timestamp)
                except (ValueError, TypeError, KeyError, RecursionError):
                    continue
                if found:
                    candidates.append(found)
                    break
        except (OSError, UnicodeError, ValueError, RecursionError):
            continue
    if not candidates:
        raise ValueError("No Codex limit record found")
    return max(candidates, key=lambda record: record["data_time"])


def claude_windows(windows):
    if not isinstance(windows, list) or not windows:
        raise ValueError("Claude windows are empty or invalid")
    result = []
    for window in windows:
        name = text(window["label"])
        percent = number(window["percentUsed"], 0, 100)
        if window["resetsAt"] is None:
            continue
        iso_epoch(window["resetsAt"])
        result.append(dict(label=name, percentUsed=percent, resetsAt=window["resetsAt"]))
    if not result:
        raise ValueError("No valid Claude windows")
    return result


def claude_save(raw):
    data = json.loads(raw)
    windows = claude_windows(data.get("plan", data)["windows"])
    record = {"saved_at": time.time(), "windows": windows}
    atomic_json(CLAUDE_CACHE, record)
    return record


def claude_usage():
    if not CLAUDE_CACHE.exists():
        return {}
    data = read_json(CLAUDE_CACHE)
    timestamp = number(data["saved_at"])
    items = [limit("Claude", x["label"],
                   "weekly" if "weekly" in x["label"].lower() else "5-hour",
                   100 - x["percentUsed"], iso_epoch(x["resetsAt"]), timestamp)
             for x in claude_windows(data["windows"])]
    return {"limits": items, "saved_at": timestamp, "data_time": timestamp}


def agy_models():
    if AGY is None:
        return {}
    items = {}
    for line in run_agy(["models"], 30).splitlines():
        if "\t" in line and not line.startswith("Fetching"):
            identifier, name = line.split("\t", 1)
            items["agy:" + text(identifier)] = text(name)
    if not items:
        raise ValueError("agy model list is empty")
    return items


def codex_models():
    items = {}
    for name in ("models_cache.json", "models_cache.codex_work_desktop.json"):
        path = codex_home() / name
        if path.exists():
            for model in read_json(path)["models"]:
                identifier = text(model["slug"])
                items["codex:" + identifier] = text(model.get("display_name") or identifier)
    if not items:
        if not (codex_home() / "sessions").exists():
            return {}
        raise ValueError("Codex model list is empty")
    return items


def models():
    result = {"models": {}, "successful": [], "errors": {}}
    for source, operation in (("agy", agy_models), ("codex", codex_models)):
        data = safe(operation)
        if "error" in data:
            result["errors"][source] = data["error"]
        elif data:
            result["successful"].append(source)
            result["models"].update(data)
    return result


def model_dictionary(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid model dictionary")
    return {text(k): text(v) for k, v in data.items()}


def model_status(data, accept_models=False):
    old = model_dictionary(read_json(MODELS_FILE)["models"]) if MODELS_FILE.exists() else {}
    if accept_models and not data["successful"]:
        raise ValueError("No successful model source")
    result = {"new": {}, "removed": {}, "baseline_created": {}}
    baseline = dict(old)
    for source in data["successful"]:
        before = {k: v for k, v in old.items() if k.startswith(source + ":")}
        now = {k: v for k, v in data["models"].items() if k.startswith(source + ":")}
        if not now:
            continue
        if before:
            result["new"].update({k: v for k, v in now.items() if k not in before})
            result["removed"].update({k: v for k, v in before.items() if k not in now})
        else:
            result["baseline_created"][source] = len(now)
        if accept_models or not before:
            baseline = {k: v for k, v in baseline.items() if not k.startswith(source + ":")}
            baseline.update(now)
    if accept_models or result["baseline_created"]:
        atomic_json(MODELS_FILE, {"updated_at": datetime.now().astimezone().isoformat(),
                                 "models": baseline})
    return result


def acquire_lock():
    global _lock
    if _lock is not None:
        return False
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                with LOCK.open("r") as file:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    status = os.fstat(file.fileno())
                    if (time.time() - status.st_mtime > 180 and
                            LOCK.stat().st_ino == status.st_ino):
                        LOCK.unlink()
                        continue
            except (OSError, ValueError):
                pass
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, str(os.getpid()).encode())
            _lock = fd, os.fstat(fd).st_ino
            return True
        except BaseException:
            os.close(fd)
            raise
    return False


def release_lock():
    global _lock
    if _lock is None:
        return
    fd, inode = _lock
    try:
        if LOCK.stat().st_ino == inode and LOCK.read_text() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass
    finally:
        os.close(fd)
        _lock = None


def auto_hello_enabled():
    try:
        settings = read_json(SETTINGS)
        return isinstance(settings, dict) and settings.get("auto_hello") is True
    except (OSError, ValueError, UnicodeError, RecursionError):
        return False


def weekly_not_started(x, previous):
    try:
        if not all(y["source"] == "Antigravity" and y["window"] == "weekly" and
                   number(y["remaining"], 0, 100) == 100 for y in (x, previous)):
            return False
        if x["name"] != previous["name"]:
            return False
        interval = number(x["data_time"]) - number(previous["data_time"])
        shift = number(x["reset"]) - number(previous["reset"])
        # A fixed reset is not a sliding counter, even within the tolerance.
        return interval >= 60 and shift > 0 and abs(shift - interval) < 300
    except (KeyError, TypeError, ValueError):
        return False


def hello_model(group, data):
    preferences = {"Gemini Models": ("gemini-3.8-flash-low", ("flash",)),
                   "Gemini 4 Argon": ("gemini-4-argon-low", ("argon",)),
                   "Claude and GPT models": ("gpt-oss-120b-medium", ("gpt-oss", "sonnet"))}
    if group not in preferences or "agy" not in data["successful"]:
        return None
    ids = [k[4:] for k in data["models"] if k.startswith("agy:")]
    preferred, keywords = preferences[group]
    if preferred in ids:
        return preferred
    for keyword in keywords:
        candidates = [k for k in ids if keyword in k.lower()]
        if candidates:
            return min(candidates, key=lambda k: ("low" not in k.lower(), k))
    return None


def send_hello(limits, model_data, previous_limits):
    if not auto_hello_enabled():
        return
    state = safe(read_json, HELLO_STATE)
    if not isinstance(state, dict):
        state = {}
    state = {k: v for k, v in state.items()
             if isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v)}
    for x in limits:
        group, timestamp = x["name"], time.time()
        if (not any(weekly_not_started(x, previous) for previous in previous_limits)
                or timestamp - state.get(group, 0) < 21600):
            continue
        model = hello_model(group, model_data)
        result = "no model"
        if model:
            try:
                run_agy(["-p", "hello", "--model", model,
                         "--output-format", "json", "--print-timeout", "60s"], 90)
                result = "sent"
                state[group] = time.time()
                atomic_json(HELLO_STATE, state)
            except Exception as error:
                result = f"error: {type(error).__name__}"
        HELLO_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HELLO_LOG.open("a", encoding="utf-8") as file:
            file.write(json.dumps(dict(time=timestamp, group=group, model=model, result=result),
                                  ensure_ascii=False) + "\n")


def last_hello():
    if not auto_hello_enabled():
        return None
    try:
        for line in reversed(tail(HELLO_LOG)):
            try:
                x = json.loads(line)
                number(x["time"])
                text(x["group"])
                text(x["result"])
                if x["model"] is not None:
                    text(x["model"])
                return dict(time=x["time"], group=text(x["group"]),
                            model=text(x["model"]) if x["model"] is not None else None,
                            result=text(x["result"]))
            except (ValueError, TypeError, KeyError):
                continue
    except (OSError, UnicodeError):
        pass
    return None


def cached_source(section, source):
    if not isinstance(section, dict):
        raise ValueError("Invalid cached source schema")
    result = {}
    for field in ("error", "last_error", "note"):
        if field in section:
            result[field] = text(section[field])
    for field in ("saved_at", "data_time"):
        if field in section:
            result[field] = number(section[field])
    if "limits" in section:
        if not isinstance(section["limits"], list):
            raise ValueError("Invalid limits list")
        result["limits"] = [limit(source, x["name"], x["window"], x["remaining"],
                                   x["reset"], x["data_time"]) for x in section["limits"]]
    return result


def cached_models(data):
    status = data.get("models_status", {})
    result = {k: model_dictionary(status.get(k, {})) for k in ("new", "removed")}
    if "error" in status:
        result["error"] = text(status["error"])
    baseline = status.get("baseline_created", {})
    result["baseline_created"] = {text(k): number(v) for k, v in baseline.items()}
    return result


def read_cache():
    try:
        raw = read_json(CACHE)
        timestamp = number(raw["timestamp"])
        if raw["version"] != 2:
            return None
        data = {"version": 2, "timestamp": timestamp}
        for key, source in SOURCES:
            data[key] = safe(cached_source, raw.get(key, {}), source)
        data["models_status"] = safe(cached_models, raw)
        models_raw = raw.get("model_sources", {})
        try:
            data["model_sources"] = {
                "models": model_dictionary(models_raw.get("models", {})),
                "errors": model_dictionary(models_raw.get("errors", {}))}
        except (ValueError, TypeError, AttributeError):
            data["model_sources"] = {"errors": {"cache": "Invalid model schema"}}
        return data
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


def keep_last_good(new, old):
    if "error" in new and old.get("limits"):
        return dict(old, last_error=new["error"])
    return new


def collect():
    old = read_cache() or {}
    if not acquire_lock():
        return dict(old, **{"note": "refresh already in progress"})
    try:
        data = {"version": 2, "timestamp": time.time()}
        for key, operation in (("agy", agy_usage), ("codex", codex_usage),
                               ("claude", claude_usage)):
            result = safe(operation)
            data[key] = keep_last_good(result, old.get(key, {}))
            if key == "agy":
                fresh_agy = "error" not in result
        data["model_sources"] = models()
        data["models_status"] = safe(model_status, data["model_sources"])
        if auto_hello_enabled() and fresh_agy:
            previous_agy = old.get("agy", {})
            previous_limits = (previous_agy.get("limits", [])
                               if not any(k in previous_agy for k in ("error", "last_error"))
                               else [])
            safe(send_hello, data["agy"].get("limits", []),
                 data["model_sources"], previous_limits)
        data["timestamp"] = time.time()
        atomic_json(CACHE, data)
        return data
    finally:
        release_lock()


def all_limits(data):
    return [x for k, _ in SOURCES for x in data.get(k, {}).get("limits", [])]


def warnings(data, now=None):
    now = time.time() if now is None else now
    result = []
    for x in all_limits(data):
        age = max(0, now - x["data_time"])
        if x["reset"] <= now or (x["source"] == "Claude" and age > 21600):
            continue
        name = x["source"] + (" " + x["name"] if x["name"] != x["source"] else "")
        remaining, duration = x["remaining"], format_duration(x["reset"] - now)
        suffix = f" (measured {format_duration(age)} ago)" if age > 7200 else ""
        if remaining < LOW_THRESHOLD:
            result.append(f"LOW: {name} {x['window']} {remaining}% left, resets "
                         f"{format_date(x['reset'])} ({duration}){suffix}")
        elif x["window"] == "weekly" and remaining >= RESET_THRESHOLD:
            h = (x["reset"] - now) / 3600
            if h < 24:
                threshold = 12 if h < 12 else 24
                result.append(f"RESETS IN {threshold}H: {name} weekly {remaining}% left, "
                             f"{duration} until reset{suffix}")
    age = max(0, now - data.get("timestamp", now))
    suffix = f" (measured {format_duration(age)} ago)" if age > 7200 else ""
    new_models = [f"NEW MODEL: {v} ({k}): review which models you use, "
                  f"then run ai_limits.py --accept-models{suffix}"
                  for k, v in data.get("models_status", {}).get("new", {}).items()]
    return new_models + result


def claude_stale_hint(data, now=None):
    now = time.time() if now is None else now
    claude = data.get("claude", {})
    timestamp = claude.get("saved_at")
    if timestamp is None:
        return ""
    if (now - timestamp > 21600 or
            any(x["reset"] <= now for x in claude.get("limits", []))):
        age = format_duration(now - timestamp)
        return (f"CLAUDE DATA STALE: last saved {age} ago; "
                "save fresh usage with ai_limits.py --claude-save")
    return ""


def cache_fresh(data):
    return (data is not None and 0 <= time.time() - data["timestamp"] < CACHE_MINUTES * 60
            and not any(x["reset"] <= time.time() for x in data.get("agy", {}).get(
                "limits", [])))


def refresh_background():
    subprocess.Popen([sys.executable, str(ROOT / "ai_limits.py"), "--refresh", "--quiet"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def prepare(args):
    data = read_cache()
    fresh = cache_fresh(data)
    # Fast modes never wait for agy, even with --refresh.
    if args.short or args.menubar:
        if not fresh or args.refresh:
            safe(refresh_background)
        data = dict(data or {})
    elif args.refresh or not fresh:
        data = collect()
        return data, "note" in data
    for k, operation in (("codex", codex_usage), ("claude", claude_usage)):
        data[k] = keep_last_good(safe(operation), data.get(k, {}))
    return data, True


def source_notes(data):
    lines = []
    for k, _ in SOURCES:
        for field in ("error", "last_error", "note"):
            if field in data.get(k, {}):
                lines.append(f"{k}: {data[k][field]}")
    status = data.get("models_status", {})
    if "error" in status:
        lines.append(f"models: {status['error']}")
    for k, error in data.get("model_sources", {}).get("errors", {}).items():
        lines.append(f"{k} models: {error}")
    if "note" in data:
        lines.append(data["note"])
    return lines


def table_name(x):
    if x["source"] == "Claude":
        if "5-hour" in x["name"].lower():
            return "5-hour"
        if "weekly" in x["name"].lower():
            suffix = x["name"][x["name"].lower().index("weekly") + len("weekly"):]
            suffix = suffix.replace("·", "").strip()
            return "Weekly" + (f" ({suffix})" if suffix and suffix.lower() != "all models" else "")
        return x["name"]
    if x["source"] == "Codex":
        return "Weekly" if x["window"] == "weekly" else x["window"]
    name = {"Gemini Models": "Gemini", "Gemini 4 Argon": "Argon",
          "Claude and GPT models": "Claude/GPT"}.get(x["name"], x["name"])
    return name + " " + ("Weekly" if x["window"] == "weekly" else x["window"])


def warning_color(warning):
    return WARNING_COLORS.get(warning.partition(":")[0], "")


def menubar(data):
    now, alerts = time.time(), warnings(data)
    items = all_limits(data)
    warning_colors = [warning_color(s) for s in alerts]
    title_color = next((r for r in WARNING_COLORS.values() if r and r in warning_colors), "")
    title = "AI" + (f" | color={title_color}" if title_color else "")
    cells, colors = [["Tool", "Limit", "Left", "Time left", "Resets"]], ["gray"]
    for x in items:
        expired = x["reset"] <= now
        old = x["source"] == "Claude" and now - x["data_time"] > 21600
        duration = "no fresh data" if expired else format_duration(x["reset"] - now)
        cells.append([x["source"], table_name(x), "-" if expired else f"{x['remaining']}%",
                         duration + (" (stale record)" if old and not expired else ""),
                         "reset" if expired else format_date(x["reset"])])
        colors.append("gray" if expired or old else "")
    widths = [max(len(s[i]) for s in cells) for i in range(5)]
    lines = [title, "---"]
    for line, color in zip(cells, colors):
        render_text = "  ".join((v.rjust(widths[i]) if i == 2 else v.ljust(widths[i]))
                          if i < 4 else v for i, v in enumerate(line))
        lines.append(render_text + " | font=Menlo size=12 trim=false" +
                        (f" color={color}" if color else ""))
    ages = [f"{name} {format_duration(now - data[k].get('data_time', 0))} ago"
              for k, name in SOURCES if data.get(k, {}).get("data_time")]
    if ages:
        lines.append("Measured: " + " · ".join(ages) + " | color=gray")
    lines.extend(text(s) + " | color=gray" for s in source_notes(data))
    lines.extend(s + (f" | color={r}" if r else "") for s, r in zip(alerts, warning_colors))
    if claude_stale_hint(data):
        lines.append(claude_stale_hint(data) + " | color=gray")
    lines.extend(["---", f'Refresh | bash="{sys.executable}" '
                     f'param1="{ROOT / "ai_limits.py"}" param2=--refresh '
                     "param3=--quiet terminal=false refresh=true"])
    return "\n".join(lines)


def render_text(data, from_cache=False):
    age = format_duration(time.time() - data.get("timestamp", time.time()))
    lines = [f"Data: {'cache, ' + age + ' ago' if from_cache else 'now'}"]
    hello = last_hello()
    for k, name in SOURCES:
        if not data.get(k) and not (k == "agy" and hello):
            continue
        lines.append(name + ":")
        for x in data.get(k, {}).get("limits", []):
            detail = ("reset, no fresh data" if x["reset"] <= time.time() else
                     f"{x['remaining']}% · {format_date(x['reset'])} "
                     f"({format_duration(x['reset'] - time.time())})")
            lines.append(f"  {x['name']} {x['window']}: {detail}")
        if k == "agy" and hello:
            lines.append(f"Last automatic message: {hello['group']} {format_date(hello['time'])} "
                         f"({hello['model']}, {hello['result']})")
    status = data.get("models_status", {})
    total = len(data.get("model_sources", {}).get("models", {}))
    lines.append(f"Models: {total}")
    for source, count in status.get("baseline_created", {}).items():
        lines.append(f"Models: {source} baseline created ({count} model)")
    for k, v in status.get("new", {}).items():
        lines.append(f"New: {v} ({k})")
    for k, v in status.get("removed", {}).items():
        lines.append(f"Removed model: {v} ({k})")
    lines.extend(source_notes(data))
    lines.append("Warnings")
    lines.extend(warnings(data) or ["None"])
    if claude_stale_hint(data):
        lines.append(claude_stale_hint(data))
    return "\n".join(lines)


def json_data(data):
    result = dict(data, warnings=warnings(data), last_hello=last_hello())
    for k, _ in SOURCES:
        result[k] = dict(data.get(k, {}))
        result[k]["limits"] = [dict(x, reset_text=format_date(x["reset"]),
                                     time_left=format_duration(x["reset"] - time.time()),
                                     expired=x["reset"] <= time.time())
                                for x in data.get(k, {}).get("limits", [])]
    return result


def accept_models():
    if not acquire_lock():
        raise ValueError("refresh already in progress")
    try:
        data = models()
        status = model_status(data, True)
        cache = read_cache()
        if cache:
            cache["models_status"] = {"new": {}, "removed": {}}
            cache["model_sources"] = data
            atomic_json(CACHE, cache)
        return ("Compared sources: " + ", ".join(data["successful"]) +
                "\nAdded: " + (", ".join(status["new"].values()) or "none") +
                "\nRemoved: " + (", ".join(status["removed"].values()) or "none") +
                "".join(f"\n{k}: {v}" for k, v in data["errors"].items()))
    finally:
        release_lock()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show AI usage limits")
    for name, help_text in (
            ("json", "Print structured JSON output"),
            ("short", "Print up to three warnings without waiting for agy"),
            ("menubar", "Print the SwiftBar menu without waiting for agy"),
            ("refresh", "Refresh cached usage and model lists"),
            ("quiet", "Suppress output"),
            ("accept-models", "Accept current model lists as the baseline"),
            ("claude-save", "Save Claude usage JSON from standard input")):
        parser.add_argument("--" + name, action="store_true", help=help_text)
    args = parser.parse_args(argv)
    try:
        fast = args.short or args.menubar
        if args.claude_save and not fast:
            claude_save(sys.stdin.read())
            return 0
        if args.accept_models and not fast:
            output = accept_models()
        else:
            data, from_cache = prepare(args)
            if args.short:
                alerts = warnings(data)
                selected = alerts[:3]
                if len(alerts) > 3:
                    selected[-1] += f" (+{len(alerts) - 3} warnings: ai_limits.py)"
                lines = ["; ".join(selected[:2])] if selected else []
                if len(selected) == 3:
                    lines.append(selected[2])
                if claude_stale_hint(data):
                    lines.append(claude_stale_hint(data))
                output = "\n".join(lines)
            elif args.menubar:
                output = menubar(data)
            elif args.json:
                output = json.dumps(json_data(data), ensure_ascii=False, indent=2)
            else:
                output = render_text(data, from_cache)
        if output and not args.quiet:
            print(output)
        return 0
    except BaseException as error:
        if not args.quiet and not args.short:
            if args.menubar:
                print("AI\n---\nCould not read data | color=gray")
            else:
                prefix = ("Could not save Claude usage" if args.claude_save else
                          "Could not accept models" if args.accept_models else
                          "Could not complete operation")
                print(f"{prefix}: {str(error) or 'No details'}", file=sys.stderr)
        if args.short or args.menubar:
            return 0
        return 0 if args.quiet and not (args.accept_models or args.claude_save) else 1


if __name__ == "__main__":
    raise SystemExit(main())

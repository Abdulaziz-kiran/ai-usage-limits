# ai-usage-limits

See how much of your Claude, Codex and Antigravity limits is left, in one place: terminal, JSON, or the macOS menu bar. It warns you when a weekly limit is about to reset with quota still unused, when a limit runs low, and when a new model appears.

A single Python file with no dependencies. By default it never sends a prompt to any model.

> Unofficial. Not affiliated with Anthropic, OpenAI or Google. It relies on local files and CLI output that are not stable public interfaces, so a tool update can break it.

## Status of each source

| Tool | How it is read | Automatic? |
|---|---|---|
| Antigravity | `agy -p /usage` and `agy models` | Yes |
| Codex | The latest `rate_limits` record in `~/.codex/sessions` | Yes, as of your last Codex session |
| Claude | JSON you save with `--claude-save` | **No** |

The Claude row is the main limitation. I have not found a way to read plan usage from a plain shell, so Claude data is only as fresh as your last save. Pull requests are welcome.

You do not need all three tools. Anything not installed is skipped.

## Install

Requires Python 3.9+ on macOS or Linux. Developed and used on macOS; Linux is untested.

```bash
git clone https://github.com/Abdulaziz-kiran/ai-usage-limits.git
cd ai-usage-limits
python3 ai_limits.py --refresh
```

## Usage

```bash
python3 ai_limits.py                 # readable summary (30-minute cache)
python3 ai_limits.py --refresh       # measure now
python3 ai_limits.py --short         # warnings only; prints nothing if all is well
python3 ai_limits.py --json          # for scripts
python3 ai_limits.py --menubar       # SwiftBar format
python3 ai_limits.py --accept-models # treat the current model list as known
```

## Warnings

| Label | Meaning |
|---|---|
| `LOW` | Under 10% left |
| `RESETS IN 24H` / `RESETS IN 12H` | A weekly limit resets soon with at least 30% unused |
| `NEW MODEL` | A model appeared that was not in your known list. Clear it with `--accept-models` |
| `CLAUDE DATA STALE` | Your saved Claude data is over 6 hours old or has passed its reset |

## Saving Claude usage

Pipe JSON to `--claude-save`. `resetsAt` needs a timezone, and windows with a `null` reset are ignored. A `{"plan": {"windows": [...]}}` wrapper is also accepted.

```bash
python3 ai_limits.py --claude-save <<'EOF'
{"windows": [
  {"label": "5-hour", "percentUsed": 42, "resetsAt": "2026-11-09T20:00:00Z"},
  {"label": "Weekly · All models", "percentUsed": 18, "resetsAt": "2026-11-14T08:00:00Z"}
]}
EOF
```

## Menu bar (macOS)

Install [SwiftBar](https://github.com/swiftbar/SwiftBar), then set its plugin folder to this repository's `swiftbar/` directory. Keep only the plugin in that folder, because SwiftBar treats every file there as a plugin.

The menu title is `AI`. It turns red or orange for the 12-hour and 24-hour reset warnings, and blue for a new model.

## Warnings at Claude Code session start (optional)

Add this to `~/.claude/settings.json`, using your own path:

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "python3 /path/to/ai_limits.py --short", "timeout": 5}]}
    ]
  }
}
```

`--short` only reads cached files and refreshes in the background, so it does not slow session start.

## Automatic hello for Antigravity (optional, off by default)

Antigravity's weekly counter appears to start only at a group's first message, so an unused window can sit idle. To have the tool start it for you, create `data/settings.json`:

```json
{"auto_hello": true}
```

When a group's weekly counter looks unstarted across two measurements, a background refresh sends it the single word `hello` with the cheapest available model, at most once per group every 6 hours. Attempts are logged to `data/hello-log.jsonl`.

This is experimental. It uses a small amount of your quota, it also starts that group's 5-hour window, and the assumption about the weekly counter has not been confirmed.

## What it touches

- Writes only to `data/` next to the script. Delete that folder to reset.
- Reads the end of the five most recent Codex session files, and parses only lines containing `rate_limits`. Those logs hold your conversations, but only limit numbers are stored.
- Reads Codex's model cache files. Honours `CODEX_HOME`.
- Runs `agy` only for `/usage` and `models`, at most once every 30 minutes unless you ask for `--refresh`. The optional automatic hello is the only other call allowed. Anything else is refused in code.
- Makes no network requests itself. `agy` does its own.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary directories and fake processes.

## License

[MIT](LICENSE)

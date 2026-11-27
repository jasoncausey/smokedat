# SmokeDat

Connects to an iBBQ-protocol Bluetooth thermometer (Oprol 6-probe / "BBQ 6X" app), logs every probe,
serves a live web app, predicts time-to-goal, and pushes alerts to your phone.

## Run

There are two ways to run the bridge: from a checkout with `smokedat.sh`, or as a command-line tool
installed with [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/). Either way, the page is
at http://127.0.0.1:9339. For HTTPS on your tailnet, run this once:

```bash
tailscale serve --bg 9339   # https://<your-machine>.<your-tailnet>.ts.net (tailnet only)
```

### From a checkout

```bash
cp config.example.json config.json   # optional, one-time: starter alert presets, notifications off
./smokedat.sh                    # keeps the Mac awake while running
```

The script creates `venv/` on first run, and keeps the config, keys and logs in the checkout directory.

### With uvx

`uvx` builds and runs the bridge in a cached, throwaway environment. You don't install anything.

```bash
uvx --from /path/to/smokedat smokedat                 # from a local checkout
uvx --from git+<repo-url> smokedat                    # straight from the git repo
caffeinate -i uvx --from /path/to/smokedat smokedat   # keep the Mac awake while it runs
```

After you change a local checkout, add `--reinstall` so uv rebuilds it. With a git URL, add
`--refresh` to pick up new commits.

### With pipx

`pipx` installs `smokedat` as a command on your `PATH`.

```bash
pipx install /path/to/smokedat        # or: pipx install git+<repo-url>
caffeinate -i smokedat                # keep the Mac awake while it runs
pipx install --force /path/to/smokedat   # update after pulling changes
pipx uninstall smokedat
```

You can also use `uv tool install` in place of `pipx install`.

### Data directory and options

When the bridge runs from uvx or pipx, it keeps its config, push keys and logs in a data directory:

- macOS: `~/Library/Application Support/SmokeDat`
- Other systems: `~/.local/share/smokedat`

To use a different directory, pass `--data-dir DIR` or set `SMOKEDAT_DATA_DIR=DIR`. To keep using a
checkout's existing files, point it at the checkout:

```bash
smokedat --data-dir /path/to/smokedat
```

A new data directory gets a new web-push key, so each phone has to tap **Enable on this device**
again. If there is no `config.json`, the bridge starts with built-in alert presets. Copy
`config.example.json` into the directory as `config.json` if you want the example presets instead.

Other options: `--port`, `--host`, `--device` (BLE name or address) and `-v`. Run `smokedat --help`
for the full list.

Only one Bluetooth central can hold the thermometer. Close the BBQ 6X phone app while the bridge runs.
The thermometer stops advertising while the app is connected, so the bridge can't see it.

## Phone setup

1. Open the `https://…ts.net` address in Safari, then tap Share → **Add to Home Screen**.
2. Open **SmokeDat** from the home screen and tap **Enable on this device** (web push).
   These alerts follow the ring/silent switch, like any app notification.
3. For alerts that sound **through silent mode**, set up Pushover. In the Pushover app's settings,
   turn on Critical Alerts. Then paste your user key and an app token into the page.

## Acknowledging alarms

Acknowledge an alarm in any of these ways: tap **Acknowledge** in the red banner, tap the web-push
notification, or acknowledge it in the Pushover app. Any one of them stops every outstanding Pushover
repeat and silences the thermometer's beeper. A later alarm still notifies as normal, for example from
another probe, or from the same probe after it moves back past the threshold.

The page keeps the screen awake while it is open, using the Screen Wake Lock API. Turn this off with the
**Screen on** button.

## Chart zoom

Drag across the chart to zoom. Drag sideways to zoom time, up or down to zoom temperature, or draw a box
to zoom both. A temperature zoom is handy for cutting off the drop when you pull the meat. On a phone, a
vertical swipe scrolls the page, so draw a box instead (start the drag sideways). Drag again to zoom
further, and tap **Zoom out** to step back. **Session** fits the whole session, from the first logged
reading to the last. The 30m–24h buttons go back to a live view that follows the newest readings.

## Sessions and export

Under the chart, **New session** starts fresh for the next cook. It clears the chart, the time-to-goal
estimates and the recent alerts list, and starts a new log file. Alarms you haven't acknowledged yet stay
in the list. Earlier CSV files stay in `logs/`. The session survives a bridge restart, and a reset on one
device shows up on every open page.

**Export CSV** downloads the whole current session, including any part older than the 24 hours the chart
shows. Temperatures are in the units the page is set to. The file has one row every 10 s with
`time`, `elapsed_min` and `epoch` columns, plus one column for each probe that recorded a reading. On iPhone,
Export opens the share sheet, so you can save the file to Files, AirDrop it or mail it.

## Files

The paths below the package files are relative to the data directory (the checkout, when you use `smokedat.sh`).

| Path                                           | What                                                                                                                            |
|------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| `smokedat.sh`                                  | Starts the bridge from the checkout (creates the venv on first run)                                                             |
| `pyproject.toml`                               | Package metadata, dependencies and the `smokedat` command                                                                       |
| `smokedat/bridge.py`                           | BLE client, alert logic, time-to-goal model, web server                                                                         |
| `smokedat/static/`                             | Web app (page, service worker, manifest, icons)                                                                                 |
| `config.json`                                  | Saved alerts, probe assignments, notification settings (includes Pushover keys). Not in git; copy it from `config.example.json` |
| `config.example.json`                          | Starter config with the alert presets and no credentials                                                                        |
| `logs/cook-*.csv`                              | One CSV per run or session, one row every 10 s, temperatures in °C. The current session's last 24 h is reloaded on restart      |
| `logs/session.json`                            | When the current session started                                                                                                |
| `vapid_private.pem`, `push_subscriptions.json` | Web-push signing key and subscribed devices                                                                                     |

## Time to goal

For each probe with a high alert, the bridge fits a Newton's-law heating curve
`T = Ta − B·e^(−t/τ)` to the last hour of readings, and a straight line. It uses the curve when the curve
fits clearly better, because the curve captures the slowdown as meat approaches pit temperature. If the
fitted level-off temperature is below the goal, the probe shows as **stalled** instead of giving a
misleading ETA. The bridge needs at least 5 minutes of data before it predicts. A stall that has only
just started can still make the first estimates too optimistic.

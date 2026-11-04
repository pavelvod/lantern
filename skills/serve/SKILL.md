---
name: serve
description: Start the lantern web UI for a map, where the user answers, comments, approves and ends their turn. Use for `/lantern:serve [slug]`.
disable-model-invocation: true
---

# lantern:serve — the UI

`L` is `../../bin/lantern.py` relative to this skill's base directory, as an absolute path. Slug omitted: use the only map in `.lantern/` (or `.dagmap/`). If there are several, ask which one.

Start it from the cwd that holds `.lantern/`, as a long-running process.

If `$HERDR_PANE_ID` is set, the user works in herdr: start it in a pane split to the right, so they can see it.

```bash
P=$(herdr pane split $HERDR_PANE_ID --direction right --cwd "$PWD" | jq -r '.result.pane.pane_id')
herdr pane run $P "python3 $L serve <slug>"
herdr pane wait-output $P --regex 'lantern: http|Error' --timeout 10000
```

Otherwise, run `python3 $L serve <slug>` as a background command and read its output until the `lantern: http://...` line or an error appears.

Give the user the URL from the `lantern: http://...` line, only after it appears. If the output shows `Address already in use`, another server holds port 8765: run it again with `--port 0`, which picks a free port and prints it. The server reads `ui.html` at startup: restart it after the UI changes. An open tab reloads itself when the server restarts.

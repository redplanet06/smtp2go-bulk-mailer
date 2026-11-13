# SMTP2GO Bulk Mailer

A local, single-file web console for sending templated email through the [SMTP2GO](https://www.smtp2go.com/) API.

- **Standard library only** — no packages to install.
- **Runs entirely on your machine** — the page is served on `127.0.0.1`, and API keys stay server-side; they are never sent to the browser.
- Send per-recipient or in batches, with pacing, dry-run, test sends and BCC.
- Paste a plain list or a CSV (Microsoft 365 / Entra exports work as-is) with merge fields like `{{first_name}}`, `{{last_name}}` and `{{full_name}}`.
- Pick SMTP2GO templates from your account, manage multiple labelled API keys, and view a per-key delivery log.

## Run

```bash
python3 smtp2go_sender.py
```

On Windows: `py smtp2go_sender.py`. The console opens automatically at <http://127.0.0.1:8765>.

## Configuration

| Setting | How |
| --- | --- |
| API key | Paste it in the page, or set the `SMTP2GO_API_KEY` environment variable (never written to disk) |
| Port | `SMTP2GO_PORT` environment variable (default `8765`) |

Settings, keys and recipient lists are saved to `smtp2go_config.json` beside the script, and sent message IDs to `smtp2go_sent_ids.json`. Both files are plain text and are excluded from git by `.gitignore` — keep them out of version control.

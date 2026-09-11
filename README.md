# Apokalypse Code Analysis System

A multi-language code analysis engine with a secure FastAPI WebUI and a local language model
for semantic review. It parses source, indexes dependencies, checks deterministic facts, and
manages resumable project analysis.

## Installation and first run

These instructions use Ollama because it is available on Windows, Linux, and macOS and exposes
the local API expected by ACAS. Lemonade Server can also be used; see **Using Lemonade Server**
below.

### Common preparation

Before choosing the instructions for your operating system, prepare the following:

1. A computer with enough memory for the model. The recommended
   [`deepseek-coder-v2:16b`](https://ollama.com/library/deepseek-coder-v2) download is about 8.9 GB,
   and the model needs additional RAM or VRAM while analysing code.
2. A working SMTP account that can send verification and password-reset messages. Obtain its SMTP
   host, port, username, password or app password, sender address, and whether it uses STARTTLS
   (commonly port 587) or SSL (commonly port 465).
3. An owner username and owner email address. The first account registered with both exact values
   becomes the protected owner/administrator. Owner usernames must be 3-30 characters and contain
   only letters, numbers, `_`, or `-`.
4. An owner password containing at least 15 characters.

Every command below containing a value beginning with `YOUR_` must be edited before it is run.
Do not copy placeholder values unchanged. The examples make ACAS available only on the local
computer and use `http://127.0.0.1:8000` in verification links.

### Windows Command Prompt

1. Install [Git for Windows](https://git-scm.com/download/win),
   [Python](https://www.python.org/downloads/windows/), and
   [Ollama for Windows](https://ollama.com/download/windows). Select **Add Python to PATH** during
   Python installation, then restart Command Prompt.
2. Check the installed commands:

```batch
git --version
py --version
ollama --version
```

3. Clone ACAS and enter its directory:

```batch
git clone https://github.com/Doktor-Apokalypse/ACAS.git
cd ACAS
```

4. Create a Python environment and install the dependencies:

```batch
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

5. Download the default model:

```batch
ollama pull deepseek-coder-v2:16b
```

6. Replace every `YOUR_...` value, then configure this Command Prompt session:

```batch
set "OWNER_USERNAME=YOUR_OWNER_USERNAME"
set "OWNER_EMAIL=YOUR_OWNER_EMAIL_ADDRESS"
set "SMTP_HOST=YOUR_SMTP_HOST"
set "SMTP_PORT=587"
set "SMTP_USERNAME=YOUR_SMTP_USERNAME"
set "SMTP_PASSWORD=YOUR_SMTP_PASSWORD_OR_APP_PASSWORD"
set "SMTP_FROM=YOUR_SENDER_EMAIL_ADDRESS"
set "PUBLIC_BASE_URL=http://127.0.0.1:8000"
set "OLLAMA_MODEL=deepseek-coder-v2:16b"
```

7. Start ACAS and leave the window open:

```batch
python main.py
```

### Windows PowerShell

1. Install [Git for Windows](https://git-scm.com/download/win),
   [Python](https://www.python.org/downloads/windows/), and
   [Ollama for Windows](https://ollama.com/download/windows). Select **Add Python to PATH** during
   Python installation, then restart PowerShell.
2. Check the installed commands:

```powershell
git --version
py --version
ollama --version
```

3. Clone ACAS and enter its directory:

```powershell
git clone https://github.com/Doktor-Apokalypse/ACAS.git
Set-Location ACAS
```

4. Create a Python environment and install the dependencies:

```powershell
py -3 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The execution-policy change applies only to this PowerShell process.

5. Download the default model:

```powershell
ollama pull deepseek-coder-v2:16b
```

6. Replace every `YOUR_...` value, then configure this PowerShell session:

```powershell
$env:OWNER_USERNAME = "YOUR_OWNER_USERNAME"
$env:OWNER_EMAIL = "YOUR_OWNER_EMAIL_ADDRESS"
$env:SMTP_HOST = "YOUR_SMTP_HOST"
$env:SMTP_PORT = "587"
$env:SMTP_USERNAME = "YOUR_SMTP_USERNAME"
$env:SMTP_PASSWORD = "YOUR_SMTP_PASSWORD_OR_APP_PASSWORD"
$env:SMTP_FROM = "YOUR_SENDER_EMAIL_ADDRESS"
$env:PUBLIC_BASE_URL = "http://127.0.0.1:8000"
$env:OLLAMA_MODEL = "deepseek-coder-v2:16b"
```

7. Start ACAS and leave the window open:

```powershell
python main.py
```

### Linux

These package commands are for Ubuntu and Debian. On another distribution, install Git, Python
3.10 or newer, pip, and Python venv support with the distribution package manager.

1. Install Git and Python:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

2. Install Ollama using its official Linux installer:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

3. Clone ACAS, create the Python environment, and install the dependencies:

```bash
git clone https://github.com/Doktor-Apokalypse/ACAS.git
cd ACAS
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

4. Ensure Ollama is running, then download the model:

```bash
sudo systemctl start ollama
ollama pull deepseek-coder-v2:16b
```

If the installer did not create a system service, run `ollama serve` in a second terminal and
leave it open.

5. Replace every `YOUR_...` value, then configure this terminal session:

```bash
export OWNER_USERNAME="YOUR_OWNER_USERNAME"
export OWNER_EMAIL="YOUR_OWNER_EMAIL_ADDRESS"
export SMTP_HOST="YOUR_SMTP_HOST"
export SMTP_PORT="587"
export SMTP_USERNAME="YOUR_SMTP_USERNAME"
export SMTP_PASSWORD="YOUR_SMTP_PASSWORD_OR_APP_PASSWORD"
export SMTP_FROM="YOUR_SENDER_EMAIL_ADDRESS"
export PUBLIC_BASE_URL="http://127.0.0.1:8000"
export OLLAMA_MODEL="deepseek-coder-v2:16b"
```

6. Start ACAS and leave the terminal open:

```bash
python main.py
```

### macOS

Ollama requires macOS 14 Sonoma or newer. Apple silicon supports CPU and GPU inference; Intel Macs
use CPU inference.

1. Install [Python](https://www.python.org/downloads/macos/) and
   [Ollama for macOS](https://ollama.com/download). Open Ollama once after installing it. Install
   the Apple command-line tools, which provide Git:

```bash
xcode-select --install
```

2. Open a new Terminal window and check the installed commands:

```bash
git --version
python3 --version
ollama --version
```

3. Clone ACAS, create the Python environment, and install the dependencies:

```bash
git clone https://github.com/Doktor-Apokalypse/ACAS.git
cd ACAS
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

4. Download the default model:

```bash
ollama pull deepseek-coder-v2:16b
```

5. Replace every `YOUR_...` value, then configure this Terminal session:

```bash
export OWNER_USERNAME="YOUR_OWNER_USERNAME"
export OWNER_EMAIL="YOUR_OWNER_EMAIL_ADDRESS"
export SMTP_HOST="YOUR_SMTP_HOST"
export SMTP_PORT="587"
export SMTP_USERNAME="YOUR_SMTP_USERNAME"
export SMTP_PASSWORD="YOUR_SMTP_PASSWORD_OR_APP_PASSWORD"
export SMTP_FROM="YOUR_SENDER_EMAIL_ADDRESS"
export PUBLIC_BASE_URL="http://127.0.0.1:8000"
export OLLAMA_MODEL="deepseek-coder-v2:16b"
```

6. Start ACAS and leave the terminal open:

```bash
python main.py
```

### SMTP variations

The commands above assume STARTTLS on port 587, which matches the ACAS defaults. If the mail
provider requires implicit SSL, also set `SMTP_USE_TLS=false`, set `SMTP_USE_SSL=true`, and use its
SSL port. If authentication is not required, leave `SMTP_USERNAME` and `SMTP_PASSWORD` empty.
ACAS requires `SMTP_HOST`, `SMTP_FROM`, and `PUBLIC_BASE_URL` before it can send account links.

### First sign-in

1. Keep Ollama and ACAS running.
2. Open <http://127.0.0.1:8000>.
3. Select **Register**.
4. Register with the exact owner username and owner email configured above, and a password of at
   least 15 characters.
5. Open the verification message and follow its link on the same computer.
6. Sign in. The matching account is created as the protected owner and administrator.
7. Upload a small source file first and confirm that analysis completes before trying a large
   project.

Environment variables set by these examples last only for the current terminal session. Set them
again before the next start, or configure them securely through the operating system or a service
manager. The application does not automatically load `.env`; [`.env.example`](.env.example) is a
reference list of all available settings.

### Using Lemonade Server

Install Lemonade Server and a compatible local model, then set `OLLAMA_URL` to its server address
and `OLLAMA_MODEL` to the exact installed model name. Lemonade models whose names end in `-Hybrid`
use `/v1/chat/completions`; other model names use `/api/chat`. Skip `ollama pull` when Lemonade
manages the model.

### Confirming the installation

While ACAS is running:

- Open <http://127.0.0.1:8000/health> to confirm the web application and database are available.
- Open <http://127.0.0.1:8000/ready> to confirm the database, job worker, model server, and
  configured model are ready.
- The WebUI header shows the detected model.
- Stop ACAS with **Ctrl+C** in its terminal.

### Project uploads

Use the `+` button in the WebUI to upload:

- **Individual files:** one source file or several files grouped as one project. Use the folder
  option when directory structure matters.
- **A project folder:** preserves its directories and files.
- **A ZIP project:** accepts a normal, unencrypted `.zip` containing the project files.

Function analysis supports Python, JavaScript, TypeScript, C, C++, C#, Rust, Pascal, PowerShell,
shell scripts, SQL, and HTML. Related configuration, documentation, stylesheet, and data files may
be included as project context. Common dependency, virtual-environment, version-control, cache, and
build-output directories are skipped. Executables and uploaded source are stored but never run.

### Account ownership

Configure `OWNER_USERNAME` and `OWNER_EMAIL` before registering the first account. An account that
registers with both exact values becomes the protected owner and administrator.

The owner can manage users and administrators, registration, jobs, storage limits, announcements,
backups, and the audit log. Administrators can manage permitted users and active jobs. The protected
owner cannot be demoted, banned, anonymized, or deleted through the administration interface.

### Error push notifications

Application `ERROR` and `CRITICAL` console records are also published to the configured NTFY
topic. Delivery uses a bounded background queue, so a slow or unavailable notification server
does not delay requests or model work. Identical errors are suppressed for 60 seconds by default,
long tracebacks are bounded, and delivery failures produce at most one console warning every five
minutes instead of recursively generating more notifications.

Set `NTFY_TOPIC` to a private topic name to enable alerts, or leave it empty to disable them.
Use `NTFY_SERVER_URL` for a self-hosted server. A protected topic can use an access token supplied
only through the `NTFY_ACCESS_TOKEN` environment variable.

### Public deployment

For a public instance:

1. Put the application behind HTTPS or let `python main.py` create an ngrok HTTPS tunnel.
2. Set `PUBLIC_BASE_URL` to the exact external origin, with no path or trailing slash.
3. Put the corresponding hostname in the comma-separated `TRUSTED_HOSTS` setting.
4. Configure SMTP and keep its credentials in the deployment environment or secret store.
5. Copy the configured backup directory to separate storage so a host or disk failure cannot
   destroy both the database and its backups.

When an existing database has pending schema migrations, the application automatically creates
and validates an online pre-migration copy in `MIGRATION_BACKUP_DIR`. Fresh databases and normal
restarts do not create redundant copies. Keep ordinary off-machine backups as well; migration
copies protect upgrades but are not a disaster-recovery strategy. The application also makes a
verified online backup every `PERIODIC_BACKUP_INTERVAL_SECONDS` (24 hours by default), keeping the
newest `PERIODIC_BACKUP_RETENTION_COUNT` routine copies. These settings can be disabled with
`CREATE_PERIODIC_BACKUPS=false`; routine rotation never deletes pre-migration copies.

To restore, stop the service, preserve the current `CHAT_DB_PATH`, copy the chosen periodic `.db`
file into that path, and run `PRAGMA quick_check` before restarting. Restore testing and an
off-machine copy remain necessary: local rotation protects against accidental data damage, not
loss of the whole host.

The server refuses incomplete email/public-URL configuration at startup. Cross-origin unsafe
requests and untrusted `Host` headers are rejected. Responses also include a nonce-based content
security policy, clickjacking/MIME protections, a no-referrer policy, and HSTS on HTTPS requests.
Malformed, out-of-range, or contradictory environment settings stop startup with an error that
names every invalid relationship instead of being silently clamped.

## Common settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | HTTP bind address and port |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server base URL |
| `OLLAMA_MODEL` | `deepseek-coder-v2:16B` | Model used for chat and analysis |
| `CHAT_DB_PATH` | `chat_memory.db` beside the code | SQLite database location |
| `PUBLIC_BASE_URL` | empty | Canonical external HTTP(S) origin |
| `TRUSTED_HOSTS` | local hosts | Additional comma-separated hostnames |
| `NTFY_TOPIC` | NTFY notification topic name | NTFY topic for error alerts; empty disables alerts |
| `JOB_QUEUE_CAPACITY` | `20` | Maximum queued Ollama jobs |
| `MAX_ACTIVE_JOBS_PER_USER` | `3` | Per-user queued/processing limit |
| `MAX_MESSAGE_CHARS` | `1500000` | Maximum submitted message size |

See `.env.example` for model limits, throttling, session lifetime, SMTP, and analysis settings.

## Health probes

- `GET /health` is a lightweight liveness probe. It returns HTTP 200 while the web process can
  serve requests and does not expose internal configuration.
- `GET /ready` checks SQLite, the dedicated job worker, Ollama, and the configured model. It
  returns HTTP 200 only when chat work can be completed, otherwise HTTP 503. Results are cached
  briefly to prevent monitoring traffic from repeatedly contacting Ollama.

## Request correlation

Every HTTP response includes `X-Request-ID`. A safe ID supplied by a trusted upstream is
preserved; malformed values are replaced with a generated ID. Completion logs include that ID,
the method, path, status, and elapsed time. Query strings are deliberately excluded so email
verification and password-reset tokens are not written to access logs. Unexpected HTTP 500
responses are generic and include the request ID for correlation with the server traceback.

## Database maintenance

Expired login sessions, registration links, and password-reset links are removed at startup and
periodically while the service runs. Stale login and registration throttles are pruned on the same
schedule. Dedicated expiry indexes keep maintenance efficient without deleting active sessions,
valid one-time links, or current lockouts.

Terminal chat-job rows retain their status and links to canonical chat messages, but duplicate
prompt/reply payloads and detailed progress logs are cleared after
`TERMINAL_JOB_PAYLOAD_RETENTION_DAYS` (seven days by default). Delayed polling still resolves a
completed reply from the linked assistant message. Chat history content itself is not removed by
this maintenance task.

Opening a chat initially returns only the newest `CHAT_HISTORY_PAGE_SIZE` messages (20 by default).
The browser offers a cursor-based **Load earlier messages** control, so long conversations do not
require one unbounded database query, JSON response, and DOM render. Progress history is queried
only for assistant messages present in the requested page.

Analysis-workspace lookup uses `CHAT_LIST_PAGE_SIZE` (50 by default). Its opaque keyset cursor
includes every list-ordering field; the WebUI automatically selects the newest returned workspace.

## Account data export

Signed-in users can select **Export data** in the account header to download a streaming JSON copy
of their profile and complete canonical chat/message history. The export includes compact context
stored alongside messages, but never includes password hashes, session credentials, verification
or reset tokens, rate-limit records, or duplicate background-job payloads. Export responses are
marked `no-store` and use an attachment filename derived from the validated username.

Passwords are stored with bounded scrypt parameters. Older valid hashes are accepted and upgraded
to the current work factor after a successful login, so existing users do not need to reset their
passwords when the hashing policy changes. New and reset passwords require at least 15 characters,
allow spaces and Unicode, and are checked against a local common/expected-password blocklist rather
than requiring predictable uppercase, number, and symbol combinations.

Registration and password-reset email requests also have separate persistent per-IP allowances.
Rate-limited requests retain the same generic response as accepted requests, preventing the limit
from becoming an account-enumeration signal. Valid requests also share a short configurable
response-time floor so the faster unknown-account path does not reveal whether an identity exists.

Successful login rotates any session presented by that browser. Each account retains only its
newest configured number of sessions, limiting forgotten or duplicated credentials while allowing
normal multi-device use. Logout expires the cookie with attributes matching its HTTPS session.

Every successful administrator account or job-control action is recorded transactionally with the
acting and target account snapshots, action, time, HTTP request ID, and relevant safe details. The
owner can review and filter events at `GET /api/admin/audit` or download the filtered results from
`GET /api/admin/audit/export`; ordinary administrators cannot read this trail. Records are retained for
`ADMIN_AUDIT_RETENTION_DAYS` (365 by default) and pruned by the normal security-maintenance cycle.

## Changelog

Signed-in users can open **Changelog** from the account header. Database entries are generated from
the ordered migration registry and show whether and when each migration was applied to the current
database. Curated entries cover application changes that do not require a schema migration.

The repository copy is [`changelog.md`](changelog.md), newest first, with headings and bullet lists.
The WebUI's **Download Markdown** link downloads the same format. Regenerate it after adding a
migration or curated entry with:

```powershell
python changelog.py
```

Use `python changelog.py --database chat_memory.db` when a separate output should also include that
database's installation status. Migration timestamps are chronology markers, not release dates.

## Verification

The regression suite uses temporary databases and fake Ollama streams; it does not modify the
live chat database or require Ollama to be running:

```powershell
python -m unittest discover -s tests -v
python -m py_compile main.py app_config.py api_models.py authentication.py database.py analysis_engine.py changelog.py project_uploads.py project_inventory.py web_assets.py migrations.py
```

## Troubleshooting

- **Ollama connection failure:** confirm `ollama serve` is running and `OLLAMA_URL` is reachable.
- **Model occupies VRAM but inference never starts:** inspect Ollama's `server.log` in
  `%LOCALAPPDATA%\Ollama`. `timed out waiting for llama-server to start` is a backend
  loading failure. The engine stops the pass on this error, model-load/memory failures,
  connection failures or an idle response timeout, preserving completed reviews and leaving
  active functions pending for resumption. Batch requests are not split and retried against
  an unavailable backend. Fix Ollama before resuming; extending output-token limits will
  not resolve a model that cannot finish loading.
- **Integrated GPU memory allocation:** reserving more RAM for the GPU leaves less for
  Windows, model loading and host buffers. Monitor available physical RAM and paging as well
  as VRAM. A larger pagefile increases the commit limit but does not add physical RAM.
  `ollama ps` reports model placement while a model is loaded; it is not a GPU-utilization meter.
- **Model not found:** run `ollama pull <model>` and make `OLLAMA_MODEL` match that name.
- **Registration mail fails:** configure both `SMTP_HOST` and `SMTP_FROM`; authenticated servers
  normally also need `SMTP_USERNAME` and `SMTP_PASSWORD`.
- **Public host returns 400:** add only the hostname (not a URL) to `TRUSTED_HOSTS`, then restart.
- **Startup rejects the public URL:** use an exact `http://` or `https://` origin without a path,
  query string, or fragment.

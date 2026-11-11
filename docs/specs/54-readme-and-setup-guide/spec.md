# Spec 54: README and setup guide

- **Status:** Approved (owner review, 2026-11-11)
- **Phase and stream:** Phase 3 (together), after spec 53
- **Depends on:** 53 (the Docker commands and the demo transcript), 10 (the HLD link), 11 (the images), 55 (the experience document link)
- **Requirements covered:** deliverable 5 (setup instructions) and deliverable 2.4 (setup instructions and an example run)

---

## Part A — Product (review this part)

### A1. Goal

An OpsFleet reviewer opens the repository on an unknown operating system. In about 15 minutes, the reviewer has the chat running and gets the first answer. The README also leads the reviewer to each deliverable of the brief.

### A2. Behavior the user sees

1. **First screen.** The reviewer opens the repository on GitHub. The README starts with three sentences about the assistant, the prototype picture (spec 11), and a table of the deliverables with links.
2. **Prerequisites.** A short table lists what the reviewer needs. The list is: Docker (or Python 3.12 with uv or pip), a Google Cloud project, the Google Cloud CLI, and a free-tier Gemini key.
3. **Credentials for each operating system.** The reviewer opens the section for macOS, Linux or Windows. Each section is a collapsed block. The other blocks stay closed and do not distract.
4. **A free Gemini key.** The reviewer follows 5 steps and gets a key on the free tier. A warning explains the HTTP 402 error. That error comes from a key in a project with billing.
5. **Docker first.** The quick start uses Docker: copy `.env`, build, run. The run command is different for each operating system. The reviewer copies one block.
6. **Without Docker.** A second section shows uv, and a third shows `pip install -r requirements.txt`, as the brief describes.
7. **Example run.** The reviewer sees a real, short chat. It has a question, a refused request for email addresses, a report with an HTML file, and a delete with the typed `delete N`.
8. **What to try.** A table lists the demo users (`maya`, `alex`, `ceo`), their brands, and sample questions. Another table lists the CLI commands.
9. **Problems.** A troubleshooting table lists each common error, its cause and the fix.
10. **Cost for the reviewer.** One short section explains the cost. The Gemini key is on the free tier. Each small BigQuery query bills about 10 MB. The prototype has a 1 GB cap for each query and a 20 GB cap for each day.

### A3. Scope

- **In scope:**
  - The full text of `README.md`. It replaces the current README.
  - The setup for Docker, uv and pip on macOS, Linux and Windows.
  - The Google Cloud ADC setup for each operating system.
  - The free-tier Gemini key steps.
  - A real example run.
  - The links to the documents.
  - The troubleshooting table.
  - A short developer section.
- **Out of scope:**
  - The Docker image and its commands (spec 53). This spec copies the tested commands.
  - The HLD (spec 10) and the experience document (spec 55).
  - Production deployment.

### A4. Limits the user sees

- **Length.** The reviewer reaches the first `docker run` command after at most two screens of text. The details per operating system and the troubleshooting are in collapsed blocks or at the end.
- **Time.** About 15 minutes for a first setup when Docker and a Google account exist. The first image build takes several minutes.
- **Tested commands only.** Each command in the README ran on at least one machine during spec 53 or this spec. B4 records where. A command that only had a review, not a run, has a note.
- **Figures in the example change.** Google can regenerate the public dataset. The README says that the figures in the example run can differ from the reviewer's figures.

### A5. Done when

- [ ] The README sections are in the order of B2, sections 1–15.
- [ ] The deliverables table links to each deliverable of the brief: the diagrams, the HLD, the prototype, the CLI, the setup, and the experience document.
- [ ] The credentials section has one collapsed block each for macOS, Linux and Windows. Each block shows where the ADC file is.
- [ ] The Gemini key section says to create the key in a project without billing. It explains the HTTP 402 error.
- [ ] The Docker section has one run command for each operating system. The commands are the same as in spec 53, B2.
- [ ] The Linux command contains `--user "$(id -u):$(id -g)"`.
- [ ] The uv path and the pip path each start the chat on a clean clone.
- [ ] The example run is a real transcript from the demo of spec 53. It contains no key, no project ID and no PII.
- [ ] The troubleshooting table has each row of B2.
- [ ] All relative links work. The images show on GitHub.
- [ ] A person who did not write the README follows it on a clean clone and gets the first answer.
- [ ] The STE lint (`ste-lint.py`) gives no hard violations for `README.md`.

---

## Part B — Technical

### B1. Key decisions

| | Decision | Choice | Alternatives | Reason |
|---|---|---|---|---|
| ⚑ | One README or a separate setup guide | One `README.md`. The steps for each operating system are in collapsed `<details>` blocks. Troubleshooting is at the end. | `README.md` and `docs/setup.md` | The reviewer starts at the README. One file means no switch between files. GitHub shows `<details>` blocks as collapsed sections. |
| ⚑ | Google Cloud project for the reviewer | Recommend one Google Cloud project without a billing account for both items: the free-tier Gemini key and the BigQuery queries (BigQuery sandbox). A project with billing also works for BigQuery. | Always a project with billing | A project without billing cannot make a cost. The Gemini key from it is on the free tier. The sandbox allows queries on public data, with 1 TB each month. We check this before the README recommends it (B4, check 3). |
| ⚑ | Example run | A real transcript from the spec 53 demo, shortened, as a text block | A hand-written example. A screenshot. | A real transcript shows the real behavior. A text block can be read and searched. A hand-written example can differ from the real output. |
| | Order of the setup paths | Docker, then uv, then pip | pip first, as in the brief | The architecture decision (section 3): Docker for reviewers on an unknown operating system. The brief's pip path stays available. |
| | Windows shell | PowerShell | Git Bash, cmd | Git Bash changes paths in `-v` options. PowerShell has `$env:APPDATA` and `${PWD}`. |

### B2. Design: the content of `README.md`

**1. Summary.** Three sentences: what the assistant does, for whom, and that the repository has the design and a CLI prototype. Then the prototype image (`docs/design/diagrams/prototype.png`).

**2. Deliverables table.**

| Brief deliverable | Where |
|---|---|
| 1. Architecture diagram | `docs/design/diagrams/` and the HLD, section 1 |
| 2. Technical explanation | `docs/design/hld.md` |
| 3. Working prototype | `src/opsfleet_agent/` and this README |
| 4. CLI | The quick start below |
| 5. Runs on another machine | The setup below |
| 6. Framework and experience | The document of spec 55 |

**3. Prerequisites.**

| Item | Docker path | uv or pip path |
|---|---|---|
| Docker Desktop, OrbStack or Docker Engine | Yes | No |
| Python 3.12 | No | Yes (uv can install it) |
| Google Cloud CLI (`gcloud`) | Yes, for the credentials | Yes |
| A Google Cloud project | Yes | Yes |
| A Gemini API key on the free tier | Yes | Yes |

**4. Google Cloud credentials (ADC).**

Common steps:

1. Install the Google Cloud CLI (the link to the official install page).
2. Run `gcloud auth application-default login`. A browser opens. Sign in with the Google account.
3. Optional: run `gcloud auth application-default set-quota-project <PROJECT_ID>`.
4. If BigQuery says that the API is off, run `gcloud services enable bigquery.googleapis.com --project <PROJECT_ID>`.

One collapsed block for each operating system:

| OS | Install the CLI | ADC folder (to mount in Docker) |
|---|---|---|
| macOS | `brew install --cask gcloud-cli`, or the installer from Google | `~/.config/gcloud` |
| Linux | The apt or dnf package from Google, or the archive | `~/.config/gcloud` |
| Windows | The Google Cloud CLI installer (`.exe`) | `%APPDATA%\gcloud` (PowerShell: `$env:APPDATA\gcloud`) |

The file is `application_default_credentials.json` in that folder.

A short note about the alternative: a service-account key in `GOOGLE_APPLICATION_CREDENTIALS`. In Docker, mount the key as spec 53 describes. A key file is a secret. Do not add it to git.

A short note for a machine without a browser: `gcloud auth application-default login --no-browser`.

**5. A free-tier Gemini key.**

1. Open Google AI Studio (`https://aistudio.google.com/apikey`). Sign in with a Google account.
2. Select "Create API key".
3. Create the key in a new project, or in a Google Cloud project without a billing account.
4. In the key list, check that the key is on the free tier.
5. Write the key in `.env` as `GOOGLE_API_KEY`.

Warning block:

- A key from a project with a billing account is not on the free tier.
- If that billing account is in prepay mode and has no credit, each call returns HTTP 402.
- On the free tier, Google can use the prompts to improve its products. The prototype never sends PII to the model (Req 2).
- The free tier has limits for each minute and each day. The assistant waits and tries again a limited number of times.

**6. Configure `.env`.**

- `cp .env.example .env` (PowerShell: `Copy-Item .env.example .env`).
- Set `GOOGLE_API_KEY` and `GOOGLE_CLOUD_PROJECT`.
- No quotes around the values.
- Leave `GOOGLE_APPLICATION_CREDENTIALS` empty for Docker.

**7. Quick start with Docker.**

1. `mkdir data`
2. `docker build -t opsfleet-agent .`
3. The run command for the operating system: macOS, Linux or Windows. Copy the commands from spec 53, B2, after the spec 53 checks pass.
4. Optional: `docker compose run --rm cli chat --user maya`, if `compose.yaml` exists.

**8. Without Docker.**

- **uv:**
  1. Install uv (the link to the official install page, with the macOS and Linux command and the Windows command).
  2. `uv sync --no-dev`
  3. `uv run opsfleet-agent chat --user maya`
- **pip:**
  1. Create a Python 3.12 virtual environment.
  2. `pip install -r requirements.txt`. Run it in the repository folder, because the file installs the project itself (`-e .`).
  3. `opsfleet-agent chat --user maya`

**9. Example run.** The shortened transcript from the spec 53 demo, steps 1, 4, 3 and 7. A note says that the figures can differ.

**10. What to try.**

- A table of the demo users: `maya` (two brands), `alex` (one brand), `ceo` (all brands). Use the brands from `config/users.yaml`.
- Sample questions from the brief's capabilities: customer behavior, product performance, time-based metrics, schema questions, a multi-step question, a report.
- A table of the CLI commands from spec 37: `/reports`, `/export`, `/trace`, `/stats`, `/help`, `/exit`.

**11. Cost and limits for the reviewer.**

- The limits table of spec 00, A4, in short form.
- The cost: free-tier Gemini calls, and about 10 MB billed for each small query.
- `config/settings.yaml` holds the limits.

**12. Runtime data.**

- All runtime data is in `data/`: the database, the checkpoints, the traces and the report files.
- Git and Docker ignore `data/`.
- To start again with no data, delete the folder.

**13. Documents.** Links to the HLD, `architecture.md`, `product-definition.md`, `roadmap.md`, `gap-analysis.md`, the specs, the brief, and the experience document.

**14. Troubleshooting.** A table with three columns: symptom, cause, fix. Rows:

| Symptom | Cause | Fix |
|---|---|---|
| Gemini returns HTTP 402 | The key is from a project with billing in prepay mode, with no credit | Create a key in a project without billing (section 5) |
| Gemini returns HTTP 429 | The free-tier limit for each minute or each day | Wait one minute. For the daily limit, wait until the next day (Pacific time). |
| "API key not valid" | A wrong key, or quotes around the value in `.env` with Docker | Copy the key again. Write the value without quotes. |
| The ADC message at start, or `DefaultCredentialsError` | No `gcloud auth application-default login`, or a wrong mount path | Run the login. Check the ADC folder for the operating system (section 4). |
| BigQuery: "Access Denied … bigquery.jobs.create" | `GOOGLE_CLOUD_PROJECT` is a project where the account cannot run jobs | Use a project where the account has the BigQuery Job User role, or the owner role |
| BigQuery: "API has not been used in project … or it is disabled" | The BigQuery API is off in the project | `gcloud services enable bigquery.googleapis.com --project <PROJECT_ID>` |
| "the input device is not a TTY" | The run has no `-it`, or Git Bash on Windows | Add `-it`. On Windows, use PowerShell. |
| "Permission denied" in `data/` on Linux | The container user is not the host user | Add `--user "$(id -u):$(id -g)"` |
| "exec format error", or a platform warning | The image is for another CPU type | Build the image on this machine (section 7) |
| "The daily limit … is reached" | The daily limit of the prototype (spec 00) | Wait until midnight UTC, or change `config/settings.yaml` |
| "This query reads too much data" | The 1 GB cap for each query | Ask about a shorter period or fewer brands |
| A warning about "end user credentials … without a quota project" | ADC has no quota project | Optional: `gcloud auth application-default set-quota-project <PROJECT_ID>` |
| pip: "requires a different Python" | The Python version is not 3.12 or later | Use Python 3.12 |

**15. Development.** The current development setup (uv, pre-commit, the checks) in short form. It is for developers, not reviewers.

### B3. Files and interfaces

```text
README.md              # replaced by the content of B2
docs/design/diagrams/prototype.png   # from spec 11
.env.example           # no change. The README refers to it.
```

The README uses relative links only, so the links work in the public repository and in a clone.

### B4. Tests

1. **Clean clone, Docker.** Clone the repository into an empty folder. Follow the README on macOS (this Mac) with Docker. Get the first answer. Record the time.
2. **Linux, natively.** Follow the Linux Docker path and the pip path on a Linux machine. Option: an OrbStack Linux machine on this Mac (arm64), with the owner's approval. Record the result.
3. **No-billing project.** With the owner's approval, check the BigQuery sandbox in a project without billing:
   - one dry run,
   - one query of about 10 MB with `maximum_bytes_billed`,
   - one job label.

   If a check fails, the README recommends a project with billing for BigQuery, and keeps the no-billing project for the key only.
4. **Windows.** We have no Windows machine. A person checks the PowerShell commands by reading them. The README says so in the Windows block, until a run on Windows passes.
5. **Links and images.** Check all relative links with a Markdown link checker (for example `lychee --offline README.md`). Check the image on GitHub after the repository becomes public, or in a local Markdown preview before.
6. **Transcript check.** Search the example run for the key, the project ID, `@` signs, and street patterns. There must be no result.
7. **Style.** `python3 -I ~/.claude/skills/asd-ste100/scripts/ste-lint.py README.md` gives no hard violations.

### B5. Cost

- Check 1: about 3 Gemini calls and 1–2 queries (about 10–20 MB billed).
- Check 2: about 6 Gemini calls and 2–4 queries.
- Check 3: 1 dry run (free), 1 query (about 10 MB billed). Inside the sandbox, the cost is zero.
- The example run uses the transcript of spec 53. It needs no new calls.
- Total: about 9 Gemini calls (free-tier key) and less than 100 MB of BigQuery.

### B6. Risks and open points

- **Windows is not tested on a real machine.** The PowerShell commands can have an error that a reading does not find. The README states this.
- **Google pages change.** The AI Studio screens, the free-tier rules and the HTTP 402 behavior can change. The README links to the official pages and states the date of the check.
- **BigQuery sandbox limits.** The sandbox has its own limits, for example on storage and on some features. The prototype only reads public data, so the risk is low. Check 3 tests it.
- **The current README** is a development README. This spec replaces it. The development setup moves to section 15.
- **Free-tier terms.** The README states the data use of the free tier. The text must match the current Gemini API terms on the date of the check (gap analysis B3 says to check the current terms).

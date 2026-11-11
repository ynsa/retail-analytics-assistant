# Spec 11: Draw.io diagrams

- **Status:** Done (2026-11-11): `docs/design/diagrams/` and `scripts/export-diagrams.sh`.
- **Phase and stream:** Phase 2, stream A
- **Depends on:** 00, 01. Spec 10 uses the images.
- **Requirements covered:** deliverable 1 (architecture diagram). The diagrams show the components of Req 1–8.

---

## Part A — Product (review this part)

### A1. Goal

The reviewer sees the system in two pictures. The first picture shows the production system on Google Cloud, with the official Google Cloud icons. The second picture shows the prototype on one machine. The pictures show the same components and flows as the Mermaid diagrams in `docs/product/architecture.md`. The reviewer can open the source files in draw.io and change them.

### A2. Behavior the user sees

1. **Production picture in the HLD.** The reviewer opens `docs/design/hld.md`. Near the start, the reviewer sees the production picture with Google Cloud icons. Numbers on the arrows (1, 2, 3 …) match the rows of the data flow table under the picture.
2. **Prototype picture in the README.** The README shows the prototype picture. It shows the CLI, the assistant core, Gemini, BigQuery, and the local files in `data/`. It also shows what is inside the Docker container and what comes from the host.
3. **Readable in both themes.** The pictures have a white background. They are readable on GitHub in the light theme and in the dark theme.
4. **Editable source.** Under each picture, a link opens the `.drawio` file. The reviewer can open it in draw.io or at app.diagrams.net. The PNG file also contains a copy of the diagram, so draw.io can open the PNG file directly.
5. **Same as the Mermaid diagrams.** Each box in a picture has the same name as a node in the matching Mermaid diagram. The reviewer does not see two different designs.

### A3. Scope

- **In scope:**
  - Two `.drawio` source files.
  - One PNG image for each source file.
  - A legend in each picture.
  - The flow numbers in the production picture.
  - A repeatable export command.
  - A list of the differences between the pictures and the Mermaid diagrams, if any.
- **Out of scope:**
  - The HLD text (spec 10).
  - The README text (spec 54).
  - New components that are not in `architecture.md`. A new component needs a change to `architecture.md` first.
  - Sequence diagrams. They stay in Mermaid in the HLD.

### A4. Limits the user sees

- One page for each diagram. No hidden layers.
- At most about 25 boxes in the production picture and 20 in the prototype picture. More detail goes into the HLD text.
- Labels use the same words as the Mermaid diagrams. Labels have at most 6 words on one line.
- Each PNG file is smaller than 1,000 KB. The pre-commit hook `check-added-large-files` blocks larger files.

### A5. Done when

- [ ] `docs/design/diagrams/production-gcp.drawio` and `docs/design/diagrams/prototype.drawio` exist and open in draw.io without errors.
- [ ] `production-gcp.png` and `prototype.png` exist. Each one is smaller than 1,000 KB and at least 1,600 px wide.
- [ ] Each Google Cloud service in the production picture uses its icon from the draw.io Google Cloud library (`gcp2`). The list is in B2.
- [ ] Each box in the production picture matches a node in `architecture.md` section 0.4. Each box in the prototype picture matches a node in section 0.1. B4 describes the check.
- [ ] The production picture shows a number on each flow. The HLD data flow table uses the same numbers.
- [ ] Each picture has a legend for the line types and the box types.
- [ ] The pictures are readable on GitHub in the light theme and in the dark theme.
- [ ] The PNG files contain the diagram copy, so draw.io opens them as editable diagrams.
- [ ] One command exports both images again (`scripts/export-diagrams.sh`).
- [ ] The README and the HLD show the images, with a link to the source under each image.

---

## Part B — Technical

### B1. Key decisions

| | Decision | Choice | Alternatives | Reason |
|---|---|---|---|---|
| | Export tool | The draw.io desktop app and its command line `drawio`. The owner installed it on 2026-11-11 (version 32.6.10). | A third-party Docker image for draw.io export. A manual export each time. | The official app gives a repeatable export with one command. A third-party image is code from an unknown source. |
| ⚑ | Image format | PNG at scale 2, white background, with the diagram copy in the file | SVG | draw.io SVG files use `foreignObject` for HTML labels. Some viewers (IDE previews, some Markdown tools) do not show that text. PNG shows the same in all viewers. |
| | Location | `docs/design/diagrams/` | `docs/diagrams/` | The HLD is in `docs/design/` (spec 10). Short relative links. |
| | Source format | Uncompressed XML in the `.drawio` file | Compressed XML (the draw.io default in some versions) | A reviewer can read a git diff of uncompressed XML. |
| | Icons | The "GCP Icons" palettes and the "Google Cloud Platform" product cards in the draw.io `gcp2` library | Icons downloaded from the Google Cloud website | The task asks for the draw.io library. The icons are the same official images, inside draw.io. |

**Check of the tools on this Mac (2026-11-11):**

| Check | Result |
|---|---|
| `ls /Applications/draw.io.app` | Installed by the owner on 2026-11-11. |
| `command -v drawio` | `/opt/homebrew/bin/drawio` |
| `drawio --version` | `32.6.10` |
| `uname -m` | `arm64` (Apple silicon), macOS 26.6.2 |

**Check of the icon library (2026-11-11).** We read `Sidebar-GCP2.js` in the draw.io source on GitHub (`jgraph/drawio`, branch `dev`).

- The search library `gcp2` has the "GCP Icons / …" palettes and the "Google Cloud Platform / …" palettes.
- The "GCP Icons" entries are images. Their style is `shape=image;image=data:image/svg+xml,…`. They are not short stencil names. Only a few shapes have a stencil name, for example `mxgraph.gcp2.zones` for the boundary boxes.
- Each service in the production picture has an icon. The table in B2 lists the palette for each one.

### B2. Design

**Production picture (`production-gcp.drawio`).** Left to right: people, channels, the Google Cloud boundary.

| Box (same name as in `architecture.md` 0.4) | Icon | Palette in draw.io |
|---|---|---|
| Managers and CEO, Persona editor, Analysts | User icon | GCP Icons / general icons (`gcp2`) |
| Web chat, Google Chat or Slack | Generic box | none (not a Google Cloud service) |
| Identity-Aware Proxy | Identity-Aware Proxy product card | Google Cloud Platform / Security |
| Cloud Run: FastAPI agent API + LangGraph | Cloud Run | GCP Icons / Compute |
| Admin page: persona and tone | Cloud Run | GCP Icons / Compute |
| Vertex AI Gemini | Vertex AI | GCP Icons / AI and Machine Learning |
| BigQuery warehouse | BigQuery | GCP Icons / Data Analytics |
| BigQuery vector search | BigQuery | GCP Icons / Data Analytics |
| BigQuery log sink | BigQuery | GCP Icons / Data Analytics |
| Cloud SQL PostgreSQL | Cloud SQL | GCP Icons / Databases |
| Cloud Storage: persona and prompt config | Cloud Storage | GCP Icons / Storage |
| Cloud Storage: Golden Bucket trios | Cloud Storage | GCP Icons / Storage |
| Cloud Run job: ingest and embed | Cloud Run | GCP Icons / Compute |
| Cloud Run job: purge deleted reports | Cloud Run | GCP Icons / Compute |
| Cloud Scheduler | Cloud Scheduler | GCP Icons / Integration Services |
| Secret Manager | Secret Manager | GCP Icons / Security |
| Cloud Logging, Trace and Monitoring | One icon for each of the three | GCP Icons / Operations |

Boundaries: a "Google Cloud" box, a "Project" box and a "Region: US" box. Use the zone shapes (`mxgraph.gcp2.zones`) of the `gcp2` library.

**Flow numbers.** One number for each flow. The HLD data flow table (spec 10, section 1) uses the same numbers.

| # | Flow |
|---|---|
| 1 | Managers and CEO → channel → IAP → agent API |
| 2 | Agent API → Vertex AI Gemini |
| 3 | Agent API → BigQuery warehouse (read only, scoped) |
| 4 | Agent API → Cloud SQL (reports, checkpoints, preferences, audit, brand access) |
| 5 | Agent API → BigQuery vector search (similar trios) |
| 6 | Agent API → persona config (read, 60 s cache) |
| 7 | Persona editor → IAP → admin page → persona config |
| 8 | Analysts → Golden Bucket → ingest job → vector search |
| 9 | Cloud Scheduler → ingest job and purge job |
| 10 | Secret Manager → agent API |
| 11 | Agent API → Cloud Logging, Trace and Monitoring → BigQuery log sink |

**Prototype picture (`prototype.drawio`).** The same nodes as `architecture.md` section 0.1:

- People and interfaces: the tester or manager, the CLI (Typer + Rich), the local web UI, the local FastAPI app.
- The assistant core (LangGraph), as one group box. Inside: the agent graph, the SQL guard, the result masker and output scanner, the report tools, the report renderer.
- Outside services: the Gemini API (AI Studio, free tier), the BigQuery wrapper, `BigQueryRunner` (`bq_client.py`, unchanged), BigQuery `thelook_ecommerce`.
- Local files: SQLite, `config/users.yaml`, `traces.jsonl`, `data/reports/`.

Additions that show the run environment. They add no component:

- A dashed boundary "Docker container (optional)" around the CLI, the FastAPI app and the core.
- Three inputs from the host: `.env` (secrets), the gcloud ADC folder (read only), and the `data/` folder (one volume). These match spec 53.
- The local web UI has a dashed border and the label "nice to have".
- BigQuery uses the `gcp2` BigQuery icon. Gemini uses a generic box, because the AI Studio API is not a Google Cloud console service.

**Style rules for both pictures:**

- White background. Dark text. No transparent export.
- Solid arrows for requests and data. Dashed arrows for scheduled or background flows. The legend says so.
- One font size for labels (12 pt) and one for boundaries (14 pt, bold).
- Grid on, and boxes aligned to the grid.
- Labels in plain English, the same words as the Mermaid nodes.

**How to make the files.** The author can draw in the draw.io app or write the XML directly. For each Google Cloud icon, the style text must come from the draw.io `gcp2` library. Do not draw a new icon.

**Export:**

```bash
DRAWIO=/Applications/draw.io.app/Contents/MacOS/draw.io
for name in production-gcp prototype; do
  "$DRAWIO" --export --format png --scale 2 --border 20 --embed-diagram \
    --output "docs/design/diagrams/$name.png" "docs/design/diagrams/$name.drawio"
done
```

Fallback without the app:

1. Open the `.drawio` file at app.diagrams.net. The app runs in the browser and does not send the file to a server.
2. Select File → Export as → PNG.
3. Set zoom 200% and border 20.
4. Select "Include a copy of my diagram".
5. Do not select "Transparent background".

### B3. Files and interfaces

```text
docs/design/diagrams/
  production-gcp.drawio    # source, uncompressed XML, one page
  production-gcp.png       # export, scale 2, diagram copy inside
  prototype.drawio
  prototype.png
scripts/
  export-diagrams.sh       # the export command in B2
```

The HLD and the README embed the images:

```markdown
![Production on Google Cloud](docs/design/diagrams/production-gcp.png)

Source: [production-gcp.drawio](docs/design/diagrams/production-gcp.drawio)
```

### B4. Tests

1. **Opens and exports.** `scripts/export-diagrams.sh` exits with code 0. The export also proves that draw.io can read the file.
2. **Valid XML.** `python3 -c "import xml.etree.ElementTree as E; E.parse('docs/design/diagrams/production-gcp.drawio')"` gives no error. The same for `prototype.drawio`. The root element `mxfile` contains a `diagram` element with an `mxGraphModel` child (uncompressed).
3. **Same names as Mermaid.** List the labels of the boxes in each `.drawio` file. List the node labels of `architecture.md` sections 0.1 and 0.4. Compare the two lists. A missing or extra box is a defect, except the boundaries and the legend.
4. **Official icons.** Each Google Cloud box in the production file has a style with `image=data:image/svg+xml` from the `gcp2` palette, or a `mxgraph.gcp2` shape.
5. **Size.** Each PNG file is smaller than 1,000 KB (`ls -l`). Each one is at least 1,600 px wide (`sips -g pixelWidth` on macOS).
6. **Themes.** Look at the README on GitHub in the light theme and in the dark theme. All text is readable.
7. **Editable PNG.** Open the PNG file in draw.io. The diagram opens as editable boxes.

### B5. Cost

- 0 Gemini calls. 0 BigQuery queries.
- The draw.io desktop app is free. The install needs about 200 MB of disk space and the owner's approval.

### B6. Risks and open points

- **Gaps in `architecture.md` section 0.4.** The Mermaid diagram does not show three parts that the text of `architecture.md` describes:
  - the Cloud Storage bucket for report files (section 2),
  - the embedding call from the ingest job to Vertex AI (section 2),
  - the alerts in Cloud Monitoring (section 7).

  The pictures must match the Mermaid diagram. So the owner must first decide whether to add these parts to section 0.4. Then both diagrams change together.
- **Icon set age.** The draw.io `gcp2` icons can be older than the current Google Cloud icons. The task asks for the `gcp2` library, so we accept this.
- **Missing icon.** If a future service has no icon in `gcp2`, use a generic box with the service name. Write the reason in the legend.
- **Manual drift.** A change to a Mermaid diagram in `architecture.md` needs a change to the `.drawio` file and a new export. The check in B4 (step 3) finds the drift.
- **Export without the app.** If the owner does not approve the install, the export is manual. Then the export is not repeatable with one command.

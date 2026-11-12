# Spec 52: Local test UI — build

- **Status:** Done (2026-11-12)
- **Phase and stream:** Phase 3 (after the prototype works in the CLI)
- **Depends on:** 40 (screens and API), 31–37
- **Requirements covered:** none from OpsFleet. Our own tool for manual testing.

---

## Part A — Product (review this part)

### A1. Goal

Build the local test UI exactly as spec 40 and its sketches describe. The build starts only after the CLI passes the safety tests (spec 22), so the UI cannot delay the required scope.

### A2. Behavior the user sees

The five screens of spec 40, with the real assistant.

### A3. Scope

- **In scope:** the API, the page, the SSE stream, screenshots for the PR, the `opsfleet-agent ui` command, and the UI in the Docker image (`-p 8000:8000`).
- **Out of scope:** everything that spec 40 puts out of scope.

### A4. Limits the user sees

The same limits as the CLI (spec 00).

### A5. Done when

- [ ] All checks in spec 40, section A5, pass. (All except the safety set through the UI API: see spec 40, B7, deviation 12.)
- [x] The PR has a screenshot of each screen from a real run (`docs/images/ui/`).
- [ ] The UI works in the Docker image on `linux/amd64` and `linux/arm64`. (Moved to spec 53, which builds the image. The command is ready: `opsfleet-agent ui --host 0.0.0.0`.)

---

## Part B — Technical

### B1. Key decisions

All key decisions are in spec 40.

### B2. Design

See spec 40, section B2.

### B3. Files and interfaces

See spec 40, section B3. The server binds to `127.0.0.1` on the host. In Docker, it binds to `0.0.0.0` inside the container, and the `docker run` command publishes the port only on `127.0.0.1` of the host (`-p 127.0.0.1:8000:8000`).

### B4. Tests

The API tests and the Playwright screenshots from spec 40. The safety set of spec 22 runs once through the UI API.

### B5. Cost

About 15 Gemini calls and 5 small queries for the manual check. The safety run through the API costs the same as the CLI run of spec 22.

### B6. Risks and open points

- If time is short, we skip this spec. The sketches stay in spec 40 as a design.

### B7. Implementation notes (2026-11-12)

- The build, the deviations, the tests and the live check are in [spec 40, B7](../40-local-ui/spec.md#b7-implementation-notes-2026-11-12).
- New dependencies: `fastapi` and `uvicorn` (runtime), `httpx` (development, for the FastAPI `TestClient`).
- Command: `uv run opsfleet-agent ui [--port 8000] [--host 127.0.0.1]`.
- Docker: not done here. Spec 53 builds the image and runs the UI with `--host 0.0.0.0` inside the container and `-p 127.0.0.1:8000:8000`.
- The safety set of spec 22 through the UI API: not run (budget of 10 Gemini calls). Spec 50 can run it.
- B5, cost: the live check used 10 Gemini calls and 83.9 MB (4 queries). The estimate was 15 calls.

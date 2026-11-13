# Spec 55: Framework choice and experience document

- **Status:** Done (2026-11-13)
- **Phase and stream:** Phase 3
- **Depends on:** the architecture decision N21 (LangGraph)
- **Requirements covered:** deliverable 6 of the brief: "Use a framework of your choice, explain why you choose it and your level of experience with it."

---

## Part A — Product (review this part)

### A1. Goal

A short document for the OpsFleet reviewers. It explains why the project uses LangGraph and states the author's level of experience with it. The author writes the experience part. Claude prepares the structure and the technical reasons only.

### A2. Behavior the user sees

The reviewer finds `docs/framework-choice.md` through a link in the README. The document has three parts:

1. **Why LangGraph:** a summary of the comparison in `docs/product/architecture.md`, section 1.1, and the five reasons.
2. **How the project uses it:** the graph, the `interrupt()` confirmation, the checkpointer, the self-correction cycle, and the Mermaid export of the real graph.
3. **My experience:** the author's own text. Claude does not invent it.

### A3. Scope

- **In scope:** the document and the link in the README.
- **Out of scope:** any claim about experience that the author did not write.

### A4. Limits the user sees

One page.

### A5. Done when

- [x] The document exists and the README links to it.
- [x] Part 3 contains only the author's own words.
- [x] The graph diagram in part 2 comes from the real code (`draw_mermaid()`), not from a drawing by hand.

---

## Part B — Technical

### B1. Key decisions

None. The framework decision is closed (N21).

### B2. Design

Claude drafts parts 1 and 2. The author gives the facts for part 3 (2026-11-13): LangGraph on multiple projects, more control, and support for models from different providers. Claude writes these facts as text and adds no other claims.

### B3. Files and interfaces

`docs/framework-choice.md`, and one link in `README.md`.

### B4. Tests

The link check in the README review (spec 54).

### B5. Cost

None.

### B6. Risks and open points

None.

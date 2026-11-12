"""Long conversations and memory (spec 34).

- `messages.py`: the recent window, its items and the reducer (`append`, `compact`).
- `pinned.py`: the pinned facts that code writes.
- `context_builder.py`: the context of each model call, with the token budget.
- `summary.py`: the rolling summary of older turns, and its fallback.
- `result_cache.py`: the full masked rows of each result, for 30 days.
- `conversations.py`: the conversation log, the list and the resume.
"""

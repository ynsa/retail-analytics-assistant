"""A checkpoint saver that does not stop the turn when a write fails (spec 36, B2.6 #5).

LangGraph writes a checkpoint after each step. When the SQLite file is locked or the disk is full,
the write raises, and without this wrapper the user loses the answer. `GuardedSaver` delegates all
calls to the real saver. A failed write is logged and remembered: the session then adds the note
"This turn is not saved." to the answer. Reads are not guarded: a failed read is a real error.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

_logger = logging.getLogger("opsfleet_agent")


class GuardedSaver(BaseCheckpointSaver):
    def __init__(self, inner: BaseCheckpointSaver) -> None:
        super().__init__(serde=inner.serde)
        self.inner = inner
        self.write_failed = False

    # --- writes: guarded ---------------------------------------------------------------------------
    def put(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        try:
            return self.inner.put(config, checkpoint, metadata, new_versions)
        except Exception:  # noqa: BLE001 - the answer must still show (spec 36, B2.6 #5)
            self._failed("put")
            return {
                "configurable": {
                    **config.get("configurable", {}),
                    "checkpoint_id": checkpoint.get("id"),
                }
            }

    def put_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        try:
            self.inner.put_writes(config, writes, task_id, task_path)
        except Exception:  # noqa: BLE001 - the answer must still show (spec 36, B2.6 #5)
            self._failed("put_writes")

    def _failed(self, what: str) -> None:
        self.write_failed = True
        _logger.exception("checkpoint %s failed", what)

    # --- reads and other calls: delegated ---------------------------------------------------------
    def get_tuple(self, config: Any) -> Any:
        return self.inner.get_tuple(config)

    def list(self, config: Any, **kwargs: Any) -> Iterator[Any]:
        return self.inner.list(config, **kwargs)

    def delete_thread(self, thread_id: str) -> None:
        self.inner.delete_thread(thread_id)

    def get_next_version(self, current: Any, channel: Any) -> Any:
        return self.inner.get_next_version(current, channel)

    @property
    def config_specs(self) -> list[Any]:
        return self.inner.config_specs

"""ConversationRecorder: the one entry point agents use to persist and resume conversations.

Facade over ChatMetadataRepository + ChatHistoryRepository (both injected), plus a small adapter between
ChatHistoryRecord and LangChain messages. Every call is owner-scoped by the trusted `user_id` (app-layer RLS).
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from shared.persistence.chat_history import (
    BaseChatHistoryRepository,
    ChatHistoryRecord,
    ContentBlock,
    MessageRole,
    SequenceConflictError,
)
from shared.persistence.chat_metadata import (
    BaseChatMetadataRepository,
    ChatMetadataRecord,
    ChatMode,
    SessionNotFoundError,
    TitleSource,
)
from shared.persistence.ownership import verify_session_owner

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

log = logging.getLogger(__name__)

# (first_prompt, first_answer) -> short title, or None to keep the provisional one
TitleGenerator = Callable[[str, str], Awaitable[str | None]]

PROVISIONAL_TITLE_CHARS = 80
GENERATED_TITLE_CHARS = 60
DEFAULT_TITLE = "New chat"
_SEQUENCE_RETRIES = 3
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def clean_title(raw: str, max_chars: int) -> str:
    """Strip control characters / quotes, collapse whitespace, cut to `max_chars`."""
    text = _CONTROL_CHARS.sub(" ", raw or "")
    text = " ".join(text.split()).strip().strip("\"'`")
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _text_of(record: ChatHistoryRecord) -> str:
    return "\n\n".join(
        b.body for b in record.content_blocks if b.block_type == "text" and isinstance(b.body, str)
    )


def to_langchain(records: list[ChatHistoryRecord]) -> list[BaseMessage]:
    """Adapter: stored turns -> LangChain messages (HUMAN->HumanMessage, AI->AIMessage, text blocks only)."""
    from langchain_core.messages import AIMessage, HumanMessage

    messages: list[BaseMessage] = []
    for record in records:
        text = _text_of(record)
        if not text:
            continue
        if record.role == MessageRole.HUMAN:
            messages.append(HumanMessage(content=text, id=record.message_id))
        elif record.role == MessageRole.AI:
            messages.append(AIMessage(content=text, id=record.message_id))
    return messages


class ConversationRecorder:
    """Persist one conversation turn at a time; rehydrate an old chat on a cold runtime."""

    def __init__(
        self,
        metadata: BaseChatMetadataRepository,
        history: BaseChatHistoryRepository,
        title_generator: TitleGenerator | None = None,
    ) -> None:
        self._metadata = metadata
        self._history = history
        self._title_generator = title_generator
        self._background: set[asyncio.Task[None]] = set()

    # -- public API ------------------------------------------------------------------------------------------

    async def start_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        prompt: str,
        agent_type: str | None = None,
        mode: str | None = None,
        model_id: str | None = None,
        document_type: str | None = None,
        analysis_type: str | None = None,
    ) -> ChatMetadataRecord:
        """Ensure the caller owns `session_id` (creating its metadata row on first use) and record the prompt.

        Raises SessionNotFoundError if the session id already belongs to another user. Errors propagate: a turn
        whose prompt cannot be recorded (or whose owner cannot be proven) must not run.
        """
        return await asyncio.to_thread(
            self._start_turn_sync,
            user_id,
            session_id,
            prompt,
            agent_type,
            mode,
            model_id,
            document_type,
            analysis_type,
        )

    async def finish_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        text: str,
        ui_surface: dict[str, Any] | None = None,
        citations: list[dict[str, Any]] | None = None,
        thinking: str | None = None,
        first_prompt: str | None = None,
        message_id: str | None = None,
        completion_status: str | None = None,
    ) -> int | None:
        """Record the agent's answer; returns its sequence (None on failure). Never raises: the user still gets
        the answer if persistence fails."""
        try:
            sequence = await asyncio.to_thread(
                self._append,
                user_id,
                session_id,
                MessageRole.AI,
                self._ai_blocks(text, ui_surface, thinking),
                citations or [],
                message_id,
                completion_status,
            )
        except Exception as exc:  # noqa: BLE001 — logged, never surfaced to the user
            log.error("chat history: failed to record AI turn session=%s error_type=%s", session_id, type(exc).__name__)
            return None
        if sequence == 2 and self._title_generator is not None and first_prompt:
            task = asyncio.create_task(self._generate_title(user_id, session_id, first_prompt, text))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return sequence

    async def record_human_action(
        self, *, user_id: str, session_id: str, text: str, ui_surface: dict[str, Any] | None = None
    ) -> None:
        """Record a HITL answer (`op: resume`) as a HUMAN turn.

        Raises SessionNotFoundError if the session belongs to another user; skips (logged) if this user has no
        metadata row for it (e.g. its first turn was never persisted).
        """
        await asyncio.to_thread(self._assert_owner, user_id, session_id)
        if await asyncio.to_thread(self._metadata.get, user_id, session_id) is None:
            log.warning("chat history: HITL answer for unrecorded session=%s skipped", session_id)
            return
        blocks = [ContentBlock(block_type="text", body=text)]
        if ui_surface:
            blocks.append(ContentBlock(block_type="ui", body=ui_surface))
        await asyncio.to_thread(self._append, user_id, session_id, MessageRole.HUMAN, blocks, [])

    async def assert_owner(self, *, user_id: str, session_id: str) -> None:
        """Fail-closed ownership check, run before any other session work.

        Raises SessionNotFoundError (someone else's session) or OwnershipCheckError (owner lookup failed).
        """
        await asyncio.to_thread(self._assert_owner, user_id, session_id)

    async def session_metadata(self, *, user_id: str, session_id: str) -> ChatMetadataRecord | None:
        """Read settings for the authorized owner, including newly updated modes."""
        await asyncio.to_thread(self._assert_owner, user_id, session_id)
        try:
            return await asyncio.to_thread(self._metadata.get, user_id, session_id)
        except Exception as exc:
            raise OwnershipCheckError("Could not read session settings") from exc

    async def initialize_turn_settings(self, *, user_id: str, session_id: str,
                                       mode: str | None, model_id: str | None) -> ChatMetadataRecord:
        try:
            return await asyncio.to_thread(self._metadata.initialize_turn_settings, user_id, session_id,
                                           mode=mode, model_id=model_id)
        except SessionNotFoundError:
            raise
        except Exception as exc:
            raise OwnershipCheckError("Could not initialize session settings") from exc

    async def rehydrate(self, *, user_id: str, session_id: str, n_turns: int = 20) -> list[BaseMessage]:
        """The last `n_turns` stored messages as LangChain messages (oldest first). Empty on any failure."""
        try:
            records = await asyncio.to_thread(self._history.load_recent, user_id, session_id, n_turns)
        except Exception as exc:  # noqa: BLE001 — a cold start without memory beats a failed turn
            log.error("chat history: rehydrate failed session=%s error_type=%s", session_id, type(exc).__name__)
            return []
        return to_langchain(records)

    async def drain(self) -> None:
        """Wait for background title tasks (tests, graceful shutdown)."""
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # -- internals -------------------------------------------------------------------------------------------

    def _assert_owner(self, user_id: str, session_id: str) -> None:
        # The metadata row is keyed (user_id, session_id), so it cannot see a colliding id owned by someone
        # else; the history partition's stamped owner can.
        try:
            verify_session_owner(self._history, user_id, session_id)
        except SessionNotFoundError:
            log.warning("chat history: refused foreign session session=%s", session_id)
            raise

    def _start_turn_sync(
        self,
        user_id: str,
        session_id: str,
        prompt: str,
        agent_type: str | None,
        mode: str | None,
        model_id: str | None,
        document_type: str | None,
        analysis_type: str | None,
    ) -> ChatMetadataRecord:
        if not user_id:
            raise ValueError("user_id is required")
        self._assert_owner(user_id, session_id)
        now = datetime.now(UTC)
        meta = self._metadata.get_or_create(
            ChatMetadataRecord(
                session_id=session_id,
                user_id=user_id,
                title=clean_title(prompt, PROVISIONAL_TITLE_CHARS) or DEFAULT_TITLE,
                title_source=TitleSource.PROMPT,
                agent_type=agent_type,
                document_type=document_type,
                analysis_type=analysis_type,
                model_id=model_id,
                mode=ChatMode(mode) if mode in ChatMode._value2member_map_ else None,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            )
        )
        # The SPA creates the row first through POST /sessions, usually without agent_type; stamp it here so
        # History can reopen the chat in this runtime. Never overwrite a value that is already set.
        # Only a row with no owning agent yet is stamped; a later turn never rewrites how a session started.
        missing = {
            field: value
            for field, value in (
                ("agent_type", agent_type), ("document_type", document_type), ("analysis_type", analysis_type),
            )
            if value and getattr(meta, field, None) is None
        } if agent_type and meta.agent_type is None else {}
        if missing and hasattr(self._metadata, "initialize_routing"):
            meta = self._metadata.initialize_routing(user_id, session_id, **missing)
        self._append(user_id, session_id, MessageRole.HUMAN, [ContentBlock(block_type="text", body=prompt)], [])
        return meta

    def _append(
        self,
        user_id: str,
        session_id: str,
        role: MessageRole,
        blocks: list[ContentBlock],
        citations: list[dict[str, Any]],
        message_id: str | None = None,
        completion_status: str | None = None,
    ) -> int:
        """Allocate the next sequence number from the metadata counter and write one message item."""
        for _ in range(_SEQUENCE_RETRIES):
            sequence = self._metadata.increment_message_count(
                user_id, session_id, user_activity=role == MessageRole.HUMAN
            )
            record = ChatHistoryRecord(
                session_id=session_id,
                message_id=message_id or uuid.uuid4().hex,
                role=role,
                user_id=user_id,
                content_blocks=blocks,
                citations=citations,
                timestamp=datetime.now(UTC),
                sequence_number=sequence,
                completion_status=completion_status,
            )
            try:
                self._history.append(record)
                return sequence
            except SequenceConflictError:
                # counter drifted behind the partition (e.g. a legacy writer); the next increment skips past it
                log.warning("chat history: sequence %s taken, re-allocating session=%s", sequence, session_id)
        raise SequenceConflictError(f"{session_id}: no free sequence after {_SEQUENCE_RETRIES} attempts")

    @staticmethod
    def _ai_blocks(text: str, ui_surface: dict[str, Any] | None, thinking: str | None) -> list[ContentBlock]:
        blocks = [ContentBlock(block_type="text", body=text or "")]
        if ui_surface:
            blocks.append(ContentBlock(block_type="ui", body=ui_surface))
        if thinking:
            blocks.append(ContentBlock(block_type="thinking", body=thinking))
        return blocks

    async def _generate_title(self, user_id: str, session_id: str, prompt: str, answer: str) -> None:
        assert self._title_generator is not None
        try:
            raw = await self._title_generator(prompt, answer)
            title = clean_title(raw or "", GENERATED_TITLE_CHARS)
            if title:
                await asyncio.to_thread(self._metadata.set_generated_title, user_id, session_id, title)
        except Exception as exc:  # noqa: BLE001 — the provisional prompt title stays
            log.error("chat history: title generation failed session=%s error_type=%s", session_id, type(exc).__name__)

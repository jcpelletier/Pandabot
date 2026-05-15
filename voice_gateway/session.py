"""
Session management for voice gateway.

Maintains per-device conversation history in OpenAI message format.
Sessions are pruned after 1 hour of inactivity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MAX_MESSAGES = 20  # keep last 20 messages (10 turns)
SESSION_TIMEOUT = timedelta(hours=1)


@dataclass
class ConversationSession:
    history: list[dict] = field(default_factory=list)
    last_active: datetime = field(default_factory=datetime.utcnow)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ConversationSession] = {}

    def get_or_create(self, device_id: str) -> ConversationSession:
        if device_id not in self._sessions:
            logger.info("Creating new session for device %s", device_id)
            self._sessions[device_id] = ConversationSession()
        return self._sessions[device_id]

    def add_turn(self, device_id: str, user_text: str, assistant_text: str) -> None:
        session = self.get_or_create(device_id)
        session.history.append({"role": "user", "content": user_text})
        session.history.append({"role": "assistant", "content": assistant_text})
        # Rolling window: keep last MAX_MESSAGES messages
        if len(session.history) > MAX_MESSAGES:
            session.history = session.history[-MAX_MESSAGES:]
        session.last_active = datetime.utcnow()
        self.prune()

    def get_history(self, device_id: str) -> list[dict]:
        session = self.get_or_create(device_id)
        return list(session.history)

    def prune(self) -> None:
        """Remove sessions inactive for more than SESSION_TIMEOUT."""
        now = datetime.utcnow()
        expired = [
            device_id
            for device_id, session in self._sessions.items()
            if (now - session.last_active) > SESSION_TIMEOUT
        ]
        for device_id in expired:
            logger.info("Pruning expired session for device %s", device_id)
            del self._sessions[device_id]

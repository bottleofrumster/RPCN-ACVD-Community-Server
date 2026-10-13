"""Bounded live hire bindings; no durable team changes or reward authority."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .udp import GameSession

MAX_FEEDBACK_CONTEXTS = 256


@dataclass
class HireFeedbackContext:
    """Recent accepted relationship; no live matchmaking or payment authority."""

    employer_online_id: str
    employer_id: int
    mercenary_online_id: str
    mercenary_id: int
    expires: float
    request_key: str | None = None


@dataclass(frozen=True)
class HireBinding:
    employer: GameSession
    mercenary: GameSession
    employer_id: int
    mercenary_id: int
    employer_team: int
    mercenary_team: int
    expires: float


class HireBindings:
    """Require both authenticated peers and one unambiguous recent offer.

    Native acceptance sends UnregisterMercenary field 1=true. That bit alone
    does not identify an employer. Bind it only to a unique live negotiation.
    These records permit a bounded squad assignment, never payment or ownership.
    """

    def __init__(
        self, get_sessions: Callable[[], Iterable[GameSession]], get_team_id: Callable[[str], int]
    ) -> None:
        self.get_sessions, self.get_team_id = get_sessions, get_team_id
        self.offers: dict[tuple[str, str], HireBinding] = {}
        self.accepted: dict[str, HireBinding] = {}
        self.feedback_contexts: dict[tuple[str, str], HireFeedbackContext] = {}

    def is_current_member(self, session: GameSession, player_id: int, team_id: int) -> bool:
        """Match the exact transport to its live identity and durable team."""
        return (
            type(player_id) is int
            and 0 < player_id < 1 << 32
            and session.player_id == player_id
            and session.established
            and session.peer is not None
            and time.monotonic() - session.last_seen_monotonic <= 90
            and any(s is session for s in self.get_sessions())
            and team_id > 0
            and self.get_team_id(session.online_id) == team_id
        )

    def _valid(self, item: HireBinding) -> bool:
        return (
            time.monotonic() <= item.expires
            and self.is_current_member(item.employer, item.employer_id, item.employer_team)
            and self.is_current_member(item.mercenary, item.mercenary_id, item.mercenary_team)
            and item.employer_team != item.mercenary_team
        )

    def purge(self) -> None:
        self.offers = {key: value for key, value in self.offers.items() if self._valid(value)}
        self.accepted = {key: value for key, value in self.accepted.items() if self._valid(value)}
        self._purge_feedback()

    def discard(self, online_id: str) -> None:
        # Disconnects remove live match authority, not the bounded proof of a
        # past accepted hire. Offline recipients can still receive feedback.
        self.offers = {key: value for key, value in self.offers.items() if online_id not in key}
        self.accepted = {
            key: value
            for key, value in self.accepted.items()
            if online_id not in (value.employer.online_id, value.mercenary.online_id)
        }

    def offer(self, employer: GameSession, player_id: int) -> None:
        self.purge()
        targets = [s for s in self.get_sessions() if s.player_id == player_id and s.established]
        if len(targets) != 1 or targets[0] is employer:
            return
        mercenary = targets[0]
        item = HireBinding(
            employer,
            mercenary,
            employer.player_id,
            mercenary.player_id,
            self.get_team_id(employer.online_id),
            self.get_team_id(mercenary.online_id),
            time.monotonic() + 30,
        )
        if self._valid(item):
            current = self.accepted.get(mercenary.online_id)
            if current is not None and current.employer is employer:
                return
            self.offers[(employer.online_id, mercenary.online_id)] = item

    def unregister(self, mercenary: GameSession, accepted: bool) -> bool:
        self.purge()
        candidates = [value for value in self.offers.values() if value.mercenary is mercenary]
        current = self.accepted.get(mercenary.online_id)
        if accepted and not candidates and current is not None and current.mercenary is mercenary:
            return True  # An exact retry does not renew the original expiry.
        self.discard(mercenary.online_id)
        if not accepted or len(candidates) != 1:
            return False
        item = candidates[0]
        self.accepted[mercenary.online_id] = HireBinding(
            item.employer,
            mercenary,
            item.employer_id,
            item.mercenary_id,
            item.employer_team,
            item.mercenary_team,
            time.monotonic() + 1800,
        )
        accepted_item = self.accepted[mercenary.online_id]
        key = (item.employer.online_id, mercenary.online_id)
        self.feedback_contexts.pop(key, None)
        self.feedback_contexts[key] = HireFeedbackContext(
            item.employer.online_id,
            item.employer_id,
            mercenary.online_id,
            item.mercenary_id,
            accepted_item.expires,
        )
        while len(self.feedback_contexts) > MAX_FEEDBACK_CONTEXTS:
            self.feedback_contexts.pop(next(iter(self.feedback_contexts)))
        return True

    def resolve(self, employer: GameSession, player_id: int) -> HireBinding | None:
        self.purge()
        values = [
            value
            for value in self.accepted.values()
            if value.employer is employer and value.mercenary_id == player_id
        ]
        return values[0] if len(values) == 1 else None

    def _purge_feedback(self) -> None:
        now = time.monotonic()
        self.feedback_contexts = {
            key: value for key, value in self.feedback_contexts.items() if now <= value.expires
        }

    def feedback_sender_is_current(self, employer: GameSession) -> bool:
        return self.is_current_member(
            employer, employer.player_id, self.get_team_id(employer.online_id)
        )

    def feedback_for(
        self, employer: GameSession, player_id: int, request_key: str
    ) -> HireFeedbackContext | None:
        """Select the latest accepted hire for a new, nonfinancial request.

        Completed exact-request retries are resolved from durable notices first.
        Recipient presence and current team are not new-hire requirements here.
        """
        self._purge_feedback()
        if not self.feedback_sender_is_current(employer):
            return None
        candidates = [
            item
            for item in self.feedback_contexts.values()
            if item.employer_online_id == employer.online_id
            and item.employer_id == employer.player_id
            and item.mercenary_id == player_id
            and item.request_key in (None, request_key)
        ]
        return candidates[0] if len(candidates) == 1 else None

    def reserve_feedback(self, item: HireFeedbackContext, request_key: str) -> None:
        """Keep an I/O retry tied to the same request without renewing expiry."""
        key = (item.employer_online_id, item.mercenary_online_id)
        if (
            self.feedback_contexts.get(key) is not item
            or time.monotonic() > item.expires
            or item.request_key not in (None, request_key)
        ):
            raise ValueError("Accepted feedback context is no longer available")
        item.request_key = request_key

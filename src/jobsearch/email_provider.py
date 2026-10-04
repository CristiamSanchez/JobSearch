"""Provider-neutral email intake interface (Phase 4A).

Defines the small protocol any mailbox backend implements so the domain can
retrieve emails without knowing which provider serves them. The email
itself is the ingestion boundary: messages are normalized into
:class:`~jobsearch.models.EmailMessage` and nothing downstream ever
branches on who sent them.

No provider SDK appears here — Gmail lives in :mod:`jobsearch.gmail`, and a
future provider would be another adapter behind the same protocol.
"""

from __future__ import annotations

from email.utils import parseaddr
from typing import Protocol, runtime_checkable

from .models import EmailMessage

__all__ = ["EmailProvider", "EmailProviderError", "source_hint_from_sender"]

# Domain labels that are not the organization name (e.g. example.co.uk).
_GENERIC_DOMAIN_LABELS = frozenset(
    {"co", "com", "org", "net", "ac", "gov", "edu", "io"}
)


class EmailProviderError(ValueError):
    """Raised when a provider returns a malformed or unusable response."""


@runtime_checkable
class EmailProvider(Protocol):
    """Retrieve normalized emails from any mailbox backend.

    Implementations must be side-effect free with respect to the mailbox:
    search and retrieval only — no sending, deleting, modifying, or
    changing read state.
    """

    def search(self, query: str) -> list[EmailMessage]:
        """Return messages matching ``query``, in provider order."""
        ...

    def get_message(self, message_id: str) -> EmailMessage:
        """Return one message by its stable ``message_id``."""
        ...


def source_hint_from_sender(sender: str) -> str | None:
    """Best-effort, **non-authoritative** hint derived from the sender.

    Purely generic: the organization label of the sender's domain, for any
    domain ("Jobs <jobs@example.com>" -> "example"). Returns ``None`` when
    no domain can be determined (e.g. a display name without an address).

    This hint is informational only — classification and routing must never
    depend on it.
    """
    _, address = parseaddr(sender)
    if "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip().lower().rstrip(".")
    labels = domain.split(".")
    if len(labels) < 2:
        return None
    label = labels[-2]
    if label in _GENERIC_DOMAIN_LABELS and len(labels) >= 3:
        label = labels[-3]
    return label or None

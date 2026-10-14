"""Provider-agnostic email sending. Business code depends on this, never on Brevo."""

from dataclasses import dataclass
from typing import Protocol


class EmailSendError(Exception):
    """The provider did not accept the message.

    The text is only ever an error class name or status code: never a response body,
    a header, an address or a key.
    """


@dataclass(frozen=True)
class EmailMessage:
    to_address: str
    to_name: str
    subject: str
    text: str
    html: str


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> None:
        """Deliver `message`, or raise EmailSendError."""
        ...

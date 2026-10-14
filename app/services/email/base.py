"""Provider-agnostic email sending. Business code depends on this, never on Brevo."""

from dataclasses import dataclass
from typing import Protocol


class EmailSendError(Exception):
    """The provider did not accept the message.

    The text is only ever an error class name or status code: never a response body,
    a header, an address or a key.

    `permanent` is True when the provider rejected this message for good (an HTTP 4xx such
    as an invalid address or sender): sending it again can only fail again. It is False for
    anything that may pass later (a timeout, a 5xx, a network error, rate limiting).
    """

    def __init__(self, message: str = "", *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


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

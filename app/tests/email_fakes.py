"""A recording email sender for tests. No network, and Brevo is never called."""

from app.services.email.base import EmailMessage, EmailSendError


class FakeEmailSender:
    """Records every message. `fail_next` makes the next N sends raise EmailSendError."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.attempts = 0
        self.fail_next = 0

    async def send(self, message: EmailMessage) -> None:
        self.attempts += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise EmailSendError("scripted failure")
        self.sent.append(message)

    def to(self, address: str) -> list[EmailMessage]:
        return [m for m in self.sent if m.to_address == address]

"""HMAC-SHA256 signing and verification helpers for event tickets."""

import hashlib
import hmac


def sign_ticket(secret: str, event_id: str, ticket_id: str, holder_contact: str) -> str:
    """Return a hex HMAC-SHA256 signature binding event, ticket, and holder contact."""
    message = f"{event_id}:{ticket_id}:{holder_contact}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    event_id: str,
    ticket_id: str,
    holder_contact: str,
    signature: str,
) -> bool:
    """Verify a ticket signature in constant time."""
    if not signature:
        return False
    expected = sign_ticket(secret, event_id, ticket_id, holder_contact)
    return hmac.compare_digest(expected, signature.strip())


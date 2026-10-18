"""Explicit failures at the boundary between policy checks and stored work."""


class VerificationUnavailable(RuntimeError):
    """A required check could not be completed; never equivalent to permission."""

    def __init__(self, check: str):
        self.check = check
        super().__init__(f"I couldn't check {check}. The business needs to review this request.")


def capacity_conflict(exc: Exception) -> bool:
    """Recognise only our database guard, without exposing SQL/provider details."""
    return isinstance(exc, CapacityConflict) or "BLAM8_CAPACITY_CONFLICT" in str(exc)


CAPACITY_MESSAGE = "That slot has just been taken. Nothing changed. Please choose another time."


class CapacityConflict(RuntimeError):
    def __init__(self):
        super().__init__(CAPACITY_MESSAGE)


def translate_capacity(error_type):
    """Translate only the atomic reservation failure at a writer's boundary."""
    from contextlib import contextmanager

    @contextmanager
    def guard():
        try:
            yield
        except Exception as exc:
            if capacity_conflict(exc):
                raise error_type(CAPACITY_MESSAGE) from None
            raise
    return guard()

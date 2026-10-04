"""Shared error raised when the Redis-backed circuit breaker is open."""


class CircuitOpenError(Exception):
    """The model call was rejected because its upstream circuit is open."""

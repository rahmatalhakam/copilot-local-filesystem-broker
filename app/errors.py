from __future__ import annotations


class ConfigurationError(ValueError):
    """Raised when local broker configuration is missing or unsafe."""


class RequestValidationError(ValueError):
    """Raised when fields do not match the selected operation."""


class PolicyViolation(Exception):
    """A stable, caller-safe rejection at a security policy boundary."""

    def __init__(self, code: str, message: str, rule: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.rule = rule

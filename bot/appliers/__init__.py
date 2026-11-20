"""Extensible recruitment platform appliers."""

from bot.appliers.base import (
    ApplicationDocuments,
    ApplicationOutcome,
    FormField,
    FormInspection,
    HumanInterventionRequired,
    PlatformApplier,
)
from bot.appliers.detector import detect_platform, resolve_apply_url
from bot.appliers.registry import ApplierRegistry

__all__ = [
    "ApplicationDocuments",
    "ApplicationOutcome",
    "ApplierRegistry",
    "FormField",
    "FormInspection",
    "HumanInterventionRequired",
    "PlatformApplier",
    "detect_platform",
    "resolve_apply_url",
]

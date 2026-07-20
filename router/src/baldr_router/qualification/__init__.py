"""Operator-only real-environment qualification.

The public MCP facade remains setup/status/run. Qualification is an operational
release gate exposed through the CLI and thin client UIs.
"""

from .receipts import latest_client_receipt, record_client_receipt
from .extension_host import run_extension_host_cancellation_canary
from .runner import (
    latest_qualification,
    list_qualifications,
    qualification_template,
    promotion_status,
    qualification_receipt_sha256,
    run_qualification,
    write_qualification_template,
)

__all__ = [
    "latest_client_receipt",
    "record_client_receipt",
    "run_extension_host_cancellation_canary",
    "latest_qualification",
    "list_qualifications",
    "qualification_template",
    "promotion_status",
    "qualification_receipt_sha256",
    "run_qualification",
    "write_qualification_template",
]

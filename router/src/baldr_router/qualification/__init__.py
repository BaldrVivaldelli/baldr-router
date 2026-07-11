"""Operator-only real-environment qualification.

The public MCP facade remains setup/status/run. Qualification is an operational
release gate exposed through the CLI and thin client UIs.
"""

from .receipts import latest_client_receipt, record_client_receipt
from .runner import (
    latest_qualification,
    list_qualifications,
    qualification_template,
    run_qualification,
    write_qualification_template,
)

__all__ = [
    "latest_client_receipt",
    "record_client_receipt",
    "latest_qualification",
    "list_qualifications",
    "qualification_template",
    "run_qualification",
    "write_qualification_template",
]

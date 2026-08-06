"""Platform-owned scenario contract identifiers.

These values are attached by trusted orchestration code after a workflow shape
has been recognized.  Planner output and business Artifact names must never be
treated as authority to select a scenario-specific validation policy.
"""

ANNUAL_LEAVE_REPORT_V1 = "annual_leave_report_v1"


__all__ = ["ANNUAL_LEAVE_REPORT_V1"]

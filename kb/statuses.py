"""Internal answer scale. Every worksheet's own scale is mapped onto these values."""

STATUSES = {
    "STANDARD": "In the current GA release (out of the box or via configuration)",
    "SUPPORTED": "Supported, but the source scale didn't say how (e.g. Yes / Comply)",
    "PARTIAL": "Partially met; caveats should be in the comment",
    "SCHEDULED": "Committed for an upcoming, named release",
    "FUTURE": "On the roadmap / future release",
    "CUSTOM": "Requires modification or custom development",
    "THIRD_PARTY": "Met through a third-party product or partner",
    "NOT_SUPPORTED": "Cannot be met",
    "NEEDS_DISCUSSION": "Answered as 'need more information'",
    "NOT_APPLICABLE": "Not applicable",
    "NARRATIVE": "No scale answer; the response is free text in the comment",
}

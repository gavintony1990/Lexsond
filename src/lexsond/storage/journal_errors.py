"""Workflow-journal integrity errors shared by PostgreSQL adapters."""


class WorkflowJournalCorruption(RuntimeError):
    """Stored workflow history is internally inconsistent or malformed."""


class WorkflowJournalIntegrityError(RuntimeError):
    """A durable write violated the workflow journal contract."""

"""Shared exception types."""


class JobError(Exception):
    """A problem the CLI can explain to the user without a traceback."""


class UsageError(JobError):
    """Bad input from the user or from a scripted caller."""


class DatabaseError(JobError):
    """Database is missing, unreadable, or not a job database."""


class MigrationError(JobError):
    """Migration files and database state disagree, or applying failed."""


class Cancelled(JobError):
    """The user cancelled an interactive flow."""

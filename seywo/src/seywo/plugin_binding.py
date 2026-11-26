"""Recoverable workspace-binding state shared by the plugin and its app."""


class ProjectRequired(ValueError):
    """The host did not supply one unambiguous local workspace."""


class ReconnectRequired(ValueError):
    """The requested workspace differs from this process's pinned binding."""

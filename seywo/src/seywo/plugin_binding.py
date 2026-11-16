"""Recoverable workspace-binding state shared by the plugin and its app."""


class ProjectRequired(ValueError):
    """The host did not supply one unambiguous local workspace."""

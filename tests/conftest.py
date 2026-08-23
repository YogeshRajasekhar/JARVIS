"""
Shared pytest configuration.

Registers the `integration` marker (tests that make real, live network calls
against the Anthropic API) so pytest doesn't warn about an unknown marker,
and so `pytest -m "not integration"` cleanly excludes them when no API key
is configured.
"""

def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: marks tests that make live calls to an external API"
    )

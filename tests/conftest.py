"""Shared test setup."""

import pytest
import pytest_socket

LOCAL_SERVER_MODULES = {"test_api", "test_discover"}


@pytest.fixture(autouse=True)
def allow_local_test_servers(request):
    """These modules talk to fake servers on 127.0.0.1, which HA's test plugin blocks."""
    if request.module.__name__.rsplit(".", 1)[-1] in LOCAL_SERVER_MODULES:
        pytest_socket.enable_socket()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Let Home Assistant tests load SkyLink, without forcing HA on the other tests."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")

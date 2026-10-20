"""Rules capture fixtures: the in-process environment plus the local "rules site" (robots.txt + saved rules pages)."""

from tests.p2d.conftest import _p2d_mock_server, p2d, p2d_mock_url, p2d_registry  # noqa: F401 - re-exported fixtures
from tests.p4b.conftest import _rules_site, rules_site, site_hits  # noqa: F401 - re-exported fixtures

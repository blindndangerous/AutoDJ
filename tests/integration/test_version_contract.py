from autodj.server import create_app
from autodj.version import current_version


def test_api_version_derives_from_project_metadata(bridge) -> None:
    assert create_app(bridge).version == current_version()

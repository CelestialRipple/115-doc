import importlib.util
import sys
from pathlib import Path
from types import ModuleType
import pytest

app = ModuleType("app")
plugins = ModuleType("app.plugins")
plugins._PluginBase = type("Base", (), {})
sys.modules.update({"app": app, "app.plugins": plugins})
ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "blurayreleasecalendar",
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
module = importlib.util.module_from_spec(spec)
sys.modules["blurayreleasecalendar"] = module
spec.loader.exec_module(module)


@pytest.fixture
def plugin(tmp_path):
    p = module.BlurayReleaseCalendar()
    p.get_data_path = lambda: tmp_path
    p.init_plugin({"enabled": True})
    return p


@pytest.fixture
def client(plugin):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    for route in plugin.get_api():
        app.add_api_route(
            "/api/v1/plugin/BlurayReleaseCalendar" + route["path"],
            route["endpoint"],
            methods=route["methods"],
        )
    with TestClient(app) as client:
        yield client

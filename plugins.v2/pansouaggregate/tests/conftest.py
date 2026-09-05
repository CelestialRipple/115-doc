import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if "app.plugins" not in sys.modules:
    app = ModuleType("app")
    plugins = ModuleType("app.plugins")
    plugins._PluginBase = type("PluginBase", (), {})
    log = ModuleType("app.log")
    log.logger = SimpleNamespace(warning=lambda *a, **kw: None)
    context = ModuleType("app.core.context")
    context.TorrentInfo = SimpleNamespace
    sys.modules.update(
        {
            "app": app,
            "app.plugins": plugins,
            "app.log": log,
            "app.core.context": context,
        }
    )
spec = importlib.util.spec_from_file_location(
    "pansouaggregate", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
module = importlib.util.module_from_spec(spec)
sys.modules["pansouaggregate"] = module
spec.loader.exec_module(module)


@pytest.fixture
def plugin():
    plugin = module.PanSouAggregate()
    plugin.update_config = lambda config: None
    plugin.init_plugin(
        {
            "enabled": True,
            "pansou_url": "http://nas:7080",
            "ui_key": "test-private-key",
            "bt4g_enabled": True,
        }
    )
    yield plugin
    plugin.stop_service()


@pytest.fixture
def client(plugin):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    for route in plugin.get_api():
        app.add_api_route(
            "/api/v1/plugin/PanSouAggregate" + route["path"],
            route["endpoint"],
            methods=route["methods"],
        )
    with TestClient(app) as client:
        yield client

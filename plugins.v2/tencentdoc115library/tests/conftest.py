import sys
import importlib.util
from concurrent.futures import ThreadPoolExecutor
import pytest
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _install_app_stubs() -> None:
    class DummyRequestUtils:
        def __init__(self, *args, **kwargs):
            pass

    app = ModuleType("app")
    sdk = ModuleType("app.sdk")
    config = ModuleType("app.sdk.config")
    logging = ModuleType("app.sdk.logging")
    network = ModuleType("app.sdk.network")
    plugins = ModuleType("app.sdk.plugins")
    media_sdk = ModuleType("app.sdk.media")
    chain = ModuleType("app.chain")
    chain_media = ModuleType("app.chain.media")
    schemas = ModuleType("app.schemas")
    schemas_file = ModuleType("app.schemas.file")
    schemas_types = ModuleType("app.schemas.types")

    class DummyMediaType(Enum):
        MOVIE = "电影"
        TV = "电视剧"

    class DummyFileItem:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    config.settings = SimpleNamespace(PROXY=None)
    logging.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
    )
    network.RequestUtils = DummyRequestUtils
    plugins.PluginManager = object
    media_sdk.MetaInfo = lambda *args, **kwargs: SimpleNamespace(
        begin_season=None,
        begin_episode=None,
    )
    chain_media.MediaChain = object
    schemas_file.FileItem = DummyFileItem
    schemas_types.MediaType = DummyMediaType
    schemas_types.TorrentStatus = SimpleNamespace(
        DOWNLOADING=SimpleNamespace(value="downloading"),
        TRANSFER=SimpleNamespace(value="transfer"),
    )
    schemas.Response = SimpleNamespace
    schemas.DownloaderTorrent = SimpleNamespace
    plugin_base = ModuleType("app.plugins")
    plugin_base._PluginBase = type("PluginBase", (), {"__init__": lambda self: None})
    context = ModuleType("app.domain.context")
    context.TorrentInfo = SimpleNamespace
    thread = ModuleType("app.runtime.thread")

    class ThreadHelper:
        pool = ThreadPoolExecutor(max_workers=4)

        def submit(self, function, *args, **kwargs):
            return self.pool.submit(function, *args, **kwargs)

    thread.ThreadHelper = ThreadHelper
    sys.modules.update(
        {
            "app": app,
            "app.log": logging,
            "app.plugins": plugin_base,
            "app.domain.context": context,
            "app.runtime.thread": thread,
            "app.sdk": sdk,
            "app.sdk.config": config,
            "app.sdk.logging": logging,
            "app.sdk.network": network,
            "app.sdk.plugins": plugins,
            "app.sdk.media": media_sdk,
            "app.chain": chain,
            "app.chain.media": chain_media,
            "app.schemas": schemas,
            "app.schemas.file": schemas_file,
            "app.schemas.types": schemas_types,
        }
    )


_install_app_stubs()
spec = importlib.util.spec_from_file_location(
    "tencentdoc115library",
    PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)


@pytest.fixture
def plugin(tmp_path):
    instance = package.TencentDoc115Library()
    instance.get_data_path = lambda: tmp_path / "data"
    instance.update_config = lambda config: None
    instance.init_plugin({"output_root": str(tmp_path / "output"), "enabled": False})
    yield instance
    instance.stop_service()


def pytest_sessionfinish(session, exitstatus):
    sys.modules["app.runtime.thread"].ThreadHelper.pool.shutdown(wait=True)

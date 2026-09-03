import sys
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
    sys.modules.update(
        {
            "app": app,
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


package = ModuleType("tencentdoc115library")
package.__path__ = [str(PLUGIN_DIR)]
sys.modules.setdefault("tencentdoc115library", package)
_install_app_stubs()

import sys
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
    config.settings = SimpleNamespace(PROXY=None)
    logging.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    network.RequestUtils = DummyRequestUtils
    plugins.PluginManager = object
    sys.modules.update(
        {
            "app": app,
            "app.sdk": sdk,
            "app.sdk.config": config,
            "app.sdk.logging": logging,
            "app.sdk.network": network,
            "app.sdk.plugins": plugins,
        }
    )


package = ModuleType("tencentdoc115library")
package.__path__ = [str(PLUGIN_DIR)]
sys.modules.setdefault("tencentdoc115library", package)
_install_app_stubs()

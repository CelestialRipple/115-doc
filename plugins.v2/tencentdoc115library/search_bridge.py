"""Bridge MoviePilot's native search when no indexer sites are available."""

from collections.abc import Sequence
from inspect import currentframe, isawaitable
from threading import Lock
from typing import Any, Callable, Iterator, Optional


LOCAL_INDEXER_MARKER = "tencentdoc115library_local_indexer"
LOCAL_INDEXER_ID = -115000001


class LocalSearchResults(Sequence):
    """A sequence that prevents MoviePilot from invoking its network indexer."""

    def __init__(self, items: Sequence[Any]) -> None:
        self._items = tuple(items)

    def __getitem__(self, index: Any) -> Any:
        return self._items[index]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


def _called_from_search_chain() -> bool:
    """Only expose the synthetic indexer to MoviePilot's search chain."""
    frame = currentframe()
    try:
        caller = frame.f_back if frame else None
        for _ in range(8):
            if not caller:
                return False
            filename = str(caller.f_code.co_filename).replace("\\", "/")
            if filename.endswith("/app/chain/search.py"):
                return True
            caller = caller.f_back
        return False
    finally:
        del frame


class MoviePilotSearchBridge:
    """Supply one local-only indexer when MoviePilot has no usable sites.

    MoviePilot 2.15.x returns before plugin search modules are dispatched when
    ``SitesHelper`` yields no indexers. The bridge is deliberately scoped to
    calls originating in ``app/chain/search.py`` so site settings, schedulers,
    and other helpers continue to observe the original empty site list.
    """

    _patch_lock = Lock()
    _owner_attribute = "tencentdoc115library_search_bridge_owner"

    def __init__(
        self,
        enabled_provider: Callable[[], bool],
        search_call_checker: Callable[[], bool] = _called_from_search_chain,
    ) -> None:
        self._enabled_provider = enabled_provider
        self._search_call_checker = search_call_checker
        self._sites_helper_class: Optional[type] = None
        self._original_get_indexers: Optional[Callable[..., Any]] = None
        self._original_async_get_indexers: Optional[Callable[..., Any]] = None
        self._sync_wrapper: Optional[Callable[..., Any]] = None
        self._async_wrapper: Optional[Callable[..., Any]] = None

    @staticmethod
    def local_indexer() -> dict:
        """Return the marker indexer consumed only by this plugin."""
        return {
            "id": LOCAL_INDEXER_ID,
            "name": "腾讯文档115媒体库",
            "domain": "https://tencentdoc115.local",
            "public": True,
            "is_active": True,
            "pri": 0,
            "language": "zh",
            "result_num": 100,
            LOCAL_INDEXER_MARKER: True,
        }

    def _should_supply_local_indexer(self, indexers: Any) -> bool:
        return (
            not indexers
            and bool(self._enabled_provider())
            and bool(self._search_call_checker())
        )

    def install(self) -> bool:
        """Patch ``SitesHelper`` once; return whether this bridge owns it."""
        try:
            from app.helper.sites import SitesHelper
        except (ImportError, AttributeError):
            return False

        with self._patch_lock:
            owner = getattr(SitesHelper, self._owner_attribute, None)
            if owner is self:
                return True
            if owner is not None:
                return False

            original_sync = getattr(SitesHelper, "get_indexers", None)
            original_async = getattr(SitesHelper, "async_get_indexers", None)
            if not callable(original_sync) or not callable(original_async):
                return False

            bridge = self

            def get_indexers(helper: Any, *args: Any, **kwargs: Any) -> Any:
                indexers = original_sync(helper, *args, **kwargs)
                if bridge._should_supply_local_indexer(indexers):
                    return [bridge.local_indexer()]
                return indexers

            async def async_get_indexers(
                helper: Any, *args: Any, **kwargs: Any
            ) -> Any:
                indexers = original_async(helper, *args, **kwargs)
                if isawaitable(indexers):
                    indexers = await indexers
                if bridge._should_supply_local_indexer(indexers):
                    return [bridge.local_indexer()]
                return indexers

            self._sites_helper_class = SitesHelper
            self._original_get_indexers = original_sync
            self._original_async_get_indexers = original_async
            self._sync_wrapper = get_indexers
            self._async_wrapper = async_get_indexers
            setattr(SitesHelper, "get_indexers", get_indexers)
            setattr(SitesHelper, "async_get_indexers", async_get_indexers)
            setattr(SitesHelper, self._owner_attribute, self)
            return True

    def uninstall(self) -> None:
        """Restore the exact methods replaced by this bridge."""
        with self._patch_lock:
            helper_class = self._sites_helper_class
            if not helper_class:
                return
            if getattr(helper_class, self._owner_attribute, None) is not self:
                return
            if (
                self._sync_wrapper is not None
                and getattr(helper_class, "get_indexers", None) is self._sync_wrapper
                and self._original_get_indexers is not None
            ):
                setattr(helper_class, "get_indexers", self._original_get_indexers)
            if (
                self._async_wrapper is not None
                and getattr(helper_class, "async_get_indexers", None)
                is self._async_wrapper
                and self._original_async_get_indexers is not None
            ):
                setattr(
                    helper_class,
                    "async_get_indexers",
                    self._original_async_get_indexers,
                )
            setattr(helper_class, self._owner_attribute, None)
            self._sites_helper_class = None
            self._original_get_indexers = None
            self._original_async_get_indexers = None
            self._sync_wrapper = None
            self._async_wrapper = None

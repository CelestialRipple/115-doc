"""定时同步必须跑完整轮，且下一次触发不能被上一批的分页暂停阻断。"""

from concurrent.futures import Future
from types import SimpleNamespace


class ImmediatePool:
    def submit(self, function, *args, **kwargs):
        future = Future()
        future.set_result(function(*args, **kwargs))
        return future


def test_scheduled_sync_finishes_all_pages_then_builds_pending(plugin, monkeypatch):
    monkeypatch.setattr("tencentdoc115library.ThreadHelper", ImmediatePool)

    class Synchronizer:
        calls = []

        def sync(self, mode="manual", reset=False):
            self.calls.append((mode, reset))
            if len(self.calls) % 2:
                return {"status": "paused", "processed_pages": 5, "processed_rows": 500}
            return {"status": "completed", "processed_pages": 1, "processed_rows": 50}

    class Builder:
        sheet_ids = []

        def build(self, **kwargs):
            self.sheet_ids.append(kwargs["sheet_ids"])
            if len(self.sheet_ids) % 2:
                return {"status": "completed", "processed": 1, "success": 1, "failed": 0}
            return {"status": "completed", "processed": 0, "success": 0, "failed": 0}

    plugin._enabled = True
    plugin._store = SimpleNamespace(
        list_sheets=lambda enabled_only=False: [{"sheet_id": "enabled-sheet"}]
    )
    plugin._synchronizer = Synchronizer()
    plugin._builder = Builder()

    plugin._automatic_sync()
    assert plugin._task_snapshot()["state"] == "completed"
    assert plugin._pipeline_snapshot()["synced_pages"] == 6
    assert plugin._pipeline_snapshot()["built"] == 1
    assert plugin._builder.sheet_ids == [["enabled-sheet"], ["enabled-sheet"]]

    plugin._automatic_sync()
    assert plugin._task_snapshot()["state"] == "completed"
    assert plugin._synchronizer.calls == [
        ("automatic", True),
        ("automatic", False),
        ("automatic", True),
        ("automatic", False),
    ]
    assert len(plugin._builder.sheet_ids) == 4


def test_daily_cron_migrates_and_reinitialization_clears_old_pause(plugin, tmp_path):
    saved = []
    plugin.update_config = lambda config: saved.append(dict(config))
    plugin._task_state = "paused"
    plugin._resume_spec = (lambda: None, (), {})

    plugin.init_plugin(
        {
            "enabled": False,
            "auto_sync": True,
            "auto_build": True,
            "sync_cron": "0 */6 * * *",
            "output_root": str(tmp_path / "output"),
        }
    )

    assert plugin._task_snapshot()["state"] == "idle"
    assert plugin._config["sync_cron"] == "0 3 * * *"
    assert saved[-1]["sync_cron"] == "0 3 * * *"
    plugin._enabled = True
    service_ids = {service["id"] for service in plugin.get_service()}
    assert "TencentDoc115LibrarySync" in service_ids
    assert "TencentDoc115LibraryBuild" not in service_ids


def test_custom_schedule_is_preserved(plugin, tmp_path):
    plugin.init_plugin(
        {
            "enabled": False,
            "auto_sync": True,
            "sync_cron": "0 4 * * *",
            "output_root": str(tmp_path / "output"),
        }
    )
    assert plugin._config["sync_cron"] == "0 4 * * *"


def test_scheduled_sync_does_not_report_build_failure_as_complete(plugin):
    plugin._synchronizer = SimpleNamespace(
        sync=lambda **kwargs: {
            "status": "completed",
            "processed_pages": 1,
            "processed_rows": 10,
        }
    )
    plugin._builder = SimpleNamespace(
        build=lambda **kwargs: {
            "status": "failed",
            "message": "构建失败",
            "processed": 0,
            "success": 0,
            "failed": 0,
        }
    )
    assert plugin._sync_all_and_build(mode="automatic")["status"] == "failed"

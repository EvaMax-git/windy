import pytest
from pathlib import Path
from mneme.storage.path_resolver import get_storage_path, check_nas_available


class TestCheckNasAvailable:
    def test_nonexistent_path(self):
        ok, msg = check_nas_available("/nonexistent/path/that/does/not/exist")
        assert ok is False
        assert len(msg) > 0

    def test_local_path_ok(self, tmp_path):
        ok, msg = check_nas_available(str(tmp_path))
        assert ok is True


class TestGetStoragePath:
    def test_local_mode(self, monkeypatch):
        monkeypatch.setenv("MNEME_STORAGE_MODE", "local")
        monkeypatch.setenv("MNEME_STORAGE_ROOT", "mneme_data")
        from mneme.config import get_settings
        get_settings.cache_clear()
        p = get_storage_path()
        assert "mneme_data" in str(p)

    def test_auto_fallback_local(self, monkeypatch):
        monkeypatch.setenv("MNEME_STORAGE_MODE", "auto")
        monkeypatch.setenv("MNEME_NAS_PATH", "/nonexistent/nas/path")
        monkeypatch.setenv("MNEME_STORAGE_ROOT", "mneme_data")
        from mneme.config import get_settings
        get_settings.cache_clear()
        p = get_storage_path()
        assert "mneme_data" in str(p)

    def test_preferred_override(self, tmp_path):
        p = get_storage_path(str(tmp_path))
        assert p == Path(str(tmp_path))

    def test_auto_uses_nas_if_available(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MNEME_STORAGE_MODE", "auto")
        monkeypatch.setenv("MNEME_NAS_PATH", str(tmp_path))
        monkeypatch.setenv("MNEME_STORAGE_ROOT", "mneme_data")
        from mneme.config import get_settings
        get_settings.cache_clear()
        p = get_storage_path()
        assert str(tmp_path) in str(p)

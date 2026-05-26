import pytest
from pathlib import Path
from mneme.security.key_store import save_key, load_key, key_path


class TestKeyPath:
    def test_default_path(self):
        p = key_path("test")
        assert p.name == "test.key"
        assert "keys" in str(p)

    def test_custom_name(self):
        p = key_path("mykey")
        assert p.name == "mykey.key"


class TestSaveLoadKey:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MNEME_KEY_DIR", str(tmp_path))
        from mneme.config import get_settings
        get_settings.cache_clear()
        key = b"0123456789abcdef0123456789abcdef"  # 32 bytes
        save_key(key, "test")
        loaded = load_key("test")
        assert loaded == key

    def test_file_created(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MNEME_KEY_DIR", str(tmp_path))
        from mneme.config import get_settings
        get_settings.cache_clear()
        key = b"x" * 32
        path = save_key(key, "perm_test")
        assert path.exists()

    def test_load_nonexistent_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MNEME_KEY_DIR", str(tmp_path))
        from mneme.config import get_settings
        get_settings.cache_clear()
        with pytest.raises(FileNotFoundError):
            load_key("nonexistent")

    def test_save_overwrites(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MNEME_KEY_DIR", str(tmp_path))
        from mneme.config import get_settings
        get_settings.cache_clear()
        save_key(b"a" * 32, "test")
        save_key(b"b" * 32, "test")
        assert load_key("test") == b"b" * 32

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            key_path("")
        with pytest.raises(ValueError, match="不能为空"):
            key_path("  ")

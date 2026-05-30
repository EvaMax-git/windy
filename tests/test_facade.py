"""Facade API integration tests — A-38 full pipeline + A-39 bug detection.

Tests all public interfaces specified in W4:
  - parser: process_file, parse_file, clean_text, chunk_text
  - encrypt: encrypt_file, decrypt_file, generate_key
  - storage: get_storage_path
  - Full pipeline: parse → clean → chunk → encrypt → store → decrypt
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mneme.facade.parser import chunk_text, clean_text, parse_file, process_file
from mneme.facade.encrypt import decrypt_file, encrypt_file, generate_key
from mneme.facade.storage import get_storage_path


# ── parser: clean_text ────────────────────────────────────────────────


class TestCleanText:
    def test_normalizes_whitespace(self):
        result = clean_text("hello   world\t\ttabs")
        assert "   " not in result
        assert "\t\t" not in result

    def test_removes_control_chars(self):
        result = clean_text("hello\x00\x01\x02world")
        assert "\x00" not in result
        assert "hello" in result
        assert "world" in result

    def test_merges_empty_lines(self):
        result = clean_text("line1\n\n\n\n\nline2")
        assert "\n\n\n" not in result
        assert "line1" in result
        assert "line2" in result

    def test_strips_page_footers(self):
        result = clean_text("正文内容\n第 1 页\n更多正文")
        assert "第 1 页" not in result
        assert "正文内容" in result

    def test_none_raises(self):
        with pytest.raises(TypeError):
            clean_text(None)

    def test_chinese_content(self):
        raw = "  这是一段   中文文本\t\t带有空格  "
        result = clean_text(raw)
        assert "中文文本" in result
        assert "\t\t" not in result


# ── parser: chunk_text ────────────────────────────────────────────────


class TestChunkText:
    def test_returns_list_of_strings(self):
        text = "A" * 1200
        result = chunk_text(text, size=500, overlap=50)
        assert isinstance(result, list)
        assert all(isinstance(c, str) for c in result)

    def test_chunk_count_scales_with_size(self):
        text = "A" * 2000
        small = chunk_text(text, size=200, overlap=20)
        large = chunk_text(text, size=800, overlap=50)
        assert len(small) > len(large)

    def test_empty_text_returns_empty(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_none_raises(self):
        with pytest.raises(TypeError):
            chunk_text(None)

    def test_overlap_ge_size_raises(self):
        with pytest.raises(ValueError):
            chunk_text("hello", size=10, overlap=10)
        with pytest.raises(ValueError):
            chunk_text("hello", size=10, overlap=15)

    def test_short_text_single_chunk(self):
        text = "短文本"
        result = chunk_text(text, size=500, overlap=50)
        assert len(result) == 1
        assert result[0] == text


# ── parser: parse_file ────────────────────────────────────────────────


class TestParseFile:
    def test_parse_txt_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Hello World\n第二行", encoding="utf-8")
        result = parse_file(str(f))
        assert "Hello World" in result
        assert "第二行" in result

    def test_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            parse_file("/nonexistent/file.txt")

    def test_none_raises(self):
        with pytest.raises(TypeError):
            parse_file(None)


# ── parser: process_file ──────────────────────────────────────────────


class TestProcessFile:
    def test_returns_expected_keys(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Hello World " * 100, encoding="utf-8")
        result = process_file(str(f))
        assert "chunks" in result
        assert "word_count" in result
        assert "file_type" in result
        assert "filename" in result
        assert "size" in result

    def test_chunks_is_list_of_strings(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("A" * 2000, encoding="utf-8")
        result = process_file(str(f))
        assert isinstance(result["chunks"], list)
        assert all(isinstance(c, str) for c in result["chunks"])

    def test_word_count_positive(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Hello World 你好世界", encoding="utf-8")
        result = process_file(str(f))
        assert result["word_count"] > 0

    def test_file_type_detected(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("content", encoding="utf-8")
        result = process_file(str(f))
        assert result["file_type"] is not None

    def test_filename_matches(self, tmp_path):
        f = tmp_path / "myfile.txt"
        f.write_text("content", encoding="utf-8")
        result = process_file(str(f))
        assert result["filename"] == "myfile.txt"

    def test_size_matches_content(self, tmp_path):
        content = "Hello World" * 50
        f = tmp_path / "test.txt"
        f.write_text(content, encoding="utf-8")
        result = process_file(str(f))
        assert result["size"] == len(content.encode("utf-8"))

    def test_custom_chunk_params(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("A" * 2000, encoding="utf-8")
        r1 = process_file(str(f), chunk_size=200, overlap=20)
        r2 = process_file(str(f), chunk_size=800, overlap=50)
        assert len(r1["chunks"]) > len(r2["chunks"])

    def test_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            process_file("/nonexistent/file.txt")

    def test_none_raises(self):
        with pytest.raises(TypeError):
            process_file(None)


# ── encrypt: generate_key ─────────────────────────────────────────────


class TestGenerateKey:
    def test_returns_32_bytes(self):
        key = generate_key()
        assert isinstance(key, bytes)
        assert len(key) == 32

    def test_keys_are_random(self):
        keys = {generate_key() for _ in range(10)}
        assert len(keys) == 10


# ── encrypt: encrypt_file / decrypt_file ──────────────────────────────


class TestEncryptDecrypt:
    def test_roundtrip_with_auto_key(self):
        original = "Hello, World! 你好世界".encode("utf-8")
        encrypted, key = encrypt_file(original)
        assert encrypted != original
        assert len(key) == 32
        decrypted = decrypt_file(encrypted, key)
        assert decrypted == original

    def test_roundtrip_with_provided_key(self):
        key = generate_key()
        original = b"secret data"
        encrypted, returned_key = encrypt_file(original, key=key)
        assert returned_key is key
        decrypted = decrypt_file(encrypted, key)
        assert decrypted == original

    def test_wrong_key_fails(self):
        encrypted, key = encrypt_file(b"secret")
        wrong_key = generate_key()
        with pytest.raises(ValueError):
            decrypt_file(encrypted, wrong_key)

    def test_empty_content_raises(self):
        with pytest.raises(ValueError):
            encrypt_file(b"")

    def test_non_bytes_content_raises(self):
        with pytest.raises(TypeError):
            encrypt_file("string not bytes")

    def test_non_bytes_encrypted_raises(self):
        with pytest.raises(TypeError):
            decrypt_file("string", generate_key())

    def test_non_bytes_key_raises(self):
        with pytest.raises(TypeError):
            decrypt_file(b"data", "string")

    def test_chinese_content(self):
        original = "测试中文加密解密".encode("utf-8")
        encrypted, key = encrypt_file(original)
        decrypted = decrypt_file(encrypted, key)
        assert decrypted == original

    def test_large_content(self):
        original = b"x" * 100_000
        encrypted, key = encrypt_file(original)
        decrypted = decrypt_file(encrypted, key)
        assert decrypted == original

    def test_magic_header_present(self):
        encrypted, _ = encrypt_file(b"test")
        assert encrypted[:4] == b"MNME"


# ── storage: get_storage_path ─────────────────────────────────────────


class TestGetStoragePath:
    def test_returns_string(self):
        result = get_storage_path()
        assert isinstance(result, str)

    def test_returns_valid_path(self):
        result = get_storage_path()
        assert len(result) > 0
        assert Path(result).exists() or Path(result).parent.exists()

    def test_returns_absolute_path(self):
        result = get_storage_path()
        assert Path(result).is_absolute()

    def test_encrypted_returns_keys_dir(self):
        result = get_storage_path(encrypted=True)
        assert result.endswith("keys") or result.endswith("keys\\")

    def test_public_and_private_differ(self):
        pub = get_storage_path(encrypted=False)
        priv = get_storage_path(encrypted=True)
        assert pub != priv


# ── Full pipeline integration ─────────────────────────────────────────


class TestFullPipeline:
    """End-to-end: write file → parse → clean → chunk → encrypt → decrypt → verify."""

    def test_txt_pipeline(self, tmp_path):
        # 1. Create a test file
        content = "这是测试文档。\n" * 50
        src = tmp_path / "doc.txt"
        src.write_text(content, encoding="utf-8")

        # 2. process_file (parse → clean → chunk)
        result = process_file(str(src))
        assert result["filename"] == "doc.txt"
        assert result["word_count"] > 0
        assert len(result["chunks"]) > 0

        # 3. Encrypt the content
        raw_bytes = src.read_bytes()
        encrypted, key = encrypt_file(raw_bytes)
        assert encrypted[:4] == b"MNME"

        # 4. Write encrypted to "storage"
        stored = tmp_path / "private" / "doc.txt.enc"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(encrypted)

        # 5. Read and decrypt
        decrypted = decrypt_file(stored.read_bytes(), key)
        assert decrypted == raw_bytes

        # 6. Process decrypted file
        dec_file = tmp_path / "decrypted.txt"
        dec_file.write_bytes(decrypted)
        result2 = process_file(str(dec_file))
        assert result2["word_count"] == result["word_count"]
        assert len(result2["chunks"]) == len(result["chunks"])

    def test_encrypt_decrypt_content_match(self, tmp_path):
        """Encrypt → store → decrypt → content must be identical."""
        original = "机密内容：项目代号 Mneme\n" * 20
        src = tmp_path / "secret.txt"
        src.write_bytes(original.encode("utf-8"))

        encrypted, key = encrypt_file(src.read_bytes())

        stored = tmp_path / "private" / "secret.txt.enc"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(encrypted)

        decrypted = decrypt_file(stored.read_bytes(), key)
        assert decrypted == original.encode("utf-8")
        assert decrypted.decode("utf-8") == original

    def test_batch_files_pipeline(self, tmp_path):
        """Process multiple files through the full pipeline."""
        files = {
            "doc1.txt": "文档一的内容 " * 30,
            "doc2.txt": "文档二的内容 " * 30,
            "doc3.txt": "文档三的内容 " * 30,
        }
        key = generate_key()
        results = []

        for name, text in files.items():
            f = tmp_path / name
            f.write_text(text, encoding="utf-8")

            # Parse
            parsed = process_file(str(f))
            assert parsed["filename"] == name

            # Encrypt
            enc, _ = encrypt_file(f.read_bytes(), key=key)
            assert enc[:4] == b"MNME"

            # Store
            stored = tmp_path / "private" / f"{name}.enc"
            stored.parent.mkdir(parents=True, exist_ok=True)
            stored.write_bytes(enc)

            # Decrypt and verify
            dec = decrypt_file(stored.read_bytes(), key)
            assert dec == text.encode("utf-8")

            results.append(parsed)

        assert len(results) == 3
        assert all(r["word_count"] > 0 for r in results)


# ── test_docs integration ─────────────────────────────────────────────


@pytest.fixture
def test_docs_dir():
    """Locate test_docs/ relative to project root."""
    candidates = [
        Path(__file__).parent.parent / "test_docs",
        Path("test_docs"),
    ]
    for p in candidates:
        if p.is_dir():
            return p
    pytest.skip("test_docs/ directory not found")


class TestWithTestDocs:
    """Run facade APIs against the project's test_docs/ fixtures."""

    def test_process_whitespace_file(self, test_docs_dir):
        f = test_docs_dir / "test_A09_whitespace.txt"
        if not f.exists():
            pytest.skip("test_A09_whitespace.txt not found")
        result = process_file(str(f))
        assert result["word_count"] > 0
        assert len(result["chunks"]) > 0
        # cleaned chunks should not contain multiple consecutive spaces
        for chunk in result["chunks"]:
            assert "   " not in chunk

    def test_process_emptylines_file(self, test_docs_dir):
        f = test_docs_dir / "test_A10_emptylines.txt"
        if not f.exists():
            pytest.skip("test_A10_emptylines.txt not found")
        result = process_file(str(f))
        assert result["word_count"] > 0
        # chunks should not have 3+ consecutive newlines
        for chunk in result["chunks"]:
            assert "\n\n\n" not in chunk

    def test_process_control_file(self, test_docs_dir):
        f = test_docs_dir / "test_A11_control.txt"
        if not f.exists():
            pytest.skip("test_A11_control.txt not found")
        result = process_file(str(f))
        assert result["word_count"] > 0
        # chunks should not contain control characters
        for chunk in result["chunks"]:
            assert "\x00" not in chunk
            assert "\x07" not in chunk  # BEL

    def test_process_footer_file(self, test_docs_dir):
        f = test_docs_dir / "test_A12_footer.txt"
        if not f.exists():
            pytest.skip("test_A12_footer.txt not found")
        result = process_file(str(f))
        assert result["word_count"] > 0
        # page footers should be stripped from chunks
        for chunk in result["chunks"]:
            assert "第 1 页" not in chunk

    def test_process_chunking_file(self, test_docs_dir):
        f = test_docs_dir / "test_A13_chunking.txt"
        if not f.exists():
            pytest.skip("test_A13_chunking.txt not found")
        result = process_file(str(f))
        assert result["word_count"] > 0
        assert len(result["chunks"]) >= 1

    def test_parse_all_test_docs(self, test_docs_dir):
        """Every .txt in test_docs/ should parse without error."""
        txt_files = list(test_docs_dir.glob("*.txt"))
        if not txt_files:
            pytest.skip("no .txt files in test_docs/")
        for f in txt_files:
            text = parse_file(str(f))
            assert isinstance(text, str)
            assert len(text) > 0, f"{f.name} parsed to empty string"

    def test_encrypt_decrypt_all_test_docs(self, test_docs_dir):
        """Every .txt in test_docs/ should encrypt/decrypt roundtrip."""
        txt_files = list(test_docs_dir.glob("*.txt"))
        if not txt_files:
            pytest.skip("no .txt files in test_docs/")
        key = generate_key()
        for f in txt_files:
            original = f.read_bytes()
            encrypted, _ = encrypt_file(original, key=key)
            decrypted = decrypt_file(encrypted, key)
            assert decrypted == original, f"Roundtrip failed for {f.name}"


# ── Edge cases & error handling ───────────────────────────────────────


class TestEdgeCases:
    def test_process_file_empty_txt(self, tmp_path):
        """Empty txt file should return empty chunks."""
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = process_file(str(f))
        assert result["chunks"] == []
        assert result["word_count"] == 0

    def test_chunk_text_boundary_overlap(self):
        """overlap = size - 1 should work."""
        text = "A" * 100
        result = chunk_text(text, size=50, overlap=49)
        assert len(result) >= 1

    def test_encrypt_file_single_byte(self):
        """Single byte content should encrypt/decrypt."""
        encrypted, key = encrypt_file(b"\xff")
        assert decrypt_file(encrypted, key) == b"\xff"

    def test_storage_path_consistency(self):
        """Multiple calls return the same path."""
        p1 = get_storage_path()
        p2 = get_storage_path()
        assert p1 == p2

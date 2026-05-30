"""Mneme facade — unified API for parser, encrypt, storage.

Usage::

    from mneme.facade import parser, encrypt, storage

    result = parser.process_file("doc.pdf")
    encrypted, key = encrypt.encrypt_file(content)
    path = storage.get_storage_path()
"""

from mneme.facade import parser, encrypt, storage

__all__ = ["parser", "encrypt", "storage"]

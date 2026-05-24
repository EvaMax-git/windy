"""Setup Xiaomi MiMo provider in Mneme Gateway.

Usage:
    cd e:\\科电
    DATABASE_URL="postgresql+psycopg2://mneme:mneme_dev_password@localhost:5432/mneme" \\
    XIAOMI_MIMO_API_KEY="your-key" \\
    uv run python scripts/setup_xiaomi_mimo.py
"""

from __future__ import annotations

import os
import sys
from uuid import uuid4

sys.path.insert(0, ".")


def main():
    from mneme.db.base import SessionLocal
    from mneme.db.gateway import (
        create_provider,
        create_provider_model,
        create_capability,
        create_capability_binding,
        resolve_capability_binding,
    )
    from sqlalchemy import text
    from mneme.db.vault import create_credential
    from mneme.vault.encryption import get_vault_encryption

    api_key = os.environ.get("XIAOMI_MIMO_API_KEY", "")
    endpoint = os.environ.get("XIAOMI_MIMO_ENDPOINT", "https://token-plan-cn.xiaomimimo.com/anthropic")
    model_name = os.environ.get("XIAOMI_MIMO_MODEL", "MiMo")

    if not api_key:
        print("ERROR: Set XIAOMI_MIMO_API_KEY environment variable")
        sys.exit(1)

    vault = get_vault_encryption()
    ciphertext, key_wrap, fingerprint = vault.encrypt(api_key.encode("utf-8"))

    # 1. Provider (idempotent)
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT provider_id FROM providers WHERE provider_code = 'xiaomi_mimo'")
        ).fetchone()

    if row:
        provider_id = row[0]
        print(f"[OK] Provider (existing): {provider_id}")
    else:
        provider = create_provider(
            provider_code="xiaomi_mimo",
            name="小米 MiMo",
            provider_type="llm",
            status="active",
            endpoint_base=endpoint,
        )
        provider_id = provider["provider_id"]
        print(f"[OK] Provider (new): {provider_id}")

    # 2. Model (idempotent)
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT provider_model_id FROM provider_models WHERE provider_id = :pid AND model_code = :mc"),
            {"pid": provider_id, "mc": model_name},
        ).fetchone()

    if row:
        model_id = row[0]
        print(f"[OK] Model (existing): {model_id}")
    else:
        model = create_provider_model(
            provider_id=provider_id,
            model_code=model_name,
            external_model_id=model_name,
            model_type="chat",
            display_name=model_name,
            context_window_tokens=128000,
            status="active",
        )
        model_id = model["provider_model_id"]
        print(f"[OK] Model (new): {model_id}")

    # 3. Capability (reuse existing if present)
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT capability_id FROM capabilities WHERE capability_code = 'chat.completion'")
        ).fetchone()

    if row:
        cap_id = row[0]
        print(f"[OK] Capability (existing): {cap_id}")
    else:
        cap = create_capability(
            capability_code="chat.completion",
            name="Chat Completion",
            category="chat",
            risk_level="normal",
        )
        cap_id = cap["capability_id"]
        print(f"[OK] Capability (new): {cap_id}")

    # 4. Credential
    cred = create_credential(
        provider_id=provider_id,
        credential_name="小米 MiMo API Key",
        credential_type="api_key",
        status="active",
        ciphertext=ciphertext,
        key_wrap=key_wrap,
        key_version=vault.key_version,
        fingerprint=fingerprint,
        scope_json={},
        metadata_json={},
    )
    cred_id = cred["credential_id"]
    print(f"[OK] Credential: {cred_id}")

    # 5. Binding
    binding = create_capability_binding(
        capability_id=cap_id,
        provider_id=provider_id,
        provider_model_id=model_id,
        credential_id=cred_id,
        status="active",
    )
    binding_id = binding["capability_binding_id"]
    print(f"[OK] Binding: {binding_id}")

    print(f"\n[SUCCESS] 小米 MiMo 已配置!")
    print(f"  端点: {endpoint}")
    print(f"  模型: {model_name}")
    print(f"  格式: Anthropic 兼容")
    print(f"\n现在可以使用 /api/v4/ask 和 /api/v4/chat 调用 MiMo。")


if __name__ == "__main__":
    main()

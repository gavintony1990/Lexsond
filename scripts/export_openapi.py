from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from lexsond.web.app import create_app
from lexsond.web.auth import AuthConfiguration
from lexsond.credentials import ExecutionCredentialBinder


class _SchemaStore:
    def for_workspace(self, _workspace_id: str):
        return self


class _SchemaService:
    store = _SchemaStore()

    def operation(self):
        raise RuntimeError("schema export does not execute requests")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the FastAPI OpenAPI document")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    app = create_app(
        service=_SchemaService(),
        suite_path=PROJECT_ROOT / "suites/canary/openai-compatible.json",
        frontend_path=PROJECT_ROOT / ".openapi-no-frontend",
        auth_configuration=AuthConfiguration.from_values(
            auth_mode="required",
            listen_host="127.0.0.1",
            cookie_secure=True,
        ),
        authentication=object(),
        credential_binder=ExecutionCredentialBinder(b"openapi-contract-only-key-value-01"),
    )
    document = app.openapi()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

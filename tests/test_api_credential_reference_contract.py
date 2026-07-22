from __future__ import annotations

import unittest
from uuid import uuid4

from pydantic import ValidationError

from lexsond.web.api_models import CatalogRequest, RunCreate


class ApiCredentialReferenceContractTests(unittest.TestCase):
    def test_catalog_accepts_exactly_one_temporary_or_saved_credential(self) -> None:
        credential_id = uuid4()

        self.assertEqual(
            CatalogRequest(credential_profile_id=credential_id).credential_profile_id,
            credential_id,
        )
        self.assertIsNotNone(CatalogRequest(api_key="sk-temporary").api_key)
        with self.assertRaises(ValidationError):
            CatalogRequest(
                api_key="sk-temporary",
                credential_profile_id=credential_id,
            )

    def test_run_accepts_saved_credential_without_serializing_a_secret(self) -> None:
        credential_id = uuid4()
        value = RunCreate(
            target_id=uuid4(),
            model="gpt-test",
            credential_profile_id=credential_id,
        )

        serialized = value.model_dump(mode="json")

        self.assertEqual(serialized["credential_profile_id"], str(credential_id))
        self.assertIsNone(serialized["api_key"])
        with self.assertRaises(ValidationError):
            RunCreate(
                target_id=uuid4(),
                api_key="sk-temporary",
                credential_profile_id=credential_id,
            )


if __name__ == "__main__":
    unittest.main()

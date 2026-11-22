import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Literal
from zipfile import BadZipFile, ZipFile, ZipInfo
from zlib import error as CompressionError

from ecommerce_store_agent_orchestrator.domain.evidence import EvidenceModel, Sha256
from ecommerce_store_agent_orchestrator.domain.review import EvidenceId, GitSha, PullNumber

_SCENARIO_ID = "E2E_Catalog_BrowsePhoneDetails_01"
_MAX_JSON_BYTES = 1024 * 1024
_MAX_RESULTS_BYTES = 16 * _MAX_JSON_BYTES


class StorefrontManifest(EvidenceModel):
    schema_version: Literal[1]
    pr_number: PullNumber
    pr_head_sha: GitSha
    tested_commit_sha: GitSha
    feature_sha256: Sha256
    workflow_run_id: EvidenceId
    run_attempt: EvidenceId
    playwright_outcome: Literal["success", "failure"]


class IncompleteEvidenceError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class StorefrontProof:
    manifest: StorefrontManifest
    scenario_status: str


def read_storefront_proof(content: bytes) -> StorefrontProof:
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            manifests = [
                entry for entry in entries
                if entry.filename in ("storefront-evidence.json", "TestResults/storefront-evidence.json")
            ]
            if len(manifests) != 1 or manifests[0].file_size > _MAX_JSON_BYTES:
                raise IncompleteEvidenceError("storefront_manifest_missing")
            manifest = StorefrontManifest.model_validate_json(archive.read(manifests[0]))
            return StorefrontProof(manifest, _scenario_status(archive, entries))
    except IncompleteEvidenceError:
        raise
    except (BadZipFile, ValueError, RuntimeError, OSError, CompressionError) as error:
        raise IncompleteEvidenceError("invalid_storefront_evidence") from error


def _scenario_status(archive: ZipFile, entries: list[ZipInfo]) -> str:
    candidates: list[ZipInfo] = []
    for entry in entries:
        path = PurePosixPath(entry.filename)
        if path.parent.as_posix() in ("allure-results", "TestResults/allure-results") and path.name.endswith(
            "-result.json"
        ):
            candidates.append(entry)
    if sum(entry.file_size for entry in candidates) > _MAX_RESULTS_BYTES:
        raise IncompleteEvidenceError("invalid_storefront_evidence")

    matches = []
    for entry in candidates:
        if entry.file_size > _MAX_JSON_BYTES:
            raise IncompleteEvidenceError("invalid_storefront_evidence")
        result = json.loads(archive.read(entry))
        if not isinstance(result, dict) or not isinstance(result.get("labels"), list):
            continue
        if any(
            isinstance(label, dict) and label.get("name") == "scenarioId" and label.get("value") == _SCENARIO_ID
            for label in result["labels"]
        ):
            matches.append(result)
    if len(matches) != 1:
        raise IncompleteEvidenceError("scenario_result_missing")
    result = matches[0]
    status = result.get("status")
    if status not in ("passed", "failed", "broken", "skipped") or result.get("stage") not in (None, "finished"):
        raise IncompleteEvidenceError("invalid_storefront_evidence")
    return status

"""M0.1 Provider capability, size boundary, ROI comparison, Files & tool-image probe.

Strictly scoped to M0.1 verification (no business system).
Uses M0.0 explicit authorization and budget accounting from experiments.probe_gate.
Never marks documented_supported or untested capabilities as runtime_verified.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import httpx2
import openai
import yaml
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolMessageParam
from PIL import Image, ImageDraw

from experiments.probe_gate import (
    ProbeBudgetLedger,
    ProbeGateConfig,
    ProbeGateError,
    sanitize_record,
    validate_files_upload_authorization,
    validate_live_gate,
)

CapabilityStatus = Literal[
    "documented_supported",
    "runtime_verified",
    "unsupported",
    "not_tested",
]

VALID_CAPABILITY_STATUSES: frozenset[str] = frozenset(
    {"documented_supported", "runtime_verified", "unsupported", "not_tested"}
)

VALID_REGION_IDS: tuple[str, ...] = (
    "full",
    "r1c1",
    "r1c2",
    "r1c3",
    "r2c1",
    "r2c2",
    "r2c3",
    "r3c1",
    "r3c2",
    "r3c3",
)

TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({408, 429, 500, 503, 504})
DEFAULT_PROVIDER_CONFIG_PATH = Path("experiments/config/provider_deepseek.yaml")
DEFAULT_BUDGET_CONFIG_PATH = Path("experiments/config/probe_budget.example.yaml")
DEFAULT_CAPABILITIES_OUT = Path("experiments/capabilities.json")
DEFAULT_OFFLINE_REPORT_OUT = Path("experiments/reports/m0_1_offline_run_record.json")


class CapabilityContractError(ValueError):
    """Raised when a capability matrix violates M0.1 status integrity invariants."""


@dataclass(frozen=True, slots=True)
class ProviderDocumentedLimits:
    """Official provider contract limits loaded from YAML configuration."""

    supported_image_formats: tuple[str, ...]
    max_request_body_bytes: int
    max_single_inline_image_bytes: int
    max_single_file_id_image_bytes: int
    max_images_per_request: int
    max_total_image_bytes_inline: int
    max_total_image_bytes_with_files: int
    max_image_side_px_under_15_images: int
    image_count_dimension_threshold: int
    max_image_side_px_at_or_above_15_images: int
    detail_values: tuple[str, ...]
    low_detail_downscale_px: int
    internal_resize_approx_pixels_side: int
    internal_max_tokens_per_image: int
    files_api_min_ttl_seconds: int
    files_api_max_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class ProviderContractConfig:
    """Configured provider & model contract (model IDs are config, never hardcoded)."""

    provider: str
    api_family: str
    base_url: str
    api_key_env: str
    model_id: str
    documented_model_version: str
    text_only_reference_model_id: str
    documented_text_only_model_version: str
    snapshot_date: str
    official_sources: dict[str, str]
    limits: ProviderDocumentedLimits
    project_p0_limits: dict[str, Any]
    pricing_reference: dict[str, Any]

    @classmethod
    def from_yaml(
        cls,
        path: Path,
        *,
        model_id_override: str | None = None,
    ) -> ProviderContractConfig:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError(f"Invalid provider config YAML: {path}")
        oc = raw["official_contract"]
        lim = raw["documented_limits"]
        model_id = (model_id_override or str(raw["model_id"])).strip()
        if not model_id:
            raise ValueError("Configured model_id must be non-empty")
        return cls(
            provider=str(raw["provider"]),
            api_family=str(raw["api_family"]),
            base_url=str(raw["base_url"]).rstrip("/"),
            api_key_env=str(raw["api_key_env"]),
            model_id=model_id,
            documented_model_version=str(raw["documented_model_version"]),
            text_only_reference_model_id=str(raw["text_only_reference_model_id"]),
            documented_text_only_model_version=str(raw["documented_text_only_model_version"]),
            snapshot_date=str(oc["snapshot_date"]),
            official_sources={str(k): str(v) for k, v in oc["sources"].items()},
            limits=ProviderDocumentedLimits(
                supported_image_formats=tuple(str(x) for x in lim["supported_image_formats"]),
                max_request_body_bytes=int(lim["max_request_body_bytes"]),
                max_single_inline_image_bytes=int(lim["max_single_inline_image_bytes"]),
                max_single_file_id_image_bytes=int(lim["max_single_file_id_image_bytes"]),
                max_images_per_request=int(lim["max_images_per_request"]),
                max_total_image_bytes_inline=int(lim["max_total_image_bytes_inline"]),
                max_total_image_bytes_with_files=int(lim["max_total_image_bytes_with_files"]),
                max_image_side_px_under_15_images=int(lim["max_image_side_px_under_15_images"]),
                image_count_dimension_threshold=int(lim["image_count_dimension_threshold"]),
                max_image_side_px_at_or_above_15_images=int(
                    lim["max_image_side_px_at_or_above_15_images"]
                ),
                detail_values=tuple(str(x) for x in lim["detail_values"]),
                low_detail_downscale_px=int(lim["low_detail_downscale_px"]),
                internal_resize_approx_pixels_side=int(lim["internal_resize_approx_pixels_side"]),
                internal_max_tokens_per_image=int(lim["internal_max_tokens_per_image"]),
                files_api_min_ttl_seconds=int(lim["files_api_min_ttl_seconds"]),
                files_api_max_ttl_seconds=int(lim["files_api_max_ttl_seconds"]),
            ),
            project_p0_limits=dict(raw["project_p0_limits"]),
            pricing_reference=dict(raw["pricing_reference"]),
        )


@dataclass(slots=True)
class CapabilityErrorRecord:
    """Preserved error reason on a probe failure (never silently dropped)."""

    error_code: str
    message: str
    http_status: int | None = None
    attempt_id: str | None = None
    timestamp_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "http_status": self.http_status,
            "message": self.message,
            "attempt_id": self.attempt_id,
            "timestamp_utc": self.timestamp_utc,
        }


@dataclass(slots=True)
class CapabilityEntry:
    """Single capability row in the M0.1 capability matrix."""

    capability_id: str
    description: str
    status: CapabilityStatus
    documented_status: CapabilityStatus
    doc_source: str | None
    live_executed: bool = False
    verification_passed: bool | None = None
    runtime_evidence_id: str | None = None
    failure_count: int = 0
    last_error: CapabilityErrorRecord | None = None
    notes: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def record_runtime_success(
        self,
        *,
        evidence_run_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if not evidence_run_id.strip():
            raise CapabilityContractError(
                "runtime_verified requires a non-empty runtime evidence run ID"
            )
        self.live_executed = True
        self.verification_passed = True
        self.runtime_evidence_id = evidence_run_id
        self.status = "runtime_verified"
        if details:
            self.details.update(dict(details))

    def record_runtime_failure(
        self,
        *,
        error_code: str,
        message: str,
        http_status: int | None = None,
        attempt_id: str | None = None,
        mark_unsupported: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Preserve first/latest failure reason; NEVER promote to runtime_verified."""
        self.live_executed = True
        self.verification_passed = False
        self.failure_count += 1
        self.last_error = CapabilityErrorRecord(
            error_code=error_code,
            http_status=http_status,
            message=message,
            attempt_id=attempt_id,
        )
        if mark_unsupported:
            self.status = "unsupported"
        elif self.status == "runtime_verified":
            # Never leave a failed capability marked runtime_verified.
            self.status = self.documented_status
        if details:
            self.details.update(dict(details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "description": self.description,
            "status": self.status,
            "documented_status": self.documented_status,
            "doc_source": self.doc_source,
            "live_executed": self.live_executed,
            "verification_passed": self.verification_passed,
            "runtime_evidence_id": self.runtime_evidence_id,
            "failure_count": self.failure_count,
            "last_error": self.last_error.to_dict() if self.last_error else None,
            "notes": self.notes,
            "details": self.details,
        }


def validate_capability_entry(entry: Mapping[str, Any]) -> None:
    """Enforce M0.1 capability status invariants on serialized entries."""
    status = entry.get("status")
    if status not in VALID_CAPABILITY_STATUSES:
        raise CapabilityContractError(f"Invalid capability status: {status!r}")
    live_executed = entry.get("live_executed") is True
    verification_passed = entry.get("verification_passed")
    evidence_id = entry.get("runtime_evidence_id")

    if status == "runtime_verified":
        if not live_executed:
            raise CapabilityContractError(
                f"Capability {entry.get('capability_id')!r} marked runtime_verified "
                "without live_executed=True"
            )
        if verification_passed is not True:
            raise CapabilityContractError(
                f"Capability {entry.get('capability_id')!r} marked runtime_verified "
                "without verification_passed=True"
            )
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise CapabilityContractError(
                f"Capability {entry.get('capability_id')!r} marked runtime_verified "
                "without non-empty runtime_evidence_id"
            )

    if live_executed and verification_passed is False and entry.get("last_error") is None:
        raise CapabilityContractError(
            f"Capability {entry.get('capability_id')!r} failed live execution "
            "without preserving last_error reason"
        )


def validate_capability_matrix(matrix: Mapping[str, Any]) -> None:
    """Validate all rows in a capability matrix dictionary."""
    capabilities = matrix.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise CapabilityContractError("Capability matrix must include non-empty 'capabilities'")
    for row in capabilities:
        if not isinstance(row, Mapping):
            raise CapabilityContractError("Each capability row must be a mapping")
        validate_capability_entry(row)


def classify_http_error(status_code: int, message: str = "") -> tuple[str, bool]:
    """Normalize DeepSeek/OpenAI-compatible HTTP status codes -> (error_code, retryable)."""
    if status_code == 400:
        return ("BAD_REQUEST_INVALID_FORMAT", False)
    if status_code == 401:
        return ("AUTH_FAILED", False)
    if status_code == 402:
        return ("INSUFFICIENT_BALANCE", False)
    if status_code == 403:
        return ("FORBIDDEN", False)
    if status_code == 404:
        return ("ENDPOINT_OR_RESOURCE_NOT_FOUND", False)
    if status_code == 408:
        return ("REQUEST_TIMEOUT", True)
    if status_code == 413:
        return ("REQUEST_TOO_LARGE", False)
    if status_code == 422:
        return ("INVALID_PARAMETERS", False)
    if status_code == 429:
        return ("RATE_LIMIT_REACHED", True)
    if status_code in {500, 502, 503, 504}:
        return (f"TRANSIENT_SERVER_{status_code}", True)
    return (f"HTTP_{status_code}", False)


def serialize_json_body(payload: Mapping[str, Any]) -> bytes:
    """Serialize JSON request payload identically to wire body to measure exact byte size."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_image_data_url(image_bytes: bytes, mime_type: str = "image/png") -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


def image_to_bytes(img: Image.Image, *, fmt: Literal["PNG", "JPEG"] = "PNG") -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(buf, format="JPEG", quality=90)
    else:
        img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@dataclass(frozen=True, slots=True)
class EncodedProbeImage:
    """Metadata and bytes for an offline-constructed or authorized probe image."""

    label: str
    width: int
    height: int
    mime_type: str
    raw_bytes: bytes
    sha256: str
    detail: str | None = None
    region_id: str = "full"
    selection_preview_only: bool = False

    @property
    def max_side_px(self) -> int:
        return max(self.width, self.height)

    def to_image_url_part(self) -> dict[str, Any]:
        image_url: dict[str, Any] = {
            "url": encode_image_data_url(self.raw_bytes, self.mime_type),
        }
        if self.detail is not None:
            image_url["detail"] = self.detail
        return {"type": "image_url", "image_url": image_url}


def make_encoded_image(
    img: Image.Image,
    *,
    label: str,
    fmt: Literal["PNG", "JPEG"] = "PNG",
    detail: str | None = None,
    region_id: str = "full",
    selection_preview_only: bool = False,
) -> EncodedProbeImage:
    raw = image_to_bytes(img, fmt=fmt)
    mime = "image/jpeg" if fmt == "JPEG" else "image/png"
    return EncodedProbeImage(
        label=label,
        width=img.width,
        height=img.height,
        mime_type=mime,
        raw_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        detail=detail,
        region_id=region_id,
        selection_preview_only=selection_preview_only,
    )


def make_synthetic_boundary_image(
    width: int,
    height: int,
    *,
    label: str,
    detail: str = "high",
) -> EncodedProbeImage:
    """Create a real PNG image with exact pixel dimensions (e.g. 4096x64 or 4097x64)."""
    img = Image.new("RGB", (width, height), color=(240, 244, 248))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=(30, 80, 160), width=2)
    draw.text((12, max(4, height // 4)), f"{label} {width}x{height}", fill=(10, 10, 10))
    return make_encoded_image(img, label=label, fmt="PNG", detail=detail)


def make_ordered_marker_images(count: int = 3) -> list[EncodedProbeImage]:
    """Generate distinct ordered synthetic frames with explicit sequence markers."""
    colors = [(220, 60, 60), (60, 170, 80), (60, 110, 220), (200, 150, 40)]
    images: list[EncodedProbeImage] = []
    for idx in range(1, count + 1):
        img = Image.new("RGB", (320, 180), color=colors[(idx - 1) % len(colors)])
        draw = ImageDraw.Draw(img)
        draw.rectangle([(16, 16), (304, 164)], fill=(255, 255, 255), outline=(0, 0, 0), width=3)
        draw.text((40, 75), f"FRAME_SEQ_{idx:02d}", fill=(0, 0, 0))
        images.append(make_encoded_image(img, label=f"seq_{idx:02d}", fmt="PNG", detail="low"))
    return images


def build_synthetic_software_tutorial_frame(
    width: int = 1920,
    height: int = 1080,
) -> Image.Image:
    """Build a synthetic 1080p CAD/software-tutorial-like UI frame for offline payload tests.

    NOTE: Synthetic UI frames measure serialization bytes and cropping geometry ONLY;
    they do NOT replace real authorized software_tutorial video frames for model quality claims.
    """
    img = Image.new("RGB", (width, height), color=(236, 238, 242))
    draw = ImageDraw.Draw(img)
    # Top toolbar
    draw.rectangle([(0, 0), (width - 1, 88)], fill=(45, 55, 72))
    draw.text(
        (24, 30),
        "CAD Tutorial UI (Synthetic Fixture) - Extrude / Hole Dialog",
        fill=(255, 255, 255),
    )
    # Left parameter dialog centered around r2c2 / r2c1 boundary, target fields inside r2c2
    panel_box = (660, 380, 1260, 700)
    draw.rectangle(panel_box, fill=(255, 255, 255), outline=(60, 60, 60), width=2)
    draw.rectangle([(660, 380), (1260, 420)], fill=(210, 220, 235))
    draw.text((680, 392), "Hole Parameters / Kongjing Settings", fill=(20, 20, 20))
    draw.text((690, 460), "Diameter (mm): 20", fill=(10, 10, 10))
    draw.text((690, 505), "Depth (mm): 45", fill=(10, 10, 10))
    draw.text((690, 550), "Tolerance: +/-0.05", fill=(10, 10, 10))
    draw.text((690, 595), "Reference Check (Not 200): 20", fill=(180, 20, 20))
    return img


def render_960_overview(img: Image.Image) -> Image.Image:
    """Resize preserving aspect ratio so max(width, height) <= 960."""
    max_side = max(img.width, img.height)
    if max_side <= 960:
        return img.copy()
    scale = 960.0 / float(max_side)
    new_w = max(1, int(round(img.width * scale)))
    new_h = max(1, int(round(img.height * scale)))
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def render_numbered_3x3_grid_preview(img: Image.Image) -> Image.Image:
    """Render a 3x3 numbered grid (r1c1..r3c3) on a copy for region selection ONLY."""
    preview = img.copy()
    draw = ImageDraw.Draw(preview)
    w, h = preview.size
    for i in (1, 2):
        x = (w * i) // 3
        y = (h * i) // 3
        draw.line([(x, 0), (x, h)], fill=(220, 40, 40), width=2)
        draw.line([(0, y), (w, y)], fill=(220, 40, 40), width=2)
    for r in range(3):
        for c in range(3):
            x0 = (w * c) // 3 + 8
            y0 = (h * r) // 3 + 8
            tag = f"r{r + 1}c{c + 1}"
            draw.rectangle([(x0 - 2, y0 - 2), (x0 + 42, y0 + 18)], fill=(255, 255, 220))
            draw.text((x0 + 2, y0 + 2), tag, fill=(180, 0, 0))
    return preview


def crop_grid_roi_from_original(
    original: Image.Image,
    region_id: str,
    *,
    pad_ratio: float = 0.10,
) -> tuple[Image.Image, dict[str, int]]:
    """Crop r1c1..r3c3 (or full) from the UNGRIDDED original image with bounded padding."""
    if region_id not in VALID_REGION_IDS:
        raise ValueError(f"Invalid region_id: {region_id!r}")
    w, h = original.size
    if region_id == "full":
        return original.copy(), {"x0": 0, "y0": 0, "x1": w, "y1": h}

    row = int(region_id[1]) - 1
    col = int(region_id[3]) - 1
    cell_w = w / 3.0
    cell_h = h / 3.0
    pad_x = cell_w * pad_ratio
    pad_y = cell_h * pad_ratio

    x0 = max(0, int(round(col * cell_w - pad_x)))
    y0 = max(0, int(round(row * cell_h - pad_y)))
    x1 = min(w, int(round((col + 1) * cell_w + pad_x)))
    y1 = min(h, int(round((row + 1) * cell_h + pad_y)))
    cropped = original.crop((x0, y0, x1, y1))
    return cropped, {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def build_chat_payload(
    *,
    model_id: str,
    messages: Sequence[Mapping[str, Any]],
    response_format: Mapping[str, Any] | None = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    disable_thinking: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": list(messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format is not None:
        payload["response_format"] = dict(response_format)
    if tools is not None:
        payload["tools"] = list(tools)
    if disable_thinking:
        payload["thinking"] = {"type": "disabled"}
    return payload


def count_images_in_messages(messages: Sequence[Mapping[str, Any]]) -> int:
    """Count all image/file content parts across all messages in a request."""
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, Mapping) and part.get("type") in {
                    "image_url",
                    "input_image",
                    "file",
                }:
                    total += 1
    return total


def evaluate_image_boundary_case(
    *,
    case_id: str,
    model_id: str,
    images: Sequence[EncodedProbeImage],
    limits: ProviderDocumentedLimits,
    simulated_payload_bytes: int | None = None,
) -> dict[str, Any]:
    """Offline validation of 14/15/16 image counts, 4096/4097px dimensions, and 48 MiB body cap.

    Explicitly verifies whether the case exercises the >=15 images 4096px boundary;
    ordinary 1080p images CANNOT prove this limit was tested.
    """
    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": f"Boundary probe case {case_id} ({len(images)} images)."}
    ]
    content_parts.extend(img.to_image_url_part() for img in images)
    messages = [{"role": "user", "content": content_parts}]
    payload = build_chat_payload(model_id=model_id, messages=messages, max_tokens=64)
    serialized_body = serialize_json_body(payload)
    actual_body_bytes = len(serialized_body)
    serialized_body_sha256 = hashlib.sha256(serialized_body).hexdigest()
    effective_body_bytes = (
        simulated_payload_bytes if simulated_payload_bytes is not None else actual_body_bytes
    )

    image_count = count_images_in_messages(messages)
    max_side_px = max((img.max_side_px for img in images), default=0)
    max_single_bytes = max((len(img.raw_bytes) for img in images), default=0)
    total_raw_image_bytes = sum(len(img.raw_bytes) for img in images)

    effective_max_side = (
        limits.max_image_side_px_at_or_above_15_images
        if image_count >= limits.image_count_dimension_threshold
        else limits.max_image_side_px_under_15_images
    )

    violations: list[str] = []
    if image_count > limits.max_images_per_request:
        violations.append("MAX_IMAGES_PER_REQUEST_EXCEEDED")
    if max_side_px > effective_max_side:
        violations.append("IMAGE_DIMENSION_LIMIT_EXCEEDED")
    if max_single_bytes > limits.max_single_inline_image_bytes:
        violations.append("SINGLE_INLINE_IMAGE_SIZE_EXCEEDED")
    if total_raw_image_bytes > limits.max_total_image_bytes_inline:
        violations.append("TOTAL_INLINE_IMAGE_SIZE_EXCEEDED")
    if effective_body_bytes > limits.max_request_body_bytes:
        violations.append("REQUEST_BODY_LIMIT_EXCEEDED")

    exercises_15_img_4096_boundary = (
        image_count >= limits.image_count_dimension_threshold
        and max_side_px >= limits.max_image_side_px_at_or_above_15_images
    )
    triggers_15_img_4096_rejection = (
        image_count >= limits.image_count_dimension_threshold
        and max_side_px > limits.max_image_side_px_at_or_above_15_images
    )
    ordinary_1080p_note: str | None = None
    if max_side_px <= 1920:
        ordinary_1080p_note = (
            "ordinary_1080p_cannot_prove_4096px_limit: max_side_px<=1920 does not cross 4096px"
        )

    return {
        "case_id": case_id,
        "image_count": image_count,
        "max_side_px": max_side_px,
        "effective_max_side_px": effective_max_side,
        "actual_serialized_body_bytes": actual_body_bytes,
        "serialized_request_body_sha256": serialized_body_sha256,
        "evaluated_body_bytes": effective_body_bytes,
        "max_request_body_bytes": limits.max_request_body_bytes,
        "allowed_by_documented_contract": len(violations) == 0,
        "violations": violations,
        "exercises_15_img_4096_boundary": exercises_15_img_4096_boundary,
        "triggers_15_img_4096_rejection": triggers_15_img_4096_rejection,
        "ordinary_1080p_note": ordinary_1080p_note,
    }


def run_offline_boundary_suite(config: ProviderContractConfig) -> dict[str, Any]:
    """Run offline 14/15/16 image x 4096/4097px + 1080p non-proof + 48 MiB body checks."""
    img_4096 = make_synthetic_boundary_image(4096, 64, label="synth_4096")
    img_4097 = make_synthetic_boundary_image(4097, 64, label="synth_4097")
    img_1080p = make_synthetic_boundary_image(1920, 1080, label="synth_1080p")

    cases = [
        evaluate_image_boundary_case(
            case_id="14_images_4096px",
            model_id=config.model_id,
            images=[img_4096] * 14,
            limits=config.limits,
        ),
        evaluate_image_boundary_case(
            case_id="14_images_4097px",
            model_id=config.model_id,
            images=[img_4097] * 14,
            limits=config.limits,
        ),
        evaluate_image_boundary_case(
            case_id="15_images_4096px",
            model_id=config.model_id,
            images=[img_4096] * 15,
            limits=config.limits,
        ),
        evaluate_image_boundary_case(
            case_id="15_images_4097px",
            model_id=config.model_id,
            images=[img_4097] * 15,
            limits=config.limits,
        ),
        evaluate_image_boundary_case(
            case_id="16_images_4096px",
            model_id=config.model_id,
            images=[img_4096] * 16,
            limits=config.limits,
        ),
        evaluate_image_boundary_case(
            case_id="16_images_4097px",
            model_id=config.model_id,
            images=[img_4097] * 16,
            limits=config.limits,
        ),
        evaluate_image_boundary_case(
            case_id="15_images_ordinary_1080p_non_proof",
            model_id=config.model_id,
            images=[img_1080p] * 15,
            limits=config.limits,
        ),
        evaluate_image_boundary_case(
            case_id="body_over_48mib_offline_guard",
            model_id=config.model_id,
            images=[img_1080p],
            limits=config.limits,
            simulated_payload_bytes=config.limits.max_request_body_bytes + 1,
        ),
    ]

    proves_boundary_offline = any(c["triggers_15_img_4096_rejection"] for c in cases) and any(
        c["case_id"] == "15_images_4096px" and c["allowed_by_documented_contract"] for c in cases
    )
    return {
        "execution_mode": "offline_contract_check",
        "live_executed": False,
        "proves_15_image_4096_contract_offline": proves_boundary_offline,
        "ordinary_1080p_proves_4096_limit": False,
        "project_vs_provider_note": (
            "Project P0 caps (8 index / 16 QA images per request) are separate from provider caps "
            "(600 images max, 4096px side limit at >=15 images, 48 MiB serialized body)."
        ),
        "cases": cases,
    }


def prepare_software_tutorial_comparison(
    *,
    config: ProviderContractConfig,
    frame_image: Image.Image,
    source_kind: Literal["authorized_real_frame", "synthetic_offline_fixture"],
    roi_region_id: str = "r2c2",
) -> dict[str, Any]:
    """Build Strategy A (original frame detail=high) vs Strategy B (960px + 3x3 grid + ROI crop).

    Records exact serialized JSON request body bytes for both strategies and explicitly notes
    that detail='high' does NOT guarantee small-text readability.
    """
    orig_encoded = make_encoded_image(
        frame_image,
        label="tutorial_original_high",
        fmt="JPEG",
        detail="high",
        region_id="full",
    )
    prompt_a = (
        "Read the software dialog in this original frame and return JSON with fields "
        "{observed_labels, numeric_values, uncertain_items}. If small text is unclear, "
        "record it in uncertain_items without guessing."
    )
    payload_a = build_chat_payload(
        model_id=config.model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_a},
                    orig_encoded.to_image_url_part(),
                ],
            }
        ],
        response_format={"type": "json_object"},
    )
    serialized_a = serialize_json_body(payload_a)
    body_bytes_a = len(serialized_a)
    body_sha256_a = hashlib.sha256(serialized_a).hexdigest()

    overview_960 = render_960_overview(frame_image)
    grid_preview_img = render_numbered_3x3_grid_preview(overview_960)
    roi_crop_img, crop_bbox = crop_grid_roi_from_original(frame_image, roi_region_id)

    overview_encoded = make_encoded_image(
        overview_960,
        label="tutorial_overview_960",
        fmt="JPEG",
        detail="high",
        region_id="full",
    )
    grid_preview_encoded = make_encoded_image(
        grid_preview_img,
        label="tutorial_grid_preview_960",
        fmt="JPEG",
        detail="low",
        region_id="full",
        selection_preview_only=True,
    )
    roi_encoded = make_encoded_image(
        roi_crop_img,
        label=f"tutorial_roi_{roi_region_id}_ungridded",
        fmt="JPEG",
        detail="high",
        region_id=roi_region_id,
        selection_preview_only=False,
    )

    prompt_b = (
        "Use the 960px overview and the ungridded ROI crop from region "
        f"{roi_region_id} (selected via 3x3 numbered grid) to return JSON with fields "
        "{observed_labels, numeric_values, region_hint, uncertain_items}. "
        "Grid overlay is for region selection only, not fact evidence."
    )
    payload_b = build_chat_payload(
        model_id=config.model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_b},
                    overview_encoded.to_image_url_part(),
                    grid_preview_encoded.to_image_url_part(),
                    roi_encoded.to_image_url_part(),
                ],
            }
        ],
        response_format={"type": "json_object"},
    )
    serialized_b = serialize_json_body(payload_b)
    body_bytes_b = len(serialized_b)
    body_sha256_b = hashlib.sha256(serialized_b).hexdigest()

    return {
        "source_kind": source_kind,
        "high_guarantees_small_text_legibility": False,
        "vendor_internal_resize_note": (
            f"Official Vision docs state detail='high' equals 'original' (no 512px low pre-scale), "
            f"but before inference every image is still resized to ~"
            f"{config.limits.internal_resize_approx_pixels_side}x"
            f"{config.limits.internal_resize_approx_pixels_side} "
            f"(capped at {config.limits.internal_max_tokens_per_image} tokens/image). "
            "Therefore detail='high' does NOT guarantee small text is always readable."
        ),
        "strategy_a_original_high": {
            "name": "original_frame_detail_high",
            "detail": "high",
            "image_dimensions": [[orig_encoded.width, orig_encoded.height]],
            "image_parts": 1,
            "raw_image_bytes": len(orig_encoded.raw_bytes),
            "serialized_request_body_bytes": body_bytes_a,
            "serialized_request_body_sha256": body_sha256_a,
            "within_48mib_limit": body_bytes_a <= config.limits.max_request_body_bytes,
            "latency_ms": None,
            "usage": None,
            "quality_status": "not_tested_live",
            "payload": payload_a,
        },
        "strategy_b_960_plus_grid_roi": {
            "name": "overview_960_plus_numbered_grid_roi",
            "roi_region_id": roi_region_id,
            "roi_cropped_from_ungridded_original": True,
            "roi_crop_bbox_px": crop_bbox,
            "grid_preview_is_fact_evidence": False,
            "image_dimensions": [
                [overview_encoded.width, overview_encoded.height],
                [grid_preview_encoded.width, grid_preview_encoded.height],
                [roi_encoded.width, roi_encoded.height],
            ],
            "image_parts": 3,
            "raw_image_bytes": (
                len(overview_encoded.raw_bytes)
                + len(grid_preview_encoded.raw_bytes)
                + len(roi_encoded.raw_bytes)
            ),
            "serialized_request_body_bytes": body_bytes_b,
            "serialized_request_body_sha256": body_sha256_b,
            "within_48mib_limit": body_bytes_b <= config.limits.max_request_body_bytes,
            "latency_ms": None,
            "usage": None,
            "quality_status": "not_tested_live",
            "payload": payload_b,
        },
    }


def inspect_openai_sdk_tool_image_support(model_id: str) -> dict[str, Any]:
    """Distinguish official endpoint docs vs current OpenAI SDK static typing & wire serialization.

    Runs 100% offline using httpx.MockTransport (zero sockets opened).
    """
    annotations = ChatCompletionToolMessageParam.__annotations__
    content_annotation = str(annotations.get("content", ""))
    sdk_static_type_supports_image = "Image" in content_annotation

    captured_body: dict[str, Any] = {}

    def _mock_handler(request: httpx2.Request) -> httpx2.Response:
        captured_body["raw"] = json.loads(request.content.decode("utf-8"))
        return httpx2.Response(
            status_code=200,
            json={
                "id": "chatcmpl-offline-sdk-check",
                "object": "chat.completion",
                "created": 1700000000,
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "offline-serialized-ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            },
        )

    tiny_img = make_synthetic_boundary_image(32, 32, label="tool_tiny", detail="low")
    tool_call_id = "call_m0_tool_img_001"
    raw_messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Inspect frame 1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": "inspect_clip",
                        "arguments": '{"start_ms":0,"end_ms":1000}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": [
                {"type": "text", "text": "Attached synthetic frame for SDK wire check"},
                tiny_img.to_image_url_part(),
            ],
        },
    ]

    wire_serialized_ok = False
    wire_preserves_tool_call_id = False
    wire_preserves_image_url = False
    sdk_serialization_error: str | None = None

    with httpx2.Client(transport=httpx2.MockTransport(_mock_handler)) as http_client:
        client = openai.OpenAI(
            api_key="sk-offline-sdk-serialization-check",
            base_url="https://api.deepseek.com",
            max_retries=0,
            http_client=http_client,
        )
        try:
            client.chat.completions.create(
                model=model_id,
                messages=cast(list[ChatCompletionMessageParam], raw_messages),
                max_tokens=16,
            )
            sent = captured_body.get("raw", {})
            sent_msgs = sent.get("messages", [])
            tool_msgs = [m for m in sent_msgs if m.get("role") == "tool"]
            if tool_msgs:
                tmsg = tool_msgs[0]
                wire_preserves_tool_call_id = tmsg.get("tool_call_id") == tool_call_id
                tcontent = tmsg.get("content")
                if isinstance(tcontent, list):
                    wire_preserves_image_url = any(
                        isinstance(p, dict) and p.get("type") == "image_url" for p in tcontent
                    )
                wire_serialized_ok = wire_preserves_tool_call_id and wire_preserves_image_url
        except Exception as exc:  # pragma: no cover - defensive capture
            sdk_serialization_error = f"{type(exc).__name__}: {exc}"

    return {
        "sdk_name": "openai",
        "sdk_version": openai.__version__,
        "endpoint_documented_status": "documented_supported",
        "sdk_static_type_annotation_for_tool_content": content_annotation,
        "sdk_static_type_supports_image_part": sdk_static_type_supports_image,
        "sdk_offline_wire_serialization_verified": wire_serialized_ok,
        "wire_preserves_tool_call_id": wire_preserves_tool_call_id,
        "wire_preserves_image_url_part": wire_preserves_image_url,
        "sdk_serialization_error": sdk_serialization_error,
        "endpoint_runtime_status": "documented_supported",
        "p0_default_tool_return_mode": "text_observation_json",
        "note": (
            "Official DeepSeek Vision docs document image support in role='tool' messages, "
            "but installed OpenAI SDK static typing (ChatCompletionToolMessageParam) only types "
            "text content parts (requires untyped dict/cast to serialize image_url). Live endpoint "
            "round-trip remains unverified until --live runs; P0 defaults to text Observation JSON."
        ),
    }


def parse_structured_json_response(content: str | None) -> tuple[bool, dict[str, Any] | None, str]:
    """Parse JSON Output response, explicitly catching DeepSeek's U+0020 whitespace fallback."""
    if content is None:
        return (False, None, "EMPTY_NULL_CONTENT")
    if content == "" or content.strip() == "":
        return (False, None, "WHITESPACE_ONLY_JSON_FALLBACK")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return (False, None, f"JSON_DECODE_ERROR: {exc.msg}")
    if not isinstance(parsed, dict):
        return (False, None, "JSON_ROOT_NOT_OBJECT")
    return (True, parsed, "OK")


class ProviderProbeClient:
    """Budgeted, zero-SDK-retry HTTP probe client supporting MockTransport for offline tests."""

    def __init__(
        self,
        *,
        config: ProviderContractConfig,
        ledger: ProbeBudgetLedger,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self._api_key = api_key
        # SDK/transport retries strictly disabled; app retries are explicit and ledger-accounted.
        self._client = httpx.Client(
            base_url=config.base_url,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ProviderProbeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def request_model_json(
        self,
        *,
        operation: str,
        payload: Mapping[str, Any],
        image_parts: int = 0,
    ) -> dict[str, Any]:
        """POST /chat/completions with reserve-before-send, app retries, and exact body bytes."""
        body_bytes = serialize_json_body(payload)
        payload_len = len(body_bytes)
        if payload_len > self.config.limits.max_request_body_bytes:
            raise ProbeGateError(
                "REQUEST_BODY_LIMIT_EXCEEDED",
                (
                    f"Serialized body {payload_len} bytes exceeds documented 48 MiB limit "
                    f"({self.config.limits.max_request_body_bytes})."
                ),
            )

        max_tries = 1 + self.ledger.budget.max_retries_per_request
        last_result: dict[str, Any] = {}
        for try_idx in range(max_tries):
            attempt, timeout_s = self.ledger.reserve_model_attempt(
                operation=operation if try_idx == 0 else f"{operation}.retry_{try_idx}",
                payload_bytes=payload_len,
                image_parts=image_parts,
            )
            t0 = time.monotonic()
            try:
                resp = self._client.post(
                    "/chat/completions",
                    content=body_bytes,
                    headers={"Content-Type": "application/json"},
                    timeout=timeout_s,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                latency_ms = int((time.monotonic() - t0) * 1000)
                self.ledger.settle(
                    attempt,
                    status="unknown",
                    error_code=type(exc).__name__,
                    latency_ms=latency_ms,
                )
                return {
                    "ok": False,
                    "attempt_id": attempt.attempt_id,
                    "http_status": None,
                    "error_code": f"NETWORK_OR_TIMEOUT_UNKNOWN:{type(exc).__name__}",
                    "message": str(exc),
                    "latency_ms": latency_ms,
                    "serialized_body_bytes": payload_len,
                    "usage": None,
                    "response_model": None,
                    "request_id": None,
                    "data": None,
                }

            latency_ms = int((time.monotonic() - t0) * 1000)
            req_id = resp.headers.get("x-request-id") or resp.headers.get("request-id")
            if 200 <= resp.status_code < 300:
                data = resp.json()
                usage = data.get("usage") if isinstance(data, dict) else None
                self.ledger.settle(
                    attempt,
                    status="confirmed",
                    http_status=resp.status_code,
                    latency_ms=latency_ms,
                    usage=usage if isinstance(usage, Mapping) else None,
                )
                return {
                    "ok": True,
                    "attempt_id": attempt.attempt_id,
                    "http_status": resp.status_code,
                    "error_code": None,
                    "message": "OK",
                    "latency_ms": latency_ms,
                    "serialized_body_bytes": payload_len,
                    "usage": usage,
                    "response_model": data.get("model") if isinstance(data, dict) else None,
                    "system_fingerprint": (
                        data.get("system_fingerprint") if isinstance(data, dict) else None
                    ),
                    "request_id": req_id,
                    "data": data,
                }

            error_code, retryable = classify_http_error(resp.status_code, resp.text)
            self.ledger.settle(
                attempt,
                status="failed",
                http_status=resp.status_code,
                error_code=error_code,
                latency_ms=latency_ms,
            )
            last_result = {
                "ok": False,
                "attempt_id": attempt.attempt_id,
                "http_status": resp.status_code,
                "error_code": error_code,
                "message": resp.text[:400],
                "latency_ms": latency_ms,
                "serialized_body_bytes": payload_len,
                "usage": None,
                "response_model": None,
                "request_id": req_id,
                "data": None,
            }
            if not retryable or try_idx + 1 >= max_tries:
                return last_result
        return last_result

    def request_provider_http(
        self,
        *,
        method: Literal["GET", "POST", "DELETE"],
        path: str,
        operation: str,
        files: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        estimated_payload_bytes: int = 0,
    ) -> dict[str, Any]:
        """Non-inference Provider HTTP call (GET /models, Files API) counted separately."""
        max_tries = 1 + self.ledger.budget.max_retries_per_request
        last_result: dict[str, Any] = {}
        for try_idx in range(max_tries):
            attempt, timeout_s = self.ledger.reserve_provider_http_attempt(
                operation=operation if try_idx == 0 else f"{operation}.retry_{try_idx}",
                payload_bytes=estimated_payload_bytes,
            )
            t0 = time.monotonic()
            try:
                resp = self._client.request(
                    method,
                    path,
                    files=dict(files) if files is not None else None,
                    data=dict(data) if data is not None else None,
                    timeout=timeout_s,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                latency_ms = int((time.monotonic() - t0) * 1000)
                self.ledger.settle(
                    attempt,
                    status="unknown",
                    error_code=type(exc).__name__,
                    latency_ms=latency_ms,
                )
                return {
                    "ok": False,
                    "attempt_id": attempt.attempt_id,
                    "http_status": None,
                    "error_code": f"NETWORK_OR_TIMEOUT_UNKNOWN:{type(exc).__name__}",
                    "message": str(exc),
                    "latency_ms": latency_ms,
                    "data": None,
                }

            latency_ms = int((time.monotonic() - t0) * 1000)
            if 200 <= resp.status_code < 300:
                parsed = resp.json()
                self.ledger.settle(
                    attempt,
                    status="confirmed",
                    http_status=resp.status_code,
                    latency_ms=latency_ms,
                )
                return {
                    "ok": True,
                    "attempt_id": attempt.attempt_id,
                    "http_status": resp.status_code,
                    "error_code": None,
                    "message": "OK",
                    "latency_ms": latency_ms,
                    "data": parsed,
                }

            error_code, retryable = classify_http_error(resp.status_code, resp.text)
            self.ledger.settle(
                attempt,
                status="failed",
                http_status=resp.status_code,
                error_code=error_code,
                latency_ms=latency_ms,
            )
            last_result = {
                "ok": False,
                "attempt_id": attempt.attempt_id,
                "http_status": resp.status_code,
                "error_code": error_code,
                "message": resp.text[:400],
                "latency_ms": latency_ms,
                "data": None,
            }
            if not retryable or try_idx + 1 >= max_tries:
                return last_result
        return last_result


def build_initial_capability_entries(
    config: ProviderContractConfig,
    *,
    sdk_tool_img_check: Mapping[str, Any],
    offline_boundaries: Mapping[str, Any],
) -> dict[str, CapabilityEntry]:
    """Initialize M0.1 capability rows from official docs.

    STRICT RULE: Documented capabilities start as 'documented_supported' (NEVER 'runtime_verified').
    Explicitly unsupported items are marked 'unsupported'. Untested project-specific quality items
    start as 'not_tested'.
    """
    src = config.official_sources
    entries: list[CapabilityEntry] = [
        CapabilityEntry(
            capability_id="models_discovery",
            description=(
                "GET /models lists account-available models and machine-readable modalities"
            ),
            status="documented_supported",
            documented_status="documented_supported",
            doc_source=src["list_models"],
            notes=(
                f"Configured model_id={config.model_id!r} "
                f"(documented version {config.documented_model_version}); "
                "alias is not an immutable weight lock."
            ),
        ),
        CapabilityEntry(
            capability_id="text_chat_completion",
            description="Basic text completion via OpenAI-compatible /chat/completions",
            status="documented_supported",
            documented_status="documented_supported",
            doc_source=src["pricing"],
        ),
        CapabilityEntry(
            capability_id="vision_multi_image_order",
            description="Ordered multi-image base64 data URLs in user content array",
            status="documented_supported",
            documented_status="documented_supported",
            doc_source=src["vision"],
            notes="Supported formats: JPEG, PNG, GIF, WebP (detected from file content).",
        ),
        CapabilityEntry(
            capability_id="vision_detail_levels",
            description="image_url.detail parameter (low/high/original/auto)",
            status="documented_supported",
            documented_status="documented_supported",
            doc_source=src["vision"],
            notes=(
                "low downscales to 512x512; high/original/auto avoid 512px pre-scale but images "
                "are still internally resized to ~1300x1300 (<=1024 tokens/image). "
                "high does NOT guarantee small text legibility."
            ),
        ),
        CapabilityEntry(
            capability_id="structured_candidate_json",
            description="JSON Output via response_format={'type':'json_object'}",
            status="documented_supported",
            documented_status="documented_supported",
            doc_source=src["json_mode"],
            notes=(
                "Requires word 'json' in prompt; non-JSON model output may be replaced with "
                "U+0020 spaces even when finish_reason='stop' (must detect whitespace-only)."
            ),
        ),
        CapabilityEntry(
            capability_id="tool_calls_text_observation",
            description="Function tool calling with tool_call_id and text JSON Observation return",
            status="documented_supported",
            documented_status="documented_supported",
            doc_source=src["tool_calls"],
            notes="Default P0 architecture returns structured text Observation + evidence_id.",
        ),
        CapabilityEntry(
            capability_id="tool_calls_image_roundtrip",
            description="Returning image_url content blocks inside role='tool' message",
            status="documented_supported",
            documented_status="documented_supported",
            doc_source=src["vision"],
            notes=(
                "Endpoint docs allow images in tool messages, while installed OpenAI SDK "
                f"({sdk_tool_img_check['sdk_version']}) static typing restricts "
                "ChatCompletionToolMessageParam.content to text parts. "
                "Offline wire serialization verified via MockTransport; live endpoint untested. "
                "Not P0 default."
            ),
            details=dict(sdk_tool_img_check),
        ),
        CapabilityEntry(
            capability_id="usage_and_error_reporting",
            description="Token usage accounting and HTTP 400/401/402/408/413/422/429/5xx errors",
            status="documented_supported",
            documented_status="documented_supported",
            doc_source=src["error_codes"],
        ),
        CapabilityEntry(
            capability_id="software_tutorial_high_vs_grid_roi",
            description="Small-text quality comparison: original high vs 960px + 3x3 numbered ROI",
            status="not_tested",
            documented_status="not_tested",
            doc_source=src["vision"],
            notes=(
                "Offline payload serialization & ungridded ROI crop geometry checked on synthetic "
                "1080p UI frame; real authorized software_tutorial live quality remains not_tested."
            ),
        ),
        CapabilityEntry(
            capability_id="image_count_14_15_16_and_4096px_boundary",
            description="14/15/16 image count x 4096/4097px side limit & 48 MiB body size check",
            status="documented_supported",
            documented_status="documented_supported",
            doc_source=src["vision"],
            notes=(
                "Offline contract verifier passed on synthetic 4096/4097px images; ordinary 1080p "
                "cannot prove the 4096px limit. Live boundary probe not yet executed."
            ),
            details={"offline_contract_summary": offline_boundaries},
        ),
        CapabilityEntry(
            capability_id="files_api_upload_reuse_expiry_delete",
            description="Files API upload (short TTL), file_id reuse, retrieve, and delete verify",
            status="documented_supported",
            documented_status="documented_supported",
            doc_source=src["files_api"],
            notes=(
                "Explicitly NOT used in P0 production path. Requires separate upload authorization "
                "and explicit TTL (3600..2592000s). file_id reuse avoids re-upload only; "
                "inference image tokens are still billed."
            ),
            details={
                "p0_production_allowed": False,
                "file_id_reuse_makes_inference_free": False,
            },
        ),
        CapabilityEntry(
            capability_id="assistant_message_images",
            description="Sending image blocks inside role='assistant' messages",
            status="unsupported",
            documented_status="unsupported",
            doc_source=src["vision"],
            notes="Official Vision docs explicitly state images in assistant messages return 400.",
        ),
        CapabilityEntry(
            capability_id="text_only_model_vision",
            description=f"Vision input on {config.text_only_reference_model_id}",
            status="unsupported",
            documented_status="unsupported",
            doc_source=src["pricing"],
            notes=(
                f"Official pricing/models docs state {config.text_only_reference_model_id} "
                "does not support Vision."
            ),
        ),
        CapabilityEntry(
            capability_id="chat_completions_mid_conversation_tool_insertion",
            description="Inserting synthetic tool calls mid-conversation in Chat Completions",
            status="unsupported",
            documented_status="unsupported",
            doc_source=src["tool_calls"],
            notes=(
                "Official Tool Calls docs state Chat Completions does not support inserting "
                "tool calls mid-conversation (Anthropic/Responses API only)."
            ),
        ),
    ]
    return {e.capability_id: e for e in entries}


def run_files_api_lifecycle_probe(
    *,
    client: ProviderProbeClient,
    gate_config: ProbeGateConfig,
    model_id: str,
    sample_image: EncodedProbeImage,
    entry: CapabilityEntry,
    run_id: str,
) -> dict[str, Any]:
    """Run controlled Files API upload -> retrieve -> reuse -> delete under separate auth & budget.

    Files HTTP calls & retries are counted in provider_http_attempts (NOT model_attempts);
    the reuse chat completion is counted in model_attempts; both share transferred_bytes & deadline.
    """
    ttl_seconds = validate_files_upload_authorization(gate_config.authorization)
    multipart_overhead = 512
    upload_res = client.request_provider_http(
        method="POST",
        path="/files",
        operation="files.upload",
        data={
            "purpose": "user_data",
            "expires_after[anchor]": "created_at",
            "expires_after[seconds]": str(ttl_seconds),
        },
        files={
            "file": (f"{sample_image.label}.png", sample_image.raw_bytes, sample_image.mime_type)
        },
        estimated_payload_bytes=len(sample_image.raw_bytes) + multipart_overhead,
    )
    if not upload_res["ok"]:
        entry.record_runtime_failure(
            error_code=str(upload_res["error_code"]),
            http_status=upload_res["http_status"],
            message=str(upload_res["message"]),
            attempt_id=str(upload_res["attempt_id"]),
        )
        return {"ok": False, "stage": "upload", "upload": upload_res}

    uploaded_data = upload_res["data"] or {}
    file_id = str(uploaded_data.get("id", ""))
    expires_at = uploaded_data.get("expires_at")
    if not file_id:
        entry.record_runtime_failure(
            error_code="FILES_MISSING_ID",
            http_status=upload_res["http_status"],
            message="Upload succeeded without file id",
            attempt_id=str(upload_res["attempt_id"]),
        )
        return {"ok": False, "stage": "upload", "upload": upload_res}

    retrieve_res = client.request_provider_http(
        method="GET",
        path=f"/files/{file_id}",
        operation="files.retrieve",
        estimated_payload_bytes=0,
    )

    reuse_payload = build_chat_payload(
        model_id=model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Briefly state what marker text is visible."},
                    {"type": "file", "file_id": file_id},
                ],
            }
        ],
        max_tokens=64,
    )
    reuse_res = client.request_model_json(
        operation="files.reuse_chat",
        payload=reuse_payload,
        image_parts=1,
    )

    # Always attempt cleanup deletion even if retrieve/reuse failed.
    delete_res = client.request_provider_http(
        method="DELETE",
        path=f"/files/{file_id}",
        operation="files.delete",
        estimated_payload_bytes=0,
    )
    deleted_flag = bool((delete_res.get("data") or {}).get("deleted") is True)

    all_ok = bool(
        upload_res["ok"]
        and retrieve_res["ok"]
        and reuse_res["ok"]
        and delete_res["ok"]
        and deleted_flag
    )
    summary = {
        "ok": all_ok,
        "p0_production_allowed": False,
        "file_id_reuse_makes_inference_free": False,
        "file_id_prefix": file_id[:12] + "..." if len(file_id) > 12 else file_id,
        "ttl_seconds_requested": ttl_seconds,
        "expires_at_returned": expires_at,
        "upload_ok": upload_res["ok"],
        "retrieve_ok": retrieve_res["ok"],
        "reuse_ok": reuse_res["ok"],
        "reuse_usage": reuse_res.get("usage"),
        "delete_ok": delete_res["ok"] and deleted_flag,
    }
    if all_ok:
        entry.record_runtime_success(evidence_run_id=run_id, details=summary)
    else:
        failing = next(
            (r for r in (retrieve_res, reuse_res, delete_res) if not r["ok"]),
            delete_res,
        )
        entry.record_runtime_failure(
            error_code=str(failing.get("error_code") or "FILES_LIFECYCLE_INCOMPLETE"),
            http_status=failing.get("http_status"),
            message=str(failing.get("message") or "Files lifecycle step failed"),
            attempt_id=failing.get("attempt_id"),
            details=summary,
        )
    return summary


def execute_live_probe_suite(
    *,
    config: ProviderContractConfig,
    gate_config: ProbeGateConfig,
    client: ProviderProbeClient,
    entries: dict[str, CapabilityEntry],
    run_id: str,
    tutorial_frame: Image.Image | None = None,
    enable_files_probe: bool = False,
) -> dict[str, Any]:
    """Execute budgeted live probes (also testable offline with httpx.MockTransport)."""
    results: dict[str, Any] = {}

    # 1. Models discovery (provider_http channel)
    models_res = client.request_provider_http(
        method="GET",
        path="/models",
        operation="models.list",
    )
    if models_res["ok"] and isinstance(models_res["data"], Mapping):
        model_list = models_res["data"].get("data", [])
        found = next(
            (m for m in model_list if isinstance(m, Mapping) and m.get("id") == config.model_id),
            None,
        )
        if found is not None:
            entries["models_discovery"].record_runtime_success(
                evidence_run_id=run_id,
                details={
                    "configured_model_id": config.model_id,
                    "account_model_metadata": {
                        "id": found.get("id"),
                        "name": found.get("name"),
                        "context_window": found.get("context_window"),
                        "max_output_tokens": found.get("max_output_tokens"),
                        "input_modalities": found.get("input_modalities"),
                        "output_modalities": found.get("output_modalities"),
                    },
                },
            )
        else:
            entries["models_discovery"].record_runtime_failure(
                error_code="MODEL_NOT_IN_ACCOUNT",
                http_status=models_res["http_status"],
                message=f"Configured model {config.model_id!r} not found in GET /models",
                attempt_id=models_res["attempt_id"],
            )
    else:
        entries["models_discovery"].record_runtime_failure(
            error_code=str(models_res["error_code"]),
            http_status=models_res["http_status"],
            message=str(models_res["message"]),
            attempt_id=models_res["attempt_id"],
        )
    results["models_discovery"] = models_res

    # 2. Text completion
    text_payload = build_chat_payload(
        model_id=config.model_id,
        messages=[{"role": "user", "content": "Reply with the exact token PING_OK."}],
        max_tokens=32,
    )
    text_res = client.request_model_json(operation="probe.text_basic", payload=text_payload)
    if text_res["ok"]:
        entries["text_chat_completion"].record_runtime_success(
            evidence_run_id=run_id,
            details={
                "response_model": text_res["response_model"],
                "latency_ms": text_res["latency_ms"],
                "usage": text_res["usage"],
                "serialized_body_bytes": text_res["serialized_body_bytes"],
            },
        )
        if isinstance(text_res.get("usage"), Mapping) and "total_tokens" in text_res["usage"]:
            entries["usage_and_error_reporting"].record_runtime_success(
                evidence_run_id=run_id,
                details={"observed_usage_keys": sorted(text_res["usage"].keys())},
            )
    else:
        entries["text_chat_completion"].record_runtime_failure(
            error_code=str(text_res["error_code"]),
            http_status=text_res["http_status"],
            message=str(text_res["message"]),
            attempt_id=text_res["attempt_id"],
        )
    results["text_basic"] = {k: v for k, v in text_res.items() if k != "data"}

    # 3. Multi-image order
    ordered_imgs = make_ordered_marker_images(3)
    order_payload = build_chat_payload(
        model_id=config.model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "List the exact marker text in each image in order, "
                            "separated by commas."
                        ),
                    },
                    *[im.to_image_url_part() for im in ordered_imgs],
                ],
            }
        ],
        max_tokens=64,
    )
    order_res = client.request_model_json(
        operation="probe.multi_image_order",
        payload=order_payload,
        image_parts=len(ordered_imgs),
    )
    if order_res["ok"]:
        content_str = str(
            ((order_res["data"] or {}).get("choices") or [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        i1 = content_str.find("FRAME_SEQ_01")
        i2 = content_str.find("FRAME_SEQ_02")
        i3 = content_str.find("FRAME_SEQ_03")
        ordered_ok = i1 != -1 and i2 != -1 and i3 != -1 and (i1 < i2 < i3)
        if ordered_ok:
            entries["vision_multi_image_order"].record_runtime_success(
                evidence_run_id=run_id,
                details={
                    "latency_ms": order_res["latency_ms"],
                    "usage": order_res["usage"],
                    "serialized_body_bytes": order_res["serialized_body_bytes"],
                },
            )
        else:
            entries["vision_multi_image_order"].record_runtime_failure(
                error_code="MULTI_IMAGE_ORDER_MISMATCH",
                http_status=order_res["http_status"],
                message=f"Expected ordered FRAME_SEQ_01..03, got: {content_str[:120]!r}",
                attempt_id=order_res["attempt_id"],
            )
    else:
        entries["vision_multi_image_order"].record_runtime_failure(
            error_code=str(order_res["error_code"]),
            http_status=order_res["http_status"],
            message=str(order_res["message"]),
            attempt_id=order_res["attempt_id"],
        )
    results["multi_image_order"] = {k: v for k, v in order_res.items() if k != "data"}

    # 4. Detail levels (low vs high)
    detail_img_low = make_synthetic_boundary_image(640, 360, label="detail_probe", detail="low")
    detail_img_high = make_synthetic_boundary_image(640, 360, label="detail_probe", detail="high")
    detail_outcomes: dict[str, Any] = {}
    all_details_ok = True
    for detail_name, dimg in (("low", detail_img_low), ("high", detail_img_high)):
        dpayload = build_chat_payload(
            model_id=config.model_id,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Read the label text in this image."},
                        dimg.to_image_url_part(),
                    ],
                }
            ],
            max_tokens=48,
        )
        dres = client.request_model_json(
            operation=f"probe.detail_{detail_name}",
            payload=dpayload,
            image_parts=1,
        )
        detail_outcomes[detail_name] = {k: v for k, v in dres.items() if k != "data"}
        if not dres["ok"]:
            all_details_ok = False
            entries["vision_detail_levels"].record_runtime_failure(
                error_code=str(dres["error_code"]),
                http_status=dres["http_status"],
                message=f"detail={detail_name} failed: {dres['message']}",
                attempt_id=dres["attempt_id"],
                details={"detail_outcomes": detail_outcomes},
            )
            break
    if all_details_ok:
        entries["vision_detail_levels"].record_runtime_success(
            evidence_run_id=run_id,
            details={"detail_outcomes": detail_outcomes},
        )
    results["detail_levels"] = detail_outcomes

    # 5. Structured candidate JSON
    json_payload = build_chat_payload(
        model_id=config.model_id,
        messages=[
            {
                "role": "system",
                "content": (
                    "Output strictly valid json matching "
                    '{"kind":"observed","text":"...","confidence":"high"}.'
                ),
            },
            {
                "role": "user",
                "content": "Return a sample observation json object for text 'diameter 20'.",
            },
        ],
        response_format={"type": "json_object"},
        max_tokens=128,
    )
    json_res = client.request_model_json(
        operation="probe.structured_json",
        payload=json_payload,
    )
    if json_res["ok"]:
        raw_content = (
            ((json_res["data"] or {}).get("choices") or [{}])[0].get("message", {}).get("content")
        )
        parsed_ok, parsed_obj, parse_reason = parse_structured_json_response(raw_content)
        if parsed_ok and parsed_obj is not None and "kind" in parsed_obj and "text" in parsed_obj:
            entries["structured_candidate_json"].record_runtime_success(
                evidence_run_id=run_id,
                details={
                    "parsed_keys": sorted(parsed_obj.keys()),
                    "latency_ms": json_res["latency_ms"],
                    "usage": json_res["usage"],
                },
            )
        else:
            entries["structured_candidate_json"].record_runtime_failure(
                error_code=parse_reason,
                http_status=json_res["http_status"],
                message=f"Structured JSON validation failed: {parse_reason}",
                attempt_id=json_res["attempt_id"],
            )
    else:
        entries["structured_candidate_json"].record_runtime_failure(
            error_code=str(json_res["error_code"]),
            http_status=json_res["http_status"],
            message=str(json_res["message"]),
            attempt_id=json_res["attempt_id"],
        )
    results["structured_candidate_json"] = {k: v for k, v in json_res.items() if k != "data"}

    # 6. Tool calling with default text JSON Observation return
    tools = [
        {
            "type": "function",
            "function": {
                "name": "inspect_clip",
                "description": (
                    "Inspect video frames in [start_ms, end_ms) and return text observation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "start_ms": {"type": "integer"},
                        "end_ms": {"type": "integer"},
                        "region_id": {"type": "string", "enum": list(VALID_REGION_IDS)},
                    },
                    "required": ["start_ms", "end_ms", "region_id"],
                },
            },
        }
    ]
    tool_turn1_payload = build_chat_payload(
        model_id=config.model_id,
        messages=[
            {
                "role": "user",
                "content": "Call inspect_clip for start_ms=0, end_ms=2000, region_id='full'.",
            }
        ],
        tools=tools,
        max_tokens=128,
    )
    t1_res = client.request_model_json(
        operation="probe.tool_call_turn1",
        payload=tool_turn1_payload,
    )
    if t1_res["ok"]:
        msg1 = (((t1_res["data"] or {}).get("choices") or [{}])[0]).get("message", {})
        tool_calls = msg1.get("tool_calls") or []
        if tool_calls and isinstance(tool_calls, list):
            tc0 = tool_calls[0]
            tc_id = str(tc0.get("id", ""))
            tool_obs = json.dumps(
                {
                    "evidence_id": "ev_frame_001",
                    "kind": "observed",
                    "text": "Diameter field displays 20 mm",
                },
                ensure_ascii=False,
            )
            tool_turn2_payload = build_chat_payload(
                model_id=config.model_id,
                messages=[
                    tool_turn1_payload["messages"][0],
                    msg1,
                    {"role": "tool", "tool_call_id": tc_id, "content": tool_obs},
                ],
                tools=tools,
                max_tokens=128,
            )
            t2_res = client.request_model_json(
                operation="probe.tool_call_turn2_text_obs",
                payload=tool_turn2_payload,
            )
            if t2_res["ok"]:
                entries["tool_calls_text_observation"].record_runtime_success(
                    evidence_run_id=run_id,
                    details={
                        "tool_call_id_matched": bool(tc_id),
                        "return_mode": "text_observation_json",
                        "turn1_latency_ms": t1_res["latency_ms"],
                        "turn2_latency_ms": t2_res["latency_ms"],
                    },
                )
            else:
                entries["tool_calls_text_observation"].record_runtime_failure(
                    error_code=str(t2_res["error_code"]),
                    http_status=t2_res["http_status"],
                    message=str(t2_res["message"]),
                    attempt_id=t2_res["attempt_id"],
                )
            results["tool_calls_text_observation"] = {
                "turn1": {k: v for k, v in t1_res.items() if k != "data"},
                "turn2": {k: v for k, v in t2_res.items() if k != "data"},
            }
        else:
            entries["tool_calls_text_observation"].record_runtime_failure(
                error_code="TOOL_CALL_NOT_EMITTED",
                http_status=t1_res["http_status"],
                message="Model did not emit tool_calls in turn 1",
                attempt_id=t1_res["attempt_id"],
            )
            results["tool_calls_text_observation"] = {
                "turn1": {k: v for k, v in t1_res.items() if k != "data"}
            }
    else:
        entries["tool_calls_text_observation"].record_runtime_failure(
            error_code=str(t1_res["error_code"]),
            http_status=t1_res["http_status"],
            message=str(t1_res["message"]),
            attempt_id=t1_res["attempt_id"],
        )
        results["tool_calls_text_observation"] = {
            "turn1": {k: v for k, v in t1_res.items() if k != "data"}
        }

    # 7. Software tutorial comparison (only if an authorized real frame is explicitly supplied)
    if tutorial_frame is not None:
        comp = prepare_software_tutorial_comparison(
            config=config,
            frame_image=tutorial_frame,
            source_kind="authorized_real_frame",
        )
        res_a = client.request_model_json(
            operation="probe.tutorial_strategy_a_high",
            payload=comp["strategy_a_original_high"]["payload"],
            image_parts=1,
        )
        res_b = client.request_model_json(
            operation="probe.tutorial_strategy_b_960_roi",
            payload=comp["strategy_b_960_plus_grid_roi"]["payload"],
            image_parts=3,
        )
        comp["strategy_a_original_high"].pop("payload", None)
        comp["strategy_b_960_plus_grid_roi"].pop("payload", None)
        comp["strategy_a_original_high"]["latency_ms"] = res_a["latency_ms"]
        comp["strategy_a_original_high"]["usage"] = res_a["usage"]
        comp["strategy_b_960_plus_grid_roi"]["latency_ms"] = res_b["latency_ms"]
        comp["strategy_b_960_plus_grid_roi"]["usage"] = res_b["usage"]
        if res_a["ok"] and res_b["ok"]:
            entries["software_tutorial_high_vs_grid_roi"].record_runtime_success(
                evidence_run_id=run_id,
                details=comp,
            )
        else:
            fail_res = res_a if not res_a["ok"] else res_b
            entries["software_tutorial_high_vs_grid_roi"].record_runtime_failure(
                error_code=str(fail_res["error_code"]),
                http_status=fail_res["http_status"],
                message=str(fail_res["message"]),
                attempt_id=fail_res["attempt_id"],
                details=comp,
            )
        results["software_tutorial_comparison"] = comp

    # 8. Optional Files API lifecycle probe (requires separate explicit authorization + short TTL)
    if enable_files_probe:
        tiny_sample = make_synthetic_boundary_image(
            128, 128, label="files_probe_sample", detail="low"
        )
        results["files_api_lifecycle"] = run_files_api_lifecycle_probe(
            client=client,
            gate_config=gate_config,
            model_id=config.model_id,
            sample_image=tiny_sample,
            entry=entries["files_api_upload_reuse_expiry_delete"],
            run_id=run_id,
        )

    return results


def build_capability_matrix_artifact(
    *,
    config: ProviderContractConfig,
    entries: Mapping[str, CapabilityEntry],
    mode: Literal["offline", "live"],
    run_id: str,
    ledger_summary: Mapping[str, Any] | None,
    software_tutorial_offline_comparison: Mapping[str, Any],
    decision: Literal["continue_with_conditions", "pause_pending_live_verification", "abort"],
    decision_rationale: str,
) -> dict[str, Any]:
    """Assemble and validate the M0.1 capability matrix JSON artifact."""
    matrix: dict[str, Any] = {
        "schema_version": "m0.1-capability-matrix-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "execution_mode": mode,
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "openai_sdk_version": openai.__version__,
            "httpx_version": httpx.__version__,
        },
        "provider": {
            "name": config.provider,
            "api_family": config.api_family,
            "base_url": config.base_url,
            "configured_model_id": config.model_id,
            "documented_model_version": config.documented_model_version,
            "official_contract_snapshot_date": config.snapshot_date,
            "official_contract_sources": config.official_sources,
            "model_alias_is_immutable_weight_lock": False,
        },
        "limits_summary": {
            "provider_documented": {
                "max_request_body_bytes": config.limits.max_request_body_bytes,
                "max_single_inline_image_bytes": config.limits.max_single_inline_image_bytes,
                "max_single_file_id_image_bytes": config.limits.max_single_file_id_image_bytes,
                "max_images_per_request": config.limits.max_images_per_request,
                "max_image_side_px_under_15_images": (
                    config.limits.max_image_side_px_under_15_images
                ),
                "image_count_dimension_threshold": config.limits.image_count_dimension_threshold,
                "max_image_side_px_at_or_above_15_images": (
                    config.limits.max_image_side_px_at_or_above_15_images
                ),
                "internal_resize_approx_pixels_side": (
                    config.limits.internal_resize_approx_pixels_side
                ),
                "internal_max_tokens_per_image": config.limits.internal_max_tokens_per_image,
            },
            "project_p0": config.project_p0_limits,
        },
        "capabilities": [entries[k].to_dict() for k in entries],
        "software_tutorial_comparison_baseline": software_tutorial_offline_comparison,
        "budget_ledger": ledger_summary,
        "decision": {
            "verdict": decision,
            "rationale": decision_rationale,
        },
    }
    validate_capability_matrix(matrix)
    return matrix


def _strip_payload_from_comparison(comp: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: ({sk: sv for sk, sv in v.items() if sk != "payload"} if isinstance(v, Mapping) else v)
        for k, v in comp.items()
    }


def evaluate_m0_a1_extracted_samples(
    *,
    config: ProviderContractConfig,
    manifest_path: Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Evaluate real extracted M0-A1 benchmark frames offline (SHA-256, ROI sizing, PTS order).

    Strips raw base64 payloads from the returned report and NEVER marks live capabilities
    as runtime_verified.
    """
    root = (repo_root or Path.cwd()).resolve()
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples = raw_manifest.get("m0_a1_extracted_samples", [])

    roi_comparisons: list[dict[str, Any]] = []
    multi_image_sequences: list[dict[str, Any]] = []
    missing_local_files: list[str] = []
    verified_frame_count = 0

    for sample in samples:
        sample_id = str(sample.get("sample_id", ""))
        benchmark = str(sample.get("benchmark", ""))
        purpose = str(sample.get("probe_purpose", ""))
        roi_region = str(sample.get("recommended_roi_region_id", "r2c2"))
        frames = list(sample.get("frames", []))

        resolved_paths: list[tuple[Mapping[str, Any], Path]] = []
        all_present = True
        for fr in frames:
            rel_path = str(fr.get("relative_path", ""))
            fpath = root / rel_path
            if not fpath.exists():
                missing_local_files.append(rel_path)
                all_present = False
                continue
            expected_sha = str(fr.get("sha256", ""))
            actual_sha = hashlib.sha256(fpath.read_bytes()).hexdigest()
            if expected_sha and actual_sha != expected_sha:
                raise ValueError(
                    f"M0-A1 frame SHA-256 mismatch for {rel_path}: "
                    f"expected {expected_sha}, got {actual_sha}"
                )
            verified_frame_count += 1
            resolved_paths.append((fr, fpath))

        if not all_present or not resolved_paths:
            continue

        if purpose in {"software_tutorial_high_vs_grid_roi", "chinese_small_text_ocr"}:
            # Pick representative middle frame (or only frame for static GUI screenshots).
            rep_meta, rep_path = resolved_paths[len(resolved_paths) // 2]
            with Image.open(rep_path) as im:
                rgb = im.convert("RGB")
            comp_raw = prepare_software_tutorial_comparison(
                config=config,
                frame_image=rgb,
                source_kind="authorized_real_frame",
                roi_region_id=roi_region,
            )
            roi_comparisons.append(
                {
                    "sample_id": sample_id,
                    "benchmark": benchmark,
                    "parent_artifact_id": sample.get("parent_artifact_id"),
                    "probe_purpose": purpose,
                    "frame_id": rep_meta.get("frame_id"),
                    "frame_relative_path": rep_meta.get("relative_path"),
                    "frame_sha256": rep_meta.get("sha256"),
                    "timestamp_kind": rep_meta.get("timestamp_kind"),
                    "actual_pts_ms": rep_meta.get("actual_pts_ms"),
                    "comparison": _strip_payload_from_comparison(comp_raw),
                }
            )
        elif purpose == "vision_multi_image_order":
            encoded_imgs: list[EncodedProbeImage] = []
            pts_sequence: list[int] = []
            for idx, (fr_meta, fpath) in enumerate(resolved_paths):
                with Image.open(fpath) as im:
                    rgb = im.convert("RGB")
                encoded_imgs.append(
                    make_encoded_image(
                        rgb,
                        label=f"{sample_id}_f{idx}",
                        fmt="JPEG",
                        detail="high",
                        region_id="full",
                    )
                )
                if isinstance(fr_meta.get("actual_pts_ms"), int):
                    pts_sequence.append(int(fr_meta["actual_pts_ms"]))
            boundary = evaluate_image_boundary_case(
                case_id=f"m0_a1_order_{sample_id}",
                model_id=config.model_id,
                images=encoded_imgs,
                limits=config.limits,
            )
            pts_strictly_increasing = len(pts_sequence) == len(resolved_paths) and all(
                pts_sequence[i] < pts_sequence[i + 1] for i in range(len(pts_sequence) - 1)
            )
            multi_image_sequences.append(
                {
                    "sample_id": sample_id,
                    "benchmark": benchmark,
                    "parent_artifact_id": sample.get("parent_artifact_id"),
                    "frame_count": len(encoded_imgs),
                    "actual_pts_ms_sequence": pts_sequence,
                    "pts_strictly_increasing": pts_strictly_increasing,
                    "serialized_request_body_bytes": boundary["actual_serialized_body_bytes"],
                    "serialized_request_body_sha256": boundary["serialized_request_body_sha256"],
                    "allowed_by_documented_contract": boundary["allowed_by_documented_contract"],
                    "within_p0_index_8_image_cap": len(encoded_imgs) <= 8,
                    "quality_status": "not_tested_live",
                }
            )

    return {
        "manifest_relative_path": manifest_path.as_posix(),
        "manifest_schema_version": raw_manifest.get("schema_version"),
        "dataset_total_bytes": raw_manifest.get("totals", {}).get("total_bytes"),
        "dataset_within_20gb_cap": raw_manifest.get("totals", {}).get("within_20gb_budget"),
        "verified_real_frame_count": verified_frame_count,
        "missing_local_frame_count": len(missing_local_files),
        "live_executed": False,
        "roi_comparisons": roi_comparisons,
        "multi_image_order_sequences": multi_image_sequences,
    }


def run_offline_m0_1(
    *,
    provider_config_path: Path = DEFAULT_PROVIDER_CONFIG_PATH,
    model_id_override: str | None = None,
    m0_a1_manifest_path: Path | None = None,
    capabilities_out: Path | None = DEFAULT_CAPABILITIES_OUT,
    report_out: Path | None = DEFAULT_OFFLINE_REPORT_OUT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run M0.1 offline contract, SDK serialization, boundary, and ROI comparison checks."""
    config = ProviderContractConfig.from_yaml(
        provider_config_path,
        model_id_override=model_id_override,
    )
    run_id = f"m0.1-offline-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"

    sdk_tool_img = inspect_openai_sdk_tool_image_support(config.model_id)
    offline_boundaries = run_offline_boundary_suite(config)
    synth_frame = build_synthetic_software_tutorial_frame(1920, 1080)
    tutorial_comp_raw = prepare_software_tutorial_comparison(
        config=config,
        frame_image=synth_frame,
        source_kind="synthetic_offline_fixture",
        roi_region_id="r2c2",
    )
    tutorial_comp_sanitized = _strip_payload_from_comparison(tutorial_comp_raw)

    real_m0_a1_eval: dict[str, Any] | None = None
    if m0_a1_manifest_path is not None and m0_a1_manifest_path.exists():
        real_m0_a1_eval = evaluate_m0_a1_extracted_samples(
            config=config,
            manifest_path=m0_a1_manifest_path,
        )
        if real_m0_a1_eval["roi_comparisons"]:
            # Promote primary baseline comparison to the first verified real VideoGUI/OCR frame
            # while retaining exact serialization metrics and quality_status='not_tested_live'.
            primary_real = real_m0_a1_eval["roi_comparisons"][0]
            tutorial_comp_sanitized = {
                **primary_real["comparison"],
                "sample_id": primary_real["sample_id"],
                "benchmark": primary_real["benchmark"],
                "frame_relative_path": primary_real["frame_relative_path"],
                "frame_sha256": primary_real["frame_sha256"],
                "all_extracted_m0_a1_summary": {
                    "roi_sample_count": len(real_m0_a1_eval["roi_comparisons"]),
                    "multi_image_sequence_count": len(
                        real_m0_a1_eval["multi_image_order_sequences"]
                    ),
                    "verified_real_frame_count": real_m0_a1_eval["verified_real_frame_count"],
                },
            }

    entries = build_initial_capability_entries(
        config,
        sdk_tool_img_check=sdk_tool_img,
        offline_boundaries=offline_boundaries,
    )

    ledger_summary = {
        "model_attempts": 0,
        "provider_http_attempts": 0,
        "transferred_bytes": 0,
        "sent_image_parts": 0,
        "cost_status": "confirmed",
        "note": "Offline verification only; zero outbound cloud calls were made.",
    }

    has_real_samples = bool(real_m0_a1_eval and real_m0_a1_eval["verified_real_frame_count"] > 0)
    rationale = (
        "Official DeepSeek contract (2026-11-25), offline 14/15/16 x 4096/4097px & 48 MiB "
        "boundary checks, OpenAI SDK tool-image typing/serialization checks, and "
        + (
            f"offline ROI & multi-image serialization checks on "
            f"{real_m0_a1_eval['verified_real_frame_count']} real extracted M0-A1 frames "
            "(VideoGUI, MME-VideoOCR, Video-MME v2; <=20 GB subset) "
            if has_real_samples and real_m0_a1_eval is not None
            else "synthetic 1080p high vs 960+grid ROI serialization comparison "
        )
        + "are complete. However, no live credentials or cloud egress consent were executed "
        "in this run; all live-dependent capabilities remain documented_supported or "
        "not_tested (zero runtime_verified). Supply explicit M0.0 live budget + credentials "
        "to execute live M0-A1 probes on the extracted frames before entering M1.5 cloud slice."
    )

    matrix = build_capability_matrix_artifact(
        config=config,
        entries=entries,
        mode="offline",
        run_id=run_id,
        ledger_summary=ledger_summary,
        software_tutorial_offline_comparison=tutorial_comp_sanitized,
        decision="pause_pending_live_verification",
        decision_rationale=rationale,
    )

    run_record = sanitize_record(
        {
            "run_id": run_id,
            "execution_mode": "offline",
            "generated_at_utc": matrix["generated_at_utc"],
            "configured_model_id": config.model_id,
            "sdk_tool_image_inspection": sdk_tool_img,
            "offline_boundary_suite": offline_boundaries,
            "software_tutorial_offline_comparison": tutorial_comp_sanitized,
            "m0_a1_real_benchmark_offline_evaluation": real_m0_a1_eval,
            "budget_ledger": ledger_summary,
            "decision": matrix["decision"],
        }
    )

    sanitized_matrix = sanitize_record(matrix)
    validate_capability_matrix(sanitized_matrix)

    if capabilities_out is not None:
        capabilities_out.parent.mkdir(parents=True, exist_ok=True)
        capabilities_out.write_text(
            json.dumps(sanitized_matrix, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if report_out is not None:
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(
            json.dumps(run_record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return sanitized_matrix, run_record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="M0.1 Provider capability, boundary & ROI probe (offline by default)."
    )
    parser.add_argument(
        "--provider-config",
        type=Path,
        default=DEFAULT_PROVIDER_CONFIG_PATH,
        help="Path to provider contract YAML (model ID comes from config).",
    )
    parser.add_argument(
        "--budget-config",
        type=Path,
        default=DEFAULT_BUDGET_CONFIG_PATH,
        help="Path to M0.0 authorization & budget YAML.",
    )
    parser.add_argument(
        "--model-id",
        type=str,
        default=None,
        help="Optional configured model ID override.",
    )
    parser.add_argument(
        "--m0-a1-manifest",
        type=Path,
        default=Path("experiments/manifests/benchmark_subset_manifest.json"),
        help="Optional curated M0-A1 benchmark subset manifest for real-frame offline checks.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Explicitly request live cloud execution (requires M0.0 consent, budget & API key).",
    )
    parser.add_argument(
        "--enable-files-probe",
        action="store_true",
        help=(
            "Also run small-sample Files API probe (requires separate Files upload consent + TTL)."
        ),
    )
    parser.add_argument(
        "--tutorial-frame",
        type=Path,
        default=None,
        help=(
            "Optional path to an authorized software_tutorial frame image for live A/B comparison."
        ),
    )
    parser.add_argument(
        "--capabilities-out",
        type=Path,
        default=DEFAULT_CAPABILITIES_OUT,
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=DEFAULT_OFFLINE_REPORT_OUT,
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.live:
        manifest_arg = args.m0_a1_manifest if args.m0_a1_manifest.exists() else None
        matrix, _ = run_offline_m0_1(
            provider_config_path=args.provider_config,
            model_id_override=args.model_id,
            m0_a1_manifest_path=manifest_arg,
            capabilities_out=args.capabilities_out,
            report_out=args.report_out,
        )
        print(
            f"M0.1 offline verification complete: run_id={matrix['run_id']} "
            f"verdict={matrix['decision']['verdict']}"
        )
        return 0

    # Live path: strictly enforce M0.0 gate before touching the network.
    config = ProviderContractConfig.from_yaml(
        args.provider_config,
        model_id_override=args.model_id,
    )
    gate_cfg = ProbeGateConfig.from_yaml(args.budget_config)
    api_key = validate_live_gate(
        gate_cfg,
        cli_live_flag=True,
        api_key_env=config.api_key_env,
        environ=os.environ,
        require_files_upload=bool(args.enable_files_probe),
    )

    run_id = f"m0.1-live-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    sdk_tool_img = inspect_openai_sdk_tool_image_support(config.model_id)
    offline_boundaries = run_offline_boundary_suite(config)
    entries = build_initial_capability_entries(
        config,
        sdk_tool_img_check=sdk_tool_img,
        offline_boundaries=offline_boundaries,
    )
    ledger = ProbeBudgetLedger(budget=gate_cfg.budget)

    real_frame: Image.Image | None = None
    if args.tutorial_frame is not None:
        with Image.open(args.tutorial_frame) as im:
            real_frame = im.convert("RGB")

    with ProviderProbeClient(config=config, ledger=ledger, api_key=api_key) as client:
        live_results = execute_live_probe_suite(
            config=config,
            gate_config=gate_cfg,
            client=client,
            entries=entries,
            run_id=run_id,
            tutorial_frame=real_frame,
            enable_files_probe=bool(args.enable_files_probe),
        )

    core_ids = (
        "models_discovery",
        "text_chat_completion",
        "vision_multi_image_order",
        "vision_detail_levels",
        "structured_candidate_json",
        "tool_calls_text_observation",
    )
    all_core_verified = all(entries[cid].status == "runtime_verified" for cid in core_ids)
    verdict: Literal["continue_with_conditions", "pause_pending_live_verification", "abort"] = (
        "continue_with_conditions" if all_core_verified else "pause_pending_live_verification"
    )
    matrix = build_capability_matrix_artifact(
        config=config,
        entries=entries,
        mode="live",
        run_id=run_id,
        ledger_summary=ledger.summary(),
        software_tutorial_offline_comparison=live_results.get("software_tutorial_comparison", {}),
        decision=verdict,
        decision_rationale=(
            "Live core probe verified under M0.0 budget."
            if all_core_verified
            else (
                "One or more core live probes failed or remained untested; "
                "inspect preserved errors."
            )
        ),
    )
    sanitized_matrix = sanitize_record(matrix, secrets=(api_key,))
    validate_capability_matrix(sanitized_matrix)
    args.capabilities_out.parent.mkdir(parents=True, exist_ok=True)
    args.capabilities_out.write_text(
        json.dumps(sanitized_matrix, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    sanitized_record = sanitize_record(
        {
            "run_id": run_id,
            "execution_mode": "live",
            "live_results": live_results,
            "budget_ledger": ledger.summary(),
            "decision": matrix["decision"],
        },
        secrets=(api_key,),
    )
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(
        json.dumps(sanitized_record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"M0.1 live probe complete: run_id={run_id} verdict={verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

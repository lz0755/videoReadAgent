"""Versioned prompt registry, strict Jinja2 rendering, and content hashing (M1.6).

Templates MUST be UTF-8, LF-only, without BOM. Template hashes are computed over exact
raw bytes without stripping. Bundle hashes combine sorted relative paths + template hashes,
render engine version, tool schema hash, and output schema hash. Runtime variables produce
a separate request_input_digest and never mutate template or bundle identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import jinja2

from video_agent.domain.errors import ErrorCode, PromptIntegrityError

_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True, slots=True)
class PromptTemplateEntry:
    """Verified metadata for a single registered prompt template."""

    name: str
    relative_path: str
    label: str
    template_sha256: str


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """Rendered prompt text together with separated template/bundle/input identities."""

    name: str
    label: str
    relative_path: str
    template_hash: str
    bundle_hash: str
    request_input_digest: str
    rendered_text: str


def canonical_json_sha256(payload: object) -> str:
    """Compute SHA-256 of canonical UTF-8 JSON (sort_keys, compact separators, allow_nan=False)."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_and_decode_template(path: Path, expected_sha256: str) -> str:
    if not path.is_file():
        raise PromptIntegrityError(
            ErrorCode.PROMPT_INTEGRITY_ERROR,
            f"registered prompt template missing: {path.name}",
        )
    raw = path.read_bytes()
    if raw.startswith(_UTF8_BOM):
        raise PromptIntegrityError(
            ErrorCode.PROMPT_INTEGRITY_ERROR,
            f"prompt template must not contain UTF-8 BOM: {path.name}",
        )
    if b"\r" in raw:
        raise PromptIntegrityError(
            ErrorCode.PROMPT_INTEGRITY_ERROR,
            f"prompt template must use LF line endings only: {path.name}",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromptIntegrityError(
            ErrorCode.PROMPT_INTEGRITY_ERROR,
            f"prompt template is not valid UTF-8: {path.name}",
        ) from exc

    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise PromptIntegrityError(
            ErrorCode.PROMPT_INTEGRITY_ERROR,
            f"prompt template SHA-256 mismatch for {path.name}",
        )
    return text


class PromptRegistry:
    """Strict loader, integrity verifier, and renderer for versioned prompt bundles."""

    def __init__(self, registry_path: Path) -> None:
        if not registry_path.is_file():
            raise PromptIntegrityError(
                ErrorCode.PROMPT_INTEGRITY_ERROR,
                "prompt registry file not found",
            )
        self._registry_path = registry_path
        self._root_dir = registry_path.parent
        self._env = jinja2.Environment(
            undefined=jinja2.StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        self._entries: dict[str, PromptTemplateEntry] = {}
        self._templates: dict[str, jinja2.Template] = {}

        try:
            raw_json = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PromptIntegrityError(
                ErrorCode.PROMPT_INTEGRITY_ERROR,
                f"failed to parse prompt registry JSON: {exc}",
            ) from exc

        templates_list = raw_json.get("templates") if isinstance(raw_json, dict) else None
        if not isinstance(templates_list, list) or not templates_list:
            raise PromptIntegrityError(
                ErrorCode.PROMPT_INTEGRITY_ERROR,
                "prompt registry must contain a non-empty templates list",
            )

        for item in templates_list:
            if not isinstance(item, dict):
                raise PromptIntegrityError(
                    ErrorCode.PROMPT_INTEGRITY_ERROR,
                    "prompt registry entry must be an object",
                )
            name = item.get("name")
            rel_path = item.get("relative_path")
            label = item.get("label")
            expected_sha = item.get("template_sha256")
            if not all(
                isinstance(v, str) and v.strip() for v in (name, rel_path, label, expected_sha)
            ):
                raise PromptIntegrityError(
                    ErrorCode.PROMPT_INTEGRITY_ERROR,
                    "prompt registry entry is missing required string fields",
                )
            assert isinstance(name, str)
            assert isinstance(rel_path, str)
            assert isinstance(label, str)
            assert isinstance(expected_sha, str)

            if name in self._entries:
                raise PromptIntegrityError(
                    ErrorCode.PROMPT_INTEGRITY_ERROR,
                    f"duplicate prompt template name: {name}",
                )

            resolved = (self._root_dir / rel_path).resolve()
            if self._root_dir.resolve() not in resolved.parents:
                raise PromptIntegrityError(
                    ErrorCode.PROMPT_INTEGRITY_ERROR,
                    f"prompt path escapes prompts directory: {rel_path}",
                )

            text = _validate_and_decode_template(resolved, expected_sha)
            try:
                compiled = self._env.from_string(text)
            except jinja2.TemplateError as exc:
                raise PromptIntegrityError(
                    ErrorCode.PROMPT_INTEGRITY_ERROR,
                    f"invalid Jinja2 syntax in {rel_path}: {exc}",
                ) from exc

            self._entries[name] = PromptTemplateEntry(
                name=name,
                relative_path=rel_path,
                label=label,
                template_sha256=expected_sha,
            )
            self._templates[name] = compiled

    @property
    def render_engine_version(self) -> str:
        return f"jinja2:{jinja2.__version__}"

    @property
    def entries(self) -> dict[str, PromptTemplateEntry]:
        return dict(self._entries)

    def compute_bundle_hash(
        self,
        *,
        tool_schema: Mapping[str, object],
        output_schema: Mapping[str, object],
    ) -> str:
        """Compute deterministic bundle SHA-256 over sorted templates, engine, and schema hashes."""
        sorted_templates = [
            {
                "relative_path": entry.relative_path,
                "template_hash": entry.template_sha256,
            }
            for entry in sorted(self._entries.values(), key=lambda item: item.relative_path)
        ]
        bundle_doc = {
            "templates": sorted_templates,
            "render_engine_version": self.render_engine_version,
            "tool_schema_hash": canonical_json_sha256(tool_schema),
            "output_schema_hash": canonical_json_sha256(output_schema),
        }
        return canonical_json_sha256(bundle_doc)

    def render(
        self,
        name: str,
        variables: Mapping[str, object],
        *,
        tool_schema: Mapping[str, object],
        output_schema: Mapping[str, object],
    ) -> RenderedPrompt:
        """Strictly render a registered template with separated template/bundle/input hashes."""
        if name not in self._entries:
            raise PromptIntegrityError(
                ErrorCode.PROMPT_INTEGRITY_ERROR,
                f"unknown prompt template: {name}",
            )
        entry = self._entries[name]
        template = self._templates[name]
        try:
            rendered_text = template.render(**dict(variables))
        except jinja2.TemplateError as exc:
            raise PromptIntegrityError(
                ErrorCode.PROMPT_INTEGRITY_ERROR,
                f"failed to render prompt '{name}': {exc}",
            ) from exc

        bundle_hash = self.compute_bundle_hash(
            tool_schema=tool_schema,
            output_schema=output_schema,
        )
        input_digest = canonical_json_sha256(dict(variables))
        return RenderedPrompt(
            name=entry.name,
            label=entry.label,
            relative_path=entry.relative_path,
            template_hash=entry.template_sha256,
            bundle_hash=bundle_hash,
            request_input_digest=input_digest,
            rendered_text=rendered_text,
        )

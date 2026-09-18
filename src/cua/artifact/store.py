"""Load/save/validate capability artifacts. File name is `<id>@<version>.json`."""

from __future__ import annotations

import json
from pathlib import Path

from cua.artifact.schema import Capability


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, cap: Capability) -> Path:
        return self.root / f"{cap.ref}.json"

    def save(self, cap: Capability) -> Path:
        p = self.path_for(cap)
        p.write_text(cap.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
        return p

    @staticmethod
    def load(path: Path) -> Capability:
        return Capability.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[Capability]:
        return [self.load(p) for p in sorted(self.root.glob("*@*.json"))]

    def latest(self, cap_id: str) -> Capability | None:
        caps = [c for c in self.list() if c.id == cap_id]
        if not caps:
            return None
        return max(caps, key=lambda c: tuple(int(x) for x in c.version.split(".")))

    @staticmethod
    def json_schema() -> str:
        return json.dumps(Capability.model_json_schema(), indent=2)

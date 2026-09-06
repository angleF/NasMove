from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ConnectionStageResult:
    name: str
    success: bool
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionTestReport:
    stages: tuple[ConnectionStageResult, ...]

    @classmethod
    def all_passed(cls) -> ConnectionTestReport:
        names = ("地址解析", "TCP", "SMB 协商", "认证", "共享访问")
        return cls(tuple(ConnectionStageResult(name, True) for name in names))

    @property
    def success(self) -> bool:
        return all(stage.success for stage in self.stages)


@dataclass(frozen=True, slots=True)
class ConnectionRequest:
    config: Any
    password: str


@dataclass(frozen=True, slots=True)
class RemoteBrowseReport:
    path: Any
    entries: tuple[Any, ...]
    free_space: int | None
    error: str | None = None

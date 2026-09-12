"""配置加载：default_config.yaml → ~/.supermouse/config.yaml → 内存中的 dict。

刻意不用 pydantic 强校验——黑客松期间配置结构会频繁改动，
用点号路径访问 + 默认值更省事：cfg.get("eyes.calibration.hard_threshold", 0.55)
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

HOME = Path("~/.supermouse").expanduser()
USER_CONFIG = HOME / "config.yaml"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "default_config.yaml"


class Config:
    def __init__(self, data: dict, path: Path | None = None):
        self._data = data
        self.path = path

    # ---------- 读 ----------

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    def section(self, dotted: str) -> dict:
        v = self.get(dotted, {})
        return v if isinstance(v, dict) else {}

    def path_of(self, dotted: str, default: str | None = None) -> Path | None:
        v = self.get(dotted, default)
        return Path(str(v)).expanduser() if v else None

    def __getitem__(self, dotted: str) -> Any:
        return self.get(dotted)

    @property
    def data(self) -> dict:
        return self._data

    # ---------- 写 ----------

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise TypeError(f"配置路径冲突：{dotted}")
        node[parts[-1]] = value

    def save(self) -> None:
        target = self.path or USER_CONFIG
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as f:
            yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)
        log.info("配置已保存到 %s", target)


def load_config(path: str | Path | None = None) -> Config:
    """加载配置。用户配置深合并到默认配置之上。"""
    with DEFAULT_CONFIG.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    target = Path(path).expanduser() if path else USER_CONFIG
    if target.exists():
        with target.open(encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        data = _deep_merge(data, user)
        log.info("已合并用户配置 %s", target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as f:
            yaml.safe_dump(copy.deepcopy(data), f, allow_unicode=True, sort_keys=False)
        log.info("已创建用户配置 %s", target)

    return Config(data, target)


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

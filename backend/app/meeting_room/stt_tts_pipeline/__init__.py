from typing import Callable, Dict, TypeVar

T = TypeVar("T")


def select_provider(chosen: str, builders: Dict[str, Callable[[], T]]) -> T:
    try:
        builder = builders[chosen]
    except KeyError as exc:
        raise ValueError(f"Unknown provider {chosen!r}; expected one of {sorted(builders)}") from exc
    return builder()

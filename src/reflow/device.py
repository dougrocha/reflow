"""The device a local model runs on."""

from __future__ import annotations

DEVICES = ("auto", "cuda", "mps", "cpu")       # docling_layout.make_converter accepts all four


def resolve_device(requested: str = "auto") -> str:
    """The concrete device ("cuda", "mps" or "cpu") a model runs on. The one place `auto` is
    resolved: cuda when torch sees it, then mps, else cpu. A named GPU that is not there raises
    ValueError before any work; only `auto` may land on cpu."""
    if requested == "cpu":
        return "cpu"
    try:
        import torch
    except ImportError:
        if requested != "auto":
            raise ValueError(f"{requested.upper()} requested but torch is not installed; "
                             "use --device auto or --device cpu.") from None
        return "cpu"
    mps = getattr(torch.backends, "mps", None)
    has = {"cuda": torch.cuda.is_available(), "mps": bool(mps and mps.is_available())}
    if requested in has:
        if not has[requested]:
            why = "torch.cuda.is_available() is False" if requested == "cuda" \
                else "torch.backends.mps.is_available() is False"
            raise ValueError(f"{requested.upper()} requested but not available ({why}); "
                             "use --device auto or --device cpu.")
        return requested
    return next((d for d in ("cuda", "mps") if has[d]), "cpu")

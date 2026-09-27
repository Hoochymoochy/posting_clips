"""Analytics package — live YouTube / Instagram metrics for published clips."""

__all__ = ["router"]


def __getattr__(name: str):
    if name == "router":
        from .routes import router

        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

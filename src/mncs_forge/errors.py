"""Domain errors with stable machine-readable codes."""


class ForgeError(RuntimeError):
    """A bounded error safe to expose through the CLI or MCP."""

    def __init__(self, code: str, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}

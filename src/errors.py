class AppError(Exception):
    """An operational error safe to display without browser/session details."""

    def __init__(self, code: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code

"""Operator welcome text sent over the radio, at most once per node per server run."""
import os

ENV_NAME = "EMPIRE_RADIO_WELCOME"
MAX_BYTES = 200
MAX_REMEMBERED = 20000


def clean(text):
    """Collapse whitespace and control characters so the text is one radio line."""
    return " ".join(str(text or "").split())


def byte_length(text):
    return len(clean(text).encode("utf-8"))


def fits(text):
    return byte_length(text) <= MAX_BYTES


def trim(text):
    """Cut to one radio packet without splitting a multi-byte character."""
    return clean(text).encode("utf-8")[:MAX_BYTES].decode("utf-8", "ignore").rstrip()


def launch_environment(enabled, text):
    """Environment for the server: the text when turned on, otherwise an explicit blank."""
    return {ENV_NAME: trim(text) if enabled else ""}


class WelcomeGate:
    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ
        self.greeted = set()

    def take(self, node):
        """Return the welcome text the first time this node asks, otherwise None."""
        text = trim(self.environ.get(ENV_NAME, ""))
        if not text or node in self.greeted:
            return None
        if len(self.greeted) >= MAX_REMEMBERED:
            self.greeted.clear()
        self.greeted.add(node)
        return text

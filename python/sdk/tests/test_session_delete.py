from deepseek_harness import HarnessClient, SessionDeletionResult
import pytest


def test_deletion_keeps_outcomes_and_exact_identity(monkeypatch):
    client = object.__new__(HarnessClient)
    for status in ("deleted", "not-found", "incomplete", "unknown", "refused"):
        value = SessionDeletionResult(sessionId="exact", status=status, reason="writer-held" if status == "refused" else None)
        monkeypatch.setattr(client, "request", lambda *args, **kwargs: value)
        assert client.delete_session("exact").status == status
    for value in (SessionDeletionResult(sessionId="neighbor", status="deleted"), SessionDeletionResult(sessionId="exact", status="refused")):
        monkeypatch.setattr(client, "request", lambda *args, **kwargs: value)
        with pytest.raises(TypeError, match="unknown session deletion outcome"):
            client.delete_session("exact")

"""Review signals must use evidence available at the cutoff, never later news."""
import copy

import pytest
from pydantic import ValidationError

from hellabandz.agents import reviews
from hellabandz.agents.reviews import ReviewRequest, analyze, example


def request():
    data = example()
    return {k: data[k] for k in ("as_of", "provider", "reviews")}


def test_review_deterioration_and_insufficient_evidence():
    data = request()
    result = analyze(ReviewRequest.model_validate(data))
    assert result["signal"] == "avoid_or_reduce"
    assert result["provider"] == "offline"
    assert result["model"] == "rating-baseline-v1"
    assert result["recent"]["count"] == result["baseline"]["count"] == 5
    assert result["sentiment_change"] == pytest.approx(-1.6)
    assert result == analyze(ReviewRequest.model_validate(data))
    data["reviews"] = data["reviews"][:-1]
    assert analyze(ReviewRequest.model_validate(data))["signal"] == "insufficient_data"


def test_reviews_exclude_future_observations_and_duplicate_evidence():
    data = request()
    expected = analyze(ReviewRequest.model_validate(data))
    late = {**data["reviews"][0], "review_id": "late-observation", "observed_at": "2024-06-21T00:00:00Z"}
    future = {**data["reviews"][0], "review_id": "future", "published_at": "2024-06-22T00:00:00Z", "observed_at": "2024-06-22T00:00:00Z"}
    duplicate = {**data["reviews"][0], "review_id": "cross-post", "text": "  COMFORTABLE running shoe; good cushioning. "}
    data["reviews"] += [late, future, duplicate, copy.deepcopy(data["reviews"][1])]
    actual = analyze(ReviewRequest.model_validate(data))
    assert actual["recent"] == expected["recent"] and actual["baseline"] == expected["baseline"]
    assert actual["signal"] == expected["signal"]
    assert [r["reason"] for r in actual["excluded"]].count("unavailable_at_cutoff") == 2
    assert len(actual["evidence"]) == 10
    data["reviews"].reverse()
    assert analyze(ReviewRequest.model_validate(data)) == actual


@pytest.mark.parametrize("change", [{"rating": True}, {"rating": 6}, {"source_url": "javascript:alert(1)"},
                                   {"observed_at": "2024-01-01"}, {"observed_at": "2024-01-01T00:00:00Z"}])
def test_review_input_validation(change):
    data = request()
    data["reviews"][0].update(change)
    with pytest.raises(ValidationError):
        ReviewRequest.model_validate(data)


def test_claude_requires_key_and_never_silently_uses_other_provider(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    data = request()
    data["provider"] = "claude"
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        analyze(ReviewRequest.model_validate(data))


@pytest.mark.parametrize("labels", [[], [{"review_id": "fabricated", "sentiment": 1, "themes": ["other"]}],
                                     [{"review_id": "synthetic-0", "sentiment": float("nan"), "themes": ["other"]}]])
def test_provider_cannot_invent_evidence_or_nonfinite_scores(monkeypatch, labels):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    from hellabandz.agents import llm
    monkeypatch.setattr(llm, "chat_json", lambda *a, **kw: ({"reviews": labels}, {"model": "test"}))
    data = request()
    data["provider"] = "openai"
    with pytest.raises(ValueError):
        analyze(ReviewRequest.model_validate(data))


def test_provider_only_sees_eligible_untrusted_text_and_is_labeled(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    from hellabandz.agents import llm
    import json
    def fake(system, user, **kw):
        assert "untrusted" in system
        rows = json.loads(user)
        assert len(rows) == 10
        assert all(set(r) == {"review_id", "text"} for r in rows)
        return {"reviews": [{"review_id": r["review_id"], "sentiment": -.5, "themes": ["quality"]} for r in rows]}, {"model": "mock-text-model"}
    monkeypatch.setattr(llm, "chat_json", fake)
    data = request()
    data["provider"] = "openai"
    result = analyze(ReviewRequest.model_validate(data))
    assert result["signal"] == "hold" and result["provider"] == "openai"
    assert result["model"] == "mock-text-model"
    assert all(r["themes"] == ["quality"] for r in result["evidence"])


def test_claude_messages_contract_and_explicit_selection(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder")
    monkeypatch.setenv("QS_ANTHROPIC_MODEL", "test-claude-model")
    def post(url, *, headers, json, timeout):
        assert url == "https://api.anthropic.com/v1/messages"
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["x-api-key"] == "test-placeholder" and timeout == 60
        assert json["model"] == "test-claude-model" and "untrusted" in json["system"]
        import json as codec
        rows = codec.loads(json["messages"][0]["content"])
        labels = {"reviews": [{"review_id": r["review_id"], "sentiment": .1, "themes": ["demand"]} for r in rows]}
        return reviews.httpx.Response(200, request=reviews.httpx.Request("POST", url),
                                     json={"model": "test-claude-model", "content": [{"type": "text", "text": codec.dumps(labels)}]})
    monkeypatch.setattr(reviews.httpx, "post", post)
    data = request()
    data["provider"] = "claude"
    result = analyze(ReviewRequest.model_validate(data))
    assert result["provider"] == "claude" and result["model"] == "test-claude-model"

import json

from gugu.research.hypotheses import record_source_unavailable, record_hypothesis


def test_source_unavailable_is_auditable(tmp_path, monkeypatch):
    monkeypatch.setattr("gugu.config.PROJECT_ROOT", tmp_path)
    record = record_source_unavailable("https://example.invalid/article", "微信环境验证")
    assert record["status"] == "source_unavailable"
    assert record["strategy_admission"] == "never_automatic"
    rows = [
        json.loads(x)
        for x in (tmp_path / "data" / "research_hypotheses.jsonl").read_text().splitlines()
    ]
    assert rows[0]["source"] == "https://example.invalid/article"


def test_hypothesis_status_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr("gugu.config.PROJECT_ROOT", tmp_path)
    try:
        record_hypothesis(source="x", status="verified")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown status must fail")

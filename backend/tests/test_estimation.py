from app.estimation import comparison_count, timing


def test_comparison_count_excludes_same_paragraph_or_document():
    # Four chunks in three paragraphs; first document contains first two paragraphs.
    assert comparison_count(4, [2, 1, 1]) == 5
    assert comparison_count(4, [3, 1]) == 3
    assert comparison_count(0, []) == 0
    assert comparison_count(4, [4]) == 0


def test_timing_labels_uncalibrated_and_historical_values(monkeypatch):
    monkeypatch.setenv("SCAN_ESTIMATE_COMPARISONS_PER_SECOND", "100")
    initial = timing(10000, 0, 768, [])
    historical = timing(10000, 0, 768, [200, 200, 100000])
    assert initial["basis"] == "initial" and initial["history_samples"] == 0
    assert initial["seconds_low"] == 25 and initial["seconds_high"] == 400
    assert historical["basis"] == "history" and historical["history_samples"] == 3
    assert historical["seconds_low"] == 30 and historical["seconds_high"] == 100

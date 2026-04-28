from alert_explainer.models import Alert


def test_priority_rank_ordering() -> None:
    crit = Alert(labels={"severity": "critical", "alertname": "X"})
    warn = Alert(labels={"severity": "warning", "alertname": "Y"})
    info = Alert(labels={"severity": "info", "alertname": "Z"})
    unknown = Alert(labels={"alertname": "W"})  # no severity label

    assert crit.priority_rank < warn.priority_rank < info.priority_rank <= unknown.priority_rank


def test_priority_rank_is_case_insensitive() -> None:
    a = Alert(labels={"severity": "CRITICAL"})
    b = Alert(labels={"severity": "critical"})
    assert a.priority_rank == b.priority_rank == 0


def test_alertname_default() -> None:
    assert Alert().alertname == "Unnamed"

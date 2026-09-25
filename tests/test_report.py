import time

from arbscan import report
from arbscan.store import connect


def test_report_summarizes_episodes(tmp_path, capsys):
    db = connect(str(tmp_path / "r.db"))
    now = time.time()
    db.executemany("INSERT INTO sweeps (ts, n_pairs, dur_ms, depth_fetches, errors) VALUES (?,?,?,?,?)",
                   [(now - 3600 + 3 * i, 10, 800, 1, 0) for i in range(1200)])
    db.executemany(
        "INSERT INTO episodes (pair, direction, start_ts, end_ts, n_obs, max_top_edge, max_profit, max_size, "
        "first_profit, cost_at_max, days_to_resolve) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("K-1|p-1", "K:YES+P:NO", now - 3000, now - 2994, 3, 0.012, 1.80, 150, 1.50, 146.0, 2.0),
            ("K-1|p-1", "K:YES+P:NO", now - 1000, now - 997, 1, 0.006, 0.30, 50, 0.30, 49.0, 2.0),
            ("K-2|p-2", "K:NO+P:YES", now - 500, now - 400, 30, 0.08, 12.0, 150, 12.0, 120.0, 30.0),
        ],
    )
    db.execute("INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (now - 400, "K-2|p-2", "K:NO+P:YES", 0.08, 150, 120.0, 12.0, 0.07, 30.0, "[]", "[]"))
    db.commit()

    report.run(db, hours=2, min_profit=0, top=5)
    out = capsys.readouterr().out
    assert "Profitable windows: 3" in out
    assert "$14.10" in out  # 1.80 + 0.30 + 12.00
    assert "K-1|p-1 K:YES+P:NO" in out
    # An 8c gap is flagged as more likely a market mismatch than free money.
    k2 = next(line for line in out.splitlines() if "K-2|p-2" in line and "$" in line)
    assert "check:" in k2


def test_report_without_sweeps(tmp_path, capsys):
    db = connect(str(tmp_path / "r.db"))
    report.run(db, hours=24, min_profit=0, top=5)
    assert "No scanner sweeps" in capsys.readouterr().out

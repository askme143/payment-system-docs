from __future__ import annotations

from datetime import date

from payments.scheduler import runner


def test_runner_dispatches_billing_command(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_run_from_config(config, *, job_type, billing_date, limit, dry_run):
        calls.append(
            {
                "config": config,
                "job_type": job_type,
                "billing_date": billing_date,
                "limit": limit,
                "dry_run": dry_run,
            }
        )
        return 0

    monkeypatch.setattr(
        runner,
        "run_scheduler_batch_once_from_config",
        fake_run_from_config,
    )
    monkeypatch.setattr(
        runner,
        "payment_scheduler_config_from_env",
        lambda: "config",
    )

    exit_code = runner.main(
        [
            "billing",
            "--billing-date",
            "2026-06-10",
            "--batch-size",
            "12",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert calls == [
        {
            "config": "config",
            "job_type": "billing",
            "billing_date": date(2026, 6, 10),
            "limit": 12,
            "dry_run": True,
        }
    ]

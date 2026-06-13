from __future__ import annotations

from datetime import UTC, datetime

from payments.application.billing_cycles import next_billing_at


def test_monthly_billing_uses_closest_date_and_preserves_31st_anchor() -> None:
    january_billing = datetime(2026, 1, 31, tzinfo=UTC)
    february_billing = next_billing_at(
        january_billing,
        "monthly",
        billing_anchor_day=31,
    )
    march_billing = next_billing_at(
        february_billing,
        "monthly",
        billing_anchor_day=31,
    )

    assert february_billing == datetime(2026, 2, 28, tzinfo=UTC)
    assert march_billing == datetime(2026, 3, 31, tzinfo=UTC)


def test_monthly_billing_uses_closest_date_and_preserves_30th_anchor() -> None:
    january_billing = datetime(2026, 1, 30, tzinfo=UTC)
    february_billing = next_billing_at(
        january_billing,
        "monthly",
        billing_anchor_day=30,
    )
    march_billing = next_billing_at(
        february_billing,
        "monthly",
        billing_anchor_day=30,
    )

    assert february_billing == datetime(2026, 2, 28, tzinfo=UTC)
    assert march_billing == datetime(2026, 3, 30, tzinfo=UTC)


def test_yearly_billing_preserves_february_29_anchor_across_common_years() -> None:
    billing_dates = [datetime(2024, 2, 29, tzinfo=UTC)]
    for _ in range(4):
        billing_dates.append(
            next_billing_at(
                billing_dates[-1],
                "yearly",
                billing_anchor_day=29,
            )
        )

    assert billing_dates == [
        datetime(2024, 2, 29, tzinfo=UTC),
        datetime(2025, 2, 28, tzinfo=UTC),
        datetime(2026, 2, 28, tzinfo=UTC),
        datetime(2027, 2, 28, tzinfo=UTC),
        datetime(2028, 2, 29, tzinfo=UTC),
    ]

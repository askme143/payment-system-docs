from datetime import UTC, datetime


class Clock:
    def now(self):
        return datetime.now(UTC)

    def normalize_tz(self, dt: datetime):
        dt.astimezone(UTC)

"""Log-debugging fixture generator & verifier helpers for eval/."""

from __future__ import annotations

SERVICES = [
    ("auth", "ERR_JWT_EXPIRED", "token expired for tenant=acme-42"),
    ("billing", "ERR_STRIPE_429", "rate limit exceeded on charge ch_9981"),
    ("worker", "ERR_REDIS_OOM", "OOM command not allowed maxmemory=512mb"),
    ("gateway", "ERR_UPSTREAM_504", "upstream timed out host=10.4.2.19:8080"),
    ("indexer", "ERR_DISK_ENOSPC", "no space left on device /var/lib/idx"),
    ("mailer", "ERR_SMTP_AUTH", "535 authentication failed user=noreply@acme.test"),
]


def generate_noisy_log(service: str, total_lines: int = 450) -> str:
    target = next(s for s in SERVICES if s[0] == service)
    lines = []
    for i in range(1, total_lines + 1):
        if i == 277:
            lines.append(f"2026-11-24T12:04:37Z ERROR [{target[0]}] code={target[1]} msg=\"{target[2]}\" req=req-{i:04d}")
        else:
            lines.append(f"2026-11-24T12:04:{i%60:02d}Z INFO [{target[0]}] heartbeat tick={i} latency_ms=12 status=200 padding_0123456789")
    return "\n".join(lines) + "\n"

"""LLM API call logging — tracks token usage and estimated cost per conversation."""

import datetime
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "scheduler.db")

# USD per million tokens (input_price, output_price)
# Update this dict when Anthropic changes pricing.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5":           (0.80,   4.00),
    "claude-haiku-4-5-20251001":  (0.80,   4.00),
    "claude-sonnet-4-5":          (3.00,  15.00),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-opus-4-5":           (15.00,  75.00),
    "claude-opus-4-7":           (15.00,  75.00),
}
_DEFAULT_PRICING = (3.00, 15.00)  # fallback for unknown models


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    inp_price, out_price = _PRICING.get(model, _DEFAULT_PRICING)
    return (input_tokens * inp_price + output_tokens * out_price) / 1_000_000


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_usage (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT    NOT NULL,
                conversation_id TEXT    NOT NULL,
                model           TEXT    NOT NULL,
                input_tokens    INTEGER NOT NULL,
                output_tokens   INTEGER NOT NULL,
                cost_usd        REAL    NOT NULL,
                user_message    TEXT    NOT NULL,
                context         TEXT    NOT NULL DEFAULT 'main'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS llm_usage_ts   ON llm_usage(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS llm_usage_conv ON llm_usage(conversation_id)")


def log_call(
    conversation_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    user_message: str,
    context: str = "main",
) -> None:
    """Record one API call. Never raises — logging must not break the bot."""
    cost = cost_usd(model, input_tokens, output_tokens)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO llm_usage "
                "(ts, conversation_id, model, input_tokens, output_tokens, cost_usd, user_message, context) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, conversation_id, model, input_tokens, output_tokens, cost, user_message[:500], context),
            )
    except Exception:
        pass


def query_usage(action: str = "recent", days: int = 30, limit: int = 20) -> str:
    """
    Return a formatted usage/cost report.

    action:
      recent    — last `limit` conversations (grouped), newest first
      daily     — cost per calendar day for the last `days` days
      monthly   — cost per month (all time, up to 24 months)
      by_model  — cost breakdown by model for the last `days` days
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row

            if action == "recent":
                rows = conn.execute("""
                    SELECT MIN(ts)              AS ts,
                           conversation_id,
                           GROUP_CONCAT(DISTINCT model) AS models,
                           SUM(input_tokens)    AS total_in,
                           SUM(output_tokens)   AS total_out,
                           SUM(cost_usd)        AS total_cost,
                           MIN(user_message)    AS user_message
                    FROM llm_usage
                    GROUP BY conversation_id
                    ORDER BY MIN(ts) DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                if not rows:
                    return "No LLM usage recorded yet."
                lines = [
                    f"{'Timestamp (UTC)':<20} {'Models':<22} {'In':>7} {'Out':>7} {'Cost':>10}  Message"
                ]
                lines.append("-" * 105)
                for r in rows:
                    msg = r["user_message"][:55].replace("\n", " ")
                    lines.append(
                        f"{r['ts'][:16]:<20} {r['models']:<22} "
                        f"{r['total_in']:>7,} {r['total_out']:>7,} "
                        f"${r['total_cost']:>9.5f}  {msg}"
                    )
                grand = sum(r["total_cost"] for r in rows)
                lines.append(f"\nShowing last {len(rows)} conversations. Shown total: ${grand:.5f}")
                return "\n".join(lines)

            elif action == "daily":
                cutoff = (
                    datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(days=days)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows = conn.execute("""
                    SELECT date(ts)             AS day,
                           GROUP_CONCAT(DISTINCT model) AS models,
                           COUNT(DISTINCT conversation_id) AS conversations,
                           SUM(input_tokens)    AS total_in,
                           SUM(output_tokens)   AS total_out,
                           SUM(cost_usd)        AS total_cost
                    FROM llm_usage
                    WHERE ts >= ?
                    GROUP BY date(ts)
                    ORDER BY day DESC
                """, (cutoff,)).fetchall()
                if not rows:
                    return f"No LLM usage in the last {days} days."
                lines = [
                    f"{'Date':<12} {'Convos':>6} {'In':>9} {'Out':>9} {'Cost':>11}  Models"
                ]
                lines.append("-" * 75)
                for r in rows:
                    lines.append(
                        f"{r['day']:<12} {r['conversations']:>6} "
                        f"{r['total_in']:>9,} {r['total_out']:>9,} "
                        f"${r['total_cost']:>10.5f}  {r['models']}"
                    )
                grand = sum(r["total_cost"] for r in rows)
                lines.append(f"\nTotal last {days} days: ${grand:.5f}")
                return "\n".join(lines)

            elif action == "monthly":
                rows = conn.execute("""
                    SELECT strftime('%Y-%m', ts) AS month,
                           GROUP_CONCAT(DISTINCT model) AS models,
                           COUNT(DISTINCT conversation_id) AS conversations,
                           SUM(input_tokens)    AS total_in,
                           SUM(output_tokens)   AS total_out,
                           SUM(cost_usd)        AS total_cost
                    FROM llm_usage
                    GROUP BY strftime('%Y-%m', ts)
                    ORDER BY month DESC
                    LIMIT 24
                """).fetchall()
                if not rows:
                    return "No LLM usage recorded yet."
                lines = [
                    f"{'Month':<10} {'Convos':>6} {'In':>10} {'Out':>10} {'Cost':>11}  Models"
                ]
                lines.append("-" * 78)
                for r in rows:
                    lines.append(
                        f"{r['month']:<10} {r['conversations']:>6} "
                        f"{r['total_in']:>10,} {r['total_out']:>10,} "
                        f"${r['total_cost']:>10.5f}  {r['models']}"
                    )
                return "\n".join(lines)

            elif action == "by_model":
                cutoff = (
                    datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(days=days)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows = conn.execute("""
                    SELECT model,
                           COUNT(*)             AS calls,
                           COUNT(DISTINCT conversation_id) AS conversations,
                           SUM(input_tokens)    AS total_in,
                           SUM(output_tokens)   AS total_out,
                           SUM(cost_usd)        AS total_cost
                    FROM llm_usage
                    WHERE ts >= ?
                    GROUP BY model
                    ORDER BY total_cost DESC
                """, (cutoff,)).fetchall()
                if not rows:
                    return f"No LLM usage in the last {days} days."
                lines = [
                    f"{'Model':<30} {'Calls':>5} {'Convos':>6} {'In':>10} {'Out':>10} {'Cost':>11}"
                ]
                lines.append("-" * 82)
                for r in rows:
                    lines.append(
                        f"{r['model']:<30} {r['calls']:>5} {r['conversations']:>6} "
                        f"{r['total_in']:>10,} {r['total_out']:>10,} "
                        f"${r['total_cost']:>10.5f}"
                    )
                grand = sum(r["total_cost"] for r in rows)
                lines.append(f"\nTotal last {days} days: ${grand:.5f}")
                return "\n".join(lines)

            else:
                return f"Unknown action: {action!r}. Use 'recent', 'daily', 'monthly', or 'by_model'."

    except Exception as e:
        return f"Error querying LLM usage: {e}"

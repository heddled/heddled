"""Writes tickets to var/tickets/ so the demo has a visible side effect."""

import json
import time
import uuid
from pathlib import Path

TICKETS = Path(__file__).resolve().parents[2] / "var" / "tickets"


def handle(args, ctx):
    TICKETS.mkdir(parents=True, exist_ok=True)
    ticket_id = f"T-{uuid.uuid4().hex[:6].upper()}"
    record = {
        "ticket_id": ticket_id,
        "subject": args["subject"],
        "body": args["body"],
        "priority": args.get("priority", "normal"),
        "created_at": time.time(),
        "session_id": ctx.session_id,
        "agent": ctx.agent,
    }
    (TICKETS / f"{ticket_id}.json").write_text(json.dumps(record, indent=2))
    ctx.log(f"created {ticket_id}")
    return {"ticket_id": ticket_id, "url": f"/var/tickets/{ticket_id}.json"}

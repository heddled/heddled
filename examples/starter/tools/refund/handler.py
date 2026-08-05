"""The tool the concept doc gates behind `requires_approval: true`.

By the time this runs, an approver has already said yes through an approval
adapter — the handler itself stays a plain function.
"""

import json
import time
import uuid
from pathlib import Path

REFUNDS = Path(__file__).resolve().parents[2] / "var" / "refunds"


def handle(args, ctx):
    amount = float(args["amount_eur"])
    if amount <= 0:
        raise ValueError("refund amount must be positive")

    REFUNDS.mkdir(parents=True, exist_ok=True)
    refund_id = f"R-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "refund_id": refund_id,
        "invoice_number": str(args["invoice_number"]).upper(),
        "amount_eur": amount,
        "reason": args.get("reason", ""),
        "issued_at": time.time(),
        "session_id": ctx.session_id,
        "approved": True,
    }
    (REFUNDS / f"{refund_id}.json").write_text(json.dumps(record, indent=2))
    ctx.log(f"refunded €{amount:.2f} on {record['invoice_number']}")
    return {"refund_id": refund_id, "status": "issued", "amount_eur": amount}

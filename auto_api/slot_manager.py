"""
Centralized atomic slot assignment.

Prevents thundering-herd: when 200 sessions detect slots simultaneously, each
gets a *different* slot assigned atomically, not all racing for the same one.

Usage:
  sm = SlotManager()
  sm.update_pool(slots)           # called by any detector session
  lease = sm.request_slot("s1", visible_slots)
  if lease:
      sm.confirm(lease)           # booking succeeded
      # or
      sm.release(lease, "already_taken")   # try another
"""

import threading
import time
from dataclasses import dataclass, field


@dataclass
class SlotLease:
    slot_key:   str    # "{date}_{period_id}"
    date:       str
    period_id:  str
    session_id: str
    leased_at:  float
    TIMEOUT:    float = field(default=60.0, repr=False)


class SlotManager:
    def __init__(self, lease_timeout: float = 60.0):
        self._lock    = threading.Lock()
        self._timeout = lease_timeout
        # slot_key → {"date", "period_id", "fail_count"}
        self._pool:   dict[str, dict] = {}
        # slot_key → SlotLease (currently leased)
        self._leases: dict[str, SlotLease] = {}
        # slot_key → True (permanently taken/confirmed)
        self._taken:  set[str] = set()

    def update_pool(self, slots: list[dict]) -> int:
        """
        Merge fresh slot data into the pool.
        slots: list of {"date": "YYYY-MM-DD", "periods": [{"id": "1", ...}, ...]}
        Returns number of new slots added.
        """
        added = 0
        with self._lock:
            for slot in slots:
                date = slot.get("date", "")
                for p in slot.get("periods", []):
                    pid = str(p.get("id", ""))
                    key = f"{date}_{pid}"
                    if key not in self._pool and key not in self._taken:
                        self._pool[key] = {"date": date, "period_id": pid, "fail_count": 0}
                        added += 1
        return added

    def _expire_leases(self) -> None:
        now = time.monotonic()
        expired = [k for k, l in self._leases.items()
                   if now - l.leased_at > l.TIMEOUT]
        for k in expired:
            del self._leases[k]

    def request_slot(self, session_id: str,
                     visible_slots: list[dict] | None = None) -> SlotLease | None:
        """
        Atomically assign an unclaimed, unleased slot to session_id.
        Prefers slots visible to this session if provided.
        Returns None if no slots available.
        """
        with self._lock:
            self._expire_leases()

            # Build visible key set
            visible_keys: set[str] = set()
            if visible_slots:
                for s in visible_slots:
                    date = s.get("date", "")
                    for p in s.get("periods", []):
                        visible_keys.add(f"{date}_{str(p.get('id',''))}")

            candidates = {k: v for k, v in self._pool.items()
                         if k not in self._taken and k not in self._leases}

            if not candidates:
                return None

            preferred = {k: v for k, v in candidates.items() if k in visible_keys}
            pool = preferred if preferred else candidates

            # Pick: lowest fail_count, then earliest date, then lowest period_id
            best_key = min(pool, key=lambda k: (
                pool[k]["fail_count"],
                pool[k]["date"],
                pool[k]["period_id"].zfill(6),
            ))
            entry = pool[best_key]
            lease = SlotLease(
                slot_key=best_key,
                date=entry["date"],
                period_id=entry["period_id"],
                session_id=session_id,
                leased_at=time.monotonic(),
                TIMEOUT=self._timeout,
            )
            self._leases[best_key] = lease
            return lease

    def confirm(self, lease: SlotLease) -> None:
        """Mark slot as successfully booked (remove from pool permanently)."""
        with self._lock:
            self._taken.add(lease.slot_key)
            self._leases.pop(lease.slot_key, None)
            self._pool.pop(lease.slot_key, None)

    def release(self, lease: SlotLease, reason: str = "") -> None:
        """
        Release a lease back to the pool.
        reason="already_taken" → remove from pool so no other session tries it.
        Other reasons → increment fail_count, keep in pool.
        """
        with self._lock:
            self._leases.pop(lease.slot_key, None)
            if reason == "already_taken":
                self._pool.pop(lease.slot_key, None)
                self._taken.add(lease.slot_key)
            elif lease.slot_key in self._pool:
                self._pool[lease.slot_key]["fail_count"] += 1

    def stats(self) -> dict:
        with self._lock:
            self._expire_leases()
            return {
                "pool":    len(self._pool),
                "leased":  len(self._leases),
                "taken":   len(self._taken),
                "available": len([k for k in self._pool
                                  if k not in self._taken and k not in self._leases]),
            }

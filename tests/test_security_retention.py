"""Security review 2026-09: M14 (the 90-day sweep clears what was derived from
the email, not just the body) and M15 (closing an account removes its files
and portal chats)."""
from app import storage
from app.jobs import purge_email_log


class _Q:
    def __init__(self, log, table):
        self.log, self.table = log, table

    def update(self, row):
        self.log.append((self.table, "update", row)); return self

    def delete(self):
        self.log.append((self.table, "delete", None)); return self

    def __getattr__(self, _name):          # eq / lt / is_ / in_ … chain
        return lambda *a, **k: self

    def execute(self):
        return type("R", (), {"data": []})()


class _DB:
    def __init__(self):
        self.log = []

    def table(self, name):
        return _Q(self.log, name)


def test_sweep_clears_derived_email_columns(monkeypatch):
    fake = _DB()
    monkeypatch.setattr(purge_email_log, "db", lambda: fake)
    import app.db as dbmod
    monkeypatch.setattr(dbmod, "redact_expired_tickets", lambda d: 0)
    monkeypatch.setattr(dbmod, "expire_offered_bookings", lambda: 0)
    purge_email_log.run()
    upd = [row for t, op, row in fake.log if t == "email_log" and op == "update"]
    assert len(upd) == 1
    row = upd[0]
    assert row["body_in"] == "[redacted]" and row["draft_reply"] == "[redacted]"
    for col in ("knowledge_gap", "request_summary", "action_summary", "booking_details",
                "category_reason", "draft_sources", "searched_for"):
        assert col in row and row[col] is None, col
    # the audit fields stay
    assert not {"from_address", "subject", "status", "created_at"} & set(row)


class _Bucket:
    def __init__(self, tree):
        self.tree, self.removed = tree, []

    def list(self, prefix, _opts=None):
        return self.tree.get(prefix, [])

    def remove(self, paths):
        self.removed += paths


def test_remove_tenant_files_walks_every_bucket():
    T = "11111111-2222-3333-4444-555555555555"
    buckets = {
        "documents": _Bucket({T: [{"name": "c1", "id": None}], f"{T}/c1": [
            {"name": "passport.pdf", "id": "x"}, {"name": "cert.pdf", "id": "y"}]}),
        "condition-photos": _Bucket({T: [{"name": "b1", "id": None}], f"{T}/b1": [{"name": "out", "id": None}],
                                     f"{T}/b1/out": [{"name": "0.jpg", "id": "z"}]}),
        "portal-branding": _Bucket({T: [{"name": "banner-ab.png", "id": "w"}]}),
    }
    client = type("C", (), {"storage": type("S", (), {"from_": staticmethod(lambda n: buckets[n])})()})()
    assert storage.remove_tenant_files(T, client=client, dry_run=True) == {
        "documents": 2, "condition-photos": 1, "portal-branding": 1}
    assert all(not b.removed for b in buckets.values())
    storage.remove_tenant_files(T, client=client)
    assert sorted(buckets["documents"].removed) == [f"{T}/c1/cert.pdf", f"{T}/c1/passport.pdf"]
    assert buckets["condition-photos"].removed == [f"{T}/b1/out/0.jpg"]
    assert buckets["portal-branding"].removed == [f"{T}/banner-ab.png"]


def test_delete_tenant_takes_portal_chats(monkeypatch):
    import app.db as dbmod
    fake = _DB()
    monkeypatch.setattr(dbmod, "db", lambda: fake)
    dbmod.delete_tenant("t1")
    assert [(t, op) for t, op, _ in fake.log] == [("portal_messages", "delete"), ("tenants", "delete")]


def test_close_account_removes_files_before_the_cascade():
    import inspect
    from app import dashboard
    src = inspect.getsource(dashboard.close_account)
    assert src.index("remove_tenant_files") < src.index("db.delete_tenant(")

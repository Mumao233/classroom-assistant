"""Creation-based ordering shared by startup selection and classroom lists."""


def creation_order(session):
    value = session.get("creation_order", 0)
    return value if type(value) is int and value > 0 else 0


def newest_first(sessions):
    # Legacy timestamps have only second precision. Their real tie order is
    # unknowable: retain the old startup fallback (smallest ID), not mtime.
    by_id = sorted(sessions, key=lambda session: session["id"])
    return sorted(by_id, key=lambda session: (session["created_at"], creation_order(session)), reverse=True)

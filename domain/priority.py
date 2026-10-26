"""A user's scheduling priority: their fairshare and their pending jobs.

Slurm has no single per-user priority. A job's priority is a weighted
sum (sprio) in which the fairshare factor of the job's (account, user)
association weighs most on Triton, and a user may have several
associations: a department account nobody submits to beside the
aalto_users account their jobs run in. The headline fairshare is the
association the user's jobs actually ran under — the one with the most
decayed usage — and, when no usage tells, their default account's (the
one a job without ``-A`` runs in).
"""


def primary_account(assocs, default_account):
    """The account whose fairshare stands for the user, or None.

    The association with the most ``raw_usage`` wins; with no usage
    anywhere, or a tie at the top, the default account's association;
    with no default known among the associations, the first listed.
    """
    if not assocs:
        return None
    accounts = [a["account"] for a in assocs]
    usage = [a["raw_usage"] or 0 for a in assocs]
    top = max(usage)
    leaders = [acc for acc, u in zip(accounts, usage) if u == top]
    if top > 0 and len(leaders) == 1:
        return leaders[0]
    if default_account in leaders:
        return default_account
    return leaders[0]


def headline_fairshare(assocs, default_account):
    """``(fairshare, account)`` of the user's primary association, or
    ``(None, None)`` for a user sshare does not list."""
    account = primary_account(assocs, default_account)
    for a in assocs or ():
        if a["account"] == account:
            return a["fairshare"], account
    return None, None


def associations(assocs, default_account):
    """Every association of the user, each marked ``primary`` when it
    is the one ``headline_fairshare`` reports."""
    account = primary_account(assocs, default_account)
    return [dict(a, primary=a["account"] == account) for a in assocs or ()]


def pending_with_rank(rows, user):
    """The user's sprio rows, each with its place in its partition's
    queue.

    ``rank`` is 1 + the number of pending rows in the same partition
    with a strictly higher priority, so equal priorities share a rank;
    ``queue_length`` counts the partition's pending rows. A job pending
    in several partitions has one row (and one rank) per partition.
    Rows are ordered by partition, then rank. The user matches
    case-insensitively, like the Jobs tab's user filter.
    """
    by_partition = {}
    for r in rows:
        by_partition.setdefault(r["partition"], []).append(r["priority"] or 0)
    wanted = user.casefold()
    out = []
    for r in rows:
        if r["user"].casefold() != wanted:
            continue
        queue = by_partition[r["partition"]]
        mine = r["priority"] or 0
        out.append(dict(r, rank=1 + sum(1 for p in queue if p > mine),
                        queue_length=len(queue)))
    out.sort(key=lambda r: (r["partition"], r["rank"], r["jobid"]))
    return out

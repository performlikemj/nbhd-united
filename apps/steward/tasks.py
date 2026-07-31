from __future__ import annotations


def steward_collect_github_task():
    from apps.steward.collectors.github import collect_github

    return collect_github()


def steward_collect_asc_task():
    from apps.steward.collectors.asc import collect_asc

    return collect_asc()

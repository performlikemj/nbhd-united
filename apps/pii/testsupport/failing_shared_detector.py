"""Run the production server entrypoint with a deterministic load failure."""

from unittest.mock import patch

from apps.pii import shared_server


def _fail_load(_engine: str):
    raise RuntimeError("synthetic model load failure")


if __name__ == "__main__":
    with patch.object(shared_server, "_local_pipeline_loader", side_effect=_fail_load):
        raise SystemExit(shared_server.main())

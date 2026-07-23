"""RailPulse — Belgian transit SQL analysis (Sprint 1).

A deliberately thin Python layer around a deliberately thick SQL layer.

The challenge forbids using pandas (or any data-frame engine) to filter or
aggregate, so this package is built as **ELT rather than ETL**:

* Python owns exactly two responsibilities — *network I/O* (:mod:`requests`)
  and *executing raw SQL* (:mod:`sqlite3`).
* Every cast, cleaning rule, join, aggregation and metric lives in a file
  under ``sql/`` where a reviewer can read it, diff it and run it by hand.

If you find yourself about to write a loop in Python that inspects a value,
write it in SQL instead — that is the whole design.

Module map
----------
:mod:`railpulse.config`           paths, environment, endpoint + rate-limit constants
:mod:`railpulse.db`               connection factory, PRAGMAs, SQL-script runner
:mod:`railpulse.api_client`       polite HTTP client for the Belgian Mobility API
:mod:`railpulse.ingest_static`    GTFS Static zip  ->  ``stg_*`` tables
:mod:`railpulse.ingest_realtime`  GTFS-RT JSON     ->  ``rt_*`` tables
:mod:`railpulse.build`            orchestrates the full rebuild
:mod:`railpulse.analyse`          runs ``sql/analysis/*.sql`` and writes results
:mod:`railpulse.verify`           post-build integrity assertions
:mod:`railpulse.cli`              ``python -m railpulse <command>``
"""

__version__ = "1.0.0"
__all__ = ["__version__"]

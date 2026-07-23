"""A miniature GTFS feed, built on purpose to be broken in known ways.

Testing the pipeline against the real 26 MB feed proves the pipeline runs; it
does not prove the cleaning rules *fire*, because the real feed only trips one
of the nine (DQ-03, twelve times). So the fixture below is a hand-written feed
of a few dozen rows that violates every rule exactly once, in a way we can
count.

Each defect is tagged with the rule it is meant to trigger, and
``tests/test_transform.py`` asserts both that the good rows survive and that
each bad row lands in ``rejected_row`` with the right ``rule_code``. If someone
weakens a transform rule, a test goes red rather than a number quietly moving.

The fixture also mirrors the two structural quirks of the real SNCB feed, so
the tests exercise the code paths that actually matter here:
  * columns are in ALPHABETICAL order, not GTFS spec order (a positional
    loader would corrupt this feed and the tests would catch it);
  * calendar.txt weekday flags are all zero, with the real calendar in
    calendar_dates.txt.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

# --------------------------------------------------------------------------
# The feed. Note the deliberately alphabetical column order throughout.
# --------------------------------------------------------------------------

AGENCY = """\
agency_fare_url,agency_id,agency_lang,agency_name,agency_phone,agency_timezone,agency_url
,testagency,fr,Test Rail,"",Europe/Brussels,http://example.test/
"""

# DQ-06: the leading space on both dates is exactly what the real feed ships.
FEED_INFO = """\
feed_id,default_lang,feed_contact_email,feed_contact_url,feed_end_date,feed_lang,feed_publisher_name,feed_publisher_url,feed_start_date,feed_version
testfeed,"","",http://example.test/, 20260131,fr,Test Publisher,http://example.test/, 20260101,2026-01-01
"""

# Two stations, four platforms, one platform-less child per station (as the
# real feed does), plus one orphan whose parent does not exist -> DQ-04.
STOPS = """\
location_type,parent_station,platform_code,stop_code,stop_desc,stop_id,stop_lat,stop_lon,stop_name,stop_url,wheelchair_boarding,zone_id
1,,,,STATION,S_CENTRAL,50.845,4.357,Testville-Central,,,
0,S_CENTRAL,,"",PLATFORM,P_CENTRAL_NONE,50.845,4.357,Testville-Central,"",,
0,S_CENTRAL,1,"",PLATFORM,P_CENTRAL_1,50.845,4.357,Testville-Central,"",,
0,S_CENTRAL,2,"",PLATFORM,P_CENTRAL_2,50.845,4.357,Testville-Central,"",,
1,,,,STATION,S_NORTH,51.220,4.421,Testville-Nord,,,
0,S_NORTH,,"",PLATFORM,P_NORTH_NONE,51.220,4.421,Testville-Nord,"",,
0,S_NORTH,1,"",PLATFORM,P_NORTH_1,51.220,4.421,Testville-Nord,"",,
0,S_MISSING,9,"",PLATFORM,P_ORPHAN,51.000,4.000,Nowhere,"",,
"""

# route_type 2 = rail, 3 = replacement bus, mirroring the real feed's split.
ROUTES = """\
agency_id,route_color,route_desc,route_id,route_long_name,route_short_name,route_text_color,route_type,route_url
testagency,016AB3,"",R_RAIL,Central -- Nord,IC,FFFFFF,2,""
testagency,000000,"",R_BUS,Central -- Nord replacement,BUS,FFFFFF,3,""
"""

# DQ-01: every weekday flag is 0, as in the real feed.
CALENDAR = """\
end_date,friday,monday,saturday,service_id,start_date,sunday,thursday,tuesday,wednesday
20260131,0,0,0,SVC_DAILY,20260101,0,0,0,0
20260131,0,0,0,SVC_WEEKEND,20260101,0,0,0,0
20260131,0,0,0,SVC_ONEOFF,20260101,0,0,0,0
"""

# SVC_DAILY   Mon 05 Jan - Fri 09 Jan 2026, i.e. 5 weekdays -> High Frequency
# SVC_WEEKEND Sat 03 + Sun 04 Jan                           -> Medium Frequency
# SVC_ONEOFF  a single date                                 -> Low Frequency
# Plus: one duplicate pair -> DQ-05, and one unknown service_id -> DQ-04.
CALENDAR_DATES = """\
date,exception_type,service_id
20260105,1,SVC_DAILY
20260106,1,SVC_DAILY
20260107,1,SVC_DAILY
20260108,1,SVC_DAILY
20260109,1,SVC_DAILY
20260103,1,SVC_WEEKEND
20260104,1,SVC_WEEKEND
20260115,1,SVC_ONEOFF
20260105,1,SVC_DAILY
20260110,1,SVC_GHOST
"""

# T_ORPHAN_ROUTE references a route that does not exist -> DQ-09.
# bikes_allowed is 1 on rail and empty on the bus, mirroring the real split.
TRIPS = """\
bikes_allowed,block_id,direction_id,route_id,service_id,shape_id,trip_headsign,trip_id,trip_short_name,wheelchair_accessible
1,,,R_RAIL,SVC_DAILY,,Testville-Nord,T_MORNING,101,
1,,,R_RAIL,SVC_DAILY,,Testville-Central,T_EVENING,102,
1,,,R_RAIL,SVC_WEEKEND,,Testville-Nord,T_WEEKEND,103,
,,,R_BUS,SVC_ONEOFF,,Testville-Nord,T_BUS,104,
1,,,R_MISSING,SVC_DAILY,,Nowhere,T_ORPHAN_ROUTE,105,
"""

# Row-by-row intent:
#   T_MORNING  seq 1,2   clean 07:00 origin -> counts as a morning trip
#   T_EVENING  seq 1,2   clean 18:00 origin
#   T_EVENING  seq 2 AGAIN                  -> DQ-05 duplicate call
#   T_WEEKEND  seq 1,2   crosses midnight, published as 24:10 -> day_offset 1
#   T_BUS      seq 1     pickup=1 AND drop_off=1 -> pass-through, not boardable
#   T_MORNING  seq 3     87:16:00                -> DQ-03 implausible
#   T_GHOST    seq 1     unknown trip            -> DQ-04 orphan
#   T_MORNING  seq 4     unknown stop            -> DQ-04 orphan
STOP_TIMES = """\
arrival_time,departure_time,drop_off_type,pickup_type,shape_dist_traveled,stop_headsign,stop_id,stop_sequence,trip_id
07:00:00,07:00:00,1,0,,"",P_CENTRAL_1,1,T_MORNING
07:45:00,07:45:00,0,1,,"",P_NORTH_1,2,T_MORNING
18:00:00,18:00:00,1,0,,"",P_NORTH_1,1,T_EVENING
18:45:00,18:45:00,0,1,,"",P_CENTRAL_2,2,T_EVENING
18:45:00,18:45:00,0,1,,"",P_CENTRAL_2,2,T_EVENING
23:40:00,23:40:00,1,0,,"",P_CENTRAL_1,1,T_WEEKEND
24:10:00,24:10:00,0,1,,"",P_NORTH_1,2,T_WEEKEND
09:00:00,09:00:00,1,1,,"",P_CENTRAL_NONE,1,T_BUS
87:16:00,87:16:00,0,1,,"",P_NORTH_1,3,T_MORNING
10:00:00,10:00:00,0,0,,"",P_CENTRAL_1,1,T_GHOST
11:00:00,11:00:00,0,0,,"",P_NOWHERE,4,T_MORNING
"""

TRANSFERS = """\
from_stop_id,min_transfer_time,to_stop_id,transfer_type,from_trip_id,to_trip_id
P_CENTRAL_1,300,P_CENTRAL_2,2,,
"""

# DQ-07: record_id is empty, so translations are keyed by value.
TRANSLATIONS = """\
table_name,field_name,record_id,record_sub_id,field_value,language,translation
stops,stop_name,"","",Testville-Central,nl,Testdorp-Centraal
stops,stop_name,"","",Testville-Nord,nl,Testdorp-Noord
trips,trip_headsign,"","",Testville-Nord,nl,Testdorp-Noord
"""

FILES = {
    "agency.txt": AGENCY,
    "feed_info.txt": FEED_INFO,
    "stops.txt": STOPS,
    "routes.txt": ROUTES,
    "calendar.txt": CALENDAR,
    "calendar_dates.txt": CALENDAR_DATES,
    "trips.txt": TRIPS,
    "stop_times.txt": STOP_TIMES,
    "transfers.txt": TRANSFERS,
    "translations.txt": TRANSLATIONS,
}

#: What the fixture is engineered to produce, as a quick reference. The
#: authoritative assertions live in tests/test_transform.py — this table is
#: here so a reader can see the arithmetic without running anything.
#:
#:   staged  loaded  quarantined  why
#:   ------  ------  -----------  ---------------------------------------------
#:        6       5            1  platforms: P_ORPHAN has no parent (DQ-04)
#:       10       8            2  service_dates: 1 duplicate (DQ-05),
#:                                1 unknown service (DQ-04)
#:        5       4            1  trips: T_ORPHAN_ROUTE (DQ-09)
#:       11       7            4  stop_times: 1 duplicate call (DQ-05),
#:                                1 at 87:16:00 (DQ-03), 1 unknown trip and
#:                                1 unknown stop (DQ-04)
EXPECTED = {
    "stations": 2,
    "platforms": 5,
    "routes": 2,
    "services": 3,
    "service_dates": 8,
    "trips": 4,
    "stop_times": 7,
}


@pytest.fixture(scope="session")
def mini_feed_zip(tmp_path_factory) -> Path:
    """Write the fixture feed to a zip, exactly as the real feed is shipped."""
    directory = tmp_path_factory.mktemp("mini_gtfs")
    zip_path = directory / "mini_gtfs.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in FILES.items():
            archive.writestr(name, content)
    return zip_path


@pytest.fixture(scope="session")
def built_db(mini_feed_zip, tmp_path_factory) -> Path:
    """Run the real build pipeline over the fixture feed."""
    from railpulse.build import build

    db_path = tmp_path_factory.mktemp("mini_db") / "railpulse_test.db"
    build(offline=True, keep_staging=True, zip_path=mini_feed_zip, db_path=db_path)
    return db_path


@pytest.fixture()
def conn(built_db):
    """A read-only connection to the fixture database."""
    from railpulse.db import connect

    connection = connect(built_db, read_only=True)
    yield connection
    connection.close()

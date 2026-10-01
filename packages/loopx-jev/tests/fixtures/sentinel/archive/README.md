# Sentinel historical recordings

This directory stores immutable evidence that is no longer an active replay
input. Files below `pre_retired_scheduler_cleanup/` preserve the exact bytes
recorded before retired App scheduler commands were removed from the active
fixture matrix. The archived matrix, summary, affected provenance case, and
provider response come from repository tree `ea9e360daa41db631037ea96a599840969a86298`.

The active generator reads only `../constructed/` and `../real/`; replay reads
only the explicitly supplied matrix and response directory. Do not use this
archive as current product guidance or copy its retired command examples into
active fixtures. `test_historical_recording_archive_is_byte_preserving` pins
the archived file set and every byte digest.

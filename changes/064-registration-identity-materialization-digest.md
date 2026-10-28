# Registration identity in materialization verification

The SQLite materialization digest now includes each Worker registration's
`actor_id` and `key_id`. Local integrity checks therefore detect drift in the
projected identity binding as well as routing fields. Cache timestamps remain
excluded because live writes and rebuilds can record different values.

This changes only the verification digest. It does not authenticate signatures,
change Worker authority, or modify immutable RuntimeRecords. A passing integrity
check continues to mean local record/materialization consistency only.

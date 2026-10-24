# Synthetic calibration report task

Work only from the supplied case and standard JSON objects. These fixtures
are synthetic and do not establish real-world sensor quality or regulatory
compliance.

Use normalize_calibration with the complete case and standard. Then pass the
complete JSON result from that successful tool call to analyze_calibration.
Return the complete report produced by the analysis tool as one JSON object.
Do not omit or edit fields or rows. A report with within_tolerance=false can
still be a correct report of an outside-tolerance synthetic measurement.

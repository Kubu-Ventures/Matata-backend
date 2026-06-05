# HDX Disaster Damage Dataset Field Mapping



## Overview



This document defines how CrisisMap export fields map to the Humanitarian Data Exchange (HDX) disaster damage data model.



The mapping is used by the GeoJSON export endpoint (`GET /api/v1/export/geojson`) and serves as the canonical reference for interoperability with humanitarian data platforms and downstream GIS tools.



By aligning exported fields with HDX and HXL conventions, CrisisMap data can be more easily consumed by humanitarian organizations, disaster-response teams, and geospatial analysis workflows.



## Design Principles



The export format follows three core principles:



### Standards Alignment



Where possible, exported fields are mapped to established HDX and HXL conventions to simplify future integration with humanitarian data systems.



### Privacy by Default



Personally identifiable information and internal operational metadata are excluded from all exports.



Only the minimum information required for analysis and coordination is included.



### Stable External Schema



Internal database structures may evolve over time, but exported field names should remain stable to preserve compatibility with external consumers.



---



## Geometry



Each exported report is represented as a GeoJSON Feature with a Point geometry in the WGS84 coordinate reference system (EPSG:4326).



| CrisisMap Field | GeoJSON Key | HDX Standard       | Notes                             |

| --------------- | ----------- | ------------------ | --------------------------------- |

| `lng`, `lat`    | `geometry`  | `Point [lng, lat]` | WGS84 decimal degrees (EPSG:4326) |



---



## Feature Properties



The following table defines the mapping between internal CrisisMap fields and exported GeoJSON properties.



| CrisisMap Field | GeoJSON Property | HDX Standard Field | Type | Notes |

| --------------- | ---------------- | ------------------ | ---- | ----- |

| ...             | ...              | ...                | ...  | ...   |



---



## Privacy and Export Restrictions



Some fields are intentionally excluded from all export formats.



These exclusions are enforced by the `ExportService` and cannot be overridden by API consumers.



The goal is to protect reporter privacy, avoid leaking internal system signals, and prevent exposure of implementation-specific metadata.



| Field | Reason |

| ----- | ------ |

| ...   | ...    |



---



## Shapefile Compatibility



When exporting to ESRI Shapefile format, attribute names must comply with the DBF 10-character field-name limitation.



The following mapping defines the canonical translation between the GeoJSON property names and DBF column names.



| Python Attribute | DBF Column |

| ---------------- | ---------- |

| ...              | ...        |



---



## References



The following resources informed the export schema and interoperability design:



* HDX Data Standards

* Humanitarian Exchange Language (HXL)

* Microsoft Africa Building Footprints
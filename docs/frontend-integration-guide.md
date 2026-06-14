# CrisisMap Frontend Integration Guide

**Base URL (production):** `http://157.173.121.74:8000`
**Interactive docs:** `http://157.173.121.74:8000/docs`
**API version prefix:** `/api/v1`

All request and response bodies are JSON unless the endpoint explicitly uses `multipart/form-data`. All timestamps are ISO 8601 UTC strings. All IDs are UUIDs.

---

## Table of Contents

1. [Concepts You Must Understand First](#1-concepts-you-must-understand-first)
2. [Authentication and Roles](#2-authentication-and-roles)
3. [Error Response Format](#3-error-response-format)
4. [Controlled Vocabularies (Enums)](#4-controlled-vocabularies-enums)
5. [Reporter Flow](#5-reporter-flow)
6. [Analyst and Responder Flow](#6-analyst-and-responder-flow)
7. [Admin Flow](#7-admin-flow)
8. [Public Endpoints (No Auth Required)](#8-public-endpoints-no-auth-required)
9. [Export Endpoints](#9-export-endpoints)
10. [Real-Time Event Stream (SSE)](#10-real-time-event-stream-sse)
11. [Health Endpoints](#11-health-endpoints)
12. [Rate Limits](#12-rate-limits)
13. [Internationalisation](#13-internationalisation)
14. [Offline Sync Protocol](#14-offline-sync-protocol)
15. [Complete Worked Examples](#15-complete-worked-examples)

---

## 1. Concepts You Must Understand First

### Three User Types

The system has three distinct user types. Each has a different authentication path and a different set of permissions.

| User type | How they authenticate | What they can do |
|---|---|---|
| Anonymous reporter | One API call, no credentials | Submit damage reports |
| Verified reporter | Phone number + SMS OTP | Submit reports with higher trust tier |
| Analyst / Responder / Admin | Phone number + SMS OTP (provisioned account) | Review reports, run exports, manage accounts |

### How Tokens Work

Every protected endpoint reads the caller's token from one of two headers. You must send exactly one of them on every request after login:

```
Authorization: Bearer <token>
```

or

```
X-Session-Token: <token>
```

Both headers are equivalent. Use `Authorization: Bearer` as the default. Use `X-Session-Token` only if your HTTP client cannot set the `Authorization` header.

### Token Lifetime

| Token | Lifetime | What to do when it expires |
|---|---|---|
| Anonymous session token | 60 minutes | Call `POST /auth/anonymous` again |
| Access token (reporter / analyst) | 60 minutes | Call `POST /auth/refresh` |
| Refresh token | 30 days | User must log in again with OTP |

Tokens are single-use for refresh rotation. Every call to `POST /auth/refresh` invalidates the old refresh token and returns a new one. Store the new refresh token immediately.

### Role Values Embedded in Tokens

When you decode the JWT (you do not need to, but you can), the `role` claim is one of:

- `anonymous_reporter`
- `reporter`
- `analyst`
- `responder`
- `admin`

The `role` value is also returned directly in the login response body so you do not need to decode the JWT yourself.

---

## 2. Authentication and Roles

### 2.1 Anonymous Reporter Login

Use this when the user has not registered a phone number. No credentials are needed.

**Request**

```
POST /api/v1/auth/anonymous
```

No request body. No headers required.

**Response: 200**

```json
{
  "session_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

Store `session_token` and send it as `Authorization: Bearer <session_token>` on subsequent calls. This token grants `anonymous_reporter` role, which allows report submission only.

---

### 2.2 Reporter Login (Phone OTP)

This is a two-step process. Step 1 sends the OTP. Step 2 verifies it and returns a token.

#### Step 1 of 2: Send OTP

**Request**

```
POST /api/v1/auth/otp/send
Content-Type: application/json
```

```json
{
  "phone": "+254700123456"
}
```

The `phone` field must be E.164 format: a `+` sign, the country code, then the subscriber number, with no spaces or dashes. Example: `+254700123456` for a Kenyan number.

**Response: 200**

```json
{
  "message": "OTP sent successfully."
}
```

The user receives a 6-digit code via SMS (or a voice call if SMS fails).

**Error cases**

| HTTP status | Meaning |
|---|---|
| 400 | Phone number format is invalid |
| 503 | SMS gateway failed to deliver the message |

---

#### Step 2 of 2: Verify OTP

**Request**

```
POST /api/v1/auth/otp/verify
Content-Type: application/json
```

```json
{
  "phone": "+254700123456",
  "otp": "483920"
}
```

The `otp` field must be exactly 6 digits, as a string.

**Response: 200**

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "550e8400-e29b-41d4-a716-446655440000",
  "role": "reporter"
}
```

Store both `token` and `refresh_token`. Send `token` as `Authorization: Bearer <token>` on all subsequent requests.

If the phone number belongs to a provisioned analyst, responder, or admin account, the `role` field will be `analyst`, `responder`, or `admin` instead of `reporter`. The OTP flow is identical; only the returned role differs.

**Error cases**

| HTTP status | Meaning | Action |
|---|---|---|
| 400 | OTP is wrong or has expired | Show error, let user try again or resend |
| 429 | Too many failed attempts (5 failures in 15 min) | Show lockout message, wait 15 minutes |

---

### 2.3 Token Refresh

Call this before the access token expires (60 minutes) to stay logged in without re-doing the OTP flow.

**Request**

```
POST /api/v1/auth/refresh
Content-Type: application/json
```

```json
{
  "refresh_token": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response: 200**

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "a3f8c2d1-91ab-4c3e-b8e7-112233445566"
}
```

The old refresh token is immediately invalidated. Store the new `refresh_token` before making any other calls.

**Error cases**

| HTTP status | Meaning |
|---|---|
| 401 | Refresh token is invalid, expired (30 days), or already used |

When you receive 401 from refresh, the user must log in again with OTP.

---

### 2.4 Logout

**Request**

```
DELETE /api/v1/auth/logout
Authorization: Bearer <token>
```

No request body.

**Response: 200**

```json
{
  "message": "Logged out successfully."
}
```

The token is added to a denylist and rejected on all future requests. Always call logout when the user explicitly signs out.

---

## 3. Error Response Format

Every error from this API has the same shape:

```json
{
  "error": "A human-readable description of what went wrong."
}
```

Never rely on the `error` string value for programmatic logic. Use the HTTP status code instead. The string is for display to the user.

**Common status codes**

| Code | Meaning |
|---|---|
| 400 | Bad request: invalid input data |
| 401 | Unauthenticated: missing or invalid token |
| 403 | Forbidden: valid token but insufficient role |
| 404 | Resource not found |
| 422 | Unprocessable: request structure is valid but content was rejected (e.g. photo failed moderation) |
| 429 | Rate limit exceeded |
| 500 | Server error |
| 503 | Downstream service unavailable (e.g. SMS gateway) |

Every response also carries an `X-Request-ID` header. Include this value in any bug reports or support requests.

---

## 4. Controlled Vocabularies (Enums)

These are the only accepted string values for enum fields. Sending any other value returns HTTP 422.

### crisis_type

| Value | Description |
|---|---|
| `flood` | Flooding event |
| `earthquake` | Seismic event |
| `conflict` | Armed conflict or civil unrest |
| `wildfire` | Fire event |
| `other` | Any other crisis type |

### infrastructure_type

| Value | Description |
|---|---|
| `residential` | Houses and apartments |
| `commercial` | Shops, offices, markets |
| `government` | Government buildings, schools |
| `utilities` | Water, power, telecoms infrastructure |
| `transport` | Roads, bridges, rail |
| `community` | Community centres, places of worship |

### damage_severity (used in report submission)

| Value | Description |
|---|---|
| `minimal` | Superficial damage, structure is usable |
| `partial` | Significant damage, structure is compromised |
| `destroyed` | Complete loss, structure is unusable |

### report_status

| Value | Set by | Meaning |
|---|---|---|
| `pending` | System (on creation) | Awaiting analyst review |
| `verified` | Analyst | Confirmed as accurate |
| `rejected` | Analyst | Dismissed as inaccurate or out of scope |
| `duplicate` | System or analyst | Merged into another report |
| `pending_merge_review` | System (duplicate detector) | Possible duplicate flagged, awaiting analyst decision |

### photo_status

| Value | Meaning |
|---|---|
| `pending` | No photo submitted yet |
| `processing` | Photo uploaded, moderation running |
| `accepted` | Photo passed moderation |
| `rejected` | Photo failed moderation (explicit content) |
| `insufficient_quality` | Photo too blurry or dark for AI analysis |
| `ai_processing_failed` | AI worker encountered an error |

### review_priority (analyst queue)

| Value | Meaning |
|---|---|
| `critical` | AI confidence below 60% or image unusable. Human review mandatory before any action. |
| `high` | AI confidence 60-80%, or AI disagrees with reporter's severity. Analyst should review promptly. |
| `normal` | Default before AI processes the report. |
| `low` | AI confidence above 80%, AI agrees with reporter. Safe to defer. |

### electricity_status

`functional` | `non_functional` | `unknown`

### health_services_status

`accessible` | `inaccessible` | `unknown`

### rejection reason codes (used when rejecting a report)

`inaccurate` | `duplicate` | `poor_quality` | `out_of_scope` | `other`

---

## 5. Reporter Flow

This section covers everything a reporter-facing screen needs.

### 5.1 The Complete Reporter Submission Flow

```
1. GET /api/v1/auth/anonymous          (or use existing token)
2. GET /api/v1/reports/nearby          (check for duplicates at this location)
3. POST /api/v1/reports                (submit the report)
4. GET /api/v1/reports/{id}            (optional: show status to user)
5. PATCH /api/v1/reports/{id}/photo    (optional: upload photo later)
```

Steps 3, 4, and 5 require the token from step 1.

---

### 5.2 Check for Nearby Reports (Pre-submission Duplicate Check)

Show this before the user submits. If matching reports already exist, show them so the user can decide not to submit a duplicate.

**Request**

```
GET /api/v1/reports/nearby?lat=-1.2921&lng=36.8219&radius_m=30
```

No authentication required.

**Query parameters**

| Parameter | Type | Required | Default | Range | Description |
|---|---|---|---|---|---|
| `lat` | float | Yes | - | -90 to 90 | WGS84 latitude |
| `lng` | float | Yes | - | -180 to 180 | WGS84 longitude |
| `radius_m` | float | No | 30 | 1 to 100 | Search radius in metres |

**Response: 200**

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "lat": -1.2920,
    "lng": 36.8218,
    "status": "pending",
    "damage_severity": "partial",
    "created_at": "2026-06-13T08:00:00Z",
    "similarity_score": 0.97
  }
]
```

`similarity_score` is 0.0 to 1.0 where 1.0 means the same GPS location. Show reports with `similarity_score` above 0.8 as likely duplicates.

---

### 5.3 Submit a Report

This endpoint uses `multipart/form-data`, not JSON. There are two parts: a `metadata` field (a JSON string) and an optional `photo` file.

**Request**

```
POST /api/v1/reports
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

Form fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `metadata` | string (JSON) | Yes | See metadata schema below |
| `photo` | file | No | JPEG recommended, max 15 MB |

**metadata JSON schema**

```json
{
  "crisis_type": "flood",
  "infrastructure_type": "residential",
  "damage_severity": "partial",
  "lat": -1.2921,
  "lng": 36.8219,
  "gps_accuracy_m": 10.5,
  "landmark_description": null,
  "electricity_status": "non_functional",
  "health_services_status": "unknown",
  "most_pressing_needs": "Clean water and medical supplies.",
  "debris_clearing_needed": true,
  "offline_queued_at": null
}
```

**metadata field reference**

| Field | Type | Required | Rules |
|---|---|---|---|
| `crisis_type` | string (enum) | Yes | See Section 4 |
| `infrastructure_type` | string (enum) | Yes | See Section 4 |
| `damage_severity` | string (enum) | Yes | See Section 4 |
| `lat` | float or null | Conditional | Required if `landmark_description` is null |
| `lng` | float or null | Conditional | Required if `landmark_description` is null |
| `gps_accuracy_m` | float or null | No | Device GPS accuracy in metres |
| `landmark_description` | string or null | Conditional | Required if `lat`/`lng` are null. Max 500 characters. |
| `electricity_status` | string (enum) or null | No | See Section 4 |
| `health_services_status` | string (enum) or null | No | See Section 4 |
| `most_pressing_needs` | string or null | No | Max 1,000 characters |
| `debris_clearing_needed` | boolean or null | No | - |
| `offline_queued_at` | ISO 8601 string or null | No | Set this to the time the user filled in the form when submitting offline-queued data |

Either `lat` + `lng`, or `landmark_description`, must be present. Both can be present at the same time.

**How to build the multipart request (JavaScript example)**

```javascript
const formData = new FormData();
formData.append('metadata', JSON.stringify({
  crisis_type: 'flood',
  infrastructure_type: 'residential',
  damage_severity: 'partial',
  lat: -1.2921,
  lng: 36.8219,
}));
if (photoFile) {
  formData.append('photo', photoFile);
}

const response = await fetch('/api/v1/reports', {
  method: 'POST',
  headers: { 'Authorization': `Bearer ${token}` },
  body: formData,
  // Do NOT set Content-Type manually. The browser sets it with the correct boundary.
});
```

**Response: 201**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "building_id": null
}
```

`building_id` is null immediately after submission. The GIS worker resolves it in the background within seconds. Call `GET /reports/{id}` after a short delay to get the populated value.

**Error cases**

| HTTP status | Meaning |
|---|---|
| 401 | No valid token |
| 422 | Photo rejected by content moderation (do not disclose rejection reason to user) |
| 429 | Rate limit: maximum 10 reports per hour per session |

---

### 5.4 Get Your Own Report

**Request**

```
GET /api/v1/reports/{report_id}
Authorization: Bearer <token>
```

**Response: 200**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "building_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "crisis_type": "flood",
  "infrastructure_type": "residential",
  "damage_severity": "partial",
  "lat": -1.2921,
  "lng": 36.8219,
  "gps_accuracy_m": 10.5,
  "landmark_description": null,
  "electricity_status": "non_functional",
  "health_services_status": "unknown",
  "most_pressing_needs": "Clean water and medical supplies.",
  "debris_clearing_needed": true,
  "photo_url": "reports/2026/06/13/550e8400.jpg",
  "photo_status": "accepted",
  "status": "pending",
  "reporter_trust_tier": 0,
  "ai_severity_prediction": "partial",
  "ai_confidence": 0.87,
  "ai_quality_score": 0.91,
  "created_at": "2026-06-13T08:00:00Z",
  "updated_at": "2026-06-13T08:00:05Z"
}
```

**Error cases**

| HTTP status | Meaning |
|---|---|
| 403 | The report belongs to a different session |
| 404 | Report ID does not exist |

---

### 5.5 Upload a Photo to an Existing Report (Offline Sync)

Use this when the user submitted the metadata while offline and now wants to attach a photo.

**Request**

```
PATCH /api/v1/reports/{report_id}/photo
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

Form fields:

| Field | Type | Required |
|---|---|---|
| `photo` | file | Yes |

**Response: 200**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "photo_url": "reports/2026/06/13/550e8400.jpg",
  "status": "accepted"
}
```

**Error cases**

| HTTP status | Meaning |
|---|---|
| 403 | The report belongs to a different session |
| 404 | Report ID does not exist |
| 422 | Photo rejected by content moderation |

---

## 6. Analyst and Responder Flow

Analysts and responders log in using the standard OTP flow (Section 2.2). The returned `role` will be `analyst` or `responder`. All analyst endpoints require one of these roles.

The difference between analyst and responder:

- **Analyst**: full access to all reports, can change status, can override AI severity, can merge duplicates
- **Responder**: read-only access scoped to their assigned geographic region, can add notes

---

### 6.1 List Reports (Paginated Feed)

**Request**

```
GET /api/v1/analyst/reports
Authorization: Bearer <analyst-or-responder-token>
```

**Query parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `page` | integer | 1 | Page number, 1-based |
| `limit` | integer | 50 | Items per page, max 200 |
| `crisis_type` | string | - | Comma-separated: `flood,earthquake` |
| `damage_severity` | string | - | Comma-separated: `partial,destroyed` |
| `infrastructure_type` | string | - | Comma-separated: `residential,commercial` |
| `status` | string | - | Comma-separated: `pending,verified` |
| `time_from` | ISO 8601 string | - | Lower bound for `created_at` |
| `time_to` | ISO 8601 string | - | Upper bound for `created_at` |
| `min_ai_confidence` | float | - | 0.0 to 1.0 |
| `review_priority` | string | - | Comma-separated: `critical,high` |
| `divergence_only` | boolean | - | `true` to show only AI/reporter disagreements |
| `sort_by` | string | - | `severity` or `created_at` |

**Response: 200**

```json
{
  "total": 142,
  "page": 1,
  "limit": 50,
  "items": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "building_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "crisis_type": "flood",
      "infrastructure_type": "residential",
      "damage_severity": "partial",
      "status": "pending",
      "photo_status": "accepted",
      "lat": -1.2921,
      "lng": 36.8219,
      "ai_confidence": 0.87,
      "ai_severity_prediction": "partial",
      "ai_divergence": false,
      "analyst_severity_override": null,
      "review_priority": "low",
      "reporter_trust_tier": 1,
      "created_at": "2026-06-13T08:00:00Z",
      "updated_at": "2026-06-13T08:00:05Z"
    }
  ]
}
```

To fetch page 2: `?page=2&limit=50`. The `total` field tells you the total number of matching records so you can compute the number of pages: `Math.ceil(total / limit)`.

---

### 6.2 Get Full Report Detail

**Request**

```
GET /api/v1/analyst/reports/{report_id}
Authorization: Bearer <analyst-or-responder-token>
```

**Response: 200**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "building_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "footprint_geojson": "{\"type\":\"Polygon\",\"coordinates\":[[[36.821,- 1.292],[36.822,-1.292],[36.822,-1.291],[36.821,-1.291],[36.821,-1.292]]]}",
  "crisis_type": "flood",
  "infrastructure_type": "residential",
  "damage_severity": "partial",
  "lat": -1.2921,
  "lng": 36.8219,
  "gps_accuracy_m": 10.5,
  "landmark_description": null,
  "electricity_status": "non_functional",
  "health_services_status": "unknown",
  "most_pressing_needs": "Clean water and medical supplies.",
  "debris_clearing_needed": true,
  "photo_url": "reports/2026/06/13/550e8400.jpg",
  "photo_status": "accepted",
  "ai_quality_score": 0.91,
  "ai_severity_prediction": "partial",
  "ai_confidence": 0.87,
  "ai_divergence": false,
  "analyst_severity_override": null,
  "status": "pending",
  "possible_duplicate_of_id": null,
  "duplicate_score": null,
  "review_priority": "low",
  "reporter_trust_tier": 1,
  "analyst_notes": [
    {
      "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
      "body": "Verified against satellite imagery. Damage matches flood pattern.",
      "created_at": "2026-06-13T09:30:00Z"
    }
  ],
  "building_timeline": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "damage_severity": "partial",
      "status": "pending",
      "created_at": "2026-06-13T08:00:00Z"
    }
  ],
  "created_at": "2026-06-13T08:00:00Z",
  "updated_at": "2026-06-13T08:00:05Z"
}
```

`footprint_geojson` is a GeoJSON Polygon string representing the matched building outline. Parse it with `JSON.parse()` to render it on a map. It is `null` if the GIS worker has not yet matched the report to a building.

`building_timeline` lists all reports associated with the same building, ordered by `created_at`. This lets you see the damage history of a specific structure.

`analyst_notes` are internal-only notes. They are never included in any export.

---

### 6.3 Transition Report Status

Use this to verify, reject, or mark a report as a duplicate.

**Request**

```
PATCH /api/v1/analyst/reports/{report_id}/status
Authorization: Bearer <analyst-token>
Content-Type: application/json
```

**Request body: verify**

```json
{
  "status": "verified",
  "notes": "Confirmed by satellite imagery."
}
```

**Request body: reject**

```json
{
  "status": "rejected",
  "reason_code": "inaccurate",
  "notes": "Location does not match any known flood-affected area."
}
```

`reason_code` is required when `status` is `"rejected"`. Accepted values: `inaccurate`, `duplicate`, `poor_quality`, `out_of_scope`, `other`.

**Request body: mark as duplicate**

```json
{
  "status": "duplicate"
}
```

**Response: 200**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "verified",
  "reporter_trust_tier": 1
}
```

`reporter_trust_tier` is updated by the system as a side effect: verifying a report increments it (max 2), rejecting a report decrements it (min 0).

**Error cases**

| HTTP status | Meaning |
|---|---|
| 403 | Caller is a responder, not an analyst |
| 404 | Report not found |
| 422 | Invalid status transition or missing `reason_code` |

---

### 6.4 Override AI Severity Prediction

Use this when the analyst disagrees with the AI's damage severity assessment.

**Request**

```
POST /api/v1/analyst/reports/{report_id}/severity-override
Authorization: Bearer <analyst-token>
Content-Type: application/json
```

```json
{
  "analyst_severity_override": "destroyed"
}
```

This does not change `damage_severity` (the reporter's value) or `ai_severity_prediction` (the AI's value). It writes to a separate `analyst_severity_override` field, preserving all three values independently.

**Response: 200**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "analyst_severity_override": "destroyed"
}
```

---

### 6.5 Add an Analyst Note

**Request**

```
POST /api/v1/analyst/reports/{report_id}/notes
Authorization: Bearer <analyst-or-responder-token>
Content-Type: application/json
```

```json
{
  "body": "Cross-referenced with local government damage assessment. Building is condemned."
}
```

Max 5,000 characters.

**Response: 201**

```json
{
  "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "body": "Cross-referenced with local government damage assessment. Building is condemned.",
  "created_at": "2026-06-13T09:30:00Z"
}
```

Author identity is never returned. Notes are visible to all analysts on the detail endpoint.

---

### 6.6 Duplicate Merge Workflow

When the duplicate detection system identifies a likely duplicate, it sets `status` to `pending_merge_review` on the newer report and populates `possible_duplicate_of_id` and `duplicate_score`. The analyst must then either confirm or reject the merge.

**Step 1: Detect.** When you see a report with `status === "pending_merge_review"`, show it in the merge review queue. Display the `duplicate_score` (0.0 to 1.0) and the `possible_duplicate_of_id` to help the analyst decide.

**Step 2a: Confirm the merge.**

```
POST /api/v1/analyst/reports/{report_id}/confirm-merge
Authorization: Bearer <analyst-token>
```

No request body.

**Response: 200**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "duplicate",
  "merged_into": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

The confirmed duplicate's `status` becomes `"duplicate"` and `duplicate_of_id` points to the surviving primary record.

**Step 2b: Reject the merge.**

```
POST /api/v1/analyst/reports/{report_id}/reject-merge
Authorization: Bearer <analyst-token>
```

No request body.

**Response: 200**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending"
}
```

The report returns to `"pending"` status for normal analyst review. `possible_duplicate_of_id` and `duplicate_score` are cleared.

---

### 6.7 Manual Merge

Analysts can manually merge reports they have identified as duplicates without waiting for the system to flag them.

**Request**

```
POST /api/v1/analyst/reports/merge
Authorization: Bearer <analyst-token>
Content-Type: application/json
```

```json
{
  "primary_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "duplicate_ids": [
    "550e8400-e29b-41d4-a716-446655440000",
    "660f9500-f30c-52e5-b827-557766551111"
  ]
}
```

`primary_id` is the report that survives. All IDs in `duplicate_ids` are merged into it and their status becomes `"duplicate"`.

**Response: 200**

```json
{
  "primary_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "merged_count": 2
}
```

---

### 6.8 AI Accuracy Metrics

**Request**

```
GET /api/v1/analyst/ai-accuracy
Authorization: Bearer <analyst-token>
```

**Response: 200**

```json
{
  "total_feedback": 184,
  "agreement_rate": 0.82,
  "high_confidence_agreement_rate": 0.91,
  "avg_ai_confidence": 0.74,
  "by_feedback_type": {
    "verify": { "count": 120, "agreement_rate": 0.88 },
    "reject": { "count": 40, "agreement_rate": 0.70 },
    "severity_override": { "count": 24, "agreement_rate": 0.58 }
  },
  "recommended_divergence_threshold": 0.68,
  "high_confidence_feedback_count": 95,
  "min_sample_for_calibration": 30,
  "threshold_updated_at": "2026-06-12T14:00:00Z",
  "threshold_is_stale": false
}
```

This endpoint also applies the recommended divergence threshold to the live system when `high_confidence_feedback_count >= min_sample_for_calibration`. Call it periodically (e.g. once per analyst session) to keep the AI calibration current.

---

## 7. Admin Flow

Admin accounts log in using the same OTP flow. Their `role` is `"admin"`. Admin accounts can do everything analysts can, plus they can provision and deactivate other accounts.

---

### 7.1 Provision a New Analyst, Responder, or Admin Account

**Request**

```
POST /api/v1/auth/analyst/register
Authorization: Bearer <admin-token>
Content-Type: application/json
```

```json
{
  "phone": "+254700123456",
  "role": "analyst",
  "region_geojson": null
}
```

`role` must be one of: `analyst`, `responder`, `admin`.

`region_geojson` is a GeoJSON string defining the geographic scope for a responder. It is required when `role` is `"responder"` and ignored for `analyst` and `admin`.

Example `region_geojson` for a responder covering Nairobi:

```json
"{\"type\":\"Polygon\",\"coordinates\":[[[36.65,-1.45],[37.10,-1.45],[37.10,-1.15],[36.65,-1.15],[36.65,-1.45]]]}"
```

Note: the entire GeoJSON object must be a string (JSON-encoded string inside the JSON body).

**Response: 201**

```json
{
  "message": "Account provisioned. The analyst can now log in via POST /auth/otp/send.",
  "account": {
    "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
    "role": "analyst",
    "region_geojson": null,
    "is_active": true,
    "created_by_sub": "sha256-hash-of-admin-phone"
  }
}
```

After provisioning, the analyst uses the standard OTP flow (Section 2.2) to log in. They do not receive any separate invitation; you must communicate their login instructions out of band.

**Error cases**

| HTTP status | Meaning |
|---|---|
| 400 | Phone number is already registered as an account |
| 403 | Caller is not an admin |
| 422 | Invalid role or invalid phone format |

---

### 7.2 List All Active Accounts

**Request**

```
GET /api/v1/auth/analyst/accounts
Authorization: Bearer <admin-token>
```

**Response: 200**

```json
[
  {
    "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
    "role": "analyst",
    "region_geojson": null,
    "is_active": true,
    "created_by_sub": "sha256-hash-of-admin-phone"
  }
]
```

Only active accounts are returned. Deactivated accounts do not appear.

---

### 7.3 Deactivate an Account

**Request**

```
DELETE /api/v1/auth/analyst/accounts/{account_id}
Authorization: Bearer <admin-token>
```

No request body.

**Response: 200**

```json
{
  "message": "Account deactivated. Existing tokens will expire naturally."
}
```

Deactivation is not instant for tokens already in use. The user's current access token remains valid until it expires (up to 60 minutes). Their refresh token becomes unusable immediately: they will not be able to get a new access token after the current one expires.

**Error cases**

| HTTP status | Meaning |
|---|---|
| 404 | Account not found or already deactivated |
| 403 | Caller is not an admin |

---

## 8. Public Endpoints (No Auth Required)

These endpoints do not require a token.

---

### 8.1 Statistics Summary

**Request**

```
GET /api/v1/stats/summary
```

**Response: 200** (cached 60 seconds)

```json
{
  "total": 842,
  "by_severity": {
    "minimal": 312,
    "partial": 401,
    "destroyed": 129
  },
  "by_crisis_type": {
    "flood": 520,
    "earthquake": 80,
    "conflict": 142,
    "wildfire": 60,
    "other": 40
  },
  "pending_duplicate_count": 7,
  "last_updated": "2026-06-13T09:00:00Z"
}
```

`pending_duplicate_count` is the number of reports in `pending_merge_review` status awaiting analyst action.

---

### 8.2 Heatmap Data

**Request**

```
GET /api/v1/stats/heatmap
```

**Response: 200** (cached 60 seconds)

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "Point",
        "coordinates": [36.8219, -1.2921]
      },
      "properties": {
        "weight": 0.9
      }
    }
  ]
}
```

The `weight` property (0.0 to 1.0) encodes damage severity: `minimal` = 0.33, `partial` = 0.67, `destroyed` = 1.0. Use this directly with any heatmap library (Leaflet.heat, Mapbox, Google Maps heatmap layer).

Note: GeoJSON coordinates are `[longitude, latitude]` order, not `[latitude, longitude]`.

---

## 9. Export Endpoints

All export endpoints require `analyst` or `responder` role. All accept the same filter query parameters.

### Common filter parameters

| Parameter | Description |
|---|---|
| `crisis_type` | Comma-separated crisis types |
| `damage_severity` | Comma-separated severity levels |
| `infrastructure_type` | Comma-separated infrastructure types |
| `status` | Comma-separated report statuses |
| `time_from` | ISO 8601 UTC lower bound for `created_at` |
| `time_to` | ISO 8601 UTC upper bound for `created_at` |
| `min_ai_confidence` | Float 0.0 to 1.0 |

### Async export behaviour

If the filtered result set exceeds 10,000 records, the endpoint does not return the file immediately. Instead it returns a job ID:

```json
{
  "job_id": "export-job-abc123",
  "status": "processing"
}
```

Poll `GET /api/v1/export/jobs/{job_id}` until `status` is `"complete"` or `"failed"`, then download from `download_url`.

---

### 9.1 GeoJSON Export

**Request**

```
GET /api/v1/export/geojson?crisis_type=flood&damage_severity=partial,destroyed
Authorization: Bearer <analyst-or-responder-token>
```

Additional parameter:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `include_footprints` | boolean | false | Append building footprint polygons to the export |

**Response: 200**

Content-Type: `application/geo+json`
File download: `crisismap_export_2026-06-13.geojson`

---

### 9.2 CSV Export

**Request**

```
GET /api/v1/export/csv
Authorization: Bearer <analyst-or-responder-token>
```

**Response: 200**

Content-Type: `text/csv; charset=utf-8`
File download: `crisismap_export_2026-06-13.csv`

- Booleans are exported as `TRUE` or `FALSE`
- Timestamps are ISO 8601 UTC
- Analyst notes are excluded from all exports

---

### 9.3 Shapefile Export

**Request**

```
GET /api/v1/export/shapefile
Authorization: Bearer <analyst-or-responder-token>
```

**Response: 200**

Content-Type: `application/zip`
File download: `crisismap_export_2026-06-13.zip`

The ZIP contains `.shp`, `.dbf`, `.shx`, and `.prj` files. CRS is WGS84 (EPSG:4326).

---

### 9.4 Check Async Export Job Status

**Request**

```
GET /api/v1/export/jobs/{job_id}
Authorization: Bearer <analyst-or-responder-token>
```

**Response: 200**

```json
{
  "status": "complete",
  "download_url": "https://storage.example.com/exports/crisismap_export_2026-06-13.geojson?signature=...",
  "expires_at": "2026-06-14T09:00:00Z"
}
```

`status` is one of: `processing`, `complete`, `failed`.
`download_url` is a presigned URL valid for 24 hours. It is `null` while status is `processing`.
Poll every 3 to 5 seconds while status is `processing`.

---

## 10. Real-Time Event Stream (SSE)

The analyst dashboard can receive live push notifications when new reports are created or updated. This uses the Server-Sent Events (SSE) protocol, which is a one-way stream from server to client over a persistent HTTP connection.

**Request**

```
GET /api/v1/analyst/stream
Authorization: Bearer <analyst-or-responder-token>
Accept: text/event-stream
```

**How to connect (JavaScript example)**

```javascript
const eventSource = new EventSource(
  'http://157.173.121.74:8000/api/v1/analyst/stream',
  {
    headers: { 'Authorization': `Bearer ${token}` }
    // Note: native EventSource does not support custom headers.
    // Use a polyfill such as 'event-source-polyfill' or 'eventsource' npm package.
  }
);

eventSource.addEventListener('report.created', (e) => {
  const data = JSON.parse(e.data);
  console.log('New report:', data);
});

eventSource.addEventListener('report.updated', (e) => {
  const data = JSON.parse(e.data);
  console.log('Report updated:', data);
});

eventSource.addEventListener('report.critical', (e) => {
  const data = JSON.parse(e.data);
  console.log('Critical report:', data);
  // Show a high-priority alert
});
```

**Event types**

| Event name | When it fires | Payload |
|---|---|---|
| `report.created` | A new report has been submitted | Report ID, lat, lng, severity, crisis type |
| `report.updated` | A report's status or AI data has changed | Report ID and updated fields |
| `report.critical` | A report with `review_priority = "critical"` has arrived | Report ID, lat, lng, severity |

**Heartbeat**

The server sends a comment line every 30 seconds:

```
: heartbeat
```

This keeps the connection alive through reverse proxies and load balancers. Your SSE client should ignore comment lines automatically.

**Native EventSource and headers**

The browser's native `EventSource` API does not support custom request headers, which means you cannot pass `Authorization: Bearer` with it. Use one of these approaches:

Option A: Use the `event-source-polyfill` npm package, which supports headers.

Option B: Use a short-lived query-parameter token (contact the backend team if this approach is needed).

Option C: Use `X-Session-Token` as a cookie if your setup allows it.

**Responder geographic scoping**

If the logged-in user is a responder with an assigned region, the stream automatically filters events to reports within that region. No extra configuration is needed on the frontend.

---

## 11. Health Endpoints

These are used by deployment infrastructure. You can call them from the frontend to check if the server is up.

### Liveness

```
GET /health
```

Response: `{"status": "ok", "version": "0.1.0"}`

This always responds immediately with no I/O. If this endpoint fails, the server process is not running.

### Readiness

```
GET /health/ready
```

Response: `{"status": "ready", "checks": {"postgres": "ok", "redis": "ok", "storage": "ok"}}`

HTTP 200 when all dependencies are healthy, HTTP 503 when any are not. If the `storage` check shows an error but `postgres` and `redis` are `"ok"`, report submission still works (photo upload may fail).

---

## 12. Rate Limits

When a rate limit is exceeded you receive HTTP 429 with:

```json
{
  "error": "Rate limit exceeded. Please try again later."
}
```

The response also includes these headers:

| Header | Description |
|---|---|
| `X-RateLimit-Limit` | Maximum requests allowed in the window |
| `X-RateLimit-Remaining` | Requests remaining in the current window |
| `X-RateLimit-Reset` | Unix timestamp when the window resets |
| `Retry-After` | Seconds to wait before retrying |

**Rate limits by endpoint**

| Endpoint | Limit |
|---|---|
| `POST /auth/otp/send` | Per phone number (backend-enforced) |
| `POST /auth/otp/verify` | 5 failures per 15 minutes per phone number, then locked out |
| `POST /reports` | 10 submissions per hour per session token |

When the OTP verify endpoint returns 429, show a message telling the user to wait 15 minutes before trying again. Do not offer a "resend" button during the lockout period.

---

## 13. Internationalisation

The API supports multiple languages for error messages. Add the `lang` query parameter to any request:

```
POST /api/v1/auth/otp/send?lang=sw
```

Or set the `Accept-Language` header:

```
Accept-Language: sw, en;q=0.9
```

Language resolution order: `lang` query parameter takes priority, then `Accept-Language` header, then English as the default.

Currently supported language codes: `en` (English), `sw` (Swahili). Additional languages can be added by the backend team without code changes.

---

## 14. Offline Sync Protocol

CrisisMap supports reporters who submit from areas with intermittent connectivity. The offline sync protocol has two steps.

**Step 1: Submit metadata while offline (or immediately when connectivity returns)**

Submit `POST /api/v1/reports` with `offline_queued_at` set to the ISO 8601 timestamp of when the user actually filled in the form. Include the photo if available.

```json
{
  "crisis_type": "flood",
  "infrastructure_type": "residential",
  "damage_severity": "partial",
  "lat": -1.2921,
  "lng": 36.8219,
  "offline_queued_at": "2026-06-13T06:30:00Z"
}
```

**Step 2: Upload the photo later**

If the photo was too large to upload in step 1, or the user took the photo after submitting the metadata, use:

```
PATCH /api/v1/reports/{id}/photo
Authorization: Bearer <token>
Content-Type: multipart/form-data
```

with `photo` as the only form field.

**Important:** The anonymous session token expires after 60 minutes. If the user submitted a report anonymously and then loses connectivity for more than 60 minutes, they will not be able to upload the photo later because the token will have expired. Handle this case by showing a warning and prompting the user to re-authenticate before they leave connectivity range.

---

## 15. Complete Worked Examples

### Example A: Anonymous reporter submits a flood damage report with a photo

```javascript
// Step 1: Get anonymous token
const authRes = await fetch('/api/v1/auth/anonymous', { method: 'POST' });
const { session_token } = await authRes.json();

// Step 2: Check for duplicates near this location
const nearbyRes = await fetch('/api/v1/reports/nearby?lat=-1.2921&lng=36.8219&radius_m=30');
const nearby = await nearbyRes.json();
if (nearby.length > 0 && nearby[0].similarity_score > 0.8) {
  // Show duplicate warning to user
}

// Step 3: Submit the report
const formData = new FormData();
formData.append('metadata', JSON.stringify({
  crisis_type: 'flood',
  infrastructure_type: 'residential',
  damage_severity: 'partial',
  lat: -1.2921,
  lng: 36.8219,
  gps_accuracy_m: 12.0,
  electricity_status: 'non_functional',
  most_pressing_needs: 'Emergency shelter needed.',
  debris_clearing_needed: false,
}));
formData.append('photo', photoFile);

const reportRes = await fetch('/api/v1/reports', {
  method: 'POST',
  headers: { 'Authorization': `Bearer ${session_token}` },
  body: formData,
});

if (reportRes.status === 201) {
  const { id } = await reportRes.json();
  showSuccess(`Report submitted. ID: ${id}`);
} else if (reportRes.status === 422) {
  showError('Your photo was rejected. Please try a different image.');
} else if (reportRes.status === 429) {
  showError('You have submitted too many reports. Please wait 1 hour.');
}
```

---

### Example B: Analyst logs in and works through the review queue

```javascript
// Step 1: Send OTP
await fetch('/api/v1/auth/otp/send', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ phone: '+254700123456' }),
});

// Step 2: Verify OTP (user types in 6-digit code from SMS)
const verifyRes = await fetch('/api/v1/auth/otp/verify', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ phone: '+254700123456', otp: '483920' }),
});
const { token, refresh_token, role } = await verifyRes.json();
// role will be "analyst" for a provisioned analyst account

// Step 3: Load the critical review queue
const queueRes = await fetch(
  '/api/v1/analyst/reports?review_priority=critical,high&status=pending&sort_by=severity',
  { headers: { 'Authorization': `Bearer ${token}` } }
);
const { total, items } = await queueRes.json();

// Step 4: Open a specific report
const detailRes = await fetch(`/api/v1/analyst/reports/${items[0].id}`, {
  headers: { 'Authorization': `Bearer ${token}` },
});
const report = await detailRes.json();

// Step 5: Verify the report
await fetch(`/api/v1/analyst/reports/${report.id}/status`, {
  method: 'PATCH',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    status: 'verified',
    notes: 'Confirmed against satellite imagery.',
  }),
});

// Step 6: Refresh the token before it expires (do this proactively, e.g. every 50 minutes)
const refreshRes = await fetch('/api/v1/auth/refresh', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ refresh_token }),
});
const newTokens = await refreshRes.json();
// Store newTokens.token and newTokens.refresh_token
```

---

### Example C: Admin provisions a new analyst

```javascript
const res = await fetch('/api/v1/auth/analyst/register', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${adminToken}`,
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    phone: '+254711000000',
    role: 'analyst',
    region_geojson: null,
  }),
});

if (res.status === 201) {
  const { account } = await res.json();
  showSuccess(`Account created. ID: ${account.id}`);
} else if (res.status === 400) {
  showError('This phone number is already registered.');
}
```

---

### Example D: Frontend handles token expiry

```javascript
async function apiFetch(url, options = {}) {
  const token = localStorage.getItem('access_token');
  const res = await fetch(url, {
    ...options,
    headers: {
      ...options.headers,
      'Authorization': `Bearer ${token}`,
    },
  });

  if (res.status === 401) {
    // Token expired. Try to refresh.
    const refreshToken = localStorage.getItem('refresh_token');
    if (!refreshToken) {
      redirectToLogin();
      return;
    }

    const refreshRes = await fetch('/api/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });

    if (!refreshRes.ok) {
      // Refresh token is also expired or invalid. User must log in again.
      localStorage.removeItem('access_token');
      localStorage.removeItem('refresh_token');
      redirectToLogin();
      return;
    }

    const { token: newToken, refresh_token: newRefresh } = await refreshRes.json();
    localStorage.setItem('access_token', newToken);
    localStorage.setItem('refresh_token', newRefresh);

    // Retry the original request with the new token
    return fetch(url, {
      ...options,
      headers: {
        ...options.headers,
        'Authorization': `Bearer ${newToken}`,
      },
    });
  }

  return res;
}
```

---

## Quick Reference Card

### Reporter endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/auth/anonymous` | None | Get anonymous token |
| POST | `/api/v1/auth/otp/send` | None | Send OTP to phone |
| POST | `/api/v1/auth/otp/verify` | None | Verify OTP, get token |
| POST | `/api/v1/auth/refresh` | None | Rotate refresh token |
| DELETE | `/api/v1/auth/logout` | Token | Revoke token |
| GET | `/api/v1/reports/nearby` | None | Check for duplicate reports |
| POST | `/api/v1/reports` | Token | Submit damage report |
| GET | `/api/v1/reports/{id}` | Token | View own report |
| PATCH | `/api/v1/reports/{id}/photo` | Token | Upload photo (offline sync) |

### Public stats endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/stats/summary` | None | Aggregated counters |
| GET | `/api/v1/stats/heatmap` | None | GeoJSON heatmap data |

### Analyst and responder endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/analyst/reports` | Analyst or Responder | List reports (paginated) |
| GET | `/api/v1/analyst/reports/{id}` | Analyst or Responder | Full report detail |
| PATCH | `/api/v1/analyst/reports/{id}/status` | Analyst only | Verify, reject, or mark duplicate |
| POST | `/api/v1/analyst/reports/{id}/severity-override` | Analyst only | Override AI severity |
| POST | `/api/v1/analyst/reports/{id}/notes` | Analyst or Responder | Add note |
| POST | `/api/v1/analyst/reports/{id}/confirm-merge` | Analyst only | Confirm system-suggested merge |
| POST | `/api/v1/analyst/reports/{id}/reject-merge` | Analyst only | Reject system-suggested merge |
| POST | `/api/v1/analyst/reports/merge` | Analyst only | Manual duplicate merge |
| GET | `/api/v1/analyst/ai-accuracy` | Analyst only | AI accuracy metrics |
| GET | `/api/v1/analyst/stream` | Analyst or Responder | SSE real-time events |

### Export endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/export/geojson` | Analyst or Responder | GeoJSON export |
| GET | `/api/v1/export/csv` | Analyst or Responder | CSV export |
| GET | `/api/v1/export/shapefile` | Analyst or Responder | Shapefile ZIP export |
| GET | `/api/v1/export/jobs/{job_id}` | Analyst or Responder | Async job status |

### Admin endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/auth/analyst/register` | Admin only | Provision new account |
| GET | `/api/v1/auth/analyst/accounts` | Admin only | List active accounts |
| DELETE | `/api/v1/auth/analyst/accounts/{id}` | Admin only | Deactivate account |

### Health endpoints (no auth, no `/api/v1` prefix)

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness probe |
| GET | `/health/ready` | Readiness probe (checks Postgres, Redis, storage) |

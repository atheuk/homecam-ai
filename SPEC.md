# HomeCam AI — Master Product & Engineering Specification

**Status:** Initial build specification  
**Development:** Visual Studio Code + GitHub Copilot Agent  
**Source control:** GitHub  
**Deployment strategy:** Local Docker first; Microsoft Azure later  
**Architecture:** Local-first, provider-independent, AI-enabled home camera platform

---

## 1. Mission

Build **HomeCam AI**, a modern, privacy-conscious web application that combines existing home camera systems into one intelligent interface.

Initial hardware:

1. **Dahua DHI-NVR4204-P-4KS2** NVR with IP cameras.
2. **Eufy Video Doorbell 2K (Battery-Powered), model T8210**, connected to **Eufy HomeBase 2**.

The existing Dahua NVR and Eufy HomeBase remain responsible for their normal recording/storage functions. HomeCam AI is an intelligence, search, viewing, event-correlation, and notification layer above them.

The application must not require replacing existing cameras, the NVR, HomeBase, or doorbell.

Long term, HomeCam AI should provide:

- unified live camera viewing
- recorded video playback
- unified event timeline
- doorbell events
- AI event descriptions
- person/vehicle/animal/package detection
- natural-language camera-history search
- cross-camera activity correlation
- intelligent notifications
- daily/away summaries
- optional person clustering
- system-health monitoring
- secure remote access
- future Microsoft Azure deployment

The UI should feel like a modern smart-home camera product rather than a traditional NVR administration interface.

---

## 2. Non-negotiable architectural principles

### 2.1 Provider independence

HomeCam Core must not depend directly on Dahua or Eufy implementation details.

All hardware-specific behavior must be isolated behind provider adapters.

```text
Dahua NVR ──────> DahuaProvider ────┐
                                    │
Eufy HomeBase ──> EufyProvider ─────┤
                                    │
Mock devices ───> MockProvider ─────┤
                                    │
Future ONVIF ───> OnvifProvider ────┤
                                    ▼
                              HomeCam Core
```

Adding a new provider must not require rewriting the core application or frontend.

### 2.2 Existing systems remain authoritative

Dahua remains authoritative for Dahua recordings.

Eufy/HomeBase remains authoritative for Eufy recordings where integration permits.

HomeCam must not continuously duplicate all video.

HomeCam primarily stores:

- provider/device metadata
- normalized events
- AI analysis
- thumbnails
- embeddings
- activity correlations
- recording references
- optionally short event clips

### 2.3 AI is event-driven

Do not continuously send all camera video to AI.

Preferred pipeline:

```text
Provider event
      ↓
Normalize event
      ↓
Retrieve snapshot / short clip
      ↓
Local inexpensive object detection
      ↓
Is event interesting?
      ↓
AI vision analysis
      ↓
Structured description
      ↓
Embedding
      ↓
Searchable event database
```

This reduces compute, bandwidth, cost, latency, and privacy exposure.

### 2.4 Local-first

The first working system must run locally using Docker Compose.

Azure is a later deployment target and must not be required for development.

### 2.5 Graceful degradation

A failure in Dahua, Eufy, AI, or one camera must not bring down the rest of HomeCam.

Capabilities unavailable from a provider must be shown as unavailable rather than simulated or invented.

### 2.6 Never fabricate integration behavior

Do not invent Dahua endpoints, Eufy endpoints, undocumented protocol behavior, device IDs, credentials, URLs, or API responses.

When external behavior is uncertain:

1. document the uncertainty;
2. isolate it behind the provider interface;
3. use the mock provider;
4. continue building components that do not depend on it.

---

## 3. Primary user experience

Support:

- desktop browsers
- tablets
- mobile browsers

Primary navigation:

```text
Home
Live
Events
Search
Activities
People
System
Settings
```

The UI should be:

- responsive
- fast
- accessible
- dark-mode capable
- camera-focused
- visually clean
- suitable for touch devices

---

## 4. Home dashboard

Show:

- overall system status
- camera tiles
- latest snapshot/live preview
- provider status
- recent important events
- doorbell activity
- AI summary
- warnings such as offline cameras or low doorbell battery

Example:

```text
HOME                                      ● SYSTEM HEALTHY

CAMERAS

┌─────────────────┐ ┌─────────────────┐
│ FRONT DOOR      │ │ DRIVEWAY        │
│                 │ │                 │
│      LIVE       │ │      LIVE       │
└─────────────────┘ └─────────────────┘

┌─────────────────┐ ┌─────────────────┐
│ BACKYARD        │ │ GARDEN          │
│                 │ │                 │
│      LIVE       │ │      LIVE       │
└─────────────────┘ └─────────────────┘

┌─────────────────┐
│ FRONT DOORBELL  │
│ EUFY T8210      │
│      LIVE       │
└─────────────────┘

RECENT ACTIVITY

21:42  👤 Person at front door
21:31  🚗 Vehicle entered driveway
20:54  📦 Package delivery
20:17  👤 Person in backyard
```

Each camera tile should expose, when available:

- name
- latest image/live preview
- online/offline state
- provider
- device type
- last event
- battery for battery-powered devices
- quick fullscreen action

Camera names must be editable.

---

## 5. Camera provider abstraction

Create a generic provider contract.

Conceptual model:

```typescript
interface CameraProvider {
  getProviderInfo(): Promise<ProviderInfo>;
  discoverDevices(): Promise<Camera[]>;
  getCapabilities(cameraId: string): Promise<CameraCapabilities>;
  getSnapshot(cameraId: string): Promise<CameraSnapshot>;
  getLiveStream(cameraId: string): Promise<StreamDescriptor>;
  getEvents(cameraId: string, range: TimeRange): Promise<CameraEvent[]>;
  getRecordings(cameraId: string, range: TimeRange): Promise<RecordingReference[]>;
  subscribeEvents(callback: EventCallback): Promise<Subscription>;
  getHealth(): Promise<ProviderHealth>;
}
```

Not every provider supports every operation.

Capability status should support:

```text
SUPPORTED
UNSUPPORTED
UNAVAILABLE
UNKNOWN
```

Example capabilities:

```json
{
  "liveStream": "SUPPORTED",
  "snapshot": "SUPPORTED",
  "recordings": "SUPPORTED",
  "motionEvents": "SUPPORTED",
  "personEvents": "SUPPORTED",
  "vehicleEvents": "UNSUPPORTED",
  "doorbellEvents": "SUPPORTED",
  "twoWayAudio": "UNKNOWN",
  "battery": "SUPPORTED",
  "ptz": "UNSUPPORTED"
}
```

The frontend must adapt to capabilities instead of hard-coding provider assumptions.

---

## 6. Unified camera model

Conceptual model:

```typescript
interface Camera {
  id: string;
  providerId: string;
  providerCameraId: string;
  manufacturer?: string;
  model?: string;
  name: string;
  location?: string;
  type: "camera" | "doorbell";
  status: "online" | "offline" | "degraded" | "unknown";
  capabilities: CameraCapabilities;
}
```

Initial logical devices for mock development:

```text
Front Door
Driveway
Backyard
Garden
Front Doorbell
```

---

## 7. Dahua provider

Create:

```text
DahuaProvider
```

Target:

```text
Dahua DHI-NVR4204-P-4KS2
```

Use documented/supported interfaces wherever possible.

Preferred integration strategy:

```text
Dahua HTTP/API/CGI where verified
ONVIF where appropriate
RTSP for media
```

Do not scrape the Dahua web UI.

Responsibilities, where supported:

```text
connect/authenticate
discoverCameras
getCameraStatus
getCapabilities

getSnapshot
getLiveStream

searchRecordings
getRecordingReference
exportClip

subscribeEvents
getMotionEvents
getSmartEvents

getNvrHealth
getStorageHealth
```

All Dahua credentials remain server-side.

The Dahua provider must include timeouts, retries, structured errors, health status, and useful logs without secrets.

Implement functionality incrementally and verify each capability against the real NVR before marking it supported.

---

## 8. Eufy T8210 + HomeBase 2 provider

Create:

```text
EufyProvider
```

Explicit target hardware:

```text
Doorbell: Eufy Video Doorbell 2K (Battery-Powered)
Model: T8210
Base station: Eufy HomeBase 2
```

Architecture:

```text
Eufy T8210
    │
    ▼
HomeBase 2
    │
    ▼
EufyProvider
    │
    ▼
HomeCam Core
```

Do not assume standard ONVIF/RTSP support.

Do not assume a feature is available merely because it exists in the Eufy mobile application.

The Eufy provider must dynamically determine or explicitly configure supported capabilities.

Attempt to expose, where technically verified:

- HomeBase/device status
- doorbell press events
- motion events
- person events
- snapshots/event thumbnails
- recorded event retrieval
- live video
- audio
- battery percentage/state
- connectivity state
- two-way audio

Unsupported functionality must fail gracefully.

Eufy-specific or unofficial integration code must remain isolated under:

```text
apps/api/app/providers/eufy/
```

If the selected integration is unofficial or reverse-engineered, document this clearly in `docs/eufy.md`.

Core HomeCam behavior must not depend on an undocumented Eufy feature.

### 8.1 Doorbell priority

A physical doorbell press is a high-priority semantic event.

Normalize it approximately as:

```json
{
  "type": "doorbell",
  "source": "provider",
  "priority": "high"
}
```

Target pipeline:

```text
T8210 doorbell press
        ↓
EufyProvider
        ↓
HomeCam Event
        ├── thumbnail if available
        ├── local detection
        ├── AI analysis if configured
        └── notification evaluation
        ↓
Frontend real-time update
```

### 8.2 Eufy live-video fallback

If reliable live-video access is available, route it through the HomeCam media layer.

If it is not available, HomeCam must still support the doorbell through available features such as:

- latest snapshot
- event thumbnails
- recent events
- battery state
- status
- AI analysis of event media

Eufy live streaming is not a V1 blocker.

### 8.3 Battery

Where accessible, display battery status.

Default low-battery warning threshold:

```text
20%
```

Threshold must be configurable.

---

## 9. Unified event model

All providers map to a single event model.

Conceptual model:

```typescript
interface HomeCamEvent {
  id: string;
  providerId: string;
  cameraId: string;
  providerEventId?: string;

  startTime: Date;
  endTime?: Date;

  type:
    | "motion"
    | "person"
    | "vehicle"
    | "animal"
    | "package"
    | "doorbell"
    | "intrusion"
    | "unknown";

  source:
    | "provider"
    | "local-ai"
    | "cloud-ai";

  priority:
    | "low"
    | "normal"
    | "high"
    | "critical";

  thumbnailUrl?: string;
  recordingReference?: string;
  description?: string;
  aiAnalysisId?: string;
}
```

The frontend must not implement separate Dahua and Eufy event UIs except for capability-specific actions.

---

## 10. Live video

Browsers must not connect directly to RTSP.

Preferred architecture:

```text
Camera / NVR
     │
    RTSP or provider stream
     │
     ▼
Media Gateway
     │
     ├── WebRTC
     └── HLS fallback
             │
             ▼
          Browser
```

Preferred tools:

```text
MediaMTX
FFmpeg
```

Use lower-resolution/substreams for multi-camera dashboards where available.

Use the main/high-quality stream for single-camera/fullscreen viewing.

Avoid unnecessary transcoding.

Live screen should support when capabilities allow:

- camera grid
- single camera
- fullscreen
- quality selection
- audio mute/unmute
- two-way audio
- provider/status indicator

---

## 11. Recorded video

Provide a unified playback experience.

Users should be able to:

- select camera
- select date
- view timeline
- see event markers
- seek
- jump to an event
- play/pause
- adjust playback speed
- export/download a clip where supported

Example:

```text
18:00      19:00      20:00      21:00      NOW
──────────────────────────────────────────────
       ▲          ▲       ▲           ▲
     Person     Motion   Vehicle     Doorbell
```

Opening an event should attempt to start playback roughly 10 seconds before the event when technically possible.

Do not duplicate complete Dahua recording archives into HomeCam.

---

## 12. Event ingestion

Implement background event processing.

```text
Provider event
     ↓
Normalize
     ↓
Persist
     ↓
Get snapshot / media if available
     ↓
Local detector
     ↓
AI analysis if required
     ↓
Embedding
     ↓
Notification rules
     ↓
Activity correlation
     ↓
Real-time frontend update
```

Use WebSocket or Server-Sent Events for near-real-time frontend updates.

---

## 13. Local object detection

Support an inexpensive local detection stage.

Initial preferred approach:

```text
YOLO
```

Useful categories include:

```text
person
car
truck
bicycle
motorcycle
dog
cat
package/object where feasible
```

Do not call expensive cloud AI for obviously irrelevant motion unless configuration explicitly requests it.

The detector implementation must be replaceable.

---

## 14. AI provider abstraction

Create:

```text
AIProvider
```

Conceptual contract:

```typescript
interface AIProvider {
  analyzeImage(
    image: Buffer,
    context: AnalysisContext
  ): Promise<ImageAnalysis>;

  analyzeClip?(
    clip: Buffer,
    context: AnalysisContext
  ): Promise<VideoAnalysis>;

  createEmbedding(text: string): Promise<number[]>;

  answerQuestion(
    question: string,
    events: HomeCamEvent[]
  ): Promise<AIAnswer>;
}
```

Potential implementations:

```text
AzureOpenAIProvider
OpenAIProvider
LocalAIProvider
MockAIProvider
```

HomeCam Core must not import provider-specific AI SDKs directly.

---

## 15. AI output

AI analysis must return validated structured data, not only prose.

Example:

```json
{
  "summary": "A person approached the front door carrying a cardboard package.",
  "objects": ["person", "package"],
  "actions": ["approaching front door", "carrying package"],
  "eventCategory": "delivery",
  "importance": "normal",
  "confidence": 0.89
}
```

Validate model output using application schemas.

AI-generated claims must remain linked to actual camera events.

Do not let the LLM invent activity.

Use uncertainty-aware language for uncertain classifications.

---

## 16. Natural-language search

Search is a first-class feature.

Example queries:

```text
Who came to the house today?

Show me cars on the driveway yesterday.

Was anyone in the garden last night?

When was a package delivered?

Show people at the front door this week.

What happened while I was away?
```

Architecture:

```text
Question
    ↓
Query parser
    ↓
Extract:
- time range
- cameras
- objects
- event types
- semantic query
    ↓
SQL filters + vector similarity
    ↓
Relevant real events
    ↓
AI-generated answer grounded in those events
```

Every result/answer should link to the underlying event(s).

---

## 17. Search result UI

Example:

```text
SEARCH

"package yesterday"

3 results

────────────────────────────────────

14:31 — Front Doorbell

[ thumbnail ]

Package delivery

A person approached the door carrying
a cardboard package.

[ PLAY EVENT ]

────────────────────────────────────
```

---

## 18. Cross-camera activity correlation

HomeCam should correlate related events from different providers.

Example:

```text
14:31:02
Dahua Driveway
Person detected

14:31:12
Eufy T8210
Person detected

14:31:18
Eufy T8210
Doorbell pressed

14:31:47
Eufy T8210
Additional event
```

Potential normalized activity:

```text
14:31 — Visitor / delivery

A person entered the driveway,
approached the front door and
rang the doorbell.

Cameras:
- Driveway
- Front Doorbell

[ WATCH ACTIVITY ]
```

Correlation must be based on actual timestamps/events.

Never fabricate missing intermediate actions.

Initial correlation should use:

- temporal proximity
- camera sequence
- detected objects
- event categories

More advanced visual matching may be added later.

---

## 19. Activity model

Conceptual model:

```typescript
interface Activity {
  id: string;
  startTime: Date;
  endTime: Date;
  eventIds: string[];
  cameras: string[];

  category:
    | "arrival"
    | "departure"
    | "delivery"
    | "visitor"
    | "vehicle"
    | "unknown";

  summary: string;
  confidence?: number;
}
```

---

## 20. Daily summaries

Generate optional grounded daily summaries.

Example:

```text
TODAY

17 relevant events.

08:14 — Activity at front door.
10:31 — Package delivery.
12:42 — Vehicle entered driveway.
15:37 — Package collected.
18:03 — People arrived home.

No significant overnight activity was identified.
```

Every statement must be traceable to one or more events.

---

## 21. Away mode

Implement:

```text
HOME
AWAY
```

When Away mode starts, persist the timestamp.

When it ends, generate a summary such as:

```text
WHILE YOU WERE AWAY

12:03 → 18:43

37 raw events
4 relevant activities

14:03 — Package delivery
15:41 — Visitor activity
17:12 — Person arrived
18:32 — Animal detected in garden
```

All items link to actual events.

---

## 22. People feature

Design optional support for person/appearance clustering.

Default: disabled.

Potential flow:

```text
person detection
      ↓
appearance embedding
      ↓
cluster
      ↓
Person 01
Person 02
Person 03
```

Do not automatically assign real-world identities.

The user may optionally label clusters.

The entire feature must be disableable.

---

## 23. Notifications

Create configurable notification rules.

Examples:

```text
Notify when:

[x] Doorbell pressed
[x] Package detected
[x] Unknown/unclustered person
[ ] Any person
[ ] Vehicle
[ ] Animal

Cameras:

[x] Front Doorbell
[x] Driveway
[ ] Garden

Schedule:
22:00 → 07:00
```

Prefer descriptive notifications such as:

```text
Front Door — 16:42

A person appears to have left a package
near the front door.
```

instead of:

```text
Motion detected.
```

Use confidence-aware wording.

Notification delivery channels must be abstracted so web push, email, mobile push, etc. can be added later.

---

## 24. Privacy modes

Implement configuration modes:

```text
LOCAL ONLY
HYBRID
CLOUD
```

### LOCAL ONLY

No image/video media leaves the local environment.

### HYBRID

Local detection occurs first.

Only selected event media may be sent to configured cloud AI.

### CLOUD

Configured event media may be processed by cloud AI.

Continuous video must never be sent to cloud AI by default.

---

## 25. Retention

Allow separate retention settings for:

- event metadata
- thumbnails
- AI descriptions
- embeddings
- temporary clips
- logs

Deleting HomeCam metadata must not automatically delete original NVR/HomeBase recordings unless an explicit future feature supports this.

---

## 26. Security requirements

Never expose:

- Dahua username/password
- Eufy credentials
- AI API keys
- RTSP credentials
- database credentials
- signing/session secrets

to frontend JavaScript.

Secrets must never appear in:

- Git
- URLs
- browser localStorage
- frontend bundles
- normal application logs

Provide `.env.example`.

Actual `.env` must be ignored by Git.

Production security should include:

- HTTPS
- secure cookies
- server-side sessions or secure token strategy
- password hashing
- authentication rate limiting
- authorization
- input validation
- CSRF protection where applicable
- audit logging
- secret storage
- secure headers

Never expose Dahua RTSP or NVR management ports directly to the public Internet.

Do not expose the Eufy HomeBase directly to the Internet for HomeCam.

Prevent arbitrary URL/RTSP injection and SSRF in media/provider APIs.

---

## 27. Authentication

Initial local version may use HomeCam-native authentication.

Requirements:

- username/email
- password
- secure password hashing
- session
- logout

Architecture must allow Microsoft Entra ID later.

Entra ID is not required for local development.

---

## 28. Recommended backend

Preferred stack:

```text
Python 3.12+
FastAPI
SQLAlchemy 2.x
Pydantic
Alembic
```

Backend responsibilities:

- REST API
- WebSocket/SSE
- authentication
- provider orchestration
- events
- recordings
- AI orchestration
- semantic search
- activities
- notifications
- health
- settings

---

## 29. Recommended frontend

Use:

```text
Next.js
TypeScript
React
Tailwind CSS
shadcn/ui
```

Use strict TypeScript.

Provider-specific credentials and device integration logic must never live in the frontend.

---

## 30. Database

Use:

```text
PostgreSQL
pgvector
```

Core tables/entities:

```text
users

providers
cameras

events
event_detections
event_ai_analysis

activities
activity_events

recording_references

person_clusters
person_appearances

notification_rules
notifications

ai_jobs

system_health

audit_log

settings
```

Use Alembic migrations.

---

## 31. Event persistence

Minimum event fields:

```text
id UUID
camera_id UUID
provider_id UUID
provider_event_id nullable

type
priority
source

start_time
end_time nullable

thumbnail_path nullable
recording_reference nullable

raw_provider_metadata JSONB

created_at
updated_at
```

---

## 32. AI analysis persistence

Minimum:

```text
id UUID
event_id UUID

provider
model

summary
objects JSONB
actions JSONB

category
confidence

embedding VECTOR

created_at
```

Embedding dimensionality must be configurable/compatible with the selected embedding model.

---

## 33. API

Use versioned routes.

Suggested API:

```text
/api/v1/auth

/api/v1/providers
/api/v1/providers/{id}

/api/v1/cameras
/api/v1/cameras/{id}
/api/v1/cameras/{id}/snapshot
/api/v1/cameras/{id}/live

/api/v1/events
/api/v1/events/{id}

/api/v1/recordings

/api/v1/activities
/api/v1/activities/{id}

/api/v1/search

/api/v1/people

/api/v1/notifications
/api/v1/notification-rules

/api/v1/system/health

/api/v1/settings
```

Generate OpenAPI documentation automatically.

---

## 34. Real-time API

Provide:

```text
/api/v1/ws
```

or a well-documented SSE equivalent.

Potential events:

```text
event.created
event.updated

camera.online
camera.offline

doorbell.ring

activity.created

notification.created

system.warning
```

---

## 35. Media service

Run MediaMTX as a separate service/container.

Responsibilities:

- RTSP ingestion where applicable
- WebRTC delivery
- HLS fallback

FFmpeg may be used for:

- snapshots
- transcoding
- clip generation
- thumbnails
- format conversion

Avoid transcoding when passthrough/remuxing is sufficient.

---

## 36. Background jobs

Use a separate worker process for:

- event processing
- AI jobs
- snapshots
- local object detection
- embeddings
- activity correlation
- summaries
- notifications
- health checks
- retention cleanup

Use a job-queue abstraction.

For local V1, Redis is acceptable.

Choose a simple Python worker framework such as Celery, RQ, or Dramatiq and document the decision.

Do not add complexity without a concrete need.

---

## 37. Docker/local development

Everything necessary for local development should run through Docker Compose where practical.

Expected services:

```text
homecam-web
homecam-api
homecam-worker
homecam-media
postgres
redis
```

Optional:

```text
local-ai
```

Target command:

```bash
docker compose up -d
```

Provide service health checks.

The README must document any host dependencies required for camera networking, GPU acceleration, or development.

---

## 38. Monorepo structure

Use a monorepo.

Suggested layout:

```text
homecam-ai/

├── README.md
├── SPEC.md
├── LICENSE
├── .gitignore
├── .env.example
├── docker-compose.yml
├── Makefile
│
├── apps/
│   ├── web/
│   │   ├── src/
│   │   ├── public/
│   │   └── package.json
│   │
│   └── api/
│       ├── app/
│       │   ├── api/
│       │   ├── auth/
│       │   ├── models/
│       │   ├── schemas/
│       │   ├── services/
│       │   ├── providers/
│       │   │   ├── base/
│       │   │   ├── mock/
│       │   │   ├── dahua/
│       │   │   └── eufy/
│       │   ├── ai/
│       │   ├── events/
│       │   ├── activities/
│       │   ├── search/
│       │   └── workers/
│       ├── migrations/
│       └── tests/
│
├── infrastructure/
│   ├── docker/
│   └── azure/
│
├── docs/
│   ├── architecture.md
│   ├── dahua.md
│   ├── eufy.md
│   ├── ai.md
│   ├── security.md
│   └── deployment.md
│
└── scripts/
```

Copilot may adjust details when there is a good technical reason, but architectural boundaries must remain intact.

---

## 39. Configuration

Use environment variables and typed application configuration.

Example `.env.example`:

```text
APP_ENV=development

DATABASE_URL=
REDIS_URL=

SECRET_KEY=

DAHUA_HOST=
DAHUA_USERNAME=
DAHUA_PASSWORD=

EUFY_USERNAME=
EUFY_PASSWORD=

AI_PROVIDER=mock

OPENAI_API_KEY=
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_DEPLOYMENT=

MEDIAMTX_URL=
```

Never put real credentials in `.env.example`.

Provider credentials should eventually move to an appropriate secret store in production.

---

## 40. Mock provider — mandatory first implementation

Implement:

```text
MockCameraProvider
```

before requiring physical hardware.

Simulate five cameras:

1. Front Door
2. Driveway
3. Backyard
4. Garden
5. Front Doorbell

The fifth camera should behave as:

```text
provider = mock-eufy
type = doorbell
model = T8210
```

Mock capabilities should cover:

- snapshots
- simulated live view
- motion
- person
- vehicle
- doorbell press
- recordings
- online/offline changes
- battery level for doorbell

Generate deterministic or controllable mock events so automated tests remain reliable.

Provide development controls/endpoints for generating mock events where useful.

---

## 41. Testing

Backend:

```text
pytest
```

Frontend:

```text
Vitest
React Testing Library
```

End-to-end:

```text
Playwright
```

Test at minimum:

- authentication
- authorization
- provider discovery
- capability handling
- event normalization
- event persistence
- AI schema validation
- semantic-search plumbing
- notification rules
- camera offline behavior
- provider failure isolation
- doorbell event
- low battery warning
- real-time event update
- API validation

Provider tests must run without physical cameras.

Real-hardware integration tests should be clearly separated and opt-in.

---

## 42. Observability

Implement structured logs containing, where relevant:

```text
timestamp
service
severity
requestId
providerId
cameraId
eventId
```

Never log secrets.

Provide:

```text
/health
/ready
```

Health output should distinguish dependencies and degraded states.

---

## 43. Failure behavior

Handle:

- camera offline
- Dahua NVR offline
- HomeBase offline
- bad credentials
- stream unavailable
- snapshot timeout
- provider timeout
- AI unavailable
- Redis unavailable
- database unavailable
- provider API/protocol change

Example UI:

```text
Front Door        ● Online
Driveway          ● Online
Garden            ● Online
Doorbell          ○ Unavailable

Eufy HomeBase connection unavailable.

[ Retry ]
```

One provider failure must not crash the entire application.

---

## 44. System page

Example:

```text
SYSTEM

Dahua NVR                 ● ONLINE
Eufy HomeBase             ● ONLINE

PostgreSQL                ● ONLINE
Redis                     ● ONLINE
Media Gateway             ● ONLINE
AI Worker                 ● ONLINE

CAMERAS

Front Door                ● ONLINE
Driveway                  ● ONLINE
Backyard                  ● ONLINE
Garden                    ● ONLINE
Front Doorbell            ● ONLINE

NVR STORAGE

██████████████░░░░       71%

EVENT PROCESSING

Last event                 21:47
Last AI analysis           21:47
Queue                       0
```

Only display metrics that are actually available.

---

## 45. GitHub requirements

The project is stored in GitHub from the beginning.

Suggested branch convention:

```text
main
feature/*
fix/*
```

Add GitHub Actions for:

- backend lint
- backend type checks where configured
- backend tests
- frontend lint
- TypeScript checks
- frontend tests
- production builds
- Docker build validation

Do not automatically deploy to Azure during initial development.

Later deployment workflows may include:

```text
deploy-dev
deploy-production
```

Production deployment must require explicit configuration/approval.

Never store production secrets in the repository or workflow files.

---

## 46. Azure future deployment

Do not deploy Azure resources during the initial implementation.

Design for eventual use of suitable Azure services, potentially including:

```text
Azure Container Apps
Azure Container Registry
Azure Database for PostgreSQL
Azure Managed Redis or appropriate Azure Redis offering
Azure Key Vault
Azure OpenAI
Application Insights
Log Analytics
Microsoft Entra ID
```

Actual service selection must be reviewed at deployment time rather than blindly assuming these remain the best/current options.

Infrastructure as code should use:

```text
Bicep
```

Store it under:

```text
/infrastructure/azure
```

Do not create paid Azure resources automatically without explicit instruction.

---

## 47. Home-network connectivity for future Azure deployment

The Dahua NVR and Eufy HomeBase are private home-network devices.

A cloud-hosted backend must not require publicly exposing them.

Design toward a future component:

```text
HomeCam Edge Agent
```

Future architecture:

```text
HOME NETWORK

Dahua NVR ───────┐
                 │
Eufy HomeBase ───┤
                 ▼
          HomeCam Edge Agent
                 │
       secure outbound connection
                 │
                 ▼
              Azure
                 │
                 ▼
             HomeCam
```

The Edge Agent should initiate outbound connectivity.

No inbound public Internet access to the NVR/HomeBase should be required.

Do not build the complete Edge Agent in Phase 0.

Keep provider/service boundaries compatible with moving hardware-facing code to an edge process later.

Consider that media traffic can be large; do not assume all continuous video should transit Azure.

---

## 48. Development phases

### Phase 0 — Repository bootstrap

Build:

- monorepo structure
- README
- SPEC
- `.gitignore`
- `.env.example`
- Docker Compose
- frontend skeleton
- backend skeleton
- PostgreSQL + pgvector
- Redis
- MediaMTX
- Alembic
- provider abstraction
- MockCameraProvider
- health endpoints
- CI

Acceptance:

```bash
docker compose up -d
```

starts a usable development environment.

No real Dahua, Eufy, cloud AI, or Azure dependency is required.

### Phase 1 — Core application using mocks

Build:

- authentication
- responsive application shell
- dashboard
- camera grid
- camera detail/live mock screen
- events timeline
- doorbell event UX
- system page
- settings foundation
- mock recordings
- real-time mock events

The UI must work completely against MockCameraProvider.

### Phase 2 — Dahua integration

Target:

```text
DHI-NVR4204-P-4KS2
```

Implement incrementally:

1. connectivity
2. authentication
3. camera discovery
4. capabilities
5. snapshots
6. live streams
7. events
8. recording search/playback
9. health/storage
10. clip export where supported

Verify each feature against actual hardware.

Document verified integration behavior in `docs/dahua.md`.

### Phase 3 — Eufy integration

Target:

```text
Eufy T8210 + HomeBase 2
```

Before implementation, research and document the currently viable integration mechanism.

Then implement in priority order:

1. authentication/connectivity
2. HomeBase/device discovery
3. status
4. doorbell press events
5. motion/person events
6. thumbnails/snapshots
7. battery
8. recording retrieval
9. live video if reliable
10. two-way audio only after basic integration is stable

Do not make live video a blocker for useful T8210 integration.

Document verified capabilities and limitations in `docs/eufy.md`.

### Phase 4 — AI

Implement:

- local object detector
- AIProvider abstraction
- MockAIProvider
- configured cloud/local provider
- image analysis
- structured AI output
- event descriptions
- embeddings
- pgvector indexing/search

### Phase 5 — Natural-language search

Implement:

- query parsing
- time interpretation
- camera filters
- event filters
- vector search
- result ranking
- grounded answer generation
- links to events/playback

### Phase 6 — Activities

Implement:

- temporal correlation
- cross-camera/provider correlation
- Activity persistence
- activity summaries
- activity UI

### Phase 7 — Intelligence

Implement:

- notification rules
- intelligent notifications
- away mode
- daily summaries
- optional person clustering
- privacy modes
- configurable retention

### Phase 8 — Azure readiness

Review current Azure architecture and implement:

- Bicep
- container deployment definitions
- Key Vault integration
- managed PostgreSQL configuration
- cache/queue configuration
- monitoring
- Azure AI provider configuration
- optional Entra ID
- HomeCam Edge Agent architecture

Do not deploy without explicit user instruction.

---

## 49. Coding standards

Use:

- strict TypeScript
- Python type annotations
- Pydantic validation
- typed provider contracts
- async I/O where appropriate
- dependency injection where useful
- small focused modules
- explicit interfaces
- structured errors
- migrations for schema changes

Avoid:

- giant files
- hard-coded IP addresses
- hard-coded credentials
- provider-specific frontend logic
- untyped arbitrary JSON crossing boundaries
- silent exception swallowing
- global mutable state
- premature microservices

Every external integration should define:

- timeout
- retry policy
- structured error behavior
- logging
- health status

---

## 50. AI/Copilot implementation rules

GitHub Copilot Agent must not attempt to build the entire product in one pass.

For each phase:

1. read `SPEC.md`;
2. inspect the current repository;
3. state the implementation plan;
4. identify uncertainties/decisions;
5. implement the smallest coherent increment;
6. add/update tests;
7. run tests;
8. run lint/type checks;
9. run relevant builds;
10. fix failures;
11. update documentation;
12. summarize what changed;
13. list known limitations;
14. recommend the next increment.

Do not remove working functionality simply to make a new implementation easier.

Do not silently replace real functionality with placeholders.

Do not invent external API behavior.

If an integration cannot yet be verified, implement its interface/mocks and document the blocker instead.

---

## 51. Definition of Done for V1

V1 is complete when:

- HomeCam starts locally using Docker Compose.
- A user can authenticate.
- Dashboard displays configured cameras.
- Dahua cameras can be discovered from the target NVR.
- Dahua snapshots work.
- Dahua live video works.
- Dahua events appear in the unified timeline.
- Dahua recorded footage can be found and opened.
- Eufy T8210 appears as a doorbell provider/device.
- Supported T8210/HomeBase 2 events appear in the unified timeline.
- Doorbell presses receive high-priority treatment.
- T8210 battery/status is shown where integration permits.
- AI can analyze configured events.
- AI output is structured and persisted.
- Events receive searchable descriptions.
- Natural-language search returns real matching events.
- Search results link to relevant events/footage.
- Providers can fail independently.
- Secrets remain server-side.
- Automated tests pass.
- GitHub CI passes.

Azure deployment is not required for V1.

---

## 52. First task for GitHub Copilot Agent

**Do not immediately implement Dahua, Eufy, cloud AI, or Azure.**

Start with Phase 0 and only the minimum Phase 1 foundation necessary to prove the architecture.

Perform:

1. Read this entire `SPEC.md`.
2. Inspect the repository.
3. Present a concise implementation plan before making major changes.
4. Create the monorepo structure.
5. Create the Next.js/TypeScript frontend.
6. Create the FastAPI/Python backend.
7. Configure PostgreSQL + pgvector.
8. Configure Redis.
9. Configure MediaMTX.
10. Configure Docker Compose.
11. Configure Alembic.
12. Create initial database models.
13. Implement the generic CameraProvider abstraction.
14. Implement MockCameraProvider.
15. Simulate:
    - Front Door
    - Driveway
    - Backyard
    - Garden
    - Front Doorbell (mock Eufy T8210)
16. Implement deterministic mock events including:
    - motion
    - person
    - vehicle
    - doorbell press
    - low battery
17. Implement `/health` and `/ready`.
18. Implement basic camera and event API endpoints.
19. Implement a real-time event mechanism.
20. Build the initial responsive dashboard using mock data.
21. Build a basic Events page.
22. Build a basic System page.
23. Add backend tests.
24. Add frontend tests.
25. Add at least one basic end-to-end test if practical.
26. Add GitHub Actions CI.
27. Create/update README with exact local startup instructions.
28. Run tests, linting, type checks, and production builds.
29. Fix failures before declaring the task complete.

Do not connect to real hardware yet.

Do not deploy to Azure.

At completion, report exactly these sections:

```text
IMPLEMENTED

TEST RESULTS

HOW TO RUN

KNOWN LIMITATIONS

NEXT RECOMMENDED TASK
```

---

## 53. Dependency rule

Maintain this dependency direction:

```text
                    HomeCam Core
                         │
             ┌───────────┴───────────┐
             │                       │
       CameraProvider            AIProvider
             │                       │
     ┌───────┼───────┐       ┌──────┼──────┐
     │       │       │       │      │      │
   Dahua   Eufy    Mock    Azure  OpenAI  Local/Mock
```

HomeCam Core must not import implementation-specific provider logic.

The frontend must not contain Dahua/Eufy credentials or direct hardware integration logic.

---

## 54. First prompt to use in VS Code GitHub Copilot

After placing this file at the repository root as `SPEC.md`, use the following prompt in GitHub Copilot Agent mode:

```text
Read SPEC.md completely.

We are starting this project from scratch. Implement ONLY Phase 0 and the minimum Phase 1 foundation specified in SPEC.md.

Before writing code:
1. Inspect the repository.
2. Give me your implementation plan.
3. Identify important technical decisions or uncertainties.
4. Then begin implementation.

Use MockCameraProvider only. Do not connect to my real Dahua DHI-NVR4204-P-4KS2 or Eufy T8210/HomeBase 2 yet.

The application must run locally using Docker Compose.

Run all relevant tests, linting, type checks and builds after implementation. Fix failures before declaring the phase complete.

Do not deploy anything to Microsoft Azure yet.

Do not invent external device API behavior. Follow the architectural boundaries in SPEC.md.
```

---

## 55. Guiding product principle

HomeCam AI is not a Dahua website with an Eufy add-on.

It is a provider-independent home-camera intelligence platform.

Dahua and Eufy are the first two real providers.

All future design and implementation decisions should preserve that distinction.

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str = "development"
    # Scripted demo cameras (``mock-*``). ``None`` means automatic: shown in
    # development, and in production only while no real provider (Dahua or
    # Eufy) is configured, so a live dashboard never mixes fake cameras in
    # with the owner's real ones. ``true``/``false`` force either way.
    mock_cameras_enabled: bool | None = None
    database_url: str = "sqlite+aiosqlite:///./homecam.db"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "development-only"
    media_root: str = "./media"
    cors_origins: str = "http://localhost:3000"
    ai_provider: str = "mock"
    # AI pipeline (SPEC sections 12-15). Defaults keep HomeCam fully local,
    # deterministic and dependency-free; real backends are strictly opt-in.
    ai_detector_backend: str = "mock"
    ai_detector_model_path: str = ""
    # Refuse to start when the requested detector backend cannot be honoured
    # (unknown name, missing model file, missing runtime) instead of running
    # blind on the mock detector. Off by default because a crash-looping
    # security system protects nobody; when off, the degradation is still
    # reported loudly (ERROR log, /ready, /api/v1/system/status).
    ai_detector_strict: bool = False
    # Zero-detection watchdog (incident 2026-09-30). Frames were ingested at
    # 100% success for three hours while the detector - silently replaced by
    # the mock - produced literally nothing, and no signal said so. Keyed on
    # "frames succeeded AND the detector returned no box at all", never on
    # "no events", because events are legitimately suppressed for parked
    # vehicles during quiet periods.
    detector_watchdog_enabled: bool = True
    detector_blackout_window_seconds: float = Field(default=2700.0, gt=0.0)
    # Frames that must have been detected on within the window before
    # silence counts as evidence; a camera that delivered three frames in an
    # hour is an acquisition problem, not a detector blackout.
    detector_blackout_min_frames: int = Field(default=60, ge=1)
    ai_analysis_enabled: bool = True
    embedding_dimensions: int = Field(default=384, ge=8, le=4096)
    best_photo_frames: int = Field(default=3, ge=1, le=10)
    best_photo_enabled: bool = True
    # Person identity / re-identification (Azure AI Foundry backed).
    #
    # ``foundry_endpoint``/``foundry_api_key`` point at an Azure AI Services
    # ("AI Foundry") account. Two *separate* capabilities of that one account
    # are used, and each degrades independently:
    #
    #  * multimodal image embeddings (``/computervision/retrieval:vectorizeImage``)
    #    turn a person crop into a vector used to recognize the same person on
    #    a later visit. Without it, a deterministic local embedding is used so
    #    the feature still works offline/in tests (it just cannot generalize
    #    across lighting/pose the way the real model does).
    #  * a vision chat deployment used to caption the crop in plain language
    #    ("a person in a dark jacket at the front door"), because a bare
    #    cropped image with no words is not "understandable" on its own.
    foundry_endpoint: str | None = None
    foundry_api_key: str | None = None
    foundry_vision_deployment: str = "homecam-vision"
    foundry_vision_api_version: str = "2024-10-21"
    foundry_embedding_api_version: str = "2024-02-01"
    foundry_embedding_model_version: str = "2023-04-15"
    foundry_timeout_seconds: float = Field(default=20.0, gt=0)
    person_recognition_enabled: bool = True
    # How long a camera-discovery sweep is reused by frequently-polled
    # callers (``GET /cameras`` and the ingestion loop). Discovery reaches
    # through to the Dahua NVR, which only sustains ~1-2 concurrent CGI
    # sessions, so re-discovering on every browser poll exhausted it and
    # made it report *all* channels offline for as long as the edge
    # connector cached that failure.
    camera_discovery_cache_seconds: float = Field(default=10.0, ge=0.0)
    # Cosine similarity above which a new sighting is considered the *same*
    # person as an existing identity.
    #
    # Measured against the deployed Azure multimodal embedder using tight
    # subject crops: the same subject under a lighting change or a small
    # position shift scored 0.984-0.989, while visibly different subjects in
    # the same scene scored 0.857-0.934. 0.96 sits in that gap.
    #
    # Note how high the floor is: nothing measured scored below 0.85, so the
    # intuitive-looking 0.86 would have merged every visitor into a single
    # identity. These vectors describe whole images, not faces, so absolute
    # similarity runs high and only the margin is meaningful.
    #
    # Erring high is deliberate: an unrecognised returning visitor is a minor
    # annoyance the user can fix by naming them, whereas two people merged
    # into one identity is a wrong answer they may never notice.
    person_match_threshold: float = Field(default=0.96, gt=0.0, le=1.0)
    # Never keep more than this many example vectors per person; the centroid
    # plus a bounded sample list keeps matching cheap and storage predictable.
    person_max_samples: int = Field(default=25, ge=1, le=500)
    person_caption_enabled: bool = True
    # Structured appearance analysis (apparent age band, clothing, carried
    # items, face visibility) plus a "is a person really there" second
    # opinion used to verify detection borders. Uses the same Foundry vision
    # deployment as captioning. Deliberately does not infer ethnicity or
    # gender; see :mod:`app.ai.appearance` for why.
    appearance_analysis_enabled: bool = True
    # Cosine similarity at or above which two *identities* (not two
    # sightings) are considered the same person and merged when the user
    # names one of them. Kept at the same floor as ``person_match_threshold``
    # rather than loosened: merging is harder to notice and harder to undo
    # than a missed match, so extra evidence comes from comparing whole
    # clusters, not from lowering the bar.
    person_merge_threshold: float = Field(default=0.96, gt=0.0, le=1.0)
    # Animal species + breed identification (SPEC 13 animal category). Uses
    # the same Foundry vision deployment as captioning; without Foundry the
    # event still reports the detected species class, just no breed.
    animal_identification_enabled: bool = True
    zone_min_overlap: float = Field(default=0.3, gt=0.0, le=1.0)
    parked_vehicle_seconds: float = Field(default=60.0, gt=0.0)
    audio_detection_enabled: bool = False
    audio_energy_threshold: float = Field(default=0.02, gt=0.0, le=1.0)
    activity_correlation_enabled: bool = True
    activity_correlation_window_seconds: float = Field(default=120.0, gt=0.0)
    # Continuous event ingestion (SPEC section 12): periodically snapshots
    # every online camera and runs it through the configured detector so
    # events exist without a human manually POSTing /mock/events. Disabled
    # by default (matches every other opt-in AI knob above) so tests/local
    # dev never spawn a background polling loop unexpectedly; the deployed
    # Azure API turns this on explicitly.
    event_ingestion_enabled: bool = False
    # Minimum spacing between CGI snapshots of one camera by the ingestion
    # loop. Cameras with a live sub-stream (see ``stream_frames_*``) only use
    # snapshots as a fallback, still at most this often, so a stream outage
    # never turns into more load on a session-limited NVR than before.
    event_poll_interval_seconds: float = Field(default=20.0, gt=0.0)
    event_cooldown_seconds: float = Field(default=120.0, gt=0.0)
    # Stream-based frame source (incident fix). A Dahua NVR sustains only
    # ~1-2 concurrent CGI sessions and refused 69-82% of 4K snapshot.cgi
    # requests in production, so a camera yielded one usable frame per
    # ~100s and people crossing in 5-10s were never sampled. Frames are read
    # instead from the H.264 sub-stream (704x576) the edge already relays via
    # MediaMTX HLS, which costs no extra NVR CGI sessions.
    stream_frames_enabled: bool = True
    # How often each camera is sampled from its stream (and the ingestion
    # loop tick). RT-DETR r18 costs ~225ms of CPU per frame and decoding a
    # 2s sub-stream segment ~100ms, so 4s with four cameras is ~0.33 of a
    # core, inside the API's 0.5 vCPU while still sampling anyone in view
    # for >=4s at least once. Cooldowns still bound events (and so Foundry
    # calls, which are per event, never per frame).
    stream_sample_interval_seconds: float = Field(default=4.0, gt=0.0)
    # A cached stream frame older than this is stale: ingestion falls back
    # to a CGI snapshot (rate limited by event_poll_interval_seconds).
    stream_frame_max_age_seconds: float = Field(default=15.0, gt=0.0)
    # A reader nobody has asked for a frame within this long stops, so a
    # camera that went away does not keep an HLS session open forever.
    stream_reader_idle_seconds: float = Field(default=300.0, gt=0.0)
    # Display aspect (width/height) for stream frames. Dahua sub-streams are
    # anamorphic (704x576 of a 16:9 scene, no SAR flag); when unset the
    # aspect is learned from the camera's last real snapshot.
    stream_frame_aspect_ratio: float | None = Field(default=None, gt=0.0)
    # Stationary-object suppression: a new event for the same camera and
    # subject whose every box matches (IoU / containment >= threshold) a box
    # of the last emitted event is a repeat of an object that has not moved
    # (a parked car) and is suppressed for this long. People are never
    # suppressed this way.
    stationary_suppress_seconds: float = Field(default=1800.0, ge=0.0)
    stationary_iou_threshold: float = Field(default=0.8, gt=0.0, le=1.0)
    stationary_subjects: str = "vehicle,animal,package"
    # While a known stationary object is still in view, an unmatched box
    # only counts as a new object at or above this confidence. On the live
    # front-yard camera the unmatched boxes beside the parked car were all
    # 0.51-0.65 (far street traffic and flicker at the frame edge). Each one
    # re-emitted an event whose best photo was the parked car. A nearby
    # arriving car scores far higher.
    stationary_new_object_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    # How often per-camera frame acquisition statistics are logged.
    ingestion_stats_log_seconds: float = Field(default=300.0, gt=0.0)
    # Persistent scene state (see app/services/scene_state.py). Vehicles are
    # tracked across frames and restarts instead of re-emitted on a cooldown:
    # one event when a vehicle is confirmed, none while it stays parked, and
    # one per supported transition (moved / departed / returned / a person
    # at the vehicle).
    vehicle_tracking_enabled: bool = True
    # A new track needs at least this confidence; weaker boxes can only
    # continue an existing track. Street traffic and edge flicker on the live
    # front-yard camera scored 0.51-0.65.
    vehicle_new_track_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    # Matching observations before a track is reported (an arrival event).
    # 2 means a car driving past, seen in one sample, is not an "arrival".
    vehicle_confirm_observations: int = Field(default=2, ge=1, le=20)
    # Matching observations in the same place before a vehicle is parked;
    # from then on it emits nothing until something changes.
    vehicle_stable_observations: int = Field(default=5, ge=2, le=100)
    # IoU with the track's anchor box counted as "same place". Box jitter
    # on a parked car at 704x576 stays well above this.
    vehicle_stable_iou: float = Field(default=0.7, gt=0.0, le=1.0)
    # IoU (or containment) at which a box can belong to a track at all.
    vehicle_match_iou: float = Field(default=0.3, gt=0.0, le=1.0)
    # Unseen for this long, while the camera was actually delivering frames,
    # counts as departed. Camera outages do not count toward it.
    vehicle_absence_seconds: float = Field(default=180.0, gt=0.0)
    # A departed vehicle seen again in the same place within this window,
    # with a compatible appearance, is reported as returned.
    vehicle_return_window_seconds: float = Field(default=86400.0, ge=0.0)
    # Appearance (colour signature) is checked when a track is re-associated
    # after a gap at least this long; consecutive samples seconds apart are
    # the same car whatever the lighting does.
    vehicle_appearance_gap_seconds: float = Field(default=60.0, ge=0.0)
    vehicle_appearance_min_similarity: float = Field(default=0.5, ge=0.0, le=1.0)
    # A person overlapping a reported vehicle for this many consecutive
    # samples is an interaction; re-reported at most once per cooldown.
    vehicle_interaction_observations: int = Field(default=2, ge=1, le=20)
    vehicle_interaction_cooldown_seconds: float = Field(default=600.0, ge=0.0)
    # No frame from a camera for this long is an outage: absence timers and
    # zone comparisons restart instead of treating the gap as evidence.
    scene_outage_seconds: float = Field(default=60.0, gt=0.0)
    # Mailbox delivery (zones of kind "mailbox"; nothing happens without one).
    mailbox_delivery_enabled: bool = True
    # Samples a person must overlap the mailbox for; a walk-by is one.
    mailbox_min_observations: int = Field(default=2, ge=1, le=20)
    # Share of the mailbox rectangle a person box must cover.
    mailbox_min_zone_overlap: float = Field(default=0.3, gt=0.0, le=1.0)
    # Samples without the person before a visit is over.
    mailbox_end_after_misses: int = Field(default=2, ge=1, le=20)
    # One delivery event per this window, however many visits.
    mailbox_dedupe_seconds: float = Field(default=900.0, ge=0.0)
    # Bins placed out / emptied (zones of kind "bin"; nothing without one).
    bin_detection_enabled: bool = True
    # Seconds between region comparisons (a bin does not move quickly).
    bin_check_interval_seconds: float = Field(default=20.0, gt=0.0)
    # Normalized region difference counted as a change, and how many
    # consecutive unoccluded checks must agree before it is a candidate.
    bin_change_threshold: float = Field(default=0.35, gt=0.0)
    bin_change_confirm_checks: int = Field(default=3, ge=1, le=20)
    # How long an interaction (person / collection vehicle at the bin)
    # remains evidence for a following change.
    bin_interaction_window_seconds: float = Field(default=900.0, gt=0.0)
    bin_dedupe_seconds: float = Field(default=3600.0, ge=0.0)
    # Explicit opt-in rule: a present bin that disappears while the camera
    # was continuously observing, right after a collection vehicle, counts
    # as emptied. Off: disappearance alone never means emptied.
    bin_removal_counts_as_emptied: bool = False
    # Detector labels that directly evidence a bin in the zone. RT-DETR's
    # COCO classes have none, so empty by default.
    bin_local_labels: str = ""
    # Mailbox/bin questions to the Foundry vision deployment are only asked
    # on candidate sequences, and at most this often per zone.
    scene_verifier_enabled: bool = True
    scene_verifier_min_interval_seconds: float = Field(default=120.0, ge=0.0)
    mediamtx_url: str = "http://localhost:8889"
    # Own public FQDN (e.g. the Container App's https://... ingress URL). Used
    # to rewrite private-only (Tailscale tailnet / container-localhost) live
    # stream URLs into a public HLS proxy path a real browser can reach.
    public_api_base_url: str | None = None
    low_battery_threshold: int = 20
    session_ttl_minutes: int = 60 * 12
    dahua_enabled: bool = False
    dahua_scheme: str = "http"
    dahua_host: str | None = None
    dahua_port: int = 80
    dahua_username: str | None = None
    dahua_password: str | None = None
    dahua_serial: str = "5J006FCPAZ6B52A"
    dahua_channels: str = ""
    dahua_timeout_seconds: float = Field(default=5.0, gt=0)
    dahua_retries: int = Field(default=1, ge=0, le=5)
    # Edge connector mode (Home Assistant/Raspberry Pi bridge, see
    # docs/edge-connector.md). "direct" keeps the existing behavior above;
    # "edge" talks to a local edge connector over a private VPN/overlay
    # (Tailscale recommended) instead of the raw Dahua HTTP/RTSP surface.
    dahua_mode: str = "direct"
    dahua_edge_url: str | None = None
    dahua_edge_token: str | None = None
    dahua_edge_timeout_seconds: float = Field(default=5.0, gt=0)
    dahua_edge_retries: int = Field(default=1, ge=0, le=5)
    eufy_enabled: bool = False
    eufy_adapter_url: str | None = None
    eufy_adapter_token: str | None = None
    eufy_timeout_seconds: float = Field(default=10.0, gt=0)
    eufy_retries: int = Field(default=1, ge=0, le=5)
    # --- Security essentials (arming modes, incidents, audit, auth hardening) ---
    # New alert-worthy events for the same camera+zone+kind merge into an
    # already-open incident within this window instead of raising a
    # duplicate (detection dedup at the *actionable* layer; the underlying
    # Event rows are never deduplicated/deleted).
    incident_merge_window_seconds: float = Field(default=300.0, gt=0.0)
    # An open, unacknowledged incident is escalated (bumped a severity/urgency
    # level, re-broadcast, and audit-logged) after this long unattended. A
    # physical alarm panel keeps sounding until someone silences it; this is
    # the software equivalent, without ever paging/dispatching anyone.
    incident_escalation_seconds: float = Field(default=180.0, gt=0.0)
    # No more than this many escalation bumps per incident, so an incident
    # nobody ever acknowledges settles at a high-but-bounded severity instead
    # of growing without limit.
    incident_max_escalation_level: int = Field(default=3, ge=0, le=10)
    # AI-assisted incident risk summary (Foundry chat deployment). Purely
    # descriptive/advisory text alongside the deterministic summary -
    # disabled it changes nothing about whether/when an incident is raised,
    # only whether it also carries a plain-language summary.
    incident_ai_summary_enabled: bool = True
    # Camera health watchdog (deterministic pixel heuristics, see
    # app/ai/camera_health.py - never a learned model). A frame whose
    # standard deviation of luminance falls below this is flat/uniform
    # enough to be a lens covered/blocked, not a real scene.
    camera_obstruction_std_threshold: float = Field(default=6.0, ge=0.0)
    # Consecutive obstruction-looking samples required before raising an
    # incident, so one dark/glare frame is not a false tamper alert.
    camera_obstruction_confirm_samples: int = Field(default=3, ge=1, le=50)
    # Two frames whose mean pixel difference is below this are "the same"
    # for tamper/frozen-feed purposes (compression noise still varies a real
    # scene by more than this).
    camera_frozen_diff_threshold: float = Field(default=1.5, ge=0.0)
    # A camera reporting frames that are all "the same" for at least this
    # long is flagged as a frozen/tampered feed. Long and conservative on
    # purpose: outdoor cameras pointed at a quiet scene can look static for
    # minutes at a time and must not misfire.
    camera_frozen_seconds: float = Field(default=1800.0, gt=0.0)
    # A camera-health incident is re-raised for the same camera at most once
    # per this window after it resolves, so a flapping camera does not spam
    # the incident feed.
    camera_health_dedupe_seconds: float = Field(default=1800.0, ge=0.0)
    # Auth hardening (OWASP ASVS / IoT Top 10 brute-force guidance). After
    # this many consecutive failed logins for one account, further attempts
    # are refused (regardless of password correctness) until the lockout
    # expires, independent of whether the guessed password is eventually
    # correct.
    auth_max_failed_attempts: int = Field(default=5, ge=1, le=100)
    auth_lockout_minutes: float = Field(default=15.0, gt=0.0)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()

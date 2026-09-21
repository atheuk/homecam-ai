"""Provider-independent AI/detection pipeline (SPEC sections 12-15, 18-19).

Everything in this package consumes *normalized* HomeCam data only: snapshot
bytes, normalized event dicts and camera ids. No module here may import a
provider-specific client or data structure, so the same pipeline runs
identically for Dahua NVR channels, the Eufy T8210 doorbell and the mock
providers.
"""

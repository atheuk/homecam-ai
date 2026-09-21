"""Minimal running-stack smoke test: python scripts/smoke.py [base_url]."""
import sys
import httpx
base=sys.argv[1] if len(sys.argv)>1 else "http://localhost:8000"
r=httpx.get(base+"/api/v1/system/health",timeout=5); r.raise_for_status()
assert r.json()["status"]=="ok"
c=httpx.get(base+"/api/v1/cameras",timeout=5); c.raise_for_status()
assert len(c.json())==5
print("HomeCam smoke test passed")

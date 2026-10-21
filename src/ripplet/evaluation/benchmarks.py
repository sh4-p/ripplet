from __future__ import annotations
import time
def time_index(fn):
    t0=time.time(); r=fn(); return {"seconds": round(time.time()-t0,3), "result": r}

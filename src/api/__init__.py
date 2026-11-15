"""Local, authenticated HTTP case API (FastAPI) over the authorized case operations.

Every protected operation goes through ``src.authorized_case_operations``; this
package only parses HTTP, passes the raw bearer credential to the layer, and
maps typed outcomes to safe HTTP responses. It is checked by
``scripts/check_case_access_boundary.py``. Localhost demonstration only.
"""

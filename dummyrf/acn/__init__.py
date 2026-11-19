"""ACN (ANSI E1.17) — the control protocol Shure Wireless Workbench uses.

Shure receivers expose far more over ACN than over their command-string port,
including RF scanning on SLX-D.  This package implements just enough of it —
the SDT session layer and DMP property access — worked out from captures of
WWB talking to an SLXD4D (see ``slxd.py`` for the property map).
"""

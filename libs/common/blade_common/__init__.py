"""Shared code for every blade service: message contract, NATS helpers, timing, KLV, diag."""
from .contracts import CONTRACT_VERSION, Envelope

__all__ = ["CONTRACT_VERSION", "Envelope"]

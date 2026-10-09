"""Screen capture with DXGI desktop duplication (Windows only)."""
from __future__ import annotations

import numpy as np


class DxcamScreen:
    def __init__(self, output: int = 0):
        import dxcam

        self.camera = dxcam.create(output_idx=output, output_color="BGR")

    def grab(self) -> np.ndarray | None:
        return self.camera.grab()

    def close(self) -> None:
        del self.camera
